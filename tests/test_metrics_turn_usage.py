"""nhi-15 wave 1 — the native turn_usage write path (store + REST + migration).

Contract under test:

* the store batch write: rows land with ``harness_id``/``project``/
  ``agent`` attribution, replays dedup on the
  ``(harness_id, session, turn_id)`` unique index (``accepted`` vs
  ``deduped``), and one signal is ONE transaction — a bad batch writes
  NOTHING (a partial money ledger would lie);
* harness isolation: the same ``(session, turn_id)`` under two
  ``harness_id`` values is TWO rows — cross-harness money baselines
  never bleed into each other (owner directive 2026-10-09);
* the failure split: contract refusals RAISE ``ValueError`` (the REST
  caller answers 4xx), infrastructure failure degrades to ``None``
  (telemetry never breaks the host);
* the wave-0 → wave-1 migration: a sidecar whose ``turn_usage`` lacks
  the harness columns gains them via idempotent ``ALTER TABLE ADD
  COLUMN`` + the dedup index, legacy rows preserved with
  ``harness_id='unknown'``; fresh stores are born final;
* the REST surface ``POST /signals/turn-usage``: 202
  ``{"accepted", "deduped"}``, oversized body → 413 (on the RAW body),
  per-harness rate cap → 429, validation → 422, disabled metrics
  plane → 503.

Sequential-suite discipline (owner directive 2026-10-06): this file is
self-isolated (tmp dirs, no live ~/.config, no live sidecar).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vesma.api import main as api_main
from vesma.api.main import app, lifespan
from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.metrics.sink import MetricsStore

HARNESS = "zcode"
SESSION = "sess_turn_1"


def _turn(turn_id: str = "t1", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "turn_id": turn_id,
        "ts": 1_760_000_000.0,
        "model": "GLM-5.3-Flash",
        "status": "completed",
        "input_tokens": 1_000,
        "cached_read_tokens": 800,
        "cached_write_tokens": 0,
        "output_tokens": 100,
        "reasoning_tokens": 0,
    }
    base.update(over)
    return base


def _batch(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "harness_id": HARNESS,
        "session": SESSION,
        "project": "proj-x",
        "agent": "agent-x",
        "turns": [_turn("t1"), _turn("t2", input_tokens=2_000)],
    }
    payload.update(over)
    return payload


def _rows(db: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT session, turn_id, harness_id, project, agent, ts, status, model,"
            " input_tokens, cached_read_tokens, cached_write_tokens, output_tokens,"
            " reasoning_tokens FROM turn_usage ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _columns(db: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [str(r[1]) for r in conn.execute("PRAGMA table_info(turn_usage)")]
    finally:
        conn.close()


def _has_dedupe_index(db: Path) -> bool:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_turn_dedupe'"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[MetricsStore]:
    st = MetricsStore(tmp_path / "metrics.sqlite")
    yield st
    st.close()


# ── store write path ───────────────────────────────────────────────────────


class TestStoreWritePath:
    def test_batch_insert_and_readback(self, store: MetricsStore, tmp_path: Path) -> None:
        result = store.record_turn_usage_batch(**_batch())
        assert result == (2, 0)
        rows = _rows(tmp_path / "metrics.sqlite")
        assert [r[1] for r in rows] == ["t1", "t2"]
        assert all(r[2] == HARNESS for r in rows)  # harness_id
        assert rows[0][3] == "proj-x" and rows[0][4] == "agent-x"
        assert rows[0][6] == "completed" and rows[0][7] == "GLM-5.3-Flash"
        assert rows[1][8] == 2_000 and rows[1][9] == 800  # in / cached_read

    def test_replay_dedups_and_new_turns_accept(self, store: MetricsStore, tmp_path: Path) -> None:
        assert store.record_turn_usage_batch(**_batch()) == (2, 0)
        # the SAME signal re-posted (a Stop-handler replay) absorbs all rows
        assert store.record_turn_usage_batch(**_batch()) == (0, 2)
        # a genuinely new turn in the same session still lands
        replay_plus_new = _batch(turns=[_turn("t1"), _turn("t3", output_tokens=7)])
        assert store.record_turn_usage_batch(**replay_plus_new) == (1, 1)
        assert len(_rows(tmp_path / "metrics.sqlite")) == 3

    def test_harness_isolation_same_session_turn(self, store: MetricsStore, tmp_path: Path) -> None:
        assert store.record_turn_usage_batch(**_batch()) == (2, 0)
        other = _batch(harness_id="other-harness")
        # same (session, turn_id) under another harness = different turns
        assert store.record_turn_usage_batch(**other) == (2, 0)
        rows = _rows(tmp_path / "metrics.sqlite")
        assert sorted({r[2] for r in rows}) == ["other-harness", HARNESS]

    def test_bad_row_refuses_whole_batch_no_partial_write(
        self, store: MetricsStore, tmp_path: Path
    ) -> None:
        bad = _batch(turns=[_turn("ok-turn"), _turn("bad-turn", ts=float("inf"))])
        with pytest.raises(ValueError):
            store.record_turn_usage_batch(**bad)
        # validation is pre-connection: the sidecar may not even exist yet
        db = tmp_path / "metrics.sqlite"
        assert not db.exists() or _rows(db) == []

    def test_infrastructure_failure_degrades_to_none(self, tmp_path: Path) -> None:
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("file, not a directory")
        st = MetricsStore(blocker / "metrics.sqlite")
        try:
            assert st.record_turn_usage_batch(**_batch()) is None
        finally:
            st.close()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"harness_id": ""},
            {"harness_id": "a\nb"},
            {"harness_id": "x" * 129},
            {"session": ""},
            {"project": "a\nb"},
            {"turns": []},
            {"turns": "not-a-list"},  # type: ignore[dict-item]
            {"turns": [_turn(turn_id="")]},
            {"turns": [_turn(turn_id="a\nb")]},
            {"turns": [_turn(ts=-1.0)]},
            {"turns": [_turn(ts=True)]},
            {"turns": [_turn(ts=float("nan"))]},
            {"turns": [_turn(status="bogus")]},
            {"turns": [_turn(input_tokens=-5)]},
            {"turns": [_turn(output_tokens=True)]},
            {"turns": [_turn(reasoning_tokens="100")]},  # type: ignore[list-item]
        ],
    )
    def test_contract_refusals_raise_valueerror(
        self, store: MetricsStore, tmp_path: Path, kwargs: dict[str, Any]
    ) -> None:
        with pytest.raises(ValueError):
            store.record_turn_usage_batch(**_batch(**kwargs))
        # validation is pre-connection: the sidecar may not even exist yet
        db = tmp_path / "metrics.sqlite"
        assert not db.exists() or _rows(db) == []


# ── migration (wave-0 sidecars → harness attribution) ─────────────────────


def _legacy_wave0_sidecar(path: Path) -> None:
    """A sidecar whose turn_usage predates the harness columns."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE turn_usage ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " session TEXT, turn_id TEXT, ts REAL NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'completed'"
        " CHECK (status IN ('completed','error','cancelled','other')),"
        " model TEXT,"
        " input_tokens INTEGER NOT NULL DEFAULT 0,"
        " cached_read_tokens INTEGER NOT NULL DEFAULT 0,"
        " cached_write_tokens INTEGER NOT NULL DEFAULT 0,"
        " output_tokens INTEGER NOT NULL DEFAULT 0,"
        " reasoning_tokens INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO turn_usage (session, turn_id, ts, input_tokens, output_tokens)"
        " VALUES ('legacy-sess', 'legacy-turn', 1759000000.0, 500, 50)"
    )
    conn.commit()
    conn.close()


