"""SL-06 true-container PID1 probe: the REAL Supervisor as container PID 1.

Run INSIDE a container (its PID namespace makes this process PID 1):

    python -m vesma.service.pid1_probe

Sequence (stdlib only, plus the ``vesma.service`` package under test):

1. write a minimal child manifest + sleeper script into a work dir
   (XDG roots pointed inside it, so the container layout stays private);
2. start the REAL supervisor (``prctl(PR_SET_CHILD_SUBREAPER)`` +
   mandatory SIGTERM/SIGINT handlers + reaper);
3. wait for the child to turn healthy;
4. double-fork an orphaned grandchild: the middle process dies at once,
   the grandchild reparents into the supervisor (the subreaper) and is
   reaped by its drain — the probe waits until ``/proc`` shows the
   grandchild FULLY GONE (a zombie would still be listed as state ``Z``),
   which only the supervisor could have arranged;
5. print a READY marker, then SIGTERM ourselves — the mandatory PID1
   handler must turn that into a graceful stop;
6. ``shutdown()`` (children stopped and reaped BEFORE the probe exits —
   the §3.4 journal in the summary is the ordering evidence), a final
   ``/proc`` zombie scan, and a JSON summary on stdout.

Exit code 0 iff: child stopped+reaped, no zombies, orphan reaped before
the stop, supervisor exit code 0. Host-side drivers:
``scripts/pid1_conformance.sh`` and ``tests/test_service_pid1_container.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

READY_MARKER = "VESMA_PID1_PROBE_READY"
SUMMARY_MARKER = "VESMA_PID1_SUMMARY"

_HEALTHY_TIMEOUT_S = 30.0
_ORPHAN_REAP_TIMEOUT_S = 15.0
_EXTERNAL_STOP_TIMEOUT_S = 60.0
_SETTLE_S = 0.3

MANIFEST_DOC: dict[str, object] = {
    "apiVersion": "vesma.component/v1",
    "kind": "child-process",
    "metadata": {
        "name": "pid1-probe-child",
        "version": "0.1.0",
        "tier": "optional",
        "description": "SL-06 PID1 container probe child",
        "provenance": {
            "repo": "https://example.com/vesma-pid1-probe",
            "license": "MIT",
        },
    },
    "launch": {"argv": ["{{python}}", "{{workdir}}/child.py"]},
    "depends_on": [],
    "stop": {"signal": "SIGTERM", "grace_period": "2s"},
}

CHILD_SCRIPT = '''\
"""SL-06 probe child: sleeps until signalled (stdlib only)."""
import time

deadline = time.monotonic() + 600.0
while time.monotonic() < deadline:
    time.sleep(0.05)
'''


def _proc_state(pid: int) -> str | None:
    """State letter from ``/proc/<pid>/stat``, or None when gone (reaped)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None  # fully gone — the entry only disappears once reaped
    return stat.rpartition(")")[2].split()[0]


def _zombie_pids() -> list[int]:
    zombies: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text(encoding="utf-8")
        except OSError:
            continue
        if stat.rpartition(")")[2].split()[0] == "Z":
            zombies.append(int(entry))
    return zombies


