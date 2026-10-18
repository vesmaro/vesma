#!/usr/bin/env python3
"""Round-8 STAGE-2 checkpoint selection — mono-aware (round-6 prereg §3
rule) with the RATIFIED round-8 llm recalibration (r8 prereg §2) applied to
the selection proxy — selection-internal consistency with the gate.

Rule fixed BEFORE the run (run_config.json stage2.selection):

  1. candidates = ALL phase-2 checkpoints (KD-replay epochs included)
     passing the cross-class PROXY:
       seed_dup_median >= 0.85 AND seed_dup_share_ge085 >= 0.50
       AND llm_median >= 0.90  AND llm_share_ge085   >= 0.75
     (RECALIBRATED numbers — the superseded 0.93/0.90 must NOT appear
     anywhere in round-8; proxy = the per-epoch monitor harness, identical
     for every epoch — selection-internal consistency)
  2. among candidates: MAX mono_control_median
     (tie-break: higher min-over-slices R@5, then lower epoch)
  3. no candidates -> checkpoint with MAX composite (min-over-slices R@5)
     among ALL run checkpoints (both phases) with mono_control_median >=
     0.8932 (= prod 0.8982 - 0.005, unchanged); tie-break lower epoch.
  4. still nothing -> REJECT without shopping.

The judged corpus c2ce056d is never read here.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

MONO_GUARD_LINE = 0.8932
PROXY_THRESHOLDS = {
    "seed_dup_median": 0.85,
    "seed_dup_share_ge085": 0.50,
    "llm_median": 0.90,   # RECALIBRATED r8 prereg §2 (superseded 0.93)
    "llm_share_ge085": 0.75,  # RECALIBRATED r8 prereg §2 (superseded 0.90)
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True, type=Path)
    args = ap.parse_args()

    records = [json.loads(l) for l in (args.run_dir / "metrics.jsonl").read_text().splitlines() if l.strip()]
    if not records:
        raise SystemExit("no metrics records")

    phase2 = [r for r in records if r.get("phase") == 2]

    def proxy_pass(r: dict) -> bool:
        m = r.get("proxy_monitor")
        if not m:
            return False
        return (
            m["seed_dup_median"] >= PROXY_THRESHOLDS["seed_dup_median"]
            and m["seed_dup_share_ge085"] >= PROXY_THRESHOLDS["seed_dup_share_ge085"]
            and m["llm_median"] >= PROXY_THRESHOLDS["llm_median"]
            and m["llm_share_ge085"] >= PROXY_THRESHOLDS["llm_share_ge085"]
        )

    candidates = [r for r in phase2 if proxy_pass(r)]
    chosen, route = None, None
    if candidates:
        ranked = sorted(
            candidates,
            key=lambda r: (
                -r["proxy_monitor"]["mono_control_median"],
                -r["selection_criterion"],
                r["epoch"],
            ),
        )
        chosen = ranked[0]
        route = "step2: max mono_control_median among phase-2 cross-class-proxy passers (r8 recalibrated llm 0.90/0.75)"
    else:
        pool = [
            r for r in records
            if r.get("proxy_monitor") and r["proxy_monitor"]["mono_control_median"] >= MONO_GUARD_LINE
        ]
        if pool:
            ranked = sorted(
                pool,
                key=lambda r: (-r["selection_criterion"], r["epoch"]),
            )
            chosen = ranked[0]
            route = "step3_fallback: max min-over-slices R@5 among all checkpoints with mono >= 0.8932"

    sel = {
        "kind": "embed-r8-checkpoint-selection",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rule": "round-6 prereg §3 + r8 recalibrated selection proxy (run_config.stage2.selection)",
        "proxy_thresholds": PROXY_THRESHOLDS,
        "mono_guard_line": MONO_GUARD_LINE,
        "epochs_on_record": [r["epoch"] for r in records],
        "phase2_epochs": [r["epoch"] for r in phase2],
        "candidate_epochs": [r["epoch"] for r in candidates],
        "candidate_trail": [
            {"epoch": r["epoch"], "mono": r["proxy_monitor"]["mono_control_median"],
             "seed_dup": r["proxy_monitor"]["seed_dup_median"],
             "seed_dup_share": r["proxy_monitor"]["seed_dup_share_ge085"],
             "llm": r["proxy_monitor"]["llm_median"],
             "llm_share": r["proxy_monitor"]["llm_share_ge085"],
             "min_over_slices_r5": r["selection_criterion"]}
            for r in sorted(candidates, key=lambda r: r["epoch"])
        ],
        "fallback_pool_epochs": (
            [r["epoch"] for r in records
             if r.get("proxy_monitor") and r["proxy_monitor"]["mono_control_median"] >= MONO_GUARD_LINE]
            if not candidates else None
        ),
        "route": route or "REJECT: no candidates and no checkpoint with mono >= 0.8932 (rule step 4)",
        "chosen_epoch": chosen["epoch"] if chosen else None,
        "chosen": chosen,
    }
    (args.run_dir / "selection.json").write_text(json.dumps(sel, indent=1, ensure_ascii=False) + "\n")
    if chosen:
        m = chosen["proxy_monitor"]
        print(
            f"chosen epoch {chosen['epoch']} via {route}\n"
            f"  mono={m['mono_control_median']} sd={m['seed_dup_median']}/{m['seed_dup_share_ge085']} "
            f"llm={m['llm_median']}/{m['llm_share_ge085']} min-slices={chosen['selection_criterion']:.4f} "
            f"R@5@384={chosen['s1m_val_by_slice']['384']['recall_at_5']:.4f}"
        )
        return 0
    print("REJECT: selection rule exhausted — no candidates, no checkpoint with mono >= 0.8932")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
