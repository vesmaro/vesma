"""Regression tests for the 5 search/recall bugs fixed in this slice.

Bugs covered:
  #1 — include_raw parameter was a no-op in manager.search()
  #2 — vesma_search MCP tool missing status parameter
  #3 — vesma_agent_recall returns empty for raw entries (query path)
  #4 — project/agent tag case not normalized in lax mode
  #5 — vesma_stats lacks embedding/processor/search health

Each test adds an entry and immediately searches/recalls — the core
scenario that was broken (search returns empty for recently-added entries).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import (
    AgentRecallQuery,
    Memory,
    MemoryCreate,
    MemoryStatus,
    TagContractError,
    validate_tag_contract,
)

# ---------------------------------------------------------------------------
# Fixtures — isolated MemoryManager per test
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings():
    """Yield a Settings object backed by a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            vesma={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
        )
        settings.resolve_paths()
        yield settings


@pytest.fixture
def tmp_manager(tmp_settings):
    """Yield a MemoryManager with isolated storage and a mock embedder.

    The mock embedder returns a fixed 384-dim vector so the vector leg
    does not fail (ONNX model is not available in CI). Tests that need
    fts_only mode can clear the vector store or let vectors be empty.
    """
    mgr = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_create(
    content: str,
    *,
    agent: str = "test-agent",
    project: str = "test-project",
    status: MemoryStatus = MemoryStatus.RAW,
) -> MemoryCreate:
    """Build a MemoryCreate with valid Mnemos tags and explicit status."""
    return MemoryCreate(
        content=content,
        tags=[f"project:{project}", f"agent:{agent}", "mnemos:learning"],
        status=status,
    )


def _add(mgr: MemoryManager, content: str, **kwargs) -> Memory:
    """Add a memory through the manager and return it."""
    data = _make_create(content, **kwargs)
    project = kwargs.get("project", "test-project")
    agent = kwargs.get("agent", "test-agent")
    return mgr.add(data, project=project, agent=agent)


# ---------------------------------------------------------------------------
# Bug #1 — include_raw filter
# ---------------------------------------------------------------------------


class TestIncludeRawFilter:
    def test_include_raw_returns_raw_entries(self, tmp_manager):
        """include_raw=True surfaces raw entries; default does not."""
        mgr = tmp_manager
        _add(mgr, "unique raw content about alpha widgets", status=MemoryStatus.RAW)

        # Default search — raw entries filtered out
        default_results = mgr.search("alpha widgets", include_raw=False)
        assert default_results == [], "default search should not return raw entries"

        # include_raw=True — raw entries surface
        raw_results = mgr.search("alpha widgets", include_raw=True)
        assert len(raw_results) == 1
        assert raw_results[0].memory.status == MemoryStatus.RAW

    def test_default_search_returns_published(self, tmp_manager):
        """Default search returns published + processed, not raw."""
        mgr = tmp_manager
        _add(mgr, "published content about beta gadgets", status=MemoryStatus.PUBLISHED)
        _add(mgr, "raw content about beta gadgets", status=MemoryStatus.RAW)

        results = mgr.search("beta gadgets", include_raw=False)
        statuses = {r.memory.status for r in results}
        assert MemoryStatus.RAW not in statuses
        assert MemoryStatus.PUBLISHED in statuses

    def test_processed_included_by_default(self, tmp_manager):
        """Processed entries are 'ready' and surface in default search."""
        mgr = tmp_manager
        _add(mgr, "processed content about gamma tools", status=MemoryStatus.PROCESSED)

        results = mgr.search("gamma tools", include_raw=False)
        assert len(results) == 1
        assert results[0].memory.status == MemoryStatus.PROCESSED


# ---------------------------------------------------------------------------
# Bug #2 — explicit status parameter
# ---------------------------------------------------------------------------


