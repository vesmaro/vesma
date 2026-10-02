"""ADR-0030 A0 (issue #321) — memory_edges edge-kinds migration tests.

The one-shot window: production carried 0 edges on 1662 memories (live
probe 2026-09-15), so the table is rebuilt into its final shape while it
is empty — after minting starts a rebuild becomes a real migration.

Covered:

* fresh installs get the final schema from day one (two-kind CHECK,
  ``weight`` REAL NOT NULL DEFAULT 1.0, ``provenance`` TEXT NOT NULL
  DEFAULT 'declared', nullable ``scope_project``/``scope_agent``);
* legacy supersedes-only databases (ADR-0018 Phase 1 shape) are rebuilt
  in one transaction — a SEEDED edge survives with the column defaults,
  and the production case (0 rows) upgrades just as cleanly;
* the SQL CHECK accepts exactly the ``_EDGE_KINDS`` whitelist (sync
  guaranteed at the DB level, not just in the wrapper) and rejects
  anything else;
* the manager wrapper accepts ``relates_to`` with the contract defaults
  and persists explicit weight/provenance/scope;
* crash safety: an orphaned rebuild table converges on reopen, and a
  failure inside the rebuild script rolls back to the legacy table.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import Memory, MemorySource, MemoryStatus, MemoryType
from vesma.storage.sqlite_store import _EDGE_KINDS, SQLiteStore

# The pre-A0 memory_edges DDL (ADR-0018 Phase 1), verbatim — the schema
# this migration window must consume.
_LEGACY_EDGES_DDL = """
CREATE TABLE memory_edges (
    from_memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    to_memory_id   TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('supersedes')),
    created_at     TEXT NOT NULL,
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
        tags=["project:a0", "agent:a0-agent", "mnemos:learning"],
        source=MemorySource.MANUAL,
        source_url=None,
        memory_type=MemoryType.NOTE,
        created_at=now,
        updated_at=now,
        metadata={},
        file_path=None,
        category=None,
        project="a0",
        agent="a0-agent",
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


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, dict[str, object]]:
    return {
        str(r[1]): {"type": str(r[2]), "notnull": int(r[3]), "dflt": r[4], "pk": int(r[5])}
        for r in conn.execute(f"PRAGMA table_info({table})")  # nosec B608 - test-local name
    }


def _object_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    return row is not None


