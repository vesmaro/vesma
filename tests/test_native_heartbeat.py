"""W2a wave 0 (ADR-0035) — native awareness heartbeat, shadow contour.

Covers the C12 delta probe + composite index, the ``awrh:`` cursor, the
``compose_heartbeat`` contour (envelope / calm-line / ceiling / ORDER-only
relevance), the ``call_tool`` wrapper gating (off / shadow / canary / on),
the deny-list, and the ADR-0026-family events in the metrics sidecar.

The six CI pins from the wave brief, by class:

1. ``TestOffByteIdentity`` — mode=off: the response is byte-identical to
   the pre-feature shape (the dispatch-level result), any tool, any
   condition.
2. ``TestShadowByteIdentity`` (+ ``TestHeartbeatEvents``) — shadow:
   byte-identical to off, events written.
3. ``TestCanaryOnRendering::test_canary_calm_appends_exactly_one_line``
   (+ ``TestComposeHeartbeat::test_calm_line_is_timestamp_free_deterministic_constant``)
   — empty delta at canary/on: exactly one line, the pinned calm-line
   LITERAL.
4. ``TestDenyList`` — assemble/export/import never carry the tail.
5. ``TestComposeHeartbeat::test_second_compose_after_advance_is_calm`` —
   advance exactly once per delivery; a retry sees "no delta".
6. ``TestEnvelope::test_agent_lines_observed_only_no_scores`` —
   order/counters/ids only, no numeric scores.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from vesmaro import mcp_server as mcp_server_module
from vesmaro.awareness import (
    AWARENESS_DISCLAIMER,
    HEARTBEAT_CALM_LINE,
    HEARTBEAT_ENVELOPE_TOKEN_CEILING,
    HEARTBEAT_FLAG_LINE,
    HEARTBEAT_PROJECT_ID_MAX_CHARS,
    compose_heartbeat,
    sanitize_project_id,
)
from vesmaro.config import AwarenessConfig, Settings
from vesmaro.heartbeat import HEARTBEAT_DENY_TOOLS
from vesmaro.lanes import (
    AWARENESS_CURSOR_PREFIX,
    AWARENESS_HEARTBEAT_CURSOR_PREFIX,
    awareness_cursor_key,
    awareness_heartbeat_cursor_key,
    read_awareness_heartbeat_cursor,
    write_awareness_heartbeat_cursor,
)
from vesmaro.manager import MemoryManager
from vesmaro.mcp_server import _call_tool_dispatch, _canonical_tools, call_tool
from vesmaro.metrics.schema import validate_awareness_meta
from vesmaro.metrics.sink import MetricsStore
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus
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


# ── compose_heartbeat: the contour ────────────────────────────────────────────


class TestComposeHeartbeat:
    def test_empty_store_is_calm_literal(self, manager: MemoryManager) -> None:
        result = compose_heartbeat(manager, project=PROJECT, agent=AGENT, tool="mnemos_list_recent")
        assert result["state"] == "calm"
        assert result["text"] == HEARTBEAT_CALM_LINE
        assert result["lines"] == 1
        # Probe-negative calm: NO cursor write happened at all.
        assert read_awareness_heartbeat_cursor(manager, project=PROJECT, agent=AGENT) is None
        assert result["cursor_before"] is None and result["cursor_after"] is None
        kinds = [kind for kind, _meta in result["events"]]
        assert kinds == ["heartbeat_delivery"]
        assert result["events"][0][1]["state"] == "calm"

    def test_peer_delta_renders_envelope_and_advances_cursor(self, manager: MemoryManager) -> None:
        _knowledge(manager, "neighbor writes about the deploy pipeline")
        result = compose_heartbeat(manager, project=PROJECT, agent=AGENT, tool="mnemos_search")
        assert result["state"] == "delta"
        text = result["text"]
        assert text.startswith(f"## Peer awareness — heartbeat (project {PROJECT})")
        assert AWARENESS_DISCLAIMER in text
        assert HEARTBEAT_FLAG_LINE in text
        assert f"- {NEIGHBOR_A}: 1 entries, last " in text
        # Cursor advanced BEFORE the return: readable back, identity in log.
        stored = read_awareness_heartbeat_cursor(manager, project=PROJECT, agent=AGENT)
        assert stored == result["cursor_after"]
        assert result["cursor_after"] > (result["cursor_before"] or "")
        kinds = [kind for kind, _meta in result["events"]]
        assert kinds == ["delta_available", "heartbeat_delivery"]
        meta = result["events"][1][1]
        assert meta["state"] == "delta"
        assert meta["tool"] == "mnemos_search"
        assert meta["cursor_before"] is None and meta["cursor_after"] == stored

    def test_second_compose_after_advance_is_calm(self, manager: MemoryManager) -> None:
        """At-most-once: after a delivery consumed the delta, a retry (the
        same call again) sees no delta — the calm-line, not a re-delivery."""
        _knowledge(manager, "one delta row is enough")
        first = compose_heartbeat(manager, project=PROJECT, agent=AGENT)
        assert first["state"] == "delta"
        second = compose_heartbeat(manager, project=PROJECT, agent=AGENT)
        assert second["state"] == "calm"
        assert second["text"] == HEARTBEAT_CALM_LINE
        # No further advancement on the calm leg.
        assert second["cursor_after"] == first["cursor_after"]

    def test_caller_own_rows_only_is_calm_with_cursor_past_self_writes(
        self, manager: MemoryManager
    ) -> None:
        """The probe is agent-agnostic (fires on own rows), the delta is not
        (excludes the caller): consumed-and-quiet — calm-line, cursor moved
        past the self-writes so the probe stops re-firing on them."""
        _knowledge(manager, "my own write", agent=AGENT)
        first = compose_heartbeat(manager, project=PROJECT, agent=AGENT)
        assert first["state"] == "calm"
        assert first["cursor_after"] is not None
        second = compose_heartbeat(manager, project=PROJECT, agent=AGENT)
        assert second["state"] == "calm"

    def test_calm_line_is_timestamp_free_deterministic_constant(self) -> None:
        assert HEARTBEAT_CALM_LINE == "## Peer awareness — no new peer activity"
        assert "20" not in HEARTBEAT_CALM_LINE  # no year, no clock, no nonce


class TestEnvelope:
    def test_agent_lines_observed_only_no_scores(self, manager: MemoryManager) -> None:
        _checkpoint(
            manager,
            goals="quokka importer release window",
            agent=NEIGHBOR_A,
        )
        _knowledge(manager, "neighbor wrote about deploy", agent=NEIGHBOR_A)
        result = compose_heartbeat(manager, project=PROJECT, agent=AGENT)
        body_lines = [
            ln
            for ln in result["text"].splitlines()
            if ln.startswith("- ") and "entries, last" in ln
        ]
        assert body_lines, "envelope must carry per-agent lines"
        for ln in body_lines:
            # Form pin: id, integer counter, time — and NOTHING numeric
            # between them (no relevance scores ride the tail).
            assert re.fullmatch(r"- [A-Za-z0-9_.\-]+: \d+ entries, last \S+", ln), ln

    def test_relevance_order_is_goal_overlap_first(self, manager: MemoryManager) -> None:
        """ORDER-only relevance (addendum §2): the neighbor whose goal
        overlaps mine leads; ties stay deterministic regardless of write
        order."""
        _checkpoint(manager, goals="ship the quokka importer release", agent=AGENT, session=SESSION)
        # Written SECOND and alphabetically FIRST — must still render SECOND
        # (zero overlap).
        _knowledge(manager, "aardvark unrelated note", agent="aaa-neighbor")
        _checkpoint(manager, goals="quokka importer fix", agent="zzz-neighbor")
        result = compose_heartbeat(manager, project=PROJECT, agent=AGENT)
        lines = [ln for ln in result["text"].splitlines() if "entries, last" in ln]
        agents_in_order = [ln.split(":")[0][2:] for ln in lines]
        assert agents_in_order == ["zzz-neighbor", "aaa-neighbor"]

    def test_envelope_ceiling_holds_under_eight_hostile_ids(self, manager: MemoryManager) -> None:
        for i in range(8):
            _knowledge(
                manager,
                f"hostile neighbor {i} body",
                agent=f"peer-with-a-very-long-hostile-identifier-{i:04d}",
            )
        result = compose_heartbeat(manager, project=PROJECT, agent=AGENT)
        assert result["state"] == "delta"
        assert result["tokens_est"] <= HEARTBEAT_ENVELOPE_TOKEN_CEILING
        # Truncation is observable, never silent.
        assert "more peers not shown" in result["text"]
        shown = [ln for ln in result["text"].splitlines() if "entries, last" in ln]
        assert 1 <= len(shown) < 8

    def test_sanitized_agent_ids_never_carry_markdown_or_control(
        self, manager: MemoryManager
    ) -> None:
        _knowledge(manager, "hostile id body", agent="**evil**`x`\nagent")
        result = compose_heartbeat(manager, project=PROJECT, agent=AGENT)
        text = result["text"]
        for forbidden in ("*", "`", "\nagent"):
            assert forbidden not in text
        line = next(ln for ln in text.splitlines() if "entries, last" in ln)
        assert re.fullmatch(r"- [A-Za-z0-9_.\-]+: \d+ entries, last \S+", line), line

    def test_compact_seen_is_minute_utc(self, manager: MemoryManager) -> None:
        _knowledge(manager, "compact time body")
        result = compose_heartbeat(manager, project=PROJECT, agent=AGENT)
        line = next(ln for ln in result["text"].splitlines() if "entries, last" in ln)
        stamp = line.rsplit("last ", 1)[1]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z", stamp), stamp


class TestComposeSuppression:
    def test_rate_cap_suppresses_with_event_not_error(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="canary", heartbeat_rate_limit=1))
            _knowledge(mgr, "rate cap body")
            first = compose_heartbeat(mgr, project=PROJECT, agent=AGENT)
            assert first["state"] == "delta"
            second = compose_heartbeat(mgr, project=PROJECT, agent=AGENT)
            assert second["state"] == "suppressed"
            assert second["reason"] == "rate_cap"
            assert second["text"] == ""
            assert second["events"] == [("heartbeat_suppressed", {"reason": "rate_cap"})]
            # Refusal consumed no quota: the cursor was not advanced by it.
            assert second["cursor_after"] is None
            mgr.close()

    def test_probe_error_suppresses_and_never_raises(
        self, manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _knowledge(manager, "probe error body")

        def _raise(self: SQLiteStore, project: str, since_iso: str) -> bool:
            raise sqlite3.OperationalError("disk exploded")

        monkeypatch.setattr(SQLiteStore, "exists_since", _raise)
        result = compose_heartbeat(manager, project=PROJECT, agent=AGENT)
        assert result["state"] == "suppressed"
        assert result["reason"] == "probe_error"
        assert result["text"] == ""
        # The cursor is untouched by a failed probe.
        assert read_awareness_heartbeat_cursor(manager, project=PROJECT, agent=AGENT) is None


# ── The call_tool wrapper: mode ladder + deny-list (CI pins 1, 2, 4) ─────────


def _reset_call_tracker() -> dict[str, Any]:
    """Snapshot/restore helper for the module-global reminder tracker."""
    return dict(mcp_server_module._checkpoint_tracker)


class TestOffByteIdentity:
    """CI pin 1 — mode=off: byte-identical to the pre-feature response."""

    async def test_off_equals_dispatch_bytes_with_delta_present(
        self, manager: MemoryManager
    ) -> None:
        _knowledge(manager, "off-path byte identity body")
        args = {"project": PROJECT, "agent": AGENT, "session": SESSION, "limit": 3}
        snap = _reset_call_tracker()
        with patch("vesmaro.mcp_server.get_manager", return_value=manager):
            direct = await _call_tool_dispatch("mnemos_list_recent", args)
            mcp_server_module._checkpoint_tracker.clear()
            mcp_server_module._checkpoint_tracker.update(snap)
            via_wrapper = await call_tool("mnemos_list_recent", args)
        assert [c.text for c in via_wrapper] == [c.text for c in direct]
        assert len(via_wrapper) == 1

    async def test_off_writes_no_heartbeat_state(self, manager: MemoryManager) -> None:
        _knowledge(manager, "off writes nothing body")
        args = {"project": PROJECT, "agent": AGENT, "session": SESSION}
        with patch("vesmaro.mcp_server.get_manager", return_value=manager):
            await call_tool("mnemos_list_recent", args)
        assert read_awareness_heartbeat_cursor(manager, project=PROJECT, agent=AGENT) is None

    @pytest.mark.parametrize(
        ("tool", "args", "seed"),
        [
            # A read tool with identity and a pending delta (the case where
            # a tail WOULD exist in canary) — the primary pin condition.
            (
                "mnemos_list_recent",
                {"project": PROJECT, "agent": AGENT, "session": SESSION, "limit": 3},
                "delta",
            ),
            # A calm store (probe negative — the most common real state).
            ("mnemos_list_recent", {"project": PROJECT, "agent": AGENT, "limit": 3}, "none"),
            # A search-shaped tool (JSON-serialized result path).
            (
                "mnemos_search",
                {"query": "quokka", "project": PROJECT, "agent": AGENT, "session": SESSION},
                "delta",
            ),
            # A deny-listed surface with a pending delta.
            (
                "mnemos_assemble_context",
                {"project": PROJECT, "agent": AGENT, "session": SESSION, "query": "x"},
                "delta",
            ),
            # An identity-less call (no agent — no heartbeat possible).
            ("mnemos_stats", {}, "delta"),
        ],
    )
    async def test_off_equals_dispatch_bytes_any_tool_any_condition(
        self, tmp_path: Path, tool: str, args: dict[str, Any], seed: str
    ) -> None:
        """The pin's full wording: mode=off ⇒ byte-identical response for
        ANY tool under ANY condition — delta present, calm store,
        deny-listed surface, identity-less call."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="off"))
            if seed == "delta":
                _knowledge(mgr, "off identity sweep body")
                _checkpoint(mgr, goals="off identity neighbor goal", agent=NEIGHBOR_A)
            snap = _reset_call_tracker()
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                direct = await _call_tool_dispatch(tool, dict(args))
                mcp_server_module._checkpoint_tracker.clear()
                mcp_server_module._checkpoint_tracker.update(snap)
                via_wrapper = await call_tool(tool, dict(args))
            assert [c.text for c in via_wrapper] == [c.text for c in direct]
            assert len(via_wrapper) == len(direct)
            mgr.close()


