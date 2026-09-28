"""Sidecar SQLite store for the project code graph (ADR-0032 PG-0).

The project graph is MEMORY, not a per-process index: a separate
WAL-mode SQLite file (``code_graph.db``) in data_dir beside
``vectors.db``, shared by every agent of the server. The main DB is
untouched — a derived, rebuildable index must not be tied to chronicle
migrations and backups; extending ``memory_edges`` is forbidden (its
CHECK on ``kind``, the FK cascade into ``memories``, and the spent
one-shot rebuild window close that door).

PG1 (zero source bytes) is schema-enforced: the columns carry names,
qnames, repo-relative paths, line ranges, content hashes and edge
provenance — never source text, docstrings or literal defaults.
``signature`` is a plain column BY CONTRACT OF CONTENT (the caller
persists only the signature shape, no default values); PG1 is not a
``CHECK``-able predicate, it lives in the extraction layer (wave PG-0
slice 2) and in the audit review.

Security invariants (ADR-0032 §3.6): PG5 born-no-federate — the store
exposes no export surface; federation of graph artifacts rides a
separate ADR per the ADR-0016 pattern.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

#: Allowed node kinds (ADR-0032 §3.1; CHECK-enforced).
NODE_KINDS = (
    "Project",
    "File",
    "Module",
    "Class",
    "Function",
    "Method",
    "Type",
)

#: Allowed edge kinds (ADR-0032 §3.1; CHECK-enforced). ``USES`` is the
#: provenance-honest fallback — an unproven call is NEVER ``CALLS``.
EDGE_KINDS = (
    "CONTAINS_FILE",
    "DEFINES",
    "IMPORTS",
    "CALLS",
    "INHERITS",
    "TESTS",
    "USES",
)

#: ``meta`` key prefix for the per-project epoch (main DB). The
#: length-prefix segment makes the key colon-safe — the same discipline
#: as ``graph_epoch`` (manager.py, issue #415 review finding).
_PROJECT_GRAPH_EPOCH_PREFIX = "project_graph_epoch:"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_nodes (
    id          TEXT PRIMARY KEY,   -- <project>#<rel_path>#<symbol>#<line>
    project     TEXT NOT NULL,      -- logical FK to projects.id (main DB)
    kind        TEXT NOT NULL CHECK (kind IN
                  ('Project','File','Module','Class','Function','Method','Type')),
    name        TEXT NOT NULL,
    qname       TEXT NOT NULL,
    path        TEXT,               -- ALWAYS repo-relative (PG1)
    start_line  INTEGER,
    end_line    INTEGER,
    lang        TEXT,
    signature   TEXT,               -- NO defaults (PG1); NO docstrings/literals
    metadata    TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS project_edges (
    from_id     TEXT NOT NULL REFERENCES project_nodes(id) ON DELETE CASCADE,
    to_id       TEXT NOT NULL REFERENCES project_nodes(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN
                  ('CONTAINS_FILE','DEFINES','IMPORTS','CALLS','INHERITS','TESTS','USES')),
    weight      REAL NOT NULL DEFAULT 1.0,
    provenance  TEXT NOT NULL DEFAULT 'tree-sitter',
    PRIMARY KEY (from_id, to_id, kind),
    CHECK (from_id <> to_id)
);
CREATE TABLE IF NOT EXISTS graph_files (
    project     TEXT NOT NULL,
    path        TEXT NOT NULL,        -- repo-relative
    mtime       REAL,
    size        INTEGER,
    hash        TEXT,                 -- sha256 at index time (freshness, NOT content)
    parse_ok    INTEGER,
    parse_error TEXT,
    indexed_at  TEXT,
    PRIMARY KEY (project, path)
);
CREATE TABLE IF NOT EXISTS graph_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_project ON project_nodes(project, kind);
CREATE INDEX IF NOT EXISTS idx_nodes_qname ON project_nodes(qname);
CREATE INDEX IF NOT EXISTS idx_nodes_path ON project_nodes(project, path);
CREATE INDEX IF NOT EXISTS idx_edges_from ON project_edges(from_id, kind);
CREATE INDEX IF NOT EXISTS idx_edges_to ON project_edges(to_id, kind);
"""


@dataclass(frozen=True, slots=True)
class CodeGraphNode:
    """One ``project_nodes`` row (wave-1 write surface).

    ``id`` is the canonical ``<project>#<rel_path>#<symbol>#<line>``
    key built by the extraction layer (slice 2). ``signature`` carries
    the signature SHAPE only — no docstrings, no default values (PG1).
    """

    id: str
    project: str
    kind: str
    name: str
    qname: str
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    lang: str | None = None
    signature: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CodeGraphEdge:
    """One ``project_edges`` row. ``weight`` defaults to 1.0 at the SQL
    level; ``provenance`` defaults to ``'tree-sitter'`` there too —
    both stay optional here so thin call sites stay readable."""

    from_id: str
    to_id: str
    kind: str
    weight: float = 1.0
    provenance: str = "tree-sitter"


