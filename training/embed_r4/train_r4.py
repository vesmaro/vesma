"""Round-4 F-leg KD trainer (ratified variant F: 30M student).

LINEAGE: byte-for-byte adaptation of training/probe/probe_train.py
(capacity-probe-r4, addendum-6 §A.4) — the probe's leg (a) 30M control IS
the ratified F student config (ADR 0004): rubert-tiny2 312-hidden init,
projection 312->384, MRL dims [64,128,256,384] uniform weights, KD to a
frozen Qwen3-Embedding-0.6B teacher via PRECOMPUTED 1024-d vectors
(lockstep npz), mean-pool + L2 geometry, seed 42, CPU.

Differences from the probe run (protocol, not code):
  * corpus = the SEALED round-4 corpus (25496 texts; train 24221 / val
    1275, stratified 95/5, rng42; fingerprint 7a3bf00b22951df5…8b645);
  * epoch budget 6 (the agreed compensation zone: probe stopped at 3
    epochs on a 38k corpus with val still improving; this corpus is
    1.5x smaller — a checkpoint is written EVERY epoch and the
    checkpoint is selected on the val slice ONLY: max S1m-val
    recall@5, tie-break higher nDCG@10, then lower epoch);
  * the sealed judged corpus c2ce056d is NOT read anywhere in this
    module — selection uses the frozen teacher-top5 val protocol only.

The branch (b) 45M identity-init machinery is retained verbatim from the
probe so the parallel capacity-probe lane can reuse the same proven
engine; the round-4 F-leg runs with --branch a-30m.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import torch

TRAIN_DIR = Path(__file__).resolve().parents[1]  # vesma/training
if str(TRAIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(TRAIN_DIR.parent))

from training.distill import (  # noqa: E402
    format_teacher_input,
    load_encoder,
    l2_normalise,
    mean_pool,
    parse_mrl_dims,
    parse_mrl_weights,
    set_determinism,
)
from training.distill import mrl_kd_loss  # noqa: E402

MAX_LENGTH = 256
EMBED_DIM = 384


# ── §A.1 asserts ──────────────────────────────────────────────────────────────


def assert_fresh_sanity(student: Any, blocks: Any, tokenizer: Any, device: str) -> dict:
    """Assert: embedding of 'test' must be finite, unit-norm, non-degenerate.

    A fresh (or identity-widened) init must produce a usable vector at
    start — «старт с нуля, не с минуса». NaN/degenerate → loud exit.
    """
    import torch

    student.eval()
    enc = tokenizer(["test"], padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="pt")
    enc = {k: v.to(device) for k, v in enc.items()}
    with torch.no_grad():
        out = student(**enc)
        hidden = out.last_hidden_state
        if blocks is not None:
            hidden = run_grown_blocks(blocks, hidden, extended_mask(enc["attention_mask"], hidden.dtype))
        emb = l2_normalise(mean_pool(hidden, enc["attention_mask"]))[0]
    e = emb.float().tolist()
    finite = all(math.isfinite(x) for x in e)
    norm_ok = abs(sum(x * x for x in e) - 1.0) < 1e-3
    nonzero = any(abs(x) > 1e-8 for x in e)
    std = float(torch.std(emb.float()).item())
    if not (finite and norm_ok and nonzero and std > 1e-6):
        raise SystemExit(
            f"A.1 fresh-init sanity FAILED: finite={finite} norm_ok={norm_ok} "
            f"nonzero={nonzero} std={std}"
        )
    stats = {"finite": finite, "norm_ok": norm_ok, "std": round(std, 6)}
    print(f"A.1 fresh-init sanity OK: {stats}", flush=True)
    return stats


# ── §A.1 identity-growth for rubert-tiny2 (312-hidden) ───────────────────────


def extended_mask(attention_mask: Any, dtype: Any) -> Any:
    """Additive attention mask, the same formula BertEncoder applies."""
    import torch

    mask = attention_mask[:, None, None, :].to(dtype=dtype)
    mask = (1.0 - mask) * torch.finfo(dtype).min
    return mask.expand(mask.shape[0], 1, mask.shape[-1], mask.shape[-1])


def run_grown_blocks(blocks: Any, hidden: Any, ext_mask: Any) -> Any:
    """Residual pass through the ReZero-wrapped grown depth (α gates)."""
    for blk in blocks:
        hidden = blk(hidden_states=hidden, attention_mask=ext_mask)[0]
    return hidden


def grow_identity(student: Any, extra_layers: int) -> Any:
    """Append `extra_layers` ReZero-gated BertLayer blocks (§A.1).

    DEPTH growth (fixed choice for the probe): rubert-tiny2 is a 3-layer
    312-hidden BertModel — each new block is a fresh BertLayer with the
    attention-output and FFN-output dense kernels initialised to ZERO
    (zero-init, §A.1 item 1) and wrapped with a ReZero gate α=0. At step
    0 the widened student is FUNCTIONALLY IDENTICAL to rubert-tiny2
    (verified by the fresh-init assert + an explicit identity check
    against the ungrown backbone on a probe batch).
    """
    import torch
    from torch import nn
    from transformers.models.bert.modeling_bert import BertLayer

    blocks = nn.ModuleList()
    for _ in range(extra_layers):
        blk = BertLayer(student.config)
        nn.init.zeros_(blk.attention.output.dense.weight)
        nn.init.zeros_(blk.attention.output.dense.bias)
        nn.init.zeros_(blk.output.dense.weight)
        nn.init.zeros_(blk.output.dense.bias)
        blocks.append(_ReZeroWrap(blk, nn.Parameter(torch.zeros(1))))
    student._probe_blocks = blocks  # type: ignore[attr-defined]
    return student


class _ReZeroWrap(torch.nn.Module):
    """x + α · Block(x), α scalar init 0 (§A.1 ReZero option).

    BertLayer is post-LN internally, so no pre-block norm is added; the
    zero-init kernels additionally force the block output to exactly 0
    regardless of α — belt-and-suspenders per §A.1 item 1.
    """

    def __init__(self, block: Any, gate: Any) -> None:
        super().__init__()
        self.block = block
        self.alpha = gate

    def forward(self, hidden_states: Any, attention_mask: Any = None, **kw: Any) -> tuple:
        out = self.block(hidden_states, attention_mask=attention_mask, **kw)
        residual = hidden_states + self.alpha * out[0]
        return (residual,) + tuple(out[1:])


def identity_check(student: Any, blocks: Any, tokenizer: Any, device: str) -> float:
    """§A.1 verification: grown student output == bare rubert-tiny2 output.

    Runs the same probe batch through the ungrown backbone and through
    backbone+blocks; asserts max abs diff == 0 (α=0 + zero-init)."""
    import torch

    student.eval()
    texts = ["Контрольная фраза для identity-проверки grown-блоков.", "identity check text"]
    enc = tokenizer(texts, padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="pt")
    enc = {k: v.to(device) for k, v in enc.items()}
    with torch.no_grad():
        base = student(**enc).last_hidden_state
        grown = run_grown_blocks(blocks, base, extended_mask(enc["attention_mask"], base.dtype))
    diff = float((grown - base).abs().max())
    if diff != 0.0:
        raise SystemExit(f"A.1 identity check FAILED: max|Δ|={diff}")
    print(f"A.1 identity check OK: grown == backbone (max|Δ|=0)", flush=True)
    return diff


# ── teacher precompute + S1m-val ─────────────────────────────────────────────


def precompute_teacher(
    teacher_id: str,
    instruct_template: str,
    texts: list[str],
    out_npz: Path,
    *,
    device: str = "cpu",
    threads: int = 8,
    batch_size: int = 32,
    max_length: int = MAX_LENGTH,
) -> Path:
    """Encode ALL texts with the frozen teacher once; store fp32 vectors.

    Round-3 teacher geometry: left padding + last-token pooling +
    instruct-prefix on the query side (distill.format_teacher_input).
    fp32 targets (round-3 ran the teacher in fp32 on CPU).
    """
    import torch
    from transformers import AutoModel, AutoTokenizer

    # Interop=1 is a PROBE-CRITICAL setting: the torch default (16 interop
    # pools) oversubscribes the intra-op threads inside the teacher forward
    # and the batch takes ~50x longer (stall reproduced 2026-10-07: batch-64
    # L=58 8s with interop=1 vs 50+s without). Sequential interop is exactly
    # what a single-stream precompute loop wants; intra-op threads carry it.
    torch.set_num_interop_threads(1)
    torch.set_num_threads(threads)
    from training.distill import detect_teacher_pooling, last_token_pool

    tok = AutoTokenizer.from_pretrained(teacher_id)
    # bf16 targets: fp32 (125 ms/seq) vs bf16 (117 ms/seq) differ <7% on this
    # CPU and bf16 halves the RSS (3.1 GB vs 5.7 GB — the box runs at ~27/30
    # GB). Round-3 parity note: round-3 ran the LIVE teacher in fp32; the
    # slice-geometry equivalence is asserted by the branch lockstep (both
    # branches consume THESE vectors), not by dtype.
    model = AutoModel.from_pretrained(teacher_id, dtype=torch.bfloat16).to(device).eval()
    if detect_teacher_pooling(model, teacher_id) == "last_token":
        tok.padding_side = "left"
    vecs: list[np.ndarray] = []
    t_start = time.perf_counter()
    for start in range(0, len(texts), batch_size):
        chunk = [format_teacher_input(t, instruct_template) for t in texts[start : start + batch_size]]
        enc = tok(chunk, padding=True, truncation=True, max_length=max_length, return_tensors="pt")
        enc = {k: v.to(device) for k, v in enc.items()}
        with torch.no_grad():
            out = model(**enc)
        pooled = last_token_pool(out.last_hidden_state, enc["attention_mask"])
        vecs.append(pooled.float().cpu().numpy())
        done = start + len(chunk)
        if (start // batch_size) % 10 == 0:
            el = time.perf_counter() - t_start
            eta = el / done * (len(texts) - done)
            print(
                f"teacher encode: {done}/{len(texts)} elapsed={el:.0f}s eta={eta / 60:.0f}min",
                flush=True,
            )
    arr = np.concatenate(vecs, axis=0).astype(np.float32)
    np.savez(out_npz, vectors=arr, texts=np.array([str(t) for t in texts], dtype=object))
    el = time.perf_counter() - t_start
    print(f"teacher vectors: {arr.shape} -> {out_npz} in {el:.0f}s", flush=True)
    return out_npz


def s1m_val_metrics(embeddings: np.ndarray, targets: np.ndarray, relevant: list[set[int]]) -> dict[str, float]:
    """S1m formulas (BASELINE.md §10) on the val slice.

    Same metric shapes as benchmarks/stands/s1_quality/model_contour.py:
    precision/recall@k per query, MRR on the first relevant hit, nDCG@k
    with binary gains and the min(|rel|, k) ideal DCG.
    """
    e = embeddings / np.maximum(np.linalg.norm(embeddings, axis=1, keepdims=True), 1e-9)
    t = targets / np.maximum(np.linalg.norm(targets, axis=1, keepdims=True), 1e-9)
    n = e.shape[0]
    sims = e @ t.T
    np.fill_diagonal(sims, -np.inf)
    order = np.argsort(-sims, axis=1)[:, :10]

    def dcg(gains: list[float]) -> float:
        return sum(g / math.log2(i + 2) for i, g in enumerate(gains))

    r5: list[float] = []
    r10: list[float] = []
    p5: list[float] = []
    mrrs: list[float] = []
    nd5: list[float] = []
    nd10: list[float] = []
    for i in range(n):
        rel = relevant[i]
        ranked = [int(j) for j in order[i] if sims[i, int(j)] > -np.inf]
        top5, top10 = ranked[:5], ranked[:10]
        h5 = len(set(top5) & rel)
        h10 = len(set(top10) & rel)
        r5.append(h5 / len(rel) if rel else 0.0)
        r10.append(h10 / len(rel) if rel else 0.0)
        p5.append(h5 / 5)
        first = next((k + 1 for k, j in enumerate(ranked) if j in rel), 0)
        mrrs.append(1.0 / first if first else 0.0)
        d5 = dcg([1.0] * min(len(rel), 5))
        nd5.append(dcg([1.0 if j in rel else 0.0 for j in top5]) / d5 if d5 else 0.0)
        d10 = dcg([1.0] * min(len(rel), 10))
        nd10.append(dcg([1.0 if j in rel else 0.0 for j in top10]) / d10 if d10 else 0.0)

    return {
        "n_queries": n,
        "recall_at_5": round(sum(r5) / max(1, n), 6),
        "recall_at_10": round(sum(r10) / max(1, n), 6),
        "precision_at_5": round(sum(p5) / max(1, n), 6),
        "mrr": round(sum(mrrs) / max(1, n), 6),
        "ndcg_at_5": round(sum(nd5) / max(1, n), 6),
        "ndcg_at_10": round(sum(nd10) / max(1, n), 6),
    }


# ── training loop ─────────────────────────────────────────────────────────────


def embed_student(
    student: Any,
    blocks: Any,
    tokenizer: Any,
    texts: list[str],
    device: str,
    *,
    requires_grad: bool,
    projector: Any = None,
    batch_size: int = 32,
) -> np.ndarray:
    """Pooled (and projected) student embeddings for a text list."""
    import torch

    pooled_all: list[np.ndarray] = []
    for start in range(0, len(texts), batch_size):
        chunk = texts[start : start + batch_size]
        enc = tokenizer(chunk, padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="pt")
        enc = {k: v.to(device) for k, v in enc.items()}
        ctx = torch.enable_grad() if requires_grad else torch.no_grad()
        with ctx:
            hidden = student(**enc).last_hidden_state
            if blocks is not None:
                hidden = run_grown_blocks(blocks, hidden, extended_mask(enc["attention_mask"], hidden.dtype))
            pooled = mean_pool(hidden, enc["attention_mask"])
            if projector is not None:
                pooled = projector(pooled)
        pooled_all.append(pooled.float().detach().cpu().numpy())
    return np.concatenate(pooled_all, axis=0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="round-4 F-leg trainer (probe leg a-30m engine)")
    ap.add_argument("--branch", required=True, choices=["a-30m", "b-45m"])
    ap.add_argument("--corpus-dir", required=True, type=Path)
    ap.add_argument("--teacher-vectors", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr-base", type=float, default=2e-5)
    ap.add_argument("--lr-new", type=float, default=6e-5, help="grown-blocks LR (×3 base, depth-growth §A.1)")
    ap.add_argument("--temperature", type=float, default=0.05)
    ap.add_argument("--extra-layers", type=int, default=2)
    ap.add_argument("--mrl-dims", default="64,128,256,384")
    ap.add_argument("--mrl-weights", default=None, help="default uniform (round-3 parity)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--teacher-id", default="Qwen/Qwen3-Embedding-0.6B")
    ap.add_argument("--student-init", default="cointegrated/rubert-tiny2")
    ap.add_argument("--precompute", action="store_true", help="only encode texts -> npz, then exit")
    ap.add_argument("--skip-freeze-epoch1", action="store_true", help="§A.1 freeze off (dev only)")
    args = ap.parse_args(argv)

    import torch

    torch.set_num_threads(args.threads)
    set_determinism(args.seed)
    device = "cpu"
    args.out_dir.mkdir(parents=True, exist_ok=True)

    mrl_dims = parse_mrl_dims(args.mrl_dims, full_dim=EMBED_DIM)
    mrl_weights = parse_mrl_weights(args.mrl_weights, len(mrl_dims))

    # corpus in lockstep with the npz: train texts first, then val texts
    texts_all: list[str] = []
    n_val_texts = 0
    for path in (args.corpus_dir / "train.jsonl", args.corpus_dir / "val.jsonl"):
        cnt = 0
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                texts_all.append(json.loads(line)["text"])
                cnt += 1
        if path.name == "val.jsonl":
            n_val_texts = cnt
    val_slice_start = len(texts_all) - n_val_texts

    if args.precompute:
        precompute_teacher(
            args.teacher_id,
            "Given a memory note, retrieve similar notes",
            texts_all,
            args.teacher_vectors,
            device=device,
            threads=args.threads,
            batch_size=args.batch_size,
        )
        print("precompute done.")
        return 0

    npz = np.load(args.teacher_vectors, allow_pickle=True)
    npz_texts = [str(x) for x in npz["texts"]]
    if npz_texts != texts_all:
        raise SystemExit("teacher-vectors npz texts != corpus files (lockstep violated)")
    vectors = npz["vectors"]  # (N, 1024) fp32, one geometry for both branches
    print(f"corpus lockstep OK: {len(texts_all)} texts, vectors {vectors.shape}", flush=True)

    # full val slice = S1m-val surface; relevance = teacher-top5 (frozen)
    val_indices = list(range(val_slice_start, len(texts_all)))
    val_texts = [texts_all[i] for i in val_indices]
    val_vecs = vectors[val_indices]
    v = val_vecs / np.maximum(np.linalg.norm(val_vecs, axis=1, keepdims=True), 1e-9)
    sims = v @ v.T
    np.fill_diagonal(sims, -np.inf)
    order_n = np.argsort(-sims, axis=1)[:, :5]
    relevant = [set(int(j) for j in row) for row in order_n]
    print(
        f"S1m-val: {len(val_indices)} queries (5% slice), relevance=frozen teacher-top5",
        flush=True,
    )

    # student init — projector BEFORE growth: identical RNG draw in both branches
    tokenizer_s, student = load_encoder(args.student_init, MAX_LENGTH, device=device)
    probe = student(torch.zeros((1, MAX_LENGTH), dtype=torch.long))
    student_dim = probe.last_hidden_state.shape[-1]
    projector: Any = None
    if student_dim != EMBED_DIM:
        projector = torch.nn.Linear(student_dim, EMBED_DIM, bias=False).to(device)
        print(f"projection head: {student_dim} -> {EMBED_DIM} (random init, same seed both branches)")
    blocks: Any = None
    if args.branch == "b-45m":
        student = grow_identity(student, args.extra_layers)
        blocks = student._probe_blocks
        print(f"branch (b): +{args.extra_layers} ReZero blocks (zero-init, α=0) — DEPTH growth", flush=True)
        identity_check(student, blocks, tokenizer_s, device)
    assert_fresh_sanity(student, blocks, tokenizer_s, device)

    base_params = [q for n_, q in student.named_parameters() if "_probe_blocks" not in n_]
    new_params = [q for n_, q in student.named_parameters() if "_probe_blocks" in n_]
    assert (args.branch == "b-45m") == bool(new_params), "branch/params mismatch"
    freeze_ep1 = args.branch == "b-45m" and not args.skip_freeze_epoch1
    if freeze_ep1:
        for q in base_params:
            q.requires_grad_(False)
        print("A.1 freeze-epoch1: base frozen, trainable = new blocks only", flush=True)
    groups: list[dict[str, Any]] = []
    if new_params:
        groups.append({"params": new_params, "lr": args.lr_new})
    groups.append({"params": base_params, "lr": args.lr_base})
    if projector is not None:
        groups.append({"params": list(projector.parameters()), "lr": args.lr_base})
    optimizer = torch.optim.AdamW(groups)
    metrics_path = args.out_dir / "metrics.jsonl"
    order_rng = random.Random(args.seed)

    for epoch in range(1, args.epochs + 1):
        if (args.out_dir / "STOP").exists():
            print(f"stop-flag detected before epoch {epoch} — exiting cleanly", flush=True)
            break
        if freeze_ep1 and epoch == 2:
            for q in base_params:
                q.requires_grad_(True)
            print("A.1: epoch 2 — base unfrozen (full unfold)", flush=True)

        epoch_t0 = time.perf_counter()
        train_idx = list(range(0, val_slice_start))
        order_rng.shuffle(train_idx)
        student.train()
        total_loss, n_batches = 0.0, 0
        for start in range(0, len(train_idx), args.batch_size):
            chunk_idx = train_idx[start : start + args.batch_size]
            chunk = [texts_all[i] for i in chunk_idx]
            enc = tokenizer_s(chunk, padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="pt")
            enc = {k: v_.to(device) for k, v_ in enc.items()}
            hidden = student(**enc).last_hidden_state
            if blocks is not None:
                hidden = run_grown_blocks(blocks, hidden, extended_mask(enc["attention_mask"], hidden.dtype))
            student_pooled = mean_pool(hidden, enc["attention_mask"])
            teacher_target = torch.tensor(vectors[chunk_idx][..., :EMBED_DIM], dtype=torch.float32, device=device)
            loss = mrl_kd_loss(
                student_pooled, teacher_target, mrl_dims, args.temperature, projector=projector, weights=mrl_weights
            )
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()
            lv = float(loss.detach())
            if not math.isfinite(lv):
                print(f"error: non-finite loss epoch {epoch} batch {n_batches}", file=sys.stderr)
                return 4
            total_loss += lv
            n_batches += 1

        # S1m-val: embed the full val slice with this epoch's weights
        student.eval()
        val_emb = embed_student(student, blocks, tokenizer_s, val_texts, device, requires_grad=False, projector=projector)
        metrics = s1m_val_metrics(val_emb, val_vecs[..., :EMBED_DIM], relevant)
        e_n = val_emb / np.maximum(np.linalg.norm(val_emb, axis=1, keepdims=True), 1e-9)
        t_n = val_vecs[..., :EMBED_DIM] / np.maximum(np.linalg.norm(val_vecs[..., :EMBED_DIM], axis=1, keepdims=True), 1e-9)
        cos_teacher = float(np.mean(np.sum(e_n * t_n, axis=1)))
        ep_sec = time.perf_counter() - epoch_t0
        record = {
            "branch": args.branch,
            "epoch": epoch,
            "avg_kd_loss": round(total_loss / max(1, n_batches), 6),
            "epoch_wall_sec": round(ep_sec, 1),
            "s1m_val": metrics,
            "val_cosine_vs_teacher_384": round(cos_teacher, 6),
            "mrl_dims": mrl_dims,
            "mrl_weights": [round(w, 4) for w in mrl_weights],
        }
        with metrics_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        m = metrics
        print(
            f"epoch {epoch}: loss={record['avg_kd_loss']:.3f} R@5={m['recall_at_5']:.4f} "
            f"MRR={m['mrr']:.4f} nDCG@10={m['ndcg_at_10']:.4f} cosT={cos_teacher:.4f} "
            f"wall={ep_sec / 60:.1f}min",
            flush=True,
        )

        ckpt = args.out_dir / f"epoch{epoch}"
        ckpt.mkdir(parents=True, exist_ok=True)
        student.save_pretrained(ckpt)
        tokenizer_s.save_pretrained(ckpt)
        if projector is not None:
            torch.save(projector.state_dict(), ckpt / "projector.pt")
        (ckpt / "probe_meta.json").write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        if blocks is not None:
            torch.save(blocks.state_dict(), ckpt / "probe_blocks.pt")
            alphas = [round(float(b.alpha.detach()), 6) for b in blocks]
            print(f"checkpoint: {ckpt} (ReZero alphas={alphas})", flush=True)
        else:
            print(f"checkpoint: {ckpt}", flush=True)

    lines = metrics_path.read_text(encoding="utf-8").strip().splitlines()
    last = json.loads(lines[-1]) if lines else {}
    out_json = args.out_dir / "s1m_val.json"
    out_json.write_text(json.dumps(last, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"done: {out_json}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
