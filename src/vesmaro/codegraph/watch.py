"""The project-graph watch poll — contract §3.2 trigger (b), slice 6.

``watch_start`` stops being a stub: a LIGHT poll task registered inside
the server process — no daemon, no separate process (the engine is
exactly one long-lived process, ADR-0032 §3). Design invariants:

* **Cooperative scheduling** — ONE background thread for ALL
  registrations (never thread-per-project). The thread sleeps until the
  earliest due registration (adaptive interval: base + 1s per 500
  indexed files, capped — the DeusData model, contract §3.2), wakes,
  polls the due projects inline, sleeps again. It never competes for
  the request path beyond one serialized index run at a time (the
  incremental per-project lock registry is the serialization point).
* **Cheap checks, actual changes only** — a poll classifies the surface
  against ``graph_files`` by mtime+size (zero byte reads, no sha256 —
  the «не режь латентность» contract ban). Only when the classification
  finds actual changes does the poll run the incremental reindexation
  through ``CodeGraphService.index_project`` with ``reason='watch'`` —
  which is also where the PG7 audit event with the cause lands.
* **Lifetime binding** — an MCP/REST watch call carries no session
  context, so per-session caps are not enforceable; the slice-6 binding
  is the GLOBAL registration cap (``CodeGraphConfig.watch_max_registrations``,
  default 8) plus the manual ``watch_stop``. Registrations live for the
  process lifetime (in-memory; a restart drops them — the operator or
  agent re-registers).
* **Fail-soft poll** — the poll thread survives every error: a failed
  run records ``last_error`` and keeps the schedule; a project that
  lost its PG2 registration (or whose root vanished from disk) is
  unregistered automatically (the registration is dead by definition).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from vesmaro.codegraph.service import (
    CodeGraphService,
    GraphConfinementError,
    GraphToolError,
)

logger = logging.getLogger(__name__)

#: Thread name for the single poll thread (observability).
WATCH_THREAD_NAME = "codegraph-watch"


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class WatchRegistration:
    """One active watch registration (in-memory, process-lifetime)."""

    project_id: str
    graph_key: str
    root: str
    agent: str
    session: str | None
    registered_at: str
    next_run: float
    interval_sec: float
    runs: int = 0
    reindexes: int = 0
    last_run_at: str | None = None
    last_result: str | None = None
    last_error: str | None = None

    def as_status(self) -> dict[str, Any]:
        """The caller-facing projection (monotonic clock values stay
        internal — they are meaningless across processes/tests)."""
        return {
            "project": self.graph_key,
            "project_id": self.project_id,
            "root": self.root,
            "agent": self.agent,
            "session": self.session,
            "registered_at": self.registered_at,
            "interval_sec": round(self.interval_sec, 3),
            "runs": self.runs,
            "reindexes": self.reindexes,
            "last_run_at": self.last_run_at,
            "last_result": self.last_result,
            "last_error": self.last_error,
        }


class GraphWatchScheduler:
    """The single-thread cooperative poll over watch registrations.

    Owned by the ``MemoryManager`` (slice 6 wiring) and built over the
    per-manager ``CodeGraphService``; the poll interval model and the
    registration cap come from ``CodeGraphConfig`` via the manager.
    """

    def __init__(
        self,
        service: CodeGraphService,
        *,
        base_interval_sec: float = 5.0,
        interval_per_500_files: float = 1.0,
        max_interval_sec: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._base = base_interval_sec
        self._per_500 = interval_per_500_files
        self._cap_interval = max_interval_sec
        self._clock = clock
        self._registrations: dict[str, WatchRegistration] = {}  # by graph_key
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop_flag = threading.Event()
        self._thread: threading.Thread | None = None

    # ── registration surface (manager.watch_* call these) ────────────────

    def register(
        self,
        project_id: str,
        *,
        agent: str,
        session: str | None = None,
        cap: int = 8,
    ) -> dict[str, Any]:
        """Register one project for the poll (idempotent per project).

        The registration REFUSES when the project has no index yet: the
        poll reindexes an EXISTING index on actual changes — it never
        seeds a first index silently (that is ``index_project``'s
        explicit, audited job). Re-registering an already-watched
        project returns the existing registration unchanged.
        """
        graph_key, root = self._service.watch_probe(project_id)
        if self._service.store.count_files(graph_key) == 0:
            raise GraphToolError(
                f"project {project_id!r} has no index yet — call mnemos_index_project "
                "first (the watch poll reindexes; it never seeds a first index)"
            )
        with self._lock:
            existing = self._registrations.get(graph_key)
            if existing is not None:
                info = existing.as_status()
                info["status"] = "already-registered"
                return info
            if len(self._registrations) >= cap:
                raise GraphToolError(
                    f"watch registration cap ({cap}) reached — stop one with "
                    "watch_stop first (contract §3.2: capped registrations)"
                )
            interval = self._interval(graph_key)
            reg = WatchRegistration(
                project_id=project_id,
                graph_key=graph_key,
                root=root,
                agent=agent,
                session=session,
                registered_at=_utcnow_iso(),
                next_run=self._clock() + interval,
                interval_sec=interval,
            )
            self._registrations[graph_key] = reg
            self._ensure_thread_locked()
            self._wake.set()
            info = reg.as_status()
            info["status"] = "registered"
            return info

    def stop(self, project_id: str | None = None) -> int:
        """Drop one registration (by project id or graph key) or ALL of
        them when no argument is given; the poll thread exits once the
        registry empties. Returns the number of dropped registrations."""
        with self._lock:
            if project_id is None:
                dropped = len(self._registrations)
                self._registrations.clear()
            else:
                dropped = 0
                for key in [
                    k
                    for k, r in self._registrations.items()
                    if k == project_id or r.project_id == project_id
                ]:
                    del self._registrations[key]
                    dropped += 1
            if not self._registrations:
                self._stop_flag.set()
            self._wake.set()
        if project_id is None and dropped:
            # Best-effort bounded wait for the exit to be noticed: the
            # thread COMMITS its own exit (clears ``_thread`` under the
            # lock, see ``_run_loop``) — stop() must not null the slot
            # of a still-running thread, or a concurrent register would
            # start a SECOND poller over the same registry.
            thread = self._thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=5.0)
        return dropped

    def status(self) -> dict[str, Any]:
        """Active registrations + the last poll outcome per project."""
        with self._lock:
            regs = [r.as_status() for r in self._registrations.values()]
            thread = self._thread
        return {
            "running": bool(thread is not None and thread.is_alive()),
            "registrations": regs,
        }

    def close(self) -> None:
        """Full stop (manager ``close`` path)."""
        self.stop()

    # ── poll loop ──────────────────────────────────────────────────────────

    def _interval(self, graph_key: str) -> float:
        """The adaptive interval (contract §3.2): base + 1s per 500
        indexed files, capped. A sidecar count — no filesystem walk."""
        files = self._service.store.count_files(graph_key)
        raw = self._base + (files // 500) * self._per_500
        return min(raw, self._cap_interval)

    def _ensure_thread_locked(self) -> None:
        """Make sure a poll thread owns the loop (the lock is already
        held; called only from ``register``).

        The caller just added (or confirmed) a registration, so the loop
        MUST keep running: any pending exit flag is cleared whether we
        reuse the live thread or start a fresh one. ``_thread`` is
        non-``None`` only while some thread owns the loop and has NOT
        yet committed its exit (see ``_run_loop``) — a live handle here
        means that thread WILL re-evaluate the flag and the registry
        before leaving, so the new registration is guaranteed a poller;
        a fresh thread starts only after the previous one committed
        (``_thread is None``)."""
        self._stop_flag.clear()
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, name=WATCH_THREAD_NAME, daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        """The cooperative loop: poll everything due, sleep until the
        next due registration (or until ``register``/``stop`` wakes us
        to recompute). The wake event is consumed at the TOP of each
        iteration, so a registration that lands mid-poll or mid-sleep
        can never lose its wake-up (no bounded-delay miss).

        Exit protocol (review 10173a2a-3): the decision to leave and
        its observable commit — ``self._thread = None`` — happen in ONE
        lock-held critical section, and ``register`` decides under the
        same lock. A registration can therefore NEVER sit unpolled:
        either it lands before the decision (the flag was cleared and
        the registry is non-empty — the loop keeps running and polls
        it) or after the commit (a fresh thread starts). The old shape
        — ``register`` observing a live thread that had already decided
        to exit — was exactly the window where a registration was
        silently registered and never polled."""
        try:
            while True:
                self._wake.clear()
                with self._lock:
                    if self._stop_flag.is_set() or not self._registrations:
                        self._thread = None  # the exit commit — under the lock
                        return
                    now = self._clock()
                    due = [r for r in self._registrations.values() if r.next_run <= now]
                    next_due = min(r.next_run for r in self._registrations.values())
                for reg in due:
                    if self._stop_flag.is_set():
                        break  # the loop top commits the exit under the lock
                    self._poll_registration(reg)
                sleep = max(0.01, min(next_due - self._clock(), self._cap_interval))
                self._wake.wait(timeout=sleep)
        finally:
            # Crash insurance: a loop that died mid-iteration (outside
            # the lock) still releases the thread slot so a later
            # ``register`` restarts polling instead of trusting a dead
            # thread handle.
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _poll_registration(self, reg: WatchRegistration) -> None:
        """One poll tick for one registration: the cheap classification
        rides INSIDE ``index_project`` (incremental path — mtime+size,
        no hashing of fresh files); a publish lands an audit event with
        reason ``watch`` (PG7/§3.6), a no-change run audits nothing."""
        started = time.monotonic()
        try:
            result = self._service.index_project(
                reg.project_id,
                agent=reg.agent,
                session=reg.session,
                incremental=True,
                reason="watch",
            )
        except GraphConfinementError as exc:
            # The project lost its registration or its root — the poll
            # can never succeed again; drop the dead registration.
            logger.info("codegraph-watch: %s unregistered (%s)", reg.graph_key, exc)
            reg.last_error = f"confinement: {exc}"
            reg.last_run_at = _utcnow_iso()
            self.stop(reg.project_id)
            return
        except GraphToolError as exc:
            reg.last_error = f"{type(exc).__name__}: {exc}"
            reg.last_run_at = _utcnow_iso()
            logger.warning("codegraph-watch: poll of %s failed: %s", reg.graph_key, exc)
            return
        except Exception as exc:
            reg.last_error = f"{type(exc).__name__}: {exc}"
            reg.last_run_at = _utcnow_iso()
            logger.warning("codegraph-watch: poll of %s crashed: %s", reg.graph_key, exc)
            return
        duration = time.monotonic() - started
        status = str(result.get("status"))
        with self._lock:
            reg.runs += 1
            reg.last_error = None
            reg.last_run_at = _utcnow_iso()
            if status == "fresh":
                reg.last_result = "fresh (no changes)"
            elif status == "in-progress":
                reg.last_result = "in-progress (another index holds the project lock)"
            else:
                reg.reindexes += 1
                reg.last_result = (
                    f"reindexed ({status}): {result.get('files_indexed', '?')} files "
                    f"in {duration:.2f}s"
                )
            reg.interval_sec = self._interval(reg.graph_key)
            reg.next_run = self._clock() + reg.interval_sec
        logger.debug("codegraph-watch: %s poll → %s (%.3fs)", reg.graph_key, status, duration)
