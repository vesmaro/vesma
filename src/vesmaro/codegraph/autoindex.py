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
  auto-registered only when its cwd carries a packaging MANIFEST
  (``pyproject.toml`` / ``setup.py`` / ``package.json`` / ``go.mod`` /
  ``Cargo.toml``; lockfiles do not count). A bare ``.git`` is NOT a
  trigger (PR #443 review P2-2: a dotfiles ``$HOME`` is a git repo —
  the auto path must not swallow it), and ``$HOME``/the filesystem
  root are refused even when a manifest sits there.
* **One root = one graph** (PR #443 review P2-1) — before any
  auto-registration the existing projects are searched for one whose
  ``paths`` contain the resolved root (:meth:`CodeGraphService.
  find_project_by_root`); a hit means the hint's graph operations run
  under the EXISTING project (audit ``auto-register-reused``), never a
  duplicate ``projects`` row. A global cap
  (``auto_register_max_projects``) bounds how many projects the auto
  path may ever create — past it, a silent skip with audit
  ``auto-register-capped``, never an error to the caller.
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
* **A failed first index suspends the auto path** (PR #443 review
  P2-2) — any failure of an ``auto-first`` run writes the sidecar
  stamp ``auto_suspended:{len}:{project}=1``: further hints skip the
  tree entirely (no disk walk) until a SUCCESSFUL manual
  ``index_project`` (the watch poll rides the same method) or a
  ``delete_graph_project`` lifts the flag. An ``auto-stale`` run over
  an existing valid index never suspends anything.
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
from pathlib import Path

from vesmaro.codegraph.incremental import staleness_check
from vesmaro.codegraph.service import (
    CodeGraphService,
    GraphToolError,
    auto_suspended_key,
)
from vesmaro.codegraph.watch import GraphWatchScheduler
from vesmaro.config import CodeGraphConfig
from vesmaro.models import Project

logger = logging.getLogger(__name__)

#: Packaging manifests accepted as auto-registration markers (PR #443
#: review P2-2): the operator's footprint on disk, the canonical
#: manifests of the supported ecosystems. A bare ``.git`` deliberately
#: does NOT qualify — a dotfiles ``$HOME`` is a repo, not a project —
#: and neither do lockfiles (generated artifacts, not declarations).
PROJECT_MARKERS: tuple[str, ...] = (
    "pyproject.toml",
    "setup.py",
    "package.json",
    "go.mod",
    "Cargo.toml",
)

#: Stable description prefix marking an AUTO-registered ``projects`` row
#: (the table has no ``registered_by`` column — the marker IS the
#: provenance, and ``auto_register_max_projects`` counts it).
AUTO_REGISTER_DESCRIPTION_PREFIX = "auto-registered by "

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
    """The FIRST packaging manifest found in ``cwd`` (a stat per
    candidate — cheap by contract), or ``None`` when the directory is
    not a project root. A non-directory ``cwd`` never registers; a bare
    ``.git`` is not a marker (P2-2)."""
    if not os.path.isdir(cwd):
        return None
    for marker in PROJECT_MARKERS:
        if os.path.exists(os.path.join(cwd, marker)):
            return marker
    return None


def _is_forbidden_root(cwd: str) -> bool:
    """``$HOME`` and the filesystem root are NEVER auto-registered
    (PR #443 review P2-2): even a manifest sitting there (a dotfiles
    repo exporting a ``package.json`` into ``$HOME``) must not turn the
    server's own home into a graph project."""
    root = Path(cwd)
    if str(root) == root.anchor:  # "/" on POSIX, "C:\\" on Windows
        return True
    try:
        return root == Path.home()
    except RuntimeError:  # no resolvable home — the manifest gate decides
        return False


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
        """One hint: auto-register (manifest-gated, one-root-one-graph,
        capped) → first index or stale reindex, behind the per-project
        throttle and the failed-first suspension. A concurrent index on
        the same project is NOT waited for: ``index_project`` returns
        ``in-progress`` immediately — for the auto path that is a silent
        skip, never a queued retry."""
        main = self._service.main
        project = main.get_project(hint.project_id) or main.get_project_by_name(hint.project_id)
        target = hint.project_id
        if project is None:
            marker = project_marker(hint.cwd)
            if marker is None:
                logger.debug(
                    "codegraph-autoindex: %s not registered, cwd %s has no manifest — skip",
                    hint.project_id,
                    hint.cwd,
                )
                return
            if _is_forbidden_root(hint.cwd):
                logger.debug(
                    "codegraph-autoindex: %s cwd %s is $HOME or the filesystem root — "
                    "auto-registration refused",
                    hint.project_id,
                    hint.cwd,
                )
                return
            existing = self._service.find_project_by_root(hint.cwd)
            if existing is not None:
                # One root = one graph (P2-1): no duplicate projects row,
                # no second full indexation — the hint rides the existing
                # project under its registered name.
                self._service.audit.record(
                    existing.name,
                    "auto-register-reused",
                    hint.agent,
                    session=hint.session,
                    reason="auto-register-reused (root already registered)",
                    details={"root": hint.cwd, "hint": hint.project_id},
                )
                logger.info(
                    "codegraph-autoindex: hint %s reuses registered root %s (project %s)",
                    hint.project_id,
                    hint.cwd,
                    existing.name,
                )
                project = existing
            elif self._auto_registered_count() >= self._config.auto_register_max_projects:
                self._service.audit.record(
                    hint.project_id,
                    "auto-register-capped",
                    hint.agent,
                    session=hint.session,
                    reason=(
                        "auto-register-capped "
                        f"(auto_register_max_projects={self._config.auto_register_max_projects})"
                    ),
                    details={"root": hint.cwd},
                )
                logger.info(
                    "codegraph-autoindex: auto-registration of %s skipped — cap %d reached",
                    hint.project_id,
                    self._config.auto_register_max_projects,
                )
                return
            else:
                registered_at = datetime.now(UTC).isoformat()
                project = Project(
                    name=hint.project_id,
                    paths=[hint.cwd],
                    description=(
                        f"{AUTO_REGISTER_DESCRIPTION_PREFIX}{hint.agent} at {registered_at} "
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
        target = project.name
        try:
            graph_key, root = self._service.watch_probe(target)
        except GraphToolError as exc:
            logger.info("codegraph-autoindex: %s unresolvable (%s) — skip", target, exc)
            return
        if self._suspended(graph_key):
            logger.debug(
                "codegraph-autoindex: %s auto path suspended (failed first index) — skip",
                graph_key,
            )
            return
        if self._throttled(graph_key):
            return
        if self._service.store.count_files(graph_key) == 0:
            try:
                self._service.index_project(
                    target,
                    agent=hint.agent,
                    session=hint.session,
                    incremental=True,
                    reason="auto-first",
                )
            except Exception:
                # A failed FIRST auto index suspends the auto path (P2-2):
                # the tree is not re-walked on every hint until a manual
                # index / delete / watch succeeds. The failure itself is
                # already audited by ``index_project`` (limit-refused) or
                # carried by this log line.
                self._service.store.set_meta(auto_suspended_key(graph_key), "1")
                logger.info(
                    "codegraph-autoindex: first auto index of %s failed — auto path "
                    "suspended until a successful manual index/delete/watch",
                    graph_key,
                    exc_info=True,
                )
            return
        report = staleness_check(graph_key, root, self._service.store)
        if not report.changed_files:
            return  # fresh — the beacon already says so; nothing to do
        self._service.index_project(
            target,
            agent=hint.agent,
            session=hint.session,
            incremental=True,
            reason="auto-stale",
        )

    def _auto_registered_count(self) -> int:
        """Projects created by the AUTO path, counted through the
        description marker (the ``projects`` table's only provenance
        column; operator registrations never match it)."""
        return sum(
            1
            for project in self._service.main.list_projects()
            if (project.description or "").startswith(AUTO_REGISTER_DESCRIPTION_PREFIX)
        )

    def _suspended(self, graph_key: str) -> bool:
        """Whether the auto path is suspended for this project (a failed
        first auto index; lifted by a successful publish or a delete)."""
        return self._service.store.get_meta(auto_suspended_key(graph_key)) == "1"

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