class TestShadowByteIdentity:
    """CI pin 2 — shadow: byte-identical to off, but the contour ran."""

    async def test_shadow_response_equals_off_response(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="off"))
            _knowledge(mgr, "shadow byte identity body")
            args = {"project": PROJECT, "agent": AGENT, "session": SESSION, "limit": 3}
            snap = _reset_call_tracker()
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                off_contents = await call_tool("mnemos_list_recent", args)
                # Same store, same rows — flip ONLY the mode.
                mgr.settings.awareness.native_heartbeat_mode = "shadow"
                mcp_server_module._checkpoint_tracker.clear()
                mcp_server_module._checkpoint_tracker.update(snap)
                shadow_contents = await call_tool("mnemos_list_recent", args)
            assert [c.text for c in shadow_contents] == [c.text for c in off_contents]
            assert len(shadow_contents) == 1  # no tail rendered in shadow
            # …and the contour really ran: shadow advanced the cursor (the
            # measurement parity — canary would have delivered this delta).
            assert read_awareness_heartbeat_cursor(mgr, project=PROJECT, agent=AGENT) is not None
            mgr.close()


class TestCanaryOnRendering:
    async def test_canary_appends_tail_last(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="canary"))
            _knowledge(mgr, "canary tail body")
            _checkpoint(mgr, goals="canary neighbor goal", agent=NEIGHBOR_A)
            args = {"project": PROJECT, "agent": AGENT, "session": SESSION, "limit": 3}
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                contents = await call_tool("mnemos_list_recent", args)
            assert len(contents) == 2
            tail = contents[-1]
            assert tail.text.startswith(f"## Peer awareness — heartbeat (project {PROJECT})")
            assert AWARENESS_DISCLAIMER in tail.text
            mgr.close()

    async def test_canary_calm_appends_exactly_one_line(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="canary"))
            args = {"project": PROJECT, "agent": AGENT, "session": SESSION, "limit": 3}
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                contents = await call_tool("mnemos_list_recent", args)
            assert len(contents) == 2
            tail = contents[-1].text
            assert tail == HEARTBEAT_CALM_LINE  # the pinned LITERAL, one line
            assert "\n" not in tail
            mgr.close()

    async def test_mode_on_renders_like_canary(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="on"))
            _knowledge(mgr, "mode on body")
            args = {"project": PROJECT, "agent": AGENT, "session": SESSION, "limit": 3}
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                contents = await call_tool("mnemos_list_recent", args)
            assert len(contents) == 2
            assert contents[-1].text.startswith("## Peer awareness — heartbeat")
            mgr.close()


