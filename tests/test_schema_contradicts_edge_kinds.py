"""ADR 0002 duty #6 (wave B6-conflict) — the ``contradicts`` edge kind.

Covered:

* fresh installs admit ``contradicts`` at the DB CHECK level and
  reject unknown kinds (whitelist sync at the DB level, not just the
  wrapper);
* pre-B6 databases (2-kind CHECK) are rebuilt by
  ``_migrate_edges_contradicts_kind`` — rows survive with all eight
  columns, ``contradicts`` becomes insertable, the rebuild table
  converges away, and the migration is idempotent on reopen;
* the manager mint surface: minted/duplicate outcomes with the
  contract defaults (rank-only weight 1.0, provenance
  ``conflict-detector``), NC-3 suppression in both directions, and one
  CONFLICT-MINT telemetry line per attempt.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import Memory, MemorySource, MemoryStatus, MemoryType
from vesma.storage.sqlite_store import SQLiteStore

# The pre-B6 memory_edges DDL (post-A0 shape), verbatim — the schema
# this migration window must consume.
_PRE_CONTRADICTS_EDGES_DDL = """
CREATE TABLE memory_edges (
    from_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    to_memory_id   TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('supersedes', 'relates_to')),
    created_at     TEXT NOT NULL,
    weight         REAL NOT NULL DEFAULT 1.0,
    provenance     TEXT NOT NULL DEFAULT 'declared',
    scope_project  TEXT,
    scope_agent    TEXT,
    PRIMARY KEY (from_memory_id, to_memory_id, kind),
    CHECK (from_memory_id <> to_memory_id)
)
"""


def _make_memory(mid: str, content: str = "hello world") -> Memory:
    now = datetime.now(UTC)
    return Memory(
        id=mid,
        content=content,
        title="test",
        tags=["project:b6", "agent:b6-agent", "mnemos:learning"],
        source=MemorySource.MANUAL,
        source_url=None,
        memory_type=MemoryType.NOTE,
        created_at=now,
        updated_at=now,
        metadata={},
        file_path=None,
        category=None,
        project="b6",
        agent="b6-agent",
        status=MemoryStatus.RAW,
        quality_score=None,
        confidence=None,
        source_coverage=None,
        cluster_id=None,
        derived_from=[],
        embedding_id=None,
        raw_content=None,
        clean_content=None,
        filter_profile=None,
        filter_stats=None,
        filter_version=None,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    s = SQLiteStore(tmp_path / "edges.db")
    yield s
    s.close()


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp_path / "vault"),
            "data_dir": str(tmp_path / "data"),
            "db_name": "test.db",
        },
        scanner={"enabled": False},  # type: ignore[arg-type]
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def manager(tmp_path: Path) -> Iterator[MemoryManager]:
    from vesma.models import MemoryCreate

    mgr = MemoryManager(_settings(tmp_path))
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    a = mgr.add(
        MemoryCreate(content="first memory", tags=["mnemos:learning"]),
        project="b6",
        agent="b6-agent",
    )
    b = mgr.add(
        MemoryCreate(content="second memory", tags=["mnemos:learning"]),
        project="b6",
        agent="b6-agent",
    )
    mgr._fixture_ids = (a.id, b.id)  # type: ignore[attr-defined]
    yield mgr
    mgr.close()


def _downgrade_edges_to_pre_contradicts(path: Path) -> None:
    """Rebuild memory_edges into the pre-B6 (2-kind) shape.

    Rows survive the downgrade (all eight columns are copied), so a
    seeded edge exercises the migration's copy path. FK enforcement is
    off on this raw connection — DROP TABLE on the child side is safe.
    """
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT from_memory_id, to_memory_id, kind, created_at, weight, "
            "provenance, scope_project, scope_agent FROM memory_edges"
        ).fetchall()
        conn.execute("DROP TABLE memory_edges")
        conn.execute(_PRE_CONTRADICTS_EDGES_DDL)
        conn.executemany(
            "INSERT INTO memory_edges (from_memory_id, to_memory_id, kind, created_at,"
            " weight, provenance, scope_project, scope_agent) VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _seeded_db(path: Path) -> None:
    """A store-written DB with one supersedes and one relates_to edge."""
    s = SQLiteStore(path)
    s.save(_make_memory("m-new", "replacement memory"))
    s.save(_make_memory("m-old", "superseded memory"))
    s.save(_make_memory("m-rel", "related memory"))
    assert s.add_memory_edge("m-new", "m-old") is True
    assert s.add_memory_edge("m-rel", "m-old", kind="relates_to") is True
    s.close()


# ── Fresh install: contradicts at the DB level ────────────────────────────────


class TestFreshContradictsKind:
    def test_db_check_accepts_contradicts(self, store: SQLiteStore) -> None:
        store.save(_make_memory("m-a"))
        store.save(_make_memory("m-b"))
        assert store.add_memory_edge("m-a", "m-b", kind="contradicts") is True
        row = (
            store._get_conn()
            .execute("SELECT kind, weight, provenance FROM memory_edges WHERE kind = 'contradicts'")
            .fetchone()
        )
        assert row is not None

    def test_db_check_still_rejects_unknown_kind(self, store: SQLiteStore) -> None:
        store.save(_make_memory("m-a"))
        store.save(_make_memory("m-b"))
        conn = store._get_conn()
        now = datetime.now(UTC).isoformat()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            conn.execute(
                "INSERT INTO memory_edges (from_memory_id, to_memory_id, kind, created_at)"
                " VALUES ('m-a','m-b','bogus',?)",
                (now,),
            )


# ── Migration: pre-B6 databases are rebuilt ───────────────────────────────────


class TestMigrationFromPreB6:
    def test_rebuild_admits_contradicts_and_keeps_rows(self, tmp_path: Path) -> None:
        _seeded_db(tmp_path / "edges.db")
        _downgrade_edges_to_pre_contradicts(tmp_path / "edges.db")
        s = SQLiteStore(tmp_path / "edges.db")
        try:
            conn = s._get_conn()
            # rows survived the rebuild with their kinds
            kinds = dict(conn.execute("SELECT kind, COUNT(*) FROM memory_edges GROUP BY kind"))
            assert kinds == {"supersedes": 1, "relates_to": 1}
            # contradicts is now insertable
            assert s.add_memory_edge("m-rel", "m-new", kind="contradicts") is True
            assert (
                conn.execute("SELECT 1 FROM memory_edges WHERE kind = 'contradicts'").fetchone()
                is not None
            )
            # the rebuild table converged away
            orphan = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'memory_edges_kind_rebuild'"
            ).fetchone()
            assert orphan is None
        finally:
            s.close()

    def test_migration_is_idempotent_on_reopen(self, tmp_path: Path) -> None:
        _seeded_db(tmp_path / "edges.db")
        _downgrade_edges_to_pre_contradicts(tmp_path / "edges.db")
        s1 = SQLiteStore(tmp_path / "edges.db")
        s1.close()
        s2 = SQLiteStore(tmp_path / "edges.db")
        try:
            kinds = dict(
                s2._get_conn().execute("SELECT kind, COUNT(*) FROM memory_edges GROUP BY kind")
            )
            assert kinds == {"supersedes": 1, "relates_to": 1}
        finally:
            s2.close()


# ── Manager mint surface ──────────────────────────────────────────────────────


class TestManagerMintSurface:
    def test_minted_outcome_carries_contract_defaults(
        self, manager: MemoryManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        a, b = manager._fixture_ids  # type: ignore[attr-defined]
        with caplog.at_level(logging.INFO, logger="vesma.manager"):
            assert manager.mint_contradicts_edge(a, b) == "minted"
        row = (
            manager.sqlite._get_conn()
            .execute("SELECT weight, provenance FROM memory_edges WHERE kind = 'contradicts'")
            .fetchone()
        )
        assert tuple(row) == (1.0, "conflict-detector")
        assert any("CONFLICT-MINT attempt" in r.message for r in caplog.records)

    def test_duplicate_outcome_is_idempotent(self, manager: MemoryManager) -> None:
        a, b = manager._fixture_ids  # type: ignore[attr-defined]
        assert manager.mint_contradicts_edge(a, b) == "minted"
        assert manager.mint_contradicts_edge(a, b) == "duplicate"

    def test_nc3_suppression_both_directions(self, manager: MemoryManager) -> None:
        a, b = manager._fixture_ids  # type: ignore[attr-defined]
        assert manager.add_memory_edge(a, b) is True  # supersedes
        assert manager.mint_contradicts_edge(a, b) == "suppressed-supersedes"
        assert manager.mint_contradicts_edge(b, a) == "suppressed-supersedes"
        kinds = [
            r[0]
            for r in manager.sqlite._get_conn().execute("SELECT kind FROM memory_edges").fetchall()
        ]
        assert "contradicts" not in kinds
