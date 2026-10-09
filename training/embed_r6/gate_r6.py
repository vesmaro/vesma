#!/usr/bin/env python3
"""Round-6 single-shot gate runner — prereg thresholds UNCHANGED.

Mechanics = round-5 gate (training.embed_r5.gate_r5) VERBATIM — same
NanoProvider harness, same prod plumbing assert (stored characterization
numbers, engine runtime ORT 1.27), same threshold table, same monitored
surfaces, same g6a delegation to gate_latency.py (engine runtime), same
refusal to run on a non-chosen artifact. Only the report kind and the
run-context strings differ.

Runs EXACTLY ONCE on the chosen final artifact
(export/int8-dynamic/model.onnx — the prereg-fixed form). Population =
sealed gate set cb367577 (180 pairs), one pass, no re-rolls, no
post-hoc checkpoint switching. Honest REJECT is a final legal outcome.

Binding gates (round-5 prereg §4.1, inherited unchanged):
  g1   S1m no-harm >= 0.9252  (judged c2ce056d, production mechanics —
       runs ONCE separately via gate_s1m_candidate.py --artifact)
  gX0  |delta| mono control median vs prod 0.8982 <= 0.005
  seed-dup (n=22) median >= 0.85 AND share(cos>=0.85) >= 0.50
  llm      (n=68) median >= 0.93 AND share(cos>=0.85) >= 0.90
  g6a  mean latency <= 45 ms (§A.2, engine runtime)
  g7   artifact size <= 60 MB
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

TRAIN_DIR = Path(__file__).resolve().parents[1]  # vesma/training
if str(TRAIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(TRAIN_DIR.parent))

from training.embed_r5.gate_r5 import main as gate_main  # noqa: E402  (proven round-5 runner)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="round-6 single-shot gate pass (round-5 mechanics, kind=r6)")
    ap.add_argument("--artifact-dir", required=True, type=Path,
                    help="chosen form dir (model.onnx + tokenizer.json) — the prereg-fixed int8-dynamic form")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--sibling-pop", type=Path,
                    default=TRAIN_DIR / "runs" / "embed-r6" / "sibling_pop.json")
    ap.add_argument("--skip-latency-subprocess", action="store_true")
    args = ap.parse_args(argv)

    out: Path = args.out
    report_path = out

    # Run the proven round-5 runner (its main() parses sys.argv) into a temp
    # path, then re-kind the report.
    tmp_out = out.with_suffix(".r5-mechanics.json")
    sys.argv = [
        "gate_r5-mechanics",
        "--artifact-dir", str(args.artifact_dir),
        "--out", str(tmp_out),
        "--sibling-pop", str(args.sibling_pop),
    ] + (["--skip-latency-subprocess"] if args.skip_latency_subprocess else [])
    code = gate_main()
    if code != 0:
        return code

    report = json.loads(tmp_out.read_text())
    report["kind"] = "embed-r6-single-shot-gate-pass"
    report["round6_note"] = (
        "round-6 decisive gate: mechanics/thresholds/population identical to round-5 "
        "(training.embed_r5.gate_r5); artifact form prereg-fixed to int8-dynamic; "
        "single pass, no re-rolls"
    )
    report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    tmp_out.unlink(missing_ok=True)
    print(f"-> {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
