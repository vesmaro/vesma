"""Wave 9 integration tests — the usage loop exposed as an MCP tool.

The deep sink/analyzer/exposer contracts are pinned in
``tests/test_usage_phase_c.py`` (vendored master suite) and the phase A/C
wiring in ``tests/test_vitals_phase_a.py``/``test_vitals_phase_c.py``.
This suite verifies the NEW MCP surface only, driven through the real
dispatch (``_call_tool_dispatch``, the same surface an MCP client hits):

  - ``vesma_usage_report`` closes the loop end-to-end: the assemble
    result carries the additive ``usage_report.metrics_id`` key, a report
    through the tool lands in ``usage_reports``, and UsageAnalyzer reads
    1.0 loop closure;
  - refusals come back as clean ``{"error": ...}`` dicts through the tool
    surface and write NO rows (unknown metrics_id, hostile shapes,
    boundary type guards);
  - the async handle envelope carries NO metrics_id — a usage report
    against it degrades to a clean error, never a row;
  - harness-authored loop semantics: the server never self-reports (the
    only row writer for usage_reports under the MCP path is the tool
    itself, and the assemble boundary only records the assemble row);
  - RL-S2: the ``/api/v1/metrics`` exposition stays label-free after
    reports land;
  - the manager wrapper keeps the guest contract: a broken sidecar
    swallows to ``None``, never a raise.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vesma.mcp_server as mcp_mod
from vesma.api import main as api_main
from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.mcp_server import _call_tool_dispatch, list_tools
from vesma.metrics.usage import UsageAnalyzer


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


async def _dispatch(mgr: MemoryManager, name: str, args: dict) -> dict:
    """Drive one tool call through the real dispatch and parse its JSON.

    Uses the module-level ``_manager`` handle exactly as the MCP client
    path does (mirrors the test_vitals_phase_c.py driving pattern). The
    module-level checkpoint counter is reset per call — the shared
    ``_checkpoint_tracker`` otherwise crosses the 12-call reminder
    threshold mid-file and appends a ⚠️ line that breaks JSON parsing of
    later (hermetic) tests.
    """
    mcp_mod._checkpoint_tracker["calls_since_save"] = 0
    mcp_mod._checkpoint_tracker["last_save_ts"] = 0.0
    mcp_mod._manager = mgr
    try:
        contents = await _call_tool_dispatch(name, args)
    finally:
        mcp_mod._manager = None
    text = contents[0].text
    try:
        return json.loads(text)
    except ValueError:
        # Tag-contract / generic error paths render as ❌-prefixed text —
        # normalize into the {"error": ...} shape the handlers use.
        return {"error": text}


def _usage_rows(store) -> list[sqlite3.Row]:
    """All usage_reports rows — [] when the sidecar was never created
    (the sidecar file only exists after the first successful write)."""
    if not store.db_path.exists():
        return []
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM usage_reports ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return []  # empty sidecar file without tables yet
    finally:
        conn.close()


@pytest.fixture()
def mgr(tmp_path: Path):
    manager = _manager(_settings(tmp_path))
    yield manager
    manager.close()


class TestManifest:
    async def test_usage_report_advertised(self):
        names = [t.name for t in await list_tools()]
        assert "vesma_usage_report" in names

    async def test_schema_shape_required_and_optionals(self):
        tools = {t.name: t for t in await list_tools()}
        schema = tools["vesma_usage_report"].input_schema
        assert schema["required"] == ["metrics_id", "block_ids_touched"]
        props = schema["properties"]
        assert props["metrics_id"]["type"] == "integer"
        assert props["block_ids_touched"]["type"] == "array"
        assert props["tokens_out"]["type"] == "integer"
        assert props["wrong_tool_flag"]["type"] == "boolean"

    async def test_description_states_loop_contract(self):
        tools = {t.name: t for t in await list_tools()}
        desc = tools["vesma_usage_report"].description
        assert "caller is the harness" in desc.lower()
        assert "never self-reports" in desc.lower()
        assert "empty list is legitimate" in desc.lower()
        # the opaque ordinal format is documented for the caller
        assert "<metrics_id>:<i>" in desc


class TestAssembleMetricsIdSurfacing:
    async def test_sync_result_carries_metrics_id(self, mgr: MemoryManager, caplog):
        data = await _dispatch(mgr, "vesma_assemble_context", {"session": "s1", "project": "demo"})
        ur = data.get("usage_report")
        assert isinstance(ur, dict) and isinstance(ur.get("metrics_id"), int)
        assert ur["metrics_id"] > 0
        # the id addresses a real sidecar row
        store = mgr._vitals_store
        assert store is not None
        conn = sqlite3.connect(store.db_path)
        row = conn.execute(
            "SELECT session, project FROM assemble_metrics WHERE id = ?",
            (ur["metrics_id"],),
        ).fetchone()
        conn.close()
        assert row == ("s1", "demo")

    async def test_async_envelope_carries_no_metrics_id(self, mgr: MemoryManager):
        data = await _dispatch(
            mgr,
            "vesma_assemble_context",
            {"session": "s2", "project": "demo", "mode": "async"},
        )
        assert "usage_report" not in data  # additive key born on sync results only
        store = mgr._vitals_store
        assert store is not None
        # the envelope itself is not recorded (phase A rule): no assemble
        # row, hence no sidecar tables materialized on disk at all
        assert _usage_rows(store) == []
        conn = sqlite3.connect(store.db_path)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        except sqlite3.OperationalError:
            tables = set()
        conn.close()
        assert "assemble_metrics" not in tables  # nothing was recorded

    async def test_disabled_plane_leaves_result_untouched(self, tmp_path: Path):
        settings = _settings(tmp_path, vitals={"enabled": False})
        manager = _manager(settings)
        try:
            assert manager._vitals_store is None
            data = await _dispatch(
                manager, "vesma_assemble_context", {"session": "s3", "project": "demo"}
            )
            assert "usage_report" not in data  # no id — and no fabricated zero
            assert "text" in data  # the assembly itself is intact
        finally:
            manager.close()


class TestUsageReportTool:
    async def test_roundtrip_through_tool_closes_loop(self, mgr: MemoryManager):
        data = await _dispatch(mgr, "vesma_assemble_context", {"session": "s4", "project": "demo"})
        mid = data["usage_report"]["metrics_id"]
        out = await _dispatch(
            mgr,
            "vesma_usage_report",
            {
                "metrics_id": mid,
                "block_ids_touched": [f"{mid}:0"],
                "tokens_out": 7,
            },
        )
        assert out.get("status") == "recorded"
        assert isinstance(out.get("usage_id"), int)
        store = mgr._vitals_store
        assert store is not None
        rows = _usage_rows(store)
        assert len(rows) == 1
        assert rows[0]["metrics_id"] == mid
        assert json.loads(rows[0]["block_ids_touched_json"]) == [f"{mid}:0"]
        assert rows[0]["tokens_out"] == 7
        assert rows[0]["wrong_tool_flag"] == 0
        report = UsageAnalyzer(store).assemble_usage_rate()
        assert report["status"] == "OK"
        assert report["assemble_usage_rate"] == 1.0

    async def test_block_ordinals_match_injection_blocks(self, mgr: MemoryManager):
        """The tool accepts the opaque ordinals the assemble boundary wrote
        for real injected blocks (roundtrip on the ordinal contract)."""
        await _dispatch(
            mgr,
            "vesma_add",
            {
                "content": "Usage loop closure sentence for the MCP wave 9 roundtrip test.",
                "tags": ["project:demo", "agent:tester", "mnemos:learning"],
            },
        )
        data = await _dispatch(mgr, "vesma_assemble_context", {"session": "s5", "project": "demo"})
        mid = data["usage_report"]["metrics_id"]
        assert data["blocks"], "setup: expected an injected block"
        out = await _dispatch(
            mgr,
            "vesma_usage_report",
            {"metrics_id": mid, "block_ids_touched": [f"{mid}:0"]},
        )
        assert out.get("status") == "recorded"
        store = mgr._vitals_store
        assert store is not None
        conn = sqlite3.connect(store.db_path)
        injected = conn.execute(
            "SELECT block_id FROM injection_blocks WHERE metrics_id = ?", (mid,)
        ).fetchall()
        touched = conn.execute(
            "SELECT block_ids_touched_json FROM usage_reports WHERE metrics_id = ?", (mid,)
        ).fetchone()[0]
        conn.close()
        assert [i[0] for i in injected] == [f"{mid}:0"]
        assert json.loads(touched) == [f"{mid}:0"]

    async def test_wrong_tool_flag_roundtrip(self, mgr: MemoryManager):
        data = await _dispatch(mgr, "vesma_assemble_context", {"session": "s6", "project": "demo"})
        mid = data["usage_report"]["metrics_id"]
        out = await _dispatch(
            mgr,
            "vesma_usage_report",
            {"metrics_id": mid, "block_ids_touched": [], "wrong_tool_flag": True},
        )
        assert out.get("status") == "recorded"
        store = mgr._vitals_store
        assert store is not None
        row = _usage_rows(store)[0]
        assert row["wrong_tool_flag"] == 1
        assert json.loads(row["block_ids_touched_json"]) == []  # empty is legitimate
        report = UsageAnalyzer(store).wrong_tool_rate()
        assert report["wrong_tool_rate"] == 1.0

    async def test_unknown_metrics_id_clean_error_no_row(self, mgr: MemoryManager):
        n = len(_usage_rows(mgr._vitals_store)) if mgr._vitals_store else 0
        out = await _dispatch(
            mgr,
            "vesma_usage_report",
            {"metrics_id": 999_999, "block_ids_touched": ["x"]},
        )
        assert "error" in out
        assert "refused" in out["error"]
        store = mgr._vitals_store
        assert store is not None
        assert len(_usage_rows(store)) == n  # no row, clean error dict

    async def test_hostile_shapes_clean_error_no_rows(self, mgr: MemoryManager):
        data = await _dispatch(mgr, "vesma_assemble_context", {"session": "s7", "project": "demo"})
        mid = data["usage_report"]["metrics_id"]
        store = mgr._vitals_store
        assert store is not None
        before = len(_usage_rows(store))
        # Boundary type guards at the MCP layer
        for bad_args in (
            {"metrics_id": "7", "block_ids_touched": ["x"]},
            {"metrics_id": True, "block_ids_touched": ["x"]},
            {"block_ids_touched": ["x"]},  # metrics_id absent
            {"metrics_id": mid, "block_ids_touched": "x"},  # not a list
            {"metrics_id": mid, "block_ids_touched": ["x"], "tokens_out": "5"},
            {"metrics_id": mid, "block_ids_touched": ["x"], "wrong_tool_flag": "yes"},
        ):
            out = await _dispatch(mgr, "vesma_usage_report", bad_args)
            assert "error" in out, bad_args
        # Sink-owned hostile shapes passed the boundary guards: the sink
        # refuses the write loudly (warning log) and the tool surfaces a
        # clean error dict.
        for bad_args in (
            {"metrics_id": mid, "block_ids_touched": ["x"], "tokens_out": -1},
            {"metrics_id": mid, "block_ids_touched": ["x", 7]},
            {"metrics_id": mid, "block_ids_touched": ["x\ny"]},
            {"metrics_id": mid, "block_ids_touched": [""]},
            {"metrics_id": mid, "block_ids_touched": ["z" * 129]},
            {"metrics_id": mid, "block_ids_touched": ["x"] * 257},
        ):
            out = await _dispatch(mgr, "vesma_usage_report", bad_args)
            assert "error" in out, bad_args
        assert len(_usage_rows(store)) == before  # a hostile battery wrote nothing

    async def test_report_on_async_envelope_is_clean_error(self, mgr: MemoryManager):
        """The async envelope carries no metrics_id; a drifted harness
        report against mode='async' is a clean error, never a row."""
        out = await _dispatch(
            mgr,
            "vesma_usage_report",
            {"metrics_id": 1, "block_ids_touched": ["x"]},
        )
        store = mgr._vitals_store
        assert store is not None
        # either the boundary refused or (no assemble row exists yet) the
        # sink refused the unknown parent — both are clean error dicts.
        assert "error" in out
        assert len(_usage_rows(store)) == 0


class TestManagerWrapper:
    async def test_wrapper_swallows_broken_sidecar(self, mgr: MemoryManager):
        store = mgr._vitals_store
        assert store is not None

        def broken(*_a, **_kw):
            raise sqlite3.OperationalError("sidecar exploded")

        store.record_usage = broken  # type: ignore[method-assign]
        out = await _dispatch(
            mgr,
            "vesma_usage_report",
            {"metrics_id": 1, "block_ids_touched": ["x"]},
        )
        assert "error" in out  # degraded, never raised into the harness

    def test_wrapper_returns_none_when_plane_disabled(self, tmp_path: Path):
        settings = _settings(tmp_path, vitals={"enabled": False})
        manager = _manager(settings)
        try:
            assert manager.record_usage_vitals(1, block_ids_touched=["x"], tokens_out=0) is None
            assert manager.record_assemble_vitals({"stats": {}, "blocks": []}) is None
        finally:
            manager.close()


class TestExpositionLabelFree:
    def test_rls2_label_free_after_reports_land(self, tmp_path: Path):
        """RL-S2: the usage family on /api/v1/metrics stays label-free
        once usage reports exist (counter rendered), and the assemble
        boundary's project slug never crosses to Prometheus."""
        manager = _manager(_settings(tmp_path))
        try:
            api_main._manager = manager
            result = manager.assemble_context(session="sess-e", project="vitals-9")
            mid = manager.record_assemble_vitals(result)
            assert mid is not None
            recorded = manager.record_usage_vitals(
                mid, block_ids_touched=[f"{mid}:0"], tokens_out=3
            )
            assert recorded is not None
            test_app = FastAPI()
            for route in api_main.app.routes:
                test_app.routes.append(route)
            with TestClient(test_app) as client:
                resp = client.get("/api/v1/metrics")
            assert resp.status_code == 200
            text = resp.text
            assert "vesma_usage_reports_total" in text  # reports render now
            assert "vesma_usage_loop_rate 1.0" in text  # closure visible
            assert "vitals-9" not in text  # project slug never leaks
        finally:
            manager.close()
            api_main._manager = None