def _downgrade_edges_to_legacy(path: Path) -> None:
    """Rebuild memory_edges into the pre-A0 (supersedes-only) shape.

    Rows survive the downgrade (the four legacy columns are copied), so a
    seeded edge can exercise the migration's copy path. FK enforcement is
    off on this raw connection — DROP TABLE on the child side is safe.
    """
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT from_memory_id, to_memory_id, kind, created_at FROM memory_edges"
        ).fetchall()
        conn.execute("DROP TABLE memory_edges")
        conn.execute(_LEGACY_EDGES_DDL)
        conn.executemany(
            "INSERT INTO memory_edges (from_memory_id, to_memory_id, kind, created_at)"
            " VALUES (?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _seeded_db(path: Path) -> None:
    """A store-written DB with one supersedes edge hanging on two memories."""
    s = SQLiteStore(path)
    s.save(_make_memory("m-new", "replacement memory"))
    s.save(_make_memory("m-old", "superseded memory"))
    assert s.add_memory_edge("m-new", "m-old") is True
    s.close()


# ── Fresh install: the final schema from day one ──────────────────────────────


class TestFreshInstallSchema:
    def test_final_columns_and_defaults(self, store: SQLiteStore) -> None:
        cols = _columns(store._get_conn(), "memory_edges")
        assert set(cols) == {
            "from_memory_id",
            "to_memory_id",
            "kind",
            "created_at",
            "weight",
            "provenance",
            "scope_project",
            "scope_agent",
        }
        weight = cols["weight"]
        assert weight["type"] == "REAL"
        assert weight["notnull"] == 1
        # PRAGMA renders the default exactly as written in the DDL.
        assert weight["dflt"] == "1.0"
        provenance = cols["provenance"]
        assert provenance["type"] == "TEXT"
        assert provenance["notnull"] == 1
        assert str(provenance["dflt"]).strip("'") == "declared"
        for scope_col in ("scope_project", "scope_agent"):
            assert cols[scope_col]["type"] == "TEXT"
            assert cols[scope_col]["notnull"] == 0
            assert cols[scope_col]["dflt"] is None

    def test_edge_indexes_present(self, store: SQLiteStore) -> None:
        conn = store._get_conn()
        assert _object_exists(conn, "idx_memory_edges_from")
        assert _object_exists(conn, "idx_memory_edges_to")

    def test_both_kinds_accepted_with_column_defaults(self, store: SQLiteStore) -> None:
        store.save(_make_memory("m-a"))
        store.save(_make_memory("m-b"))

        assert store.add_memory_edge("m-a", "m-b", kind="supersedes") is True
        assert store.add_memory_edge("m-b", "m-a", kind="relates_to") is True

        rows = {
            r["kind"]: r
            for r in store._get_conn().execute(
                "SELECT kind, weight, provenance, scope_project, scope_agent FROM memory_edges"
            )
        }
        assert set(rows) == {"supersedes", "relates_to"}
        for row in rows.values():
            # Column defaults applied by the store INSERT (contract §3).
            assert row["weight"] == 1.0
            assert row["provenance"] == "declared"
            assert row["scope_project"] is None
            assert row["scope_agent"] is None

    def test_explicit_weight_provenance_scope_persisted(self, store: SQLiteStore) -> None:
        store.save(_make_memory("m-a"))
        store.save(_make_memory("m-b"))

        assert (
            store.add_memory_edge(
                "m-a",
                "m-b",
                kind="relates_to",
                weight=1.5,
                provenance="auto-dedupe:v1",
                scope_project="a0",
                scope_agent="a0-agent",
            )
            is True
        )
        row = (
            store._get_conn()
            .execute("SELECT weight, provenance, scope_project, scope_agent FROM memory_edges")
            .fetchone()
        )
        assert tuple(row) == (1.5, "auto-dedupe:v1", "a0", "a0-agent")

    def test_empty_provenance_rejected(self, store: SQLiteStore) -> None:
        store.save(_make_memory("m-a"))
        store.save(_make_memory("m-b"))
        with pytest.raises(ValueError, match="provenance"):
            store.add_memory_edge("m-a", "m-b", provenance="")

    # ── weight validation (#324 scope-addition from the #336 review) ──────

    def test_weight_validation_rejects_nonfinite_and_nonpositive(self, store: SQLiteStore) -> None:
        """Negative / 0 / +inf / NaN weights are rejected at the write
        boundary with a caller-actionable ValueError. The NaN case is the
        motivating one: sqlite3 binds float('nan') to SQL NULL, so without
        this check the row would fail the column's NOT NULL constraint
        with a confusing IntegrityError instead of naming the caller's
        argument. -inf rides the same isfinite arm as NaN/+inf.
        """
        store.save(_make_memory("m-a"))
        store.save(_make_memory("m-b"))
        for bad in (-1.0, 0.0, float("inf"), float("-inf"), float("nan")):
            with pytest.raises(ValueError, match="weight"):
                store.add_memory_edge("m-a", "m-b", weight=bad)
        assert store._get_conn().execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0] == 0

    def test_weight_validation_subthreshold_positive_accepted(self, store: SQLiteStore) -> None:
        """I3 companion: a positive weight below 1.0 is a VALID edge — the
        validation must not become an eligibility pre-filter (weights never
        remove eligibility, only scale ranking post-gate in A1)."""
        store.save(_make_memory("m-a"))
        store.save(_make_memory("m-b"))
        assert store.add_memory_edge("m-a", "m-b", weight=0.25) is True
        row = store._get_conn().execute("SELECT weight FROM memory_edges").fetchone()
        assert row["weight"] == 0.25

    def test_weight_validation_reaches_the_manager_wrapper(self, tmp_path: Path) -> None:
        """The manager wrapper (the path explicit callers and the auto-mint
        rule write through) inherits the store's rejection unchanged."""
        settings = Settings(
            vesma={
                "vault_path": str(tmp_path / "vault"),
                "data_dir": str(tmp_path / "data"),
                "db_name": "test.db",
            },
            scanner={"enabled": False},  # type: ignore[arg-type]
        )
        settings.resolve_paths()
        mgr = MemoryManager(settings)
        try:
            with pytest.raises(ValueError, match="weight"):
                mgr.add_memory_edge("x-a", "x-b", weight=float("nan"))
        finally:
            mgr.close()


