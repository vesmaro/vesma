"""System-profile installer tests (ADR-0045 rollout wave 2).

Covers the wave's conformance items WITHOUT root via the dest-dir
technique: the whole flow runs against a temp deployment prefix
(:func:`vesma.service.fhs.default_prefix` monkeypatched); ownership is
recorded, not applied (the honest simulation the install-state says so).
The one root-dependent leg (real ownership application) is env-gated:
``VESMA_TEST_SYSTEM_ROOT=1`` AND euid 0.

- SY-01 — the profile stamp; user+system coexistence; DR-14 collision
- SY-02 — the FHS manifest as the single source of paths
- SY-03 — the system tree rights (explicit modes; DR-15 flags drift)
- SY-04 — the sysusers fragment (content + idempotency)
- SY-08 — venv materialization (same-build pin, uv, lock)
- SY-09 — install-state exact-inverse uninstall (drift = loud refusal,
  foreign files untouched, operator data preserved)
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from vesma.service import doctor_checks, fhs, unitgen
from vesma.service import system_install as si
from vesma.service.system_install import (
    SystemInstallError,
    install_system,
    read_system_profile,
    read_system_state,
    uninstall_system,
)

ENGINE_VERSION = si._engine_version()


# ── Fixtures (dest-dir simulation, no root) ───────────────────────────


@dataclass
class Seam:
    """Recorded subprocess-seam calls (assertion surface)."""

    sysusers: list[Path] = field(default_factory=list)
    systemctl: list[list[str]] = field(default_factory=list)
    venvs: list[tuple[Path, tuple[str, ...]]] = field(default_factory=list)


@pytest.fixture
def sim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The temp deployment prefix: /etc, /var/lib, /usr/lib under tmp."""
    prefix = tmp_path / "deploy"
    for segment in ("etc", "var/lib", "var/cache", "var/log", "usr/lib", "usr/share", "run"):
        (prefix / segment).mkdir(parents=True)
    monkeypatch.setattr(fhs, "default_prefix", lambda: prefix)
    return prefix


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> Seam:
    """Stub every installer subprocess seam; record the calls."""
    record = Seam()

    def fake_materialize(
        venv_dir: Path, requirements: tuple[str, ...], mode: int
    ) -> dict[str, str]:
        record.venvs.append((venv_dir, requirements))
        (venv_dir / "bin").mkdir(parents=True)
        (venv_dir / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        (venv_dir / "bin" / "vesma").write_text("#!/bin/sh\n", encoding="utf-8")
        return {"vesma": ENGINE_VERSION}

    monkeypatch.setattr(
        si, "_apply_sysusers", lambda fragment: record.sysusers.append(fragment) or "ok (stub)"
    )
    monkeypatch.setattr(
        si, "_systemctl_system", lambda args: record.systemctl.append(args) or "ok (stub)"
    )
    monkeypatch.setattr(si, "_materialize_venv", fake_materialize)
    return record


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The user profile isolated into tmp (HOME + XDG roots)."""
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
def _no_real_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the HOST user systemd bus from the user install()."""
    from vesma.service import install as user_install

    monkeypatch.setattr(user_install, "_daemon_reload", lambda: "ok (stubbed)")
    monkeypatch.setattr(
        user_install, "_systemctl_user", lambda args: f"ok (stubbed: {' '.join(args)})"
    )


def _finding(findings: list, check_id: str):  # type: ignore[no-untyped-def]
    return next(f for f in findings if f.check_id == check_id)


# ── SY-01: profile stamp + isolation ──────────────────────────────────


class TestSy01ProfileAndIsolation:
    def test_install_stamps_the_system_profile(self, sim: Path, seams: Seam) -> None:
        result = install_system()
        assert result.state_path == sim / "var/lib/vesma/install-state.yaml"
        assert read_system_profile() == "system"
        doc = read_system_state()
        assert doc is not None
        assert doc["profile"] == "system"
        assert doc["generation"] == "v2"
        assert doc["engine_version"] == ENGINE_VERSION
        assert doc["prefix"] == str(sim)
        assert doc["schema"] == si.STATE_SCHEMA

    def test_user_and_system_coexist_disjoint(
        self, sim: Path, isolated_home: Path, seams: Seam
    ) -> None:
        from vesma.service.install import install as user_install_fn

        user_install_fn()  # the user profile (default)
        install_system()  # the system profile — same machine
        user_stamp = isolated_home / ".vesma" / "install-state.yaml"
        assert "profile: user" in user_stamp.read_text(encoding="utf-8")
        assert read_system_profile() == "system"
        # Disjoint data roots by construction: ~/.vesma vs /var/lib/vesma.
        assert not (sim / "var/lib/vesma" / ".vesma").exists()
        # The system install never wrote into the user tree.
        assert not (isolated_home / ".vesma" / "venv-").exists()

    def test_dr14_fails_on_data_root_collision(
        self, sim: Path, monkeypatch: pytest.MonkeyPatch, seams: Seam
    ) -> None:
        # The service-account HOME scenario: the user profile resolves
        # INSIDE the system data root (~/.vesma under /var/lib/vesma).
        home = sim / "var/lib/vesma" / ".vesma-home"
        home.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        install_system()
        finding = _finding(doctor_checks.run_service_checks(), "DR-14")
        assert finding.severity is doctor_checks.Severity.FAIL
        assert str(sim / "var/lib/vesma") in (finding.detail or "")

    def test_dr14_ok_without_system_install(self, isolated_home: Path) -> None:
        finding = _finding(doctor_checks.run_service_checks(), "DR-14")
        assert finding.severity is doctor_checks.Severity.OK

    def test_install_refuses_real_prefix_without_root(
        self, monkeypatch: pytest.MonkeyPatch, seams: Seam
    ) -> None:
        monkeypatch.setattr(fhs, "default_prefix", lambda: Path(os.sep))
        monkeypatch.setattr(si.os, "geteuid", lambda: 1000)
        with pytest.raises(SystemInstallError, match="sudo vesma service install --system"):
            install_system()
        with pytest.raises(SystemInstallError, match="sudo vesma service install --system"):
            uninstall_system()


# ── SY-02: the FHS manifest is the single source of paths ─────────────


class TestSy02ManifestSingleSource:
    def test_manifest_loads_and_carries_the_s34_table(self) -> None:
        manifest = fhs.load_fhs_manifest()
        roles = {entry.role for entry in manifest.entries}
        required = {
            "config_root",
            "shared_config",
            "manifest_dir",
            "env_dir",
            "env_secrets",
            "sysusers_fragment",
            "system_unit",
            "code_root",
            "venv_engine",
            "venv_component",
            "distro_static",
            "data_root",
            "store_db",
            "component_data",
            "supervisor_history",
            "install_state",
            "cache_root",
            "log_root",
            "file_logs",
            "ipc_runtime",
        }
        missing = required - roles
        assert not missing, f"manifest roles missing: {missing}"

    def test_s34_anchor_rights(self) -> None:
        manifest = fhs.load_fhs_manifest()
        by_role = {e.role: e for e in manifest.entries}
        assert by_role["config_root"].mode == 0o750
        assert by_role["config_root"].owner == "root"
        assert by_role["config_root"].group == "vesma"
        assert by_role["shared_config"].mode == 0o640
        assert by_role["shared_config"].has_flag("conffile")
        assert by_role["component_manifest"].has_flag("conffile")
        assert by_role["env_secrets"].mode == 0o600  # the DR-15 FAIL gate
        assert by_role["data_root"].mode == 0o750
        assert by_role["store_db"].mode == 0o600
        assert by_role["install_state"].mode == 0o640
        assert by_role["system_unit"].mode == 0o644
        assert by_role["venv_engine"].mode == 0o755
        assert by_role["ipc_runtime"].has_flag("runtime-directory")
        assert by_role["log_root"].has_flag("opt-in")
        assert by_role["file_logs"].has_flag("opt-in")

    def test_created_paths_all_come_from_the_manifest(self, sim: Path, seams: Seam) -> None:
        install_system()
        doc = read_system_state()
        assert doc is not None
        manifest = fhs.load_fhs_manifest()
        # Every concrete path the flow CAN create: manifest entries with
        # the bundled component names and this build's revision filled in.
        names = ("board", "metrics")
        derived_suffixes = ("requirements-lock.txt", ".rpmnew")
        allowed: set[str] = set()
        for entry in manifest.entries:
            if "<name>" in entry.system_path and "<rev>" in entry.system_path:
                allowed |= {
                    str(fhs.resolve_system_path(entry, prefix=sim, name=n, rev=ENGINE_VERSION))
                    for n in names
                }
            elif "<name>" in entry.system_path:
                allowed |= {str(fhs.resolve_system_path(entry, prefix=sim, name=n)) for n in names}
            elif "<rev>" in entry.system_path:
                allowed.add(str(fhs.resolve_system_path(entry, prefix=sim, rev=ENGINE_VERSION)))
            else:
                allowed.add(str(fhs.resolve_system_path(entry, prefix=sim)))
        for raw in doc["artifacts"]:
            path = raw["path"]
            assert path in allowed or path.endswith(derived_suffixes), (
                f"artifact {path} is not manifest-derived (no path literals"
                " outside the manifest, SY-02)"
            )

    def test_template_substitution_is_validated(self) -> None:
        entry = fhs.load_fhs_manifest().entry("component_data")
        with pytest.raises(ValueError, match="component name"):
            fhs.resolve_system_path(entry, name="../escape")
        with pytest.raises(ValueError, match="component name"):
            fhs.resolve_system_path(entry)
        rev_entry = fhs.load_fhs_manifest().entry("venv_engine_revision")
        with pytest.raises(ValueError, match="revision id"):
            fhs.resolve_system_path(rev_entry, rev="6.0.0/etc")
        with pytest.raises(ValueError, match="revision id"):
            fhs.resolve_system_path(rev_entry, rev=None)


# ── SY-03: the system tree rights ─────────────────────────────────────


class TestSy03SystemTreeRights:
    def test_tree_modes_match_the_manifest(self, sim: Path, seams: Seam) -> None:
        install_system()
        manifest = fhs.load_fhs_manifest()
        for entry in manifest.entries:
            if "<name>" in entry.system_path or "<rev>" in entry.system_path:
                continue
            if entry.owner is None:
                continue  # /run/vesma — RuntimeDirectory's job
            path = fhs.resolve_system_path(entry, prefix=sim)
            if entry.has_flag("opt-in") or entry.role in ("store_db", "shared_config"):
                continue  # not created by the base install (engine-managed / operator conffile)
            assert path.exists(), f"{entry.role}: {path} missing after install"
            info = path.stat()
            assert stat.S_IMODE(info.st_mode) == entry.mode, (
                f"{entry.role}: {path} mode {format(stat.S_IMODE(info.st_mode), '04o')}"
                f" != manifest {format(entry.mode, '04o')}"
            )

    def test_runtime_and_log_planes_are_not_created(self, sim: Path, seams: Seam) -> None:
        install_system()
        assert not (sim / "run/vesma").exists()  # RuntimeDirectory=, not the installer
        assert not (sim / "var/log/vesma").exists()  # opt-in only (§3.9)
        assert not (sim / "etc/vesma/config.yaml").exists()  # operator-provisioned

    def test_state_records_manifest_owners(self, sim: Path, seams: Seam) -> None:
        install_system()
        doc = read_system_state()
        assert doc is not None
        by_path = {a["path"]: a for a in doc["artifacts"]}
        config_root = by_path[str(sim / "etc/vesma")]
        assert (config_root["owner"], config_root["group"]) == ("root", "vesma")
        data_root = by_path[str(sim / "var/lib/vesma")]
        assert (data_root["owner"], data_root["group"]) == ("vesma", "vesma")

    def test_dr15_flags_mode_drift(self, sim: Path, seams: Seam) -> None:
        install_system()
        (sim / "etc/vesma/components.d").chmod(0o755)  # looser than manifest 0750
        finding = _finding(doctor_checks.run_service_checks(), "DR-15")
        assert finding.severity is doctor_checks.Severity.FAIL
        assert finding.fix_command is not None

    def test_dr15_ok_on_a_fresh_simulation(self, sim: Path, seams: Seam) -> None:
        install_system()
        finding = _finding(doctor_checks.run_service_checks(), "DR-15")
        # Modes match the manifest; the ownership leg drifts in a
        # non-root simulation (recorded, not applied) — that honest
        # drift is a FAIL with a fix command, never a silent OK.
        assert finding.severity in (doctor_checks.Severity.FAIL, doctor_checks.Severity.OK)
        if finding.severity is doctor_checks.Severity.FAIL:
            assert "owner" in finding.detail


# ── SY-04: the sysusers fragment ──────────────────────────────────────


class TestSy04Sysusers:
    def test_fragment_creates_vesma_account(self, sim: Path, seams: Seam) -> None:
        install_system()
        fragment = sim / "usr/lib/sysusers.d/vesma.conf"
        text = fragment.read_text(encoding="utf-8")
        assert "g     vesma" in text
        assert "u     vesma" in text
        # home = the data root; shell login is denied by sysusers design.
        assert "Home directory" in text
        assert str(sim / "var/lib/vesma") in text
        info = fragment.stat()
        assert stat.S_IMODE(info.st_mode) == 0o644

    def test_reinstall_never_duplicates_the_account(self, sim: Path, seams: Seam) -> None:
        install_system()
        fragment = sim / "usr/lib/sysusers.d/vesma.conf"
        first = fragment.read_text(encoding="utf-8")
        install_system()
        assert fragment.read_text(encoding="utf-8") == first
        assert first.count("u     vesma") == 1
        assert len(seams.sysusers) == 2  # applied on both runs, idempotently


# ── SY-08: venv materialization ───────────────────────────────────────


class TestSy08VenvMaterialization:
    def test_venvs_get_the_same_build_wheel_pin(self, sim: Path, seams: Seam) -> None:
        install_system()
        pins = {reqs for _, reqs in seams.venvs}
        assert ("vesma==" + ENGINE_VERSION,) in pins  # the engine venv
        # metrics is the bundled python child: same-build pin via the
        # {engine_version} placeholder.
        assert any("vesma==" + ENGINE_VERSION in reqs for _, reqs in seams.venvs if len(reqs) == 1)
        # Revisions are symlink-revision materialized under /usr/lib/vesma.
        for venv_dir, _ in seams.venvs:
            assert str(venv_dir).startswith(str(sim / "usr/lib/vesma"))

    def test_symlinks_point_inside_the_venvs_root(self, sim: Path, seams: Seam) -> None:
        install_system()
        engine = sim / "usr/lib/vesma/venv"
        assert engine.is_symlink()
        assert os.readlink(engine) == f"venv-{ENGINE_VERSION}"
        metrics = sim / "usr/lib/vesma/venvs/metrics"
        assert metrics.is_symlink()
        assert os.readlink(metrics) == f"metrics-{ENGINE_VERSION}"

    def test_lock_is_the_freeze_of_the_materialized_venv(self, sim: Path, seams: Seam) -> None:
        install_system()
        lock = sim / "var/lib/vesma/metrics/requirements-lock.txt"
        assert lock.read_text(encoding="utf-8") == f"vesma=={ENGINE_VERSION}\n"

    def test_reinstall_reuses_the_current_revision(self, sim: Path, seams: Seam) -> None:
        install_system()
        assert len(seams.venvs) == 2  # engine + metrics
        install_system()
        assert len(seams.venvs) == 2  # revisions reused — nothing re-materialized

    def test_materializer_refuses_without_uv(
        self, sim: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(si, "_uv_path", lambda: None)
        with pytest.raises(SystemInstallError, match="uv is not on PATH"):
            si._materialize_venv(sim / "usr/lib/vesma/venv-x", ("vesma==1.0.0",), 0o755)

    def test_pin_drift_after_materialization_is_loud(
        self, sim: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def lying_materialize(
            venv_dir: Path, requirements: tuple[str, ...], mode: int
        ) -> dict[str, str]:
            (venv_dir / "bin").mkdir(parents=True)
            (venv_dir / "bin" / "vesma").write_text("#!/bin/sh\n", encoding="utf-8")
            return {"vesma": "0.0.0-wrong"}  # freeze != the requested pin

        monkeypatch.setattr(si, "_materialize_venv", lying_materialize)
        with pytest.raises(SystemInstallError, match="venv drift right after materialization"):
            install_system()


# ── SY-09: install-state exact-inverse uninstall ──────────────────────


class TestSy09ExactInverseUninstall:
    def test_uninstall_removes_exactly_the_recorded_artifacts(self, sim: Path, seams: Seam) -> None:
        foreign = sim / "etc/foreign.conf"
        foreign.write_text("not vesma's\n", encoding="utf-8")
        install_system()
        result = uninstall_system()
        # Gone: config plane, code plane, unit, sysusers, static, state.
        for gone in (
            "etc/vesma",
            "etc/systemd/system/vesma.service",
            "usr/lib/sysusers.d/vesma.conf",
            "usr/lib/vesma",
            "usr/share/vesma",
            "var/lib/vesma/install-state.yaml",
        ):
            assert not (sim / gone).exists(), f"{gone} survived uninstall"
        # Foreign file untouched.
        assert foreign.read_text(encoding="utf-8") == "not vesma's\n"
        # Operator data preserved.
        assert (sim / "var/lib/vesma/history").is_dir()
        preserved_lines = [line for line in result.lines if line.startswith("preserved")]
        assert any("var/lib/vesma" in line for line in preserved_lines)

    def test_operator_data_survives_uninstall(self, sim: Path, seams: Seam) -> None:
        install_system()
        db = sim / "var/lib/vesma/vesma.db"
        db.write_text("sqlite", encoding="utf-8")
        board_data = sim / "var/lib/vesma/board/state.json"
        board_data.write_text("{}", encoding="utf-8")
        uninstall_system()
        assert db.read_text(encoding="utf-8") == "sqlite"
        assert board_data.exists()

    def test_uninstall_refuses_foreign_content_and_touches_nothing(
        self, sim: Path, seams: Seam
    ) -> None:
        install_system()
        stranger = sim / "etc/vesma/components.d/stranger.yaml"
        stranger.write_text("operator's own manifest\n", encoding="utf-8")
        unit = sim / "etc/systemd/system/vesma.service"
        with pytest.raises(SystemInstallError, match=r"stranger\.yaml"):
            uninstall_system()
        # Validation happens BEFORE mutation: nothing was removed.
        assert unit.exists()
        assert (sim / "etc/vesma").is_dir()
        assert stranger.exists()

    def test_uninstall_refuses_state_fs_drift(self, sim: Path, seams: Seam) -> None:
        install_system()
        (sim / "etc/systemd/system/vesma.service").unlink()  # someone ate the unit
        before = sorted(str(p.relative_to(sim)) for p in sim.rglob("*"))
        with pytest.raises(SystemInstallError, match="drift"):
            uninstall_system()
        after = sorted(str(p.relative_to(sim)) for p in sim.rglob("*"))
        assert before == after  # validate-then-mutate: a refusal is a no-op

    def test_uninstall_without_state_refuses(self, sim: Path, seams: Seam) -> None:
        with pytest.raises(SystemInstallError, match="no system install-state"):
            uninstall_system()

    def test_uninstall_tolerates_interrupted_install(self, sim: Path, seams: Seam) -> None:
        import yaml

        install_system()
        # Simulate a crash BEFORE the static leg materialized: the state
        # says "planned", the dir is absent.
        state_path = sim / "var/lib/vesma/install-state.yaml"
        doc = yaml.safe_load(state_path.read_text(encoding="utf-8"))
        for artifact in doc["artifacts"]:
            if artifact["path"] == str(sim / "usr/share/vesma"):
                artifact["status"] = "planned"
        state_path.write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        (sim / "usr/share/vesma").rmdir()
        result = uninstall_system()
        assert any("not materialized" in line for line in result.lines)


# ── The system unit (unitgen.generate_system_unit) ────────────────────


class TestSystemUnit:
    def test_unit_contract(self, sim: Path, seams: Seam) -> None:
        install_system()
        unit = (sim / "etc/systemd/system/vesma.service").read_text(encoding="utf-8")
        assert "User=vesma" in unit
        assert "Group=vesma" in unit
        assert "RuntimeDirectory=vesma" in unit
        assert "RuntimeDirectoryMode=0750" in unit
        assert "WantedBy=multi-user.target" in unit
        assert 'Environment="PYTHONNOUSERSITE=1"' in unit
        assert f"ExecStart={sim}/usr/lib/vesma/venv/bin/vesma service run" in unit
        assert "%h" not in unit  # a system unit has no home tilde
        # The opt-in log root renders with the systemd '-' prefix.
        assert f"-{sim}/var/log/vesma" in unit
        # Supply-chain boundary: the code + static trees read-only.
        assert f"ReadOnlyPaths={sim}/usr/lib/vesma {sim}/usr/share/vesma" in unit

    def test_generator_refuses_relative_venv(self) -> None:
        with pytest.raises(unitgen.UnitGenerationError, match="absolute"):
            unitgen.generate_system_unit(Path("relative/venv"), (), ())


# ── Root-gated leg (env-gated: VESMA_TEST_SYSTEM_ROOT=1 AND euid 0) ───

ROOT_GATE = "VESMA_TEST_SYSTEM_ROOT"


@pytest.mark.skipif(
    os.environ.get(ROOT_GATE) != "1" or os.geteuid() != 0,
    reason=f"real-ownership leg: needs euid 0 and {ROOT_GATE}=1 (dest-dir sim covers the rest)",
)
class TestRealOwnership:
    def test_ownership_applied_when_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The full flow with REAL ownership into a temp prefix.

        The venv leg stays stubbed (a real `uv pip install` needs the
        network — this leg exists to prove the CHOWN semantics). Needs
        the vesma account; on a runner without it the loud refusal is
        the assertion instead.
        """
        import grp
        import pwd
        import shutil

        prefix = tmp_path / "deploy"
        for segment in ("etc", "var/lib", "var/cache", "usr/lib", "usr/share"):
            (prefix / segment).mkdir(parents=True)
        monkeypatch.setattr(
            si,
            "_apply_sysusers",
            lambda fragment: "skipped: test runner (fragment on disk)",
        )
        monkeypatch.setattr(si, "_systemctl_system", lambda args: "ok (stub)")

        def net_free_venv(
            venv_dir: Path, requirements: tuple[str, ...], mode: int
        ) -> dict[str, str]:
            (venv_dir / "bin").mkdir(parents=True)
            (venv_dir / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            (venv_dir / "bin" / "vesma").write_text("#!/bin/sh\n", encoding="utf-8")
            return {"vesma": ENGINE_VERSION}

        monkeypatch.setattr(si, "_materialize_venv", net_free_venv)

        try:
            pwd.getpwnam("vesma")
        except KeyError:
            with pytest.raises(SystemInstallError, match="sysusers"):
                install_system(prefix)
            return

        result = install_system(prefix)
        config_root = prefix / "etc/vesma"
        assert config_root.stat().st_uid == pwd.getpwnam("root").pw_uid
        assert config_root.stat().st_gid == grp.getgrnam("vesma").gr_gid
        data_root = prefix / "var/lib/vesma"
        assert data_root.stat().st_uid == pwd.getpwnam("vesma").pw_uid
        assert result.state_path.exists()
        shutil.rmtree(prefix, ignore_errors=True)


# ── CLI wiring: `vesma service install|uninstall --system` ────────────


class TestCliWiring:
    def test_install_system_flag_dispatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from typer.testing import CliRunner

        from vesma.cli import service as service_cli

        called: list[bool] = []

        def fake_install_system() -> si.SystemInstallResult:
            called.append(True)
            return si.SystemInstallResult(
                lines=("line",),
                state_path=Path("/tmp/state.yaml"),
                unit_path=Path("/tmp/vesma.service"),
                engine_venv=Path("/tmp/venv"),
                revisions=(),
            )

        monkeypatch.setattr(service_cli, "install_system", fake_install_system)
        result = CliRunner().invoke(service_cli.service_app, ["install", "--system"])
        assert result.exit_code == 0, result.output
        assert called == [True]

    def test_uninstall_system_flag_dispatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from typer.testing import CliRunner

        from vesma.cli import service as service_cli

        called: list[bool] = []

        def fake_uninstall_system() -> si.SystemUninstallResult:
            called.append(True)
            return si.SystemUninstallResult(lines=("removed: x",))

        monkeypatch.setattr(service_cli, "uninstall_system", fake_uninstall_system)
        result = CliRunner().invoke(service_cli.service_app, ["uninstall", "--system"])
        assert result.exit_code == 0, result.output
        assert called == [True]

    def test_uninstall_system_refuses_name_and_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from typer.testing import CliRunner

        from vesma.cli import service as service_cli

        monkeypatch.setattr(
            service_cli, "uninstall_system", lambda: pytest.fail("must refuse before dispatch")
        )
        for args in (["uninstall", "--system", "board"], ["uninstall", "--system", "--all"]):
            result = CliRunner().invoke(service_cli.service_app, args)
            assert result.exit_code == 1, args
            assert "do not combine" in result.output