class TestProjectSanitization:
    """Cascade SEC-1 (fix-first) — the client-supplied ``project`` slug
    never rides the unsolicited tail raw: no forged server lines, no
    markdown, no control characters in the envelope header."""

    async def test_hostile_project_sanitized_in_header(self, tmp_path: Path) -> None:
        hostile = "evil\n## FAKE SERVER LINE **bold**\x1b[31m"
        # The write must land under the slug the identity will resolve
        # to, so the delta exists and the ENVELOPE (not the calm-line)
        # renders.
        slug = sanitize_project_id(hostile)
        assert slug and "\n" not in slug and "FAKE SERVER" not in slug
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="on"))
            _knowledge(mgr, "sanitize body", project=slug)
            args = {"project": hostile, "agent": AGENT, "session": SESSION, "limit": 3}
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                contents = await call_tool("mnemos_list_recent", args)
            tails = [c.text for c in contents if "Peer awareness" in c.text]
            assert tails, "the tail must still ride under a hostile project"
            tail = tails[-1]
            assert "FAKE SERVER" not in tail
            assert "**bold**" not in tail
            assert "\x1b" not in tail
            first = tail.splitlines()[0]
            assert first.startswith("## Peer awareness — heartbeat (project ")
            assert "evil" in first  # sanitized residue stays on ONE line
            mgr.close()

    def test_sanitize_project_id_pipeline(self) -> None:
        assert sanitize_project_id("  ev\r\nil\tproj ") == "ev_il_proj"
        assert sanitize_project_id("***") == "project"
        assert sanitize_project_id("Evil UPPER") == "evil_upper"  # store slug alphabet
        assert len(sanitize_project_id("x" * 500)) == HEARTBEAT_PROJECT_ID_MAX_CHARS


