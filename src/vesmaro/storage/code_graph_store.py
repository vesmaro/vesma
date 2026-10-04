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

import contextlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

#: Allowed node kinds (ADR-0032 §3.1; CHECK-enforced). ``Command`` /
#: ``Route`` (card vesma-graph-command-route-nodes, schema v2) are
#: contributed ONLY by the optional surface node-source extension —
#: a repo that does not define the CLI/API surface never grows them.
NODE_KINDS = (
    "Project",
    "File",
    "Module",
    "Class",
    "Function",
    "Method",
    "Type",
    "Command",
    "Route",
)

#: Allowed edge kinds (ADR-0032 §3.1; CHECK-enforced). ``USES`` is the
#: provenance-honest fallback — an unproven call is NEVER ``CALLS``.
#: ``INVOKES`` (Command→handler) and ``HANDLES`` (Route→endpoint) are
#: written only by the surface node-source extension (schema v2).
EDGE_KINDS = (
    "CONTAINS_FILE",
    "DEFINES",
    "IMPORTS",
    "CALLS",
    "INHERITS",
    "TESTS",
    "USES",
    "INVOKES",
    "HANDLES",
)

#: ``meta`` key prefix for the per-project epoch (main DB). The
#: length-prefix segment makes the key colon-safe — the same discipline
#: as ``graph_epoch`` (manager.py, issue #415 review finding).
_PROJECT_GRAPH_EPOCH_PREFIX = "project_graph_epoch:"

#: Sidecar ``graph_meta`` key prefix for the per-project POISONED set
#: (PG3/PGT-3): paths whose indexing ever hit the secrets detector.
#: The set LIVES THROUGH REINDEXATION — a poisoned file stays poisoned
#: even when a later scan misses (pattern drift). It is cleared ONLY by
#: the operator's explicit fresh start: :meth:`CodeGraphStore.purge_project`
#: (tool 10 ``delete_graph_project``). The atomic publish path
#: (:meth:`CodeGraphStore.publish_project_graph`) deliberately does NOT
#: touch it — a reindex must never launder a poisoned file.
_POISONED_PREFIX = "poisoned:"

#: Sidecar ``graph_meta`` key prefix: last successful (re)index stamp.
#: Key layout owned HERE (with the poisoned key) so
#: :meth:`CodeGraphStore.purge_project` can clear exactly the project's
#: keys without the storage layer importing the codegraph package.
_LAST_INDEXED_PREFIX = "last_indexed:"

#: Current sidecar schema version (``graph_meta`` key
#: ``schema_version``). v1 = the ADR-0032 PG-0 kinds; v2 (card
#: vesma-graph-command-route-nodes) adds the ``Command``/``Route`` node
#: kinds and the ``INVOKES``/``HANDLES`` edge kinds. A store opened on
#: an older file migrates IN PLACE (the standard SQLite 12-step
#: table-rebuild) — the graph is derived state, but a migration must
#: not silently empty it: rows are copied losslessly (every v1 kind is
#: a v2 kind), so no reindex is forced.
SCHEMA_VERSION = 2

_SCHEMA_VERSION_KEY = "schema_version"


def poisoned_key(project: str) -> str:
    """Sidecar ``graph_meta`` key for the project's poisoned-path set
    (length-prefixed, the same colon-safety as the epoch key)."""
    return f"{_POISONED_PREFIX}{len(project)}:{project}"


def last_indexed_key(project: str) -> str:
    """Sidecar ``graph_meta`` key for the last (re)index ISO stamp
    (length-prefixed, the same colon-safety as the epoch key)."""
    return f"{_LAST_INDEXED_PREFIX}{len(project)}:{project}"


#: Single source of truth for the two CHECK-bearing table bodies —
#: reused by the schema-version migration (which must create the NEW
#: tables with byte-identical constraint shapes; a drifted copy would
#: reintroduce the old CHECK through the back door).
_PROJECT_NODES_DDL = """
    id          TEXT PRIMARY KEY,   -- <project>#<rel_path>#<symbol>#<line>
    project     TEXT NOT NULL,      -- logical FK to projects.id (main DB)
    kind        TEXT NOT NULL CHECK (kind IN
                  ('Project','File','Module','Class','Function','Method','Type',
                   'Command','Route')),
    name        TEXT NOT NULL,
    qname       TEXT NOT NULL,
    path        TEXT,               -- ALWAYS repo-relative (PG1)
    start_line  INTEGER,
    end_line    INTEGER,
    lang        TEXT,
    signature   TEXT,               -- NO defaults (PG1); NO docstrings/literals
    metadata    TEXT NOT NULL DEFAULT '{}'
"""