@dataclass(frozen=True, slots=True)
class GraphFileRecord:
    """One ``graph_files`` row — incrementality + coverage honesty
    («clean ≠ proof»: parse failures stay visible to the agent)."""

    project: str
    path: str
    mtime: float | None = None
    size: int | None = None
    hash: str | None = None
    parse_ok: int | None = None
    parse_error: str | None = None
    indexed_at: str | None = None


class MainStoreMeta(Protocol):
    """Meta-surface of the main ``SQLiteStore`` the epoch helper needs.

    A structural Protocol (not the concrete class) so the helper stays
    in the storage layer without importing ``manager``/``sqlite_store``
    (the sidecar must not create an import cycle into the main store).
    """

    def get_meta(self, key: str) -> str | None: ...

    def set_meta(self, key: str, value: str) -> None: ...


def project_graph_epoch_key(project: str) -> str:
    """Main-DB ``meta`` key for the project's graph epoch.

    Mirrors the ``graph_epoch`` key discipline exactly
    (``project_graph_epoch:{len}:{project}``) — the ``{len}`` segment
    makes the key colon-safe (issue #415 finding, applied to the graph
    epoch in manager.py); it is a sibling namespace, never parsed by
    ``graph_epoch`` consumers.
    """
    return f"{_PROJECT_GRAPH_EPOCH_PREFIX}{len(project)}:{project}"


def read_project_graph_epoch(main_store: MainStoreMeta, project: str) -> int:
    """Read the per-project graph epoch (0 = no graph writes yet)."""
    try:
        current = main_store.get_meta(project_graph_epoch_key(project))
    except Exception:
        return 0
    return int(current) if current else 0


def bump_project_graph_epoch(main_store: MainStoreMeta, project: str) -> int:
    """Increment the per-project ``project_graph_epoch`` in the MAIN DB.

    ADR-0032 §3.1 — freshness for the rest of the engine (federation,
    awareness, vitals) rides the main DB: consumers see the epoch
    without reading the sidecar. Per-project by the same committee
    ruling as ``graph_epoch`` (ArchCom 2026-09-27: a global epoch is a
    cheap fleet-wide cache-DoS lever). Durable and monotonic within a
    project; a meta failure is the CALLER's policy — the helper raises
    so the caller decides whether the bump is best-effort (the manager
    wiring arrives in a later slice).
    """
    key = project_graph_epoch_key(project)
    current = main_store.get_meta(key)
    value = int(current) + 1 if current else 1
    main_store.set_meta(key, str(value))
    return value