class TestDenyList:
    """CI pin 4 — assemble/export/import never carry the tail."""

    async def test_deny_listed_tools_get_no_tail_in_canary(self, tmp_path: Path) -> None:
        for tool in sorted(HEARTBEAT_DENY_TOOLS):
            with tempfile.TemporaryDirectory() as tmpdir:
                mgr = _manager(_settings(Path(tmpdir), mode="canary"))
                _knowledge(mgr, f"deny list body {tool}")
                args: dict[str, Any] = {"project": PROJECT, "agent": AGENT, "session": SESSION}
                if tool == "mnemos_assemble_context":
                    args.update({"project": PROJECT, "query": "deny", "session": SESSION})
                with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                    contents = await call_tool(tool, args)
                tails = [c for c in contents if "Peer awareness" in c.text]
                assert not tails, f"{tool} must never carry the heartbeat tail"
                # And no cursor advance happened for the denied surface.
                assert read_awareness_heartbeat_cursor(mgr, project=PROJECT, agent=AGENT) is None
                mgr.close()

    async def test_identity_less_call_gets_no_tail_no_error(self, manager: MemoryManager) -> None:
        _knowledge(manager, "identity-less body")
        with patch("vesmaro.mcp_server.get_manager", return_value=manager):
            contents = await call_tool("mnemos_list_tags", {})
        assert len(contents) == 1  # the plain response, untouched

    async def test_heartbeat_failure_never_breaks_the_call(
        self, manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even a heartbeat that explodes mid-flight leaves the tool call's
        bytes intact (the wrapper's suppress-everything contract)."""

        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("heartbeat exploded")

        monkeypatch.setattr("vesmaro.heartbeat.compose_heartbeat", _boom)
        # manager fixture is mode=off — patch the settings to canary in place
        manager.settings.awareness.native_heartbeat_mode = "canary"
        _knowledge(manager, "explosion body")
        args = {"project": PROJECT, "agent": AGENT, "session": SESSION, "limit": 3}
        with patch("vesmaro.mcp_server.get_manager", return_value=manager):
            contents = await call_tool("mnemos_list_recent", args)
        assert len(contents) == 1
        assert "Peer awareness" not in contents[0].text


# ── The events plane (ADR-0026 family, metrics sidecar) ──────────────────────

SECRET_GOAL = "secret-goal-zq7x-watermark"


def _sidecar_events(mgr: MemoryManager) -> list[dict[str, Any]]:
    db = mgr.settings.mnemos.data_dir / "metrics.sqlite"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM awareness_events ORDER BY id")]
    finally:
        conn.close()


class TestHeartbeatEvents:
    async def test_shadow_writes_the_full_event_chain(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="shadow"))
            _checkpoint(mgr, goals=SECRET_GOAL, agent=NEIGHBOR_A)
            args = {"project": PROJECT, "agent": AGENT, "session": SESSION, "limit": 3}
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                await call_tool("mnemos_list_recent", args)
            events = _sidecar_events(mgr)
            kinds = [e["kind"] for e in events]
            assert kinds == ["tool_call", "delta_available", "heartbeat_delivery"]
            delivery = events[-1]
            assert delivery["project"] == PROJECT
            assert delivery["agent"] == AGENT
            assert delivery["session"] == SESSION
            meta = json.loads(delivery["meta_json"])
            assert meta["tool"] == "mnemos_list_recent"
            assert meta["state"] == "delta"
            assert meta["lines"] >= 1
            assert meta["tokens_est"] >= 1
            assert meta["cursor_before"] is None
            assert meta["cursor_after"]
            mgr.close()

    async def test_shadow_calm_writes_calm_delivery(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="shadow"))
            args = {"project": PROJECT, "agent": AGENT, "session": SESSION, "limit": 3}
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                await call_tool("mnemos_list_recent", args)
            events = _sidecar_events(mgr)
            kinds = [e["kind"] for e in events]
            assert kinds == ["tool_call", "heartbeat_delivery"]
            assert json.loads(events[-1]["meta_json"])["state"] == "calm"
            mgr.close()

    async def test_off_writes_no_events_at_all(self, manager: MemoryManager) -> None:
        _knowledge(manager, "off events body")
        args = {"project": PROJECT, "agent": AGENT, "session": SESSION, "limit": 3}
        with patch("vesmaro.mcp_server.get_manager", return_value=manager):
            await call_tool("mnemos_list_recent", args)
        assert _sidecar_events(manager) == []

    async def test_events_carry_zero_peer_content(self, tmp_path: Path) -> None:
        """CWE-359: no peer goal text, no peer ids — only the caller's
        identity slugs, counters, enums and tool ids."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="canary"))
            _checkpoint(mgr, goals=SECRET_GOAL, agent=NEIGHBOR_A)
            args = {"project": PROJECT, "agent": AGENT, "session": SESSION, "limit": 3}
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                await call_tool("mnemos_list_recent", args)
            blob = json.dumps(_sidecar_events(mgr))
            assert SECRET_GOAL not in blob
            assert NEIGHBOR_A not in blob
            mgr.close()

    async def test_peer_write_event_on_write_tools(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="shadow"))
            add_args = {
                "content": "a write through the MCP surface",
                "tags": [f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:learning"],
                "session": SESSION,
            }
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                await call_tool("mnemos_add", add_args)
            events = _sidecar_events(mgr)
            kinds = [e["kind"] for e in events]
            assert "peer_write" in kinds
            pw = next(e for e in events if e["kind"] == "peer_write")
            assert pw["project"] == PROJECT and pw["agent"] == AGENT
            assert json.loads(pw["meta_json"])["tool"] == "mnemos_add"
            mgr.close()

    async def test_deny_listed_tool_lands_in_denominator_only(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="canary"))
            _knowledge(mgr, "deny list events body")
            args = {"project": PROJECT, "agent": AGENT, "session": SESSION, "query": "x"}
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                await call_tool("mnemos_assemble_context", args)
            kinds = [e["kind"] for e in _sidecar_events(mgr)]
            assert kinds == ["tool_call"]  # no compose events for denied surfaces
            mgr.close()

    async def test_suppressed_rate_cap_event_written(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="canary", heartbeat_rate_limit=1))
            _knowledge(mgr, "rate cap events body")
            args = {"project": PROJECT, "agent": AGENT, "session": SESSION, "limit": 3}
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                await call_tool("mnemos_list_recent", args)
                await call_tool("mnemos_list_recent", args)
            suppressed = [
                e
                for e in _sidecar_events(mgr)
                if e["kind"] == "heartbeat_suppressed"
                and json.loads(e["meta_json"])["reason"] == "rate_cap"
            ]
            assert len(suppressed) == 1
            mgr.close()

    async def test_suppressed_probe_error_event_written(
        self, manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _knowledge(manager, "probe error events body")
        manager.settings.awareness.native_heartbeat_mode = "shadow"

        def _raise(self: SQLiteStore, project: str, since_iso: str) -> bool:
            raise sqlite3.OperationalError("boom")

        monkeypatch.setattr(SQLiteStore, "exists_since", _raise)
        args = {"project": PROJECT, "agent": AGENT, "session": SESSION, "limit": 3}
        with patch("vesmaro.mcp_server.get_manager", return_value=manager):
            contents = await call_tool("mnemos_list_recent", args)
        assert len(contents) == 1  # the call survived the broken probe
        suppressed = [
            e
            for e in _sidecar_events(manager)
            if e["kind"] == "heartbeat_suppressed"
            and json.loads(e["meta_json"])["reason"] == "probe_error"
        ]
        assert len(suppressed) == 1


class TestAwarenessMetaGate:
    """The C5 twin for awareness meta — fail-closed allowlist."""

    def test_clean_meta_passes(self) -> None:
        clean = validate_awareness_meta(
            {
                "tool": "mnemos_list_recent",
                "state": "delta",
                "lines": 6,
                "tokens_est": 118,
                "cursor_before": None,
                "cursor_after": "2026-10-01T12:00:00.000001+00:00",
            }
        )
        assert clean is not None and clean["state"] == "delta"

    def test_unknown_key_refuses_whole_meta(self) -> None:
        assert validate_awareness_meta({"tool": "t", "goal_title": "peer text"}) is None

    def test_bad_state_enum_refused(self) -> None:
        assert validate_awareness_meta({"state": "excited"}) is None

    def test_bad_reason_enum_refused(self) -> None:
        assert validate_awareness_meta({"reason": "because"}) is None

    def test_lines_must_be_int(self) -> None:
        assert validate_awareness_meta({"lines": "six"}) is None
        assert validate_awareness_meta({"lines": True}) is None

    def test_cursor_must_be_iso_shaped(self) -> None:
        assert validate_awareness_meta({"cursor_after": "not-a-cursor"}) is None
        assert validate_awareness_meta({"cursor_before": "2026-10-01T12:00:00+00:00"}) is not None

    def test_none_meta_passes_as_empty(self) -> None:
        assert validate_awareness_meta(None) == {}


class TestSinkAwarenessEvents:
    def test_record_and_read_roundtrip(self, tmp_path: Path) -> None:
        store = MetricsStore(tmp_path / "metrics.sqlite")
        row_id = store.record_awareness_event(
            kind="peer_write",
            project=PROJECT,
            agent=AGENT,
            session=SESSION,
            meta={"tool": "mnemos_add"},
        )
        assert row_id is not None
        conn = sqlite3.connect(tmp_path / "metrics.sqlite")
        try:
            row = conn.execute(
                "SELECT kind, project, agent, session, meta_json FROM awareness_events"
            ).fetchone()
        finally:
            conn.close()
        assert row == ("peer_write", PROJECT, AGENT, SESSION, '{"tool": "mnemos_add"}')
        store.close()

    def test_bad_kind_refused(self, tmp_path: Path) -> None:
        store = MetricsStore(tmp_path / "metrics.sqlite")
        assert (
            store.record_awareness_event(kind="peer_write", meta={"tool": "mnemos_add"}) is not None
        )
        assert store.record_awareness_event(kind="peeer_write") is None  # typo'd kind
        conn = sqlite3.connect(tmp_path / "metrics.sqlite")
        try:
            n = conn.execute("SELECT COUNT(*) FROM awareness_events").fetchone()[0]
        finally:
            conn.close()
        assert n == 1
        store.close()

    def test_table_created_on_legacy_sidecar(self, tmp_path: Path) -> None:
        """Additive delivery: a sidecar created before W2a gains the
        awareness_events table on its next open (born-final philosophy —
        CREATE IF NOT EXISTS in the connect script, no migrations)."""
        db = tmp_path / "metrics.sqlite"
        first = MetricsStore(db)
        conn = first._conn()
        assert conn is not None
        conn.execute("DROP TABLE awareness_events")
        conn.commit()
        first.close()
        second = MetricsStore(db)
        conn2 = second._conn()
        assert conn2 is not None
        names = {r[0] for r in conn2.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "awareness_events" in names
        second.close()


_WAVE0_AWARENESS_DDL = (
    "CREATE TABLE awareness_events ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " ts REAL NOT NULL,"
    " kind TEXT NOT NULL CHECK (kind IN ('peer_write','delta_available',"
    "'heartbeat_delivery','heartbeat_suppressed','tool_call')),"
    " project TEXT,"
    " agent TEXT,"
    " session TEXT,"
    " meta_json TEXT)"
)


class TestSec4AwarenessKindsMigration:
    """Cascade SEC-4: the wave-0 five-kind CHECK refuses the
    ``conflict_hint_emitted`` funnel event — legacy sidecars migrate onto
    the extended enum on their next open (a0-rebuild pattern)."""

    def test_fresh_sidecar_accepts_conflict_hint_emitted(self, tmp_path: Path) -> None:
        store = MetricsStore(tmp_path / "metrics.sqlite")
        row_id = store.record_awareness_event(kind="conflict_hint_emitted", meta={"tool": "t"})
        assert row_id is not None
        store.close()

    def test_fresh_sidecar_ddl_carries_the_kind(self, tmp_path: Path) -> None:
        db = tmp_path / "metrics.sqlite"
        store = MetricsStore(db)
        assert store._conn() is not None  # force the bootstrap
        store.close()
        conn = sqlite3.connect(db)
        try:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='awareness_events'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert "conflict_hint_emitted" in sql

    def test_legacy_sidecar_rebuilt_with_rows_kept(self, tmp_path: Path) -> None:
        db = tmp_path / "metrics.sqlite"
        store = MetricsStore(db)
        store.record_awareness_event(
            kind="tool_call", project=PROJECT, agent=AGENT, session=SESSION, meta={"tool": "t"}
        )
        store.close()
        # Regress the table to the exact wave-0 shape (with one funnel row).
        conn = sqlite3.connect(db)
        try:
            conn.executescript(
                f"DROP TABLE awareness_events; {_WAVE0_AWARENESS_DDL};"
                "INSERT INTO awareness_events (ts, kind, project, agent, session, meta_json)"
                " VALUES (1.0, 'tool_call', 'p', 'a', 's', '{\"tool\": \"t\"}');"
            )
            conn.commit()
        finally:
            conn.close()
        # Next open: the migration runs, legacy rows survive, the new
        # kind is accepted.
        second = MetricsStore(db)
        assert second.record_awareness_event(kind="conflict_hint_emitted", meta={}) is not None
        conn2 = sqlite3.connect(db)
        try:
            kinds = [r[0] for r in conn2.execute("SELECT kind FROM awareness_events ORDER BY id")]
            sql = conn2.execute(
                "SELECT sql FROM sqlite_master WHERE name='awareness_events'"
            ).fetchone()[0]
            idx = {r[0] for r in conn2.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        finally:
            conn2.close()
        assert kinds == ["tool_call", "conflict_hint_emitted"]
        assert "conflict_hint_emitted" in sql
        assert {"idx_awareness_kind_ts", "idx_awareness_project_ts"} <= idx
        second.close()

    def test_migration_is_idempotent_across_reopens(self, tmp_path: Path) -> None:
        db = tmp_path / "metrics.sqlite"
        first = MetricsStore(db)
        first.record_awareness_event(kind="tool_call", meta={})
        first.close()
        for _ in range(2):
            again = MetricsStore(db)
            assert again.record_awareness_event(kind="conflict_hint_emitted", meta={}) is not None
            again.close()
        conn = sqlite3.connect(db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM awareness_events").fetchone()[0]
        finally:
            conn.close()
        assert n == 3  # one legacy + two post-migration writes, nothing duplicated

    def test_migration_drops_nothing_on_legacy_empty_table(self, tmp_path: Path) -> None:
        db = tmp_path / "metrics.sqlite"
        store = MetricsStore(db)
        assert store._conn() is not None  # force the bootstrap (creates the fresh table)
        store.close()
        conn = sqlite3.connect(db)
        try:
            conn.executescript(f"DROP TABLE awareness_events; {_WAVE0_AWARENESS_DDL};")
            conn.commit()
        finally:
            conn.close()
        second = MetricsStore(db)
        assert second._conn() is not None  # the open runs SCHEMA_SQL + the migration
        second.close()
        conn2 = sqlite3.connect(db)
        try:
            sql = conn2.execute(
                "SELECT sql FROM sqlite_master WHERE name='awareness_events'"
            ).fetchone()[0]
        finally:
            conn2.close()
        assert "conflict_hint_emitted" in sql


# ── shared manager helpers ────────────────────────────────────────────────────


def _knowledge(
    mgr: MemoryManager, content: str, agent: str = NEIGHBOR_A, *, project: str = PROJECT
) -> str:
    memory = mgr.add(
        MemoryCreate(
            content=content,
            tags=[f"project:{project}", f"agent:{agent}", "mnemos:learning"],
            source=MemorySource.MCP,
            status=MemoryStatus.PUBLISHED,
        ),
        project=project,
        agent=agent,
    )
    return memory.id


def _checkpoint(
    mgr: MemoryManager,
    *,
    goals: str,
    agent: str,
    session: str = NEIGHBOR_SESSION,
    project: str = PROJECT,
) -> str:
    memory, _dup = mgr.save_checkpoint(
        {"goals": goals, "in_progress": "wiring"}, project=project, agent=agent, session=session
    )
    return memory.id


def _payload(contents: list[Any]) -> dict[str, Any]:
    """First JSON value of a dispatch response — the response text can
    carry the one-time checkpoint reminder / update hint appended AFTER
    the JSON payload, so a plain ``json.loads`` over the whole text is
    wrong for any test that is not the process's first dispatch."""
    value, _end = json.JSONDecoder().raw_decode(contents[0].text)
    return value


