"""DR-23 (ArchCom 2026-10-29, verdict 1c2b8d64): "artifacts outside the
platform" — an artifact outside the slot table is a doctor finding, not
a lifestyle.

The audit classes (handoff vesma-component-platform-file-layout-20261029
§3): DE-2 a root-owned unit exec'ing into a user home (the LPE class),
DE-3 legacy ``mnemos-*`` binary names in units (pre-rebrand 2026-10-01),
DE-3/DE-5 live component-named processes no supervisor unit owns. Pins:
the leg-(a) FAIL (admin dir + vendor dir, EnvironmentFile breadth, the
data-root exemption, the canonical-unit skip, the user-plane
out-of-scope), the leg-(b) WARN with the rename contract, the leg-(c)
WARN + the supervised-process negative + both honest degrades, and the
registry entry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vesma.service import doctor_checks, fhs
from vesma.service.doctor_checks import Severity

# ── Seams (dest-dir sim; the host's real state must not decide) ───────


@pytest.fixture
def platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """An isolated machine: FHS test prefix, own HOME, and a fake /proc —
    the live-process leg must never read the REAL machine in tests."""
    prefix = tmp_path / "fhs"
    prefix.mkdir()
    monkeypatch.setattr(fhs, "default_prefix", lambda: prefix)
    home = tmp_path / "doctor-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(doctor_checks, "_PROC_PATH", proc)
    return {"prefix": prefix, "proc": proc, "units": prefix / "etc/systemd/system"}


def _unit(path: Path, *lines: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(("[Unit]", "Description=sim", *lines)) + "\n", encoding="utf-8")
    return path


def _pid(proc: Path, pid: str, comm: str, cgroup: str | None) -> None:
    pid_dir = proc / pid
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / "comm").write_text(comm + "\n", encoding="utf-8")
    if cgroup is not None:
        (pid_dir / "cgroup").write_text(cgroup + "\n", encoding="utf-8")


def _benign_pid(proc: Path, pid: str = "1") -> None:
    _pid(proc, pid, "systemd", "0::/init.scope")


def _dr23(proc: Path) -> doctor_checks.Finding:
    finding = doctor_checks._dr23(proc_root=proc)
    assert finding.check_id == "DR-23"
    return finding


# ── Leg (a): root units on foreign homes — FAIL ───────────────────────


def test_root_unit_exec_into_user_home_fails(platform: dict[str, Path]) -> None:
    """The DE-2 shape: a root-owned unit exec'ing a script from a user
    home — root executing a user-writable file, the LPE class."""
    script = platform["prefix"] / "var/home/alice/.local/share/mnemos-prod/cortex-metrics.sh"
    _unit(
        platform["units"] / "vesma-cortex-metrics.service",
        "[Service]",
        "Type=simple",
        f"ExecStart={script}",
    )
    finding = _dr23(platform["proc"])
    assert finding.severity is Severity.FAIL
    assert str(script) in (finding.detail or "")
    assert "vesma-cortex-metrics.service" in (finding.detail or "")
    assert "user-writable" in (finding.detail or "")
    assert finding.fix_command is not None
    assert "platform slot" in (finding.fix_command or "")


def test_environmentfile_on_user_home_fails(platform: dict[str, Path]) -> None:
    """The directive breadth: EnvironmentFile pointing into a user home
    is the same trust violation (root reads a user-controlled file)."""
    env_file = platform["prefix"] / "home/bob/.config/vesma-board/board.env"
    _unit(
        platform["units"] / "board-container.service",
        "[Service]",
        f"EnvironmentFile={env_file}",
        "ExecStart=/usr/bin/podman start board",
    )
    finding = _dr23(platform["proc"])
    assert finding.severity is Severity.FAIL
    assert str(env_file) in (finding.detail or "")


def test_systemd_double_bang_prefix_is_judged(platform: dict[str, Path]) -> None:
    """systemd's ``!!`` privilege-requirement prefix rides the SAME argv
    token as the path — it must not hide a foreign home from leg (a)
    (found on a real machine: ``ExecStart=!!/usr/lib/…``)."""
    script = platform["prefix"] / "var/home/alice/legacy.sh"
    _unit(
        platform["units"] / "pinned.service",
        "[Service]",
        f"ExecStart=!!{script}",
    )
    finding = _dr23(platform["proc"])
    assert finding.severity is Severity.FAIL
    assert str(script) in (finding.detail or "")


def test_paths_under_data_root_are_legal(platform: dict[str, Path]) -> None:
    """Paths under the ratified service data home are the platform's own
    (including the ``-`` absence marker of EnvironmentFile)."""
    env = platform["prefix"] / "var/lib/vesma/env/foo.env"
    _unit(
        platform["units"] / "healthy.service",
        "[Service]",
        f"EnvironmentFile=-{env}",
        "ExecStart=/usr/bin/env",
    )
    finding = _dr23(platform["proc"])
    assert finding.severity is Severity.OK


def test_canonical_system_unit_is_exempt(platform: dict[str, Path]) -> None:
    """The ratified system unit is never slandered by this check — its
    drift is DR-18's byte-compare subject."""
    _unit(
        fhs.system_unit_path(),
        "[Service]",
        "ExecStart=/var/home/alice/elsewhere",
    )
    finding = _dr23(platform["proc"])
    assert finding.severity is Severity.OK


