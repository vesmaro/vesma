"""Smoke tests for MCP server dispatch routing.

Validates three contracts:
- test_routing_all_tools_recognized: every tool returned by list_tools() is
  recognized by _dispatch (routing never falls through to "Unknown tool: ...").
- test_dispatch_unknown_tool_returns_error_string: unregistered names produce
  the expected "Unknown tool: ..." sentinel string.
- test_call_tool_unknown_wraps_error_in_text_content: call_tool() negative path.
- test_list_tools_contract: list_tools() returns a non-empty list and each Tool
  carries name, description, and inputSchema (MCP schema contract).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from vesmaro.mcp_server import _dispatch, call_tool, list_tools

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Minimum valid arguments per registered tool.
# Tags for mnemos_add / mnemos_ingest_url include the required
# project:/agent:/mnemos: trio so validate_tag_contract (mocked in routing tests)
# does not need real validation logic.
_TOOL_ARGS: dict[str, dict] = {
    "mnemos_add": {
        "content": "smoke content",
        "tags": ["project:smoke", "agent:qa", "mnemos:decision"],
    },
    "mnemos_agent_recall": {"agent": "qa-agent"},
    "mnemos_auto_collect_status": {},
    "mnemos_export": {"output_path": "/tmp/smoke-export.json"},
    "mnemos_import": {"source_path": "/tmp/smoke-import.json"},
    "mnemos_ingest_url": {
        "url": "https://example.com",
        "tags": ["project:smoke", "agent:qa", "mnemos:decision"],
    },
    "mnemos_list_recent": {},
    "mnemos_list_tags": {},
    "mnemos_recall_context": {"project": "smoke"},
    "mnemos_save_context": {"project": "smoke", "goals": "smoke goals"},
    "mnemos_search": {"query": "smoke test"},
    "mnemos_stats": {},
    "mnemos_watch_start": {"project_id": "smoke", "agent": "qa-agent"},
    "mnemos_watch_status": {},
    "mnemos_watch_stop": {},
    "mnemos_align_prefix": {"text": "Session sess-abc123 at 2026-07-17T10:00:00Z"},
}

# Tools whose dispatch calls a module-level function (run_export / run_import)
# rather than a manager method. The routing-coverage test patches these so the
# mock manager never drives the real export/import logic (which needs a live
# SQLite store and would crash a MagicMock).
_MODULE_DISPATCH_TOOLS: frozenset[str] = frozenset({"mnemos_export", "mnemos_import"})

# ---------------------------------------------------------------------------
# Routing assertions map (mcp-3 finding)
# tool_name -> (expected_manager_method, [forbidden_manager_methods])
# Covers all tools with a unique 1:1 manager method.
# mnemos_save_context (shares mgr.add) and mnemos_auto_collect_status
# (no manager data method) are handled in dedicated tests below.
# ---------------------------------------------------------------------------
_ROUTING_MAP: dict[str, tuple[str, list[str]]] = {
    "mnemos_add": ("add", ["search", "list_recent", "recall_context"]),
    "mnemos_search": ("search", ["add", "list_recent", "agent_recall"]),
    "mnemos_agent_recall": ("agent_recall", ["search", "add", "recall_context"]),
    "mnemos_recall_context": ("recall_context", ["search", "add", "agent_recall"]),
    "mnemos_list_recent": ("list_recent", ["search", "add", "list_tags"]),
    "mnemos_list_tags": ("list_tags", ["search", "list_recent", "stats"]),
    "mnemos_stats": ("stats", ["search", "list_tags", "list_recent"]),
    "mnemos_ingest_url": ("ingest_url", ["add", "search", "list_recent"]),
    "mnemos_watch_start": ("watch_start", ["watch_stop", "watch_status", "search"]),
    "mnemos_watch_stop": ("watch_stop", ["watch_start", "watch_status", "search"]),
    "mnemos_watch_status": ("watch_status", ["watch_start", "watch_stop", "search"]),
    "mnemos_align_prefix": ("align_prefix", ["search", "add", "recall_context"]),
}


def _make_mock_manager() -> MagicMock:
    """Return a MagicMock MemoryManager with safe stub return values for all methods."""
    mock_memory = MagicMock()
    mock_memory.id = "smoke-id-1"
    mock_memory.auto_title.return_value = "Smoke Memory"
    mock_memory.status = "published"

    mgr = MagicMock()
    mgr.settings.mnemos.strict_tag_contract = False
    mgr.add.return_value = mock_memory
    # mnemos #251 D0: mnemos_save_context routes through the checkpoint
    # single authority (validation + binding + dedup live in the manager).
    mgr.save_checkpoint.return_value = (mock_memory, False)
    mgr.search.return_value = []
    mgr.agent_recall.return_value = []
    mgr.recall_context.return_value = []
    mgr.list_recent.return_value = []
    mgr.list_tags.return_value = {}
    mgr.stats.return_value = {"total": 0}
    mgr.ingest_url.return_value = mock_memory
    mgr.watch_start.return_value = None
    mgr.watch_stop.return_value = None
    mgr.watch_status.return_value = "watching: 0 paths"
    mgr.align_prefix.return_value = {
        "aligned_text": "aligned",
        "extracted": [],
        "prefix_stabilized": False,
        "moved_chars": 0,
    }
    return mgr


# ---------------------------------------------------------------------------
# Test 1 - routing coverage (parametrized per tool)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", sorted(_TOOL_ARGS.keys()))
async def test_routing_all_tools_recognized(tool_name: str) -> None:
    """_dispatch must route every registered tool - must NOT return 'Unknown tool: ...'."""
    mock_mgr = _make_mock_manager()
    with (
        patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr),
        patch(
            "vesmaro.mcp_server.validate_tag_contract",
            side_effect=lambda tags, **_kw: tags,
        ),
    ):
        if tool_name in _MODULE_DISPATCH_TOOLS:
            # mnemos_export / mnemos_import dispatch to module-level
            # run_export / run_import functions (imported locally inside
            # _handle_export / _handle_import). Patch them at their source
            # module so the local import picks up the fake.
            if tool_name == "mnemos_export":
                from pathlib import Path

                from vesmaro.cli.export import CompressMode, ExportFormat, ExportResult

                fake = ExportResult(
                    path=Path("/tmp/smoke-export.json"),
                    format=ExportFormat.JSON,
                    compress=CompressMode.NONE,
                    encrypted=False,
                    memory_count=0,
                    project_count=0,
                    bytes_written=0,
                )
                with patch("vesmaro.cli.export.run_export", return_value=fake):
                    result = await _dispatch(tool_name, _TOOL_ARGS[tool_name])
            else:  # mnemos_import
                from vesmaro.cli.import_ import ImportResult

                fake = ImportResult(mode="merge", dry_run=False)
                with patch("vesmaro.cli.import_.run_import", return_value=fake):
                    result = await _dispatch(tool_name, _TOOL_ARGS[tool_name])
        else:
            result = await _dispatch(tool_name, _TOOL_ARGS[tool_name])

    assert not (isinstance(result, str) and result.startswith("Unknown tool:")), (
        f"Tool {tool_name!r} was not recognized by _dispatch - routing is broken"
    )


# ---------------------------------------------------------------------------
# Test 2 - negative path via _dispatch
# ---------------------------------------------------------------------------


async def test_dispatch_unknown_tool_returns_error_string() -> None:
    """_dispatch with an unregistered name must return the 'Unknown tool: ...' sentinel."""
    mock_mgr = _make_mock_manager()
    with patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr):
        result = await _dispatch("nonexistent_tool", {})

    assert isinstance(result, str), "Expected str return for unknown tool"
    assert "Unknown tool:" in result
    assert "nonexistent_tool" in result


# ---------------------------------------------------------------------------
# Test 2b - negative path via call_tool (full stack)
# ---------------------------------------------------------------------------


async def test_call_tool_unknown_wraps_error_in_text_content() -> None:
    """call_tool() with an unregistered name returns TextContent with 'Unknown tool: ...'."""
    mock_mgr = _make_mock_manager()
    with patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr):
        contents = await call_tool("nonexistent_tool", {})

    assert len(contents) == 1
    assert "Unknown tool:" in contents[0].text
    assert "nonexistent_tool" in contents[0].text


# ---------------------------------------------------------------------------
# Test 3 - MCP Tool schema contract
# ---------------------------------------------------------------------------


async def test_list_tools_contract() -> None:
    """list_tools() returns a non-empty list; each Tool satisfies MCP schema contract."""
    tools = await list_tools()

    assert len(tools) > 0, "list_tools() must return at least one tool"
    for tool in tools:
        assert tool.name, f"Tool missing 'name': {tool!r}"
        assert tool.description, f"Tool {tool.name!r} missing 'description'"
        assert isinstance(tool.input_schema, dict), (
            f"Tool {tool.name!r} inputSchema must be a dict, got {type(tool.input_schema)}"
        )

    # Every tool defined in _TOOL_ARGS must appear in list_tools() output
    registered = {t.name for t in tools}
    for expected_name in _TOOL_ARGS:
        assert expected_name in registered, (
            f"Tool {expected_name!r} defined in _TOOL_ARGS but missing from list_tools()"
        )


# ---------------------------------------------------------------------------
# Test 4 - positive routing: each tool invokes the correct manager method (mcp-3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", sorted(_ROUTING_MAP.keys()))
async def test_routing_invokes_correct_manager_method(tool_name: str) -> None:
    """_dispatch must call the expected manager method and not a sibling (mcp-3)."""
    expected_method, forbidden_methods = _ROUTING_MAP[tool_name]
    mock_mgr = _make_mock_manager()
    with (
        patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr),
        patch(
            "vesmaro.mcp_server.validate_tag_contract",
            side_effect=lambda tags, **_kw: tags,
        ),
    ):
        await _dispatch(tool_name, _TOOL_ARGS[tool_name])

    getattr(mock_mgr, expected_method).assert_called_once()
    for forbidden in forbidden_methods:
        getattr(mock_mgr, forbidden).assert_not_called()


# ---------------------------------------------------------------------------
# Test 5 - save_context -> mgr.save_checkpoint edge (not add / search) (mcp-3,
# updated by mnemos #251 D0: the checkpoint channel has a single authority)
# ---------------------------------------------------------------------------


async def test_save_context_routes_to_save_checkpoint_not_add_or_search() -> None:
    """mnemos_save_context must route to mgr.save_checkpoint - not mgr.add/search."""
    mock_mgr = _make_mock_manager()
    with (
        patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr),
        patch(
            "vesmaro.mcp_server.validate_tag_contract",
            side_effect=lambda tags, **_kw: tags,
        ),
    ):
        await _dispatch("mnemos_save_context", _TOOL_ARGS["mnemos_save_context"])

    mock_mgr.save_checkpoint.assert_called_once()
    mock_mgr.add.assert_not_called()
    mock_mgr.search.assert_not_called()
    mock_mgr.recall_context.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6 - auto_collect_status reads no manager data methods (mcp-3)
# ---------------------------------------------------------------------------


async def test_auto_collect_status_touches_no_manager_data_method() -> None:
    """mnemos_auto_collect_status must read only module-level state - zero mgr data method calls."""
    mock_mgr = _make_mock_manager()
    with patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr):
        await _dispatch("mnemos_auto_collect_status", _TOOL_ARGS["mnemos_auto_collect_status"])

    data_methods = [
        "add",
        "search",
        "agent_recall",
        "recall_context",
        "list_recent",
        "list_tags",
        "stats",
        "ingest_url",
    ]
    for method_name in data_methods:
        getattr(mock_mgr, method_name).assert_not_called()


# ── Brand aliasing (rebrand mnemos → vesmaro, archcom 2026-09-14) ────────────


async def test_no_brand_env_canonical_manifest_only() -> None:
    """Without VESMARO_MCP_BRAND the manifest stays 38 canonical mnemos_ tools.

    Ф3 (epic #308): mnemos_ingest_document joined the canonical set —
    the count pin moved 27 → 28 with it. ADR-0032 PG-0 slice 4: the 10
    project-graph tools joined — 28 → 38."""
    with patch("vesmaro.mcp_server._MCP_BRAND", ""):
        tools = await list_tools()
    names = [t.name for t in tools]
    assert len(names) == 39
    assert all(n.startswith("mnemos_") for n in names)


async def test_brand_env_brand_primary_manifest() -> None:
    """VESMARO_MCP_BRAND=vesmaro: manifest is brand-primary — 38 vesmaro_* names ONLY.

    Owner ruling 2026-10-01: a doubled mnemos_*/brand_* manifest confuses
    clients. Legacy mnemos_* spellings leave the manifest but stay accepted
    on the call path (see test_canonicalize_known_alias_and_unknown_passthrough).
    """
    from vesmaro.mcp_server import _canonical_tools

    with patch("vesmaro.mcp_server._MCP_BRAND", "vesmaro"):
        tools = await list_tools()
    names = [t.name for t in tools]
    assert len(names) == 39
    assert all(n.startswith("vesmaro_") for n in names)
    assert not any(n.startswith("mnemos_") for n in names)
    assert "vesmaro_search" in names
    assert "vesmaro_retrieve" in names
    # renamed entries share the canonical schema objects (same Tool input_schema object)
    canonical = {t.name: t for t in await _canonical_tools()}
    renamed = next(t for t in tools if t.name == "vesmaro_search")
    assert renamed.input_schema == canonical["mnemos_search"].input_schema


async def test_canonicalize_known_alias_and_unknown_passthrough() -> None:
    """Known vesmaro_* aliases normalise; unknown branded names fall through."""
    from vesmaro.mcp_server import _canonicalize_tool_name

    with patch("vesmaro.mcp_server._MCP_BRAND", "vesmaro"):
        assert _canonicalize_tool_name("vesmaro_search") == "mnemos_search"
        assert _canonicalize_tool_name("vesmaro_save_context") == "mnemos_save_context"
        assert _canonicalize_tool_name("vesmaro_unknown_tool") == "vesmaro_unknown_tool"
        # legacy mnemos_* spellings leave the manifest but keep dispatching
        # (dual-period contract, retires no earlier than 6.0)
        assert _canonicalize_tool_name("mnemos_search") == "mnemos_search"
        assert _canonicalize_tool_name("mnemos_save_context") == "mnemos_save_context"
        assert _canonicalize_tool_name("other_tool") == "other_tool"


async def test_brand_alias_harvest_matches_manifest() -> None:
    """Harvest invariant: harvested names == manifest names (lockstep guard)."""
    from vesmaro.mcp_server import _canonical_tool_names, _canonical_tools

    manifest = {t.name for t in await _canonical_tools()}
    assert manifest == set(_canonical_tool_names())
    # digits are aliasable too (regex covers [a-z0-9_])
    assert all("_" in n or n.replace("mnemos_", "").isalpha() for n in manifest)


async def test_brand_self_alias_and_invalid_brand_rejected() -> None:
    """brand='mnemos' (self-alias) and malformed brands degrade to canonical-only."""
    from vesmaro.mcp_server import _canonical_tools

    with patch("vesmaro.mcp_server._MCP_BRAND", "mnemos"):
        tools = await list_tools()
    assert len(tools) == 39  # no doubling

    with patch("vesmaro.mcp_server._MCP_BRAND", "Bad Brand!"):
        tools = await _canonical_tools()
    assert len(tools) == 39  # malformed brand is a no-op


async def test_vesma_mcp_brand_canonical_env_read() -> None:
    """Rebrand 5.0.0: VESMA_MCP_BRAND canonical env is honoured with vesma_ aliases.

    Regression guard for 84a2579: the brand tests above patch _MCP_BRAND directly
    and never touch the dual-read env line; this test exercises it via env + import-reload."""
    import importlib

    from vesmaro import mcp_server as mcp

    with patch.dict(os.environ, {"VESMA_MCP_BRAND": "vesma", "VESMARO_MCP_BRAND": ""}):
        importlib.reload(mcp)
        tools = await mcp.list_tools()
    names = [t.name for t in tools]
    assert len(names) == 39
    assert all(n.startswith("vesma_") for n in names)


async def test_vesma_brand_wins_over_deprecated_vesmaro() -> None:
    """Both envs set and differing → canonical VESMA_MCP_BRAND wins (no vesmaro_ aliases)."""
    import importlib

    from vesmaro import mcp_server as mcp

    with patch.dict(
        os.environ,
        {"VESMA_MCP_BRAND": "vesma", "VESMARO_MCP_BRAND": "vesmaro"},
    ):
        importlib.reload(mcp)
        tools = await mcp.list_tools()
    names = [t.name for t in tools]
    ves_aliases = [n for n in names if n.startswith("vesma_")]
    vesmaro_aliases = [n for n in names if n.startswith("vesmaro_")]
    assert len(ves_aliases) == 39
    assert not vesmaro_aliases
    assert not any(n.startswith("mnemos_") for n in names)
    # restore
    importlib.reload(mcp)


async def test_vesmaro_brand_env_deprecated_alias_still_works() -> None:
    """VESMA_MCP_BRAND unset → deprecated VESMARO_MCP_BRAND still honoured (dual-period)."""
    import importlib

    from vesmaro import mcp_server as mcp

    with patch.dict(os.environ, {"VESMARO_MCP_BRAND": "vesmaro", "VESMA_MCP_BRAND": ""}):
        importlib.reload(mcp)
        tools = await mcp.list_tools()
    names = [t.name for t in tools]
    assert len(names) == 39
    assert all(n.startswith("vesmaro_") for n in names)
    importlib.reload(mcp)


# ---------------------------------------------------------------------------
# #456: one-time server-updated notice on the first dispatch
# ---------------------------------------------------------------------------


class _MetaStore:
    """Tiny stand-in for the sqlite meta surface (get/set only)."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.meta: dict[str, str] = dict(initial or {})
        self.fail = False

    def get_meta(self, key: str) -> str | None:
        if self.fail:
            raise RuntimeError("store down")
        return self.meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        if self.fail:
            raise RuntimeError("store down")
        self.meta[key] = value


class TestServerUpdateHint:
    """The mid-session upgrade is surfaced ONCE, on the first dispatch of
    the process, as one appended line; the store meta is re-stamped so
    the next process stays quiet. Fail-open on store errors."""

    @pytest.fixture(autouse=True)
    def _arm(self) -> Iterator[None]:
        from vesmaro.mcp_server import _reset_server_update_state

        _reset_server_update_state()
        yield
        _reset_server_update_state()

    def _manager(self, store: _MetaStore) -> MagicMock:
        mgr = MagicMock()
        mgr.sqlite = store
        mgr.list_tags.return_value = {}
        return mgr

    async def _dispatch(self, mgr: MagicMock) -> str:
        from vesmaro.mcp_server import _call_tool_dispatch

        with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
            content = await _call_tool_dispatch("mnemos_list_tags", {})
        return content[0].text

    async def test_hint_appears_exactly_once_after_version_change(self) -> None:
        from vesmaro import __version__

        store = _MetaStore({"last_reported_server_version": "0.0.1"})
        mgr = self._manager(store)
        first = await self._dispatch(mgr)
        assert "vesma server updated: 0.0.1 → " in first
        assert __version__ in first
        assert store.meta["last_reported_server_version"] == __version__
        second = await self._dispatch(mgr)
        assert "server updated" not in second  # exactly once, meta re-stamped

    async def test_no_hint_when_versions_match(self) -> None:
        from vesmaro import __version__

        store = _MetaStore({"last_reported_server_version": __version__})
        text = await self._dispatch(self._manager(store))
        assert "server updated" not in text

    async def test_first_contact_writes_baseline_silently(self) -> None:
        from vesmaro import __version__

        store = _MetaStore()  # no meta yet — nothing to compare
        text = await self._dispatch(self._manager(store))
        assert "server updated" not in text
        assert store.meta["last_reported_server_version"] == __version__

    async def test_store_error_never_raises(self) -> None:
        store = _MetaStore({"last_reported_server_version": "0.0.1"})
        store.fail = True
        text = await self._dispatch(self._manager(store))
        assert "server updated" not in text  # skipped silently, no crash

    async def test_failed_dispatch_keeps_notice_pending(self) -> None:
        """#464 P3-1: a failed dispatch must not burn the notice — the
        meta is NOT stamped, the hint stays pending, and the NEXT
        dispatch delivers it (retry survives)."""
        from vesmaro import __version__
        from vesmaro.mcp_server import _call_tool_dispatch

        store = _MetaStore({"last_reported_server_version": "0.0.1"})
        mgr = self._manager(store)
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mgr),
            patch(
                "vesmaro.mcp_server._dispatch",
                side_effect=[RuntimeError("boom"), {"ok": True}],
            ),
        ):
            first = (await _call_tool_dispatch("mnemos_list_tags", {}))[0].text
            assert "❌ Error: boom" in first
            assert "server updated" not in first
            # the meta write happens only on DELIVERY — not burned
            assert store.meta["last_reported_server_version"] == "0.0.1"
            second = (await _call_tool_dispatch("mnemos_list_tags", {}))[0].text
            assert "vesma server updated: 0.0.1 → " in second
            assert __version__ in second
        # delivered exactly once, meta stamped on the success path
        assert store.meta["last_reported_server_version"] == __version__
        third = await self._dispatch(mgr)
        assert "server updated" not in third

    async def test_hint_pending_until_commit(self) -> None:
        """#464 P3-1, unit level: the hint is returned repeatedly until
        committed; the meta write happens only in the commit."""
        from vesmaro import __version__
        from vesmaro.mcp_server import _commit_server_update_hint, _server_update_hint

        store = _MetaStore({"last_reported_server_version": "0.0.1"})
        mgr = self._manager(store)
        first = _server_update_hint(mgr)
        assert first is not None and "0.0.1" in first
        assert store.meta["last_reported_server_version"] == "0.0.1"  # nothing stamped yet
        assert _server_update_hint(mgr) == first  # undelivered → retry survives
        _commit_server_update_hint(mgr)
        assert store.meta["last_reported_server_version"] == __version__
        assert _server_update_hint(mgr) is None  # delivered → quiet
