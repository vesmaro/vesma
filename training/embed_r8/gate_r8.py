#!/usr/bin/env python3
"""Round-8 single-shot gate runner — mechanics = round-5 gate VERBATIM
(training.embed_r5.gate_r5), with the RATIFIED round-8 llm recalibration
(r8 prereg §2) as the ONLY threshold delta:

  llm_median  0.93 -> 0.90   (prod anchor 0.872 + 0.028)
  llm_share   0.90 -> 0.75   (prod anchor 0.647 + 0.10)

Everything else UNCHANGED: g1 0.9252, gX0 |Δ|<=0.005 vs prod 0.8982,
seed-dup 0.85/0.50, g6a 45 ms, g7 60 MB, the same NanoProvider harness,
the same prod plumbing assert (stored characterization floors, engine
runtime ORT 1.27), the same sealed gate set cb367577 (sha-asserted by
es5.load_gate_set), the same monitored surfaces, g6a delegation.

The wrapper patches ONLY the two recalibrated threshold entries in the
proven runner's THRESHOLDS table (asserted before + after), runs it once
on the chosen artifact (export/int8-dynamic/model.onnx — the prereg-fixed
form), and re-kinds the report. One pass, no re-rolls, no post-hoc
checkpoint switching. Honest REJECT is a final legal outcome (r8 §5).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TRAIN_DIR = Path(__file__).resolve().parents[1]  # vesma/training
if str(TRAIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(TRAIN_DIR.parent))

from training.embed_r5 import gate_r5  # noqa: E402  (proven round-5 runner)

RECALIBRATED = {
    # r8 prereg §2 — the ONLY gate delta of round-8
    "llm_median": 0.90,
    "llm_share": 0.75,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="round-8 single-shot gate pass (r5 mechanics, r8 recalibrated llm)")
    ap.add_argument("--artifact-dir", required=True, type=Path,
                    help="chosen form dir (model.onnx + tokenizer.json) — the prereg-fixed int8-dynamic form")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--sibling-pop", type=Path,
                    default=TRAIN_DIR / "runs" / "embed-r8" / "sibling_pop.json")
    ap.add_argument("--skip-latency-subprocess", action="store_true")
    args = ap.parse_args(argv)

    # patch the proven runner's threshold table — recalibrated entries only
    before = dict(gate_r5.THRESHOLDS)
    gate_r5.THRESHOLDS["llm_median"] = RECALIBRATED["llm_median"]
    gate_r5.THRESHOLDS["llm_share"] = RECALIBRATED["llm_share"]
    expected_after = dict(before)
    expected_after.update(RECALIBRATED)
    if gate_r5.THRESHOLDS != expected_after:
        raise SystemExit("THRESHOLD PATCH SANITY FAILED — aborting before any measurement")

    out: Path = args.out
    tmp_out = out.with_suffix(".r5-mechanics.json")
    sys.argv = [
        "gate_r5-mechanics",
        "--artifact-dir", str(args.artifact_dir),
        "--out", str(tmp_out),
        "--sibling-pop", str(args.sibling_pop),
    ] + (["--skip-latency-subprocess"] if args.skip_latency_subprocess else [])
    code = gate_r5.main()
    if code != 0:
        return code

    report = json.loads(tmp_out.read_text())
    # the report must carry EXACTLY the patched table (no silent drift)
    if report.get("thresholds") != expected_after:
        raise SystemExit("REPORT THRESHOLD MISMATCH vs the recalibrated table")
    report["kind"] = "embed-r8-single-shot-gate-pass"
    report["round8_note"] = (
        "round-8 decisive gate: mechanics/population/plumbing identical to round-5/6/7 "
        "(training.embed_r5.gate_r5 under patched THRESHOLDS); llm recalibrated per r8 prereg §2 "
        "(0.90/0.75 = prod anchor + honest margin; superseded 0.93/0.90); artifact form "
        "prereg-fixed to int8-dynamic; single pass, no re-rolls; the round-8 variable is the "
        "teacher source (FT'd harrier)"
    )
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    tmp_out.unlink(missing_ok=True)
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
