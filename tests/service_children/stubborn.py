"""Stubborn child (SL-04 helper): ignores SIGTERM; spawns a grandchild that
also ignores SIGTERM; both only die via SIGKILL (the group kill). Writes
{"pid": ..., "grandchild": ...} to --out for the test to verify.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # the point of this child
    grandchild = os.fork()
    if grandchild == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            while True:
                time.sleep(0.2)
        except BaseException:
            os._exit(0)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "grandchild": grandchild}, fh)
    try:
        while True:
            time.sleep(0.2)
    except BaseException:
        os._exit(0)


if __name__ == "__main__":
    main()
