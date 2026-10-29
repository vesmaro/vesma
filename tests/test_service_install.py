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

from vesma.service import install as install_mod
from vesma.service import layout, unitgen
from vesma.service.errors import CLAMP_VIOLATION, MANIFEST_SCHEMA_INVALID, ManifestError
from vesma.service.install import InstallError, install, uninstall
from vesma.service.manifest import bundled_manifest_path, load_bundled_manifest


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
    from vesma import __version__

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
        # The full DR-01 table (generation-aware via the resolver).
        for path, mode in layout.canonical_dir_specs():
            assert layout.verify_dir(path, mode), f"{path} must be {mode:04o}"
        if layout.detect_generation() is layout.LayoutGeneration.V1_5:
            assert layout.verify_dir(layout.state_root(), 0o700)

    def test_exactly_one_component_venv_ly05(
        self, installed: Path, fake_venv_installer: list[str]
    ) -> None:
        # board is in-process (engine venv, no component venv by design);
        # metrics is the python child — exactly one venv dir.
        assert fake_venv_installer == ["metrics"]
        venvs_root = layout.component_venvs_root()
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
        from vesma import __version__

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
        assert not (layout.component_venvs_root() / "metrics").exists()

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
        assert not (layout.component_venvs_root() / "metrics").exists()
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
        assert not (layout.component_venvs_root() / "metrics").exists()
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

    def test_bundled_metrics_requirements_come_from_manifest(self) -> None:
        """CM §3.5.1: the bundled metrics manifest carries the engine pin
        with the {engine_version} placeholder; the loader expands it to
        the running engine's version (one mechanism for bundled and
        hand-authored python children alike)."""

        from vesma import __version__
        from vesma.service.manifest import load_manifest

        manifest = load_manifest(bundled_manifest_path("metrics"))

        manifest = load_manifest(bundled_manifest_path("metrics"))
        assert manifest.launch_python_requirements() == ("vesma=={engine_version}",)
        expanded = install_mod._effective_requirements(manifest)
        assert expanded == (f"vesma=={__version__}",)

    def test_board_has_no_requirements(self) -> None:
        from vesma.service.manifest import load_manifest

        assert load_manifest(bundled_manifest_path("board")).launch_python_requirements() == ()

    def test_unknown_python_child_component_refused(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A venv-referencing manifest outside the v1 pack fails closed
        (no silent empty venv) — exercised against the REAL installer
        with the bundled pack's pip leg stubbed (the refusal under test
        must not depend on the engine version being live on PyPI)."""
        from vesma import __version__

        _stub_pip(monkeypatch, f"pip==99.0\nvesma=={__version__}\n")
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
        with pytest.raises(ManifestError, match=r"MUST pin its dependencies"):
            install()


# ── Reinstall must not launder freeze-drift (P1) ──────────────────────


def _stub_pip(monkeypatch: pytest.MonkeyPatch, freeze_outputs: list[str] | str) -> list[list[str]]:
    """Replace the pip leg: ``install`` is a no-op, ``freeze`` emits the
    given output(s) — a single string repeats on every call (a matching
    venv freezes twice: drift probe + verify), a list is consumed in
    order. Offline — no PyPI, no network, ever."""
    calls: list[list[str]] = []
    repeat = isinstance(freeze_outputs, str)
    ordered = [] if repeat else list(freeze_outputs)

    def fake_run_pip(venv_dir: Path, args: list[str]) -> str:
        calls.append(list(args))
        if args[0] == "install":
            return ""
        if repeat:
            return freeze_outputs
        return ordered.pop(0)

    monkeypatch.setattr(install_mod, "_run_pip", fake_run_pip)
    return calls


def _fake_venv_tree(name: str) -> Path:
    """Minimal venv shape (bin/python present) — nothing executes it."""
    venv_dir = layout.component_venv_dir(name)
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    python.write_text("#!/bin/true\n", encoding="utf-8")
    python.chmod(0o755)
    return venv_dir


class TestReinstallDriftRebuild:
    """P1: reinstall compares the EXISTING venv's freeze against the
    EXISTING lock FIRST; on drift the venv is rebuilt from scratch — an
    operator's hand `pip install` never becomes canon via the fix command."""

    def test_drifted_venv_is_rebuilt_not_kept(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(install_mod.unitgen, "container_detect", lambda *a, **k: False)
        from vesma import __version__

        drifted = f"pip==99.0\nvesma=={__version__}\nforeign-pkg==1.0\n"
        clean = f"pip==99.0\nvesma=={__version__}\n"
        calls = _stub_pip(monkeypatch, [drifted, clean])

        # A REAL venv (offline `python -m venv`) carrying a foreign marker,
        # with a stale lock from before the hand install.
        layout.ensure_dir(layout.component_venvs_root(), layout.MODE_DIR_DEFAULT)
        venv_dir = install_mod._create_component_venv("metrics")
        marker = venv_dir / "foreign-marker.txt"
        marker.write_text("hand installed", encoding="utf-8")
        lock_path = layout.data_dir("metrics") / "requirements-lock.txt"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(clean, encoding="utf-8")  # pre-hand-install lock

        result = install_mod.install()

        # The venv was rmtree'd and rebuilt from scratch: foreign gone.
        assert not marker.exists()
        assert (venv_dir / "bin" / "python").exists()  # real fresh venv
        # The lock is the freeze of the REBUILT venv — the hand package is
        # not canon and did not enter the new lock.
        assert lock_path.read_text(encoding="utf-8") == clean
        assert any("rebuilding" in line for line in result.lines)
        # freeze (drift probe) -> install -> freeze (verify) — exactly once each.
        assert calls == [
            ["freeze", "--disable-pip-version-check"],
            ["install", "--disable-pip-version-check", "--no-input", f"vesma=={__version__}"],
            ["freeze", "--disable-pip-version-check"],
        ]

    def test_matching_venv_is_kept(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(install_mod.unitgen, "container_detect", lambda *a, **k: False)
        from vesma import __version__

        clean = f"pip==99.0\nvesma=={__version__}\n"
        _stub_pip(monkeypatch, clean)

        layout.ensure_dir(layout.component_venvs_root(), layout.MODE_DIR_DEFAULT)
        venv_dir = install_mod._create_component_venv("metrics")
        marker = venv_dir / "first-run-marker.txt"
        marker.write_text("survives", encoding="utf-8")
        lock_path = layout.data_dir("metrics") / "requirements-lock.txt"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(clean, encoding="utf-8")

        result = install_mod.install()

        assert marker.exists()  # freeze matched the lock — the venv was kept
        assert not any("rebuilding" in line for line in result.lines)
        assert lock_path.read_text(encoding="utf-8") == clean


# ── Verify-after-install leg runs against a stubbed pip (P2-b) ────────


class TestVerifyAfterInstall:
    """The REAL `_install_component_venv` (pip stubbed, fully offline):
    a freeze matching the required pins passes and writes the lock; a
    mismatching freeze fails LOUDLY with the ready fix command."""

    def test_matching_freeze_passes_and_writes_lock(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(install_mod.unitgen, "container_detect", lambda *a, **k: False)
        from vesma import __version__

        good = f"pip==99.0\nvesma=={__version__}\n"
        _stub_pip(monkeypatch, good)
        _fake_venv_tree("metrics")
        lock_path = layout.data_dir("metrics") / "requirements-lock.txt"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(good, encoding="utf-8")  # freeze == lock: venv kept

        report: list[str] = []
        venv_dir = install_mod._install_component_venv(load_bundled_manifest("metrics"), report)

        assert venv_dir == layout.component_venv_dir("metrics")
        assert lock_path.read_text(encoding="utf-8") == good  # full freeze as the lock
        assert any(line.startswith("venv: metrics") for line in report)

    def test_mismatching_freeze_fails_with_fix_command(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(install_mod.unitgen, "container_detect", lambda *a, **k: False)
        from vesma import __version__

        bad = "pip==99.0\nvesma==0.0.0\n"  # wrong vesma version vs the required pin
        _stub_pip(monkeypatch, bad)
        _fake_venv_tree("metrics")
        lock_path = layout.data_dir("metrics") / "requirements-lock.txt"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(bad, encoding="utf-8")  # freeze == lock: venv kept

        report: list[str] = []
        with pytest.raises(InstallError) as excinfo:
            install_mod._install_component_venv(load_bundled_manifest("metrics"), report)
        message = str(excinfo.value)
        assert "drift right after install" in message
        assert f"vesma=={__version__} expected" in message
        assert "freeze reports '0.0.0'" in message
        assert "vesma service install" in message  # LY-14: the fix travels with the failure
        assert lock_path.read_text(encoding="utf-8") == bad  # no lock laundering on failure


# ── Uninstall --all is the operator's self-clean escape hatch (P3) ────


class TestUninstallAllSelfClean:
    def test_broken_components_d_warns_and_continues(self, installed: Path) -> None:
        (layout.components_dir() / "broken.yaml").write_text(
            "not: a: manifest:\n", encoding="utf-8"
        )
        env_file = layout.env_file_path("metrics")
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("METRICS_TOKEN=x\n", encoding="utf-8")

        result = uninstall(remove_all=True)

        assert any(line.startswith("WARN:") for line in result.lines)
        assert list(layout.components_dir().iterdir()) == []  # broken.yaml swept too
        assert not env_file.exists()
        assert not layout.component_venv_dir("metrics").exists()
        assert not (installed / ".config/systemd/user/vesma.service").exists()
        assert layout.data_dir("board").is_dir()  # operator data preserved
        assert layout.data_dir("metrics").is_dir()

    def test_single_component_uninstall_still_fails_closed(self, installed: Path) -> None:
        (layout.components_dir() / "broken.yaml").write_text(
            "not: a: manifest:\n", encoding="utf-8"
        )
        with pytest.raises(ManifestError):
            uninstall("board")
        # Nothing was removed: the dependency map could not be loaded.
        assert (layout.components_dir() / "board.yaml").exists()


# ── `.yml` spelling handling (card vesma-61-yml-install-sync-tail) ────


class TestInstallComponentYml:
    """`vesma install NAME` vs the non-canonical `.yml` spelling.

    A bundled manifest parked under `.yml` MOVES to the canonical `.yaml`
    (writing `.yaml` next to it would NAME_DUPLICATE on the fail-closed
    load); an operator-authored `.yml` validates in place; both spellings
    at once refuse with the addressed fix before anything is written.
    """

    #: Valid operator manifest, in-process (no venv leg), NO depends_on
    #: (the fixture installation has no board to depend on).
    MESH_YML = """\
apiVersion: vesma.component/v1
kind: in-process
metadata:
  name: mesh
  version: 0.4.2
  tier: optional
  description: "Operator sidecar parked under the .yml spelling."
  provenance:
    repo: https://example.invalid/mesh
    license: MIT
in_process:
  module: vesma.service.board
  entrypoint: create_component
  python:
    version: ">=3.11"
"""

    def test_bundled_yml_only_moves_to_canonical(self, isolated_home: Path) -> None:
        components = layout.ensure_dir(layout.components_dir(), 0o700)
        bundle = bundled_manifest_path("board").read_text(encoding="utf-8")
        (components / "board.yml").write_text(bundle, encoding="utf-8")

        result = install_mod.install_component("board")

        assert not (components / "board.yml").exists()
        assert (components / "board.yaml").read_text(encoding="utf-8") == bundle
        assert result.manifest_path.name == "board.yaml"
        assert result.written is False  # rename then "current — matches the engine bundle"
        assert any("moved" in line for line in result.lines)

    def test_bundled_yml_divergent_moves_then_backs_up(self, isolated_home: Path) -> None:
        components = layout.ensure_dir(layout.components_dir(), 0o700)
        operator_edits = "# operator tuned\n" + bundled_manifest_path("board").read_text(
            encoding="utf-8"
        )
        (components / "board.yml").write_text(operator_edits, encoding="utf-8")

        result = install_mod.install_component("board")

        assert not (components / "board.yml").exists()
        assert (components / "board.yaml").read_text(encoding="utf-8") == bundled_manifest_path(
            "board"
        ).read_text(encoding="utf-8")
        backup = components.parent / "board.yaml.pre-regen.bak"
        assert backup.read_text(encoding="utf-8") == operator_edits
        assert result.written is True

    def test_operator_yml_only_validates_in_place(self, isolated_home: Path) -> None:
        components = layout.ensure_dir(layout.components_dir(), 0o700)
        (components / "mesh.yml").write_text(self.MESH_YML, encoding="utf-8")

        result = install_mod.install_component("mesh")

        assert (components / "mesh.yml").exists()
        assert not (components / "mesh.yaml").exists()
        assert result.origin == "operator"
        assert result.manifest_path.name == "mesh.yml"
        assert result.written is False

    def test_both_spellings_refuse_before_any_write(self, isolated_home: Path) -> None:
        components = layout.ensure_dir(layout.components_dir(), 0o700)
        bundle = bundled_manifest_path("board").read_text(encoding="utf-8")
        stale_copy = "# stale operator copy\n" + bundle
        (components / "board.yaml").write_text(stale_copy, encoding="utf-8")
        (components / "board.yml").write_text(bundle, encoding="utf-8")

        with pytest.raises(InstallError) as exc:
            install_mod.install_component("board")

        assert "board.yaml" in str(exc.value)
        assert "board.yml" in str(exc.value)
        # Nothing was written or renamed: both files are byte-identical
        # to their pre-call state.
        assert (components / "board.yaml").read_text(encoding="utf-8") == stale_copy
        assert (components / "board.yml").read_text(encoding="utf-8") == bundle


# ── `install --all` backup unification (cascade finding) ─────────────


class TestInstallAllBackup:
    """`vesma install --all` used to be the ONE overwrite path without
    the `.pre-regen.bak` discipline — a diverged bundled manifest may
    carry operator edits, and `--all` destroyed them silently."""

    def test_install_all_backs_up_diverged_bundle_manifest(
        self, isolated_home: Path, fake_venv_installer: list[str]
    ) -> None:
        components = layout.ensure_dir(layout.components_dir(), 0o700)
        operator_edits = "# operator tuned\n" + bundled_manifest_path("board").read_text(
            encoding="utf-8"
        )
        (components / "board.yaml").write_text(operator_edits, encoding="utf-8")

        result = install()

        backup = components.parent / "board.yaml.pre-regen.bak"
        assert backup.read_text(encoding="utf-8") == operator_edits
        assert (components / "board.yaml").read_bytes() == bundled_manifest_path(
            "board"
        ).read_bytes()
        assert (components / "board.yaml") in result.manifests

    def test_install_all_never_clobbers_the_first_backup(
        self, isolated_home: Path, fake_venv_installer: list[str]
    ) -> None:
        components = layout.ensure_dir(layout.components_dir(), 0o700)
        first_edits = "# first divergence\n" + bundled_manifest_path("board").read_text(
            encoding="utf-8"
        )
        second_edits = "# second divergence\n" + bundled_manifest_path("board").read_text(
            encoding="utf-8"
        )
        backup = components.parent / "board.yaml.pre-regen.bak"
        backup.write_text(first_edits, encoding="utf-8")
        (components / "board.yaml").write_text(second_edits, encoding="utf-8")

        install()

        assert backup.read_text(encoding="utf-8") == first_edits, (
            "the one-time rule: the FIRST divergence record survives"
        )


# ── Instance template unit (ADR-0049 wave 1) ──────────────────────────


class TestInstanceTemplateUnit:
    def test_template_written_alongside_default(
        self, installed: Path, fake_venv_installer: list[str]
    ) -> None:
        installed_unit = installed / ".config/systemd/user/vesma@.service"
        canonical_unit = installed / ".vesma/systemd/user/vesma@.service"
        assert installed_unit.exists()
        assert canonical_unit.exists()
        assert oct(installed_unit.stat().st_mode & 0o777) == oct(0o644)
        assert installed_unit.is_symlink(), "v2: installed path is the canonical symlink"

    def test_template_carries_instance_wiring(self, installed: Path) -> None:
        text = (installed / ".config/systemd/user/vesma@.service").read_text(encoding="utf-8")
        active, _ = unitgen.parse_unit(text)
        assert "VESMA_INSTANCE_ID=%i" in active["Environment"]
        assert active["RuntimeDirectory"] == "vesma/instances/%i"
        assert active["RuntimeDirectoryMode"] == "0700"
        assert active["SyslogIdentifier"] == "vesma@%i"
        assert active["ExecStart"].endswith("/bin/vesma service run")
        assert "ExecStop" not in active
        assert active["WantedBy"] == "default.target"

    def test_default_unit_text_unchanged_by_template_install(
        self, installed: Path, fake_venv_installer: list[str]
    ) -> None:
        """Backward compatibility (contract §3): the DEFAULT unit's text is
        byte-identical to a fresh generate() — the template wave never
        rewrites the default instance's contract surface."""
        on_disk = (installed / ".config/systemd/user/vesma.service").read_text(encoding="utf-8")
        downgraded = tuple(sorted(unitgen.DOWNGRADE_ALLOWED)) if unitgen.container_detect() else ()
        fresh = unitgen.generate(
            home=layout.home(),
            engine_venv=install_mod.engine_venv(),
            read_write_paths=layout.unit_rw_paths(),
            read_only_paths=layout.unit_ro_paths(),
            downgraded=downgraded,
        )
        assert on_disk == fresh

    def test_report_names_the_template(self, installed: Path) -> None:
        result = install()
        assert any("vesma@.service" in line for line in result.lines)
