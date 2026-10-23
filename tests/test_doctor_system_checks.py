"""Doctor system-profile gate tests (ADR-0045 rollout wave 3).

Dest-dir simulation (no root): the system tree lives under a temp
prefix, the user plane is isolated into a temp HOME — each DR verdict is
asserted against a CONTROLLED machine. Every gate gets a positive and a
negative leg:

- DR-14 — isolation: data-root collision (rollout-2 leg), config-root
  collision and the stamp cross-checks (this wave);
- DR-15 — rights vs the FHS manifest: the env.d 0600 FAIL gate (ANY
  drift), conffile-mode audit, per-component slots;
- DR-16 — symlink-revision integrity: dangling, escape, non-symlink
  slot, leftover revisions;
- DR-17 — state ↔ filesystem, both directions, conffile zone exempt;
- DR-18 — the system unit vs the canon regeneration;
- DR-19 — generation advisories, non-fatal (WARN), both profiles;
- SY-10 — the gate set is operational with the contracted severities.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import yaml

from vesma.service import doctor_checks, fhs
from vesma.service import system_install as si
from vesma.service.doctor_checks import Severity, run_service_checks
from vesma.service.system_install import install_system

ENGINE_VERSION = si._engine_version()


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def sim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temp deployment prefix + isolated user plane (controlled machine)."""
    prefix = tmp_path / "deploy"
    for segment in ("etc", "var/lib", "var/cache", "var/log", "usr/lib", "usr/share", "run"):
        (prefix / segment).mkdir(parents=True)
    monkeypatch.setattr(fhs, "default_prefix", lambda: prefix)
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
    return prefix


