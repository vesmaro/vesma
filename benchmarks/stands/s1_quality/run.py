#!/usr/bin/env python
"""S1 quality stand — single-command runner (ADR-0020, wave BF-1).

Usage (from the repository root):

    python benchmarks/stands/s1_quality/run.py            # gate mode
    python benchmarks/stands/s1_quality/run.py --record   # (re)write the baseline
    make bench-s1                                          # same, via make

Gate mode measures everything, compares against
``benchmarks/baselines/s1.json`` (corridors + invariants per ADR-0020
§Gate policy) and exits non-zero on any breach. ``--record`` rewrites
the canonical baseline JSON and regenerates ``BASELINE.md`` from it
(the JSON is the source of truth; the markdown is a generated summary).

Deterministic by construction: BLAKE2b lexical embedder, fixed corpus
order, no wall-clock in any measured value, no RNG, no network. The
``created`` field is run metadata, not a metric.

S1m (ADR-0021 NM-0): the SAME deterministic run additionally measures
the PRODUCTION embedder (the shipped default provider) over the same
judged corpus — recall/precision@k, MRR, nDCG@k — as its OWN section
(``s1m``) with a ``model_fingerprint`` field pinning provider + model +
weights sha256 + opset. The BLAKE2b reference stays the only source for
the pipeline corridors; the S1m corridor is self-comparison (its own
baseline - max(0.02; 95% CI)). An embedder change without a same-PR
``--record`` fails LOUD. If the production provider cannot be built in
the run environment, S1m reports ``status: "skipped"`` with the reason
(green by default; red when ``VESMA_BENCH_S1M_REQUIRED=1``).
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.baselines.generate_md import render_baseline_md  # noqa: E402
from benchmarks.corpus import corpus as corpus_mod  # noqa: E402
from benchmarks.corpus import danger_labels as danger_labels_mod  # noqa: E402
from benchmarks.corpus import deterministic_embedder as embedder_mod  # noqa: E402
from benchmarks.corpus import queries as queries_mod  # noqa: E402
from benchmarks.corpus import rewrite_scenario as rewrite_mod  # noqa: E402
from benchmarks.corpus import tech_patterns as tech_patterns_mod  # noqa: E402
from benchmarks.stands.s1_quality.harness import (  # noqa: E402
    _PLANTED_PROBES,
    _SLUG_ENTRY,
    K_VALUES,
    _measure_queries,
    assemble_leak_check,
    build_golden_manager,
    fresh_golden_manager,
    measure_rewrite,
    measure_search,
    overfetch_factor,
    vector_predicate_off,
)
from benchmarks.stands.s1_quality.model_contour import (  # noqa: E402
    build_production_embedder,
    gate_model_contour,
    run_model_contour,
    s1m_required,
)
from benchmarks.stands.s1_quality.scenarios import (  # noqa: E402
    check_render_neutrality,
    deterministic_manager,
    fts_only_leg,
    mcnemar_interim_hits,
    measure_detector_quarantine_fp,
    scenario_refuse_render,
    scenario_settings,
    scenario_supersede_refind,
    scenario_write_find,
)
from vesma.manager import MemoryManager  # noqa: E402

STAND_VERSION = "s1-1"
BASELINE_VERSION = 1
BASELINE_PATH = ROOT / "benchmarks" / "baselines" / "s1.json"
MARKDOWN_PATH = ROOT / "benchmarks" / "baselines" / "BASELINE.md"
REPORTS_DIR = ROOT / "benchmarks" / "reports"

#: ADR-0020 corridor rule: baseline - max(0.02; 95% CI).
CORRIDOR_FLOOR = 0.02

#: Retrieval metrics under a derived corridor (name → ci95 key is 1:1).
_CORRIDOR_METRICS: tuple[str, ...] = (
    "precision_at_5",
    "precision_at_10",
    "recall_at_5",
    "recall_at_10",
)


def corpus_fingerprint() -> str:
    """sha256 over the corpus-defining module bytes (fixed order)."""
    modules = (
        corpus_mod,
        queries_mod,
        rewrite_mod,
        embedder_mod,
        tech_patterns_mod,
        danger_labels_mod,
    )
    digest = hashlib.sha256()
    for module in modules:
        assert module.__file__ is not None
        path = Path(module.__file__)
        digest.update(path.name.encode())
        digest.update(b"\x00")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _ci95(values: list[float]) -> float:
    """Half-width of the normal 95% CI of a per-query mean (0 for n<2)."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return NormalDist().inv_cdf(0.975) * (var**0.5) / n**0.5


# ── S1m: the production-embedder contour (ADR-0021 NM-0) ─────────────────────