# ── W1 (ADR-0035 wave 1): canary slice — identity on the frequent reads, ─────
#    the SEC-2 awareness-surface deny pin, the mode-linked hooks default,
#    the ARCH-2 clamp pin and the SEC-5 hydration bound.


class TestAgentIdentityArgument:
    """(a) The optional ``agent`` argument on ``mnemos_search`` /
    ``mnemos_recall_context`` feeds the heartbeat identity extraction —
    the funnel's most frequent calls used to see None there."""

    async def test_search_agent_arg_feeds_heartbeat_identity(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="shadow"))
            _knowledge(mgr, "agent arg search body")
            args = {"query": "agent arg", "project": PROJECT, "agent": AGENT, "session": SESSION}
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                await call_tool("mnemos_search", args)
            events = _sidecar_events(mgr)
            tool_call = next(e for e in events if e["kind"] == "tool_call")
            assert tool_call["project"] == PROJECT
            assert tool_call["agent"] == AGENT
            # Identity complete → the contour actually composed.
            assert "delta_available" in [e["kind"] for e in events]
            mgr.close()

    async def test_recall_context_agent_arg_feeds_heartbeat_identity(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="shadow"))
            _checkpoint(mgr, goals="recall identity goal", agent=NEIGHBOR_A)
            args = {"project": PROJECT, "agent": AGENT, "session": SESSION}
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                await call_tool("mnemos_recall_context", args)
            events = _sidecar_events(mgr)
            tool_call = next(e for e in events if e["kind"] == "tool_call")
            assert tool_call["agent"] == AGENT
            assert "delta_available" in [e["kind"] for e in events]
            mgr.close()

    async def test_search_without_agent_stays_identity_less(self, tmp_path: Path) -> None:
        """Legacy no-agent calls keep working AND stay identity-less: the
        tool_call denominator lands, no compose runs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="shadow"))
            _knowledge(mgr, "legacy call body")
            args = {"query": "legacy call", "project": PROJECT, "session": SESSION}
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                await call_tool("mnemos_search", args)
            events = _sidecar_events(mgr)
            assert [e["kind"] for e in events] == ["tool_call"]
            assert events[0]["agent"] is None
            mgr.close()

    def test_schemas_carry_optional_agent_with_slug_cap(self) -> None:
        """Schema-level cap mirrors the agent:<slug> contract (1-64 chars
        of [a-z0-9_-]); the argument is additive — required lists and all
        legacy arguments untouched."""
        tools = {t.name: t for t in asyncio.run(_canonical_tools())}
        for name in ("mnemos_search", "mnemos_recall_context"):
            props = tools[name].input_schema["properties"]
            agent = props["agent"]
            assert agent["type"] == "string"
            assert agent["maxLength"] == 64
            # Additive: not required, legacy contract intact.
            assert "agent" not in tools[name].input_schema.get("required", [])
            assert "query" in props and "project" in props


class TestSEC2AwarenessSurfacesDenied:
    """Cascade SEC-2: the awareness surfaces themselves never carry the
    native tail (double render — the C13 assemble_context ruling)."""

    async def test_awareness_pre_flight_gets_no_native_tail(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="canary"))
            _checkpoint(mgr, goals="sec2 pre-flight goal", agent=NEIGHBOR_A)
            args = {
                "action": "pre_flight",
                "session": SESSION,
                "project": PROJECT,
                "agent": AGENT,
            }
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                contents = await call_tool("mnemos_awareness", args)
            assert all("Peer awareness — heartbeat" not in c.text for c in contents)
            # Denominator only — no compose events for a denied surface.
            kinds = [e["kind"] for e in _sidecar_events(mgr)]
            assert kinds == ["tool_call"]
            mgr.close()

    async def test_hooks_pre_llm_call_gets_no_native_tail(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="canary"))
            _knowledge(mgr, "sec2 hooks body")
            args = {
                "action": "pre_llm_call",
                "session": SESSION,
                "project": PROJECT,
                "agent": AGENT,
            }
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                contents = await call_tool("mnemos_hooks", args)
            assert len(contents) == 1  # the hook payload alone — no tail TextContent
            assert "Peer awareness — heartbeat" not in contents[0].text
            kinds = [e["kind"] for e in _sidecar_events(mgr)]
            assert kinds == ["tool_call"]
            mgr.close()


class TestHooksAwarenessDefaultOn:
    """(b) include_awareness resolves mode-linked: canary/on compose by
    default, off/shadow stay byte-identical, explicit boolean wins."""

    async def test_pre_llm_call_defaults_on_under_canary(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="canary"))
            _checkpoint(mgr, goals="default-on goal", agent=NEIGHBOR_A)
            args = {
                "action": "pre_llm_call",
                "session": SESSION,
                "project": PROJECT,
                "agent": AGENT,
            }
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                contents = await call_tool("mnemos_hooks", args)
            payload = _payload(contents)
            assert "awareness" in payload  # composed WITHOUT the explicit flag
            mgr.close()

    async def test_pre_llm_call_defaults_off_under_shadow(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="shadow"))
            args = {
                "action": "pre_llm_call",
                "session": SESSION,
                "project": PROJECT,
                "agent": AGENT,
            }
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                contents = await call_tool("mnemos_hooks", args)
            payload = _payload(contents)
            assert "awareness" not in payload  # byte-identical pre-#254 shape
            mgr.close()

    async def test_explicit_false_wins_under_canary(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="canary"))
            args = {
                "action": "pre_llm_call",
                "session": SESSION,
                "project": PROJECT,
                "agent": AGENT,
                "include_awareness": False,
            }
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                contents = await call_tool("mnemos_hooks", args)
            payload = _payload(contents)
            assert "awareness" not in payload
            mgr.close()

    async def test_on_session_start_defaults_on_under_canary(self, tmp_path: Path) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="canary"))
            _checkpoint(mgr, goals="presence default goal", agent=NEIGHBOR_A)
            args = {
                "action": "on_session_start",
                "session": SESSION,
                "project": PROJECT,
                "agent": AGENT,
            }
            with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
                contents = await call_tool("mnemos_hooks", args)
            payload = _payload(contents)
            assert "presence" in payload
            mgr.close()

    def test_off_mode_default_is_false(self, manager: MemoryManager) -> None:
        from vesmaro.hooks import _include_awareness_default

        assert _include_awareness_default(manager) is False

    def test_canary_mode_default_is_true(self, tmp_path: Path) -> None:
        from vesmaro.hooks import _include_awareness_default

        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = _manager(_settings(Path(tmpdir), mode="canary"))
            assert _include_awareness_default(mgr) is True
            mgr.close()


class TestSec5DeltaStageHydrationBound:
    """Cascade SEC-5: the delta stage hydrates at most
    :data:`DELTA_FEED_LIMIT` rows per query — pinned so the async-contour
    budget cannot silently regress to an unbounded fetch."""

    def test_heartbeat_delta_stage_queries_are_bounded(
        self, manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        limits: list[int | None] = []
        real = manager.list_recent

        def _spy(*args: Any, **kwargs: Any) -> Any:
            limits.append(kwargs.get("limit"))
            return real(*args, **kwargs)

        monkeypatch.setattr(manager, "list_recent", _spy)
        _knowledge(manager, "sec5 bound body")
        result = compose_heartbeat(manager, project=PROJECT, agent=AGENT)
        assert result["state"] == "delta"  # the expensive leg actually ran
        assert limits, "the delta stage must query through list_recent"
        assert all(lim is not None and lim <= 200 for lim in limits), (
            f"unbounded delta-stage fetch: limits={limits}"
        )
