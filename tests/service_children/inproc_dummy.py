"""Tiny in-process component for supervisor tests (CM §3.6 shape: a
no-arg entrypoint factory + a module:attr health callback, no HTTP)."""

from __future__ import annotations

from typing import Any

_instance: Any = None


class DummyComponent:
    name = "dummy"
    kind = "in-process"

    def __init__(self) -> None:
        self.alive = True
        global _instance
        _instance = self

    def shutdown(self) -> None:
        self.alive = False


def create_component() -> DummyComponent:
    return DummyComponent()


def health() -> dict[str, str]:
    if _instance is None or not _instance.alive:
        return {"state": "failed", "detail": "dummy instance not alive"}
    return {"state": "healthy"}