def run_model_leg(root: Path) -> dict[str, Any]:
    """Run the judged queries through the PRODUCTION embedder.

    Builds a second fresh golden manager — same corpus, same fixed
    order, same ``_measure_queries`` ranking path — with the production
    embedder installed instead of the BLAKE2b reference.

    ISOLATION: the leg runs under ``root / "s1m"``, NOT the shared
    ``root`` — ``golden_settings`` derives ``data_dir`` from the root,
    so both managers would otherwise share ONE ``vectors.db`` and the
    256-dim reference vectors would mix with the 384-dim production
    ones in a single store (``np.stack`` then fails on every vector
    search and the leg silently degrades to FTS-only — measured,
    honest-looking, wrong).

    Initialization may still fail (the default since NM-1c is the bundled
    vesma-embed artifact, but an operator-configured external provider
    may fetch weights), so EVERY failure inside the leg (import,
    download, init) converts to the documented SKIP with the concrete
    reason; the deterministic reference measurement in the same
    ``run_measurement`` pass is never disturbed.

    Returns the ``s1m`` report section (measured or skipped).
    """
    try:
        embedder = build_production_embedder()
        # warmup INSIDE the try: a lazy external provider may fetch its
        # model on first embed, and that fetch must skip too, not crash
        # the run.
        embedder.embed("mnemos s1m warmup")
        mgr, slug_to_id = build_golden_manager(root / "s1m", embedder=embedder)
        try:
            measurements = _measure_queries(mgr, slug_to_id)
        finally:
            mgr.close()
        ranked = [(m.qid, list(m.result_slugs)) for m in measurements]
        dimension = getattr(embedder, "dimension", None)
        return run_model_contour(ranked, dimension=dimension)
    except Exception as exc:  # provider unavailable → skip, never crash
        reason = f"{type(exc).__name__}: {exc}"
        return run_model_contour(None, skip_reason=reason)


def _full_hit(measurement: Any, k: int = 10) -> bool:
    """Per-query binary outcome for the McNemar jig: full recall@k."""
    top = set(measurement.result_slugs[:k])
    return set(measurement.expected) <= top


def _collect_issuance_surfaces(
    mgr: MemoryManager, slug_to_id: dict[str, str]
) -> dict[str, list[str]]:
    """Every issuance render the stand can reach deterministically.

    search channel: ``scan_issuance_item`` over every result of every
    judged query (the ``mnemos_search`` / REST ``/search`` semantics);
    assemble channel: ``assemble_context`` text for each planted-entry
    probe. Retraction-family renders are collected by SC-S3 and swept
    separately (format-constrained).
    """
    from vesma.assemble import assemble_context

    search_renders: list[str] = []
    for m in _measure_queries(mgr, slug_to_id):
        for slug in m.result_slugs:
            entry = _SLUG_ENTRY[slug]
            scan = mgr.scan_issuance_item(
                entry.content, title=entry.title, context=f"s1:neutrality:{m.qid}"
            )
            search_renders.append((scan.content or "") + "\n" + (scan.title or ""))

    assemble_renders: list[str] = []
    for slug, probe in _PLANTED_PROBES.items():
        entry = _SLUG_ENTRY[slug]
        block = assemble_context(
            mgr,
            session="s1-neutrality-check",
            project=entry.project,
            query=probe,
            budget=4000,
        )
        assemble_renders.append(str(block["text"]))
    return {"search_issuance": search_renders, "assembled_context": assemble_renders}


