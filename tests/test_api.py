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

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vesma.api import main as api_main
from vesma.api.main import app, lifespan
from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import TagContractError

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
    # Copy exception handlers too — route copies alone skip the handlers
    # registered on the real app (rate limiting, TagContractError → 422),
    # so the fixture must mirror production error mapping exactly.
    test_app.exception_handlers.update(app.exception_handlers)

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

    def test_create_memory_missing_required_tag_maps_to_422(self, client):
        # #422/#432 defect class: a tag-contract violation (missing the
        # required mnemos: scope) is a CLIENT error — 422 carrying the
        # contract error string, never a raw 500.
        resp = client.post(
            "/memories",
            json={
                "content": "Broken tags",
                "tags": ["project:mnemos", "agent:reviewer"],
            },
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "missing required tag: vesma:<subtype>" in detail

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
# Ingest tag-contract 422 mapping (#422/#432 defect class)
# ---------------------------------------------------------------------------


class TestIngestTagContract:
    """The ingest routes must map a tag-contract ``ValueError`` to 422 —
    same client-error discipline as ``create_memory`` — never a raw 500."""

    # Hermeticity (card vesma-hermetic-ingest-url-tests): the SSRF guard
    # resolves DNS for hostname URLs (socket.getaddrinfo) BEFORE the
    # mocked httpx client is used — an offline run would turn these
    # tests into failures. A PUBLIC literal IP passes the guard's
    # literal branch (private/TEST-NET literals are refused) with NO
    # DNS at all; the fetch itself is mocked, so nothing connects.
    INGEST_HOST = "93.184.216.34"

    def test_ingest_url_missing_required_tag_maps_to_422(self, client):
        # A tag-contract violation (missing the required mnemos: scope) is
        # a CLIENT error — 422 carrying the contract error string verbatim.
        resp = client.post(
            "/ingest-url",
            json={
                "url": f"https://{self.INGEST_HOST}/page",
                "tags": ["project:test", "agent:test"],
            },
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "missing required tag: vesma:<subtype>" in detail

    def test_ingest_url_valid_tags_still_ingest(self, client):
        # Happy path: contract-valid tags ingest normally (201, mocked
        # fetch — no network, no DNS: public literal-IP host).
        trafilatura_stub = MagicMock()
        trafilatura_stub.extract.return_value = "extracted page content"
        with (
            patch("httpx.Client") as mock_client_cls,
            patch.dict(sys.modules, {"trafilatura": trafilatura_stub}),
        ):
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.text = "page body"
            mock_resp.status_code = 200
            mock_resp.headers = {}
            mock_client.get.return_value = mock_resp
            mock_client_cls.return_value.__enter__.return_value = mock_client

            resp = client.post(
                "/ingest-url",
                json={
                    "url": f"https://{self.INGEST_HOST}/docs",
                    "tags": ["project:test", "agent:test", "mnemos:learning"],
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"]
        assert self.INGEST_HOST in data["url"]

    def test_ingest_document_missing_required_tag_maps_to_422(self, client):
        # Same discipline for the ADR-0027 document-ingest twin: the
        # contract ValueError must surface as 422, not 500.
        resp = client.post(
            "/ingest-document",
            json={
                "text": "# A\n\nBody text.",
                "doc_id": "tagcontract-doc",
                "tags": ["project:test", "agent:test"],
            },
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "missing required tag: vesma:<subtype>" in detail

    def test_ingest_document_valid_tags_still_ingest(self, client):
        # Happy path: contract-valid tags ingest normally (201, Ф3 shape).
        resp = client.post(
            "/ingest-document",
            json={
                "text": "# A\n\nBody text.",
                "doc_id": "tagcontract-doc",
                "tags": ["project:test", "agent:test", "mnemos:learning"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["doc_id"] == "tagcontract-doc"
        assert data["chunks_total"] >= 1


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
        # vesma:learning appears 2 times, vesma:decision 1 time - order preserved
        learning = next(it for it in items if it["tag"] == "vesma:learning")
        decision = next(it for it in items if it["tag"] == "vesma:decision")
        assert learning["count"] == 2
        assert decision["count"] == 1
        assert items.index(learning) < items.index(decision)


# ---------------------------------------------------------------------------
# vesma:* input alias (6.0.0 store block, ArchCom 2026-10-03 option B)
# ---------------------------------------------------------------------------


class TestVesmaTagInputAlias:
    """`vesma:<subtype>` accepted at API boundaries, stored as `mnemos:<subtype>`.

    Storage format is frozen: ``mnemos:`` stays the written prefix; the
    alias normalizes at the input boundary (create via
    ``validate_tag_contract``, filters via ``normalize_tag_aliases``).
    """

    def test_create_normalizes_alias(self, client):
        resp = client.post(
            "/memories",
            json={
                "content": "api alias storage probe",
                "tags": ["project:mnemos", "agent:reviewer", "vesma:learning"],
            },
        )
        assert resp.status_code == 201, resp.text
        tags = resp.json()["tags"]
        # The vesma:* input is already canonical — stored byte-identical.
        assert "vesma:learning" in tags
        assert "mnemos:learning" not in tags
        assert not any(t.startswith("mnemos:") for t in tags)

    def test_create_no_federate_alias_normalizes(self, client):
        resp = client.post(
            "/memories",
            json={
                "content": "api no-federate alias probe",
                "tags": ["project:mnemos", "agent:reviewer", "vesma:no-federate"],
            },
        )
        assert resp.status_code == 201, resp.text
        assert "mnemos:no-federate" in resp.json()["tags"]

    def test_create_unknown_alias_refused_with_422(self, client):
        """Unknown alias subtype refuses loudly (422 + named tag), never a 500."""
        resp = client.post(
            "/memories",
            json={
                "content": "api unknown alias probe",
                "tags": ["project:mnemos", "agent:reviewer", "vesma:bogus"],
            },
        )
        assert resp.status_code == 422
        assert "vesma:bogus" in resp.text

    def test_list_filter_accepts_alias(self, client):
        client.post(
            "/memories",
            json={
                "content": "api alias filter probe",
                "tags": ["project:mnemos", "agent:reviewer", "vesma:decision"],
            },
        )
        resp = client.get("/memories?tags=vesma:decision")
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) == 1
        assert "vesma:decision" in results[0]["tags"]
        # Legacy spelling sees the identical row set.
        resp_legacy = client.get("/memories?tags=mnemos:decision")
        assert resp_legacy.json() == results

    def test_search_accepts_alias_tags(self, client):
        client.post(
            "/memories",
            json={
                "content": "quuxly alias search probe",
                "tags": ["project:mnemos", "agent:reviewer", "vesma:learning"],
            },
        )
        resp = client.post("/search", json={"query": "quuxly", "tags": ["vesma:learning"]})
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) == 1
        assert "quuxly" in results[0]["content"]
        resp_legacy = client.post("/search", json={"query": "quuxly", "tags": ["mnemos:learning"]})
        assert resp_legacy.json() == results


# Bulk tags REST twins (#454 tail): /api/v1/tags/add, /api/v1/tags/remove
# ---------------------------------------------------------------------------


class TestTagsAddRemoveTwins:
    """REST twins of the grouped ``vesma_tags`` MCP tool actions
    ``add``/``remove`` (the #454 tail). Coverage per twin: happy path,
    the contract surface (per-row refusals ride the report at 200 — MCP
    parity; an escaping contract ``ValueError`` maps to 422, never a raw
    500) and the project/agent filter scoping."""

    def _seed(self, client: TestClient, agent: str = "twins-agent") -> str:
        resp = client.post(
            "/memories",
            json={
                "content": f"tags-twin seed {agent}",
                "tags": ["project:twins-proj", f"agent:{agent}", "mnemos:decision"],
            },
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    def _tags_of(self, client: TestClient, memory_id: str) -> list[str]:
        resp = client.get(f"/memories/{memory_id}")
        assert resp.status_code == 200, resp.text
        return list(resp.json()["tags"])

    def test_add_twin_happy(self, client):
        mid = self._seed(client)
        resp = client.post(
            "/api/v1/tags/add",
            json={
                "tags": ["severity:low"],
                "project": "twins-proj",
                "agent": "twins-agent",
                "dry_run": False,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "add"
        assert body["changed"] == 1
        assert body["dry_run"] is False
        assert "severity:low" in self._tags_of(client, mid)

    def test_remove_twin_happy(self, client):
        mid = self._seed(client)
        client.post(
            "/api/v1/tags/add",
            json={
                "tags": ["severity:low"],
                "project": "twins-proj",
                "dry_run": False,
            },
        )
        resp = client.post(
            "/api/v1/tags/remove",
            json={
                "tags": ["severity:low"],
                "project": "twins-proj",
                "dry_run": False,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "remove"
        assert body["changed"] == 1
        assert "severity:low" not in self._tags_of(client, mid)

    def test_add_twin_default_is_dry_run(self, client):
        """Boundary: the write twin is inert unless the caller says
        ``dry_run=false`` explicitly (same default as the MCP tool)."""
        mid = self._seed(client)
        resp = client.post(
            "/api/v1/tags/add",
            json={"tags": ["severity:low"], "project": "twins-proj"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True
        assert "severity:low" not in self._tags_of(client, mid)

    def test_add_twin_contract_refusal_rides_the_report(self, client):
        """An invalid ``mnemos:`` subtype is refused per memory and
        reported in ``errors`` at HTTP 200 — MCP parity: the report is
        the error channel, the store is not corrupted."""
        mid = self._seed(client)
        resp = client.post(
            "/api/v1/tags/add",
            json={
                "tags": ["mnemos:bogus_subtype"],
                "project": "twins-proj",
                "dry_run": False,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["errors"], f"expected per-memory contract errors, got {body}"
        assert "mnemos:bogus_subtype" not in self._tags_of(client, mid)
        assert "vesma:decision" in self._tags_of(client, mid)  # store intact

    def test_remove_twin_last_project_tag_blocked(self, client):
        """Removing the last ``project:`` tag breaks the tag contract —
        refused per memory in the report, never written."""
        mid = self._seed(client)
        resp = client.post(
            "/api/v1/tags/remove",
            json={
                "tags": ["project:twins-proj"],
                "project": "twins-proj",
                "dry_run": False,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["errors"], f"expected contract refusals, got {body}"
        assert "project:twins-proj" in self._tags_of(client, mid)  # not corrupted

    def test_add_twin_escaping_contract_error_maps_to_422(self, client, monkeypatch):
        """The #422/#432 defect class stays out of the new routes: a
        ``TagContractError`` escaping the manager maps to 422 with the
        same message, never a raw 500."""

        def boom(*args: object, **kwargs: object) -> dict[str, object]:
            raise TagContractError("tag must have a prefix (contain ':'): 'bogus'")

        mgr = api_main._manager
        assert mgr is not None
        monkeypatch.setattr(mgr, "tags_add", boom)
        resp = client.post("/api/v1/tags/add", json={"tags": ["severity:low"]})
        assert resp.status_code == 422
        assert "prefix" in resp.json()["detail"]

    def test_remove_twin_escaping_contract_error_maps_to_422(self, client, monkeypatch):
        def boom(*args: object, **kwargs: object) -> dict[str, object]:
            raise ValueError("strict tag contract violated: no project: tag left")

        mgr = api_main._manager
        assert mgr is not None
        monkeypatch.setattr(mgr, "tags_remove", boom)
        resp = client.post("/api/v1/tags/remove", json={"tags": ["severity:low"]})
        assert resp.status_code == 422
        assert "contract" in resp.json()["detail"]

    def test_agent_filter_scopes_the_operation(self, client):
        """The ``agent`` filter passes through the route untouched: only
        the scoped agent's memories change (the MCP surface semantics)."""
        mid_a = self._seed(client, agent="agent-a")
        mid_b = self._seed(client, agent="agent-b")
        resp = client.post(
            "/api/v1/tags/add",
            json={"tags": ["severity:low"], "agent": "agent-a", "dry_run": False},
        )
        assert resp.status_code == 200
        assert resp.json()["changed"] == 1
        assert "severity:low" in self._tags_of(client, mid_a)
        assert "severity:low" not in self._tags_of(client, mid_b)
