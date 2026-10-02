"""Tests for M3: first-class per-agent recall.

Covers:
  - SQLiteStore.list_recent_for_agent — basic, project filter, limit, recency sort
  - MemoryManager.agent_recall — without query (recency), with query (hybrid),
    project scope, exclusion of other agents, empty results, limit
  - Integration: agent_recall with query delegates to hybrid search correctly
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import AgentRecallQuery, Memory, MemoryCreate, MemoryStatus
from vesma.storage.sqlite_store import SQLiteStore

# ---------------------------------------------------------------------------
# Fixtures
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
    """Yield a MemoryManager with isolated storage."""
    mgr = MemoryManager(tmp_settings)
    yield mgr
    mgr.close()


@pytest.fixture
def tmp_sqlite(tmp_settings):
    """Yield a standalone SQLiteStore for direct SQL-layer tests."""
    store = SQLiteStore(tmp_settings.db_path)
    yield store
    store.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _memory_create(content: str, agent: str, project: str, **kwargs) -> MemoryCreate:
    """Build a MemoryCreate with valid Mnemos tag contract tags."""
    return MemoryCreate(
        content=content,
        tags=[f"project:{project}", f"agent:{agent}", "mnemos:learning"],
        **kwargs,
    )


def _memory_obj(content: str, agent: str, project: str, **kwargs) -> Memory:
    """Build a full Memory object for direct SQLiteStore use."""
    return Memory(
        content=content,
        tags=[f"project:{project}", f"agent:{agent}", "mnemos:learning"],
        project=project,
        agent=agent,
        **kwargs,
    )


def _add_via_manager(mgr: MemoryManager, content: str, agent: str, project: str, **kwargs):
    """Add a memory through MemoryManager (writes SQLite + vault)."""
    data = _memory_create(content, agent, project, **kwargs)
    return mgr.add(data, project=project, agent=agent)


# ---------------------------------------------------------------------------
# SQLiteStore.list_recent_for_agent — direct SQL layer
# ---------------------------------------------------------------------------


class TestListRecentForAgentDirect:
    def test_basic_filter_by_agent(self, tmp_sqlite):
        """Only entries for the requested agent are returned."""
        store = tmp_sqlite
        store.save(_memory_obj("alpha", "agent-a", "proj-x"))
        store.save(_memory_obj("beta", "agent-b", "proj-x"))
        store.save(_memory_obj("gamma", "agent-a", "proj-x"))

        results = store.list_recent_for_agent("agent-a", limit=10)
        assert len(results) == 2
        assert all(m.agent == "agent-a" for m in results)

    def test_project_scope(self, tmp_sqlite):
        """Project filter narrows results within the same agent."""
        store = tmp_sqlite
        store.save(_memory_obj("p1", "agent-a", "proj-1"))
        store.save(_memory_obj("p2", "agent-a", "proj-2"))
        store.save(_memory_obj("p3", "agent-a", "proj-1"))

        results = store.list_recent_for_agent("agent-a", project="proj-1", limit=10)
        assert len(results) == 2
        assert all(m.project == "proj-1" for m in results)

    def test_respects_limit(self, tmp_sqlite):
        """LIMIT is honoured even when more rows exist."""
        store = tmp_sqlite
        for i in range(5):
            store.save(_memory_obj(f"m{i}", "agent-a", "proj-x"))

        results = store.list_recent_for_agent("agent-a", limit=3)
        assert len(results) == 3

    def test_sorts_by_recency_desc(self, tmp_sqlite):
        """Most recently created entries come first."""
        store = tmp_sqlite
        store.save(_memory_obj("old", "agent-a", "proj-x"))
        store.save(_memory_obj("new", "agent-a", "proj-x"))

        results = store.list_recent_for_agent("agent-a", limit=10)
        assert [m.content for m in results] == ["new", "old"]

    def test_empty_result(self, tmp_sqlite):
        """No matches → empty list, not an error."""
        store = tmp_sqlite
        store.save(_memory_obj("only-b", "agent-b", "proj-x"))

        results = store.list_recent_for_agent("agent-a", limit=10)
        assert results == []


# ---------------------------------------------------------------------------
# MemoryManager.agent_recall — without query (recency mode)
# ---------------------------------------------------------------------------


class TestAgentRecallRecencyMode:
    def test_returns_recent_entries_for_agent(self, tmp_manager):
        """Without query, returns the most recent N entries for the agent."""
        mgr = tmp_manager
        _add_via_manager(mgr, "first", "reviewer", "mnemos")
        _add_via_manager(mgr, "second", "reviewer", "mnemos")
        _add_via_manager(mgr, "other", "architect", "mnemos")

        q = AgentRecallQuery(agent="reviewer", limit=10)
        results = mgr.agent_recall(q)

        assert len(results) == 2
        assert all(r.memory.agent == "reviewer" for r in results)
        assert results[0].memory.content == "second"
        assert results[0].search_type == "recency"

    def test_excludes_other_agents(self, tmp_manager):
        """Entries belonging to a different agent are not returned."""
        mgr = tmp_manager
        _add_via_manager(mgr, "r1", "reviewer", "mnemos")
        _add_via_manager(mgr, "a1", "architect", "mnemos")
        _add_via_manager(mgr, "r2", "reviewer", "mnemos")

        q = AgentRecallQuery(agent="reviewer", limit=10)
        results = mgr.agent_recall(q)

        contents = {r.memory.content for r in results}
        assert "a1" not in contents
        assert contents == {"r1", "r2"}

    def test_project_scope(self, tmp_manager):
        """Project filter further narrows the agent's recent entries."""
        mgr = tmp_manager
        _add_via_manager(mgr, "p-mnemos", "reviewer", "mnemos")
        _add_via_manager(mgr, "p-docs", "reviewer", "docs")
        _add_via_manager(mgr, "p-mnemos-2", "reviewer", "mnemos")

        q = AgentRecallQuery(agent="reviewer", project="mnemos", limit=10)
        results = mgr.agent_recall(q)

        assert len(results) == 2
        assert all(r.memory.project == "mnemos" for r in results)

    def test_respects_limit(self, tmp_manager):
        """Limit is passed through to the underlying list query."""
        mgr = tmp_manager
        for i in range(5):
            _add_via_manager(mgr, f"m{i}", "reviewer", "mnemos")

        q = AgentRecallQuery(agent="reviewer", limit=2)
        results = mgr.agent_recall(q)

        assert len(results) == 2

    def test_empty_result(self, tmp_manager):
        """No entries for the requested agent → empty list."""
        mgr = tmp_manager
        _add_via_manager(mgr, "only-architect", "architect", "mnemos")

        q = AgentRecallQuery(agent="reviewer", limit=10)
        results = mgr.agent_recall(q)

        assert results == []