def run_measurement() -> dict[str, Any]:
    """One complete deterministic S1 pass. Returns the ``metrics`` dict.

    The reference contour (BLAKE2b) stays byte-reproducible; the S1m
    model leg (Phase C) is measured in the SAME pass but reported in
    its own ``s1m`` section — its numbers never enter the pipeline
    corridors, and a provider-unavailable environment yields a
    ``skipped`` section instead of disturbing the reference metrics.
    """
    with tempfile.TemporaryDirectory(prefix="mnemos-s1-") as tmp:
        root = Path(tmp)

        # ── Phase A: the W4 golden measurement, reused as-is ──────────
        with fresh_golden_manager(root) as (mgr, slug_to_id):
            current = measure_search(mgr, slug_to_id, label="a9-on x4 (current)")
            with vector_predicate_off():
                off_x4 = measure_search(mgr, slug_to_id, label="a9-off x4")
            with vector_predicate_off(), overfetch_factor(2):
                pre_a9 = measure_search(mgr, slug_to_id, label="a9-off x2 (pre-A9)")
            with overfetch_factor(2):
                on_x2 = measure_search(mgr, slug_to_id, label="a9-on x2")
            rewrite = measure_rewrite(mgr)
            assemble = assemble_leak_check(mgr, slug_to_id)

            measurements = _measure_queries(mgr, slug_to_id)
            with fts_only_leg():
                fts_measurements = _measure_queries(mgr, slug_to_id)
            surfaces = _collect_issuance_surfaces(mgr, slug_to_id)

        # per-query distributions (CI inputs) + duplicate-rate invariant
        per_recall: dict[int, list[float]] = {k: [] for k in K_VALUES}
        per_precision: dict[int, list[float]] = {k: [] for k in K_VALUES}
        duplicates: dict[int, int] = {k: 0 for k in K_VALUES}
        for m in measurements:
            for k in K_VALUES:
                top = m.result_slugs[:k]
                duplicates[k] += len(top) - len(set(top))
                if not m.expected:
                    continue
                hits = len(set(top) & set(m.expected))
                per_precision[k].append(hits / k)
                per_recall[k].append(hits / len(m.expected))

        mcnemar = mcnemar_interim_hits(
            [_full_hit(m) for m in measurements if m.expected],
            [_full_hit(m) for m in fts_measurements if m.expected],
            pair="fts_only_vs_hybrid_rrf",
        )

        # ── Phase B: extension scenarios on fresh managers ────────────
        ext_mgr = deterministic_manager(scenario_settings(root))
        try:
            write_find = scenario_write_find(ext_mgr)
            supersede = scenario_supersede_refind(ext_mgr)
            detector_fp = measure_detector_quarantine_fp(ext_mgr)
        finally:
            ext_mgr.close()
        refuse = scenario_refuse_render(root)
        neutrality = check_render_neutrality(
            surfaces,
            refuse.pop("_retraction_renders"),
            refuse.pop("_quarantine_reasons"),
        )

        # ── Phase C: S1m — the production-embedder contour (NM-0) ────
        s1m = run_model_leg(root)

    a9_variants = {}
    for key, m in (
        ("current", current),
        ("a9_off_x4", off_x4),
        ("pre_a9", pre_a9),
        ("a9_on_x2", on_x2),
    ):
        a9_variants[key] = {
            "label": m.label,
            "recall_at_5": round(m.recall[5], 6),
            "recall_at_10": round(m.recall[10], 6),
            "precision_at_5": round(m.precision[5], 6),
            "precision_at_10": round(m.precision[10], 6),
            "hybrid_queries": m.hybrid_queries,
            "planted_appearances": m.planted_appearances,
        }

    injection_acceptance = (
        1.0
        if (current.planted_leaks == 0 and not assemble["leaks"])
        else 1.0 - (current.planted_leaks / current.planted_appearances)
    )
    quarantine_exclusion_ok = bool(
        refuse["quarantine_exclusion_from_issuance"] and refuse["retrievable_by_id"]
    )

    return {
        "retrieval": {
            "precision_at_5": round(current.precision[5], 6),
            "precision_at_10": round(current.precision[10], 6),
            "recall_at_5": round(current.recall[5], 6),
            "recall_at_10": round(current.recall[10], 6),
            "ci95": {
                **{f"precision_at_{k}": round(_ci95(vs), 6) for k, vs in per_precision.items()},
                **{f"recall_at_{k}": round(_ci95(vs), 6) for k, vs in per_recall.items()},
            },
            "judged_queries": current.judged_queries,
            "probe_queries": current.probe_queries,
            "hybrid_queries": current.hybrid_queries,
        },
        "invariants": {
            "injection_acceptance": {
                "value": round(injection_acceptance, 6),
                "expect": 1.0,
                "ok": injection_acceptance == 1.0,
            },
            "non_admissible_surfaced": {
                "value": current.non_admissible_surfaced,
                "expect": 0,
                "ok": current.non_admissible_surfaced == 0,
            },
            "foreign_project_surfaced": {
                "value": current.foreign_project_surfaced,
                "expect": 0,
                "ok": current.foreign_project_surfaced == 0,
            },
            "duplicate_occurrences_at_5": {
                "value": duplicates[5],
                "expect": 0,
                "ok": duplicates[5] == 0,
            },
            "duplicate_occurrences_at_10": {
                "value": duplicates[10],
                "expect": 0,
                "ok": duplicates[10] == 0,
            },
            "quarantine_exclusion": {
                "value": 1.0 if quarantine_exclusion_ok else 0.0,
                "expect": 1.0,
                "ok": quarantine_exclusion_ok,
            },
            "render_neutrality": {
                "value": len(neutrality["violations"]),
                "expect": 0,
                "ok": neutrality["ok"],
            },
            "rewrite_redemption_leaks": {
                "value": rewrite.planted_redemption_leaks,
                "expect": 0,
                "ok": rewrite.planted_redemption_leaks == 0,
            },
        },
        "injection": {
            "planted_appearances": current.planted_appearances,
            "planted_leaks": current.planted_leaks,
            "assemble_leaks": assemble["leaks"],
            "assemble_all_planted_surfaced": bool(assemble["all_planted_surfaced"]),
        },
        "a9": {
            "variants": a9_variants,
            "delta_recall10_current_vs_pre_a9": round(current.recall[10] - pre_a9.recall[10], 6),
        },
        "rewrite": {
            "events": rewrite.rewrite_events,
            "follow_up_retrieves": rewrite.follow_up_retrieves,
            "hits": rewrite.hits,
            "hit_rate": round(rewrite.hit_rate, 6),
            "whole_redemptions": rewrite.whole_redemptions,
            "regret_rate": round(rewrite.regret_rate, 6),
            "controls": rewrite.controls,
            "control_hits": rewrite.control_hits,
        },
        "scenarios": {
            "write_find": write_find,
            "supersede_refind": supersede,
            "refuse_render": refuse,
        },
        "detector_quarantine_fp": detector_fp,
        "render_neutrality": neutrality,
        "mcnemar_interim": mcnemar,
        "s1m": s1m,
    }


