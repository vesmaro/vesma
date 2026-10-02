"""Scheduler-semantics tests for the project-graph watch poll (P3 tail).

The wiring suite (``test_codegraph_wiring.py``) pins the manager-level
behavior; THIS file pins the ``GraphWatchScheduler`` lifecycle invariants
directly, over a stub service (no sidecar, no tree):

* **stop → register is never a silent orphan** (review 10173a2a-3): a
  registration that lands in the thread-exit window must still be
  polled — the exit decision and its observable commit
  (``_thread = None``) happen in one lock-held section, so ``register``
  either reuses a thread that WILL re-evaluate, or starts a fresh one;
* **full stop commits the exit** — the thread slot is released by the
  thread itself (never by ``stop``), so no second poller can be started
  over a live one.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from vesma.codegraph.service import GraphToolError
from vesma.codegraph.watch import GraphWatchScheduler

#: Deadline for a poll to show up with the shrunk intervals below.
_POLL_DEADLINE_SEC = 3.0


class _StubStore:
    """The store surface the scheduler touches (count only)."""

    @staticmethod
    def count_files(graph_key: str) -> int:
        return 10


class _StubService:
    """The CodeGraphService surface the scheduler touches — records
    every poll call, never touches a sidecar or the disk."""

    def __init__(self) -> None:
        self.store = _StubStore()
        self.calls: list[str] = []

    def watch_probe(self, project_id: str) -> tuple[str, str]:
        return project_id, f"/watch-stub/{project_id}"

    def index_project(
        self,
        project_id: str,
        *,
        agent: str,
        session: str | None = None,
        incremental: bool = True,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(project_id)
        return {"status": "ok", "files_indexed": 1}


def _scheduler(service: _StubService) -> GraphWatchScheduler:
    return GraphWatchScheduler(
        service,
        base_interval_sec=0.02,
        interval_per_500_files=0.0,
        max_interval_sec=0.05,
    )


def _wait_polled(service: _StubService, at_least: int) -> None:
    deadline = time.monotonic() + _POLL_DEADLINE_SEC
    while time.monotonic() < deadline:
        if len(service.calls) >= at_least:
            return
        time.sleep(0.01)
    pytest.fail(f"registration was never polled (calls={service.calls!r})")


def test_stop_then_register_is_always_polled() -> None:
    """Review 10173a2a-3: stop → register in the exit window must NEVER
    leave the registration silently unpolled. Repeated back-to-back to
    actually hit the window."""
    service = _StubService()
    scheduler = _scheduler(service)
    try:
        for cycle in range(5):
            scheduler.register("proj", agent="t")
            _wait_polled(service, cycle * 2 + 1)
            polled_before = len(service.calls)
            # Drop the ONLY registration (exit flag set, no join) and
            # re-register IMMEDIATELY — the old code could observe the
            # still-alive exiting thread and orphan the registration.
            assert scheduler.stop("proj") == 1
            info = scheduler.register("proj", agent="t")
            assert info["status"] == "registered"
            _wait_polled(service, polled_before + 1)
    finally:
        scheduler.close()


def test_full_stop_commits_thread_exit() -> None:
    """The thread releases its own slot on exit (``stop`` never nulls a
    live thread — that would let a register start a SECOND poller)."""
    service = _StubService()
    scheduler = _scheduler(service)
    try:
        scheduler.register("proj", agent="t")
        assert scheduler.status()["running"] is True
        assert scheduler.stop() == 1  # full stop joins the poll thread
        assert scheduler._thread is None
        assert scheduler.status()["running"] is False
    finally:
        scheduler.close()


def test_register_after_close_is_refused_never_resurrects() -> None:
    """Review 57ae9a66-2: close() is FINAL. A late registration after a
    long running job outlived the 5s join must be an honest refusal —
    the poll thread must not come back to life."""
    service = _StubService()
    scheduler = _scheduler(service)
    scheduler.register("proj", agent="t")
    _wait_polled(service, 1)
    scheduler.close()
    with pytest.raises(GraphToolError, match="closed"):
        scheduler.register("proj", agent="t")
    assert scheduler._thread is None
    assert scheduler.status()["running"] is False
    assert service.calls, "sanity: the pre-close registration WAS polled"
