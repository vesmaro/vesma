"""Exiter child (conformance helper): exits immediately with a given code."""

from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", type=int, default=0)
    parser.add_argument("--pidfile")
    args = parser.parse_args()
    if args.pidfile:
        with open(args.pidfile, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    raise SystemExit(args.code)


if __name__ == "__main__":
    main()
