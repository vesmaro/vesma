"""Sleeper child (conformance helper): stays alive, dies on SIGTERM/SIGKILL."""

from __future__ import annotations

import argparse
import os
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=600.0)
    parser.add_argument("--pidfile")
    args = parser.parse_args()
    if args.pidfile:
        with open(args.pidfile, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        time.sleep(0.05)


if __name__ == "__main__":
    main()
