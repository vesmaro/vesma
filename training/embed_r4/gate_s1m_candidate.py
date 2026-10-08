"""S1m gate measurement (g1/g2) — round-4 candidate, PRODUCTION mechanics.

Prereg round-4 §3 + BASELINE.md §10: the candidate is measured on the
SEALED judged corpus c2ce056d (191 judged queries) through the exact
production S1m path — golden manager ingest + `_measure_queries` +
`measure_production_embedder` — with the candidate ONNX artifact
installed as the nano provider via EmbeddingConfig(model=<path>).
Self-comparison only (never the BLAKE2b reference).

SINGLE-SHOT discipline (prereg §4, mission protocol): this script runs
EXACTLY ONCE per selected checkpoint, after training and export are
final. Re-running it on other checkpoints is gate-shopping.

Modes:
  --self-check   measure the PRODUCTION bundled artifact instead of the
                 candidate. Plumbing validation only: with a deterministic
                 corpus + queries it must reproduce the recorded BASELINE
                 §10 numbers (recall@5 0.9484) exactly. Produces no
                 candidate numbers; run BEFORE the single-shot.

Both modes assert the judged-corpus fingerprint equals the sealed
c2ce056d57d91143… and record it in the report for provenance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # engine repo root (worktree)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SEALED_CORPUS_FP = "c2ce056d57d91143f7a1959442ef2b37891464d4cd5f218f5eabbc785c8e72f1"
G1_FLOOR = 0.9252  # no-harm: baseline 0.9484 - max(0.02; ci95 0.0232)
G2_TARGET = 0.96  # ratified target gain (leadership, not parity)
BASELINE_RECALL_AT_5 = 0.9484  # BASELINE.md §10 (production artifact, 2026-09-15)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def measure(artifact: str | None) -> dict:
    """One measured pass through the production S1m mechanics.

    ``artifact=None`` → the default production config (self-check mode);
    otherwise a path to the candidate model.onnx.
    """
    from benchmarks.stands.s1_quality.harness import build_golden_manager, _measure_queries
    from benchmarks.stands.s1_quality.model_contour import measure_production_embedder
    from benchmarks.stands.s1_quality.run import corpus_fingerprint
    from vesmaro.config import EmbeddingConfig
    from vesmaro.embeddings import create_embedding_provider

    corpus_fp = corpus_fingerprint()
    if corpus_fp != SEALED_CORPUS_FP:
        raise SystemExit(
            f"ABORT: judged-corpus fingerprint mismatch: {corpus_fp} != {SEALED_CORPUS_FP} "
            "(benchmarks/corpus modules drifted — this is not the sealed corpus)"
        )

    if artifact is None:
        cfg = EmbeddingConfig()
    else:
        onnx = Path(artifact).expanduser().resolve()
        if not onnx.is_file():
            raise SystemExit(f"error: artifact not found: {onnx}")
        cfg = EmbeddingConfig(provider="nano", model=str(onnx))
    embedder = create_embedding_provider(cfg)
    embedder.embed("mnemos s1m warmup")

    with tempfile.TemporaryDirectory(prefix="mnemos-s1m-r4-") as tmp:
        mgr, slug_to_id = build_golden_manager(Path(tmp) / "s1m", embedder=embedder)
        try:
            measurements = _measure_queries(mgr, slug_to_id)
        finally:
            mgr.close()
    ranked = [(m.qid, list(m.result_slugs)) for m in measurements]
    metrics = measure_production_embedder(ranked)
    return {
        "metrics": metrics,
        "corpus_fingerprint": corpus_fp,
        "artifact_path": str(Path(artifact).resolve()) if artifact else "bundled:vesma-embed-v1",
        "artifact_sha256": sha256_file(Path(artifact).resolve()) if artifact else sha256_file(
            Path(
                __import__("vesmaro.embeddings", fromlist=["mnema_artifact_onnx_path"])
                .mnema_artifact_onnx_path("vesma-embed-v1")
            )
        ),
        "dimension": getattr(embedder, "dimension", None),
        "embedder_fingerprint": getattr(embedder, "fingerprint", None),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S1m single-shot gate (g1/g2) for the round-4 candidate")
    ap.add_argument("--artifact", default=None, help="path to candidate model.onnx (omit with --self-check)")
    ap.add_argument("--self-check", action="store_true", help="measure the PRODUCTION artifact (plumbing validation)")
    ap.add_argument("--out", required=True, type=Path, help="report json path")
    args = ap.parse_args(argv)
    if bool(args.artifact) == args.self_check:
        ap.error("exactly one of --artifact / --self-check is required")

    report = measure(args.artifact)
    metrics = report["metrics"]
    r5 = float(metrics["recall_at_5"])

    if args.self_check:
        ok = abs(r5 - BASELINE_RECALL_AT_5) < 1e-9
        verdict = {
            "mode": "self-check",
            "expected_recall_at_5": BASELINE_RECALL_AT_5,
            "plumbing_ok": ok,
            "note": (
                "harness self-check against the recorded BASELINE §10 — deterministic "
                "corpus/queries must reproduce it exactly; candidate gates not computed here"
            ),
        }
    else:
        verdict = {
            "mode": "single-shot-candidate",
            "g1_no_harm": {
                "threshold": G1_FLOOR,
                "measured": r5,
                "pass": r5 >= G1_FLOOR,
            },
            "g2_target_gain": {
                "threshold": G2_TARGET,
                "measured": r5,
                "pass": r5 >= G2_TARGET,
            },
            "adopt_rule": "ADOPT = g1 AND g2 (prereg §3; g1 without g2 = REJECT)",
        }
    report["verdict"] = verdict
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["verdict"], ensure_ascii=False, indent=2))
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