# ── CHECK ↔ _EDGE_KINDS whitelist sync (DB level, not just the wrapper) ───────


class TestCheckWhitelistSync:
    def test_check_accepts_exactly_the_whitelist(self, store: SQLiteStore) -> None:
        assert set(_EDGE_KINDS) == {"supersedes", "relates_to"}
        store.save(_make_memory("m-a"))
        store.save(_make_memory("m-b"))
        conn = store._get_conn()
        now = datetime.now(UTC).isoformat()
        for kind in _EDGE_KINDS:
            conn.execute(
                "INSERT INTO memory_edges (from_memory_id, to_memory_id, kind, created_at)"
                " VALUES ('m-a','m-b',?,?)",
                (kind, now),
            )
        assert conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0] == 2

    def test_check_rejects_unknown_kind_at_db_level(self, store: SQLiteStore) -> None:
        store.save(_make_memory("m-a"))
        store.save(_make_memory("m-b"))
        conn = store._get_conn()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            conn.execute(
                "INSERT INTO memory_edges (from_memory_id, to_memory_id, kind, created_at)"
                " VALUES ('m-a','m-b','derives-from',?)",
                (datetime.now(UTC).isoformat(),),
            )

    def test_self_edge_rejected_at_db_level(self, store: SQLiteStore) -> None:
        store.save(_make_memory("m-solo"))
        conn = store._get_conn()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            conn.execute(
                "INSERT INTO memory_edges (from_memory_id, to_memory_id, kind, created_at)"
                " VALUES ('m-solo','m-solo','supersedes',?)",
                (datetime.now(UTC).isoformat(),),
            )


# ── Upgrade: legacy supersedes-only databases ─────────────────────────────────


class TestLegacyUpgrade:
    def test_seeded_edge_survives_with_defaults(self, tmp_path: Path) -> None:
        db = tmp_path / "legacy_seeded.db"
        _seeded_db(db)
        _downgrade_edges_to_legacy(db)

        store = SQLiteStore(db)  # first connect runs the rebuild
        conn = store._get_conn()

        cols = _columns(conn, "memory_edges")
        assert "weight" in cols and "provenance" in cols

        rows = conn.execute(
            "SELECT from_memory_id, to_memory_id, kind, created_at, weight, provenance,"
            " scope_project, scope_agent FROM memory_edges"
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        # The legacy edge survived the copy with the column defaults.
        assert (row["from_memory_id"], row["to_memory_id"], row["kind"]) == (
            "m-new",
            "m-old",
            "supersedes",
        )
        assert row["created_at"]
        assert row["weight"] == 1.0
        assert row["provenance"] == "declared"
        assert row["scope_project"] is None
        assert row["scope_agent"] is None

        # The old CHECK is gone: relates_to writes now succeed, and the
        # rebuilt table keeps its indexes + cascade delete.
        assert store.add_memory_edge("m-new", "m-old", kind="relates_to", weight=1.25) is True
        assert _object_exists(conn, "idx_memory_edges_from")
        assert _object_exists(conn, "idx_memory_edges_to")
        store.delete("m-old")
        assert conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0] == 0
        store.close()

    def test_empty_legacy_table_upgrades(self, tmp_path: Path) -> None:
        """The production case: 0 edges on 1662 memories."""
        db = tmp_path / "legacy_empty.db"
        _seeded_db(db)
        conn = sqlite3.connect(str(db))
        conn.execute("DELETE FROM memory_edges")
        conn.commit()
        conn.close()
        _downgrade_edges_to_legacy(db)

        store = SQLiteStore(db)
        conn = store._get_conn()
        assert "weight" in _columns(conn, "memory_edges")
        assert conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0] == 0
        assert store.add_memory_edge("m-new", "m-old", kind="relates_to") is True
        store.close()

    def test_migration_idempotent_on_reopen(self, tmp_path: Path) -> None:
        db = tmp_path / "legacy.db"
        _seeded_db(db)
        _downgrade_edges_to_legacy(db)

        store = SQLiteStore(db)
        store.close()
        store = SQLiteStore(db)  # reopen: shape detection skips the rebuild
        conn = store._get_conn()
        assert not _object_exists(conn, "memory_edges_a0_rebuild")
        assert conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0] == 1
        store.close()

    def test_legacy_unknown_kind_rejected_before_migration(self, tmp_path: Path) -> None:
        """Sanity: the legacy CHECK really was supersedes-only — the
        downgrade helper produces the schema this migration targets."""
        db = tmp_path / "legacy.db"
        _seeded_db(db)
        _downgrade_edges_to_legacy(db)
        conn = sqlite3.connect(str(db))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            conn.execute(
                "INSERT INTO memory_edges (from_memory_id, to_memory_id, kind, created_at)"
                " VALUES ('m-new','m-old','relates_to','2026-09-15T00:00:00+00:00')"
            )
        conn.close()


