#!/usr/bin/env python
"""Generate ``BASELINE.md`` from the canonical baseline JSON.

The JSON (``benchmarks/baselines/s1.json``) is the source of truth;
this markdown is a human-readable summary regenerated on every
``--record`` run. NEVER hand-edit the output — change the generator or
re-record.

CLI: ``python benchmarks/baselines/generate_md.py [path-to-json]``
(default ``s1.json`` next to this file).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_JSON = Path(__file__).resolve().parent / "s1.json"
OUTPUT_MD = Path(__file__).resolve().parent / "BASELINE.md"

#: ADR-0020 corridor rule (mirrors the runner; markdown only reports it).
CORRIDOR_FLOOR = 0.02

#: S1m corridor metric names (mirror of the model contour; summary only).
S1M_CORRIDOR_METRICS: tuple[str, ...] = (
    "precision_at_5",
    "precision_at_10",
    "recall_at_5",
    "recall_at_10",
)


def _fmt(value: Any, digits: int = 4) -> str:
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _corridor(base: float, ci: float) -> str:
    margin = max(CORRIDOR_FLOOR, ci)
    return f"{base - margin:+.4f} (baseline {base:.4f} - max(0.02; ci {margin:.4f}))"


def render_baseline_md(baseline: dict[str, Any]) -> str:
    m = baseline["metrics"]
    ret = m["retrieval"]
    ci = ret["ci95"]
    inv = m["invariants"]
    inj = m["injection"]
    a9 = m["a9"]
    rw = m["rewrite"]
    sc = m["scenarios"]
    fp = m["detector_quarantine_fp"]
    neu = m["render_neutrality"]
    mc = m["mcnemar_interim"]

    lines: list[str] = []
    add = lines.append

    add("# S1 Quality Baseline — generated summary (ADR-0020 BF-1)")
    add("")
    add("> GENERATED from `benchmarks/baselines/s1.json` — the JSON is the")
    add("> source of truth; regenerate with `make bench-s1-record` (or")
    add("> `python benchmarks/stands/s1_quality/run.py --record`). Do not edit.")
    add("")
    add(f"- **baseline_version:** {baseline['baseline_version']}")
    add(f"- **stand_version:** {baseline['stand_version']}")
    add(f"- **corpus_fingerprint:** `{baseline['corpus_fingerprint']}`")
    add(f"- **created:** {baseline['created']}")
    fp_model = baseline.get("model_fingerprint")
    if fp_model:
        from benchmarks.stands.s1_quality.model_contour import fingerprint_label

        add(f"- **model_fingerprint (production embedder):** `{fingerprint_label(fp_model)}`")
        add(
            "  - full weights sha256: "
            f"`{fp_model.get('weights_sha256') or 'n/a (no local artifact)'}`"
        )
    else:
        add(
            "- **model_fingerprint (production embedder):** none — pre-NM-0 "
            "baseline or recorded without the provider available; the next "
            "`--record` pins it (ADR-0021 migration)"
        )
    add(
        f"- **environment:** python {baseline['environment']['python']}, "
        f"deterministic_embedder={baseline['environment']['deterministic_embedder']} "
        "(BLAKE2b lexical — pins the retrieval PIPELINE, not MiniLM)"
    )
    add("")
    add("## 1. Retrieval quality (judged golden queries)")
    add("")
    add("| Metric | Value | 95% CI (half-width) |")
    add("| --- | ---: | ---: |")
    for metric in ("precision_at_5", "precision_at_10", "recall_at_5", "recall_at_10"):
        add(f"| {metric.replace('_at_', '@')} | {_fmt(ret[metric])} | {_fmt(ci[metric])} |")
    add(
        f"| queries (judged / probes / hybrid) | {ret['judged_queries']} / "
        f"{ret['probe_queries']} / {ret['hybrid_queries']} | — |"
    )
    add("")
    add("## 2. Invariants (hard — any deviation is a defect, not a dip)")
    add("")
    add("| Invariant | Value | Expect | Status |")
    add("| --- | ---: | ---: | --- |")
    for name, entry in inv.items():
        status = "OK" if entry["ok"] else "VIOLATED"
        add(f"| {name} | {_fmt(entry['value'])} | {entry['expect']} | {status} |")
    add("")
    add("## 3. Injection-acceptance detail")
    add("")
    add(
        f"- planted appearances across all queries: **{inj['planted_appearances']}**, "
        f"leaks: **{inj['planted_leaks']}** (search channel)"
    )
    add(
        f"- assemble_context probes: leaks **{inj['assemble_leaks']}**, "
        f"all planted surfaced: **{inj['assemble_all_planted_surfaced']}**"
    )
    add("")
    add("## 4. A9 before/after (vector-leg predicate x over-fetch)")
    add("")
    add("| Variant | recall@5 | recall@10 | precision@5 | precision@10 | hybrid | planted |")
    add("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for key in ("current", "a9_off_x4", "pre_a9", "a9_on_x2"):
        v = a9["variants"][key]
        add(
            f"| {v['label']} | {v['recall_at_5']:.4f} | {v['recall_at_10']:.4f} "
            f"| {v['precision_at_5']:.4f} | {v['precision_at_10']:.4f} "
            f"| {v['hybrid_queries']} | {v['planted_appearances']} |"
        )
    add(f"\nDelta (current - pre-A9) recall@10: **{a9['delta_recall10_current_vs_pre_a9']:+.4f}**")
    add("")
    add("## 5. ADR-0018 rewrite pair")
    add("")
    add("| Metric | Value |")
    add("| --- | ---: |")
    add(f"| replace-hit-rate | {rw['hit_rate']:.4f} ({rw['hits']}/{rw['follow_up_retrieves']}) |")
    add(
        f"| replace-regret-rate | {rw['regret_rate']:.4f} "
        f"({rw['whole_redemptions']}/{rw['events']}) |"
    )
    add(f"| control channel | {rw['control_hits']}/{rw['controls']} |")
    add("")
    add("## 6. ADR-0019 S1-S3 scenarios")
    add("")
    for name in ("write_find", "supersede_refind", "refuse_render"):
        s = sc[name]
        status = "PASS" if s["pass"] else "FAIL"
        add(f"- **{s['scenario']}** — {status}")
    sr = sc["supersede_refind"]
    add(
        f"  - supersede: id unchanged {sr['id_unchanged_by_substitution']}; "
        f"served projection regenerated {sr['served_projection_regenerated']}; "
        f"old projection gone from the lexical leg "
        f"{sr['old_projection_gone_from_lexical_leg']}"
    )
    add(
        f"  - observation (informational): filter projection stale right "
        f"after a content edit — {sr['filter_projection_stale_after_update']} "
        f"— false since #193 (the projection is reset in the same "
        f"transaction as the content write)"
    )
    rr = sc["refuse_render"]
    add(
        f"  - refusal reasons are detector class codes: "
        f"{rr['refusal_reasons_are_class_codes']} "
        f"({rr['quarantine_reasons']['secret']}, {rr['quarantine_reasons']['injection']})"
    )
    add(
        f"  - retraction render format: {rr['retraction_render_format']}; "
        f"titles withheld: {rr['titles_withheld']}"
    )
    add(
        f"  - quarantine exclusion from issuance: {rr['quarantine_exclusion_from_issuance']}; "
        f"retrievable by id: {rr['retrievable_by_id']}"
    )
    add(f"  - CCR cached original retracted: {rr['ccr_original_retracted']}")
    add("")
    add("## 7. detector-quarantine-fp (informational; conditional corridor)")
    add("")
    add(
        f"- labelled entries: {fp['labelled_entries']} "
        f"(benign {fp['benign_entries']} / dangerous {fp['dangerous_entries']})"
    )
    add(
        f"- text-level: TP {fp['true_positives']}, FP **{fp['false_positives']}**, "
        f"FN {fp['false_negatives']}, TN {fp['true_negatives']} → "
        f"fp_rate_over_benign = **{fp['fp_rate_over_benign']:.4f}**"
    )
    if fp["fp_slugs"]:
        add(f"- false-positive slugs: {', '.join(fp['fp_slugs'])}")
    if fp["live_ingest"] is not None:
        live = fp["live_ingest"]
        add(
            f"- live ingest: {live['demoted_to_raw_by_n1']}/{live['ingested']} benign "
            "tech-pattern entries demoted to RAW by the N1 gate (observable FP cost)"
        )
    add(f"- {fp['note']}")
    add("")
    add("## 8. Render-neutrality sweep")
    add("")
    add(
        f"- surfaces checked: **{neu['surfaces_checked']}** "
        f"({', '.join(neu['issuance_surface_kinds'])} + retraction renders)"
    )
    add(f"- violations: **{len(neu['violations'])}**")
    add("")
    add("## 9. McNemar jig (interim)")
    add("")
    add(f"- pair: `{mc['pair']}` over {mc['queries']} judged queries")
    add(
        f"- hits: leg A {mc['hits_leg_a']} / leg B {mc['hits_leg_b']}; discordant "
        f"b={mc['discordant_b']}, c={mc['discordant_c']}; two-sided sign-test "
        f"p = **{mc['p_two_sided']:.4f}**"
    )
    add(f"- {mc['note']}")
    add("")
    add("## 10. S1m — production-embedder model contour (ADR-0021 NM-0)")
    add("")
    s1m = m.get("s1m") or {}
    if s1m.get("status") != "measured":
        add(f"- **status:** skipped — {s1m.get('reason') or 'provider unavailable'}")
        add(
            "- skipped is GREEN in the default local posture; set "
            "`VESMA_BENCH_S1M_REQUIRED=1` to make it a gate failure (CI)"
        )
    else:
        sm = s1m.get("metrics") or {}
        add(
            "- the PRODUCTION embedder over the same judged corpus — "
            "self-comparison only, NEVER against the BLAKE2b reference "
            "(the reference measures retrieval mechanics, the model "
            "semantic quality)"
        )
        add("")
        add("| Metric | Value | 95% CI (half-width) |")
        add("| --- | ---: | ---: |")
        for metric in S1M_CORRIDOR_METRICS:
            metric_ci = (sm.get("ci95") or {}).get(metric, 0.0)
            add(
                f"| {metric.replace('_at_', '@')} | {_fmt(sm.get(metric, 0.0))} "
                f"| {_fmt(metric_ci)} |"
            )
        add(f"| mrr | {_fmt(sm.get('mrr', 0.0))} | — |")
        add(f"| ndcg@5 | {_fmt(sm.get('ndcg_at_5', 0.0))} | — |")
        add(f"| ndcg@10 | {_fmt(sm.get('ndcg_at_10', 0.0))} | — |")
        add(f"| judged queries | {sm.get('judged_queries', 0)} | — |")
        env = s1m.get("environment") or {}
        add(
            f"- embedder: `{fingerprint_label(s1m.get('fingerprint'))}`, "
            f"dim {env.get('dimension')}, arch {env.get('arch')}"
        )
    add("")
    add("## 11. Gate corridors (derived from THIS baseline)")
    add("")
    add("| Metric | Corridor |")
    add("| --- | --- |")
    for metric in ("precision_at_5", "precision_at_10", "recall_at_5", "recall_at_10"):
        add(f"| {metric} ≥ | {_corridor(float(ret[metric]), float(ci[metric]))} |")
    add(f"| replace-hit-rate ≥ | {rw['hit_rate'] - CORRIDOR_FLOOR:+.4f} |")
    add(f"| replace-regret-rate ≤ | {rw['regret_rate'] + CORRIDOR_FLOOR:+.4f} |")
    add(f"| A9 recall@10 delta ≥ | {-CORRIDOR_FLOOR:+.4f} |")
    add("| invariants | exact (= 1.000 / = 0), never carried over a re-baseline |")
    s1m = m.get("s1m") or {}
    if s1m.get("status") == "measured":
        sm = s1m.get("metrics") or {}
        for metric in S1M_CORRIDOR_METRICS:
            metric_ci = (sm.get("ci95") or {}).get(metric, 0.0)
            add(f"| s1m {metric} ≥ | {_corridor(float(sm.get(metric, 0.0)), float(metric_ci))} |")
    else:
        add("| s1m | skipped (provider unavailable) — green unless VESMA_BENCH_S1M_REQUIRED=1 |")
    add(
        "| model_fingerprint | exact match vs this baseline — a mismatch is RED "
        "(re-baseline `--record`, same PR, per ADR-0021) |"
    )
    add("")
    add("## 12. Reproducing")
    add("")
    add("```bash")
    add("make bench-s1            # gate mode (corridors + invariants vs this baseline)")
    add("make bench-s1-record     # re-record this baseline + regenerate this file")
    add("```")
    add("")
    add("Runtime is wall-clock-bounded only by the harness itself (ADR-0020")
    add("budget: S1 < 30 s local); every measured value is deterministic —")
    add("no clock, no RNG, no network. Pre-BF-1 baseline history (the W4")
    add("record) lives in git history at `tests/golden/BASELINE.md`.")
    add("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    path = Path(argv[0]) if argv else DEFAULT_JSON
    baseline = json.loads(path.read_text())
    out = OUTPUT_MD if path == DEFAULT_JSON else path.with_suffix(".md")
    out.write_text(render_baseline_md(baseline))
    print(f"generated {out} from {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
