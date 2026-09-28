"""Incremental indexing + the slice-4/6 facade (ADR-0032 PG-0 slice 3).

Incrementality contract (ArchCom 2026-09-28 §3.2):

* ``graph_files`` is the classification base: mtime + size is the
  cheap key; sha256 is computed ONLY for files whose key changed
  (the full-index path hashes every file it parses — the incremental
  path never touches fresh ones). UNCHANGED files are not
  re-classified, their records survive; STALE and NEW files are
  re-parsed; REMOVED files drop out with the subtree delete (their
  records are simply not re-written).
* Every publish is atomic: DELETE the project subtree + bulk insert
  via the slice-1 store API — the graph never shows a half-updated
  project, and a PG7 limit breach aborts BEFORE any store write (the
  previous graph survives untouched).
* Indexation is SERIALIZED per project: an in-process lock registry
  (module-level ``threading.Lock`` map — deliberately NOT a
  MemoryManager member; the manager wiring is a later slice). A
  concurrent call does NOT block forever — it returns the
  ``in-progress`` status immediately (contract §3.2 trigger (c)).

Wave-1 note: when the classification finds changes, the publish
re-parses the whole surface — the slice-1 store has no node READ
API, so an unchanged file's nodes cannot be carried from the store
into the new transaction. The classification itself stays honest
and cheap (mtime+size, zero re-parse on a fresh tree), the rebuild
is idempotent (same bytes → same ids), and ``files_indexed`` /
``IndexResult`` report the true shape. Narrowing the re-parse to the
stale subset lands with the slice-4 node-read surface.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

from vesmaro.codegraph.file_surface import FileSurface
from vesmaro.codegraph.indexer import (
    IndexResult,
    ProjectIndexer,
    stamp_freshness,
)
from vesmaro.config import CodeGraphConfig
from vesmaro.storage.code_graph_store import CodeGraphStore, GraphFileRecord

logger = logging.getLogger(__name__)

#: IndexResult.status when the project is already being indexed
#: (contract: concurrent calls never block indefinitely).
STATUS_IN_PROGRESS = "in-progress"

#: IndexResult.status when nothing changed and NO publish happened
#: (reading must not fake freshness — no epoch bump on this path).
STATUS_FRESH = "fresh"

#: Sidecar meta key prefix: last successful (re)index ISO-8601 UTC stamp.
_LAST_INDEXED_PREFIX = "last_indexed:"


class _MetaSurface(Protocol):
    """Main-store meta surface (structural, slice-1 Protocol twin)."""

    def get_meta(self, key: str) -> str | None: ...

    def set_meta(self, key: str, value: str) -> None: ...


@dataclass(frozen=True, slots=True)
class StalenessReport:
    """Cheap freshness report — NO reindexing, mtime+size only.

    ``changed_files`` lists repo-relative paths that are new, stale
    or removed; ``fresh_percent`` is the unchanged share of the
    on-surface files, rounded to 0.1.
    """

    changed_files: list[str] = field(default_factory=list)
    total_files: int = 0
    fresh_percent: float = 100.0
    last_indexed_at: str | None = None


# ── per-project lock registry ────────────────────────────────────────────────
# Module-level (NOT a MemoryManager member): the codegraph package owns
# its own serialization until the manager wiring slice moves the facade
# behind the manager. Keyed by project id; the map is bounded by the
# number of distinct projects — the same bound the sidecar store itself
# already implies (one project subtree per project id).

_LOCK_REGISTRY: dict[str, threading.Lock] = {}
_LOCK_REGISTRY_GUARD = threading.Lock()


def _lock_for(project: str) -> threading.Lock:
    with _LOCK_REGISTRY_GUARD:
        lock = _LOCK_REGISTRY.get(project)
        if lock is None:
            lock = threading.Lock()
            _LOCK_REGISTRY[project] = lock
        return lock


def classify_files(
    surface_paths: dict[str, str],
    existing: list[GraphFileRecord],
) -> tuple[list[str], list[str], list[str]]:
    """Classify the surface against ``graph_files``.

    Returns ``(unchanged, stale_or_new, removed)`` of repo-relative
    paths. UNCHANGED = mtime and size match the record exactly (the
    cheap key — sha256 is never computed for them). STALE-OR-NEW =
    on the surface but absent or changed. REMOVED = recorded but no
    longer on the surface (denylisted paths count as removed — the
    surface is the single source of truth).
    """
    recorded: dict[str, GraphFileRecord] = {rec.path: rec for rec in existing}
    unchanged: list[str] = []
    stale: list[str] = []
    for rel, abs_path in surface_paths.items():
        rec = recorded.pop(rel, None)
        try:
            stat = os.stat(abs_path)
        except OSError:
            continue  # unreadable: not indexed, not classified
        if (
            rec is not None
            and rec.mtime is not None
            and rec.size is not None
            and (stat.st_mtime, stat.st_size) == (rec.mtime, rec.size)
        ):
            unchanged.append(rel)
        else:
            stale.append(rel)
    return sorted(unchanged), sorted(stale), sorted(recorded.keys())


def staleness_check(
    project: str,
    root: str | os.PathLike[str],
    store: CodeGraphStore,
) -> StalenessReport:
    """Cheap staleness report: mtime+size classification only.

    No parsing, no writes, no epoch bump — the session-start path
    (contract §3.2 trigger (c)) must stay read-only.
    """
    surface = FileSurface(root).collect()
    surface_paths = {sf.rel_path: sf.abs_path for sf in surface}
    unchanged, stale, removed = classify_files(surface_paths, store.get_file_records(project))
    total = len(unchanged) + len(stale)
    fresh = 100.0 if total == 0 else round(100.0 * len(unchanged) / total, 1)
    return StalenessReport(
        changed_files=sorted([*stale, *removed]),
        total_files=total,
        fresh_percent=fresh,
        last_indexed_at=store.get_meta(f"{_LAST_INDEXED_PREFIX}{len(project)}:{project}"),
    )


def index_project(
    project: str,
    root: str | os.PathLike[str],
    store: CodeGraphStore,
    main_store: _MetaSurface,
    config: CodeGraphConfig | None = None,
    *,
    incremental: bool = True,
) -> IndexResult:
    """Facade for slices 4/6 (NOT an MCP tool — the tool layer wraps
    this in the PG2 root-registration checks).

    Serialized per project: a concurrent call returns
    ``IndexResult(status='in-progress')`` immediately — it never
    blocks indefinitely. On an actual (re)publish the sidecar
    freshness meta is stamped and the MAIN-DB
    ``project_graph_epoch`` bumped (slice-1 helpers); a no-change
    run bumps NOTHING (reading must not fake freshness).
    """
    lock = _lock_for(project)
    if not lock.acquire(blocking=False):
        logger.info("codegraph: index already in progress for %s", project)
        return IndexResult(status=STATUS_IN_PROGRESS)
    try:
        indexer = ProjectIndexer(store, config)
        result = _index_serialized(indexer, project, root, incremental)
        if result.status != STATUS_FRESH:
            stamp_freshness(store, main_store, project)
        return result
    finally:
        lock.release()


def _index_serialized(
    indexer: ProjectIndexer,
    project: str,
    root: str | os.PathLike[str],
    incremental: bool,
) -> IndexResult:
    """One serialized index run (the lock is already held).

    ``incremental=True`` with a non-empty existing index first runs
    the cheap mtime+size classification:

    * nothing changed → ``fresh`` result, NO store writes, NO epoch
      bump (the facade checks ``status`` before stamping);
    * something changed → atomic full rebuild (wave-1 note in the
      module docstring) — the removed files' nodes and records go
      with the subtree delete, stale/new files re-parse to the same
      deterministic ids.

    ``incremental=False`` (or an empty index) is the plain full
    rebuild via ``ProjectIndexer.index_full``.
    """
    root = os.fspath(root)
    existing = indexer.store.get_file_records(project)
    if not incremental or not existing:
        return indexer.index_full(project, root)

    started = time.perf_counter()
    surface = FileSurface(root).collect()
    indexer.check_limits(surface, root)
    surface_paths = {sf.rel_path: sf.abs_path for sf in surface}
    unchanged, stale, removed = classify_files(surface_paths, existing)
    if not stale and not removed:
        return IndexResult(
            nodes=indexer.store.count_nodes(project),
            edges=indexer.store.count_edges(project),
            files_indexed=len(unchanged),
            duration=time.perf_counter() - started,
            incremental=True,
            status=STATUS_FRESH,
        )
    logger.info(
        "codegraph: incremental index %s: %d unchanged, %d stale/new, %d removed → atomic rebuild",
        project,
        len(unchanged),
        len(stale),
        len(removed),
    )
    result = indexer.index_full(project, root)
    result.incremental = True
    return result
