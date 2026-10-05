"""SL-02(b) fixture (cascade 2026-10-05 P2-F): the in-process entrypoint
factory raises SystemExit — a BaseException. The supervisor must treat the
raise as a CHILD failure (record + transition into backoff), never as the
death of the supervision thread.
"""

from __future__ import annotations


def create_component() -> object:
    raise SystemExit(3)


def health() -> dict[str, str]:
    return {"state": "failed", "detail": "factory never completed"}