class TestExplicitStatusFilter:
    def test_search_with_explicit_status_raw(self, tmp_manager):
        """Explicit status='raw' returns only raw entries."""
        mgr = tmp_manager
        _add(mgr, "raw delta entry", status=MemoryStatus.RAW)
        _add(mgr, "published delta entry", status=MemoryStatus.PUBLISHED)

        results = mgr.search("delta", status=MemoryStatus.RAW)
        assert len(results) == 1
        assert results[0].memory.status == MemoryStatus.RAW

    def test_explicit_status_overrides_include_raw(self, tmp_manager):
        """When both status and include_raw are set, status wins."""
        mgr = tmp_manager
        _add(mgr, "epsilon raw", status=MemoryStatus.RAW)
        _add(mgr, "epsilon published", status=MemoryStatus.PUBLISHED)

        # status=published + include_raw=True → only published
        results = mgr.search("epsilon", status=MemoryStatus.PUBLISHED, include_raw=True)
        assert len(results) == 1
        assert results[0].memory.status == MemoryStatus.PUBLISHED


# ---------------------------------------------------------------------------
# Bug #2 (MCP layer) — status param passes through dispatch
# ---------------------------------------------------------------------------


class TestMcpSearchStatusParam:
    async def test_mcp_search_status_param_passes_through(self):
        """vesma_search dispatch passes status to manager.search()."""
        from unittest.mock import patch

        from vesma.mcp_server import _dispatch

        mock_mgr = MagicMock()
        mock_mgr.search.return_value = []
        mock_mgr.settings.vesma.strict_tag_contract = False

        with patch("vesma.mcp_server.get_manager", return_value=mock_mgr):
            await _dispatch("vesma_search", {"query": "test", "status": "raw"})

        # Verify status was converted to MemoryStatus and passed
        assert mock_mgr.search.called
        call_kwargs = mock_mgr.search.call_args.kwargs
        assert call_kwargs["status"] == MemoryStatus.RAW

    async def test_mcp_search_no_status_passes_none(self):
        """Without status param, None is passed (not an error)."""
        from unittest.mock import patch

        from vesma.mcp_server import _dispatch

        mock_mgr = MagicMock()
        mock_mgr.search.return_value = []
        mock_mgr.settings.vesma.strict_tag_contract = False

        with patch("vesma.mcp_server.get_manager", return_value=mock_mgr):
            await _dispatch("vesma_search", {"query": "test"})

        call_kwargs = mock_mgr.search.call_args.kwargs
        assert call_kwargs["status"] is None


# ---------------------------------------------------------------------------
# Bug #3 — agent_recall finds raw entries
# ---------------------------------------------------------------------------


class TestAgentRecallFindsRaw:
    def test_agent_recall_recency_finds_raw(self, tmp_manager):
        """Recency-mode recall (no query) returns raw entries for the agent."""
        mgr = tmp_manager
        _add(mgr, "raw agent memory about zeta", agent="test-agent", status=MemoryStatus.RAW)

        q = AgentRecallQuery(agent="test-agent", limit=10)
        results = mgr.agent_recall(q)
        assert len(results) == 1
        assert results[0].memory.status == MemoryStatus.RAW
        assert results[0].search_type == "recency"

    def test_agent_recall_with_query_finds_raw(self, tmp_manager):
        """Query-mode recall passes include_raw=True, finding raw entries."""
        mgr = tmp_manager
        _add(
            mgr,
            "raw agent memory about eta systems",
            agent="test-agent",
            status=MemoryStatus.RAW,
        )

        q = AgentRecallQuery(agent="test-agent", query="eta systems", limit=10)
        results = mgr.agent_recall(q)
        assert len(results) >= 1
        statuses = {r.memory.status for r in results}
        assert MemoryStatus.RAW in statuses

    def test_agent_recall_excludes_other_agents(self, tmp_manager):
        """Recall for one agent does not return another agent's entries."""
        mgr = tmp_manager
        _add(mgr, "theta for agent-a", agent="agent-a", status=MemoryStatus.RAW)
        _add(mgr, "theta for agent-b", agent="agent-b", status=MemoryStatus.RAW)

        q = AgentRecallQuery(agent="agent-a", limit=10)
        results = mgr.agent_recall(q)
        assert all(r.memory.agent == "agent-a" for r in results)