_PROJECT_EDGES_DDL = """
    from_id     TEXT NOT NULL REFERENCES project_nodes(id) ON DELETE CASCADE,
    to_id       TEXT NOT NULL REFERENCES project_nodes(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN
                  ('CONTAINS_FILE','DEFINES','IMPORTS','CALLS','INHERITS','TESTS',
                   'USES','INVOKES','HANDLES')),
    weight      REAL NOT NULL DEFAULT 1.0,
    provenance  TEXT NOT NULL DEFAULT 'tree-sitter',
    PRIMARY KEY (from_id, to_id, kind),
    CHECK (from_id <> to_id)
"""

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS project_nodes (\n"
    + _PROJECT_NODES_DDL
    + "\n);\n"
    "CREATE TABLE IF NOT EXISTS project_edges (\n"
    + _PROJECT_EDGES_DDL
    + "\n);\n"
    """
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
CREATE TABLE IF NOT EXISTS graph_audit (   -- PG7 append-only audit trail
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    project   TEXT NOT NULL,
    action    TEXT NOT NULL,   -- index/reindex/delete/snippet-read/graph-read
    actor     TEXT NOT NULL,   -- agent_id (per-agent attribution, PG7)
    session   TEXT,
    reason    TEXT,
    details   TEXT NOT NULL DEFAULT '{}',  -- JSON, never source bytes
    ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_project ON project_nodes(project, kind);
CREATE INDEX IF NOT EXISTS idx_nodes_qname ON project_nodes(qname);
CREATE INDEX IF NOT EXISTS idx_nodes_path ON project_nodes(project, path);
CREATE INDEX IF NOT EXISTS idx_edges_from ON project_edges(from_id, kind);
CREATE INDEX IF NOT EXISTS idx_edges_to ON project_edges(to_id, kind);
"""
)


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

    @property
    def db_path(self) -> str:
        """Absolute path of the sidecar file (the audit trail opens its
        own connection to the SAME database — one sidecar, two writers,
        WAL-mediated)."""
        return self._db_path

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
        """Create tables + indexes if absent (idempotent, thread-safe).

        A sidecar stamped with an OLDER schema version migrates in
        place (:meth:`_migrate_schema`) — one table-rebuild per version
        bump, never per open."""
        conn = self._conn()
        with self._bootstrap_lock:
            conn.executescript(_SCHEMA)
            self._migrate_schema(conn)
        conn.commit()

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """Bring an older sidecar up to :data:`SCHEMA_VERSION`.

        ``CREATE TABLE IF NOT EXISTS`` never upgrades an EXISTING
        table's CHECK constraints — a v1 file would reject every
        ``Command``/``Route`` insert with a bare ``IntegrityError``.
        The standard SQLite table-rebuild (foreign_keys OFF, shadow
        copies, drop, rename, indexes) copies the rows losslessly —
        every v1 kind is a v2 kind — and stamps ``graph_meta`` in the
        SAME transaction. A brand-new file (tables just created with
        the current CHECKs, zero rows) rides the same copy for free.
        Any failure rolls the whole migration back: the previous
        schema and data survive untouched.
        """
        row = conn.execute(
            "SELECT value FROM graph_meta WHERE key=?", (_SCHEMA_VERSION_KEY,)
        ).fetchone()
        if row is not None and str(row[0]) == str(SCHEMA_VERSION):
            return
        prev_isolation = conn.isolation_level
        conn.isolation_level = None  # manual txn control (PRAGMA must sit outside a txn)
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DROP TABLE IF EXISTS project_nodes_v2tmp")
            conn.execute("DROP TABLE IF EXISTS project_edges_v2tmp")
            conn.execute(f"CREATE TABLE project_nodes_v2tmp ({_PROJECT_NODES_DDL})")
            conn.execute(f"CREATE TABLE project_edges_v2tmp ({_PROJECT_EDGES_DDL})")
            conn.execute(
                "INSERT INTO project_nodes_v2tmp SELECT "
                "id, project, kind, name, qname, path, start_line, end_line, "
                "lang, signature, metadata FROM project_nodes"
            )
            conn.execute(
                "INSERT INTO project_edges_v2tmp SELECT "
                "from_id, to_id, kind, weight, provenance FROM project_edges"
            )
            conn.execute("DROP TABLE project_edges")
            conn.execute("DROP TABLE project_nodes")
            conn.execute("ALTER TABLE project_nodes_v2tmp RENAME TO project_nodes")
            conn.execute("ALTER TABLE project_edges_v2tmp RENAME TO project_edges")
            for index_sql in (
                "CREATE INDEX IF NOT EXISTS idx_nodes_project "
                "ON project_nodes(project, kind)",
                "CREATE INDEX IF NOT EXISTS idx_nodes_qname ON project_nodes(qname)",
                "CREATE INDEX IF NOT EXISTS idx_nodes_path ON project_nodes(project, path)",
                "CREATE INDEX IF NOT EXISTS idx_edges_from ON project_edges(from_id, kind)",
                "CREATE INDEX IF NOT EXISTS idx_edges_to ON project_edges(to_id, kind)",
            ):
                conn.execute(index_sql)
            conn.execute(
                "INSERT OR REPLACE INTO graph_meta (key, value) VALUES (?, ?)",
                (_SCHEMA_VERSION_KEY, str(SCHEMA_VERSION)),
            )
            conn.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                # no active txn (BEGIN itself failed) — surface the ORIGINAL error
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.isolation_level = prev_isolation

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

    # ── node / edge reads (tools, wave PG-0 slice 4) ────────────────────────

    @staticmethod
    def _node_from_row(r: sqlite3.Row) -> CodeGraphNode:
        return CodeGraphNode(
            id=str(r["id"]),
            project=str(r["project"]),
            kind=str(r["kind"]),
            name=str(r["name"]),
            qname=str(r["qname"]),
            path=r["path"],
            start_line=r["start_line"],
            end_line=r["end_line"],
            lang=r["lang"],
            signature=r["signature"],
            metadata=json.loads(r["metadata"]) if r["metadata"] else {},
        )

    def get_node(self, node_id: str) -> CodeGraphNode | None:
        """Fetch ONE node by canonical id (None if absent)."""
        row = (
            self._conn()
            .execute(
                "SELECT id, project, kind, name, qname, path, start_line, end_line, "
                "lang, signature, metadata FROM project_nodes WHERE id=?",
                (node_id,),
            )
            .fetchone()
        )
        return self._node_from_row(row) if row is not None else None

    def get_nodes(
        self,
        project: str,
        *,
        kind: str | None = None,
        path: str | None = None,
        qname: str | None = None,
        limit: int = 200,
    ) -> list[CodeGraphNode]:
        """Nodes of one project filtered by exact kind/path/qname.

        All filters hit the slice-1 indexes (``idx_nodes_project``,
        ``idx_nodes_qname``, ``idx_nodes_path``) — no table scan, no
        FTS5 (the contract keeps the sidecar free of a fourth table).
        """
        if kind is not None and kind not in NODE_KINDS:
            raise ValueError(f"unknown node kind: {kind!r}")
        sql = (
            "SELECT id, project, kind, name, qname, path, start_line, end_line, "
            "lang, signature, metadata FROM project_nodes WHERE project=?"
        )
        params: list[Any] = [project]
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        if path is not None:
            sql += " AND path=?"
            params.append(path)
        if qname is not None:
            sql += " AND qname=?"
            params.append(qname)
        sql += " ORDER BY path, start_line, qname LIMIT ?"
        params.append(max(1, int(limit)))
        rows = self._conn().execute(sql, params).fetchall()
        return [self._node_from_row(r) for r in rows]

    @staticmethod
    def _like_pattern(query: str) -> str:
        """Escape the caller query into a LIKE pattern (``%``/``_``
        are literals in a search box, never wildcards)."""
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    def search_nodes(
        self,
        project: str,
        query: str,
        *,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[CodeGraphNode]:
        """Substring search over name/qname/path (LIKE, escaped).

        The query matches ANY of the three columns — name hits, dotted
        qname hits and repo-relative path hits all surface. Ranked
        ordering (exact > prefix > substring) is the SERVICE layer's
        job; the store returns the raw matches deterministically
        (kind, then name) so ranking never depends on row order.
        """
        if not query.strip():
            return []
        if kind is not None and kind not in NODE_KINDS:
            raise ValueError(f"unknown node kind: {kind!r}")
        pattern = self._like_pattern(query.strip())
        sql = (
            "SELECT id, project, kind, name, qname, path, start_line, end_line, "
            "lang, signature, metadata FROM project_nodes "
            "WHERE project=? AND (name LIKE ? ESCAPE '\\' "
            "OR qname LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\')"
        )
        params: list[Any] = [project, pattern, pattern, pattern]
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY kind, name, qname LIMIT ?"
        params.append(max(1, int(limit)))
        rows = self._conn().execute(sql, params).fetchall()
        return [self._node_from_row(r) for r in rows]

    def count_search_nodes(
        self,
        project: str,
        query: str,
        *,
        kind: str | None = None,
    ) -> int:
        """Honest COUNT over the :meth:`search_nodes` predicate — the
        SAME WHERE, no LIMIT. ``search_graph`` reports it as
        ``total_matches``: the cursor pages the top-``limit`` ranked
        slice, so deriving the total from that slice undercounts
        whenever the graph holds more than 2x ``limit`` matches
        (review 10173a2a-5). One cheap indexed count per search."""
        if not query.strip():
            return 0
        if kind is not None and kind not in NODE_KINDS:
            raise ValueError(f"unknown node kind: {kind!r}")
        pattern = self._like_pattern(query.strip())
        sql = (
            "SELECT COUNT(*) FROM project_nodes "
            "WHERE project=? AND (name LIKE ? ESCAPE '\\' "
            "OR qname LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\')"
        )
        params: list[Any] = [project, pattern, pattern, pattern]
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        return int(self._conn().execute(sql, params).fetchone()[0])

    def get_edges(
        self,
        project: str,
        *,
        from_id: str | None = None,
        to_id: str | None = None,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[CodeGraphEdge]:
        """Edges of one project filtered by endpoint / kind.

        Endpoint filters hit ``idx_edges_from``/``idx_edges_to``; the
        project scope comes via the node join (edges carry no project
        column — the endpoints' project IS the edge's project).
        """
        if kind is not None and kind not in EDGE_KINDS:
            raise ValueError(f"unknown edge kind: {kind!r}")
        sql = (
            "SELECT e.from_id, e.to_id, e.kind, e.weight, e.provenance "
            "FROM project_edges e JOIN project_nodes n ON e.from_id = n.id "
            "WHERE n.project=?"
        )
        params: list[Any] = [project]
        if from_id is not None:
            sql += " AND e.from_id=?"
            params.append(from_id)
        if to_id is not None:
            sql += " AND e.to_id=?"
            params.append(to_id)
        if kind is not None:
            sql += " AND e.kind=?"
            params.append(kind)
        sql += " ORDER BY e.from_id, e.kind, e.to_id LIMIT ?"
        params.append(max(1, int(limit)))
        return [
            CodeGraphEdge(
                from_id=str(r["from_id"]),
                to_id=str(r["to_id"]),
                kind=str(r["kind"]),
                weight=float(r["weight"]),
                provenance=str(r["provenance"]),
            )
            for r in self._conn().execute(sql, params).fetchall()
        ]

    # ── atomic publish (reindexation, ADR-0032 §3.2) ───────────────────────

    def publish_project_graph(
        self,
        project: str,
        nodes: list[CodeGraphNode],
        edges: list[CodeGraphEdge],
        file_records: list[GraphFileRecord],
    ) -> None:
        """Atomic (re)publish: DELETE the project subtree + insert the
        new nodes/edges/file records in ONE transaction.

        The single entry point the indexer uses — the previous
        three-step (delete + upserts) path let a crash between steps
        strand a half-updated project. A failure anywhere (bad kind,
        FK-less edge, limit breach upstream) rolls back to the
        PREVIOUS graph untouched. The poisoned set is deliberately
        NOT touched here — a reindex must never launder it (see
        :meth:`add_poisoned_paths`).
        """
        if not nodes:
            raise ValueError("publish_project_graph: refusing to publish an empty node set")
        conn = self._conn()
        try:
            conn.execute("DELETE FROM project_nodes WHERE project=?", (project,))
            conn.execute("DELETE FROM graph_files WHERE project=?", (project,))
            conn.executemany(
                """INSERT INTO project_nodes
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
            conn.executemany(
                """INSERT OR REPLACE INTO project_edges
                   (from_id, to_id, kind, weight, provenance)
                   VALUES (?,?,?,?,?)""",
                [(e.from_id, e.to_id, e.kind, e.weight, e.provenance) for e in edges],
            )
            stamp = datetime.now(UTC).isoformat()
            conn.executemany(
                """INSERT OR REPLACE INTO graph_files
                   (project, path, mtime, size, hash, parse_ok, parse_error, indexed_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [
                    (
                        project,
                        rec.path,
                        rec.mtime,
                        rec.size,
                        rec.hash,
                        rec.parse_ok,
                        rec.parse_error,
                        rec.indexed_at or stamp,
                    )
                    for rec in file_records
                ],
            )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise

    # ── poisoned set (PG3, lives through reindexation) ──────────────────────

    def get_poisoned_paths(self, project: str) -> set[str]:
        """The project's poisoned-path set (JSON list under the
        ``poisoned:`` meta key; empty set when absent)."""
        raw = self.get_meta(poisoned_key(project))
        if not raw:
            return set()
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return set()
        return {str(p) for p in loaded} if isinstance(loaded, list) else set()

    def add_poisoned_paths(self, project: str, paths: list[str] | set[str]) -> None:
        """Union ``paths`` INTO the poisoned set (read-modify-write).

        Union semantics IS the «навсегда» contract: once a path hit the
        secrets detector it stays poisoned across reindexations, even
        when a later scan misses (pattern drift) or the file is gone.
        Cleared only by :meth:`purge_project` (tool 10) and the #449
        allowlist un-poison (:meth:`remove_poisoned_paths`).
        """
        if not paths:
            return
        merged = self.get_poisoned_paths(project) | {str(p) for p in paths}
        self.set_meta(poisoned_key(project), json.dumps(sorted(merged)))

    def remove_poisoned_paths(self, project: str, paths: list[str] | set[str]) -> list[str]:
        """Remove ``paths`` from the poisoned set, returning what was
        actually removed (sorted; empty list = nothing matched).

        Issue #449: the operator's ``secret_allowlist`` escape hatch —
        a previously-poisoned path whose repo-relative path now matches
        an allowlist glob is UN-poisoned on the next index call. The
        ONLY removal besides :meth:`purge_project`, and it never runs
        implicitly: the caller (the indexer's allowlist pass) audits
        every removal with reason ``allowlist-unpoison`` so the
        sidecar trail shows exactly what left the set and why.
        """
        if not paths:
            return []
        current = self.get_poisoned_paths(project)
        removed = current & {str(p) for p in paths}
        if not removed:
            return []
        self.set_meta(poisoned_key(project), json.dumps(sorted(current - removed)))
        return sorted(removed)

    # ── operator fresh start (tool 10) ──────────────────────────────────────

    def purge_project(self, project: str) -> int:
        """The operator's FRESH START (tool 10 ``delete_graph_project``).

        Deletes the subtree AND clears exactly this project's sidecar
        meta keys — the poisoned set (besides the audited #449
        allowlist removal, the ONLY operation that may ever clear it)
        and the ``last_indexed`` stamp — in ONE transaction.
        Returns the number of deleted NODE rows.
        """
        conn = self._conn()
        try:
            cur = conn.execute("DELETE FROM project_nodes WHERE project=?", (project,))
            deleted = cur.rowcount if cur.rowcount >= 0 else 0
            conn.execute("DELETE FROM graph_files WHERE project=?", (project,))
            conn.execute(
                "DELETE FROM graph_meta WHERE key IN (?,?)",
                (poisoned_key(project), last_indexed_key(project)),
            )
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

    def get_parse_failures(self, project: str) -> dict[str, str]:
        """``path -> parse_error`` for files whose record says parse_ok=0
        («clean ≠ proof»: status surfaces them, secrets included as
        ``secret-detected`` without any content)."""
        rows = (
            self._conn()
            .execute(
                "SELECT path, parse_error FROM graph_files "
                "WHERE project=? AND parse_ok=0 ORDER BY path",
                (project,),
            )
            .fetchall()
        )
        return {str(r["path"]): str(r["parse_error"] or "parse-error") for r in rows}

    def list_graph_projects(self) -> list[dict[str, Any]]:
        """Distinct indexed projects with volume counts (tool 9 base)."""
        rows = (
            self._conn()
            .execute(
                "SELECT project, COUNT(*) AS nodes FROM project_nodes "
                "GROUP BY project ORDER BY project"
            )
            .fetchall()
        )
        return [
            {
                "project": str(r["project"]),
                "nodes": int(r["nodes"]),
                "edges": self.count_edges(str(r["project"])),
                "files": self.count_files(str(r["project"])),
                "poisoned": len(self.get_poisoned_paths(str(r["project"]))),
                "last_indexed_at": self.get_meta(last_indexed_key(str(r["project"]))),
            }
            for r in rows
        ]

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

    def count_nodes_by_kind(self, project: str) -> dict[str, int]:
        """Per-kind node breakdown for ONE project (status tool): the
        total (:meth:`count_nodes`) stays the honest headline; the
        breakdown makes extension-contributed kinds (``Command`` /
        ``Route``) visible without a raw SQL probe."""
        rows = self._conn().execute(
            "SELECT kind, COUNT(*) FROM project_nodes WHERE project=? GROUP BY kind",
            (project,),
        ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}

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
