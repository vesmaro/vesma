"""Install/uninstall flow tests (layout v1 §3.5/§3.8, service-lifecycle §3.6).

LY-02 (drop-in discipline: atomic per-file writes, siblings untouched,
stray file fails the load), LY-05 (exactly one venv per python child),
LY-08 (exact == pins, URL requirements rejected), unit write + daemon-
reload semantics, idempotent reinstall, uninstall scoping.

The real venv/pip leg is replaced by a fake installer in the fixture —
no test ever touches PyPI or the host user systemd bus.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vesmaro.service import install as install_mod
from vesmaro.service import layout, unitgen
from vesmaro.service.errors import CLAMP_VIOLATION, MANIFEST_SCHEMA_INVALID, ManifestError
from vesmaro.service.install import InstallError, install, uninstall


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate every XDG root + HOME into a tmp dir (empty vars = defaults)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture(autouse=True)
def no_host_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never talk to the host user systemd bus from tests."""
    monkeypatch.setattr(install_mod, "_daemon_reload", lambda: "ok (stubbed)")
    monkeypatch.setattr(
        install_mod, "_systemctl_user", lambda args: f"ok (stubbed: {' '.join(args)})"
    )


@pytest.fixture
def fake_venv_installer(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the venv/pip leg: create the dir + a full-freeze lock file.

    The lock mirrors the real behavior: after installing exactly
    ``vesma==<version>`` the lock records the full freeze (vesma + pip).
    """
    created: list[str] = []
    from vesmaro import __version__

    def fake_install_component_venv(manifest, report):  # type: ignore[no-untyped-def]
        name = manifest.name
        created.append(name)
        venv_dir = layout.component_venv_dir(name)
        venv_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(venv_dir, 0o700)
        bin_dir = venv_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / "python").write_text("#!/bin/true\n", encoding="utf-8")
        lock_path = layout.data_dir(name) / "requirements-lock.txt"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(f"pip==99.0\nvesma=={__version__}\n", encoding="utf-8")
        report.append(f"venv: {name} -> {venv_dir} (lock: {lock_path})")
        return venv_dir

    monkeypatch.setattr(install_mod, "_install_component_venv", fake_install_component_venv)
    return created


@pytest.fixture
def installed(isolated_home: Path, fake_venv_installer: list[str]) -> Path:
    install()
    return isolated_home


# ── Happy path (LY-01/LY-02/LY-05) ────────────────────────────────────


class TestInstall:
    def test_installs_both_bundled_manifests(self, installed: Path) -> None:
        components = layout.components_dir()
        assert sorted(p.name for p in components.iterdir()) == ["board.yaml", "metrics.yaml"]

    def test_canonical_dirs_with_explicit_modes(self, installed: Path) -> None:
        assert layout.verify_dir(layout.config_root(), 0o700)
        assert layout.verify_dir(layout.components_dir(), 0o700)
        assert layout.verify_dir(layout.env_dir(), 0o700)
        assert layout.verify_dir(layout.data_root(), 0o700)
        assert layout.verify_dir(layout.data_root() / "venvs", 0o700)
        assert layout.verify_dir(layout.state_root(), 0o700)
        assert layout.verify_dir(layout.history_dir(), 0o700)
        assert layout.verify_dir(layout.run_fallback_dir(), 0o700)
        assert layout.verify_dir(layout.cache_base(), 0o750)

    def test_exactly_one_component_venv_ly05(
        self, installed: Path, fake_venv_installer: list[str]
    ) -> None:
        # board is in-process (engine venv, no component venv by design);
        # metrics is the python child — exactly one venv dir.
        assert fake_venv_installer == ["metrics"]
        venvs_root = layout.data_root() / "venvs"
        assert [p.name for p in venvs_root.iterdir()] == ["metrics"]
        assert layout.verify_dir(venvs_root / "metrics", 0o700)

    def test_data_dirs_and_schema(self, installed: Path) -> None:
        board_data = layout.data_dir("board")
        assert layout.verify_dir(board_data, 0o700)
        schema = board_data / "config.schema.json"
        assert schema.exists()  # board carries schema_inline
        assert '"bind"' in schema.read_text(encoding="utf-8")
        assert not (layout.data_dir("metrics") / "config.schema.json").exists()

    def test_lock_file_written_for_python_child(self, installed: Path) -> None:
        from vesmaro import __version__

        lock = layout.data_dir("metrics") / "requirements-lock.txt"
        assert lock.exists()
        for line in lock.read_text(encoding="utf-8").splitlines():
            assert "==" in line  # every line an exact pin (LY-08)
        assert f"vesma=={__version__}" in lock.read_text(encoding="utf-8")

    def test_unit_written_0644_with_contract_content(self, installed: Path) -> None:
        unit_path = installed / ".config/systemd/user/vesma.service"
        assert unit_path.exists()
        assert oct(unit_path.stat().st_mode & 0o777) == oct(0o644)
        text = unit_path.read_text(encoding="utf-8")
        active, _ = unitgen.parse_unit(text)
        assert active["ExecStart"].endswith("/bin/vesma service run")
        assert active["KillMode"] == "mixed"
        assert "ExecStop" not in active

    def test_report_lines_are_plain_text(self, installed: Path) -> None:
        result = install()
        assert all(isinstance(line, str) and line for line in result.lines)
        assert any(line.startswith("validation:") for line in result.lines)
        assert result.daemon_reload.startswith("ok")


# ── Container downgrade path ──────────────────────────────────────────


class TestInstallContainer:
    def test_container_downgrade_is_loud_and_marked(
        self, isolated_home: Path, fake_venv_installer: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(install_mod.unitgen, "container_detect", lambda *a, **k: True)
        result = install()
        assert any(line.startswith("CONTAINER DOWNGRADE") for line in result.lines)
        assert set(result.downgraded) == set(unitgen.DOWNGRADE_ALLOWED)
        unit_path = isolated_home / ".config/systemd/user/vesma.service"
        text = unit_path.read_text(encoding="utf-8")
        active, _ = unitgen.parse_unit(text)
        assert "ProtectSystem" not in active  # downgraded
        assert unitgen.parse_downgrade_marker(text) == sorted(unitgen.DOWNGRADE_ALLOWED)

    def test_bare_host_full_hardening(
        self, isolated_home: Path, fake_venv_installer: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(install_mod.unitgen, "container_detect", lambda *a, **k: False)
        result = install()
        assert result.downgraded == ()
        unit_path = isolated_home / ".config/systemd/user/vesma.service"
        active, _ = unitgen.parse_unit(unit_path.read_text(encoding="utf-8"))
        assert active["ProtectSystem"] == "strict"
        assert active["ReadOnlyPaths"].endswith("venvs")


# ── Fail-closed validation (LY-02) + clamps (SL-18) ──────────────────


class TestInstallValidation:
    def test_stray_file_in_components_d_fails_closed(
        self, isolated_home: Path, fake_venv_installer: list[str]
    ) -> None:
        components = layout.ensure_dir(layout.components_dir(), 0o700)
        (components / "notes.txt").write_text("stray", encoding="utf-8")
        with pytest.raises(ManifestError) as excinfo:
            install()
        assert excinfo.value.code == MANIFEST_SCHEMA_INVALID
        assert "notes.txt" in str(excinfo.value)
        # The load fails BEFORE any venv/data-dir side effect: only the
        # bundled manifests + the stray file are in components.d.
        assert sorted(p.name for p in components.iterdir()) == [
            "board.yaml",
            "metrics.yaml",
            "notes.txt",
        ]
        assert not (layout.data_root() / "venvs" / "metrics").exists()

    def test_out_of_clamp_restart_override_surfaces_loader_error(
        self, isolated_home: Path, fake_venv_installer: list[str]
    ) -> None:
        components = layout.ensure_dir(layout.components_dir(), 0o700)
        bad = components / "worker.yaml"
        bad.write_text(
            "apiVersion: vesma.component/v1\n"
            "kind: child-process\n"
            "metadata:\n"
            "  name: worker\n"
            "  version: 1.0.0\n"
            "  tier: optional\n"
            "  description: bad clamps\n"
            "  provenance:\n"
            "    repo: https://example.com/worker\n"
            "    license: MIT\n"
            "launch:\n"
            "  argv:\n"
            '    - "{venv_bin}/python"\n'
            "    - -m\n"
            "    - worker\n"
            "stop:\n"
            "  signal: SIGTERM\n"
            "  grace_period: 5s\n"
            "restart:\n"
            "  backoff:\n"
            "    base: 500ms\n"
            "    max: 10m\n"
            "    reset_after: 1h\n",
            encoding="utf-8",
        )
        with pytest.raises(ManifestError) as excinfo:
            install()
        assert excinfo.value.code == CLAMP_VIOLATION

    def test_unit_downgrades_never_touch_restart(self) -> None:
        with pytest.raises(unitgen.UnitGenerationError):
            unitgen.generate(
                home=Path("/home/x"),
                engine_venv=Path("/home/x/v"),
                read_write_paths=(),
                read_only_paths=(),
                downgraded={"Restart"},
            )


# ── Idempotency + sibling safety (LY-02) ─────────────────────────────


class TestIdempotency:
    def test_reinstall_is_idempotent(self, installed: Path) -> None:
        first_unit = (installed / ".config/systemd/user/vesma.service").read_text(encoding="utf-8")
        second = install()
        assert second.unit_path == installed / ".config/systemd/user/vesma.service"
        assert (
            second.unit_path.read_text(encoding="utf-8") == first_unit
        )  # deterministic regeneration

    def test_hand_edited_unit_is_overwritten_on_reinstall(self, installed: Path) -> None:
        unit_path = installed / ".config/systemd/user/vesma.service"
        unit_path.write_text(
            unit_path.read_text(encoding="utf-8").replace("KillMode=mixed", "KillMode=none"),
            encoding="utf-8",
        )
        install()
        active, _ = unitgen.parse_unit(unit_path.read_text(encoding="utf-8"))
        assert active["KillMode"] == "mixed"  # regenerated

    def test_sibling_manifest_untouched(
        self, isolated_home: Path, fake_venv_installer: list[str]
    ) -> None:
        components = layout.ensure_dir(layout.components_dir(), 0o700)
        custom = components / "custom.yaml"
        custom.write_text("custom: operator file\n", encoding="utf-8")
        # install must fail (fail-closed: every file must be a valid manifest)
        with pytest.raises(ManifestError):
            install()
        assert custom.read_text(encoding="utf-8") == "custom: operator file\n"


# ── Uninstall scoping ────────────────────────────────────────────────


class TestUninstall:
    def test_uninstall_single_component_scope(self, installed: Path) -> None:
        env_file = layout.env_file_path("metrics")
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("METRICS_TOKEN=x\n", encoding="utf-8")
        os.chmod(env_file, 0o600)

        result = uninstall("metrics")

        assert not (layout.components_dir() / "metrics.yaml").exists()
        assert not env_file.exists()
        assert not (layout.data_root() / "venvs" / "metrics").exists()
        # board untouched; data dirs preserved (operator data).
        assert (layout.components_dir() / "board.yaml").exists()
        assert layout.data_dir("metrics").is_dir()
        assert any("data dir preserved" in line for line in result.lines)

    def test_uninstall_refuses_unknown_component(self, installed: Path) -> None:
        with pytest.raises(InstallError, match="not installed"):
            uninstall("ghost")

    def test_uninstall_refuses_when_dependent_exists(self, installed: Path) -> None:
        # hand-add a valid dependent manifest (schema-valid, depends on board)
        components = layout.components_dir()
        (components / "panel.yaml").write_text(
            "apiVersion: vesma.component/v1\n"
            "kind: in-process\n"
            "metadata:\n"
            "  name: panel\n"
            "  version: 1.0.0\n"
            "  tier: optional\n"
            "  description: dependent\n"
            "  provenance:\n"
            "    repo: https://example.com/panel\n"
            "    license: MIT\n"
            "in_process:\n"
            "  module: panel.mod\n"
            "  entrypoint: run\n"
            "depends_on:\n"
            "  - board\n",
            encoding="utf-8",
        )
        with pytest.raises(InstallError, match="required by"):
            uninstall("board")
        # board itself survives.
        assert (components / "board.yaml").exists()

    def test_uninstall_all_removes_unit_and_components(self, installed: Path) -> None:
        result = uninstall(remove_all=True)
        components = layout.components_dir()
        assert list(components.iterdir()) == []  # every install-owned file gone
        assert not (installed / ".config/systemd/user/vesma.service").exists()
        assert not (layout.data_root() / "venvs" / "metrics").exists()
        # data dirs preserved
        assert layout.data_dir("board").is_dir()
        assert layout.data_dir("metrics").is_dir()
        assert any("disable --now" in line for line in result.lines)

    def test_uninstall_without_installation_refuses(self, isolated_home: Path) -> None:
        with pytest.raises(InstallError, match="no installation found"):
            uninstall(remove_all=True)

    def test_uninstall_name_and_all_mutually_exclusive(self, installed: Path) -> None:
        with pytest.raises(InstallError, match="either a component name or --all"):
            uninstall("board", remove_all=True)
        with pytest.raises(InstallError, match="either a component name or --all"):
            uninstall()


# ── Requirement-pin policy (LY-08) ───────────────────────────────────


class TestPinPolicy:
    @pytest.mark.parametrize(
        "line",
        [
            "requests @ https://evil.example/x.whl",
            "https://evil.example/x.whl",
            "file:///tmp/x.whl",
            "requests>=2.0",
            "requests",
            "-i https://evil.example/simple",
        ],
    )
    def test_non_pin_requirements_rejected(self, line: str) -> None:
        with pytest.raises(InstallError, match="exact 'name==version' pin"):
            install_mod._validate_pin(line)

    def test_exact_pins_accepted(self) -> None:
        install_mod._validate_pin("vesma==5.4.0")
        install_mod._validate_pin("pydantic==2.14.2")

    def test_bundled_requirements_use_engine_package(self) -> None:
        from vesmaro import __version__

        assert install_mod._bundled_requirements("metrics") == (f"vesma=={__version__}",)
        assert install_mod._bundled_requirements("board") == ()

    def test_unknown_python_child_component_refused(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A venv-referencing manifest outside the v1 pack fails closed
        (no silent empty venv) — exercised against the REAL installer."""
        components = layout.ensure_dir(layout.components_dir(), 0o700)
        (components / "worker.yaml").write_text(
            "apiVersion: vesma.component/v1\n"
            "kind: child-process\n"
            "metadata:\n"
            "  name: worker\n"
            "  version: 1.0.0\n"
            "  tier: optional\n"
            "  description: unknown child\n"
            "  provenance:\n"
            "    repo: https://example.com/worker\n"
            "    license: MIT\n"
            "launch:\n"
            "  argv:\n"
            '    - "{venv_bin}/python"\n'
            "    - -m\n"
            "    - worker\n"
            "stop:\n"
            "  signal: SIGTERM\n"
            "  grace_period: 5s\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(install_mod.unitgen, "container_detect", lambda *a, **k: False)
        with pytest.raises(InstallError, match="no requirement set"):
            install()
