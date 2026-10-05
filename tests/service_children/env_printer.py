"""Env printer (SL-13/SL-14 helper): dumps the EXACT child environment and
the fd table to a JSON file, so the runner can diff it against the
constructed allow-list (the 401-storm lesson: no host variable may leak).
"""

from __future__ import annotations

import argparse
import json
import os


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    fds: dict[str, str] = {}
    for entry in os.listdir("/proc/self/fd"):
        try:
            fds[entry] = os.readlink(f"/proc/self/fd/{entry}")
        except OSError as exc:
            fds[entry] = f"unreadable:{exc.__class__.__name__}"
    payload = {"env": dict(os.environ), "fds": fds, "pid": os.getpid()}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


if __name__ == "__main__":
    main()