class TestHarnessMigration:
    def test_legacy_sidecar_gains_columns_rows_preserved(self, tmp_path: Path) -> None:
        db = tmp_path / "metrics.sqlite"
        _legacy_wave0_sidecar(db)
        st = MetricsStore(db)
        try:
            assert st.record_turn_usage_batch(**_batch()) == (2, 0)
        finally:
            st.close()
        cols = _columns(db)
        # the ALTER set landed, exactly once each
        assert cols.count("harness_id") == 1
        assert cols.count("project") == 1 and cols.count("agent") == 1
        assert _has_dedupe_index(db)
        rows = _rows(db)
        assert len(rows) == 3  # legacy + the 2-turn batch
        legacy = [r for r in rows if r[1] == "legacy-turn"]
        assert legacy and legacy[0][2] == "unknown"  # default attribution
        assert legacy[0][8] == 500 and legacy[0][11] == 50

    def test_migration_idempotent_second_open(self, tmp_path: Path) -> None:
        db = tmp_path / "metrics.sqlite"
        _legacy_wave0_sidecar(db)
        first = MetricsStore(db)
        try:
            first.record_verb(surface="cli", verb="bootstrap", status="ok", latency_ms=0.0)
        finally:
            first.close()
        second = MetricsStore(db)
        try:
            # a second open re-runs the migration: no error, no duplicate columns
            assert second.record_turn_usage_batch(**_batch()) == (2, 0)
        finally:
            second.close()
        cols = _columns(db)
        assert cols.count("harness_id") == 1
        assert len(_rows(db)) == 3  # legacy + batch; nothing duplicated

    def test_fresh_store_born_final_with_harness_id(self, tmp_path: Path) -> None:
        db = tmp_path / "metrics.sqlite"
        st = MetricsStore(db)
        try:
            st.record_verb(surface="cli", verb="bootstrap", status="ok", latency_ms=0.0)
        finally:
            st.close()
        cols = _columns(db)
        assert "harness_id" in cols and "project" in cols and "agent" in cols
        assert _has_dedupe_index(db)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='turn_usage'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert "DEFAULT 'unknown'" in str(ddl)


