"""Checkpoint selection for the round-4 F-leg — VAL SLICE ONLY.

Rule (committed in run_config.json BEFORE the run):
  pick argmax over epochs of s1m_val.recall_at_5;
  tie-break: higher s1m_val.ndcg_at_10, then LOWER epoch.

The sealed judged corpus c2ce056d is never touched here — selection
reads only <run-dir>/metrics.jsonl written by train_r4.py (the frozen
teacher-top5 val protocol on the 5% slice, 1275 queries).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="select the round-4 checkpoint (val-only rule)")
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--out", default=None, type=Path, help="default: <run-dir>/selection.json")
    args = ap.parse_args(argv)

    metrics_path = args.run_dir / "metrics.jsonl"
    records = [
        json.loads(line)
        for line in metrics_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not records:
        raise SystemExit(f"error: no epoch records in {metrics_path}")

    def key(r: dict) -> tuple[float, float, int]:
        s = r["s1m_val"]
        return (s["recall_at_5"], s["ndcg_at_10"], -int(r["epoch"]))

    best = max(records, key=key)
    ranking = [
        {
            "epoch": r["epoch"],
            "recall_at_5": r["s1m_val"]["recall_at_5"],
            "ndcg_at_10": r["s1m_val"]["ndcg_at_10"],
            "mrr": r["s1m_val"]["mrr"],
            "val_cosine_vs_teacher_384": r["val_cosine_vs_teacher_384"],
        }
        for r in sorted(records, key=key, reverse=True)
    ]
    decision = {
        "rule": "argmax recall_at_5; tie-break higher ndcg_at_10; then lower epoch (committed pre-run, run_config.json)",
        "selected_epoch": best["epoch"],
        "selected_checkpoint": str(args.run_dir / f"epoch{best['epoch']}"),
        "selected_s1m_val": best["s1m_val"],
        "val_cosine_vs_teacher_384": best["val_cosine_vs_teacher_384"],
        "ranking": ranking,
    }
    out = args.out or (args.run_dir / "selection.json")
    out.write_text(json.dumps(decision, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(decision, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
