"""Line printer (W6 e2e helper): emits numbered lines on an interval until
killed or the deadline — the log-source component for the socket
``logs``/``logs follow`` legs (tail, stream until source stop)."""

from __future__ import annotations

import argparse
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=0.05)
    parser.add_argument("--prefix", default="line")
    parser.add_argument("--seconds", type=float, default=600.0)
    args = parser.parse_args()
    number = 0
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        print(f"{args.prefix}-{number}", flush=True)
        number += 1
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