def _spawn_orphan(workdir: Path) -> int:
    """Double-fork: middle parent dies at once, grandchild writes its pid
    and sleeps, then exits — orphaned into the supervisor (subreaper)."""
    pidfile = workdir / "orphan.pid"
    middle = os.fork()
    if middle == 0:
        grandchild = os.fork()
        if grandchild == 0:
            pidfile.write_text(str(os.getpid()), encoding="utf-8")
            time.sleep(0.5)
            os._exit(0)
        os._exit(0)  # the middle parent dies — grandchild is orphaned
    return middle  # caller only needs to know a fork happened


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SL-06 PID1 conformance probe (run as container PID 1)"
    )
    parser.add_argument("--workdir", default=None, help="scratch dir (default: mkdtemp)")
    args = parser.parse_args()

    if os.getpid() != 1:
        print(
            f"{SUMMARY_MARKER} "
            f"{json.dumps({'ok': False, 'why': f'probe pid is {os.getpid()}, not 1'})}",
            flush=True,
        )
        return 2

    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="vesma-pid1-"))
    workdir.mkdir(parents=True, exist_ok=True)
    for var in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
    ):
        target = workdir / var.lower()
        target.mkdir(exist_ok=True)
        os.environ[var] = str(target)

    # Deferred imports: the failure modes above must not need the package.
    from vesma.service.logsink import HistoryJournal
    from vesma.service.manifest import load_manifest
    from vesma.service.supervisor import Supervisor

    manifest_path = workdir / "pid1-probe-child.yaml"
    manifest_path.write_text(
        json.dumps(MANIFEST_DOC)
        .replace("{{python}}", sys.executable)
        .replace("{{workdir}}", str(workdir)),
        encoding="utf-8",
    )
    (workdir / "child.py").write_text(CHILD_SCRIPT, encoding="utf-8")

    journal = HistoryJournal(workdir / "history")
    manifest = load_manifest(manifest_path)
    supervisor = Supervisor({manifest.name: manifest}, pid1=True, journal=journal)
    supervisor.start()

    deadline = time.monotonic() + _HEALTHY_TIMEOUT_S
    while supervisor.component_state(manifest.name) != "healthy":
        if time.monotonic() > deadline:
            print(
                f"{SUMMARY_MARKER} "
                f"{json.dumps({'ok': False, 'why': 'child never became healthy'})}",
                flush=True,
            )
            return 2
        time.sleep(0.05)

    _spawn_orphan(workdir)

    # The grandchild must be REAPED (gone from /proc, not a zombie) while
    # the supervisor is up — only its reaper drain can have done that.
    orphan_pid = -1
    deadline = time.monotonic() + _ORPHAN_REAP_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            orphan_pid = int((workdir / "orphan.pid").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(0.05)
            continue
        if _proc_state(orphan_pid) is None:
            break  # fully reaped — no zombie survives
        time.sleep(0.05)
    orphan_reaped = orphan_pid > 0 and _proc_state(orphan_pid) is None

    print(f"{READY_MARKER} orphan_pid={orphan_pid} orphan_reaped={orphan_reaped}", flush=True)

    # Wait for the EXTERNAL SIGTERM — the container manager's stop path
    # (scripts/pid1_conformance.sh sends it to the container's PID 1).
    # The mandatory PID1 handler must turn it into a shutdown request.
    deadline = time.monotonic() + _EXTERNAL_STOP_TIMEOUT_S
    while not supervisor.shutdown_requested:
        if time.monotonic() > deadline:
            print(
                f"{SUMMARY_MARKER} "
                f"{json.dumps({'ok': False, 'why': 'external SIGTERM never arrived'})}",
                flush=True,
            )
            return 3
        time.sleep(0.1)

    exit_code = supervisor.shutdown()

    time.sleep(_SETTLE_S)  # give the final reaper sweep a beat
    child_pid = supervisor.snapshot()["components"][manifest.name]["pid"]
    final_state = supervisor.snapshot()["components"][manifest.name]["state"]
    zombies = _zombie_pids()
    try:
        journal_lines = journal.path.read_text(encoding="utf-8").splitlines()
    except OSError:
        journal_lines = []

    summary = {
        "ok": (
            exit_code == 0
            and _proc_state(child_pid) is None
            and not zombies
            and final_state == "stopped"
            and orphan_reaped
        ),
        "pid1": True,
        "exit_code": exit_code,
        "child_pid": child_pid,
        "child_gone": _proc_state(child_pid) is None,
        "child_final_state": final_state,
        "zombies": zombies,
        "orphan_pid": orphan_pid,
        "orphan_reaped_before_stop": orphan_reaped,
        "journal": journal_lines,
    }
    print(f"{SUMMARY_MARKER} {json.dumps(summary)}", flush=True)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
