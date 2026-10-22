"""Layout v2 wave tests (ADR-0045 rollout wave: resolver + stamp + registry).

The zero-regression goldens live in :mod:`tests.test_layout_v15_pins`; the
v2 resolve table in :mod:`tests.test_service_layout`. This module covers
the FLOW-level contract: the install stamps the detected generation once
and never flips it; a new installation lands in the v2 form with the
systemd canonical storage + symlink; legacy-artifact machines stamp v1.5
and keep installing into the v1.5 plane; reserved names are refused at
install time; doctor DR-17 (mixed generations = FAIL) and DR-19
(generation advisory) behave per specs/layout/v2 §3.3/§3.14; and the
self-update ownership check accepts the legacy engine-venv slot on a
v2-resolving machine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vesma.cli import update_cmd
from vesma.service import doctor_checks, layout
from vesma.service import install as install_mod
from vesma.service.doctor_checks import Severity, run_service_checks
from vesma.service.install import InstallError, install
from vesma.service.manifest import load_bundled_manifest


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
    """Replace the venv/pip leg (same shape as test_service_install)."""
    created: list[str] = []

    def fake_install_component_venv(manifest, report):  # type: ignore[no-untyped-def]
        name = manifest.name
        created.append(name)
        venv_dir = layout.component_venv_dir(name)
        venv_dir.mkdir(parents=True, exist_ok=True)
        return venv_dir

    monkeypatch.setattr(install_mod, "_install_component_venv", fake_install_component_venv)
    return created


def _v15_artifacts(home: Path) -> None:
    """Materialize an existing v1.5 service stack (one entry per plane)."""
    for root in (
        home / ".config" / "vesma" / "components.d",
        home / ".local" / "state" / "vesma" / "history",
        home / ".cache" / "vesma" / "board",
        home / ".local" / "share" / "vesma" / "board",
    ):
        root.mkdir(parents=True)


def _finding(findings: list, check_id: str):  # type: ignore[no-untyped-def]
    return next(f for f in findings if f.check_id == check_id)


class TestInstallStampsGeneration:
    def test_new_install_is_v2_and_stamped(
        self, isolated_home: Path, fake_venv_installer: list[str]
    ) -> None:
        result = install()
        assert layout.detect_generation() is layout.LayoutGeneration.V2
        assert layout.read_generation_stamp() is layout.LayoutGeneration.V2
        tree = isolated_home / ".vesma"
        # v2 form: service stack inside ~/.vesma.
        assert layout.verify_dir(tree / "components.d", 0o700)
        assert layout.verify_dir(tree / "env.d", 0o700)
        assert layout.verify_dir(tree / "components", 0o700)
        assert layout.verify_dir(tree / "venvs", 0o700)
        assert layout.verify_dir(tree / "logs", 0o700)
        assert layout.verify_dir(tree / "history", 0o700)
        assert layout.verify_dir(tree / "run", 0o700)
        assert layout.verify_dir(tree / "systemd" / "user", 0o700)
        assert layout.verify_dir(tree / "cache", 0o750)
        # The v1.5 plane stays untouched.
        assert not (isolated_home / ".config" / "vesma").exists()
        assert not (isolated_home / ".local" / "share" / "vesma" / "venvs").exists()
        # The stamp line surfaces in the report.
        assert any("generation: v2" in line for line in result.lines)

    def test_reinstall_never_rewrites_the_stamp(
        self, isolated_home: Path, fake_venv_installer: list[str]
    ) -> None:
        install()
        stamp = isolated_home / ".vesma" / "install-state.yaml"
        first = stamp.read_text(encoding="utf-8")
        install()
        assert stamp.read_text(encoding="utf-8") == first

    def test_legacy_artifacts_stamp_v15_and_install_into_v15_plane(
        self, isolated_home: Path, fake_venv_installer: list[str]
    ) -> None:
        """ZERO-REGRESSION: an existing v1.5 install keeps the v1 tables —
        the detected v1.5 generation is stamped, never upgraded in place."""
        _v15_artifacts(isolated_home)
        install()
        assert layout.read_generation_stamp() is layout.LayoutGeneration.V1_5
        assert layout.detect_generation() is layout.LayoutGeneration.V1_5
        # Artifacts landed in the v1.5 plane, not in ~/.vesma.
        assert layout.verify_dir(isolated_home / ".config" / "vesma" / "components.d", 0o700)
        assert not (isolated_home / ".vesma" / "components.d").exists()

    def test_explicit_v15_stamp_is_trusted_even_without_artifacts(
        self, isolated_home: Path, fake_venv_installer: list[str]
    ) -> None:
        tree = isolated_home / ".vesma"
        tree.mkdir()
        (tree / "install-state.yaml").write_text("generation: v1.5\n", encoding="utf-8")
        install()
        assert layout.read_generation_stamp() is layout.LayoutGeneration.V1_5
        assert not (tree / "components.d").exists()  # nothing written into v2 slots


class TestV2UnitCanonicalStorage:
    def test_v2_canonical_storage_plus_symlink(
        self, isolated_home: Path, fake_venv_installer: list[str]
    ) -> None:
        install()
        canonical = isolated_home / ".vesma" / "systemd" / "user" / "vesma.service"
        installed = isolated_home / ".config" / "systemd" / "user" / "vesma.service"
        assert canonical.is_file()
        assert installed.is_symlink()
        assert Path.resolve(installed, strict=True) == Path.resolve(canonical, strict=True)
        # Reading through the link yields the canonical bytes (DR-07 green).
        assert installed.read_text(encoding="utf-8") == canonical.read_text(encoding="utf-8")

    def test_v2_reinstall_swaps_the_symlink_atomically(
        self, isolated_home: Path, fake_venv_installer: list[str]
    ) -> None:
        install()
        install()
        installed = isolated_home / ".config" / "systemd" / "user" / "vesma.service"
        assert installed.is_symlink()  # still a link, not a regular file

    def test_v15_unit_stays_a_regular_file(
        self, isolated_home: Path, fake_venv_installer: list[str]
    ) -> None:
        _v15_artifacts(isolated_home)
        install()
        installed = isolated_home / ".config" / "systemd" / "user" / "vesma.service"
        assert installed.is_file()
        assert not installed.is_symlink()
        assert not (isolated_home / ".vesma" / "systemd").exists()

    def test_rw_ro_paths_follow_the_generation(
        self, isolated_home: Path, fake_venv_installer: list[str]
    ) -> None:
        install()
        unit_text = (isolated_home / ".config" / "systemd" / "user" / "vesma.service").read_text(
            encoding="utf-8"
        )
        tree = isolated_home / ".vesma"
        expected_rw = " ".join(
            install_mod.unitgen.render_path(p, layout.home()) for p in layout.unit_rw_paths()
        )
        assert f"ReadWritePaths={expected_rw}" in unit_text
        assert "%h/.vesma/logs" in unit_text
        assert ".local/state/vesma" not in unit_text
        # Paths render %h-relative — the absolute home never leaks.
        assert str(tree) not in unit_text


class TestReservedNamesAtInstall:
    @pytest.mark.parametrize("name", ["venv", "venvs", "cache", "install-state.yaml", "env.d"])
    def test_install_component_refuses_reserved_names(self, name: str) -> None:
        with pytest.raises(InstallError, match="reserved-name registry"):
            install_mod.install_component(name)

    def test_bundled_names_pass_the_registry(self) -> None:
        for name in install_mod.BUNDLED_COMPONENTS:
            assert not layout.is_reserved_tree_name(name)
            manifest = load_bundled_manifest(name)
            assert not layout.is_reserved_tree_name(manifest.name)


class TestDoctorDR17GenerationMixing:
    def test_mixed_tree_fails(self, isolated_home: Path) -> None:
        _v15_artifacts(isolated_home)
        tree = isolated_home / ".vesma"
        (tree / "components.d").mkdir(parents=True)
        finding = _finding(run_service_checks(), "DR-17")
        assert finding.severity is Severity.FAIL
        assert "MIXED" in finding.detail
        assert finding.fix_command is not None

    def test_single_v15_plane_ok(self, isolated_home: Path) -> None:
        _v15_artifacts(isolated_home)
        finding = _finding(run_service_checks(), "DR-17")
        assert finding.severity is Severity.OK
        assert "v1.5" in finding.detail

    def test_single_v2_plane_ok(self, isolated_home: Path) -> None:
        (isolated_home / ".vesma" / "components.d").mkdir(parents=True)
        finding = _finding(run_service_checks(), "DR-17")
        assert finding.severity is Severity.OK

    def test_engine_run_dir_is_not_v2_drift(self, isolated_home: Path) -> None:
        """~/.vesma/run (mesh core socket) and ~/.vesma/logs (engine files)
        exist on healthy v1.5 machines — never read as v2 artifacts."""
        _v15_artifacts(isolated_home)
        (isolated_home / ".vesma" / "run").mkdir(parents=True)
        (isolated_home / ".vesma" / "logs").mkdir(parents=True)
        finding = _finding(run_service_checks(), "DR-17")
        assert finding.severity is Severity.OK

    def test_no_stack_at_all_is_na(self, isolated_home: Path) -> None:
        finding = _finding(run_service_checks(), "DR-17")
        assert finding.severity is Severity.OK
        assert "n/a" in finding.detail


class TestDoctorDR19GenerationAdvisory:
    def test_v15_gets_warn_advisory(self, isolated_home: Path) -> None:
        _v15_artifacts(isolated_home)
        finding = _finding(run_service_checks(), "DR-19")
        assert finding.severity is Severity.WARN
        assert "v1.5" in finding.detail
        assert "consolidation" in (finding.fix_command or "")

    def test_v2_stamped_ok(self, isolated_home: Path) -> None:
        (isolated_home / ".vesma" / "components.d").mkdir(parents=True)
        layout.write_generation_stamp(layout.LayoutGeneration.V2)
        finding = _finding(run_service_checks(), "DR-19")
        assert finding.severity is Severity.OK
        assert "v2" in finding.detail

    def test_unstamped_v2_tree_warns(self, isolated_home: Path) -> None:
        (isolated_home / ".vesma" / "components.d").mkdir(parents=True)
        finding = _finding(run_service_checks(), "DR-19")
        assert finding.severity is Severity.WARN
        assert "stamp" in finding.detail

    def test_no_stack_ok(self, isolated_home: Path) -> None:
        finding = _finding(run_service_checks(), "DR-19")
        assert finding.severity is Severity.OK


class TestSelfUpdateOwnershipAcrossGenerations:
    def test_legacy_slot_owned_on_v2_machine(self, isolated_home: Path) -> None:
        """A v2-resolving machine whose engine venv sits at the LEGACY
        canonical slot (installed before the generation existed) stays
        owned — the `.vesma-venv` marker is primary, the slot fallback
        must recognize BOTH slots."""
        tree = isolated_home / ".vesma"
        tree.mkdir()
        (tree / "install-state.yaml").write_text("generation: v2\n", encoding="utf-8")
        legacy_venv = isolated_home / ".local" / "share" / "vesma" / "venv"
        legacy_venv.mkdir(parents=True)
        assert update_cmd._is_canonical_engine_venv(legacy_venv) is True
        # And the v2 slot is canonical on the same machine.
        v2_venv = tree / "venv"
        v2_venv.mkdir()
        assert update_cmd._is_canonical_engine_venv(v2_venv) is True
        # A foreign venv is still foreign.
        foreign = isolated_home / "somewhere-else"
        foreign.mkdir()
        assert update_cmd._is_canonical_engine_venv(foreign) is False


class TestDoctorRegistryOrdering:
    def test_system_profile_checks_are_present(self) -> None:
        """Registry composition pin, updated by the doctor/update wave
        (ADR-0045 rollout 3): DR-14 (profile isolation, full matrix) and
        DR-15 (system rights vs the FHS manifest) landed with the system
        installer; DR-16 (symlink-revision integrity) and DR-18 (system
        unit vs regeneration) complete the §3.14 gate set alongside the
        DR-17 (state↔fs, both planes) / DR-19 (generation advisories)
        user legs."""
        ids = [f.check_id for f in run_service_checks()]
        assert "DR-14" in ids
        assert "DR-15" in ids
        assert "DR-16" in ids
        assert "DR-17" in ids
        assert "DR-18" in ids
        assert "DR-19" in ids


def test_doctor_checks_module_exports_generation_aware_runner() -> None:
    """Smoke: the doctor runner itself never crashes on a virgin home."""
    findings = run_service_checks()
    assert findings
    assert doctor_checks.exit_code(findings) in (0, 1, 2)
