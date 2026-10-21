#!/usr/bin/env python3
"""Round-8 STAGE-2 teacher precompute — encode ALL 26069 corpus texts with
the FT'd harrier (stage-1 selection output) → teacher_vectors.npz.

Recipe = run_config.json stage1.encoding_recipe VERBATIM (bare text — NO
instruction template, last-token pooling, bf16 compute / fp32 npz on save,
max_length 256, batch 32) — the SAME recipe the stage-1 base/FT proxy
evals used, so the eval numbers characterise exactly these vectors.

Proof obligations (teacher_precompute_state.json + ft_selection provenance):
  1. the encoded model = runs/embed-r8/epoch<chosen> per ft_selection.json
     (chosen_epoch asserted, model id + revision recorded);
  2. final npz lockstep: texts == r8 corpus texts (order included), vectors
     (26069, 1024) fp32 all finite — asserted on reload;
  3. resume via partial npz (same prefix assert as precompute_teacher_r7).

Output: training/runs/embed-r8/teacher_vectors.npz
  vectors (26069, 1024) fp32, texts — train rows first, then val rows
  (lockstep with the r8 corpus file order; re-asserted by the trainer).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np

TRAIN_DIR = Path(__file__).resolve().parents[1]
if str(TRAIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(TRAIN_DIR.parent))
CORPUS_DIR = Path(
    "/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r5-corpus/data/embed-r7/corpus"
)
EXPECTED_FP = "ad3d438f89ce91f47d279b63f1e88157ce747cb3a128399db1df707e8e1a3b7e"
OUT_DIR = TRAIN_DIR / "runs" / "embed-r8"
S1_DIR = OUT_DIR / "ft-stage1"  # stage-1 artifacts live SEPARATELY (metrics schema/epoch collision guard)
OUT_NPZ = OUT_DIR / "teacher_vectors.npz"
PARTIAL_NPZ = OUT_DIR / "teacher_vectors.partial.npz"
STATE_JSON = OUT_DIR / "teacher_precompute_state.json"
MAX_LENGTH = 256
BATCH = 32
THREADS = 12
SAVE_EVERY_BATCHES = 8


def corpus_texts() -> list[str]:
    digest = hashlib.sha256()
    texts: list[str] = []
    for name in ("train.jsonl", "val.jsonl"):
        p = CORPUS_DIR / name
        digest.update(p.read_bytes())
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    texts.append(json.loads(line)["text"])
    if digest.hexdigest() != EXPECTED_FP:
        raise SystemExit(f"CORPUS FINGERPRINT MISMATCH: {digest.hexdigest()}")
    return texts


MODEL_ID = "microsoft/harrier-oss-v1-0.6b"
MODEL_REVISION = "f9b9dc8d367d443f2479d27aa5d8d2850c0774ee"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=THREADS)
    args = ap.parse_args()

    sel = json.loads((S1_DIR / "ft_selection.json").read_text())
    chosen = sel.get("chosen_epoch")
    if chosen is None:
        raise SystemExit("ft_selection.json has no chosen_epoch — run select_ft_r8 first")
    if chosen == 0:
        # selection-rule fallback: the RAW pinned base is the teacher (the
        # pre-committed rule degrades a geometry-harmful FT to the
        # sweep-audited base). No epoch0 checkpoint exists by design — load
        # the pinned HF snapshot: bit-identical to the weights the stage-1
        # epoch-0 base readout measured.
        ckpt = MODEL_ID
        from_kw = {"revision": MODEL_REVISION}
        print("teacher = RAW PINNED BASE (selection fallback, chosen_epoch=0)", flush=True)
    else:
        ckpt = S1_DIR / f"epoch{chosen}"
        from_kw = {}
        if not (ckpt / "model.safetensors").exists() and not (ckpt / "config.json").exists():
            raise SystemExit(f"chosen checkpoint missing: {ckpt}")

    texts = corpus_texts()
    print(f"r8 corpus OK: {len(texts)} texts (fp {EXPECTED_FP[:12]}…); teacher = epoch{chosen}", flush=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    import torch
    from transformers import AutoModel, AutoTokenizer

    from training.distill import last_token_pool

    torch.set_num_threads(args.threads)
    tok = AutoTokenizer.from_pretrained(ckpt, **from_kw)
    model = AutoModel.from_pretrained(ckpt, dtype=torch.bfloat16, **from_kw).to("cpu").eval()
    assert int(getattr(model.config, "hidden_size", 0)) == 1024, "teacher hidden != 1024"

    vectors = np.zeros((len(texts), 1024), dtype=np.float32)
    done = 0
    if PARTIAL_NPZ.exists():
        partial = np.load(PARTIAL_NPZ, allow_pickle=True)
        ptexts = [str(x) for x in partial["texts"]]
        if ptexts == texts[: len(ptexts)]:
            done = len(ptexts)
            vectors[:done] = partial["vectors"]
            print(f"resume: {done} texts already encoded", flush=True)
        else:
            raise SystemExit("partial npz diverges from corpus prefix — delete it and restart")

    t0 = time.perf_counter()
    since_save = 0
    pos = done
    while pos < len(texts):
        chunk = texts[pos : pos + BATCH]
        enc = tok(chunk, padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="pt")
        with torch.no_grad():
            out = model(**enc)
        pooled = last_token_pool(out.last_hidden_state, enc["attention_mask"])
        arr = pooled.float().cpu().numpy()
        vectors[pos : pos + len(chunk)] = arr
        pos += len(chunk)
        since_save += 1
        el = time.perf_counter() - t0
        print(f"teacher encode: {pos}/{len(texts)} elapsed={el:.0f}s rate={pos / max(el, 1e-9):.2f}/s",
              flush=True)
        if since_save >= SAVE_EVERY_BATCHES or pos >= len(texts):
            tmp = PARTIAL_NPZ.with_suffix(".tmp.npz")
            np.savez(tmp, vectors=vectors[:pos], texts=np.array([str(t) for t in texts[:pos]], dtype=object))
            tmp.replace(PARTIAL_NPZ)
            STATE_JSON.write_text(json.dumps({
                "encoded": pos, "total": len(texts),
                "model": str(ckpt), "epoch": chosen,
                "recipe": "bare text, last-token pooling, bf16 compute, fp32 npz (run_config stage1.encoding_recipe)",
            }) + "\n")
            since_save = 0

    assert np.isfinite(vectors).all(), "non-finite vectors in assembled npz"
    np.savez(OUT_NPZ, vectors=vectors, texts=np.array([str(t) for t in texts], dtype=object))
    PARTIAL_NPZ.unlink(missing_ok=True)

    back = np.load(OUT_NPZ, allow_pickle=True)
    assert [str(x) for x in back["texts"]] == texts, "final npz lockstep violated"
    assert back["vectors"].shape == (len(texts), 1024)
    STATE_JSON.write_text(json.dumps({
        "encoded": len(texts), "total": len(texts), "final": True,
        "model": str(ckpt), "epoch": chosen,
        "recipe": "bare text, last-token pooling, bf16 compute, fp32 npz (run_config stage1.encoding_recipe)",
    }) + "\n")

    proof = {
        "kind": "embed-r8-teacher-precompute-proof",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "teacher_ckpt": str(ckpt),
        "teacher_revision": MODEL_REVISION if chosen == 0 else "local stage-1 checkpoint",
        "teacher_route": "selection fallback: raw pinned base (chosen_epoch=0)" if chosen == 0
        else f"FT stage-1 checkpoint epoch{chosen}",
        "stage1_chosen_epoch": chosen,
        "npz_sha256": hashlib.sha256(OUT_NPZ.read_bytes()).hexdigest(),
        "out_shape": list(back["vectors"].shape),
    }
    (OUT_DIR / "teacher_precompute_proof.json").write_text(json.dumps(proof, indent=1) + "\n")
    print(f"teacher vectors: {back['vectors'].shape} -> {OUT_NPZ}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
