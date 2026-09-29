"""Phase C integration tests — the usage loop inside vesmaro.

The deep sink/analyzer/exposer contract (validation exactness, atomic
FK, kappa gate, exposition format) is pinned in
``tests/test_usage_phase_c.py`` (ported from the mnemos-vitals master
suite). This suite verifies the WIRING, mirroring the phase A/A2 pattern:

  - the two collection boundaries (MCP ``mnemos_assemble_context`` and
    the ``pre_llm_call`` hook) record the assemble row via the vendored
    sink AFTER the result exists, non-fatal to the host call;
  - assemble recording works with the NEW write in the plane (no
    interference from the record_usage port);
  - NO-DATA semantics are live through the manager: a fresh deploy has
    zero ``usage_reports`` rows and the analytics report loud NO-DATA —
    never silent zeros, never exceptions into a scraper/host;
  - RL-S2: the ``/api/v1/metrics`` exposition's ``mnemos_usage_*``
    family carries NO project/endpoint/principal labels — global
    aggregates only.

The loop-CLOSURE write itself is harness-authored (post-LLM-call): the
server records the assemble row only («usage_reports» = one row per
harness response, ADR-0026 §C; server self-reporting would fake the
loop rate and poison the author plane) — the vendored sink/analyzer
here serve the harness-facing annex and the exposition plane.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vesmaro.api import main as api_main
from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.metrics.usage import UsageAnalyzer


def _settings(tmp: Path, **overrides: object) -> Settings:
    payload: dict[str, object] = {
        "mnemos": {
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


class TestCollectionBoundaries:
    def test_mcp_assemble_boundary_records_after_result(self, tmp_path: Path):
        """Boundary 1: the MCP handler records the assemble row after the
        result exists; the record must not alter the returned result."""
        import asyncio

        import vesmaro.mcp_server as mcp_mod
        from vesmaro.mcp_server import call_tool

        manager = _manager(_settings(tmp_path))
        try:
            mcp_mod._manager = manager
            try:
                result = asyncio.run(
                    call_tool(
                        "mnemos_assemble_context",
                        {"session": "sess-mcp", "project": "demo"},
                    )
                )
            finally:
                mcp_mod._manager = None
            assert isinstance(result, list) and result
            store = manager._vitals_store
            assert store is not None
            conn = sqlite3.connect(store.db_path)
            rows = conn.execute(
                "SELECT session, project, mode, blocks_count FROM assemble_metrics"
            ).fetchall()
            conn.close()
            assert rows == [("sess-mcp", "demo", "sync", 0)]
        finally:
            manager.close()

    def test_hook_boundary_records_after_result(self, tmp_path: Path):
        """Boundary 2: the pre_llm_call hook records the assemble row; a
        recording failure must not alter the hook's result or raise."""
        from vesmaro import hooks

        manager = _manager(_settings(tmp_path))
        try:
            store = manager._vitals_store
            assert store is not None
            original = store.record_assemble
            calls: list[dict] = []

            def spy(result):
                calls.append(result)
                return original(result)

            store.record_assemble = spy  # type: ignore[method-assign]
            result = hooks.pre_llm_call(
                manager,
                session="sess-hook",
                project="demo",
                agent="agent-a",
            )
            assert result.get("hook") == "pre_llm_call"  # host result untouched
            assert calls and calls[0].get("session") == "sess-hook"
            conn = sqlite3.connect(store.db_path)
            rows = conn.execute("SELECT session, project, mode FROM assemble_metrics").fetchall()
            conn.close()
            assert rows == [("sess-hook", "demo", "sync")]
        finally:
            manager.close()

    def test_record_failure_is_non_fatal_to_hook(self, tmp_path: Path):
        """A broken sidecar degrades to a warning; the hook result stands."""
        from vesmaro import hooks

        manager = _manager(_settings(tmp_path))
        try:
            store = manager._vitals_store
            assert store is not None
            store._local = None  # break the connection plane

            def broken(_result):
                raise sqlite3.OperationalError("sidecar exploded")

            store.record_assemble = broken  # type: ignore[method-assign]
            result = hooks.pre_llm_call(
                manager,
                session="sess-brk",
                project="demo",
                agent="agent-a",
            )
            assert result.get("hook") == "pre_llm_call"  # failure swallowed
            assert "text" in result  # the injection text is intact
        finally:
            manager.close()

    def test_async_envelope_not_recorded(self, tmp_path: Path):
        """mode='async' returns a handle envelope — recording it would
        poison the corpus with empty rows (phase A contract stays)."""
        import asyncio

        import vesmaro.mcp_server as mcp_mod
        from vesmaro.mcp_server import call_tool

        manager = _manager(_settings(tmp_path))
        try:
            mcp_mod._manager = manager
            try:
                result = asyncio.run(
                    call_tool(
                        "mnemos_assemble_context",
                        {"session": "sess-a", "project": "demo", "mode": "async"},
                    )
                )
            finally:
                mcp_mod._manager = None
            assert isinstance(result, list) and result
            store = manager._vitals_store
            assert store is not None
            conn = sqlite3.connect(store.db_path)
            n = conn.execute("SELECT COUNT(*) FROM assemble_metrics").fetchone()[0]
            conn.close()
            assert n == 0  # envelope excluded, exactly the phase A rule
        finally:
            manager.close()


