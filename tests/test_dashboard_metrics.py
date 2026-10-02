"""Tests for dashboard / metrics endpoints and extended memory filters.

Covers:
  - GET /api/v1/stats (structured JSON dashboard data)
  - GET /api/v1/stats/timeseries (temporal data)
  - GET /api/v1/metrics (Prometheus text format)
  - GET /metrics (backward compat)
  - GET /memories extended filters (status, project, agent, tags, since, until, offset)
  - Search instrumentation (requests_total increments)
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
# Fixtures (mirror tests/test_api.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            vesma={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test-dashboard.db",
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

    test_app = FastAPI(
        title="Mnemos-Dashboard-Test",
        version="0.1.0",
        lifespan=lifespan,
    )
    for route in app.routes:
        test_app.routes.append(route)

    api_main._manager = mgr
    with TestClient(test_app) as tc:
        yield tc
    mgr.close()
    api_main._manager = None


def _add_memory(
    client: TestClient,
    content: str,
    tags: list[str],
    *,
    status: str = "raw",
) -> dict:
    resp = client.post(
        "/memories",
        json={"content": content, "tags": tags, "status": status},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# /api/v1/stats
# ---------------------------------------------------------------------------


class TestDashboardStats:
    def test_stats_structure(self, client):
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        data = resp.json()
        # Top-level sections
        for section in (
            "version",
            "timestamp",
            "volume",
            "filter",
            "pipeline",
            "search",
            "vectors",
            "sessions",
        ):
            assert section in data, f"missing section: {section}"
        # Volume subsections
        vol = data["volume"]
        for key in ("memories_total", "by_status", "by_project", "by_agent", "by_type"):
            assert key in vol, f"missing volume key: {key}"
        # Filter subsections
        filt = data["filter"]
        for key in (
            "auto_filter",
            "filtered_total",
            "unfiltered_total",
            "avg_reduction_pct",
            "by_profile",
        ):
            assert key in filt, f"missing filter key: {key}"
        # Pipeline subsections
        pipe = data["pipeline"]
        for key in ("processed_total", "failed_total", "dlq_depth", "last_run"):
            assert key in pipe, f"missing pipeline key: {key}"
        # Search subsections
        search = data["search"]
        for key in ("requests_total", "avg_latency_ms", "avg_results"):
            assert key in search, f"missing search key: {key}"
        # Vectors + sessions
        assert "indexed_total" in data["vectors"]
        assert "active" in data["sessions"]
        assert "total" in data["sessions"]

    def test_stats_reflects_data(self, client):
        _add_memory(
            client,
            "alpha",
            ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
        )
        _add_memory(
            client,
            "beta",
            ["project:other", "agent:code-reviewer", "mnemos:decision"],
        )
        resp = client.get("/api/v1/stats")
        data = resp.json()
        assert data["volume"]["memories_total"] == 2
        assert data["volume"]["by_project"].get("mnemos") == 1
        assert data["volume"]["by_project"].get("other") == 1
        assert data["volume"]["by_agent"].get("tech-lead") == 1
        assert data["volume"]["by_agent"].get("code-reviewer") == 1
        assert data["volume"]["by_type"].get("note") == 2

    def test_stats_version_matches_package(self, client):
        from mnemos import __version__

        resp = client.get("/api/v1/stats")
        assert resp.json()["version"] == __version__


# ---------------------------------------------------------------------------
# /api/v1/stats/timeseries
# ---------------------------------------------------------------------------


class TestTimeseries:
    def test_timeseries_default(self, client):
        _add_memory(
            client,
            "today",
            ["project:mnemos", "agent:test", "mnemos:learning"],
        )
        resp = client.get("/api/v1/stats/timeseries")
        assert resp.status_code == 200
        data = resp.json()
        assert data["granularity"] == "day"
        assert data["range"] == "30d"
        assert len(data["series"]) == 1
        series = data["series"][0]
        assert series["metric"] == "memories_added"
        assert isinstance(series["points"], list)
        # At least one point for today
        assert any(p["value"] >= 1 for p in series["points"])

    def test_timeseries_custom_range(self, client):
        resp = client.get("/api/v1/stats/timeseries?range=7d&metric=memories_added")
        assert resp.status_code == 200
        data = resp.json()
        assert data["range"] == "7d"

    def test_timeseries_unknown_metric(self, client):
        resp = client.get("/api/v1/stats/timeseries?metric=nonexistent")
        assert resp.status_code == 200
        data = resp.json()
        assert data["series"][0]["points"] == []

    def test_timeseries_invalid_range(self, client):
        resp = client.get("/api/v1/stats/timeseries?range=abc")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /api/v1/metrics (Prometheus)
# ---------------------------------------------------------------------------


class TestPrometheusMetrics:
    def test_metrics_text_format(self, client):
        _add_memory(
            client,
            "prom test",
            ["project:mnemos", "agent:test", "mnemos:learning"],
        )
        resp = client.get("/api/v1/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers.get("content-type", "")
        text = resp.text
        # Prometheus exposition format markers
        assert "# HELP mnemos_memories_total" in text
        assert "# TYPE mnemos_memories_total gauge" in text
        assert "mnemos_memories_total" in text
        assert "mnemos_memories_by_status" in text
        assert "mnemos_memories_by_project" in text
        assert "mnemos_pipeline_processed_total" in text
        assert "mnemos_search_requests_total" in text
        # A9 (ArchCom 2026-08-27): the explicit global-mode counter is
        # exported next to the search total, same exposition format.
        assert "# HELP mnemos_search_cross_project_requests_total" in text
        assert "# TYPE mnemos_search_cross_project_requests_total counter" in text
        assert "mnemos_search_cross_project_requests_total 0" in text
        assert "mnemos_vectors_indexed_total" in text
        assert "mnemos_sessions_total" in text

    def test_metrics_has_labels(self, client):
        _add_memory(
            client,
            "labeled",
            ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
        )
        text = client.get("/api/v1/metrics").text
        # Label format: metric{label="value"} number
        assert 'mnemos_memories_by_project{project="mnemos"}' in text
        assert 'mnemos_memories_by_agent{agent="tech-lead"}' in text


# ---------------------------------------------------------------------------
# /metrics backward compat
# ---------------------------------------------------------------------------


class TestMetricsBackwardCompat:
    def test_metrics_returns_json(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "by_status" in data


# ---------------------------------------------------------------------------
# GET /memories extended filters
# ---------------------------------------------------------------------------


class TestMemoryFilters:
    def test_filter_by_status(self, client):
        _add_memory(
            client,
            "raw one",
            ["project:test", "agent:test", "mnemos:learning"],
            status="raw",
        )
        _add_memory(
            client,
            "published one",
            ["project:test", "agent:test", "mnemos:learning"],
            status="published",
        )
        resp = client.get("/memories?status=published")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["status"] == "published"

    def test_filter_by_project(self, client):
        _add_memory(
            client,
            "mnemos mem",
            ["project:mnemos", "agent:test", "mnemos:learning"],
        )
        _add_memory(
            client,
            "other mem",
            ["project:other", "agent:test", "mnemos:learning"],
        )
        resp = client.get("/memories?project=mnemos")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert "project:mnemos" in items[0]["tags"]

    def test_filter_by_agent(self, client):
        _add_memory(
            client,
            "agent a",
            ["project:test", "agent:alpha", "mnemos:learning"],
        )
        _add_memory(
            client,
            "agent b",
            ["project:test", "agent:beta", "mnemos:learning"],
        )
        resp = client.get("/memories?agent=alpha")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert "agent:alpha" in items[0]["tags"]

    def test_filter_by_tags_and(self, client):
        _add_memory(
            client,
            "both tags",
            ["project:mnemos", "agent:test", "mnemos:learning"],
        )
        _add_memory(
            client,
            "one tag only",
            ["project:mnemos", "agent:test", "mnemos:decision"],
        )
        resp = client.get("/memories?tags=mnemos:learning,project:mnemos")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert "mnemos:learning" in items[0]["tags"]

    def test_filter_by_date_range(self, client):
        _add_memory(
            client,
            "recent",
            ["project:test", "agent:test", "mnemos:learning"],
        )
        resp = client.get("/memories?since=2026-01-01&until=2026-06-01")
        assert resp.status_code == 200
        # Memory created now (2026-06-20) is after 'until' → excluded
        assert len(resp.json()) == 0

    def test_filter_since_includes_recent(self, client):
        _add_memory(
            client,
            "recent",
            ["project:test", "agent:test", "mnemos:learning"],
        )
        resp = client.get("/memories?since=2026-06-19")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_pagination_offset(self, client):
        for i in range(5):
            _add_memory(
                client,
                f"item {i}",
                ["project:test", "agent:test", "mnemos:learning"],
            )
        resp = client.get("/memories?limit=2&offset=0")
        page1 = resp.json()
        assert len(page1) == 2
        resp = client.get("/memories?limit=2&offset=2")
        page2 = resp.json()
        assert len(page2) == 2
        # Pages don't overlap
        ids1 = {m["id"] for m in page1}
        ids2 = {m["id"] for m in page2}
        assert ids1.isdisjoint(ids2)

    def test_invalid_status_returns_422(self, client):
        resp = client.get("/memories?status=invalid")
        assert resp.status_code == 422

    def test_combined_filters(self, client):
        _add_memory(
            client,
            "match",
            ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
            status="raw",
        )
        _add_memory(
            client,
            "no match project",
            ["project:other", "agent:tech-lead", "mnemos:learning"],
            status="raw",
        )
        _add_memory(
            client,
            "no match status",
            ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
            status="published",
        )
        resp = client.get("/memories?status=raw&project=mnemos&agent=tech-lead")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["content"] == "match"


# ---------------------------------------------------------------------------
# Search instrumentation
# ---------------------------------------------------------------------------


class TestSearchInstrumentation:
    def test_search_increments_requests_total(self, client):
        _add_memory(
            client,
            "kubernetes deploy",
            ["project:test", "agent:test", "mnemos:learning"],
        )
        # Perform 3 searches (include_raw: newly added entries are raw)
        for _ in range(3):
            client.post(
                "/search",
                json={"query": "kubernetes", "limit": 5, "include_raw": True},
            )
        resp = client.get("/api/v1/stats")
        data = resp.json()
        assert data["search"]["requests_total"] == 3
        assert data["search"]["avg_latency_ms"] >= 0.0
        assert data["search"]["avg_results"] >= 1.0

    def test_prometheus_shows_search_count(self, client):
        _add_memory(
            client,
            "prom search",
            ["project:test", "agent:test", "mnemos:learning"],
        )
        client.post("/search", json={"query": "prom", "limit": 5})
        text = client.get("/api/v1/metrics").text
        # The counter line should show at least 1
        for line in text.splitlines():
            if line.startswith("mnemos_search_requests_total ") and not line.startswith("#"):
                value = int(line.split()[-1])
                assert value >= 1
                return
        pytest.fail("mnemos_search_requests_total metric line not found")
