#!/usr/bin/env python3
"""Round-5 teacher precompute — frozen Qwen3-Embedding-0.6B over ALL 26681 texts.

Round-4 lockstep recipe (train_r4.precompute_teacher) + RESUMABILITY:
the box runs at 5-6 GB available RAM and the precompute is a ~2.2 h CPU
job, so partial state is persisted incrementally (atomic rename) and a
restart continues from the last checkpoint instead of losing the pass
(stop-and-resume rule, round-4 precedent).

Determinism: identical vectors whether the job ran straight through or
was resumed — the teacher is frozen, batches are fixed (32, corpus
order), and pooling is per-text.

Output: training/runs/embed-r5/teacher_vectors.npz
  vectors (26681, 1024) fp32, texts — train rows first, then val rows
  (lockstep with the corpus file order; asserted by the trainer).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

TRAIN_DIR = Path(__file__).resolve().parents[1]
CORPUS_DIR = Path(
    "/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r5-corpus/data/embed-r5/corpus"
)
EXPECTED_FP = "e64fdbea65f0e6a4f2830a42ea6bd0d74a2974854d381427c4d5ba2ea4901c62"
OUT_NPZ = TRAIN_DIR / "runs" / "embed-r5" / "teacher_vectors.npz"
PARTIAL_NPZ = TRAIN_DIR / "runs" / "embed-r5" / "teacher_vectors.partial.npz"
STATE_JSON = TRAIN_DIR / "runs" / "embed-r5" / "teacher_precompute_state.json"

TEACHER_ID = "Qwen/Qwen3-Embedding-0.6B"
TEMPLATE = "Given a memory note, retrieve similar notes"
MAX_LENGTH = 256
BATCH = 32
THREADS = 8
SAVE_EVERY_BATCHES = 64  # every ~2048 texts


def corpus_texts() -> list[str]:
    digest = __import__("hashlib").sha256()
    texts: list[str] = []
    for name in ("train.jsonl", "val.jsonl"):
        p = CORPUS_DIR / name
        digest.update(p.read_bytes())
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    texts.append(json.loads(line)["text"])
    if digest.hexdigest() != EXPECTED_FP:
        raise SystemExit(f"CORPUS FINGERPRINT MISMATCH: {digest.hexdigest()}")
    return texts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=THREADS)
    args = ap.parse_args()

    import torch
    from transformers import AutoModel, AutoTokenizer

    repo_root = str(TRAIN_DIR.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from training.distill import detect_teacher_pooling, format_teacher_input, last_token_pool

    texts = corpus_texts()
    print(f"corpus OK: {len(texts)} texts (fp {EXPECTED_FP[:12]}…)", flush=True)
    OUT_NPZ.parent.mkdir(parents=True, exist_ok=True)

    done = 0
    vecs: list[np.ndarray] = []
    if PARTIAL_NPZ.exists():
        partial = np.load(PARTIAL_NPZ, allow_pickle=True)
        ptexts = [str(x) for x in partial["texts"]]
        if ptexts == texts[: len(ptexts)]:
            done = len(ptexts)
            vecs = [partial["vectors"]]
            print(f"resume: {done} texts already encoded", flush=True)
        else:
            raise SystemExit("partial npz texts diverge from corpus prefix — delete it and restart")

    tok = AutoTokenizer.from_pretrained(TEACHER_ID)
    model = AutoModel.from_pretrained(TEACHER_ID, dtype=torch.bfloat16).to("cpu").eval()
    if detect_teacher_pooling(model, TEACHER_ID) == "last_token":
        tok.padding_side = "left"

    t_start = time.perf_counter()
    n_new = 0
    since_save = 0
    for start in range(done, len(texts), BATCH):
        chunk = [format_teacher_input(t, TEMPLATE) for t in texts[start : start + BATCH]]
        enc = tok(chunk, padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="pt")
        with torch.no_grad():
            out = model(**enc)
        pooled = last_token_pool(out.last_hidden_state, enc["attention_mask"])
        vecs.append(pooled.float().cpu().numpy())
        n_new += len(chunk)
        since_save += 1
        done_now = start + len(chunk)
        if (start // BATCH) % 10 == 0:
            el = time.perf_counter() - t_start
            rate = n_new / max(el, 1e-9)
            eta = (len(texts) - done_now) / max(rate, 1e-9)
            print(
                f"teacher encode: {done_now}/{len(texts)} elapsed={el:.0f}s "
                f"rate={rate:.2f}/s eta={eta / 60:.0f}min",
                flush=True,
            )
        if since_save >= SAVE_EVERY_BATCHES or done_now >= len(texts):
            arr = np.concatenate(vecs, axis=0).astype(np.float32)
            tmp = PARTIAL_NPZ.with_suffix(".tmp.npz")
            np.savez(tmp, vectors=arr, texts=np.array([str(t) for t in texts[:done_now]], dtype=object))
            tmp.replace(PARTIAL_NPZ)  # atomic
            STATE_JSON.write_text(json.dumps({"encoded": done_now, "total": len(texts)}) + "\n")
            since_save = 0

    arr = np.concatenate(vecs, axis=0).astype(np.float32)
    assert arr.shape[0] == len(texts), f"{arr.shape} vs {len(texts)}"
    np.savez(OUT_NPZ, vectors=arr, texts=np.array([str(t) for t in texts], dtype=object))
    PARTIAL_NPZ.unlink(missing_ok=True)
    STATE_JSON.write_text(json.dumps({"encoded": len(texts), "total": len(texts), "final": True}) + "\n")
    print(f"teacher vectors: {arr.shape} -> {OUT_NPZ} in {time.perf_counter()-t_start:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
