"""W2a wave 0 (ADR-0035) — native awareness heartbeat, shadow contour.

Covers the C12 delta probe + composite index, the ``awrh:`` cursor, the
``compose_heartbeat`` contour (envelope / calm-line / ceiling / ORDER-only
relevance), the ``call_tool`` wrapper gating (off / shadow / canary / on),
the deny-list, and the ADR-0026-family events in ``metrics.sqlite``.

The six CI pins from the wave brief:

1. ``TestOffByteIdentity`` — mode=off: the response is byte-identical to
   the pre-feature shape (the dispatch-level result).
2. ``TestShadowByteIdentity`` — shadow: byte-identical to off, events
   written.
3. ``TestCalmLinePin`` — empty delta at canary/on: exactly one line, the
   pinned calm-line LITERAL.
4. ``TestDenyList`` — assemble/export/import never carry the tail.
5. ``TestCursorAdvanceOnce`` — advance exactly once per delivery; a retry
   sees "no delta".
6. ``TestNoScoresInTail`` — order/counters/ids only, no numeric scores.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from vesmaro.storage.sqlite_store import SQLiteStore

PROJECT = "awrh-proj"
AGENT = "awrh-agent"
NEIGHBOR_A = "awrh-neighbor-a"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    s = SQLiteStore(tmp_path / "probe.db")
    yield s
    s.close()


def _probe_row(
    store: SQLiteStore,
    *,
    project: str = PROJECT,
    agent: str = NEIGHBOR_A,
    created_at: str,
    status: str = "published",
) -> None:
    """Raw row insert — the probe must not depend on the manager boundary."""
    conn = store._get_conn()
    conn.execute(
        "INSERT INTO memories (id, title, content, tags, memory_type, source, status,"
        " project, agent, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"row-{created_at}-{agent}",
            "t",
            "c",
            '["mnemos:learning"]',
            "note",
            "mcp",
            status,
            project,
            agent,
            created_at,
            created_at,
        ),
    )
    conn.commit()


# ── C12: the composite index ──────────────────────────────────────────────────


class TestProbeIndex:
    def test_composite_index_created_on_fresh_db(self, store: SQLiteStore) -> None:
        names = {r[1] for r in store._get_conn().execute("PRAGMA index_list(memories)")}
        assert "idx_memories_project_created" in names

    def test_composite_index_covers_probe_columns(self, store: SQLiteStore) -> None:
        cols = [
            r[2]
            for r in store._get_conn().execute("PRAGMA index_xinfo(idx_memories_project_created)")
        ]
        assert set(cols) >= {"project", "created_at"}

    def test_index_recreated_after_drop_on_reconnect(self, tmp_path: Path) -> None:
        """The index is CREATE IF NOT EXISTS on EVERY connect: a DB whose
        index is missing (the pre-W2a legacy state) gains it on the next
        store open — zero migration machinery (the C12 delivery form)."""
        db = tmp_path / "legacy.db"
        first = SQLiteStore(db)
        conn = first._get_conn()
        conn.execute("DROP INDEX idx_memories_project_created")
        conn.commit()
        names = {r[1] for r in conn.execute("PRAGMA index_list(memories)")}
        assert "idx_memories_project_created" not in names
        first.close()
        # A fresh store instance over the same file re-creates the index
        # while running the ordinary connect path (schema script).
        second = SQLiteStore(db)
        names = {r[1] for r in second._get_conn().execute("PRAGMA index_list(memories)")}
        assert "idx_memories_project_created" in names
        second.close()


# ── C12: the delta probe ─────────────────────────────────────────────────────


class TestDeltaProbe:
    def test_empty_store_no_delta(self, store: SQLiteStore) -> None:
        assert store.exists_since(PROJECT, "2026-10-01T00:00:00+00:00") is False

    def test_row_after_cursor_is_delta(self, store: SQLiteStore) -> None:
        _probe_row(store, created_at="2026-10-01T11:00:00+00:00")
        assert store.exists_since(PROJECT, "2026-10-01T10:00:00+00:00") is True

    def test_row_at_or_before_cursor_is_not_delta(self, store: SQLiteStore) -> None:
        """The probe bound is STRICT (created_at > since) — inclusive semantics
        belong to the compose feed, the cursor stores +1us for exactly this
        reason (see awareness._window_rows)."""
        _probe_row(store, created_at="2026-10-01T11:00:00+00:00")
        assert store.exists_since(PROJECT, "2026-10-01T11:00:00+00:00") is False
        assert store.exists_since(PROJECT, "2026-10-01T11:00:00.000001+00:00") is False

    def test_archived_rows_are_not_delta(self, store: SQLiteStore) -> None:
        _probe_row(store, created_at="2026-10-01T11:00:00+00:00", status="archived")
        assert store.exists_since(PROJECT, "2026-10-01T10:00:00+00:00") is False

    def test_other_project_is_not_delta(self, store: SQLiteStore) -> None:
        _probe_row(store, project="other-proj", created_at="2026-10-01T11:00:00+00:00")
        assert store.exists_since(PROJECT, "2026-10-01T10:00:00+00:00") is False

    def test_caller_own_rows_count_for_probe(self, store: SQLiteStore) -> None:
        """The probe is deliberately agent-agnostic (the ADR's literal SQL):
        the caller's own writes fire the compose, the agent post-filter
        happens inside the delta (empty-after-filter → calm-line)."""
        _probe_row(store, agent=AGENT, created_at="2026-10-01T11:00:00+00:00")
        assert store.exists_since(PROJECT, "2026-10-01T10:00:00+00:00") is True