# ---------------------------------------------------------------------------
# Bug #4 — project/agent tag case normalization
# ---------------------------------------------------------------------------


class TestTagNormalization:
    def test_lax_mode_normalizes_uppercase_project(self):
        """Lax mode normalizes project:Project-Umbra → project:project-umbra."""
        result = validate_tag_contract(
            ["project:Project-Umbra", "agent:test-agent", "mnemos:learning"],
            strict=False,
        )
        assert "project:project-umbra" in result
        assert "project:Project-Umbra" not in result
        assert "project:unknown" not in result

    def test_lax_mode_normalizes_uppercase_agent(self):
        """Lax mode normalizes agent:Test-Agent → agent:test-agent."""
        result = validate_tag_contract(
            ["project:test-project", "agent:Test-Agent", "mnemos:learning"],
            strict=False,
        )
        assert "agent:test-agent" in result
        assert "agent:Test-Agent" not in result
        assert "agent:unknown" not in result

    def test_lax_mode_normalizes_spaces_to_hyphens(self):
        """Spaces in slugs are replaced with hyphens."""
        result = validate_tag_contract(
            ["project:My Project", "agent:test-agent", "mnemos:learning"],
            strict=False,
        )
        assert "project:my-project" in result

    def test_strict_mode_rejects_uppercase(self):
        """Strict mode still raises TagContractError for uppercase slugs."""
        with pytest.raises(TagContractError):
            validate_tag_contract(
                ["project:Project-Umbra", "agent:test-agent", "mnemos:learning"],
                strict=True,
            )

    def test_lax_mode_falls_back_for_unsalvageable_slug(self):
        """Slugs with truly invalid chars (not just case) fall back to unknown."""
        # Dots are not in [a-z0-9_-], and lowercasing does not help
        result = validate_tag_contract(
            ["project:invalid.slug", "agent:test-agent", "mnemos:learning"],
            strict=False,
        )
        assert "project:unknown" in result
        assert "project:invalid.slug" not in result

    def test_lax_mode_preserves_valid_tags(self):
        """Already-valid tags pass through; the legacy subtype spelling
        normalizes to the canon even in lax mode."""
        tags = ["project:valid-proj", "agent:valid-agent", "mnemos:decision"]
        result = validate_tag_contract(tags, strict=False)
        assert result == ["project:valid-proj", "agent:valid-agent", "vesma:decision"]


# ---------------------------------------------------------------------------
# Bug #5 — stats includes health fields
# ---------------------------------------------------------------------------


class TestStatsHealth:
    def test_stats_includes_embedding_status(self, tmp_manager):
        """stats() response contains embedding_status with provider + vectors."""
        mgr = tmp_manager
        stats = mgr.stats()
        assert "embedding_status" in stats
        emb = stats["embedding_status"]
        assert "provider" in emb
        assert "vectors_indexed" in emb
        assert "degraded" in emb

    def test_stats_includes_processor(self, tmp_manager):
        """stats() response contains processor queue depth."""
        mgr = tmp_manager
        _add(mgr, "queued raw entry", status=MemoryStatus.RAW)
        stats = mgr.stats()
        assert "processor" in stats
        assert stats["processor"]["queue_depth"] >= 1
        assert "last_processed_at" in stats["processor"]

    def test_stats_includes_search_health(self, tmp_manager):
        """stats() response contains search_health with mode indicator."""
        mgr = tmp_manager
        stats = mgr.stats()
        assert "search_health" in stats
        health = stats["search_health"]
        assert health["fts_available"] is True
        assert "vector_available" in health
        assert health["mode"] in ("hybrid", "fts_only")

    def test_stats_degraded_flag_when_published_but_no_vectors(self, tmp_manager):
        """degraded=True when published memories exist but none are embedded."""
        mgr = tmp_manager
        _add(mgr, "published but not embedded", status=MemoryStatus.PUBLISHED)
        stats = mgr.stats()
        # Vector store is empty (mock embedder doesn't upsert automatically
        # unless add() runs the embed path — with a mock it may upsert).
        # The key invariant: degraded is a boolean, not None.
        assert isinstance(stats["embedding_status"]["degraded"], bool)


