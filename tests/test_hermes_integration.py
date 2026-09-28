"""E2E tests for the new HTTP endpoints added for the Hermes plugin.

Covers the endpoints introduced alongside the Hermes MemoryProvider plugin:

  - POST /context/save     — session checkpoint (incl. C1 list-fields bugfix)
  - POST /context/recall   — recall session context
  - POST /compress         — CCR compression
  - POST /retrieve         — CCR retrieval
  - GET  /auto-collect     — compaction signal vector
  - POST /ingest-url       — URL ingest (with credential stripping)
  - POST /watch/start      — start file watcher
  - POST /watch/stop       — stop file watcher (idempotent)
  - GET  /watch/status     — watcher status

These tests follow the patterns established in ``tests/test_api.py``: a
per-test ``client`` fixture builds an isolated ``MemoryManager`` with a
mocked embedder and a fresh FastAPI app whose routes are copied from the
real ``app``.  The autouse ``reset_rate_limiter`` fixture from
``conftest.py`` clears the slowapi quota between tests.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vesmaro.api import main as api_main
from vesmaro.api.main import app, lifespan
from vesmaro.config import Settings
from vesmaro.manager import MemoryManager

# ---------------------------------------------------------------------------
# Fixtures — mirror tests/test_api.py
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            mnemos={
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

    # Build a fresh FastAPI app so lifespan is isolated per test.
    test_app = FastAPI(
        title="Mnemos-Hermes-Test",
        version="0.1.0",
        lifespan=lifespan,
    )
    # Copy all routes from the real app.
    for route in app.routes:
        test_app.routes.append(route)

    # Override get_manager to return our isolated mgr.
    api_main._manager = mgr
    with TestClient(test_app) as tc:
        yield tc
    mgr.close()
    api_main._manager = None


def _large_text(n_chars: int = 1200) -> str:
    """Generate text longer than the default CCR min_size_chars (500)."""
    base = "2026-07-07T10:00:00Z INFO worker processing item "
    line = base + "0123456789" * 5 + "\n"
    repeats = max(1, n_chars // len(line) + 1)
    return (line * repeats)[:n_chars]


# ---------------------------------------------------------------------------
# POST /context/save
# ---------------------------------------------------------------------------


class TestContextSave:
    def test_save_with_string_fields(self, client):
        """String fields produce a 201 with {status, id, title}."""
        resp = client.post(
            "/context/save",
            json={
                "project": "hermes",
                "goals": "Ship the integration tests",
                "completed": "Wrote the conftest stubs",
                "in_progress": "Writing endpoint tests",
                "decisions": "Use TestClient for E2E",
                "context": "All tests run against an isolated manager.",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "saved"
        assert "id" in data
        assert isinstance(data["id"], str)
        assert "title" in data

    def test_save_with_list_fields(self, client):
        """List fields (the C1 bugfix) are accepted and joined with newlines."""
        resp = client.post(
            "/context/save",
            json={
                "project": "hermes",
                "goals": ["goal one", "goal two"],
                "completed": ["task A", "task B"],
                "in_progress": ["item 1"],
                "decisions": ["decide X"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "saved"
        assert "id" in data

    def test_save_minimal_only_project(self, client):
        """Only ``project`` with all fields empty is trivially rejected (#251 D0).

        mnemos #251 D0 item 5 replaces the legacy 201-on-empty behaviour:
        a checkpoint with all five fields empty stores nothing and tells
        the caller (400) — zero-loss, nothing silently dropped.
        """
        resp = client.post(
            "/context/save",
            json={"project": "minimal"},
        )
        assert resp.status_code == 400
        assert "all fields" in resp.json()["detail"]

    def test_save_missing_project_returns_422(self, client):
        """Missing required ``project`` → 422 validation error."""
        resp = client.post(
            "/context/save",
            json={"goals": "no project supplied"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /context/recall
# ---------------------------------------------------------------------------


class TestContextRecall:
    def test_recall_after_save(self, client):
        """Recall after a save returns a non-empty checkpoints array."""
        client.post(
            "/context/save",
            json={"project": "hermes", "goals": "Recall me"},
        )
        resp = client.post(
            "/context/recall",
            json={"project": "hermes"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["project"] == "hermes"
        assert isinstance(data["checkpoints"], list)
        assert len(data["checkpoints"]) >= 1
        first = data["checkpoints"][0]
        assert "id" in first
        assert "content" in first
        assert "tags" in first

    def test_recall_no_context(self, client):
        """Recall with no prior save returns empty checkpoints + message."""
        resp = client.post(
            "/context/recall",
            json={"project": "empty-project"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["checkpoints"] == []
        assert "message" in data
        assert data["message"]

    def test_recall_with_query(self, client):
        """Recall accepts an optional ``query`` parameter."""
        client.post(
            "/context/save",
            json={"project": "hermes", "context": "kubernetes deployment notes"},
        )
        resp = client.post(
            "/context/recall",
            json={"project": "hermes", "query": "kubernetes"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["checkpoints"], list)


# ---------------------------------------------------------------------------
# POST /compress
# ---------------------------------------------------------------------------


class TestCompress:
    def test_compress_large_text(self, client):
        """Text > 500 chars is cached and returns reduction metadata."""
        text = _large_text(1200)
        resp = client.post("/compress", json={"text": text})
        assert resp.status_code == 200
        data = resp.json()
        assert "compressed_text" in data
        assert "hash" in data
        assert data["hash"]  # non-empty hash when cached
        assert "reduction_pct" in data
        assert data["cached"] is True
        assert data["original_size"] == len(text)

    def test_compress_short_text_not_cached(self, client):
        """Text shorter than min_size_chars is returned as-is, cached=False."""
        text = "short snippet, no caching needed"
        resp = client.post("/compress", json={"text": text})
        assert resp.status_code == 200
        data = resp.json()
        assert data["cached"] is False
        assert data["compressed_text"] == text
        assert data["reduction_pct"] == 0.0

    def test_compress_missing_text_returns_422(self, client):
        """Missing required ``text`` field → 422."""
        resp = client.post("/compress", json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /retrieve
# ---------------------------------------------------------------------------


class TestRetrieve:
    def test_retrieve_after_compress(self, client):
        """Retrieve by hash returns the original content."""
        text = _large_text(1200)
        comp = client.post("/compress", json={"text": text})
        assert comp.status_code == 200
        h = comp.json()["hash"]
        assert h

        resp = client.post("/retrieve", json={"hash": h})
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is True
        assert data["original"] == text

    def test_retrieve_with_query_returns_snippets(self, client):
        """Retrieve with a query returns a snippets array."""
        text = _large_text(1500)
        comp = client.post("/compress", json={"text": text})
        h = comp.json()["hash"]

        resp = client.post("/retrieve", json={"hash": h, "query": "worker"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is True
        assert "snippets" in data
        assert isinstance(data["snippets"], list)

    def test_retrieve_unknown_hash(self, client):
        """An unknown hash returns found=False."""
        resp = client.post(
            "/retrieve",
            json={"hash": "deadbeef" * 8},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is False


# ---------------------------------------------------------------------------
# GET /auto-collect
# ---------------------------------------------------------------------------


class TestAutoCollect:
    def test_auto_collect_structure(self, client):
        """The signal vector has the expected top-level shape."""
        resp = client.get("/auto-collect")
        assert resp.status_code == 200
        data = resp.json()
        assert "auto_collect_enabled" in data
        assert "signals" in data
        assert "recommendation" in data
        signals = data["signals"]
        assert "call_counter" in signals
        assert "elapsed_secs" in signals
        assert "context_size_heuristic" in signals
        assert "summary_marker_detected" in signals
        assert "reference_drop_heuristic" in signals
        assert signals["call_counter"]["calls_since_save"] >= 0

    def test_calls_since_save_increments(self, client):
        """Memory-work operations increment the call counter."""
        # Baseline
        before = client.get("/auto-collect").json()
        calls_before = before["signals"]["call_counter"]["calls_since_save"]

        # A non-save memory-work call (compress) increments the counter.
        client.post("/compress", json={"text": "x" * 60})

        after = client.get("/auto-collect").json()
        calls_after = after["signals"]["call_counter"]["calls_since_save"]
        assert calls_after > calls_before


# ---------------------------------------------------------------------------
# POST /ingest-url
# ---------------------------------------------------------------------------


class TestIngestUrl:
    def test_ingest_url_missing_tags_returns_422(self, client):
        """Missing required ``tags`` → 422 validation error."""
        resp = client.post(
            "/ingest-url",
            json={"url": "https://example.com/"},
        )
        assert resp.status_code == 422

    def test_ingest_url_strips_credentials(self, client):
        """Credentials embedded in the URL (user:pass@) are stripped.

        We mock httpx + trafilatura so no real network call is made. The
        endpoint stores the *cleaned* URL (no credentials) on the memory
        and echoes it back in the response.
        """
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
                    "url": "https://user:secret@example.com/page",
                    "tags": ["project:test", "agent:test", "mnemos:learning"],
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        # The returned URL must NOT contain the user:pass credentials.
        assert "user:secret@" not in data["url"]
        assert "example.com" in data["url"]
        assert "id" in data

    def test_ingest_url_valid_with_tags(self, client):
        """A valid URL + tags returns 201 with id/title/url.

        Mocked to avoid real network calls (and to remain robust when
        trafilatura is not installed in the test environment).
        """
        trafilatura_stub = MagicMock()
        trafilatura_stub.extract.return_value = "extracted content"
        with (
            patch("httpx.Client") as mock_client_cls,
            patch.dict(sys.modules, {"trafilatura": trafilatura_stub}),
        ):
            mock_client = MagicMock()
            mock_resp = MagicMock()
            mock_resp.text = "body text"
            mock_resp.status_code = 200
            mock_resp.headers = {}
            mock_client.get.return_value = mock_resp
            mock_client_cls.return_value.__enter__.return_value = mock_client

            resp = client.post(
                "/ingest-url",
                json={
                    "url": "https://example.com/docs",
                    "tags": ["project:test", "agent:test", "mnemos:learning"],
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"]


# ---------------------------------------------------------------------------
# POST /watch/* and GET /watch/status
# ---------------------------------------------------------------------------


class TestWatch:
    """The watch surface is the ADR-0032 §3.2 project-graph poll registrar:
    the former M8 directory-watcher form was an unimplemented stub and is
    gone; without the operator flags the endpoint refuses (503)."""

    def test_watch_start_legacy_form_refused(self, client):
        """Legacy paths=/scan=/include_rules= body no longer validates."""
        resp = client.post(
            "/watch/start",
            json={"paths": [], "scan": False, "include_rules": False},
        )
        assert resp.status_code == 422  # project_id/agent are required now

    def test_watch_start_disabled_without_operator_flags(self, client):
        """Default config keeps code_graph.enabled=false → 503 operator gate."""
        resp = client.post("/watch/start", json={"project_id": "smoke", "agent": "qa"})
        assert resp.status_code == 503
        assert "disabled" in resp.json()["detail"].lower()

    def test_watch_status_shape(self, client):
        """Status is available without any start and reports the gate state."""
        resp = client.get("/watch/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "running" in data
        assert isinstance(data["running"], bool)
        assert data["running"] is False
        assert data["watch_enabled"] is False

    def test_watch_stop(self, client):
        """Stop returns status=stopped."""
        resp = client.post("/watch/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"

    def test_watch_stop_idempotent(self, client):
        """Stopping when nothing is running is still 'stopped'."""
        # Stop without a prior start.
        resp = client.post("/watch/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"
        # Stop again — still idempotent.
        resp2 = client.post("/watch/stop")
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "stopped"

    def test_watch_status_running_false_initially(self, client):
        """Status reports running=false before any start."""
        resp = client.get("/watch/status")
        assert resp.status_code == 200
        assert resp.json()["running"] is False