# ── Crash safety (the A1 pattern) ─────────────────────────────────────────────


class TestMigrationCrashSafety:
    def test_orphan_converges_on_final_shape_early_return(self, tmp_path: Path) -> None:
        """#324 scope-addition from the #336 review: the early-return
        branch (final-shape table — the ``weight`` column is present, so
        the rebuild script never runs) must still converge a seeded
        orphan rebuild table. ``test_orphan_rebuild_table_converges``
        below covers the rebuild-script branch's leading DROP; this pins
        the EARLY-RETURN branch's own DROP (a crash orphan on an
        already-migrated DB would otherwise sit in the schema forever,
        and the next legacy→final migration window would be shadowed).
        """
        db = tmp_path / "final_shape_orphan.db"
        _seeded_db(db)  # store-written: final shape, one edge row
        conn = sqlite3.connect(str(db))
        assert "weight" in {str(r[1]) for r in conn.execute("PRAGMA table_info(memory_edges)")}
        conn.execute("CREATE TABLE memory_edges_a0_rebuild (from_memory_id TEXT, junk TEXT)")
        conn.execute("INSERT INTO memory_edges_a0_rebuild VALUES ('x', 'stale partial copy')")
        conn.commit()
        conn.close()

        store = SQLiteStore(db)  # early-return path: no rebuild script runs
        conn = store._get_conn()
        assert not _object_exists(conn, "memory_edges_a0_rebuild"), (
            "the early-return branch must DROP the orphan, not skip past it"
        )
        # The final-shape table was untouched: shape and rows intact.
        assert "weight" in _columns(conn, "memory_edges")
        rows = conn.execute(
            "SELECT from_memory_id, to_memory_id, kind FROM memory_edges"
        ).fetchall()
        assert [(r["from_memory_id"], r["to_memory_id"], r["kind"]) for r in rows] == [
            ("m-new", "m-old", "supersedes")
        ]
        store.close()

    def test_orphan_rebuild_table_converges(self, tmp_path: Path) -> None:
        db = tmp_path / "crash.db"
        _seeded_db(db)
        _downgrade_edges_to_legacy(db)
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE memory_edges_a0_rebuild (from_memory_id TEXT, junk TEXT)")
        conn.execute("INSERT INTO memory_edges_a0_rebuild VALUES ('x', 'partial copy')")
        conn.commit()
        conn.close()

        store = SQLiteStore(db)  # must not raise
        conn = store._get_conn()
        assert not _object_exists(conn, "memory_edges_a0_rebuild")
        assert "weight" in _columns(conn, "memory_edges")
        rows = conn.execute(
            "SELECT from_memory_id, to_memory_id, kind FROM memory_edges"
        ).fetchall()
        assert [(r["from_memory_id"], r["to_memory_id"], r["kind"]) for r in rows] == [
            ("m-new", "m-old", "supersedes")
        ]
        store.close()

    def test_transaction_rolls_back_on_script_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "txfail.db"
        _seeded_db(db)
        _downgrade_edges_to_legacy(db)
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        # Force a mid-script failure: sabotage the CREATE with invalid
        # SQL (unbalanced paren) — the script dies INSIDE the transaction.
        monkeypatch.setattr(
            "vesma.storage.sqlite_store._EDGES_REBUILD_DDL",
            "CREATE TABLE memory_edges_a0_rebuild (bad_col TEXT",
        )
        with pytest.raises(sqlite3.OperationalError):
            SQLiteStore._migrate_a0_edges_kinds(conn)
        monkeypatch.undo()
        # The except-branch rolled the script back: the legacy table is
        # intact (no weight column, row survived), no orphan, no
        # dangling transaction.
        assert not conn.in_transaction
        assert not _object_exists(conn, "memory_edges_a0_rebuild")
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(memory_edges)")}
        assert "weight" not in cols
        survived = conn.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0]
        assert survived == 1
        conn.close()
        # And the store re-opens cleanly, completing the migration.
        store = SQLiteStore(db)
        migrated = store._get_conn()
        assert "weight" in _columns(migrated, "memory_edges")
        assert migrated.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0] == 1
        store.close()


