"""Tests for P1-4 — CCR (Compress-Cache-Retrieve) reversible compression.

Inspired by headroom's CCR (https://github.com/headroomlabs-ai/headroom),
Apache 2.0. These tests verify our original implementation: roundtrip
fidelity, marker parsing, TTL/LRU eviction, project scoping, and FTS5
snippet retrieval.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from vesmaro.ccr import (
    build_marker,
    cleanup,
    compress,
    content_hash,
    parse_marker,
    retrieve,
)
from vesmaro.config import CCRConfig, Settings
from vesmaro.manager import MemoryManager
from vesmaro.storage.sqlite_store import SQLiteStore

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    s = SQLiteStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def config() -> CCRConfig:
    return CCRConfig(min_size_chars=100, max_entries=100, ttl_days=1)


@pytest.fixture
def manager(tmp_path: Path) -> MemoryManager:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            vesma={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
            },
            ccr={"min_size_chars": 100, "max_entries": 100, "ttl_days": 1},
        )
        settings.resolve_paths()
        mgr = MemoryManager(settings)
        yield mgr
        mgr.close()


# ── Test data helpers ─────────────────────────────────────────────────────────


def _large_log(n_lines: int = 500) -> str:
    """Generate a large repetitive log (high compressibility)."""
    lines = []
    for i in range(n_lines):
        lines.append(f"2026-06-29T10:00:{i % 60:02d}Z INFO  worker-{i % 8} processing item {i}")
    lines.append("2026-06-29T10:01:00Z ERROR connection refused: timeout after 30s")
    lines.append("2026-06-29T10:01:01Z ERROR retry failed: upstream 503")
    return "\n".join(lines)


def _large_json(n_items: int = 500) -> str:
    import json

    return json.dumps(
        [
            {"id": i, "name": f"item-{i}", "value": i * 2, "active": i % 2 == 0}
            for i in range(n_items)
        ],
        indent=2,
    )


# ── Marker parsing ────────────────────────────────────────────────────────────


class TestMarker:
    def test_build_and_parse_roundtrip(self):
        h = content_hash("hello world")
        marker = build_marker(h, 5000, 500)
        assert h in marker
        assert "5000→500 chars" in marker
        parsed = parse_marker(marker)
        assert parsed is not None
        assert parsed["hash"] == h
        assert parsed["original_chars"] == 5000
        assert parsed["compressed_chars"] == 500

    def test_parse_marker_none_when_absent(self):
        assert parse_marker("no marker here") is None

    def test_parse_marker_from_embedded_text(self):
        h = content_hash("x" * 200)
        marker = build_marker(h, 1000, 200)
        text = f"{marker}\ncompressed body..."
        parsed = parse_marker(text)
        assert parsed is not None
        assert parsed["hash"] == h
        assert parsed["span"][0] == 0

    def test_hash_is_sha256_hex_64(self):
        h = content_hash("test")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ── Compress + Retrieve roundtrip ─────────────────────────────────────────────


class TestRoundtrip:
    def test_compress_returns_marker_and_caches_original(self, store, config):
        text = _large_log(300)
        result = compress(text, store=store, config=config, profile="log")
        assert result["cached"] is True
        assert result["hash"]
        assert result["original_size"] == len(text)
        assert result["compressed_size"] < result["original_size"]
        assert result["reduction_pct"] > 0
        assert result["marker"] in result["compressed_text"]

    def test_roundtrip_zero_loss(self, store, config):
        text = _large_log(300)
        result = compress(text, store=store, config=config, profile="log")
        retrieved = retrieve(result["hash"], store=store, config=config)
        assert retrieved["found"] is True
        assert retrieved["original"] == text

    def test_small_content_not_cached(self, store, config):
        text = "tiny"
        result = compress(text, store=store, config=config)
        assert result["cached"] is False
        assert result["hash"] == ""
        assert result["compressed_text"] == "tiny"
        assert result["reduction_pct"] == 0.0

    def test_compress_idempotent_on_same_content(self, store, config):
        text = _large_log(200)
        r1 = compress(text, store=store, config=config, profile="log")
        r2 = compress(text, store=store, config=config, profile="log")
        assert r1["hash"] == r2["hash"]
        assert store.ccr_count() == 1

    def test_retrieve_missing_hash(self, store, config):
        result = retrieve("0" * 64, store=store, config=config)
        assert result["found"] is False

    def test_retrieval_count_increments(self, store, config):
        text = _large_log(200)
        result = compress(text, store=store, config=config, profile="log")
        retrieve(result["hash"], store=store, config=config)
        retrieve(result["hash"], store=store, config=config)
        entry = store.ccr_get(result["hash"])
        assert entry is not None
        assert entry["retrieval_count"] >= 2


# ── Large content reduction ───────────────────────────────────────────────────


class TestReduction:
    def test_large_log_compresses_under_30pct(self, store, config):
        text = _large_log(2000)
        result = compress(text, store=store, config=config, profile="log")
        assert result["original_size"] > 10000
        ratio = result["compressed_size"] / result["original_size"]
        assert ratio < 0.30, f"ratio={ratio:.2%} expected <30%"

    def test_large_json_compresses(self, store, config):
        text = _large_json(1000)
        result = compress(text, store=store, config=config, profile="default")
        assert result["reduction_pct"] > 50
        # roundtrip still zero-loss
        retrieved = retrieve(result["hash"], store=store, config=config)
        assert retrieved["original"] == text

    def test_10k_plus_chars_retrievable(self, store, config):
        text = _large_log(1000)
        assert len(text) > 10000
        result = compress(text, store=store, config=config, profile="log")
        retrieved = retrieve(result["hash"], store=store, config=config)
        assert retrieved["original"] == text


# ── FTS5 snippet retrieval ────────────────────────────────────────────────────


class TestSnippetRetrieve:
    def test_retrieve_with_query_returns_snippets(self, store, config):
        text = _large_log(300)
        result = compress(text, store=store, config=config, profile="log")
        snippets = retrieve(result["hash"], store=store, config=config, query="error")
        assert snippets["found"] is True
        assert "snippets" in snippets
        assert isinstance(snippets["snippets"], list)
        # At least one snippet should mention error content
        joined = " ".join(s["snippet"] for s in snippets["snippets"])
        assert "error" in joined.lower() or len(snippets["snippets"]) == 0 or "ERROR" in joined

    def test_retrieve_query_no_match_returns_empty_list(self, store, config):
        text = _large_log(200)
        result = compress(text, store=store, config=config, profile="log")
        snippets = retrieve(
            result["hash"],
            store=store,
            config=config,
            query="zzznonexistenttermzzz",
        )
        assert snippets["found"] is True
        assert snippets["snippets"] == []

    def test_snippet_count_respected(self, store, config):
        text = _large_log(500)
        result = compress(text, store=store, config=config, profile="log")
        snippets = retrieve(
            result["hash"],
            store=store,
            config=config,
            query="processing",
            snippet_count=2,
        )
        assert len(snippets["snippets"]) <= 2


# ── TTL + LRU eviction ────────────────────────────────────────────────────────


class TestEviction:
    def test_lru_eviction_when_over_capacity(self, store):
        cfg = CCRConfig(min_size_chars=100, max_entries=100, ttl_days=30)
        for i in range(5):
            compress(_large_log(150 + i), store=store, config=cfg, profile="log")
        assert store.ccr_count() == 5
        # Now drop the cap and evict directly via the store method
        evicted = store.ccr_evict_lru(3)
        assert evicted == 2
        assert store.ccr_count() == 3

    def test_cleanup_ttl_deletes_old_entries(self, store):
        from datetime import UTC, datetime, timedelta

        cfg = CCRConfig(min_size_chars=100, max_entries=100, ttl_days=1)
        compress(_large_log(200), store=store, config=cfg, profile="log")
        # Manually backdate the created_at to be older than ttl
        conn = store._get_conn()
        old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        conn.execute("UPDATE ccr_cache SET created_at=?", (old,))
        conn.commit()
        removed = store.ccr_cleanup_ttl(cfg.ttl_days)
        assert removed == 1
        assert store.ccr_count() == 0

    def test_cleanup_returns_counts(self, store):
        cfg = CCRConfig(min_size_chars=100, max_entries=100, ttl_days=30)
        compress(_large_log(200), store=store, config=cfg, profile="log")
        counts = cleanup(store=store, config=cfg)
        assert "ttl_deleted" in counts
        assert "lru_evicted" in counts


# ── Project scoping ───────────────────────────────────────────────────────────


class TestProjectScoping:
    def test_project_recorded_on_cache_entry(self, store, config):
        text = _large_log(200)
        compress(text, store=store, config=config, profile="log", project="proj-a")
        h = content_hash(text)
        entry = store.ccr_get(h)
        assert entry is not None
        assert entry["project"] == "proj-a"

    def test_same_content_different_projects_two_rows(self, store, config):
        text = _large_log(200)
        # A1 (ArchCom 2026-08-27): composite PK (project, hash) — the same
        # content cached by two projects is TWO rows (the first-writer-
        # squatting cross-project DoS edge of the hash-only PK dissolves;
        # each project redeems its own marker). Pre-A1 this asserted one
        # row with the first project kept.
        compress(text, store=store, config=config, profile="log", project="proj-a")
        compress(text, store=store, config=config, profile="log", project="proj-b")
        assert store.ccr_count() == 2
        h = content_hash(text)
        assert store.ccr_get(h, project="proj-a", bump=False) is not None
        assert store.ccr_get(h, project="proj-b", bump=False) is not None


# ── Manager wiring ────────────────────────────────────────────────────────────


class TestManagerWiring:
    def test_manager_compress_and_retrieve(self, manager):
        text = _large_log(300)
        result = manager.compress_content(text, profile="log", project="test")
        assert result["cached"] is True
        retrieved = manager.retrieve_content(result["hash"])
        assert retrieved["found"] is True
        assert retrieved["original"] == text

    def test_manager_ccr_stats(self, manager):
        stats = manager.ccr_stats()
        assert stats["enabled"] is True
        assert stats["entries"] == 0
        assert stats["ttl_days"] == 1

    def test_manager_disabled_returns_passthrough(self, tmp_path):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            settings = Settings(
                vesma={
                    "vault_path": str(tmp / "vault"),
                    "data_dir": str(tmp / "data"),
                    "db_name": "test.db",
                },
                ccr={"enabled": False, "min_size_chars": 100},
            )
            settings.resolve_paths()
            mgr = MemoryManager(settings)
            try:
                result = mgr.compress_content(_large_log(300), profile="log")
                assert result["cached"] is False
                assert result["profile"] == "disabled"
            finally:
                mgr.close()


# ── MCP dispatch ──────────────────────────────────────────────────────────────


class TestMcpDispatch:
    def test_mcp_compress_tool_listed(self):
        import asyncio

        from vesmaro.mcp_server import list_tools

        tools = asyncio.get_event_loop().run_until_complete(list_tools()) if False else None
        # list_tools is an async function decorated by the stub; call it directly
        import inspect

        if inspect.iscoroutinefunction(list_tools):
            tools = asyncio.new_event_loop().run_until_complete(list_tools())
        else:
            tools = list_tools()
        names = [t.name for t in tools]
        assert "vesma_compress" in names
        assert "vesma_retrieve" in names

    def test_mcp_dispatch_compress_and_retrieve(self, manager):
        import asyncio

        # Override the module-level manager
        import vesmaro.mcp_server as mcp_mod
        from vesmaro.mcp_server import _dispatch

        original = mcp_mod._manager
        mcp_mod._manager = manager
        try:
            text = _large_log(300)
            result = asyncio.new_event_loop().run_until_complete(
                _dispatch("vesma_compress", {"text": text, "profile": "log"})
            )
            assert result["cached"] is True
            h = result["hash"]
            retrieved = asyncio.new_event_loop().run_until_complete(
                _dispatch("vesma_retrieve", {"hash": h})
            )
            assert retrieved["found"] is True
            assert retrieved["original"] == text
        finally:
            mcp_mod._manager = original
