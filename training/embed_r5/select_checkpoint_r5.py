#!/usr/bin/env python3
"""Round-5 checkpoint selection — prereg §1.5, fixed BEFORE training.

Criterion: max min-over-MRL-slices R@5 on the val slice ONLY (the
per-epoch `selection_criterion` recorded by train_r5.py).
Tie-break: higher R@5 at 384, then higher MRR at 384, then lower epoch.

The judged corpus c2ce056d is not read here. Gate-set proxies recorded in
metrics.jsonl are monitoring only (prereg §4.3) and never enter this rule.

Writes selection.json next to the run dir and prints the chosen epoch.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True, type=Path)
    args = ap.parse_args()

    records = [json.loads(l) for l in (args.run_dir / "metrics.jsonl").read_text().splitlines() if l.strip()]
    if not records:
        raise SystemExit("no metrics records")

    def key(r: dict):
        s384 = r["s1m_val_by_slice"]["384"]
        return (-r["selection_criterion"], -s384["recall_at_5"], -s384["mrr"], r["epoch"])

    ranked = sorted(records, key=key)
    best = ranked[0]
    top = [r["epoch"] for r in ranked[:3]]

    sel = {
        "kind": "embed-r5-checkpoint-selection",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rule": "max min-over-slices R@5 (val only); tie: higher R@5@384, higher MRR@384, lower epoch",
        "epochs_on_record": [r["epoch"] for r in records],
        "criterion_by_epoch": {str(r["epoch"]): r["selection_criterion"] for r in records},
        "top3": top,
        "chosen_epoch": best["epoch"],
        "chosen": best,
    }
    (args.run_dir / "selection.json").write_text(json.dumps(sel, indent=1) + "\n")
    print(f"chosen epoch {best['epoch']} (criterion {best['selection_criterion']}, "
          f"R@5@384 {best['s1m_val_by_slice']['384']['recall_at_5']}); top3={top}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