# ---------------------------------------------------------------------------
# MemoryManager.agent_recall — with query (hybrid search delegation)
# ---------------------------------------------------------------------------


class TestAgentRecallWithQuery:
    def test_delegates_to_search_with_agent_filter(self, tmp_manager):
        """When query is provided, agent_recall calls search() with agent= set."""
        mgr = tmp_manager

        # Seed two memories — one published (so it can appear in vector search)
        m1 = _add_via_manager(
            mgr,
            "security vulnerability in auth module",
            "reviewer",
            "mnemos",
            status=MemoryStatus.PUBLISHED,
        )
        _add_via_manager(
            mgr,
            "refactor database schema",
            "architect",
            "mnemos",
            status=MemoryStatus.PUBLISHED,
        )

        # Manually upsert a dummy embedding so vector search can find it
        dummy_emb = [0.1] * 384
        mgr.vectors.upsert(m1.id, dummy_emb, {"project": "mnemos", "agent": "reviewer"})

        # Mock embedder so query embedding matches the dummy
        mgr._embedder = MagicMock()
        mgr._embedder.embed.return_value = dummy_emb

        q = AgentRecallQuery(agent="reviewer", query="security", limit=10)
        results = mgr.agent_recall(q)

        # Should find the reviewer entry, not the architect one
        assert any(r.memory.agent == "reviewer" for r in results)
        assert not any(r.memory.agent == "architect" for r in results)

    def test_query_with_project_and_agent(self, tmp_manager):
        """Both project and agent filters are applied during hybrid search."""
        mgr = tmp_manager

        m1 = _add_via_manager(
            mgr,
            "deploy pipeline fix",
            "reviewer",
            "mnemos",
            status=MemoryStatus.PUBLISHED,
        )
        _add_via_manager(
            mgr,
            "deploy docs update",
            "reviewer",
            "docs",
            status=MemoryStatus.PUBLISHED,
        )

        dummy_emb = [0.2] * 384
        mgr.vectors.upsert(m1.id, dummy_emb, {"project": "mnemos", "agent": "reviewer"})
        mgr._embedder = MagicMock()
        mgr._embedder.embed.return_value = dummy_emb

        q = AgentRecallQuery(agent="reviewer", project="mnemos", query="deploy", limit=10)
        results = mgr.agent_recall(q)

        assert all(r.memory.project == "mnemos" for r in results)
        assert all(r.memory.agent == "reviewer" for r in results)

    def test_no_false_positives_from_other_agents(self, tmp_manager):
        """Even if query text matches another agent, agent filter excludes it."""
        mgr = tmp_manager

        m_reviewer = _add_via_manager(
            mgr,
            "shared keyword alpha",
            "reviewer",
            "mnemos",
            status=MemoryStatus.PUBLISHED,
        )
        _add_via_manager(
            mgr,
            "shared keyword alpha",
            "architect",
            "mnemos",
            status=MemoryStatus.PUBLISHED,
        )

        dummy_emb = [0.3] * 384
        mgr.vectors.upsert(
            m_reviewer.id,
            dummy_emb,
            {"project": "mnemos", "agent": "reviewer"},
        )
        mgr._embedder = MagicMock()
        mgr._embedder.embed.return_value = dummy_emb

        q = AgentRecallQuery(agent="reviewer", query="shared keyword alpha", limit=10)
        results = mgr.agent_recall(q)

        assert all(r.memory.agent == "reviewer" for r in results)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestAgentRecallEdgeCases:
    def test_agent_slug_without_prefix(self, tmp_manager):
        """AgentRecallQuery.agent is the bare slug (no 'agent:' prefix)."""
        mgr = tmp_manager
        _add_via_manager(mgr, "content", "security-reviewer", "mnemos")

        q = AgentRecallQuery(agent="security-reviewer", limit=10)
        results = mgr.agent_recall(q)

        assert len(results) == 1
        assert results[0].memory.agent == "security-reviewer"

    def test_limit_zero(self, tmp_manager):
        """limit=0 should return an empty list gracefully."""
        mgr = tmp_manager
        _add_via_manager(mgr, "content", "reviewer", "mnemos")

        q = AgentRecallQuery(agent="reviewer", limit=0)
        results = mgr.agent_recall(q)

        assert results == []

    def test_nonexistent_project(self, tmp_manager):
        """Project that has no entries for the agent → empty list."""
        mgr = tmp_manager
        _add_via_manager(mgr, "content", "reviewer", "mnemos")

        q = AgentRecallQuery(agent="reviewer", project="nonexistent", limit=10)
        results = mgr.agent_recall(q)

        assert results == []
