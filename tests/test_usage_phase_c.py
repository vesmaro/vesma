"""Phase C contract tests — the usage loop (record_usage + UsageAnalyzer).

Ported from the mnemos-vitals master suite (tests/test_usage_phase_c.py,
phase C 2026-09-29 incl. the 698a650 review fixups) — the vendored
``vesmaro.metrics`` package must carry the identical contract:

  - ``record_usage`` is the ONLY client-supplied write into the plane —
    every non-conforming shape refuses the WHOLE write loudly (warning
    + None), never a silent partial drop, never a raise into the host;
  - FK discipline: an unknown metrics_id is a loud refusal, enforced
    ATOMICALLY (conditional INSERT ... SELECT ... WHERE EXISTS — the
    M2 fix: a check-then-write race against the nightly retention job
    can never strand an immortal orphan);
  - client-supplied hostile ints (2**64-scale ids, 10**400 tokens) and
    non-finite ts degrade to loud refusals — OverflowError/ArithmeticError
    never rise into the host;
  - analytics are read-only, global, non-fatal; NO-DATA is loud and is
    never a zero;
  - the kappa gate is structural: touched_share stays informational,
    ``corridor_eligible`` is False while calibration is pending (H-K0
    verdict: touched_rate is NOT a v1 corridor metric).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from vesmaro.metrics.exposer import render_exposition
from vesmaro.metrics.sink import MetricsStore
from vesmaro.metrics.usage import (
    KAPPA_CI_FLOOR,
    KAPPA_MIN,
    KAPPA_PENDING_REASON,
    KAPPA_PREREGISTRATION,
    UsageAnalyzer,
    kappa_calibration_pending,
)


#: The frozen pre-registration doc lives in the MASTER library repo, not
#: in this vendored copy — the pin asserts reachability there.
def _find_vitals_master() -> Path | None:
    # (#436 precedent - worktree-independent): probe plausible roots instead
    # of a hardcoded absolute path that post-rebrand moves break
    candidates = (
        Path("/var/home/abyss/LABs/Projects/Project-Vesma/vesma-vitals"),
        Path(__file__).resolve().parents[4] / "vesma-vitals",
    )
    return next((c for c in candidates if (c / "pyproject.toml").exists()), None)


VITALS_MASTER = _find_vitals_master()


@pytest.fixture()
def store(tmp_path: Path) -> MetricsStore:
    s = MetricsStore(tmp_path / "metrics.sqlite")
    yield s
    s.close()


def make_assemble(store: MetricsStore, n_blocks: int = 2) -> int:
    """One assemble row with n_blocks injected blocks; returns metrics_id."""
    blocks = [
        {
            "memory_id": f"mem-{i}",
            "content_type": "note",
            "score": 0.5,
            "tokens": 50,
            "redactions": 0,
            "ccr_expanded": False,
            "ccr_hashes": [],
            "content": f"RAW BLOCK CONTENT {i}",
        }
        for i in range(n_blocks)
    ]
    mid = store.record_assemble(
        {
            "session": "sess-u",
            "project": "demo",
            "agent": "gcw-tech-lead",
            "mode": "sync",
            "text": "assembled window",
            "blocks": blocks,
            "tokens": {"budget": 1000, "estimated": 100},
            "stats": {},
        }
    )
    assert mid is not None
    return mid


def usage_rows(store: MetricsStore) -> list[sqlite3.Row]:
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM usage_reports ORDER BY id").fetchall()
    conn.close()
    return rows


class TestRecordUsage:
    def test_roundtrip(self, store: MetricsStore):
        mid = make_assemble(store, n_blocks=3)
        rid = store.record_usage(
            mid,
            block_ids_touched=[f"{mid}:0", f"{mid}:2"],
            tokens_out=42,
            wrong_tool_flag=True,
            ts=123.5,
        )
        assert isinstance(rid, int) and rid > 0
        rows = usage_rows(store)
        assert len(rows) == 1
        row = rows[0]
        assert row["metrics_id"] == mid
        assert json.loads(row["block_ids_touched_json"]) == [f"{mid}:0", f"{mid}:2"]
        assert row["tokens_out"] == 42
        assert row["wrong_tool_flag"] == 1  # bool stored as INTEGER 1/0

    def test_defaults_and_zero_boundary(self, store: MetricsStore):
        mid = make_assemble(store)
        assert store.record_usage(mid, block_ids_touched=[], tokens_out=0) is not None
        row = usage_rows(store)[0]
        assert json.loads(row["block_ids_touched_json"]) == []  # touched nothing: legit
        assert row["tokens_out"] == 0  # 0 is a value, not absence
        assert row["wrong_tool_flag"] == 0

    def test_dedup_preserves_order(self, store: MetricsStore):
        mid = make_assemble(store)
        store.record_usage(mid, block_ids_touched=["b", "a", "b", "c", "a"])
        stored = json.loads(usage_rows(store)[0]["block_ids_touched_json"])
        assert stored == ["b", "a", "c"]

    def test_boundary_256_entries_accepted(self, store: MetricsStore):
        mid = make_assemble(store)
        ids = [f"blk-{i:04d}" for i in range(256)]
        assert store.record_usage(mid, block_ids_touched=ids) is not None

    def test_no_ts_column_born_final(self, store: MetricsStore):
        """ts is accepted for call-site symmetry but never stored."""
        mid = make_assemble(store)
        store.record_usage(mid, block_ids_touched=["x"], ts=1.0)
        conn = sqlite3.connect(store.db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(usage_reports)")]
        conn.close()
        assert "ts" not in cols  # born-final pin (canary C1) holds


class TestUsageRefusals:
    """Every refusal: None returned, loud warning, zero rows written."""

    @pytest.fixture(autouse=True)
    def _parent(self, store: MetricsStore):
        self.mid = make_assemble(store, n_blocks=4)

    def _assert_refused(self, store: MetricsStore, caplog, result):
        assert result is None
        assert "record_usage failed" in caplog.text  # loud, never silent
        assert usage_rows(store) == []  # WHOLE write refused, no partial row

    def test_unknown_metrics_id_loud_refusal(self, store: MetricsStore, caplog):
        caplog.set_level(logging.WARNING, logger="vesmaro.metrics.sink")
        self._assert_refused(
            store, caplog, store.record_usage(self.mid + 999, block_ids_touched=["x"])
        )

    @pytest.mark.parametrize("bad_id", ["5", 5.0, True, None, 0, -1])
    def test_bad_metrics_id(self, store: MetricsStore, caplog, bad_id):
        caplog.set_level(logging.WARNING, logger="vesmaro.metrics.sink")
        self._assert_refused(store, caplog, store.record_usage(bad_id, block_ids_touched=["x"]))

    @pytest.mark.parametrize("bad", ["b:0", ("b:0",), {"b:0"}, None, 5])
    def test_non_list_touched(self, store: MetricsStore, caplog, bad):
        caplog.set_level(logging.WARNING, logger="vesmaro.metrics.sink")
        self._assert_refused(store, caplog, store.record_usage(self.mid, block_ids_touched=bad))

    @pytest.mark.parametrize(
        "bad_entries",
        [
            [123],  # non-string entry
            [None],
            [""],  # empty string identifies no block
            ["x" * 129],  # over 128 chars
            ["a\nb"],  # newline
            ["a\rb"],  # carriage return
            ["ok", 7],  # one bad entry poisons the whole write
        ],
    )
    def test_bad_entries(self, store: MetricsStore, caplog, bad_entries):
        caplog.set_level(logging.WARNING, logger="vesmaro.metrics.sink")
        refused = store.record_usage(self.mid, block_ids_touched=bad_entries)
        self._assert_refused(store, caplog, refused)

    def test_too_many_entries(self, store: MetricsStore, caplog):
        caplog.set_level(logging.WARNING, logger="vesmaro.metrics.sink")
        ids = [f"b-{i}" for i in range(257)]
        self._assert_refused(store, caplog, store.record_usage(self.mid, block_ids_touched=ids))

    @pytest.mark.parametrize("bad_tokens", [-1, 1.5, "5", True])
    def test_bad_tokens_out(self, store: MetricsStore, caplog, bad_tokens):
        caplog.set_level(logging.WARNING, logger="vesmaro.metrics.sink")
        refused = store.record_usage(self.mid, block_ids_touched=["x"], tokens_out=bad_tokens)
        self._assert_refused(store, caplog, refused)

    @pytest.mark.parametrize("bad_flag", [1, 0, "yes", None])
    def test_bad_wrong_tool_flag(self, store: MetricsStore, caplog, bad_flag):
        caplog.set_level(logging.WARNING, logger="vesmaro.metrics.sink")
        self._assert_refused(
            store,
            caplog,
            store.record_usage(self.mid, block_ids_touched=["x"], wrong_tool_flag=bad_flag),
        )

    @pytest.mark.parametrize("bad_ts", ["now", float("nan"), float("inf")])
    def test_bad_ts(self, store: MetricsStore, caplog, bad_ts):
        caplog.set_level(logging.WARNING, logger="vesmaro.metrics.sink")
        self._assert_refused(
            store, caplog, store.record_usage(self.mid, block_ids_touched=["x"], ts=bad_ts)
        )

    @pytest.mark.parametrize(
        "hostile",
        [
            {"metrics_id": 2**64, "tokens_out": 10},
            {"metrics_id": 1, "tokens_out": 10**400},
            {"metrics_id": 1, "tokens_out": 1, "ts": 10**400},
        ],
    )
    def test_hostile_ints_degrade_never_raise(self, store: MetricsStore, caplog, hostile):
        """Reviewer repros (master 698a650 MAJOR): client ints must degrade
        to loud refusals — OverflowError at bind never rises into the host."""
        caplog.set_level(logging.WARNING, logger="vesmaro.metrics.sink")
        refused = store.record_usage(
            hostile["metrics_id"],
            block_ids_touched=["x"],
            tokens_out=hostile.get("tokens_out"),
            ts=hostile.get("ts"),
        )
        self._assert_refused(store, caplog, refused)


class TestAtomicFK:
    """M2: validation and write are ONE statement — the retention race."""

    def test_atomic_select_where_exists_no_orphan_on_vanished_parent(
        self, store: MetricsStore, caplog
    ):
        caplog.set_level(logging.WARNING, logger="vesmaro.metrics.sink")
        mid = make_assemble(store)
        conn = sqlite3.connect(store.db_path)
        conn.execute("DELETE FROM assemble_metrics WHERE id = ?", (mid,))
        conn.commit()  # simulate retention deleting the parent mid-flight
        conn.close()
        assert store.record_usage(mid, block_ids_touched=["x"]) is None
        assert usage_rows(store) == []  # no immortal orphan stranded


class TestNonFatal:
    def test_closed_store_returns_none(self, tmp_path: Path):
        s = MetricsStore(tmp_path / "metrics.sqlite")
        s.close()
        assert s.record_usage(1, block_ids_touched=["x"]) is None  # never raises

    def test_analyzer_never_raises_into_host(self, tmp_path: Path):
        s = MetricsStore(tmp_path / "metrics.sqlite")
        s.close()
        a = UsageAnalyzer(s)
        for report in (a.assemble_usage_rate(), a.touched_share(), a.wrong_tool_rate()):
            assert report["status"] == "NO-DATA"
            assert report["reasons"][0] == "analyzer degraded: RuntimeError"
        # the touched_share degradation ALSO carries the frozen-prereg reason
        # (review nit): the operator must see why the gate is soft-locked
        a2 = UsageAnalyzer(s)
        ts = a2.touched_share()
        assert any("kappa" in r for r in ts["reasons"])
        s.close()  # idempotent


class TestUsageAnalytics:
    def test_assemble_usage_rate_exact(self, store: MetricsStore):
        mids = [make_assemble(store) for _ in range(3)]
        store.record_usage(mids[0], block_ids_touched=[f"{mids[0]}:0"])
        store.record_usage(mids[2], block_ids_touched=[f"{mids[2]}:1"])
        report = UsageAnalyzer(store).assemble_usage_rate()
        assert report["status"] == "OK"
        assert report["assemble_calls"] == 3
        assert report["closed_calls"] == 2
        assert report["assemble_usage_rate"] == pytest.approx(2 / 3)

    def test_assemble_usage_rate_no_data(self, store: MetricsStore):
        report = UsageAnalyzer(store).assemble_usage_rate()
        assert report["status"] == "NO-DATA"
        assert report["assemble_usage_rate"] is None  # never zero without data
        assert report["reasons"]

    def test_touched_share_exact_math(self, store: MetricsStore):
        a = make_assemble(store, n_blocks=4)  # touch 1 of 4 -> 0.25
        b = make_assemble(store, n_blocks=2)  # touch 2 of 2 -> 1.00
        store.record_usage(a, block_ids_touched=[f"{a}:0"])
        store.record_usage(b, block_ids_touched=[f"{b}:0", f"{b}:1"])
        report = UsageAnalyzer(store).touched_share()
        assert report["status"] == "OK"
        assert report["reports"] == 2
        assert report["calls_measured"] == 2
        assert report["touched_share"] == pytest.approx((0.25 + 1.0) / 2)

    def test_touched_share_real_zero_is_ok(self, store: MetricsStore):
        """A report touching nothing is a REAL zero, not NO-DATA."""
        mid = make_assemble(store, n_blocks=2)
        store.record_usage(mid, block_ids_touched=[])
        report = UsageAnalyzer(store).touched_share()
        assert report["status"] == "OK"
        assert report["touched_share"] == 0.0

    def test_touched_share_no_reports_loud_no_data(self, store: MetricsStore):
        make_assemble(store)  # assemble exists, reports do not
        report = UsageAnalyzer(store).touched_share()
        assert report["status"] == "NO-DATA"
        assert report["touched_share"] is None
        assert report["reports"] == 0
        assert "no usage reports" in " ".join(report["reasons"])

    def test_touched_share_skips_empty_assembly(self, store: MetricsStore):
        """0/0 (nothing injected) is undefined — skipped, never counted as 0."""
        a = make_assemble(store, n_blocks=2)
        empty_mid = make_assemble(store, n_blocks=0)
        store.record_usage(a, block_ids_touched=[f"{a}:1"])
        store.record_usage(empty_mid, block_ids_touched=[])
        report = UsageAnalyzer(store).touched_share()
        assert report["skipped_no_blocks"] == 1
        assert report["calls_measured"] == 1
        assert report["touched_share"] == pytest.approx(0.5)
        assert report["status"] == "OK"

    def test_touched_share_garbage_json_skipped(self, store: MetricsStore):
        mid = make_assemble(store, n_blocks=2)
        conn = sqlite3.connect(store.db_path)
        conn.execute(
            "INSERT INTO usage_reports (metrics_id, block_ids_touched_json, tokens_out,"
            " wrong_tool_flag) VALUES (?,?,?,?)",
            (mid, "not-json{", 0, 0),
        )
        conn.commit()
        conn.close()
        report = UsageAnalyzer(store).touched_share()
        assert report["skipped_unreadable"] == 1
        assert report["calls_measured"] == 0
        assert report["status"] == "NO-DATA"  # no measurable reports — loud, not 0.0
        assert report["touched_share"] is None

    def test_wrong_tool_rate_exact(self, store: MetricsStore):
        mids = [make_assemble(store) for _ in range(3)]
        store.record_usage(mids[0], block_ids_touched=[], wrong_tool_flag=True)
        store.record_usage(mids[1], block_ids_touched=[])
        store.record_usage(mids[2], block_ids_touched=[])
        report = UsageAnalyzer(store).wrong_tool_rate()
        assert report["status"] == "OK"
        assert report["reports"] == 3
        assert report["wrong_tool_calls"] == 1
        assert report["wrong_tool_rate"] == pytest.approx(1 / 3)

    def test_wrong_tool_rate_no_data(self, store: MetricsStore):
        report = UsageAnalyzer(store).wrong_tool_rate()
        assert report["status"] == "NO-DATA"
        assert report["wrong_tool_rate"] is None
        assert report["reasons"]


class TestKappaGate:
    def test_block_id_boundary_128_accepted_129_refused(self, store):
        """The 128-char id limit is exact: 128 accepted, 129 refused."""
        mid = make_assemble(store)
        assert mid is not None
        assert store.record_usage(mid, block_ids_touched=["x" * 128]) is not None
        assert store.record_usage(mid, block_ids_touched=["x" * 129]) is None

    def test_frozen_preregistration_constants(self):
        assert kappa_calibration_pending() is True
        assert KAPPA_MIN == 0.6
        assert KAPPA_CI_FLOOR == 0.5
        assert KAPPA_PREREGISTRATION == "docs/experiments/touched-rate-kappa.md"
        # the frozen doc lives in the MASTER library repo — reachability is
        # a dev-environment concern; the H-K0 verdict (2026-09-29) keeps the
        # gate True under that pre-registration (a flip needs a new one)
        if VITALS_MASTER is None:
            pytest.skip("vitals master clone absent (doc pin lives there)")
        doc = (VITALS_MASTER / KAPPA_PREREGISTRATION).read_text(encoding="utf-8")
        assert "0.6" in doc and "0.5" in doc, "frozen thresholds missing from the doc"

    def test_corridor_eligible_false_while_pending(self, store: MetricsStore):
        mid = make_assemble(store, n_blocks=2)
        store.record_usage(mid, block_ids_touched=[f"{mid}:0"])
        report = UsageAnalyzer(store).touched_share()
        assert report["kappa_calibration_pending"] is True
        assert report["corridor_eligible"] is False  # structural: corridors closed
        assert KAPPA_PENDING_REASON in report["reasons"]

    def test_no_data_paths_also_corridor_closed(self, store: MetricsStore):
        report = UsageAnalyzer(store).touched_share()
        assert report["corridor_eligible"] is False
        assert report["status"] == "NO-DATA"

    def test_analytics_carry_no_principal_columns(self, store: MetricsStore):
        mid = make_assemble(store)
        store.record_usage(mid, block_ids_touched=[f"{mid}:0"])
        a = UsageAnalyzer(store)
        for report in (a.assemble_usage_rate(), a.touched_share(), a.wrong_tool_rate()):
            assert not ({"session", "principal", "session_id", "user"} & set(report))


class TestUsageExposition:
    """Phase C integration: the usage plane reaches Prometheus.

    RL-S2 discipline on the new family: the usage plane carries NO
    project/endpoint/detector/principal labels anywhere — the source
    tables have no such columns, and the render must never invent
    aggregates bearing them. NO-DATA is rendered as series ABSENCE,
    never as 0. The kappa gate is structural: the touched series must
    not be scrapable while calibration is pending.
    """

    def test_counts_and_rates_global(self, store: MetricsStore):
        mids = [make_assemble(store) for _ in range(3)]
        store.record_usage(mids[0], block_ids_touched=[f"{mids[0]}:0"], tokens_out=120)
        store.record_usage(mids[1], block_ids_touched=[], wrong_tool_flag=True)
        text = render_exposition(store)
        assert "# TYPE mnemos_usage_loop_rate gauge" in text
        assert "mnemos_usage_assemble_calls_total 3" in text
        assert "mnemos_usage_closed_calls_total 2" in text
        assert f"mnemos_usage_loop_rate {2 / 3}" in text
        assert "mnemos_usage_reports_total 2" in text
        assert "mnemos_usage_wrong_tool_rate 0.5" in text
        assert "mnemos_usage_tokens_out_total 120" in text

    def test_report_driven_rates_exact(self, store: MetricsStore):
        """wrong_tool_rate counts per REPORT, not per assemble call."""
        mids = [make_assemble(store) for _ in range(4)]
        store.record_usage(mids[0], block_ids_touched=[], wrong_tool_flag=True)
        store.record_usage(mids[1], block_ids_touched=[], wrong_tool_flag=True)
        store.record_usage(mids[2], block_ids_touched=[])
        text = render_exposition(store)
        assert "mnemos_usage_reports_total 3" in text
        assert "mnemos_usage_wrong_tool_rate 0.6666666666666666" in text

    def test_no_assemble_calls_family_absent_not_zero(self, store: MetricsStore):
        text = render_exposition(store)
        assert "mnemos_usage_loop_rate " not in text  # absent, never 0.0
        assert "mnemos_usage_assemble_calls_total " not in text
        assert (
            "mnemos_usage"
            not in text.split("# HELP mnemos_verb_calls_total")[1].split(
                "# HELP mnemos_usage_loop_rate"
            )[0]
        )  # verb family unchanged in front of the usage block

    def test_assemble_without_reports_loop_series_only(self, store: MetricsStore):
        make_assemble(store)  # assemble exists, no harness responses
        text = render_exposition(store)
        assert "mnemos_usage_loop_rate 0.0" in text  # real zero: 0 closed of 1 call
        assert "mnemos_usage_closed_calls_total 0" in text
        assert "mnemos_usage_reports_total" not in text  # no reports -> no rate
        assert "mnemos_usage_wrong_tool_rate" not in text

    def test_touched_series_absent_while_kappa_pending(self, store: MetricsStore):
        """The structural kappa gate: informational signal is NOT scrapable."""
        mid = make_assemble(store, n_blocks=2)
        store.record_usage(mid, block_ids_touched=[f"{mid}:0"], tokens_out=10)
        text = render_exposition(store)
        assert kappa_calibration_pending() is True
        assert "mnemos_usage_touched_share" not in text
        assert "touched" not in text  # no series, no HELP line — nothing to promote

    def test_usage_family_absent_on_broken_sidecar(self, tmp_path: Path):
        s = MetricsStore(tmp_path / "metrics.sqlite")
        s.close()
        assert render_exposition(s) == ""  # empty scrape, never an exception

    def test_no_labels_of_any_kind_in_usage_family(self, store: MetricsStore):
        """RL-S2: zero label braces in the usage family (projectless globals)."""
        mid = make_assemble(store)  # project="demo" recorded on the assemble row
        store.record_usage(mid, block_ids_touched=[f"{mid}:0"])
        text = render_exposition(store)
        usage_family = text.split("# HELP mnemos_usage_loop_rate")[1]
        assert "demo" not in usage_family  # bearing verb never leaks
        assert "{" not in usage_family and "}" not in usage_family

    def test_usage_gauges_render_before_host_volume_gauges(self, store: MetricsStore):
        make_assemble(store)
        text = render_exposition(store, gauges={"memories_total": 7})
        usage_pos = text.find("mnemos_usage_loop_rate")
        gauges_pos = text.find("memories_total 7")
        assert 0 < usage_pos < gauges_pos  # appended after verb planes
