"""SQLite + numpy vector store for semantic search.

All data stored in WAL-mode SQLite (vectors.db).
Similarity computed with numpy cosine — fast on CPU, no Rust deps.
Vectors are gated on status=published (MemoryManager ensures this).
The collection name is "mnemos_memories".
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, cast

import numpy as np

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS embeddings (
    id       TEXT PRIMARY KEY,
    vector   BLOB NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);
"""
_CREATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_embeddings_id ON embeddings(id);"


class VectorStore:
    """SQLite + numpy vector store — no external Rust-based vector DB required."""

    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = str(data_dir / "vectors.db")
        self._local = threading.local()
        conn = self._conn()
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_INDEX)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.commit()

    def _conn(self) -> sqlite3.Connection:
        # `getattr(..., default=None)` returns `Any`; the truthy check is
        # the runtime guard. After the guard the attribute is the cached
        # Connection. We cast back to sqlite3.Connection so the declared
        # return type holds — mypy --strict enforces this.
        if not getattr(self._local, "conn", None):
            self._local.conn = sqlite3.connect(self._db_path, check_same_thread=False)
        return cast(sqlite3.Connection, self._local.conn)

    # ── pack / unpack ─────────────────────────────────────────────────────

    @staticmethod
    def _pack(embedding: list[float]) -> bytes:
        # `np.asarray(...).tobytes()` return type varies across numpy
        # stub versions (Any on 3.11, bytes on 3.12+). Construct the
        # ndarray explicitly so the type is concrete on all versions.
        arr: np.ndarray = np.asarray(embedding, dtype=np.float32)
        return arr.tobytes()

    @staticmethod
    def _unpack(blob: bytes) -> np.ndarray:
        # Same rationale as _pack — explicit annotation avoids Any-return.
        arr: np.ndarray = np.frombuffer(blob, dtype=np.float32)
        return arr

    # ── write ─────────────────────────────────────────────────────────────

    def upsert(
        self,
        memory_id: str,
        embedding: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO embeddings(id, vector, metadata) VALUES (?,?,?)",
            (memory_id, self._pack(embedding), json.dumps(metadata or {})),
        )
        conn.commit()

    def batch_upsert(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]] | None = None,
        *,
        batch_size: int = 500,
    ) -> None:
        metas: list[dict[str, Any]] = metadatas or [{} for _ in ids]
        rows = [
            (i, self._pack(e), json.dumps(m))
            for i, e, m in zip(ids, embeddings, metas, strict=True)
        ]
        conn = self._conn()
        for start in range(0, len(rows), batch_size):
            conn.executemany(
                "INSERT OR REPLACE INTO embeddings(id, vector, metadata) VALUES (?,?,?)",
                rows[start : start + batch_size],
            )
        conn.commit()

    def delete(self, memory_id: str) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM embeddings WHERE id=?", (memory_id,))
        conn.commit()

    def wipe(self) -> int:
        """Delete every embedding row. Used by ``mnemos import --mode restore`` (CLI legacy name).

        Returns the number of deleted rows. The table schema is preserved.
        """
        conn = self._conn()
        cur = conn.execute("DELETE FROM embeddings")
        conn.commit()
        return cur.rowcount

    # ── search ────────────────────────────────────────────────────────────

    @staticmethod
    def _metadata_project(raw: Any) -> str | None:
        """Best-effort read of the row's ``metadata['project']``.

        Every write path stamps ``{"project": ..., "agent": ...}`` into the
        metadata column. Returns ``None`` for missing/corrupt metadata — a
        row whose project cannot be attested never matches a scoped search
        (fail-closed) and is unaffected in global mode.
        """
        try:
            meta: Any = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            return None
        if not isinstance(meta, dict):
            return None
        value = meta.get("project")
        return value if isinstance(value, str) else None

    def search(
        self,
        query_embedding: list[float],
        limit: int = 20,
        *,
        project: str | None = None,
    ) -> list[tuple[str, float]]:
        """Cosine similarity search. Returns (memory_id, score) sorted descending.

        A9 (ArchCom 2026-08-27) — project predicate, first-class: when
        ``project`` is a non-empty slug, only rows whose embedding metadata
        ``project`` equals it participate in the ranking (the scan filters
        BEFORE the top-k cut, so foreign rows never enter the candidate set
        and never consume depth). ``project=None`` (or empty) is the
        explicit global mode — every row participates, fast path unchanged.
        The authoritative project lives on the SQLite ``Memory`` and is
        re-checked by the caller at resolve time (defence in depth against
        vector-metadata drift).
        """
        conn = self._conn()
        rows: list[Any]
        if project:
            rows = [
                (r[0], r[1])
                for r in conn.execute("SELECT id, vector, metadata FROM embeddings")
                if self._metadata_project(r[2]) == project
            ]
        else:
            rows = conn.execute("SELECT id, vector FROM embeddings").fetchall()
        if not rows:
            return []

        q: np.ndarray = np.asarray(query_embedding, dtype=np.float32)
        q_norm = np.linalg.norm(q)
        if q_norm == 0:
            return []
        q = q / q_norm

        ids = [r[0] for r in rows]
        mat = np.stack([self._unpack(r[1]) for r in rows])  # (N, D)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        mat = mat / norms
        scores: np.ndarray = mat @ q  # (N,)

        top_k = min(limit, len(scores))
        idx = np.argpartition(scores, -top_k)[-top_k:]
        idx = idx[np.argsort(scores[idx])[::-1]]
        return [(ids[i], float(scores[i])) for i in idx]

    # ── read helpers ──────────────────────────────────────────────────────

    def has(self, memory_id: str) -> bool:
        conn = self._conn()
        return bool(
            conn.execute("SELECT 1 FROM embeddings WHERE id=? LIMIT 1", (memory_id,)).fetchone()
        )

    def all_ids(self) -> list[str]:
        """All embedding row ids (the memory ids that have live vectors).

        Search v2 (issue #313): backs ``MemoryManager.backfill_embedding_ids``
        — the id-keyed join that stamps ``memories.embedding_id`` for rows
        whose vector exists. Ids are written by the manager on every upsert
        (the memory id IS the vector key), so this projects no user data.
        """
        rows = self._conn().execute("SELECT id FROM embeddings").fetchall()
        return [str(r[0]) for r in rows]

    def get_metadata(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch the stored metadata blob per id (ADR-0019 B2a freshness).

        Returns a dict keyed by id for the ids that HAVE an embedding row;
        an absent id is simply missing from the result (the sweeper treats
        absence as stale). Corrupt metadata JSON degrades to ``{}``
        (best-effort read, mirroring ``_metadata_project``).
        """
        if not ids:
            return {}
        conn = self._conn()
        # placeholders is a static join of literal "?" characters; ids are bound
        # via parameter substitution, so no user input reaches the SQL string.
        placeholders = ",".join(["?"] * len(ids))
        sql = "SELECT id, metadata FROM embeddings WHERE id IN (" + placeholders + ")"  # nosec B608
        out: dict[str, dict[str, Any]] = {}
        for row in conn.execute(sql, ids).fetchall():
            try:
                meta: Any = json.loads(row[1]) if row[1] else {}
            except (ValueError, TypeError):
                meta = {}
            out[str(row[0])] = meta if isinstance(meta, dict) else {}
        return out

    def count(self) -> int:
        # `fetchone()[0]` is `Any` (sqlite3.Row); COUNT(*) is always int.
        row = self._conn().execute("SELECT COUNT(*) FROM embeddings").fetchone()
        return int(row[0]) if row else 0

    def get_embeddings(self, ids: list[str]) -> dict[str, list[float]]:
        if not ids:
            return {}
        conn = self._conn()
        # placeholders is a static join of literal "?" characters; ids are bound
        # via parameter substitution, so no user input reaches the SQL string.
        placeholders = ",".join(["?"] * len(ids))
        sql = (
            "SELECT id, vector FROM embeddings WHERE id IN (" + placeholders + ")"  # nosec B608
        )
        rows = conn.execute(sql, ids).fetchall()
        return {r[0]: self._unpack(r[1]).tolist() for r in rows}

    # ── lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the thread-local SQLite connection if one was opened.

        Mirrors SQLiteStore.close(). Without this the connection cached in
        ``threading.local`` is only released on GC, which surfaces as
        ``ResourceWarning: unclosed database`` in tests and leaks file
        descriptors in long-running processes.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