# ── REST surface: POST /signals/turn-usage ─────────────────────────────────


def _settings(tmp: Path, *, vitals_enabled: bool = True) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        vitals={"enabled": vitals_enabled},
        code_graph={"enabled": True, "auto_index": False},
    )
    settings.resolve_paths()
    return settings


@contextmanager
def _rest(
    tmp_path: Path, *, vitals_enabled: bool = True
) -> Iterator[tuple[TestClient, MemoryManager]]:
    """The test_hooks REST pattern: patch the singleton, copy the routes,
    run the lifespan."""
    mgr = MemoryManager(_settings(tmp_path, vitals_enabled=vitals_enabled))
    api_main._manager = mgr
    api_main._turn_signal_rate.clear()
    test_app = FastAPI(title="Vesma-TurnUsage-Test", version="0.1.0", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)
    try:
        with TestClient(test_app) as client:
            yield client, mgr
    finally:
        api_main._manager = None
        api_main._turn_signal_rate.clear()
        mgr.close()


@pytest.fixture
def rest_client(tmp_path: Path) -> Iterator[TestClient]:
    with _rest(tmp_path) as (client, _mgr):
        yield client


@pytest.fixture
def rest_client_no_vitals(tmp_path: Path) -> Iterator[TestClient]:
    with _rest(tmp_path, vitals_enabled=False) as (client, _mgr):
        yield client


class TestRestTurnUsageSignal:
    def test_202_accepted_then_replay_deduped(self, rest_client: TestClient) -> None:
        resp = rest_client.post("/signals/turn-usage", json=_batch())
        assert resp.status_code == 202, resp.text
        assert resp.json() == {"accepted": 2, "deduped": 0}
        replay = rest_client.post("/signals/turn-usage", json=_batch())
        assert replay.status_code == 202
        assert replay.json() == {"accepted": 0, "deduped": 2}

    def test_rows_reach_the_manager_sidecar(self, rest_client: TestClient) -> None:
        assert rest_client.post("/signals/turn-usage", json=_batch()).status_code == 202
        mgr = api_main._manager
        assert mgr is not None and mgr._vitals_store is not None
        rows = _rows(mgr._vitals_store.db_path)
        assert len(rows) == 2 and rows[0][2] == HARNESS

    def test_422_missing_required_fields(self, rest_client: TestClient) -> None:
        payload = _batch()
        del payload["session"]
        resp = rest_client.post("/signals/turn-usage", json=payload)
        assert resp.status_code == 422

    def test_422_bad_status_enum(self, rest_client: TestClient) -> None:
        resp = rest_client.post("/signals/turn-usage", json=_batch(turns=[_turn(status="running")]))
        assert resp.status_code == 422

    def test_422_store_contract_refusal_newline_identity(self, rest_client: TestClient) -> None:
        # pydantic accepts it; the store's single-line identity contract refuses
        resp = rest_client.post("/signals/turn-usage", json=_batch(harness_id="a\nb"))
        assert resp.status_code == 422

    def test_413_oversized_raw_body(self, rest_client: TestClient) -> None:
        big = (
            b'{"harness_id":"zcode","session":"s","turns":[{"turn_id":"t","ts":1.0,"pad":"'
            + (b"a" * 1_100_000)
            + b'"}]}'
        )
        resp = rest_client.post(
            "/signals/turn-usage", content=big, headers={"content-type": "application/json"}
        )
        assert resp.status_code == 413

    def test_429_per_harness_rate_cap_then_other_harness_ok(
        self, rest_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api_main, "_TURN_SIGNAL_RATE_LIMIT", 2)
        assert rest_client.post("/signals/turn-usage", json=_batch()).status_code == 202
        assert rest_client.post("/signals/turn-usage", json=_batch()).status_code == 202
        flooded = rest_client.post("/signals/turn-usage", json=_batch())
        assert flooded.status_code == 429
        assert "zcode" in flooded.json()["detail"]
        other = rest_client.post("/signals/turn-usage", json=_batch(harness_id="other"))
        assert other.status_code == 202

    def test_503_metrics_plane_disabled(self, rest_client_no_vitals: TestClient) -> None:
        resp = rest_client_no_vitals.post("/signals/turn-usage", json=_batch())
        assert resp.status_code == 503
        assert "metrics plane" in resp.json()["detail"]
