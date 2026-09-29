"""Native auto-indexing — ADR-0032 wave PG-0.5 (owner directive 2026-09-29).

Indexation happens BY ITSELF: the first contact with a project through
the MCP dispatcher or the ``pre_llm_call`` hook emits an activity HINT,
and this module turns hints into background work on the shared
scheduler thread — no explicit ``mnemos_index_project`` call, no
instruction, no skill. Design invariants (PG1-PG7 unchanged):

* **Hints never block and never break the caller** —
  :meth:`AutoIndexer.hint` is a bounded-queue ``put`` + a one-shot
  scheduler ``submit`` and nothing else; the caller's tool call cannot
  fail because of a hint (the manager wiring additionally wraps it in
  a silent try/except).
* **Marker-gated auto-registration** — an UNREGISTERED project is
  auto-registered only when its cwd carries a cheap project marker
  (``.git`` / ``pyproject.toml`` / ``package.json`` / ``go.mod`` /
  ``Cargo.toml``). A bare directory never enters the ``projects``
  table through the auto path (PG2 stays operator-shaped, the marker
  IS the operator's footprint).
* **The manual paths keep their contracts** — auto work goes through
  the same :meth:`CodeGraphService.index_project` serialization,
  PG7 limits and audit trail as manual runs; only the ``reason``
  (``auto-first`` / ``auto-stale``) and the actor (the hinting agent)
  differ.
* **Throttled per project** — ``auto_reindex_min_interval_sec`` gates
  consecutive auto actions through the sidecar ``graph_meta`` stamp
  ``last_auto_action:{len}:{project}``. The stamp is written BEFORE
  the action runs (reserve-then-act): a failing auto index is not
  retried on every subsequent hint — the failure is audited, the
  operator investigates, the manual tools remain available.
* **v1 limitation** — a multi-path registration indexes ``paths[0]``
  only (the ``_resolve_root`` rule); pinned here until PG-1 revisits
  multi-root projects.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from vesmaro.codegraph.incremental import staleness_check
from vesmaro.codegraph.service import CodeGraphService, GraphToolError
from vesmaro.codegraph.watch import GraphWatchScheduler
from vesmaro.config import CodeGraphConfig
from vesmaro.models import Project

logger = logging.getLogger(__name__)

#: Cheap project markers (PG-0.5 auto-registration gate): the operator's
#: footprint on disk. ``.git`` covers non-packaged repos; the rest are
#: the canonical packaging manifests of the supported ecosystems.
PROJECT_MARKERS: tuple[str, ...] = (
    ".git",
    "pyproject.toml",
    "package.json",
    "go.mod",
    "Cargo.toml",
)

#: Hard bound on the hint queue — auto-indexing is best-effort, a hint
#: flood must never grow memory (drops are silent by the same logic as
#: the throttle skip).
HINT_QUEUE_MAX = 256

#: Sidecar ``graph_meta`` key prefix for the per-project throttle stamp
#: (length-prefixed, the same colon-safety discipline as the other keys).
_LAST_AUTO_ACTION_PREFIX = "last_auto_action:"


def _throttle_key(project: str) -> str:
    return f"{_LAST_AUTO_ACTION_PREFIX}{len(project)}:{project}"


def project_marker(cwd: str) -> str | None:
    """The FIRST project marker found in ``cwd`` (a stat per candidate —
    cheap by contract), or ``None`` when the directory is not a project
    root. A non-directory ``cwd`` never registers."""
    if not os.path.isdir(cwd):
        return None
    for marker in PROJECT_MARKERS:
        if os.path.exists(os.path.join(cwd, marker)):
            return marker
    return None


@dataclass(slots=True, frozen=True)
class _Hint:
    """One queued activity hint (who touched which project, where)."""

    project_id: str
    cwd: str
    agent: str
    session: str | None
    ts: float


class AutoIndexer:
    """Hint consumer: queue → one drain job on the shared scheduler.

    The drain job is COALESCED (``_drain_pending``): any number of hints
    landing while a drain is queued/running produce exactly one more
    drain, never one job per hint (no thread-per-hint, no queue storm).
    """

    def __init__(
        self,
        service: CodeGraphService,
        scheduler: GraphWatchScheduler,
        *,
        config: CodeGraphConfig | None = None,
        queue_max: int = HINT_QUEUE_MAX,
    ) -> None:
        self._service = service
        self._scheduler = scheduler
        self._config = config or service.config
        self._queue: queue.Queue[_Hint] = queue.Queue(maxsize=queue_max)
        self._drain_lock = threading.Lock()
        self._drain_pending = False

    # ── producer side (manager.codegraph_activity_hint) ────────────────────

    def hint(
        self,
        project_id: str,
        *,
        cwd: str,
        agent: str | None,
        session: str | None = None,
    ) -> bool:
        """Record first-contact activity — NEVER blocks, NEVER raises.

        Returns whether the hint was queued (``False`` = flag off, no
        agent attribution (PG7 — the auto actor is the hinting agent),
        or the bounded queue dropped it). No scheduler join happens
        here: the work rides the one-shot drain job.
        """
        if not self._config.enabled or not self._config.auto_index:
            return False
        if not isinstance(project_id, str) or not project_id.strip():
            return False
        if not isinstance(agent, str) or not agent.strip():
            return False  # PG7 binding: the auto path is attributed or absent
        if session is not None and (not isinstance(session, str) or not session.strip()):
            session = None
        hint = _Hint(
            project_id=project_id.strip(),
            cwd=os.path.abspath(cwd),
            agent=agent.strip(),
            session=session,
            ts=time.time(),
        )
        try:
            self._queue.put_nowait(hint)
        except queue.Full:
            logger.debug("codegraph-autoindex: hint dropped (queue full)")
            return False
        with self._drain_lock:
            if self._drain_pending:
                return True  # a drain already covers this hint
            self._drain_pending = True
        self._scheduler.submit(self._run_drain)
        return True

    def pending_hints(self) -> int:
        """Queue depth (observability / test assertions)."""
        return self._queue.qsize()

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        """Deterministic test join: queue empty AND the scheduler has
        finished every submitted job."""
        deadline = time.monotonic() + timeout
        while True:
            if self._queue.empty() and self._scheduler.wait_for_idle(timeout=0.05):
                return True
            if time.monotonic() >= deadline:
                return self._queue.empty() and self._scheduler.wait_for_idle(timeout=0.05)
            time.sleep(0.01)

    def close(self) -> None:
        """Drop queued hints (manager close path — the scheduler itself
        is closed by its owner)."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    # ── consumer side (the one-shot drain job) ─────────────────────────────

    def _run_drain(self) -> None:
        """Drain every hint that is queued NOW; hints landing mid-drain
        schedule their own follow-up drain (the flag resets BEFORE the
        loop starts, so no wakeup can be lost)."""
        with self._drain_lock:
            self._drain_pending = False
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._process(item)
            except Exception:
                # Fail-soft by contract: the audit trail already carries
                # what the service-layer refusal recorded; the next hint
                # (after the throttle window) retries.
                logger.warning(
                    "codegraph-autoindex: hint for %s failed", item.project_id, exc_info=True
                )

    def _process(self, hint: _Hint) -> None:
        """One hint: auto-register (marker-gated) → first index or stale
        reindex, behind the per-project throttle. A concurrent index on
        the same project is NOT waited for: ``index_project`` returns
        ``in-progress`` immediately — for the auto path that is a silent
        skip, never a queued retry."""
        main = self._service.main
        project = main.get_project(hint.project_id) or main.get_project_by_name(hint.project_id)
        if project is None:
            marker = project_marker(hint.cwd)
            if marker is None:
                logger.debug(
                    "codegraph-autoindex: %s not registered and cwd %s has no project marker — skip",
                    hint.project_id,
                    hint.cwd,
                )
                return
            registered_at = datetime.now(UTC).isoformat()
            project = Project(
                name=hint.project_id,
                paths=[hint.cwd],
                description=(
                    f"auto-registered by {hint.agent} at {registered_at} "
                    f"(marker {marker}; PG-0.5 native auto-index)"
                ),
            )
            main.save_project(project)
            self._service.audit.record(
                hint.project_id,
                "auto-register",
                hint.agent,
                session=hint.session,
                reason=f"auto-register (marker {marker})",
                details={"root": hint.cwd},
            )
            logger.info(
                "codegraph-autoindex: auto-registered %s from %s (marker %s)",
                hint.project_id,
                hint.cwd,
                marker,
            )
        try:
            graph_key, root = self._service.watch_probe(hint.project_id)
        except GraphToolError as exc:
            logger.info("codegraph-autoindex: %s unresolvable (%s) — skip", hint.project_id, exc)
            return
        if self._throttled(graph_key):
            return
        if self._service.store.count_files(graph_key) == 0:
            self._service.index_project(
                hint.project_id,
                agent=hint.agent,
                session=hint.session,
                incremental=True,
                reason="auto-first",
            )
            return
        report = staleness_check(graph_key, root, self._service.store)
        if not report.changed_files:
            return  # fresh — the beacon already says so; nothing to do
        self._service.index_project(
            hint.project_id,
            agent=hint.agent,
            session=hint.session,
            incremental=True,
            reason="auto-stale",
        )

    def _throttled(self, graph_key: str) -> bool:
        """Reserve-then-act throttle: read the ``last_auto_action`` stamp,
        and when the window is open WRITE it immediately (before any
        indexing) so a failing run is not retried on every hint."""
        interval = self._config.auto_reindex_min_interval_sec
        now = time.time()
        stamp = self._service.store.get_meta(_throttle_key(graph_key))
        try:
            last = float(stamp) if stamp else 0.0
        except ValueError:
            last = 0.0
        if now - last < interval:
            return True
        self._service.store.set_meta(_throttle_key(graph_key), repr(now))
        return False
