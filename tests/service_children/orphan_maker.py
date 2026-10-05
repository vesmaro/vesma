"""Orphan maker (SL-05 helper): spawns a grandchild and exits immediately —
the grandchild outlives its parent and must be reparented to the
PR_SET_CHILD_SUBREAPER supervisor and reaped by it (no zombie, no init).
"""

from __future__ import annotations

import argparse
import json
import os
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--gc-seconds", type=float, default=1.0)
    args = parser.parse_args()
    grandchild = os.fork()
    if grandchild == 0:
        time.sleep(args.gc_seconds)
        os._exit(0)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "grandchild": grandchild}, fh)
    os._exit(0)  # immediate parent death orphans the grandchild


if __name__ == "__main__":
    main()
