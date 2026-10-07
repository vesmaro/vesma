"""Phase A2 integration tests — the verb ledger inside vesma.

The deep ledger contract (quantiles, C5 meta gates, exposition format)
is pinned in mnemos-vitals (master). This suite verifies the WIRING:
the MCP dispatch shell, the REST route-template middleware with its
exclusions, the rollup tick cadence, the /api/v1/metrics exposition,
and the CLI entry wrapper.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vesma.api import main as api_main
from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.metrics.schema import SIDECAR_FILENAME

SRC = Path(__file__).resolve().parents[1] / "src" / "vesma"


def _settings(tmp: Path, **overrides: object) -> Settings:
    payload: dict[str, object] = {
        "vesma": {
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        "scanner": {"enabled": False},
    }
    payload.update(overrides)
    settings = Settings(**payload)
    settings.resolve_paths()
    return settings


def _manager(settings: Settings) -> MemoryManager:
    mgr = MemoryManager(settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    return mgr


@pytest.fixture()
def mgr(tmp_path: Path):
    manager = _manager(_settings(tmp_path))
    yield manager
    manager.close()


def _verb_rows(manager: MemoryManager) -> list[tuple]:
    store = manager._vitals_store
    assert store is not None
    conn = sqlite3.connect(store.db_path)
    rows = conn.execute("SELECT surface, verb, status FROM verb_metrics ORDER BY id").fetchall()
    conn.close()
    return rows


class TestVerbBoundaries:
    def test_record_verb_vitals_writes_row(self, mgr: MemoryManager):
        mgr.record_verb_vitals(
            surface="background",
            verb="pipeline.cycle",
            status="ok",
            latency_ms=5.0,
            meta={"counters": {"published": 2}},
        )
        rows = _verb_rows(mgr)
        assert rows == [("background", "pipeline.cycle", "ok")]

    def test_mcp_dispatch_shell_records_tool_verb(self, tmp_path: Path):
        """The call_tool shell (boundary #1) records one verb per tool."""
        import asyncio

        from vesma.mcp_server import call_tool

        settings = _settings(tmp_path)
        manager = _manager(settings)
        try:
            # the shell resolves via mcp_server's own get_manager (the
            # same seam the dispatch uses — M1 fix), so seed THAT manager
            import vesma.mcp_server as mcp_mod

            mcp_mod._manager = manager
            try:
                result = asyncio.run(call_tool("vesma_stats", {}))
            finally:
                mcp_mod._manager = None
            assert result  # a real tool ran
            rows = _verb_rows(manager)
            assert any(r[1] == "vesma_stats" and r[0] == "mcp" for r in rows)
        finally:
            manager.close()

    def test_rest_middleware_records_route_template_and_excludes_service(self, tmp_path: Path):
        settings = _settings(tmp_path)
        manager = _manager(settings)
        try:
            api_main._manager = manager
            test_app = FastAPI()
            for route in api_main.app.routes:
                test_app.routes.append(route)
            # routes are copied, middleware are NOT — register the boundary
            from vesma.api.middleware import VitalsVerbMiddleware

            test_app.add_middleware(VitalsVerbMiddleware)
            with TestClient(test_app) as client:
                assert client.get("/health").status_code == 200
                assert client.get("/api/v1/stats").status_code in (200, 404)
                resp = client.post(
                    "/memories",
                    json={
                        "content": "vitals rest boundary probe",
                        "tags": ["project:vitals-t", "agent:test", "mnemos:session"],
                    },
                )
                assert resp.status_code == 201
            rows = _verb_rows(manager)
            verbs = [r[1] for r in rows]
            # excluded service paths never land
            assert not any("/health" in v or "/stats" in v for v in verbs)
            # the real request lands as a ROUTE TEMPLATE, not a raw path
            assert "rest:POST:/memories" in verbs
        finally:
            manager.close()
            api_main._manager = None


class TestRollupTick:
    def test_rollup_tick_runs_hourly_and_before_retention(self, mgr: MemoryManager):
        mgr.record_verb_vitals(surface="mcp", verb="vesma_search", status="ok", latency_ms=7.0)
        store = mgr._vitals_store
        assert store is not None
        # backdate the verb row into the PREVIOUS COMPLETE hour — the
        # tick's default target
        import time as _time

        prev_hour = int(_time.time() // 3600) - 1
        conn = sqlite3.connect(store.db_path)
        conn.execute("UPDATE verb_metrics SET ts=?", (prev_hour * 3600 + 5,))
        conn.commit()
        conn.close()

        mgr._vitals_rollup_last_ts = 0.0
        mgr._maybe_run_vitals_rollup()
        conn = sqlite3.connect(store.db_path)
        n = conn.execute("SELECT COUNT(*) FROM verb_metrics_hourly").fetchone()[0]
        conn.close()
        assert n == 2  # global + per-project rows for the hour

        stamp = mgr._vitals_rollup_last_ts
        assert stamp > 0
        mgr._maybe_run_vitals_rollup()  # within the hour → skipped
        assert mgr._vitals_rollup_last_ts == stamp


class TestExpositionEndpoint:
    def test_api_v1_metrics_includes_vitals_plane(self, tmp_path: Path):
        settings = _settings(tmp_path)
        manager = _manager(settings)
        try:
            api_main._manager = manager
            manager.record_verb_vitals(surface="mcp", verb="vesma_add", status="ok", latency_ms=3.0)
            store = manager._vitals_store
            assert store is not None
            # roll a known hour directly (the tick targets the previous hour)
            conn = sqlite3.connect(store.db_path)
            conn.execute("UPDATE verb_metrics SET ts=?", (29000110 * 3600 + 3,))
            conn.commit()
            conn.close()
            store.rollup_hourly(hour=29000110)

            test_app = FastAPI()
            for route in api_main.app.routes:
                test_app.routes.append(route)
            with TestClient(test_app) as client:
                resp = client.get("/api/v1/metrics")
            assert resp.status_code == 200
            text = resp.text
            assert "mnemos_verb_calls_total" in text
            assert 'verb="vesma_add"' in text
            # RL-S2: project slug never leaks into exposition
            assert "vitals-t" not in text
        finally:
            manager.close()
            api_main._manager = None


class TestCliBoundary:
    def test_cli_entry_wrapper_records_verb(self, tmp_path: Path, monkeypatch):
        import sqlite3 as s3

        cli_settings = _settings(tmp_path)
        monkeypatch.setattr("vesma.config.load_settings", lambda _cfg=None: cli_settings)
        monkeypatch.setattr("sys.argv", ["vesma", "--version"])
        from vesma.cli.main import cli_main

        with pytest.raises(SystemExit) as excinfo:
            cli_main()
        assert excinfo.value.code in (0, None)
        db = tmp_path / "data" / SIDECAR_FILENAME
        assert db.exists()
        conn = s3.connect(db)
        rows = conn.execute("SELECT surface, verb FROM verb_metrics").fetchall()
        conn.close()
        assert rows and rows[0][0] == "cli"
