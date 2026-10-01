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

import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from vesmaro.config import AwarenessConfig, Settings
from vesmaro.lanes import (
    AWARENESS_CURSOR_PREFIX,
    AWARENESS_HEARTBEAT_CURSOR_PREFIX,
    awareness_cursor_key,
    awareness_heartbeat_cursor_key,
    read_awareness_heartbeat_cursor,
    write_awareness_heartbeat_cursor,
)
from vesmaro.manager import MemoryManager
from vesmaro.storage.sqlite_store import SQLiteStore

PROJECT = "awrh-proj"
AGENT = "awrh-agent"
SESSION = "awrh-session"
NEIGHBOR_A = "awrh-neighbor-a"
NEIGHBOR_B = "awrh-neighbor-b"
NEIGHBOR_SESSION = "sess-neighbor"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteStore]:
    s = SQLiteStore(tmp_path / "probe.db")
    yield s
    s.close()


def _settings(
    tmp: Path,
    *,
    mode: str = "off",
    heartbeat_rate_limit: int | None = None,
    **awareness_extra: Any,
) -> Settings:
    awareness: dict[str, Any] = {"native_heartbeat_mode": mode, **awareness_extra}
    if heartbeat_rate_limit is not None:
        awareness["heartbeat_rate_limit_per_minute"] = heartbeat_rate_limit
    settings = Settings(
        mnemos={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        scanner={"enabled": False},
        awareness=awareness,  # type: ignore[arg-type]
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def manager(tmp_path: Path) -> Iterator[MemoryManager]:
    """Real manager, mode=off (the inert default)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = _manager(_settings(Path(tmpdir)))
        yield mgr
        mgr.close()


def _manager(settings: Settings) -> MemoryManager:
    mgr = MemoryManager(settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    return mgr


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


# ── C14: the awrh: cursor namespace ───────────────────────────────────────────


class TestHeartbeatCursorKey:
    def test_key_is_length_prefixed_two_tuple(self) -> None:
        assert (
            awareness_heartbeat_cursor_key(project="awrh-proj", agent="awrh-agent")
            == "awrh:9:awrh-proj:10:awrh-agent"
        )

    def test_colon_in_components_does_not_alias(self) -> None:
        """The anti-aliasing property of the length prefix (C14): a plain
        ``:``-join would render both pairs identically."""
        a = awareness_heartbeat_cursor_key(project="p:1", agent="x")
        b = awareness_heartbeat_cursor_key(project="p", agent="1:x")
        assert a != b
        assert a == "awrh:3:p:1:1:x"
        assert b == "awrh:1:p:3:1:x"

    def test_heartbeat_never_collides_with_legacy_awr_namespace(self) -> None:
        """A 2-tuple heartbeat key must never parse as / overwrite a
        session-scoped 3-tuple cursor (the C14 anti-aliasing ruling)."""
        hb = awareness_heartbeat_cursor_key(project=PROJECT, agent=AGENT)
        legacy = awareness_cursor_key(project=PROJECT, agent=AGENT, session=SESSION)
        assert hb != legacy
        assert hb.startswith(AWARENESS_HEARTBEAT_CURSOR_PREFIX)
        assert legacy.startswith(AWARENESS_CURSOR_PREFIX)
        assert not legacy.startswith(AWARENESS_HEARTBEAT_CURSOR_PREFIX)

    def test_blank_components_rejected(self) -> None:
        with pytest.raises(ValueError, match="project"):
            awareness_heartbeat_cursor_key(project="  ", agent=AGENT)
        with pytest.raises(ValueError, match="agent"):
            awareness_heartbeat_cursor_key(project=PROJECT, agent="")


class TestHeartbeatCursorRoundtrip:
    def test_write_then_read_roundtrip_session_independent(self, manager: MemoryManager) -> None:
        write_awareness_heartbeat_cursor(
            manager, project=PROJECT, agent=AGENT, cursor="2026-10-01T12:00:00+00:00"
        )
        # No session leg: any session of the same agent sees one cursor.
        assert (
            read_awareness_heartbeat_cursor(manager, project=PROJECT, agent=AGENT)
            == "2026-10-01T12:00:00+00:00"
        )

    def test_agents_have_independent_cursors(self, manager: MemoryManager) -> None:
        write_awareness_heartbeat_cursor(
            manager, project=PROJECT, agent=AGENT, cursor="2026-10-01T12:00:00+00:00"
        )
        assert read_awareness_heartbeat_cursor(manager, project=PROJECT, agent=NEIGHBOR_A) is None

    def test_empty_cursor_write_rejected(self, manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            write_awareness_heartbeat_cursor(manager, project=PROJECT, agent=AGENT, cursor=" ")

    def test_upsert_overwrites_in_place(self, manager: MemoryManager) -> None:
        write_awareness_heartbeat_cursor(
            manager, project=PROJECT, agent=AGENT, cursor="2026-10-01T12:00:00+00:00"
        )
        write_awareness_heartbeat_cursor(
            manager, project=PROJECT, agent=AGENT, cursor="2026-10-01T13:00:00+00:00"
        )
        assert (
            read_awareness_heartbeat_cursor(manager, project=PROJECT, agent=AGENT)
            == "2026-10-01T13:00:00+00:00"
        )


# ── Config: the mode ladder and its env override ─────────────────────────────


class TestHeartbeatConfig:
    def test_default_mode_is_off(self) -> None:
        assert AwarenessConfig().native_heartbeat_mode == "off"

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AwarenessConfig(native_heartbeat_mode="loud")

    def test_settings_section_wired(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, mode="shadow")
        assert settings.awareness.native_heartbeat_mode == "shadow"

    def test_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VESMARO_AWARENESS__NATIVE_HEARTBEAT_MODE", "canary")
        settings = Settings(
            mnemos={
                "vault_path": str(tmp_path / "vault"),
                "data_dir": str(tmp_path / "data"),
                "db_name": "test.db",
            }
        )
        assert settings.awareness.native_heartbeat_mode == "canary"

    def test_rate_limit_default_and_bounds(self) -> None:
        assert AwarenessConfig().heartbeat_rate_limit_per_minute == 30
        with pytest.raises(ValidationError):
            AwarenessConfig(heartbeat_rate_limit_per_minute=-1)
