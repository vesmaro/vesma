"""PID1 conformance driver (SL-06 helper): runs AS PID 1 inside
`unshare --pid --fork --mount-proc <python> pid1_driver.py ...`.

Sequence: start a supervisor with one sleeper child; wait for healthy;
inject an orphaned grandchild (a forked process whose parent dies);
SIGTERM ourselves; assert run() returns 0, the child was stopped
gracefully and everything (including the orphan) was reaped — then
report the summary on stdout for the outer test to parse.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

from vesmaro.service.manifest import load_manifest
from vesmaro.service.supervisor import Supervisor


def _proc_exists(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rpartition(")")[2].split()[0] != "Z"
    except OSError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--journal-dir", required=True)
    args = parser.parse_args()

    assert os.getpid() == 1, "this driver must run as PID 1 inside the namespace"
    manifest = load_manifest(args.manifest)
    supervisor = Supervisor({manifest.name: manifest}, pid1=True)
    supervisor.start()
    deadline = time.monotonic() + 30.0
    while supervisor.component_state(manifest.name) != "healthy":
        if time.monotonic() > deadline:
            print(json.dumps({"ok": False, "why": "child never became healthy"}))
            sys.exit(1)
        time.sleep(0.05)

    orphan = os.fork()
    if orphan == 0:
        time.sleep(0.5)
        os._exit(0)  # parent (this driver's original pid) outlives it briefly

    os.kill(os.getpid(), signal.SIGTERM)  # handler → request_shutdown (PID1 MUST)
    exit_code = supervisor.shutdown()  # synchronous graceful stop + reap

    child_pid = supervisor.snapshot()["components"][manifest.name]["pid"]
    time.sleep(0.3)  # give the reaper a beat to drain the orphan
    summary = {
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "child_pid": child_pid,
        "child_gone": not _proc_exists(int(child_pid)),
        "pid1": supervisor.pid1,
    }
    print(json.dumps(summary))
    sys.exit(0 if summary["ok"] and summary["child_gone"] else 1)


if __name__ == "__main__":
    main()
