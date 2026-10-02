"""Tests for HTTP API endpoints.

Covers:
  - Health / metrics
  - Memories CRUD (create, get, list)
  - Search
  - Per-agent recall
  - Pipeline (process, synthesize, publish)
  - DLQ (list, retry, discard)
  - Traces
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vesmaro.api import main as api_main
from vesmaro.api.main import app, lifespan
from vesmaro.config import Settings
from vesmaro.manager import MemoryManager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            vesma={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
            # Scanner is exercised in test_scanner.py — disable it here so
            # the API lifespan does not spawn a daemon thread per test
            # (defence-in-depth against the singleton thread leak).
            scanner={"enabled": False},
        )
        settings.resolve_paths()
        yield settings


@pytest.fixture
def client(tmp_settings):
    """Yield a TestClient with an isolated MemoryManager per test."""
    mgr = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder

    # Build a fresh FastAPI app so lifespan is isolated per test

    test_app = FastAPI(
        title="Mnemos-Test",
        version="0.1.0",
        lifespan=lifespan,
    )
    # Copy all routes from the real app
    for route in app.routes:
        test_app.routes.append(route)

    # Override get_manager to return our isolated mgr

    api_main._manager = mgr
    with TestClient(test_app) as tc:
        yield tc
    mgr.close()
    api_main._manager = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_metrics(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "by_status" in data


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------


class TestMemories:
    def test_create_memory(self, client):
        resp = client.post(
            "/memories",
            json={
                "content": "Test memory",
                "tags": ["project:mnemos", "agent:reviewer", "mnemos:learning"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["content"] == "Test memory"
        assert "project:mnemos" in data["tags"]

    def test_get_memory(self, client):
        # Create first
        create_resp = client.post(
            "/memories",
            json={
                "content": "Fetch me",
                "tags": ["project:mnemos", "agent:reviewer", "mnemos:learning"],
            },
        )
        mem_id = create_resp.json()["id"]

        resp = client.get(f"/memories/{mem_id}")
        assert resp.status_code == 200
        assert resp.json()["content"] == "Fetch me"

    def test_get_memory_404(self, client):
        resp = client.get("/memories/nonexistent-id")
        assert resp.status_code == 404

    def test_list_memories(self, client):
        client.post(
            "/memories",
            json={
                "content": "One",
                "tags": ["project:mnemos", "agent:reviewer", "mnemos:learning"],
            },
        )
        client.post(
            "/memories",
            json={
                "content": "Two",
                "tags": ["project:mnemos", "agent:reviewer", "mnemos:learning"],
            },
        )

        resp = client.get("/memories?limit=10")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_list_memories_by_status(self, client):
        client.post(
            "/memories",
            json={
                "content": "Raw note",
                "tags": ["project:mnemos", "agent:reviewer", "mnemos:learning"],
                "status": "raw",
            },
        )
        resp = client.get("/memories?status=raw")
        assert resp.status_code == 200
        assert all(m["status"] == "raw" for m in resp.json())


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_basic(self, client):
        client.post(
            "/memories",
            json={
                "content": "kubernetes deployment patterns",
                "tags": ["project:mnemos", "agent:reviewer", "mnemos:learning"],
            },
        )
        resp = client.post(
            "/search",
            json={
                "query": "kubernetes",
                "limit": 10,
                "include_raw": True,
            },
        )
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1
        assert any("kubernetes" in r["content"].lower() for r in results)


# ---------------------------------------------------------------------------
# Tag filter exact matching (finding: LIKE → json_each)
# ---------------------------------------------------------------------------


class TestTagFilterExactMatch:
    """Tag filtering must match exact tag values, not substrings.

    Regression for the ``LIKE '%"tag"%'`` → ``json_each()`` fix: searching
    for ``project:mnemos`` must NOT match ``project:mnemos-eyes``.
    """

    def test_tag_filter_excludes_substring_match(self, client):
        """``project:mnemos`` filter does not match ``project:mnemos-eyes``."""
        client.post(
            "/memories",
            json={
                "content": "mnemos backend memory",
                "tags": ["project:mnemos", "agent:backend", "mnemos:learning"],
            },
        )
        client.post(
            "/memories",
            json={
                "content": "mnemos eyes frontend memory",
                "tags": ["project:mnemos-eyes", "agent:frontend", "mnemos:learning"],
            },
        )

        # Filter by project:mnemos — must return only the exact match.
        resp = client.get("/memories?tags=project:mnemos")
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) == 1
        assert "project:mnemos" in results[0]["tags"]
        assert "project:mnemos-eyes" not in results[0]["tags"]

    def test_tag_filter_exact_multiple_tags(self, client):
        """Multiple tag filters use AND logic with exact matching."""
        client.post(
            "/memories",
            json={
                "content": "memory with both tags",
                "tags": ["project:mnemos", "agent:backend", "mnemos:learning"],
            },
        )
        client.post(
            "/memories",
            json={
                "content": "memory with only one tag",
                "tags": ["project:mnemos", "agent:frontend", "mnemos:learning"],
            },
        )

        # Filter by project:mnemos AND agent:backend — only the first matches.
        resp = client.get("/memories?tags=project:mnemos,agent:backend")
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) == 1
        assert "agent:backend" in results[0]["tags"]


# ---------------------------------------------------------------------------
# Vector search status filter (finding: vector leg skips status filter)
# ---------------------------------------------------------------------------


class TestVectorSearchStatusFilter:
    """Vector search results must be filtered by the requested status.

    Regression for the vector-leg status filter fix: a non-published
    memory that somehow enters the vector store must not appear in
    search results when ``status=published`` is requested.
    """

    def test_search_status_excludes_non_published(self, client):
        """Search with status=published excludes raw memories."""
        # Published memory — enters the vector store on save.
        client.post(
            "/memories",
            json={
                "content": "published kubernetes note",
                "tags": ["project:mnemos", "agent:backend", "mnemos:learning"],
                "status": "published",
            },
        )
        # Raw memory — should NOT be in the vector store, but if it is,
        # the status filter must still exclude it from results.
        client.post(
            "/memories",
            json={
                "content": "raw kubernetes note",
                "tags": ["project:mnemos", "agent:backend", "mnemos:learning"],
                "status": "raw",
            },
        )

        # Search with status=published — must return only the published memory.
        resp = client.post(
            "/search",
            json={
                "query": "kubernetes",
                "status": "published",
                "limit": 10,
            },
        )
        assert resp.status_code == 200
        results = resp.json()
        # All results must be published.
        assert all(r["status"] == "published" for r in results)
        # The published memory must be present.
        assert any("published" in r["content"] for r in results)
        # The raw memory must NOT be present.
        assert all("raw" not in r["content"] for r in results)


# ---------------------------------------------------------------------------
# Per-agent recall
# ---------------------------------------------------------------------------


class TestAgentRecall:
    def test_recall_by_agent(self, client):
        client.post(
            "/memories",
            json={
                "content": "Security review note",
                "tags": ["project:mnemos", "agent:security-reviewer", "mnemos:learning"],
            },
        )
        resp = client.get("/recall/agent/security-reviewer?limit=10")
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1
        assert all("agent:security-reviewer" in r["tags"] for r in results)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class TestPipeline:
    def test_process_empty(self, client):
        """Process with no raw memories returns zero counts."""
        resp = client.post("/process")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["clusters"] == 0

    def test_synthesize_missing_cluster(self, client):
        resp = client.post("/synthesize?cluster_id=fake-id")
        assert resp.status_code == 404

    def test_publish_missing_memory(self, client):
        resp = client.post("/publish/nonexistent-id")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# DLQ
# ---------------------------------------------------------------------------


class TestDLQ:
    def test_list_empty(self, client):
        resp = client.get("/dlq")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_discard_missing(self, client):
        resp = client.delete("/dlq/fake-id")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Traces
# ---------------------------------------------------------------------------


class TestTraces:
    def test_list_empty(self, client):
        resp = client.get("/traces")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# Path-scoped rules ingest (M8)
# ---------------------------------------------------------------------------


class TestRulesIngest:
    def test_ingest_rules(self, client, tmp_path):
        # Create a temporary .instructions.md file
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        rule_file = rules_dir / "test.instructions.md"
        rule_file.write_text("---\napplyTo: '**'\n---\n# Test Rule\nbody")

        resp = client.post(
            "/rules/ingest",
            json={
                "rules_dir": str(rules_dir),
                "project": "test",
                "agent": "api-test",
                "pattern": "*.instructions.md",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["processed"] == 1

    def test_remove_rule(self, client, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        rule_file = rules_dir / "removable.instructions.md"
        rule_file.write_text("---\napplyTo: '**'\n---\n# Removable\nbody")

        # First ingest
        client.post(
            "/rules/ingest",
            json={
                "rules_dir": str(rules_dir),
                "project": "test",
                "agent": "api-test",
            },
        )

        # Then remove
        resp = client.request(
            "DELETE",
            "/rules/ingest",
            json={
                "file_path": str(rule_file),
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "removed"

    def test_remove_missing_rule(self, client):
        resp = client.request(
            "DELETE",
            "/rules/ingest",
            json={
                "file_path": "/nonexistent/path.rule.md",
            },
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Context Filter (M10)
# ---------------------------------------------------------------------------


class TestContextFilter:
    def test_filter_missing_memory(self, client):
        resp = client.post("/filter/nonexistent-id", json={})
        assert resp.status_code == 404

    def test_filter_applies(self, client):
        # Create a memory with raw_content
        resp = client.post(
            "/memories",
            json={
                "content": "Line 1\nLine 2\nLine 3",
                "title": "Test",
                "tags": ["project:test", "agent:api-test", "mnemos:learning"],
                "source": "cli",
            },
        )
        assert resp.status_code == 201
        mem_id = resp.json()["id"]

        # M1 (final review): /filter is issuance-gated — publish the memory
        # first (raw is not filterable into context).
        resp = client.post(f"/publish/{mem_id}?skip_quality_check=true")
        assert resp.status_code == 200

        # Apply filter
        resp = client.post(f"/filter/{mem_id}", json={"profile": "default"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "clean_content" in data
        assert "filter_profile" in data


# ---------------------------------------------------------------------------
# Tags (T-TAGS)
# ---------------------------------------------------------------------------


class TestTags:
    def test_empty_returns_list(self, client):
        resp = client.get("/tags")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_counts_after_add(self, client):
        client.post(
            "/memories",
            json={
                "content": "Alpha",
                "tags": ["project:mnemos", "agent:reviewer", "mnemos:learning"],
            },
        )
        client.post(
            "/memories",
            json={
                "content": "Beta",
                "tags": ["project:mnemos", "agent:reviewer", "mnemos:decision"],
            },
        )
        resp = client.get("/tags")
        assert resp.status_code == 200
        items = resp.json()
        # Each item must have exactly "tag" and "count" keys
        for item in items:
            assert set(item.keys()) == {"tag", "count"}
            assert isinstance(item["tag"], str)
            assert isinstance(item["count"], int)
        # project:mnemos and agent:reviewer appear in both memories
        by_tag = {item["tag"]: item["count"] for item in items}
        assert by_tag["project:mnemos"] == 2
        assert by_tag["agent:reviewer"] == 2
        # Tags that appear twice should come before tags that appear once
        counts = [item["count"] for item in items]
        assert counts == sorted(counts, reverse=True)

    def test_structure_stable_order(self, client):
        """Verify list (not dict) - order is deterministic (count desc)."""
        for content, tag in [
            ("A", "mnemos:learning"),
            ("B", "mnemos:learning"),
            ("C", "mnemos:decision"),
        ]:
            client.post(
                "/memories",
                json={
                    "content": content,
                    "tags": ["project:mnemos", "agent:test", tag],
                },
            )
        resp = client.get("/tags")
        assert resp.status_code == 200
        items = resp.json()
        assert isinstance(items, list)
        # project:mnemos and agent:test both appear 3 times - must be first two
        top_counts = [it["count"] for it in items[:2]]
        assert all(c == 3 for c in top_counts)
        # mnemos:learning appears 2 times, mnemos:decision 1 time - order preserved
        learning = next(it for it in items if it["tag"] == "mnemos:learning")
        decision = next(it for it in items if it["tag"] == "mnemos:decision")
        assert learning["count"] == 2
        assert decision["count"] == 1
        assert items.index(learning) < items.index(decision)
