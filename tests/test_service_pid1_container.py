"""SL-06 true-container leg (specs/service-lifecycle/v1, PID1 §3.1).

Runs ``scripts/pid1_conformance.sh`` — the REAL Supervisor as the PID 1
of a throwaway container, SIGTERM'd, asserted from the captured output +
exit code (mandatory handlers → graceful stop, orphaned grandchild
reaped with no zombie, children stopped before exit, exit code 0).

Podman resolution, in order: in-box podman (one probe — historically
broken in sandboxes/distroboxes with ``newuidmap EPERM``), then the host
podman via ``distrobox-host-exec``. When NEITHER works the leg is a SKIP
whose reason carries the EXACT probe failure (honest gap); CI sandboxes
without containers stay green, and the owner host produces the real
evidence. The unit legs live in ``test_service_supervisor.py::TestSL06Pid1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "pid1_conformance.sh"

# First run may pull the image; subsequent runs are container-lifetime
# only (probe boot + graceful stop ≈ 15-30 s).
LEG_TIMEOUT_S = 600.0


def _probe(cmdline: list[str]) -> str | None:
    """None when this podman works, else the exact failure text."""
    try:
        proc = subprocess.run(
            [*cmdline, "info", "--format", "{{.Version}}"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{' '.join(cmdline)}: {exc.__class__.__name__}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return f"{' '.join(cmdline)}: rc={proc.returncode}: {detail[-1][:200] if detail else ''}"
    return None


def _resolve_podman() -> tuple[list[str] | None, str]:
    failures: list[str] = []
    for cmdline in (["podman"], ["distrobox-host-exec", "podman"]):
        if shutil.which(cmdline[0]) is None:
            failures.append(f"{cmdline[0]}: not installed")
            continue
        failure = _probe(cmdline)
        if failure is None:
            return cmdline, "ok"
        failures.append(failure)
    return None, "; ".join(failures)


def test_pid1_true_container_leg_sigterm_graceful_stop_exit_zero() -> None:
    cmdline, probe_note = _resolve_podman()
    if cmdline is None:
        pytest.skip(
            "no working podman for the SL-06 true-container leg "
            f"({probe_note}); handler/reap/graceful unit legs covered by "
            "test_service_supervisor.py::TestSL06Pid1"
        )
    env = {**os.environ, "PODMAN": " ".join(cmdline)}
    proc = subprocess.run(
        [str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=LEG_TIMEOUT_S,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, (
        f"SL-06 container leg failed (rc={proc.returncode})\n"
        f"--- stdout tail ---\n{proc.stdout[-4000:]}\n"
        f"--- stderr tail ---\n{proc.stderr[-2000:]}"
    )
    assert "SL06_CONTAINER_LEG PASS exit_code=0" in proc.stdout
    # The summary carries the machine-checked contract facts; the script
    # already validated them, the grep pins them into this test's record.
    assert '"child_gone": true' in proc.stdout
    assert '"zombies": []' in proc.stdout
    assert '"orphan_reaped_before_stop": true' in proc.stdout
