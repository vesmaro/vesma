"""g3 MRL-slice gate — round-4 candidate, eval-harness (prereg §3 g3).

The engine runtime does NOT consume MRL slices yet (prereg §7 anti-scope:
the runtime feature is a separate lane) — so g3 is measured by the
prereg-sanctioned eval harness: embed the SEALED judged corpus (docs and
golden queries) once through the production NanoProvider tokenization,
then per slice d in {256,128,64} truncate + L2-renormalise BOTH sides and
rank by cosine; compare per-slice recall@5 against the FULL-384 number
measured by the SAME harness in the SAME pass.

Ratified tolerances: 256 >= full-0.02; 128 >= full-0.03; 64 >= full-0.05.

Doc embedding text mirrors the production _embedding_text composition
(title + content + tags, "\n"-joined, [:4096]); queries embed raw text —
the same inputs the production vector leg sees.

SINGLE-SHOT discipline: one pass on the selected checkpoint's artifact,
after export, together with the g1/g2 single-shot (no re-rolls).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]  # engine repo root (worktree)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SEALED_CORPUS_FP = "c2ce056d57d91143f7a1959442ef2b37891464d4cd5f218f5eabbc785c8e72f1"
SLICES = (256, 128, 64)
TOLERANCES = {256: 0.02, 128: 0.03, 64: 0.05}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def embedding_text(entry: object) -> str:
    """Mirror vesmaro.manager._embedding_text (title + content + tags)."""
    parts: list[str] = []
    if getattr(entry, "title", None):
        parts.append(entry.title)
    parts.append(entry.content)
    tags = getattr(entry, "tags", None)
    if tags:
        parts.append(" ".join(tags))
    return "\n".join(parts)[:4096]


def recall_at_5(qs: object, ds: object, expected: list[list[int]]) -> float:
    """Mean recall@5 over judged queries under cosine ranking."""
    import numpy as np

    q = qs / np.maximum(np.linalg.norm(qs, axis=1, keepdims=True), 1e-9)
    d = ds / np.maximum(np.linalg.norm(ds, axis=1, keepdims=True), 1e-9)
    sims = q @ d.T
    order = np.argsort(-sims, axis=1)[:, :5]
    scores = []
    for i, top in enumerate(order):
        rel = set(expected[i])
        hits = len(rel & {int(j) for j in top})
        scores.append(hits / len(rel) if rel else 0.0)
    return sum(scores) / max(1, len(scores))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="g3 MRL-slice gate (eval harness)")
    ap.add_argument("--artifact", required=True, help="path to the candidate model.onnx")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)

    from benchmarks.corpus.corpus import CORPUS
    from benchmarks.corpus.queries import GOLDEN_QUERIES
    from benchmarks.stands.s1_quality.run import corpus_fingerprint
    from vesmaro.config import EmbeddingConfig
    from vesmaro.embeddings import create_embedding_provider

    corpus_fp = corpus_fingerprint()
    if corpus_fp != SEALED_CORPUS_FP:
        raise SystemExit(f"ABORT: judged-corpus fingerprint mismatch: {corpus_fp}")

    onnx = Path(args.artifact).expanduser().resolve()
    if not onnx.is_file():
        raise SystemExit(f"error: artifact not found: {onnx}")
    embedder = create_embedding_provider(EmbeddingConfig(provider="nano", model=str(onnx)))

    slug_to_idx = {e.slug: i for i, e in enumerate(CORPUS)}
    doc_texts = [embedding_text(e) for e in CORPUS]
    judged = [q for q in GOLDEN_QUERIES if q.expected]
    q_texts = [q.text for q in judged]
    expected = [[slug_to_idx[s] for s in q.expected if s in slug_to_idx] for q in judged]
    if any(not e for e in expected):
        raise SystemExit("ABORT: a judged query references slugs outside CORPUS")

    doc_vecs = embedder.embed_batch(doc_texts)
    q_vecs = embedder.embed_batch(q_texts)

    import numpy as np

    ds_full = np.asarray(doc_vecs, dtype=np.float64)
    qs_full = np.asarray(q_vecs, dtype=np.float64)

    def slice_to(a: object, d: int) -> object:
        return a[..., :d]

    full_r5 = recall_at_5(qs_full, ds_full, expected)
    slices = {}
    for d in SLICES:
        r5 = recall_at_5(slice_to(qs_full, d), slice_to(ds_full, d), expected)
        tol = TOLERANCES[d]
        slices[str(d)] = {
            "recall_at_5": round(r5, 6),
            "full_minus": round(full_r5 - r5, 6),
            "tolerance": tol,
            "pass": r5 >= full_r5 - tol,
        }

    report = {
        "method": "eval harness: production NanoProvider tokenization, cosine ranking "
        "over the sealed judged corpus; slices truncated + L2-renormalised; "
        "full-384 measured by the same harness in the same pass",
        "corpus_fingerprint": corpus_fp,
        "artifact_path": str(onnx),
        "artifact_sha256": sha256_file(onnx),
        "n_docs": len(CORPUS),
        "n_judged_queries": len(judged),
        "full_384_recall_at_5": round(full_r5, 6),
        "slices": slices,
        "g3_pass": all(s["pass"] for s in slices.values()),
        "note": "g3 gate = all slices within ratified tolerances of the same-harness full-384 number",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
