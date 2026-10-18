#!/usr/bin/env python3
"""Round-8 STAGE-1 checkpoint selection — rule FIXED BEFORE the run
(run_config.json stage1.selection_rule_FIXED_BEFORE_RUN).

  pool = epoch 0 (base, pre-FT) + FT epochs 1..4
  candidates = pool members with
      mono_control_median >= base_mono - 0.010   (trend guard)
      cross_R@5          >= base_cross - 0.01    (geometry hold)
  rank = MAX (llm_median + seed_dup_median)/2     (quality composite)
  tie: higher mono_control_median, then lower epoch

Epoch 0 is in the pool BY DESIGN: a harmful FT degrades to the
sweep-audited base instead of forcing a bad teacher — a selection ALWAYS
exists. Readout used: full_1024 (the teacher's native space; the trunc_384
columns ride along in ft_selection.json for the record).

Output: runs/embed-r8/ft_selection.json + runlog entry. Stage-2 teacher
precompute MUST read the chosen epoch from this file.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

TREND_GUARD_MONO = 0.010
GEOMETRY_HOLD_CROSS = 0.01


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True, type=Path)
    args = ap.parse_args()

    records = [json.loads(l) for l in (args.run_dir / "metrics.jsonl").read_text().splitlines() if l.strip()]
    if not records or records[0].get("epoch") != 0:
        raise SystemExit("metrics.jsonl must start with the epoch-0 base_readout record")

    base = records[0]["proxy_readout"]["full_1024"]
    base_mono = base["mono_control_median"]
    base_cross = base["cross_R@5"]

    pool = []
    for r in records:
        full = r.get("proxy_readout", {}).get("full_1024")
        if not full:
            continue
        composite = (full["llm_median"] + full["seed_dup_median"]) / 2.0
        pool.append({
            "epoch": r["epoch"],
            "composite": round(composite, 6),
            "mono_control_median": full["mono_control_median"],
            "cross_R@5": full["cross_R@5"],
            "llm_median": full["llm_median"],
            "llm_share": full["llm_share_ge085"],
            "seed_dup_median": full["seed_dup_median"],
            "seed_dup_share": full["seed_dup_share_ge085"],
            "passes_trend_guard": full["mono_control_median"] >= base_mono - TREND_GUARD_MONO,
            "passes_geometry_hold": full["cross_R@5"] >= base_cross - GEOMETRY_HOLD_CROSS,
        })

    candidates = [p for p in pool if p["passes_trend_guard"] and p["passes_geometry_hold"]]
    chosen, route = None, None
    if candidates:
        ranked = sorted(
            candidates,
            key=lambda p: (-p["composite"], -p["mono_control_median"], p["epoch"]),
        )
        chosen = ranked[0]
        route = "candidates: max (llm_median+seed_dup_median)/2 among trend-guard+geometry-hold passers"
    else:  # unreachable by construction (epoch 0 passes its own gates) — belt and braces
        raise SystemExit("RULE VIOLATION: empty candidate pool — epoch 0 must always pass")

    sel = {
        "kind": "embed-r8-stage1-checkpoint-selection",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rule": "run_config.json stage1.selection_rule_FIXED_BEFORE_RUN (fixed before the run)",
        "base_reference": {
            "mono_control_median": base_mono,
            "cross_R@5": base_cross,
            "llm_median": base["llm_median"],
            "seed_dup_median": base["seed_dup_median"],
        },
        "guards": {
            "trend_guard_mono": base_mono - TREND_GUARD_MONO,
            "geometry_hold_cross": base_cross - GEOMETRY_HOLD_CROSS,
        },
        "pool": pool,
        "candidate_epochs": [p["epoch"] for p in candidates],
        "route": route,
        "chosen_epoch": chosen["epoch"],
        "chosen": chosen,
    }
    (args.run_dir / "ft_selection.json").write_text(json.dumps(sel, indent=1, ensure_ascii=False) + "\n")
    print(
        f"chosen epoch {chosen['epoch']} via {route}\n"
        f"  composite={chosen['composite']} llm={chosen['llm_median']}/{chosen['llm_share']} "
        f"sd={chosen['seed_dup_median']}/{chosen['seed_dup_share']} "
        f"mono={chosen['mono_control_median']} cross={chosen['cross_R@5']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