def test_vendor_dir_vesma_unit_included(platform: dict[str, Path]) -> None:
    """A vesma-named unit in the vendor dir is in the scan universe (it
    is not platform-installed unless it IS the manifest's system unit)."""
    script = platform["prefix"] / "home/alice/legacy.sh"
    _unit(
        platform["prefix"] / "usr/lib/systemd/system/vesma-legacy.service",
        "[Service]",
        f"ExecStart={script}",
    )
    finding = _dr23(platform["proc"])
    assert finding.severity is Severity.FAIL
    assert "vesma-legacy.service" in (finding.detail or "")


def test_user_plane_unit_is_out_of_scope(platform: dict[str, Path]) -> None:
    """User-plane units (``~/.config/systemd/user``) are never scanned —
    ``$HOME`` is legal there by the layout contract."""
    user_unit = platform["prefix"] / "home/operator/.config/systemd/user/vesma-sidecar.service"
    _unit(user_unit, "[Service]", "ExecStart=/home/operator/sidecar.sh")
    finding = _dr23(platform["proc"])
    assert finding.severity is Severity.OK
    assert "sidecar" not in (finding.detail or "")


# ── Leg (b): legacy mnemos-* binary names — WARN ──────────────────────


def test_legacy_mnemos_binary_warns(platform: dict[str, Path]) -> None:
    """The DE-3 shape: a unit exec'ing the pre-rebrand mnemos-* name —
    WARN naming the rename contract, not a FAIL (it runs, just outside
    the platform's name table)."""
    _unit(
        platform["units"] / "mesh.service",
        "[Service]",
        "ExecStart=/usr/local/bin/mnemos-mesh --relay :29433",
    )
    finding = _dr23(platform["proc"])
    assert finding.severity is Severity.WARN
    assert "mnemos-mesh" in (finding.detail or "")
    assert "rename contract" in (finding.detail or "")
    assert finding.fix_command is not None


def test_legacy_name_under_data_root_still_warns(platform: dict[str, Path]) -> None:
    """The data-root exemption covers leg (a) only — a legacy NAME is a
    finding wherever it lives."""
    _unit(
        platform["units"] / "mesh.service",
        "[Service]",
        f"ExecStart={platform['prefix']}/var/lib/vesma/bin/mnemos-mesh",
    )
    finding = _dr23(platform["proc"])
    assert finding.severity is Severity.WARN
    assert "mnemos-mesh" in (finding.detail or "")


# ── Leg (c): live component-named processes — WARN, best-effort ───────


def test_live_component_process_outside_supervisor_warns(
    platform: dict[str, Path],
) -> None:
    """The DE-3/DE-5 shape: a vesmaro-named process owned by an ordinary
    user unit — a live component outside the platform's lifecycle."""
    _benign_pid(platform["proc"])
    _pid(
        platform["proc"],
        "4242",
        "vesmaro-agent",
        "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-mesh.slice",
    )
    finding = _dr23(platform["proc"])
    assert finding.severity is Severity.WARN
    assert "vesmaro-agent" in (finding.detail or "")
    assert "not owned by the supervisor" in (finding.detail or "")
    assert "2 scanned" in (finding.detail or "")


def test_live_supervised_component_process_is_ok(platform: dict[str, Path]) -> None:
    """A component-named process under vesma.service (or a vesma@
    instance) is the DESIGNED state — the supervisor owns it."""
    _benign_pid(platform["proc"])
    _pid(
        platform["proc"],
        "4242",
        "vesma-agent",
        "0::/system.slice/vesma.service",
    )
    _pid(
        platform["proc"],
        "4243",
        "vesma-mesh",
        "12:devices:/system.slice/vesma@default.service",
    )
    finding = _dr23(platform["proc"])
    assert finding.severity is Severity.OK
    assert "3 scanned" in (finding.detail or "")


def test_proc_unavailable_degrades_honestly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No /proc at all — an honest status line, never an error."""
    prefix = tmp_path / "fhs"
    prefix.mkdir()
    monkeypatch.setattr(fhs, "default_prefix", lambda: prefix)
    home = tmp_path / "doctor-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(doctor_checks, "_PROC_PATH", tmp_path / "absent-proc")
    finding = doctor_checks._dr23()
    assert finding.check_id == "DR-23"
    assert finding.severity is Severity.OK
    assert "scan unavailable" in (finding.detail or "")


def test_proc_partial_readability_degrades(platform: dict[str, Path]) -> None:
    """Entries without a readable comm are skipped silently — the count
    speaks for what was actually checked (the honest degrade)."""
    _benign_pid(platform["proc"], "7")
    ghost = platform["proc"] / "8"
    ghost.mkdir()
    finding = _dr23(platform["proc"])
    assert finding.severity is Severity.OK
    assert "1 scanned" in (finding.detail or "")


# ── Registry ──────────────────────────────────────────────────────────


def test_check_is_in_the_registry(platform: dict[str, Path]) -> None:
    checks = {finding.check_id: finding for finding in doctor_checks.run_service_checks()}
    assert "DR-23" in checks
    assert checks["DR-23"].title == "artifacts outside the platform"