class TestManagerNoData:
    def test_fresh_deploy_reports_loud_no_data(self, mgr: MemoryManager):
        """NO-DATA by design on a live deploy: zero assemble rows (and no
        reports) — the analytics must say NO-DATA loudly, never zeros,
        never an exception."""
        store = mgr._vitals_store
        assert store is not None
        a = UsageAnalyzer(store)
        for report in (
            a.assemble_usage_rate(),
            a.touched_share(),
            a.wrong_tool_rate(),
        ):
            assert report["status"] == "NO-DATA"
            assert report["reasons"]  # loud, always with a reason
            for k, v in report.items():
                if k.endswith("_rate") or k.endswith("_share"):
                    assert v is None  # never a silent zero

    def test_analyzer_never_raises_through_manager(self, mgr: MemoryManager):
        store = mgr._vitals_store
        assert store is not None
        store.close()  # break the read plane
        a = UsageAnalyzer(store)
        for report in (a.assemble_usage_rate(), a.touched_share(), a.wrong_tool_rate()):
            assert report["status"] == "NO-DATA"  # degraded, never raised


class TestEndpointExposition:
    def test_usage_family_on_endpoint_label_free_and_no_data_absent(self, tmp_path: Path):
        """RL-S2 on the live endpoint: the mnemos_usage_* family carries
        zero labels; on a fresh deploy it is ABSENT (NO-DATA ≠ 0); a
        project slug recorded by the assemble boundary never leaks."""
        manager = _manager(_settings(tmp_path))
        try:
            api_main._manager = manager
            result = manager.assemble_context(session="sess-e", project="vitals-c")
            manager.record_assemble_vitals(result)
            test_app = FastAPI()
            for route in api_main.app.routes:
                test_app.routes.append(route)
            with TestClient(test_app) as client:
                resp = client.get("/api/v1/metrics")
            assert resp.status_code == 200
            text = resp.text
            assert "mnemos_usage_loop_rate 0.0" in text  # one call, zero reports
            assert "mnemos_usage_reports_total" not in text  # NO-DATA absent
            assert "vitals-c" not in text  # project slug never crosses to Prometheus
            usage_family = text.split("# HELP mnemos_usage_loop_rate")[1]
            assert "{" not in usage_family and "}" not in usage_family
        finally:
            manager.close()
            api_main._manager = None


class TestSinkPortParity:
    def test_sink_accepts_usage_write_after_assemble(self, mgr: MemoryManager):
        """The vendored sink closes the loop for a harness-authored report:
        assemble row -> record_usage -> closed loop visible in analytics.
        (The harness-facing annex path; not a server self-report.)"""
        store = mgr._vitals_store
        assert store is not None
        result = mgr.assemble_context(session="sess-l", project="demo")
        mid = store.record_assemble(result)
        assert mid is not None
        rid = store.record_usage(mid, block_ids_touched=[f"{mid}:0"], tokens_out=7)
        assert rid is not None
        report = UsageAnalyzer(store).assemble_usage_rate()
        assert report["status"] == "OK"
        assert report["assemble_usage_rate"] == 1.0
