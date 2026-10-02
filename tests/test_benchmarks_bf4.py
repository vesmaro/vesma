"""BF-4 tail tests — S2 nightly semantics, the owner report page, the
192-query corpus (ADR-0020 §5, epic #169).

Covers:

* **queries corpus** — exactly 192 entries (the ADR-0020 McNemar
  activation threshold), unique qids, every judged slug exists in the
  corpus AND is admissible (a judgment against a ``raw`` entry is
  permanently unwinnable — mislabeling guard), the four BF-4 query
  families are present, and the recorded baselines were re-recorded on
  THIS corpus (live fingerprints match, judged count = 191);
* **S2 nightly analysis** — the pure ``analyze_nightly`` traffic light:
  a tight band PASSes, a tight band beyond the baseline ceiling is a
  REGRESSION (exit 1 — the nightly gate role), a noise band wider than
  the corridor is NOISE (exit 0 — de-escalated to report + ticket per
  ADR-0020 §5, never a block); ``--record-nightly`` refuses R<3 and an
  overwrite without ``--force``; the baseline payload carries the
  workload fingerprint;
* **the one-page report** — generation from the real baselines produces
  a valid page with every family F1-F7; traffic-light semantics on
  synthetic JSON: a breached corridor / invariant is RED (and the
  generator exits 1), a skipped S1m is YELLOW, a missing S2 baseline is
  YELLOW; a run report older than its baseline is stale and ignored;
* **Makefile wiring** — ``bench-s2-nightly`` presets
  VESMA_BENCH_S1M_REQUIRED=1 (N4 on #206: the nightly contour is the
  only place where required-S1m semantics is mandatory) and runs full
  repeats; ``bench-report`` exists; neither enters ``make verify``
  (S2/S3/S4 stay nightly-class, S1 stays the only local gate).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from benchmarks.corpus.corpus import CORPUS, NON_ADMISSIBLE_SLUGS, PROJECTS
from benchmarks.corpus.queries import GOLDEN_QUERIES
from benchmarks.report_page import (
    evaluate_f1,
    evaluate_f2,
    evaluate_f4,
    load_baselines,
    load_fresh_reports,
)
from benchmarks.report_page import (
    main as report_main,
)
from benchmarks.stands.s1_quality import run as s1_run
from benchmarks.stands.s2_timing import run as s2_run
from benchmarks.stands.s4_availability import run as s4_run

ROOT = Path(s1_run.__file__).resolve().parents[3]
MAKEFILE = ROOT / "Makefile"


# ── the 192-query corpus ─────────────────────────────────────────────────────


class TestQueriesCorpus:
    def test_corpus_is_exactly_192_with_family_split(self) -> None:
        assert len(GOLDEN_QUERIES) == 192, (
            f"ADR-0020 pins the McNemar activation corpus at ~192 — got {len(GOLDEN_QUERIES)}"
        )
        judged = [q for q in GOLDEN_QUERIES if q.expected]
        probes = [q for q in GOLDEN_QUERIES if q.expect_no_results]
        assert len(judged) == 191 and len(probes) == 1

    def test_qids_unique(self) -> None:
        qids = [q.qid for q in GOLDEN_QUERIES]
        assert len(qids) == len(set(qids))

    def test_bf4_families_present(self) -> None:
        families = {
            "ph": sum(1 for q in GOLDEN_QUERIES if "-ph" in q.qid),
            "pr": sum(1 for q in GOLDEN_QUERIES if "-pr" in q.qid),
            "tp": sum(1 for q in GOLDEN_QUERIES if "-tp" in q.qid),
            "xr": sum(1 for q in GOLDEN_QUERIES if "-xr" in q.qid),
        }
        assert families["ph"] >= 60 and families["pr"] >= 30, families
        assert families["tp"] >= 10 and families["xr"] >= 15, families
        assert sum(families.values()) == 144  # 48 W4 + 144 BF-4 = 192

    def test_every_judged_slug_exists_and_is_admissible(self) -> None:
        slugs = {e.slug for e in CORPUS}
        for q in GOLDEN_QUERIES:
            for slug in q.expected:
                assert slug in slugs, f"{q.qid} judges unknown slug {slug}"
                assert slug not in NON_ADMISSIBLE_SLUGS, (
                    f"{q.qid} judges non-admissible (raw) slug {slug} — a "
                    "raw entry never surfaces; the judgment is unwinnable"
                )

    def test_projects_valid(self) -> None:
        for q in GOLDEN_QUERIES:
            assert q.project is None or q.project in PROJECTS, q.qid

    def test_baselines_rerecorded_on_this_corpus(self) -> None:
        """The canonical baselines must match the corpus on disk."""
        s1 = json.loads(s1_run.BASELINE_PATH.read_text())
        assert s1["corpus_fingerprint"] == s1_run.corpus_fingerprint()
        assert s1["metrics"]["retrieval"]["judged_queries"] == 191
        s4 = json.loads(s4_run.BASELINE_PATH.read_text())
        assert s4["corpus_fingerprint"] == s4_run.corpus_fingerprint()


# ── S2 nightly: noise band → NOISE / PASS / REGRESSION ───────────────────────


def _repeat(p50: float, jitter: float = 0.0) -> list[dict]:
    """One synthetic nightly repeat; search carries the jitter axis."""
    one = {
        "add": {"n": 100, "p50_ms": 1.0, "p95_ms": 2.0},
        "search": {"n": 100, "p50_ms": p50, "p95_ms": p50 * 2},
        "assemble": {"n": 100, "p50_ms": 2.0, "p95_ms": 4.0},
        "refine_single": {"n": 100, "p50_ms": 0.5, "p95_ms": 1.0},
    }
    return (
        [dict(one) for _ in range(3)]
        if jitter == 0
        else [
            {**one, "search": {"n": 100, "p50_ms": p50 + d, "p95_ms": (p50 + d) * 2}}
            for d in (0.0, jitter, jitter / 2)
        ]
    )


_BASELINE_METRICS = {
    "verbs": {
        "add": {"p50_ms": 0.9, "p95_ms": 1.8},
        "search": {"p50_ms": 1.4, "p95_ms": 2.8},
        "assemble": {"p50_ms": 1.9, "p95_ms": 3.8},
        "refine_single": {"p50_ms": 0.45, "p95_ms": 0.9},
    }
}


class TestS2NightlyAnalysis:
    def test_tight_band_no_baseline_passes(self) -> None:
        out = s2_run.analyze_nightly(_repeat(1.5), None)
        assert out["overall"] == "PASS" and out["exit_code"] == 0

    def test_tight_band_within_ceiling_passes(self) -> None:
        out = s2_run.analyze_nightly(_repeat(1.6), _BASELINE_METRICS)  # 1.14x
        assert out["overall"] == "PASS" and out["exit_code"] == 0

    def test_tight_breach_is_regression_exit_1(self) -> None:
        out = s2_run.analyze_nightly(_repeat(2.9), _BASELINE_METRICS)  # >2x
        assert out["overall"] == "REGRESSION" and out["exit_code"] == 1
        assert out["verbs"]["search"]["status"] == "REGRESSION"

    def test_wide_band_is_noise_deescalated_exit_0(self) -> None:
        # median 1.5 breaches nothing anyway, but the band 1.0→2.0 is 67%
        out = s2_run.analyze_nightly(_repeat(1.5, jitter=1.0), _BASELINE_METRICS)
        assert out["verbs"]["search"]["status"] == "NOISE"
        assert out["exit_code"] == 0  # de-escalated to report + ticket (ADR-0020 §5)

    def test_noise_on_one_verb_does_not_veto_regression_on_another(self) -> None:
        """Per-verb independence: a noisy verb de-escalates ITS comparison,
        not a tight-band regression on a different verb."""
        repeats: list[dict] = []
        for search_p50 in (1.4, 3.9, 2.6):  # band 2.5 / median 2.6 → NOISE
            repeats.append(
                {
                    "add": {"n": 100, "p50_ms": 1.5, "p95_ms": 3.0},  # 1.67x, tight
                    "search": {"n": 100, "p50_ms": search_p50, "p95_ms": search_p50 * 2},
                    "assemble": {"n": 100, "p50_ms": 1.9, "p95_ms": 3.8},
                    "refine_single": {"n": 100, "p50_ms": 0.45, "p95_ms": 0.9},
                }
            )
        out = s2_run.analyze_nightly(repeats, _BASELINE_METRICS)
        assert out["verbs"]["search"]["status"] == "NOISE"
        assert out["verbs"]["add"]["status"] == "REGRESSION"
        assert out["overall"] == "REGRESSION" and out["exit_code"] == 1

    def test_record_nightly_requires_three_repeats(self) -> None:
        with pytest.raises(SystemExit) as exc:
            s2_run.main(["--repeats", "1", "--record-nightly"])
        assert exc.value.code == 2  # argparse error

    def test_baseline_payload_carries_workload_fingerprint(self) -> None:
        report = {
            "created": "2026-09-03T00:00:00+00:00",
            "repeats": 3,
            "ops_per_repeat": 250,
            "environment": {"python": "3.12"},
        }
        analysis = s2_run.analyze_nightly(_repeat(1.5), None)
        payload = s2_run.build_nightly_baseline(report, analysis)
        fp = payload["workload_fingerprint"]
        assert len(fp) == 64 and int(fp, 16) >= 0
        assert set(payload["metrics"]["verbs"]) == set(s2_run.VERBS)


# ── the one-page report ──────────────────────────────────────────────────────


def _synth_baselines(tmp: Path, *, s1m_status: str = "measured", breach: str | None = None) -> Path:
    """A minimal but schema-faithful baselines dir for traffic-light tests."""
    ok_inv = {"value": 1.0, "expect": 1.0, "ok": True}
    zero_inv = {"value": 0, "expect": 0, "ok": True}
    breached = (
        {"value": 0.5, "expect": 1.0, "ok": False} if breach == "injection_acceptance" else ok_inv
    )
    dup_breached = (
        {"value": 3, "expect": 0, "ok": False}
        if breach == "duplicate_occurrences_at_10"
        else zero_inv
    )
    s1m = (
        {"status": "skipped", "reason": "synthetic no-provider"}
        if s1m_status == "skipped"
        else {
            "status": "measured",
            "metrics": {"recall_at_10": 0.94, "mrr": 0.95},
        }
    )
    baselines = tmp / "baselines"
    baselines.mkdir()
    (baselines / "s1.json").write_text(
        json.dumps(
            {
                "baseline_version": 1,
                "stand_version": "s1-1",
                "created": "2026-09-03T00:00:00+00:00",
                "metrics": {
                    "retrieval": {
                        "precision_at_5": 0.23,
                        "recall_at_10": 0.89,
                        "judged_queries": 191,
                    },
                    "invariants": {
                        "injection_acceptance": breached,
                        "non_admissible_surfaced": zero_inv,
                        "foreign_project_surfaced": zero_inv,
                        "duplicate_occurrences_at_5": zero_inv,
                        "duplicate_occurrences_at_10": dup_breached,
                        "quarantine_exclusion": ok_inv,
                        "render_neutrality": zero_inv,
                        "rewrite_redemption_leaks": zero_inv,
                    },
                    "rewrite": {"hit_rate": 0.94, "regret_rate": 0.25},
                    "a9": {"delta_recall10_current_vs_pre_a9": -0.016},
                    "s1m": s1m,
                },
            }
        )
    )
    (baselines / "s3.json").write_text(
        json.dumps(
            {
                "created": "2026-09-03T00:00:00+00:00",
                "metrics": {
                    "fact_retention": {"overall": {"rate": 1.0, "hits": 53, "probes": 53}},
                    "recall_drift": {"delta": 0.0},
                    "checkpoint_return_integrity": {"value": 1.0, "expect": 1.0, "ok": True},
                    "sufficiency_at_task": {"rate": 0.93},
                    "context_growth": {"factor": 1.0},
                    "stage_discard_profile": {
                        "assembles": 40,
                        "scan_blocks_refused": 0,
                        "budget_blocks_skipped": 0,
                    },
                },
            }
        )
    )
    (baselines / "s4.json").write_text(
        json.dumps(
            {
                "created": "2026-09-03T00:00:00+00:00",
                "metrics": {
                    "probe_pass_rate": 1.0,
                    "memory_completeness": 1.0,
                    "quarantine_exclusion": {"value": 1.0, "expect": 1.0, "ok": True},
                    "embed_staleness": {"stale": 0, "checked_refined": 0},
                    "read_only_invariant": {"ok": True},
                },
            }
        )
    )
    return baselines


class TestReportPage:
    def test_generates_from_real_baselines(self, tmp_path: Path) -> None:
        out = tmp_path / "latest.md"
        code = report_main(["--out", str(out)])
        assert code == 0, "the real (re-recorded) baselines must page green"
        page = out.read_text()
        for fid in ("F1", "F2", "F3", "F4", "F5", "F6", "F7"):
            assert f"## {fid}" in page, f"family {fid} section missing"
        assert "= 1.0000 (required = 1.0000) — OK" in page  # invariant line format
        assert "191 judged queries" in page  # new corpus count on the page
        snapshot = json.loads((tmp_path / "latest-prev.json").read_text())
        assert set(snapshot["headlines"]) == {"F1", "F2", "F3", "F4", "F5", "F6", "F7"}

    def test_real_page_f1_yellow_no_s2_baseline(self) -> None:
        baselines = load_baselines(ROOT / "benchmarks" / "baselines")
        assert "s2" not in baselines  # born on the nightly machine only
        f1 = evaluate_f1(baselines, {})
        assert f1["light"] == "yellow"

    def test_breached_invariant_is_red(self, tmp_path: Path) -> None:
        baselines = load_baselines(_synth_baselines(tmp_path, breach="injection_acceptance"))
        f2 = evaluate_f2(baselines, {})
        assert f2["light"] == "red"
        f4 = evaluate_f4(baselines, {})
        assert f4["light"] == "green"

    def test_breached_corridor_in_report_is_red_and_exits_1(self, tmp_path: Path) -> None:
        baselines_dir = _synth_baselines(tmp_path)
        reports = tmp_path / "reports"
        reports.mkdir()
        # a run report NEWER than the baseline, carrying a corridor breach
        (reports / "s1-20260903T010000_0000.json").write_text(
            json.dumps(
                {
                    "created": "2026-09-03T01:00:00+00:00",
                    "metrics": {},
                    "gate": {
                        "pass": False,
                        "failures": ["recall_at_10=0.7100 below corridor 0.8500"],
                    },
                }
            )
        )
        baselines = load_baselines(baselines_dir)
        fresh = load_fresh_reports(reports, baselines)
        assert evaluate_f2(baselines, fresh)["light"] == "red"
        code = report_main(
            [
                "--baselines",
                str(baselines_dir),
                "--reports",
                str(reports),
                "--out",
                str(tmp_path / "page.md"),
            ]
        )
        assert code == 1

    def test_duplicate_breach_reddens_f4(self, tmp_path: Path) -> None:
        baselines = load_baselines(_synth_baselines(tmp_path, breach="duplicate_occurrences_at_10"))
        assert evaluate_f4(baselines, {})["light"] == "red"

    def test_skipped_s1m_is_yellow_not_red(self, tmp_path: Path) -> None:
        baselines = load_baselines(_synth_baselines(tmp_path, s1m_status="skipped"))
        f2 = evaluate_f2(baselines, {})
        assert f2["light"] == "yellow"
        assert "SKIPPED" in "\n".join(f2["lines"])

    def test_stale_report_older_than_baseline_is_ignored(self, tmp_path: Path) -> None:
        baselines_dir = _synth_baselines(tmp_path)
        reports = tmp_path / "reports"
        reports.mkdir()
        # created BEFORE the synthetic baseline (2026-09-03T00:00) — stale
        (reports / "s1-20260902T000000_0000.json").write_text(
            json.dumps(
                {
                    "created": "2026-09-02T00:00:00+00:00",
                    "metrics": {},
                    "gate": {"pass": False, "failures": ["stale breach"]},
                }
            )
        )
        baselines = load_baselines(baselines_dir)
        fresh = load_fresh_reports(reports, baselines)
        assert "s1" not in fresh  # superseded by the re-record
        assert evaluate_f2(baselines, fresh)["light"] == "green"


# ── Makefile wiring ──────────────────────────────────────────────────────────


class TestMakefileWiring:
    def test_bench_s2_nightly_presets_s1m_required(self) -> None:
        """N4 on #206: the nightly contour presets VESMA_BENCH_S1M_REQUIRED=1.

        The env var is load-bearing because the target also runs the S1
        gate leg (in required posture) before S2 — a bare S2-only target
        would make the preset dead weight.
        """
        makefile = MAKEFILE.read_text()
        target = makefile.split("bench-s2-nightly:", 1)[1].split("\n\n", 1)[0]
        assert "VESMA_BENCH_S1M_REQUIRED=1" in target
        assert "s1_quality/run.py" in target  # the flag has a consumer
        assert "--repeats $(S2_REPEATS)" in target

    def test_bench_report_target_exists(self) -> None:
        assert "bench-report:" in MAKEFILE.read_text()
        assert "bench-report" in makefile_phony()

    def test_verify_stays_local_s1_only(self) -> None:
        """S2 nightly / S3 / S4 must NOT enter make verify (ADR-0020)."""
        makefile = MAKEFILE.read_text()
        verify = makefile.split("verify:", 1)[1].split("\n\n", 1)[0]
        assert "bench-s1" in verify
        for absent in ("bench-s2", "bench-s3", "bench-s4", "bench-report"):
            assert absent not in verify, f"{absent} leaked into make verify"

    def test_bench_s2_smoke_untouched(self) -> None:
        assert "bench-s2-smoke:" in MAKEFILE.read_text()


def makefile_phony() -> str:
    return MAKEFILE.read_text().split(".PHONY:", 1)[1].split("\n", 1)[0]