class CodeGraphStore:
    """Sidecar WAL-mode SQLite store for the project code graph.

    Patterned on ``VectorStore``/``SQLiteStore`` (thread-local
    connection, WAL, bootstrap lock). The store is derived state: it
    rebuilds from sources and is not tied to main-DB backup policy.
    """

    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = str(data_dir / "code_graph.db")
        self._local = threading.local()
        self._bootstrap_lock = threading.Lock()
        self.create_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            with self._bootstrap_lock:
                conn = getattr(self._local, "conn", None)
                if conn is None:
                    conn = sqlite3.connect(self._db_path, check_same_thread=False)
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA foreign_keys=ON")
                    conn.execute("PRAGMA busy_timeout=5000")
                    self._local.conn = conn
        return cast(sqlite3.Connection, conn)

    # ── schema / lifecycle ────────────────────────────────────────────────

    def create_schema(self) -> None:
        """Create tables + indexes if absent (idempotent, thread-safe)."""
        conn = self._conn()
        with self._bootstrap_lock:
            conn.executescript(_SCHEMA)
        conn.commit()

    def close(self) -> None:
        """Close the thread-local connection if one was opened.

        Mirrors ``VectorStore.close()`` — without this the connection
        cached in ``threading.local`` is only released on GC
        (``ResourceWarning: unclosed database`` in tests, fd leaks in a
        long-running server).
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __enter__(self) -> CodeGraphStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── node / edge writes (bulk, one transaction per call) ────────────────

    def upsert_nodes(self, nodes: list[CodeGraphNode]) -> None:
        """Insert or replace nodes in ONE transaction (executemany).

        A limit breach or bad kind fails the whole batch — no partial
        graph is published (ADR-0032 §3.2 atomicity).
        """
        if not nodes:
            return
        conn = self._conn()
        try:
            conn.executemany(
                """INSERT OR REPLACE INTO project_nodes
                   (id, project, kind, name, qname, path,
                    start_line, end_line, lang, signature, metadata)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        n.id,
                        n.project,
                        n.kind,
                        n.name,
                        n.qname,
                        n.path,
                        n.start_line,
                        n.end_line,
                        n.lang,
                        n.signature,
                        json.dumps(n.metadata or {}),
                    )
                    for n in nodes
                ],
            )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise

    def upsert_edges(self, edges: list[CodeGraphEdge]) -> None:
        """Insert or replace edges in ONE transaction (executemany).

        FK + the self-loop CHECK are enforced by SQLite; a missing
        endpoint or ``from_id == to_id`` fails the whole batch (no
        partial edge set — the reindex path must stay atomic).
        """
        if not edges:
            return
        conn = self._conn()
        try:
            conn.executemany(
                """INSERT OR REPLACE INTO project_edges
                   (from_id, to_id, kind, weight, provenance)
                   VALUES (?,?,?,?,?)""",
                [(e.from_id, e.to_id, e.kind, e.weight, e.provenance) for e in edges],
            )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise

    def delete_project(self, project: str) -> int:
        """Delete a project's ENTIRE subtree in ONE sidecar transaction.

        ADR-0032 §3.2: reindexation = DELETE by project + bulk insert
        in one transaction; ``delete_graph_project`` (tool 10) drops
        the sidecar subtree, never the project row in the main DB.
        Nodes go first so the edge FK cascade fires inside the same
        transaction; ``graph_files`` goes explicitly (no FK ties it).
        Returns the number of deleted NODE rows.
        """
        conn = self._conn()
        try:
            cur = conn.execute("DELETE FROM project_nodes WHERE project=?", (project,))
            deleted = cur.rowcount if cur.rowcount >= 0 else 0
            conn.execute("DELETE FROM graph_files WHERE project=?", (project,))
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
        return deleted

    # ── file records (incrementality) ─────────────────────────────────────

    def upsert_file_record(self, record: GraphFileRecord) -> None:
        """Insert or replace one ``graph_files`` row (stamps indexed_at)."""
        indexed_at = record.indexed_at or datetime.now(UTC).isoformat()
        conn = self._conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO graph_files
                   (project, path, mtime, size, hash, parse_ok, parse_error, indexed_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    record.project,
                    record.path,
                    record.mtime,
                    record.size,
                    record.hash,
                    record.parse_ok,
                    record.parse_error,
                    indexed_at,
                ),
            )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise

    def get_file_records(self, project: str) -> list[GraphFileRecord]:
        """All ``graph_files`` rows of one project (incremental index)."""
        rows = (
            self._conn()
            .execute(
                "SELECT project, path, mtime, size, hash, parse_ok, parse_error, indexed_at "
                "FROM graph_files WHERE project=? ORDER BY path",
                (project,),
            )
            .fetchall()
        )
        return [
            GraphFileRecord(
                project=str(r["project"]),
                path=str(r["path"]),
                mtime=r["mtime"],
                size=r["size"],
                hash=r["hash"],
                parse_ok=r["parse_ok"],
                parse_error=r["parse_error"],
                indexed_at=r["indexed_at"],
            )
            for r in rows
        ]

    # ── sidecar meta ───────────────────────────────────────────────────────

    def get_meta(self, key: str) -> str | None:
        """Read a sidecar ``graph_meta`` row (None if absent)."""
        row = self._conn().execute("SELECT value FROM graph_meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row is not None else None

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a sidecar ``graph_meta`` row (sidecar-local state —
        the freshness epoch lives in the MAIN DB, see the module-level
        ``bump_project_graph_epoch``)."""
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO graph_meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise

    # ── counts (status tool, wave PG-0/1) ──────────────────────────────────

    def count_nodes(self, project: str | None = None) -> int:
        """Node count, optionally scoped to one project."""
        row: sqlite3.Row | None
        if project:
            row = (
                self._conn()
                .execute("SELECT COUNT(*) FROM project_nodes WHERE project=?", (project,))
                .fetchone()
            )
        else:
            row = self._conn().execute("SELECT COUNT(*) FROM project_nodes").fetchone()
        return int(row[0]) if row is not None else 0

    def count_edges(self, project: str | None = None) -> int:
        """Edge count, optionally scoped to one project (via its nodes)."""
        row: sqlite3.Row | None
        if project:
            row = (
                self._conn()
                .execute(
                    "SELECT COUNT(*) FROM project_edges e "
                    "JOIN project_nodes n ON e.from_id = n.id "
                    "WHERE n.project = ?",
                    (project,),
                )
                .fetchone()
            )
        else:
            row = self._conn().execute("SELECT COUNT(*) FROM project_edges").fetchone()
        return int(row[0]) if row is not None else 0

    def count_files(self, project: str | None = None) -> int:
        """Indexed-file count, optionally scoped to one project."""
        row: sqlite3.Row | None
        if project:
            row = (
                self._conn()
                .execute("SELECT COUNT(*) FROM graph_files WHERE project=?", (project,))
                .fetchone()
            )
        else:
            row = self._conn().execute("SELECT COUNT(*) FROM graph_files").fetchone()
        return int(row[0]) if row is not None else 0
