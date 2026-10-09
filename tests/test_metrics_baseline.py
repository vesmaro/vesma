"""nhi-15 money baseline — aggregation, born-final schema and the CLI.

Contract under test:

* percentiles interpolate (median of an even sample), empty is ``None``
  — NO-DATA is not a zero;
* ``build_report`` aggregates medians/p90 per turn and per session,
  the cached share, per-model money with UNKNOWN models flagged (never
  priced), and the session_type breakdown with the ``unclassified``
  default;
* the born-final schema: fresh stores carry ``turn_usage`` (with the
  cached columns from birth) and ``session_labels``; a PRE-money-plane
  sidecar gains both tables on the next open (the additive
  ``CREATE TABLE IF NOT EXISTS`` awareness precedent); the
  ``session_type`` CHECK refuses unknown labels;
* the harness reader (ZCode ``db.sqlite``) is read-only, converts
  epoch-ms → seconds at the boundary, resolves the turn's dominant
  model from ``model_usage`` and maps ``session.task_type`` onto the
  baseline axis;
* the CLI: ``vesma metrics baseline`` renders NO-DATA loudly on an
  empty plane, real numbers (and a JSON snapshot) over seeded rows;
  ``vesma metrics session-type`` validates the label domain, persists
  a re-label and lists it back.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vesma.cli._manager import reset_manager
from vesma.cli.main import app
from vesma.metrics.baseline import (
    TurnRow,
    build_report,
    percentile,
    read_harness_session_labels,
    read_harness_turns,
    read_sidecar_turns,
)
from vesma.metrics.schema import SESSION_TYPES, SIDECAR_FILENAME
from vesma.metrics.sink import MetricsStore
from vesma.metrics.tariffs import parse_tariffs

runner = CliRunner()

TARIFFS = parse_tariffs(
    json.dumps(
        {
            "schema_version": 1,
            "as_of": "2026-10-08",
            "currency": "USD",
            "models": {"GLM-5.3-Flash": {"input": 0.15, "cached_input": 0.03, "output": 0.5}},
        }
    )
)


def _ts(days_ago: float = 0.0) -> float:
    return (datetime.now(UTC) - timedelta(days=days_ago)).timestamp()


# ── percentiles ────────────────────────────────────────────────────────────


def test_percentile_interpolates_and_none_on_empty() -> None:
    assert percentile([], 0.5) is None
    assert percentile([5.0], 0.9) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.0) == 1.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 1.0) == 4.0


# ── aggregation ────────────────────────────────────────────────────────────


def _turn(model: str | None, inp: int, cached: int, out: int, session: str = "s1") -> TurnRow:
    return TurnRow(
        session=session,
        ts=_ts(0.1),
        model=model,
        status="completed",
        input_tokens=inp,
        cached_read_tokens=cached,
        output_tokens=out,
    )


def test_build_report_numbers_and_unknown_flag() -> None:
    turns = [
        _turn("GLM-5.3-Flash", 1_000_000, 800_000, 100_000, session="s1"),
        _turn("GLM-5.3-Flash", 2_000_000, 1_600_000, 200_000, session="s1"),
        _turn("mystery-model", 500_000, 0, 10_000, session="s2"),  # unknown, flagged
    ]
    report = build_report(
        source="test",
        turns=turns,
        session_types={},
        tariffs=TARIFFS,
        window_days=7,
        since_ts=_ts(7),
        until_ts=_ts(0),
    )
    assert report.turn_count == 3
    assert report.session_count == 2
    assert report.total_input_tokens == 3_500_000
    assert report.total_cached_read_tokens == 2_400_000
    assert report.cached_share == pytest.approx(2_400_000 / 3_500_000)
    # unknown model is flagged, NOT priced
    assert report.unknown_model_tokens == 500_000
    # known money (fresh in / cached in / out in millions x 0.15/0.03/0.50):
    #   turn1: 0.2*0.15 + 0.8*0.03 + 0.1*0.5 = 0.104
    #   turn2: 0.4*0.15 + 1.6*0.03 + 0.2*0.5 = 0.208
    assert report.cost_usd == pytest.approx(0.312)
    assert report.cost_usd_per_day == pytest.approx(0.312 / 7)
    # per-turn medians: inputs 0.5M/1M/2M → 1M; outputs 10k/100k/200k → 100k
    assert report.per_turn_input[0] == 1_000_000.0
    assert report.per_turn_output[0] == 100_000.0
    # per-session medians over the session SUMS: s1 (1M+2M = 3M), s2 (0.5M) → (3+0.5)/2
    assert report.per_session_input[0] == 1_750_000.0
    # snapshot shape round-trips
    doc = report.to_dict()
    assert doc["cost_usd"]["unknown_model_tokens"] == 500_000
    assert json.dumps(doc)  # JSON-serializable


def test_build_report_session_type_breakdown_and_default() -> None:
    turns = [
        _turn("GLM-5.3-Flash", 1_000, 0, 0, session="a"),
        _turn("GLM-5.3-Flash", 3_000, 0, 0, session="b"),
        _turn("GLM-5.3-Flash", 10_000, 0, 0, session="c"),
    ]
    report = build_report(
        source="test",
        turns=turns,
        session_types={"a": "task", "b": "background"},  # c unlabeled
        tariffs=TARIFFS,
        window_days=1,
        since_ts=_ts(1),
        until_ts=_ts(0),
    )
    by_type = {t.session_type: t for t in report.session_types}
    assert set(by_type) == {"task", "background", "unclassified"}
    assert by_type["task"].turns == 1
    assert by_type["background"].turns == 1
    assert by_type["unclassified"].turns == 1
    assert by_type["unclassified"].median_input_per_turn == 10_000.0


def test_build_report_no_data_is_not_zero() -> None:
    report = build_report(
        source="test",
        turns=[],
        session_types={},
        tariffs=TARIFFS,
        window_days=7,
        since_ts=0.0,
        until_ts=1.0,
    )
    assert report.turn_count == 0
    assert report.cached_share is None
    assert report.per_turn_input == (None, None)
    assert any("NO-DATA" in n for n in report.notes)


# ── born-final schema ──────────────────────────────────────────────────────


def _materialize_store(path: Path) -> None:
    """Create the sidecar file with the full born-final schema.

    ``MetricsStore`` opens its connection lazily — one legit verb write
    materializes the file and applies ``SCHEMA_SQL``.
    """
    store = MetricsStore(path)
    try:
        store.record_verb(surface="cli", verb="test-bootstrap", status="ok", latency_ms=0.0)
    finally:
        store.close()


def test_fresh_store_carries_money_plane(tmp_path: Path) -> None:
    sidecar = tmp_path / "m.sqlite"
    _materialize_store(sidecar)
    conn = sqlite3.connect(sidecar)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"turn_usage", "session_labels"} <= tables
    cols = {r[1] for r in conn.execute("PRAGMA table_info(turn_usage)")}
    assert {"cached_read_tokens", "cached_write_tokens", "reasoning_tokens"} <= cols
    conn.close()


def test_sidecar_gains_money_tables_and_check_refuses(tmp_path: Path) -> None:
    # a PRE-money-plane sidecar: old five-table shape (turn_usage absent)
    old = tmp_path / "metrics.sqlite"
    conn = sqlite3.connect(old)
    conn.execute(
        "CREATE TABLE verb_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL)"
    )
    conn.commit()
    conn.close()

    store = MetricsStore(old)
    try:
        store.record_verb(surface="cli", verb="test-bootstrap", status="ok", latency_ms=0.0)
        sconn = sqlite3.connect(old)
        tables = {r[0] for r in sconn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "turn_usage" in tables and "session_labels" in tables
        # cached columns exist FROM BIRTH (nhi-15 v1: no ALTER TABLE train)
        cols = {r[1] for r in sconn.execute("PRAGMA table_info(turn_usage)")}
        assert {"cached_read_tokens", "cached_write_tokens"} <= cols
        # session_type CHECK refuses labels outside the born-final domain
        with pytest.raises(sqlite3.IntegrityError):
            sconn.execute(
                "INSERT INTO session_labels (session, session_type, updated_ts)"
                " VALUES ('x','work',0)"
            )
        sconn.commit()
        sconn.close()
        # sidecar reader round-trips a seeded turn
        sconn = sqlite3.connect(old)
        sconn.execute(
            "INSERT INTO turn_usage (session, ts, model, status, input_tokens,"
            " cached_read_tokens, output_tokens) VALUES ('sess_x', ?, 'GLM-5.3-Flash',"
            " 'completed', 1000, 400, 50)",
            (_ts(0.01),),
        )
        sconn.commit()
        sconn.close()
        rows = read_sidecar_turns(old, _ts(1), _ts(0))
        assert len(rows) == 1
        assert rows[0].cached_read_tokens == 400
    finally:
        store.close()


def test_session_labels_domain_matches_schema() -> None:
    assert SESSION_TYPES == ("task", "chat", "background", "unclassified")


# ── harness reader (ZCode shape, read-only) ────────────────────────────────


def _make_harness_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    now_ms = _ts(0.01) * 1000.0
    conn.execute(
        "CREATE TABLE turn_usage (session_id TEXT, turn_id TEXT, started_at INTEGER,"
        " status TEXT, input_tokens INTEGER, cache_read_input_tokens INTEGER,"
        " output_tokens INTEGER)"
    )
    conn.execute(
        "CREATE TABLE model_usage (session_id TEXT, turn_id TEXT, model_id TEXT,"
        " status TEXT, input_tokens INTEGER)"
    )
    conn.execute("CREATE TABLE session (id TEXT, task_type TEXT)")
    conn.execute(
        "INSERT INTO turn_usage VALUES ('sessA','t1',?, 'completed', 1000, 600, 100)",
        (now_ms,),
    )
    conn.execute("INSERT INTO model_usage VALUES ('sessA','t1','big/model-X','completed', 700)")
    conn.execute("INSERT INTO model_usage VALUES ('sessA','t1','GLM-5.3-Flash','completed', 300)")
    conn.execute("INSERT INTO session VALUES ('sessA', 'interactive')")
    conn.execute("INSERT INTO session VALUES ('sessB', NULL)")
    conn.commit()
    conn.close()


def test_harness_reader_ms_to_seconds_dominant_model_labels(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    _make_harness_db(db)
    rows = read_harness_turns(db, _ts(1), _ts(0))
    assert len(rows) == 1
    row = rows[0]
    # epoch-ms boundary: ts came back in SECONDS and inside the window
    assert _ts(1) < row.ts < _ts(0)
    assert row.session == "sessA"
    assert row.input_tokens == 1000 and row.cached_read_tokens == 600
    # dominant model = largest billed input across the turn's requests
    assert row.model == "big/model-X"
    labels = read_harness_session_labels(db)
    assert labels["sessA"] == "task"  # interactive → task
    assert labels["sessB"] == "unclassified"  # NULL → default


def test_harness_reader_missing_table_is_loud(tmp_path: Path) -> None:
    from vesma.metrics.baseline import BaselineSourceError

    db = tmp_path / "db.sqlite"
    db.write_text("")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE other (x INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(BaselineSourceError):
        read_harness_turns(db, 0.0, 1.0)


# ── CLI ────────────────────────────────────────────────────────────────────


@pytest.fixture
def ops_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    reset_manager()
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"vesma:\n"
        f"  vault_path: {tmp_path / 'vault'}\n"
        f"  data_dir: {tmp_path / 'data'}\n"
        f"  db_name: t.db\n"
    )
    monkeypatch.setenv("VESMA_CONFIG", str(cfg))
    yield cfg
    reset_manager()


def test_cli_baseline_no_data_and_snapshot_off(ops_config: Path) -> None:
    result = runner.invoke(app, ["metrics", "baseline", "--days", "1", "--no-snapshot"])
    assert result.exit_code == 0, result.output
    assert "n/a" in result.output  # NO-DATA renders loud, never as 0


def test_cli_baseline_seeded_numbers_and_snapshot(ops_config: Path, tmp_path: Path) -> None:
    # default config discovery via VESMA_CONFIG puts data_dir at tmp_path/data
    data_dir = tmp_path / "data"
    _materialize_store(data_dir / SIDECAR_FILENAME)
    sconn = sqlite3.connect(data_dir / SIDECAR_FILENAME)
    for i, (inp, cached) in enumerate([(1_000_000, 800_000), (3_000_000, 2_400_000)]):
        sconn.execute(
            "INSERT INTO turn_usage (session, ts, model, status, input_tokens,"
            " cached_read_tokens, output_tokens) VALUES (?, ?, 'GLM-5.3-Flash',"
            " 'completed', ?, ?, 100_000)",
            (f"sess_{i}", _ts(0.01 * (i + 1)), inp, cached),
        )
    sconn.commit()
    sconn.close()
    result = runner.invoke(app, ["metrics", "baseline", "--days", "1"])
    assert result.exit_code == 0, result.output
    assert "GLM-5.3-Flash" in result.output
    assert "unclassified" in result.output
    # snapshot written under <data_dir>/baselines/
    snapshots = list((data_dir / "baselines").glob("baseline-*.json"))
    assert len(snapshots) == 1
    doc = json.loads(snapshots[0].read_text())
    assert doc["turns"] == 2
    assert doc["tokens"]["cached_share"] == pytest.approx(3_200_000 / 4_000_000)


def test_cli_session_type_set_list_and_bad_label(ops_config: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _materialize_store(data_dir / SIDECAR_FILENAME)
    # set → relist
    result = runner.invoke(app, ["metrics", "session-type", "sess_abc", "task"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["metrics", "session-type", "--list"])
    assert result.exit_code == 0, result.output
    assert "sess_abc" in result.output and "task" in result.output
    # re-label overwrites (manual reclassification)
    result = runner.invoke(app, ["metrics", "session-type", "sess_abc", "background"])
    assert result.exit_code == 0, result.output
    sconn = sqlite3.connect(data_dir / SIDECAR_FILENAME)
    label = sconn.execute(
        "SELECT session_type FROM session_labels WHERE session = 'sess_abc'"
    ).fetchone()[0]
    sconn.close()
    assert label == "background"
    # unknown label refused, nothing written
    result = runner.invoke(app, ["metrics", "session-type", "sess_abc", "work"])
    assert result.exit_code == 2
    assert "allowed" in result.output
    # missing args → usage
    result = runner.invoke(app, ["metrics", "session-type"])
    assert result.exit_code == 2


# ── by-harness cut (nhi-15 wave 1) ─────────────────────────────────────────


def test_by_harness_money_medians_daily_and_json(tmp_path: Path) -> None:
    """The cross-harness cut: $/day, medians and the JSON snapshot axis."""
    turns = [
        TurnRow(
            session="z1",
            ts=_ts(0.1),
            model="GLM-5.3-Flash",
            status="completed",
            input_tokens=1_000_000,
            cached_read_tokens=800_000,
            output_tokens=100_000,
            harness="zcode",
        ),
        TurnRow(
            session="z2",
            ts=_ts(0.2),
            model="GLM-5.3-Flash",
            status="completed",
            input_tokens=3_000_000,
            cached_read_tokens=2_400_000,
            output_tokens=300_000,
            harness="zcode",
        ),
        TurnRow(
            session="o1",
            ts=_ts(0.3),
            model="GLM-5.3-Flash",
            status="completed",
            input_tokens=500_000,
            cached_read_tokens=0,
            output_tokens=10_000,
            harness="other-harness",
        ),
    ]
    report = build_report(
        source="test",
        turns=turns,
        session_types={},
        tariffs=TARIFFS,
        window_days=2,
        since_ts=_ts(2),
        until_ts=_ts(0),
    )
    by_harness = {h.harness: h for h in report.harnesses}
    assert set(by_harness) == {"zcode", "other-harness"}
    z = by_harness["zcode"]
    # zcode money: turn1 (0.2 fresh*0.15 + 0.8 cached*0.03 + 0.1 out*0.5) = 0.104
    #              turn2 (0.6*0.15 + 2.4*0.03 + 0.3*0.5) = 0.312  → 0.416 over 2 days
    assert z.cost_usd == pytest.approx(0.416)
    assert z.cost_usd_per_day == pytest.approx(0.208)
    assert z.turns == 2 and len(z.sessions) == 2
    assert z.median_input_per_turn == 2_000_000.0  # median of 1M/3M
    o = by_harness["other-harness"]
    # other-harness money: 0.5 fresh * 0.15 + 0.01 out * 0.5 = 0.08
    assert o.cost_usd == pytest.approx(0.08)
    assert o.median_input_per_turn == 500_000.0
    assert o.median_output_per_turn == 10_000.0
    # sorted by cost desc — zcode leads
    assert report.harnesses[0].harness == "zcode"
    doc = report.to_dict()
    assert {h["harness_id"] for h in doc["by_harness"]} == {"zcode", "other-harness"}
    zdoc = next(h for h in doc["by_harness"] if h["harness_id"] == "zcode")
    assert zdoc["cost_usd_per_day"] == pytest.approx(0.208)
    assert zdoc["daily_costs"] and len(zdoc["daily_costs"]) == 1


def test_by_harness_median_even_sample_and_unknown_flag() -> None:
    turns = [
        TurnRow(
            session="a",
            ts=_ts(0.1),
            model=None,
            status="completed",
            input_tokens=100,
            cached_read_tokens=0,
            output_tokens=1,
            harness="h1",
        ),
        TurnRow(
            session="b",
            ts=_ts(0.2),
            model="GLM-5.3-Flash",
            status="completed",
            input_tokens=300,
            cached_read_tokens=0,
            output_tokens=3,
            harness="h1",
        ),
    ]
    report = build_report(
        source="test",
        turns=turns,
        session_types={},
        tariffs=TARIFFS,
        window_days=1,
        since_ts=_ts(1),
        until_ts=_ts(0),
    )
    h = report.harnesses[0]
    assert h.median_input_per_turn == 200.0  # interpolation over the even sample
    assert h.unknown_model_tokens == 100  # unpriced model flagged per harness


def test_read_sidecar_labels_harness_and_legacy_unknown(tmp_path: Path) -> None:
    """Sidecar rows carry their harness_id; wave-0 rows read as 'unknown'."""
    db = tmp_path / "m.sqlite"
    store = MetricsStore(db)
    try:
        store.record_turn_usage_batch(
            harness_id="zcode",
            session="s1",
            turns=[
                {
                    "turn_id": "t1",
                    "ts": _ts(0.01),
                    "model": "GLM-5.3-Flash",
                    "input_tokens": 1000,
                    "cached_read_tokens": 500,
                    "output_tokens": 10,
                }
            ],
        )
    finally:
        store.close()
    rows = read_sidecar_turns(db, _ts(1), _ts(0))
    assert len(rows) == 1 and rows[0].harness == "zcode"

    legacy = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE turn_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT,"
        " turn_id TEXT, ts REAL NOT NULL, status TEXT NOT NULL DEFAULT 'completed',"
        " model TEXT, input_tokens INTEGER NOT NULL DEFAULT 0,"
        " cached_read_tokens INTEGER NOT NULL DEFAULT 0,"
        " cached_write_tokens INTEGER NOT NULL DEFAULT 0,"
        " output_tokens INTEGER NOT NULL DEFAULT 0,"
        " reasoning_tokens INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO turn_usage (session, ts, input_tokens, output_tokens)"
        f" VALUES ('old', {_ts(0.01)}, 700, 7)"
    )
    conn.commit()
    conn.close()
    legacy_rows = read_sidecar_turns(legacy, _ts(1), _ts(0))
    assert len(legacy_rows) == 1 and legacy_rows[0].harness == "unknown"


def test_cli_baseline_by_harness_flag(ops_config: Path, tmp_path: Path) -> None:
    """--by-harness prints the harness cut; empty plane prints the honest note."""
    data_dir = tmp_path / "data"
    _materialize_store(data_dir / SIDECAR_FILENAME)
    # empty plane: loud note, never a table of zeros
    result = runner.invoke(
        app, ["metrics", "baseline", "--days", "1", "--by-harness", "--no-snapshot"]
    )
    assert result.exit_code == 0, result.output
    assert "no harness-attributed turns yet" in result.output

    sconn = sqlite3.connect(data_dir / SIDECAR_FILENAME)
    sconn.execute(
        "INSERT INTO turn_usage (session, ts, model, status, input_tokens,"
        " cached_read_tokens, output_tokens, harness_id, project, agent)"
        " VALUES (?, ?, 'GLM-5.3-Flash', 'completed', 2000000, 1600000, 200000,"
        " 'zcode', 'proj', 'zcode-user')",
        ("sess_z", _ts(0.01)),
    )
    sconn.commit()
    sconn.close()
    result = runner.invoke(
        app, ["metrics", "baseline", "--days", "1", "--by-harness", "--no-snapshot"]
    )
    assert result.exit_code == 0, result.output
    assert "by harness" in result.output
    assert "zcode" in result.output
    assert "USD/day" in result.output
    # snapshot carries the cut even without the stdout flag
    result = runner.invoke(app, ["metrics", "baseline", "--days", "1"])
    assert result.exit_code == 0, result.output
    snapshots = list((data_dir / "baselines").glob("baseline-*.json"))
    doc = json.loads(snapshots[-1].read_text())
    assert [h["harness_id"] for h in doc["by_harness"]] == ["zcode"]