def build_baseline(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "baseline_version": BASELINE_VERSION,
        "stand_version": STAND_VERSION,
        "corpus_fingerprint": corpus_fingerprint(),
        # ADR-0021 NM-0: pins the PRODUCTION embedder the s1m section
        # was measured with. ``None`` = the reference contour only
        # (BLAKE2b is not a model; a no-provider environment records a
        # null fingerprint and the gate treats that as the documented
        # migration, not a hole).
        "model_fingerprint": (metrics.get("s1m") or {}).get("fingerprint"),
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "metrics": metrics,
        "environment": {
            "python": ".".join(str(v) for v in sys.version_info[:3]),
            "deterministic_embedder": True,
        },
    }


def gate_check(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """ADR-0020 gate policy for S1 + the ADR-0021 NM-0 model contour.

    Pipeline half: invariants exact, corridors derived (BLAKE2b
    reference — the only source for pipeline corridors). Model half:
    ``model_fingerprint`` fail-loud (an embedder change without a
    same-PR re-baseline is RED — the silent-substitution hole NM-0
    closes) and the S1m self-comparison corridor.
    """
    failures: list[str] = []
    base_metrics = baseline.get("metrics") or {}

    if current.get("retrieval", {}).get("judged_queries") != base_metrics.get("retrieval", {}).get(
        "judged_queries"
    ):
        failures.append("judged query count drifted — corpus or queries changed")

    if baseline.get("corpus_fingerprint") != corpus_fingerprint():
        failures.append(
            "corpus fingerprint differs from the baseline — re-baseline required "
            "(--record) per ADR-0020 event-driven triggers"
        )

    # ── fail-loud embedder substitution (ADR-0021 NM-0) ──────────────
    recorded_fp = baseline.get("model_fingerprint") or None
    live_fp = (current.get("s1m") or {}).get("fingerprint") or None
    s1m_status = (current.get("s1m") or {}).get("status")
    if recorded_fp is not None and s1m_status == "measured":
        from benchmarks.stands.s1_quality.model_contour import fingerprint_equal

        if not fingerprint_equal(recorded_fp, live_fp):
            from benchmarks.stands.s1_quality.model_contour import (
                fingerprint_label,
            )

            failures.append(
                "production embedder changed "
                f"(old={fingerprint_label(recorded_fp)} "
                f"new={fingerprint_label(live_fp)}) — explicit re-baseline "
                "required (--record), same PR per ADR-0021"
            )
    elif recorded_fp is not None and s1m_status != "measured" and s1m_required():
        # fingerprint pinned, but THIS environment could not measure the
        # model contour at all while the operator demanded it mandatory —
        # the gate cannot verify the embedder, so it must not pass silently.
        failures.append(
            "s1m skipped while the baseline pins a production fingerprint and "
            "VESMA_BENCH_S1M_REQUIRED is set — the production embedder cannot "
            "be verified in this environment"
        )
    # recorded_fp is None (pre-NM-0 baseline or recorded without the
    # provider): the documented migration — the next --record pins the
    # first fingerprint; nothing recorded can silently diverge yet.

    # S1m contour gate (self-comparison; skip semantics decided inside)
    s1m_gate = gate_model_contour(current.get("s1m") or {}, baseline)
    failures.extend(s1m_gate.get("failures", []))

    for name, entry in current["invariants"].items():
        if not entry["ok"]:
            failures.append(f"invariant {name}: {entry['value']} != {entry['expect']}")

    cur_r = current["retrieval"]
    base_r = base_metrics.get("retrieval", {})
    base_ci = base_r.get("ci95", {})
    for metric in _CORRIDOR_METRICS:
        if metric not in base_r:
            failures.append(f"baseline misses retrieval metric {metric}")
            continue
        margin = max(CORRIDOR_FLOOR, float(base_ci.get(metric, 0.0)))
        floor = float(base_r[metric]) - margin
        if float(cur_r[metric]) < floor:
            failures.append(
                f"{metric}={cur_r[metric]:.4f} below corridor "
                f"{floor:.4f} (baseline {base_r[metric]:.4f} - max(0.02; ci {margin:.4f}))"
            )

    cur_w = current["rewrite"]
    base_w = base_metrics.get("rewrite", {})
    if base_w:
        if float(cur_w["hit_rate"]) < float(base_w["hit_rate"]) - CORRIDOR_FLOOR:
            failures.append(
                f"replace-hit-rate={cur_w['hit_rate']:.4f} below corridor "
                f"{float(base_w['hit_rate']) - CORRIDOR_FLOOR:.4f}"
            )
        if float(cur_w["regret_rate"]) > float(base_w["regret_rate"]) + CORRIDOR_FLOOR:
            failures.append(
                f"replace-regret-rate={cur_w['regret_rate']:.4f} above ceiling "
                f"{float(base_w['regret_rate']) + CORRIDOR_FLOOR:.4f}"
            )
        if cur_w["control_hits"] != cur_w["controls"]:
            failures.append(
                f"control channel degraded: {cur_w['control_hits']}/{cur_w['controls']}"
            )

    a9_delta = float(current["a9"]["delta_recall10_current_vs_pre_a9"])
    if a9_delta < -CORRIDOR_FLOOR:
        failures.append(f"A9 recall@10 delta {a9_delta:+.4f} below {-CORRIDOR_FLOOR}")

    for name, scenario in current["scenarios"].items():
        if not scenario.get("pass"):
            failures.append(f"scenario {name} failed: {scenario}")

    return {"pass": not failures, "failures": failures}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="mnemos S1 quality stand (ADR-0020)")
    parser.add_argument(
        "--record",
        action="store_true",
        help="rewrite benchmarks/baselines/s1.json + BASELINE.md from this run",
    )
    parser.add_argument("--quiet", action="store_true", help="only the gate verdict")
    args = parser.parse_args(argv)

    print("s1: measuring (deterministic, no wall-clock metrics)…", file=sys.stderr)
    metrics = run_measurement()
    baseline = build_baseline(metrics)

    if args.record or not BASELINE_PATH.exists():
        _write_json(BASELINE_PATH, baseline)
        MARKDOWN_PATH.write_text(render_baseline_md(baseline))
        verdict = {
            "pass": True,
            "failures": [],
            "mode": "record",
            "note": "baseline (re)written; BASELINE.md regenerated from JSON",
        }
        print(f"s1: baseline recorded → {BASELINE_PATH}", file=sys.stderr)
        print(f"s1: markdown generated → {MARKDOWN_PATH}", file=sys.stderr)
    else:
        recorded = json.loads(BASELINE_PATH.read_text())
        verdict = gate_check(metrics, recorded)
        stamp = baseline["created"].replace(":", "").replace("-", "").replace("+", "_")
        report_path = REPORTS_DIR / f"s1-{stamp}.json"
        _write_json(report_path, {**baseline, "gate": verdict})
        print(f"s1: report → {report_path}", file=sys.stderr)

    if verdict["pass"]:
        s1m = metrics.get("s1m") or {}
        if not args.quiet:
            print("s1: gate PASS — corridors and invariants hold")
            if s1m.get("status") == "skipped":
                print(f"s1m: SKIP — {s1m.get('reason')}", file=sys.stderr)
            elif s1m.get("status") == "measured":
                m = s1m.get("metrics") or {}
                print(
                    "s1m: production embedder measured — "
                    f"recall@5={m.get('recall_at_5', 0):.4f} "
                    f"recall@10={m.get('recall_at_10', 0):.4f} "
                    f"mrr={m.get('mrr', 0):.4f} "
                    f"ndcg@10={m.get('ndcg_at_10', 0):.4f}"
                )
        return 0
    for failure in verdict["failures"]:
        print(f"s1: FAIL — {failure}", file=sys.stderr)
    print("s1: gate FAIL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