@pytest.fixture(autouse=True)
def _stub_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every installer subprocess seam; stub venv materialization
    with executable sh-stubs (the DR-16/DR-18 legs read, not execute)."""

    def fake_materialize(
        venv_dir: Path, requirements: tuple[str, ...], mode: int, **_kwargs: object
    ) -> dict[str, str]:
        version = next(
            line.split("==", 1)[1] for line in requirements if line.startswith("vesma==")
        )
        (venv_dir / "bin").mkdir(parents=True)
        vesma_bin = venv_dir / "bin" / "vesma"
        vesma_bin.write_text(f"#!/bin/sh\necho vesma {version}\n", encoding="utf-8")
        vesma_bin.chmod(0o755)
        python_bin = venv_dir / "bin" / "python"
        python_bin.write_text("#!/bin/sh\necho 3.13.1\n", encoding="utf-8")
        python_bin.chmod(0o755)
        return {"vesma": version}

    monkeypatch.setattr(si, "_apply_sysusers", lambda fragment: "ok (stub)")
    monkeypatch.setattr(si, "_systemctl_system", lambda args: "ok (stub)")
    monkeypatch.setattr(si, "_materialize_venv", fake_materialize)


def _finding(findings: list, check_id: str):  # type: ignore[no-untyped-def]
    return next(f for f in findings if f.check_id == check_id)


# ── DR-14: profile isolation (full matrix) ────────────────────────────


class TestDr14Isolation:
    def test_ok_on_disjoint_profiles(self, sim: Path) -> None:
        install_system()
        finding = _finding(run_service_checks(), "DR-14")
        assert finding.severity is Severity.OK

    def test_ok_without_system_install(self, sim: Path) -> None:
        finding = _finding(run_service_checks(), "DR-14")
        assert finding.severity is Severity.OK

    def test_data_root_collision_fails(self, sim: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = sim / "var/lib/vesma/.vesma-home"
        home.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        install_system()
        finding = _finding(run_service_checks(), "DR-14")
        assert finding.severity is Severity.FAIL
        assert "data root" in (finding.detail or "")

    def test_config_root_collision_fails(self, sim: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The user profile rooted INSIDE the system config plane.
        home = sim / "etc/vesma/operator-home"
        home.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        install_system()
        finding = _finding(run_service_checks(), "DR-14")
        assert finding.severity is Severity.FAIL
        assert "config root" in (finding.detail or "")

    def test_corrupt_system_profile_stamp_fails(self, sim: Path) -> None:
        install_system()
        state_path = sim / "var/lib/vesma/install-state.yaml"
        doc = yaml.safe_load(state_path.read_text(encoding="utf-8"))
        doc["profile"] = "banana"
        state_path.write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        finding = _finding(run_service_checks(), "DR-14")
        assert finding.severity is Severity.FAIL
        assert "profile='banana'" in (finding.detail or "")

    def test_user_stamp_claiming_system_fails(
        self, sim: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_system()
        user_stamp = Path.home() / ".vesma" / "install-state.yaml"
        user_stamp.parent.mkdir(parents=True, exist_ok=True)
        user_stamp.write_text("profile: system\ngeneration: v2\n", encoding="utf-8")
        finding = _finding(run_service_checks(), "DR-14")
        assert finding.severity is Severity.FAIL
        assert "cross-contaminated" in (finding.detail or "")


# ── DR-15: rights vs the FHS manifest (env.d 0600 gate) ───────────────


class TestDr15Rights:
    def test_env_file_exact_0600_passes_the_mode_leg(self, sim: Path) -> None:
        install_system()
        env_file = sim / "etc/vesma/env.d/app.env"
        env_file.write_text("TOKEN=redacted-not-logged\n", encoding="utf-8")
        env_file.chmod(0o600)
        finding = _finding(run_service_checks(), "DR-15")
        # A non-root simulation drifts on OWNERSHIP honestly (recorded,
        # not applied) — but the 0600 mode leg must be silent.
        assert "exactly 0600" not in (finding.detail or "")

    def test_env_file_any_drift_is_the_fail_gate(self, sim: Path) -> None:
        install_system()
        env_file = sim / "etc/vesma/env.d/app.env"
        env_file.write_text("TOKEN=redacted-not-logged\n", encoding="utf-8")
        env_file.chmod(0o644)
        finding = _finding(run_service_checks(), "DR-15")
        assert finding.severity is Severity.FAIL
        assert "exactly 0600" in (finding.detail or "")
        assert "TOKEN=redacted-not-logged" not in (finding.detail or ""), (
            "env.d contents are never logged by the doctor"
        )

    def test_env_file_stricter_mode_is_still_drift(self, sim: Path) -> None:
        install_system()
        env_file = sim / "etc/vesma/env.d/app.env"
        env_file.write_text("TOKEN=x\n", encoding="utf-8")
        env_file.chmod(0o400)  # stricter — still drift: the invariant is exact
        finding = _finding(run_service_checks(), "DR-15")
        assert finding.severity is Severity.FAIL
        assert "exactly 0600" in (finding.detail or "")

    def test_loosened_conffile_mode_fails(self, sim: Path) -> None:
        install_system()
        (sim / "etc/vesma/components.d/board.yaml").chmod(0o664)
        finding = _finding(run_service_checks(), "DR-15")
        assert finding.severity is Severity.FAIL

    def test_stricter_operator_conffile_is_soft(self, sim: Path) -> None:
        install_system()
        (sim / "etc/vesma/components.d/board.yaml").chmod(0o600)
        finding = _finding(run_service_checks(), "DR-15")
        assert "stricter than the manifest" in (finding.detail or "")
        # Soft-only findings must not render as OK.
        assert finding.severity in (Severity.FAIL, Severity.WARN)

    def test_component_slots_are_walked(self, sim: Path) -> None:
        install_system()
        data_dir = sim / "var/lib/vesma/board"
        data_dir.chmod(0o757)  # weakened vs manifest 0750
        finding = _finding(run_service_checks(), "DR-15")
        assert finding.severity is Severity.FAIL
        assert "board" in (finding.detail or "")

    def test_ok_on_a_fresh_simulation(self, sim: Path) -> None:
        install_system()
        finding = _finding(run_service_checks(), "DR-15")
        # Modes match the manifest; the ownership leg drifts honestly in
        # a non-root simulation — FAIL with a fix, never a silent OK.
        assert finding.severity in (Severity.FAIL, Severity.OK)
        if finding.severity is Severity.FAIL:
            assert "owner" in (finding.detail or "")


# ── DR-16: symlink-revision integrity ─────────────────────────────────


class TestDr16Revisions:
    def test_ok_after_install(self, sim: Path) -> None:
        install_system()
        finding = _finding(run_service_checks(), "DR-16")
        assert finding.severity is Severity.OK

    def test_dangling_symlink_fails(self, sim: Path) -> None:
        install_system()
        engine = sim / "usr/lib/vesma/venv"
        engine.unlink()
        engine.symlink_to("venv-does-not-exist")
        finding = _finding(run_service_checks(), "DR-16")
        assert finding.severity is Severity.FAIL
        assert "dangling" in (finding.detail or "")

    def test_escaping_target_fails(self, sim: Path) -> None:
        install_system()
        engine = sim / "usr/lib/vesma/venv"
        engine.unlink()
        engine.symlink_to("../../../etc/vesma")
        finding = _finding(run_service_checks(), "DR-16")
        assert finding.severity is Severity.FAIL
        assert "OUTSIDE" in (finding.detail or "")

    def test_non_symlink_slot_fails(self, sim: Path) -> None:
        install_system()
        engine = sim / "usr/lib/vesma/venv"
        rev = Path(os.path.realpath(engine))
        engine.unlink()
        shutil.copytree(rev, engine)
        finding = _finding(run_service_checks(), "DR-16")
        assert finding.severity is Severity.FAIL
        assert "must be a symlink" in (finding.detail or "")

    def test_leftover_revision_warns(self, sim: Path) -> None:
        install_system()
        ghost = sim / "usr/lib/vesma/venvs/metrics-0.0.0-ghost"
        ghost.mkdir()
        finding = _finding(run_service_checks(), "DR-16")
        assert finding.severity is Severity.WARN
        assert "leftover" in (finding.detail or "")

    def test_ok_without_system_install(self, sim: Path) -> None:
        finding = _finding(run_service_checks(), "DR-16")
        assert finding.severity is Severity.OK


# ── DR-17: state ↔ filesystem, both directions ────────────────────────


class TestDr17SystemInversion:
    def test_consistent_after_install(self, sim: Path) -> None:
        install_system()
        finding = _finding(run_service_checks(), "DR-17")
        assert finding.severity is Severity.OK
        assert "consistent" in (finding.detail or "")

    def test_missing_recorded_artifact_fails(self, sim: Path) -> None:
        install_system()
        (sim / "etc/systemd/system/vesma.service").unlink()
        finding = _finding(run_service_checks(), "DR-17")
        assert finding.severity is Severity.FAIL
        assert "MISSING" in (finding.detail or "")

    def test_unrecorded_content_in_strict_dir_fails(self, sim: Path) -> None:
        install_system()
        stray = sim / "usr/lib/vesma/stray-file.txt"
        stray.write_text("unrecorded\n", encoding="utf-8")
        finding = _finding(run_service_checks(), "DR-17")
        assert finding.severity is Severity.FAIL
        assert "unrecorded" in (finding.detail or "")

    def test_conffile_zone_is_exempt(self, sim: Path) -> None:
        """Operator conffile-zone content (drop-ins, env secrets, config)
        is operator-provisioned BY the manifest — its presence is never
        state↔fs drift."""
        install_system()
        (sim / "etc/vesma/components.d/mycomp.yaml").write_text(
            "operator drop-in\n", encoding="utf-8"
        )
        env_dir = sim / "etc/vesma/env.d"
        (env_dir / "app.env").write_text("TOKEN=redacted\n", encoding="utf-8")
        (sim / "etc/vesma/config.yaml").write_text("operator config\n", encoding="utf-8")
        finding = _finding(run_service_checks(), "DR-17")
        assert finding.severity is Severity.OK
        assert "unrecorded" not in (finding.detail or "")

    def test_type_drift_fails(self, sim: Path) -> None:
        install_system()
        unit = sim / "etc/systemd/system/vesma.service"
        unit.unlink()
        unit.mkdir()
        finding = _finding(run_service_checks(), "DR-17")
        assert finding.severity is Severity.FAIL

    def test_malformed_artifact_records_fail(self, sim: Path) -> None:
        """Coverage pin (cascade fix): a malformed install-state row is a
        loud DR-17 FAIL — never a silent skip of the unvalidatable row."""
        install_system()
        state_path = sim / "var/lib/vesma/install-state.yaml"
        doc = yaml.safe_load(state_path.read_text(encoding="utf-8"))
        doc["artifacts"].append({"role": "broken-row"})  # no path/kind/mode
        state_path.write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        finding = _finding(run_service_checks(), "DR-17")
        assert finding.severity is Severity.FAIL
        assert "malformed" in (finding.detail or "")

    def test_same_predicate_as_the_updater_preflight(self, sim: Path) -> None:
        """Detection and refusal share one function — the doctor renders
        the same drift the updater refuses on."""
        install_system()
        stray = sim / "usr/share/vesma/stray.txt"
        stray.write_text("x\n", encoding="utf-8")
        drift = si.system_state_drift()
        finding = _finding(run_service_checks(), "DR-17")
        assert drift, "the shared predicate must see the stray file"
        assert finding.severity is Severity.FAIL
        assert "unrecorded" in (finding.detail or "")


# ── DR-18: system unit vs regeneration ────────────────────────────────


class TestDr18Unit:
    def test_matches_regeneration_after_install(self, sim: Path) -> None:
        install_system()
        finding = _finding(run_service_checks(), "DR-18")
        assert finding.severity is Severity.OK
        assert "byte-for-byte" in (finding.detail or "")

    def test_hand_edit_fails(self, sim: Path) -> None:
        install_system()
        unit = sim / "etc/systemd/system/vesma.service"
        unit.write_text(unit.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
        finding = _finding(run_service_checks(), "DR-18")
        assert finding.severity is Severity.FAIL
        assert "differs" in (finding.detail or "")

    def test_missing_unit_fails(self, sim: Path) -> None:
        install_system()
        (sim / "etc/systemd/system/vesma.service").unlink()
        finding = _finding(run_service_checks(), "DR-18")
        assert finding.severity is Severity.FAIL
        assert "MISSING" in (finding.detail or "")

    def test_ok_without_system_install(self, sim: Path) -> None:
        finding = _finding(run_service_checks(), "DR-18")
        assert finding.severity is Severity.OK


# ── DR-19: generation advisories (both profiles) ──────────────────────


class TestDr19Advisories:
    def test_v15_user_tree_gets_nonfatal_advisory(self, sim: Path) -> None:
        home = Path(os.environ["HOME"])
        for root in (home / ".config" / "vesma" / "components.d",):
            root.mkdir(parents=True)
        finding = _finding(run_service_checks(), "DR-19")
        assert finding.severity is Severity.WARN
        assert "v1.5" in (finding.detail or "")
        assert "consolidation" in (finding.fix_command or "")

    def test_ok_after_install_mentions_system_generation(self, sim: Path) -> None:
        install_system()
        finding = _finding(run_service_checks(), "DR-19")
        assert finding.severity is Severity.OK
        assert "system profile: generation v2" in (finding.detail or "")

    def test_unknown_system_generation_warns(self, sim: Path) -> None:
        install_system()
        state_path = sim / "var/lib/vesma/install-state.yaml"
        doc = yaml.safe_load(state_path.read_text(encoding="utf-8"))
        doc["generation"] = "banana"
        state_path.write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        finding = _finding(run_service_checks(), "DR-19")
        assert finding.severity is Severity.WARN
        assert "banana" in (finding.detail or "")


# ── SY-10: the gate set is operational with contracted severities ─────


class TestSy10GateSetOperational:
    def test_all_system_gates_present_and_green_on_a_fresh_install(self, sim: Path) -> None:
        install_system()
        findings = run_service_checks()
        by_id = {f.check_id: f for f in findings}
        for check_id in ("DR-14", "DR-15", "DR-16", "DR-17", "DR-18", "DR-19"):
            assert check_id in by_id, f"{check_id} missing from the registry"
        assert by_id["DR-14"].severity is Severity.OK
        # DR-15's ownership leg honestly FAILs in the non-root simulation;
        # every other gate must be green.
        assert by_id["DR-16"].severity is Severity.OK
        assert by_id["DR-17"].severity is Severity.OK
        assert by_id["DR-18"].severity is Severity.OK
        assert by_id["DR-19"].severity is Severity.OK
        if by_id["DR-15"].severity is Severity.FAIL:
            assert "owner" in (by_id["DR-15"].detail or "")

    def test_doctor_runner_never_crashes_on_a_tampered_state(self, sim: Path) -> None:
        install_system()
        state_path = sim / "var/lib/vesma/install-state.yaml"
        doc = yaml.safe_load(state_path.read_text(encoding="utf-8"))
        doc["generation"] = "0.9-unknown"
        state_path.write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        findings = run_service_checks()
        assert findings
        assert doctor_checks.exit_code(findings) in (0, 1, 2)