# ── Manager wrapper: relates_to + provenance/scope contract ───────────────────


def _settings(tmp: Path) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        scanner={"enabled": False},  # type: ignore[arg-type]
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def manager(tmp_path: Path) -> Iterator[MemoryManager]:
    mgr = MemoryManager(_settings(tmp_path))
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


class TestManagerWrapper:
    def test_relates_to_with_provenance_and_scope(self, manager: MemoryManager) -> None:
        from vesma.models import MemoryCreate

        a = manager.add(
            MemoryCreate(content="near duplicate one", tags=["mnemos:learning"]),
            project="a0",
            agent="a0-agent",
        )
        b = manager.add(
            MemoryCreate(content="near duplicate two", tags=["mnemos:learning"]),
            project="a0",
            agent="a0-agent",
        )

        assert (
            manager.add_memory_edge(
                a.id,
                b.id,
                kind="relates_to",
                weight=1.5,
                provenance="auto-dedupe:v1",
                scope_project="a0",
                scope_agent="a0-agent",
            )
            is True
        )
        row = (
            manager.sqlite._get_conn()
            .execute(
                "SELECT kind, weight, provenance, scope_project, scope_agent"
                " FROM memory_edges WHERE from_memory_id = ?",
                (a.id,),
            )
            .fetchone()
        )
        assert tuple(row) == ("relates_to", 1.5, "auto-dedupe:v1", "a0", "a0-agent")

        # Reads still filter by kind — the relates_to edge is visible.
        edges = manager.get_memory_edges(a.id, kind="relates_to")
        assert len(edges) == 1 and edges[0]["to_memory_id"] == b.id

    def test_contract_defaults_applied(self, manager: MemoryManager) -> None:
        from vesma.models import MemoryCreate

        a = manager.add(
            MemoryCreate(content="declared edge source", tags=["mnemos:learning"]),
            project="a0",
            agent="a0-agent",
        )
        b = manager.add(
            MemoryCreate(content="declared edge target", tags=["mnemos:learning"]),
            project="a0",
            agent="a0-agent",
        )

        assert manager.add_memory_edge(a.id, b.id) is True
        row = (
            manager.sqlite._get_conn()
            .execute(
                "SELECT kind, weight, provenance, scope_project, scope_agent"
                " FROM memory_edges WHERE from_memory_id = ?",
                (a.id,),
            )
            .fetchone()
        )
        assert tuple(row) == ("supersedes", 1.0, "declared", None, None)

    def test_unknown_kind_still_rejected(self, manager: MemoryManager) -> None:
        from vesma.models import MemoryCreate

        a = manager.add(
            MemoryCreate(content="solo memory content", tags=["mnemos:learning"]),
            project="a0",
            agent="a0-agent",
        )
        with pytest.raises(ValueError, match="unknown edge kind"):
            manager.add_memory_edge(a.id, a.id, kind="contradicts")