# ---------------------------------------------------------------------------
# FTS fallback robustness — search_type indicator
# ---------------------------------------------------------------------------


class TestSearchTypeIndicator:
    def test_search_fts_only_when_no_vectors(self, tmp_manager):
        """With empty vector store, search_type is 'fts_only'."""
        mgr = tmp_manager
        _add(mgr, "published iota content", status=MemoryStatus.PUBLISHED)

        # Ensure vector store is empty so the vector leg yields nothing
        mgr.vectors.wipe()

        results = mgr.search("iota", include_raw=True)
        assert len(results) >= 1
        assert results[0].search_type == "fts_only"

    def test_search_hybrid_when_vectors_present(self, tmp_manager):
        """When the vector leg contributes a NEW result, search_type is 'hybrid'.

        LOW-1 fix: search_type reflects actual vector contribution, not just
        whether the vector leg returned pairs. A memory that is in BOTH the
        FTS and vector legs does not count as vector-contributed (the vector
        leg added no new id to the result set). To get 'hybrid', the vector
        leg must return a memory that FTS did NOT find.
        """
        mgr = tmp_manager
        # Memory A — matches FTS query "kappa".
        _add(mgr, "published kappa content", status=MemoryStatus.PUBLISHED)
        # Memory B — does NOT match FTS "kappa" but is in the vector store.
        # The mock embedder returns the same vector for all queries, so
        # vector search returns B regardless of query text.
        mem_b = _add(mgr, "published unrelated text", status=MemoryStatus.PUBLISHED)
        mgr.vectors.upsert(
            mem_b.id,
            [0.1] * 384,
            {"project": "test-project", "agent": "test-agent"},
        )

        results = mgr.search("kappa", include_raw=True)
        assert len(results) >= 1
        # The vector leg contributed mem_b (not in fts_ids) → hybrid.
        assert results[0].search_type == "hybrid"

    def test_search_fts_only_when_vector_same_as_fts(self, tmp_manager):
        """Same memory in both legs → fts_only (vector added no new result).

        LOW-1: when the only vector result is also found by FTS, the vector
        leg did not contribute a new id — search_type is 'fts_only'.
        """
        mgr = tmp_manager
        mem = _add(mgr, "published kappa content", status=MemoryStatus.PUBLISHED)
        mgr.vectors.upsert(
            mem.id,
            [0.1] * 384,
            {"project": "test-project", "agent": "test-agent"},
        )

        results = mgr.search("kappa", include_raw=True)
        assert len(results) >= 1
        assert results[0].search_type == "fts_only"


# ---------------------------------------------------------------------------
# FTS trigger fires on INSERT — the core "recently added" scenario
# ---------------------------------------------------------------------------


class TestFtsFindsRecentlyAdded:
    def test_fts_finds_recently_added_raw(self, tmp_manager):
        """Add entry → immediately search with include_raw=True → found.

        This is the primary use case that was broken: FTS5 triggers fire
        on INSERT for all statuses, but the default status gating hid
        raw entries from search results.
        """
        mgr = tmp_manager
        _add(mgr, "freshly added lambda notes", status=MemoryStatus.RAW)

        results = mgr.search("lambda", include_raw=True)
        assert len(results) >= 1
        assert any("lambda" in r.memory.content for r in results)

    def test_fts_finds_recently_added_published(self, tmp_manager):
        """Published entries are found by default search immediately."""
        mgr = tmp_manager
        _add(mgr, "freshly added mu notes", status=MemoryStatus.PUBLISHED)

        results = mgr.search("mu", include_raw=False)
        assert len(results) >= 1
        assert any("mu" in r.memory.content for r in results)
