#!/usr/bin/env python3
"""Round-7 teacher precompute — PARTIAL REUSE from the round-6 run + recompute
of exactly the changed llm texts (mission input contract; round-7 prereg §2:
corpus changed ONLY in llm-class rows).

Teacher UNCHANGED (Qwen/Qwen3-Embedding-0.6B, round-4/5/6 recipe verbatim:
instruction template, max_length 256, batch 32, bf16 CPU, last-token
pooling) — so a text present in BOTH corpora has a DETERMINISTIC identical
vector. Reuse rule: RAW-STRING equality into the round-6 npz
(26681 texts, runs/embed-r6, read-only source) — text_hash() normalises
(a case-fold collision old-vs-new llm text was caught in flight), so the
hash only narrows candidates and the raw string decides; the frozen
teacher encoded the raw strings, therefore byte-identical strings reuse
their vector deterministically. Every non-reused text is encoded fresh.

Proof obligations (written to reuse_proof.json + stdout, appended to runlog):
  1. reused texts: r6-vector identity via text_hash AND string equality;
  2. EVERY reused-in-r7 non-llm corpus row is covered by reuse (assert: no
     non-llm row is recomputed);
  3. recomputed set ⊆ r7 rows with source translated-llm-r4 (assert);
  4. r6 texts absent from r7 = exactly the 612 dropped llm dst texts (counted);
  5. final npz lockstep: texts == r7 corpus texts (order included), vectors
     (26069, 1024) fp32 all finite.

Output: training/runs/embed-r7/teacher_vectors.npz
  vectors (26069, 1024) fp32, texts — train rows first, then val rows
  (lockstep with the r7 corpus file order; asserted by the trainer).
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
CORPUS_DIR = Path(
    "/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r5-corpus/data/embed-r7/corpus"
)
EXPECTED_FP = "ad3d438f89ce91f47d279b63f1e88157ce747cb3a128399db1df707e8e1a3b7e"
R6_NPZ = Path(
    "/var/home/abyss/LABs/Projects/Project-Vesma/vesma/wt/embed-r6-train/training/runs/embed-r6/teacher_vectors.npz"
)
OUT_DIR = TRAIN_DIR / "runs" / "embed-r7"
OUT_NPZ = OUT_DIR / "teacher_vectors.npz"
PARTIAL_NPZ = OUT_DIR / "teacher_vectors.partial.npz"
STATE_JSON = OUT_DIR / "teacher_precompute_state.json"
PROOF_JSON = OUT_DIR / "reuse_proof.json"
RUNLOG = TRAIN_DIR / "embed_r7" / "runlog.md"

TEACHER_ID = "Qwen/Qwen3-Embedding-0.6B"
TEMPLATE = "Given a memory note, retrieve similar notes"
MAX_LENGTH = 256
BATCH = 32
THREADS = 8
SAVE_EVERY_BATCHES = 8  # small recompute set — save often

sys.path.insert(0, str(Path("/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r4-corpus/scripts")))
from gen_embed_r4_texts import text_hash  # noqa: E402  (r4 scripts, read-only, pilot-proven)


def corpus_rows() -> tuple[list[str], list[dict]]:
    """r7 corpus texts (train then val) + parsed rows; asserts sealed fp."""
    digest = hashlib.sha256()
    texts: list[str] = []
    rows: list[dict] = []
    for name in ("train.jsonl", "val.jsonl"):
        p = CORPUS_DIR / name
        digest.update(p.read_bytes())
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                texts.append(r["text"])
                rows.append(r)
    if digest.hexdigest() != EXPECTED_FP:
        raise SystemExit(f"CORPUS FINGERPRINT MISMATCH: {digest.hexdigest()}")
    return texts, rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=THREADS)
    args = ap.parse_args()

    texts, rows = corpus_rows()
    print(f"r7 corpus OK: {len(texts)} texts (fp {EXPECTED_FP[:12]}…)", flush=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── reuse mapping against the round-6 npz (read-only source) ────────────
    r6 = np.load(R6_NPZ, allow_pickle=True)
    r6_texts = [str(x) for x in r6["texts"]]
    r6_vecs = r6["vectors"]
    assert r6_vecs.shape[0] == len(r6_texts), "r6 npz malformed"
    r6_sha = hashlib.sha256(R6_NPZ.read_bytes()).hexdigest()
    print(f"r6 npz: {len(r6_texts)} texts, vectors {r6_vecs.shape}, sha {r6_sha[:12]}…", flush=True)

    # REUSE CRITERION = RAW-STRING EQUALITY. text_hash() normalises
    # (case-fold etc.), so a hash hit does NOT imply the teacher saw the same
    # input (r7 case found in flight: 'Helios-Index …' vs 'Helios-index …' —
    # old 0.6B vs new 1.7B text, same normalised form). The npz stores the
    # raw strings the teacher encoded; only byte-identical strings reuse.
    r6_index: dict[str, list[int]] = {}
    for i, t in enumerate(r6_texts):
        r6_index.setdefault(text_hash(t), []).append(i)

    r6_string_set = set(r6_texts)
    vectors = np.zeros((len(texts), r6_vecs.shape[1]), dtype=np.float32)
    todo: list[int] = []
    n_reused = 0
    recomputed_non_llm: list[int] = []
    for i, t in enumerate(texts):
        hit = None
        for j in r6_index.get(text_hash(t), ()):  # hash narrows; string decides
            if r6_texts[j] == t:
                hit = j
                break
        if hit is not None:
            vectors[i] = r6_vecs[hit]
            n_reused += 1
        else:
            todo.append(i)
            if rows[i]["source"] != "translated-llm-r4":
                recomputed_non_llm.append(i)

    dropped_r6 = [t for t in r6_texts if t not in set(texts)]

    assert not recomputed_non_llm, (
        f"PROOF FAILURE: {len(recomputed_non_llm)} non-llm rows would be recomputed "
        f"(first: {recomputed_non_llm[:3]}) — corpus delta is llm-only"
    )
    assert all(rows[i]["source"] == "translated-llm-r4" for i in todo), "recompute set left llm class"

    # cross-check: every r6 text absent from r7 must be a translated-llm-r4 row
    # of the sealed r5 corpus (the 612 dropped dst texts) — read-only data probe
    r5_corpus_dir = Path(
        "/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r5-corpus/data/embed-r5/corpus"
    )
    r5_class: dict[str, str] = {}
    for name in ("train.jsonl", "val.jsonl"):
        with open(r5_corpus_dir / name, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    r = json.loads(line)
                    r5_class[text_hash(r["text"])] = r["source"]
    dropped_non_llm = sum(
        1 for t in dropped_r6 if r5_class.get(text_hash(t)) != "translated-llm-r4"
    )
    assert dropped_non_llm == 0, (
        f"PROOF FAILURE: {dropped_non_llm} dropped r6 texts are NOT r5 llm-class rows "
        "— corpus drift beyond the llm class"
    )
    print(f"reuse mapping: reused={n_reused} recompute={len(todo)} r6-texts-absent-from-r7={len(dropped_r6)} "
          f"(all r5 llm-class: {dropped_non_llm == 0})", flush=True)

    # ── encode the missing (same recipe as precompute_teacher_r5) ───────────
    done = 0
    if PARTIAL_NPZ.exists():
        partial = np.load(PARTIAL_NPZ, allow_pickle=True)
        ptexts = [str(x) for x in partial["texts"]]
        if ptexts == [texts[i] for i in todo[: len(ptexts)]]:
            done = len(ptexts)
            for k, i in enumerate(todo[:done]):
                vectors[i] = partial["vectors"][k]
            print(f"resume: {done} recompute texts already encoded", flush=True)
        else:
            raise SystemExit("partial npz diverges from recompute list — delete it and restart")

    if done < len(todo):
        import torch
        from transformers import AutoModel, AutoTokenizer

        repo_root = str(TRAIN_DIR.parent)
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from training.distill import detect_teacher_pooling, format_teacher_input, last_token_pool

        torch.set_num_threads(args.threads)
        tok = AutoTokenizer.from_pretrained(TEACHER_ID)
        model = AutoModel.from_pretrained(TEACHER_ID, dtype=torch.bfloat16).to("cpu").eval()
        if detect_teacher_pooling(model, TEACHER_ID) == "last_token":
            tok.padding_side = "left"

        t0 = time.perf_counter()
        since_save = 0
        pos = done
        while pos < len(todo):
            idx_chunk = todo[pos : pos + BATCH]
            chunk = [format_teacher_input(texts[i], TEMPLATE) for i in idx_chunk]
            enc = tok(chunk, padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="pt")
            with torch.no_grad():
                out = model(**enc)
            pooled = last_token_pool(out.last_hidden_state, enc["attention_mask"])
            arr = pooled.float().cpu().numpy()
            for k, i in enumerate(idx_chunk):
                vectors[i] = arr[k]
            pos += len(idx_chunk)
            since_save += 1
            el = time.perf_counter() - t0
            print(f"teacher re-encode: {pos}/{len(todo)} elapsed={el:.0f}s "
                  f"rate={pos / max(el, 1e-9):.2f}/s", flush=True)
            if since_save >= SAVE_EVERY_BATCHES or pos >= len(todo):
                tmp = PARTIAL_NPZ.with_suffix(".tmp.npz")
                np.savez(tmp, vectors=vectors[todo[:pos]], texts=np.array(
                    [str(texts[i]) for i in todo[:pos]], dtype=object))
                tmp.replace(PARTIAL_NPZ)
                STATE_JSON.write_text(json.dumps({"encoded": pos, "total": len(todo)}) + "\n")
                since_save = 0

    assert np.isfinite(vectors).all(), "non-finite vectors in assembled npz"
    assert [text_hash(t) for t in texts] == [text_hash(str(x)) for x in texts]  # sanity (no-op)
    np.savez(OUT_NPZ, vectors=vectors, texts=np.array([str(t) for t in texts], dtype=object))
    PARTIAL_NPZ.unlink(missing_ok=True)
    STATE_JSON.write_text(json.dumps({"encoded": len(todo), "reused": n_reused,
                                      "total": len(texts), "final": True}) + "\n")

    # ── lockstep self-check (trainer will re-assert independently) ──────────
    back = np.load(OUT_NPZ, allow_pickle=True)
    assert [str(x) for x in back["texts"]] == texts, "final npz lockstep violated"
    assert back["vectors"].shape == (len(texts), r6_vecs.shape[1])

    proof = {
        "kind": "embed-r7-teacher-reuse-proof",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "teacher_id": TEACHER_ID,
        "teacher_unchanged_vs_round6": True,
        "source_npz": str(R6_NPZ),
        "source_npz_sha256": r6_sha,
        "source_texts": len(r6_texts),
        "r7_corpus_fp": EXPECTED_FP,
        "r7_texts": len(texts),
        "reused_by_raw_string_equality": n_reused,
        "recomputed_llm_texts": len(todo),
        "recomputed_non_llm_rows": 0,
        "r6_texts_absent_from_r7": len(dropped_r6),
        "absent_cross_check": "every absent r6 text asserted to be a translated-llm-r4 row "
                              "of the sealed r5 corpus (dropped llm dst texts; report says 612)",
        "absent_non_llm_class": dropped_non_llm,
        "recompute_recipe": "identical to precompute_teacher_r5.py (template, max_length 256, "
                            "batch 32, bf16 CPU, last-token pooling); frozen teacher → "
                            "deterministic per-text vectors",
        "out_npz": str(OUT_NPZ),
        "out_shape": list(back["vectors"].shape),
    }
    PROOF_JSON.write_text(json.dumps(proof, indent=1) + "\n")
    with open(RUNLOG, "a", encoding="utf-8") as fh:
        fh.write(f"- teacher reuse PROVEN: reused={n_reused} (raw-string identity vs r6 npz "
                 f"{r6_sha[:12]}…), recomputed={len(todo)} (all translated-llm-r4), "
                 f"r6-absent={len(dropped_r6)}; npz lockstep OK "
                 f"({back['vectors'].shape}); proof -> runs/embed-r7/reuse_proof.json\n")
    print(json.dumps({k: proof[k] for k in (
        "reused_by_raw_string_equality", "recomputed_llm_texts",
        "r6_texts_absent_from_r7")}, indent=1), flush=True)
    print(f"teacher vectors: {back['vectors'].shape} -> {OUT_NPZ}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
