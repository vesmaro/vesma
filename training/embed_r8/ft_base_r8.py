#!/usr/bin/env python3
"""Round-8 STAGE-1 — base fine-tune: harrier-oss-v1-0.6b on the sealed corpus
pairs (r8 prereg §1/§3; run_config.json stage1 — committed before the run).

Loss: in-batch multiple-negatives InfoNCE over the pair pool, batch layout
[s1,d1,s2,d2,...] partner=i^1 — train_r7.contrastive_infonce VERBATIM
(pilot-proven mechanics). Scale 20 == 1/tau, tau=0.05 (the corpus CT
operating point). All other hyperparameters fixed in run_config.stage1.

Eval EVERY epoch (and epoch 0 = base, before any FT step): the r5 proxy
harness (gate cb367577 sha-asserted + burned144 + pool 480) read out at
full 1024 AND truncated-to-384+renormalised — the 384 numbers predict the
stage-2 KD target space. The BASE's mono is NOT gate-bound (prereg §1);
trend is what matters. Selection rule lives in select_ft_r8.py / run_config
(FIXED BEFORE the run; epoch 0 is in the candidate pool by design).

Checkpointing: every epoch — model fp32 safetensors + tokenizer +
optimizer.pt (rolling: epoch N optimizer deleted when N+1 lands; model
weights of ALL epochs retained until selection). Resume: --start-epoch,
reloads backbone + optimizer, fast-forwards the LR schedule by global step.

The judged corpus c2ce056d is never read here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import torch

TRAIN_DIR = Path(__file__).resolve().parents[1]  # vesma/training
if str(TRAIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(TRAIN_DIR.parent))

from training.distill import last_token_pool, set_determinism  # noqa: E402
from training.embed_r5 import eval_surfaces_r5 as es5  # noqa: E402
from training.embed_r7.train_r7 import contrastive_infonce  # noqa: E402 (pilot-verbatim)

MODEL_ID = "microsoft/harrier-oss-v1-0.6b"
MODEL_REVISION = "f9b9dc8d367d443f2479d27aa5d8d2850c0774ee"
MAX_LENGTH = 256
CT_TAU = 0.05  # scale 20 = 1/tau (corpus CT operating point, r5 §2)
PAIRS_SHA = "52b82a84b0994b73c1ebb9c23fdcc491d7b934d948f0a15d4cd9a841171e9f55"
EXPECTED_FP = "ad3d438f89ce91f47d279b63f1e88157ce747cb3a128399db1df707e8e1a3b7e"
CORPUS_DIR = Path(
    "/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r5-corpus/data/embed-r7/corpus"
)
WARMUP_STEPS = 20
LR_FINAL_FRACTION = 0.10
EVAL_BATCH = 32


class RunLog:
    def __init__(self, out_dir: Path) -> None:
        self.db = sqlite3.connect(out_dir / "runs.db")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (ts TEXT, kind TEXT, payload TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS epoch_metrics (epoch INTEGER, payload TEXT)")
        self.db.commit()

    def event(self, kind: str, **payload) -> None:
        import time as _t

        self.db.execute(
            "INSERT INTO events VALUES (?, ?, ?)",
            (_t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()), kind, json.dumps(payload, default=str)),
        )
        self.db.commit()

    def epoch(self, record: dict) -> None:
        self.db.execute(
            "INSERT INTO epoch_metrics VALUES (?, ?)", (record["epoch"], json.dumps(record, default=str))
        )
        self.db.commit()


def lr_lambda_factory(total_steps: int):
    def lr_lambda(step: int) -> float:
        if step < WARMUP_STEPS:
            return (step + 1) / WARMUP_STEPS
        progress = (step - WARMUP_STEPS) / max(1, total_steps - WARMUP_STEPS)
        progress = min(1.0, max(0.0, progress))
        cos = 0.5 * (1.0 + math.cos(math.pi * progress))
        return LR_FINAL_FRACTION + (1.0 - LR_FINAL_FRACTION) * cos

    return lr_lambda


def assert_corpus_fp() -> None:
    digest = hashlib.sha256()
    for name in ("train.jsonl", "val.jsonl"):
        digest.update((CORPUS_DIR / name).read_bytes())
    if digest.hexdigest() != EXPECTED_FP:
        raise SystemExit(f"CORPUS FINGERPRINT MISMATCH: {digest.hexdigest()}")


def load_pairs(path: Path) -> list[dict]:
    if hashlib.sha256(path.read_bytes()).hexdigest() != PAIRS_SHA:
        raise SystemExit(f"PAIRS SHA MISMATCH: {path}")
    doc = json.loads(path.read_text())
    if doc.get("corpus_fingerprint") != EXPECTED_FP:
        raise SystemExit("pairs corpus_fingerprint mismatch")
    return doc["pairs"]


def encode(model, tok, texts: list[str], batch: int) -> np.ndarray:
    """fp32 encode: bare text, last-token pooling, L2 normalise (run_config
    stage1.encoding_recipe — the ONE recipe for eval AND teacher precompute)."""
    out = []
    with torch.no_grad():
        for s in range(0, len(texts), batch):
            enc = tok(
                texts[s : s + batch], padding=True, truncation=True,
                max_length=MAX_LENGTH, return_tensors="pt",
            )
            hs = model(**enc).last_hidden_state
            pooled = last_token_pool(hs, enc["attention_mask"])
            out.append(torch.nn.functional.normalize(pooled, p=2, dim=-1).float().numpy())
    return np.vstack(out).astype(np.float64)


def proxy_readout(model, tok, surfaces: dict, sl: dict) -> dict:
    vec = encode(model, tok, sl["texts"], EVAL_BATCH)
    ev = es5.evaluate_proxy(vec, surfaces)
    n = vec.shape[1]

    v384 = vec[..., :384]
    v384 = v384 / np.maximum(np.linalg.norm(v384, axis=1, keepdims=True), 1e-9)
    ev384 = es5.evaluate_proxy(v384, surfaces)

    def block(e: dict) -> dict:
        return {
            "seed_dup_median": e["proxy"]["by_class"]["seed-dup"]["M_cal_median"],
            "seed_dup_share_ge085": e["proxy"]["by_class"]["seed-dup"]["S_cal_share_ge_085"],
            "llm_median": e["proxy"]["by_class"]["llm"]["M_cal_median"],
            "llm_share_ge085": e["proxy"]["by_class"]["llm"]["S_cal_share_ge_085"],
            "real_median": e["proxy"]["by_class"]["real"]["M_cal_median"],
            "twin_median": e["proxy"]["by_class"]["twin"]["M_cal_median"],
            "mono_control_median": e["retrieval"]["mono"]["overall"]["median_best_distractor_cos"],
            "cross_R@5": e["retrieval"]["cross"]["overall"]["R@5"],
            "cross_R@5_en": e["retrieval"]["cross"]["by_query_lang"]["en"]["R@5"],
            "cross_R@5_ru": e["retrieval"]["cross"]["by_query_lang"]["ru"]["R@5"],
            "burned144_median": e["burned144_median"],
        }

    return {"dim": n, "full_1024": block(ev), "trunc_384": block(ev384)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="r8 stage-1 base FT (harrier, MN loss, per-epoch proxy eval)")
    ap.add_argument("--pairs", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--model-id", default=MODEL_ID)
    ap.add_argument("--revision", default=MODEL_REVISION)
    ap.add_argument("--epochs", type=int, default=4)
    ap.add_argument("--batch-pairs", type=int, default=16)
    ap.add_argument("--lr", type=float, default=1e-5)
    ap.add_argument("--weight-decay", type=float, default=0.01)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--threads", type=int, default=12)
    ap.add_argument("--start-epoch", type=int, default=1, help="resume: first epoch to TRAIN")
    ap.add_argument("--max-steps", type=int, default=0, help="SMOKE ONLY: cap optimizer steps per epoch (0 = off)")
    ap.add_argument("--skip-proxy", action="store_true")
    ap.add_argument("--skip-eval0", action="store_true", help="resume: epoch-0 record already on file")
    ap.add_argument("--grad-checkpoint", action="store_true",
                    help="recompute backbone activations in backward (memory cap lever; "
                         "loss semantics IDENTICAL — same batch layout/negatives; ~30-40%% "
                         "more compute per step; activated 2026-10-20 after the 14G-capped "
                         "smoke OOM: attention-score activations at batch 16x256 fp32 exceed "
                         "the cap without it")
    args = ap.parse_args(argv)

    import os

    torch.set_num_interop_threads(1)
    torch.set_num_threads(args.threads)
    set_determinism(args.seed)
    device = "cpu"
    args.out_dir.mkdir(parents=True, exist_ok=True)
    runlog = RunLog(args.out_dir)
    runlog.event("stage1_launch", **vars(args))

    assert_corpus_fp()
    pairs = load_pairs(args.pairs)
    print(f"corpus fp OK; pairs {len(pairs)}", flush=True)

    surfaces, sl = None, None
    if not args.skip_proxy:
        surfaces = es5.build_surfaces()
        sl = es5.proxy_texts_and_slices(surfaces)
        print(
            f"proxy surfaces: gate={sl['n_gate']} burned={sl['n_burned']} "
            f"pool={sl['n_en'] + sl['n_ru']} (pool identity: {surfaces['pool']['identity']})",
            flush=True,
        )

    from transformers import AutoModel, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(args.model_id, revision=args.revision)
    model = AutoModel.from_pretrained(args.model_id, revision=args.revision, dtype=torch.float32).to(device)
    if args.grad_checkpoint:
        model.config.use_cache = False
        model.gradient_checkpointing_enable(
            gradient_checkpointing_kwargs={"use_reentrant": False}
        )
        print("gradient checkpointing ENABLED (memory cap lever)", flush=True)
    model.eval()
    hidden = int(getattr(model.config, "hidden_size", 0))
    assert hidden == 1024, f"unexpected harrier hidden size {hidden}"

    metrics_path = args.out_dir / "metrics.jsonl"
    prev_records: list[dict] = []
    if metrics_path.exists():
        prev_records = [json.loads(l) for l in metrics_path.read_text().splitlines() if l.strip()]

    # ── epoch 0: base readout, the selection reference line ──
    if not prev_records and not args.skip_eval0:
        t0 = time.perf_counter()
        model.eval()
        readout = proxy_readout(model, tok, surfaces, sl) if surfaces else {}
        rec = {
            "epoch": 0,
            "kind": "base_readout",
            "model_id": args.model_id,
            "revision": args.revision,
            "proxy_readout": readout,
            "wall_sec": round(time.perf_counter() - t0, 1),
        }
        with metrics_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        runlog.epoch(rec)
        print(f"epoch 0 [base]: {json.dumps(readout.get('full_1024', {}), sort_keys=True)}", flush=True)
        print(f"epoch 0 [base] trunc384: {json.dumps(readout.get('trunc_384', {}), sort_keys=True)}", flush=True)
        prev_records = [rec]

    # ── flat interleaved pair texts: [s1, d1, s2, d2, ...] ──
    flat_texts = [t for p in pairs for t in (p["text_src"], p["text_dst"])]
    steps_per_epoch = math.ceil(len(pairs) / args.batch_pairs)
    total_steps = steps_per_epoch * args.epochs

    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(
        params, lr=args.lr, weight_decay=args.weight_decay, foreach=False
    )
    sched = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda_factory(total_steps))

    # resume: load the last completed checkpoint (epoch < start_epoch)
    last_done = args.start_epoch - 1
    if last_done > 0:
        ck = args.out_dir / f"epoch{last_done}"
        model.load_state_dict(
            AutoModel.from_pretrained(ck, dtype=torch.float32).state_dict()
        )
        sd = torch.load(ck / "optimizer.pt", map_location="cpu", weights_only=True)
        optimizer.load_state_dict(sd)
        for _ in range(last_done * steps_per_epoch):
            sched.step()
        print(f"resume: epoch{last_done} state loaded (backbone+optimizer), "
              f"lr fast-forwarded to step {last_done * steps_per_epoch}", flush=True)

    order_rng = random.Random(args.seed)
    epochs_trained = 0
    for epoch in range(max(1, args.start_epoch), args.epochs + 1):
        if (args.out_dir / "STOP").exists():
            print("stop-flag detected — exiting cleanly", flush=True)
            break
        epoch_t0 = time.perf_counter()
        model.train()
        order = list(range(len(pairs)))
        order_rng.shuffle(order)
        ep_loss, n_batches = 0.0, 0
        smoke_capped = False
        for start in range(0, len(order), args.batch_pairs):
            if args.max_steps and n_batches >= args.max_steps:
                smoke_capped = True
                break
            idx = order[start : start + args.batch_pairs]
            chunk = [t for i in idx for t in (pairs[i]["text_src"], pairs[i]["text_dst"])]
            enc = tok(chunk, padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="pt")
            hs = model(**enc).last_hidden_state
            pooled = last_token_pool(hs, enc["attention_mask"])
            emb = torch.nn.functional.normalize(pooled, p=2, dim=-1)
            loss = contrastive_infonce(emb, CT_TAU)
            if not math.isfinite(float(loss.detach())):
                print(f"error: non-finite FT loss epoch {epoch} batch {n_batches}", file=sys.stderr)
                runlog.event("fatal", reason="non_finite_ft_loss", epoch=epoch, batch=n_batches)
                return 4
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(params, 1.0)
            optimizer.step()
            sched.step()
            ep_loss += float(loss.detach())
            n_batches += 1

        # ── eval ──
        model.eval()
        record: dict = {
            "epoch": epoch,
            "kind": "ft_epoch",
            "avg_loss": round(ep_loss / max(1, n_batches), 6),
            "lr_last": sched.get_last_lr()[0],
            "epoch_wall_sec": round(time.perf_counter() - epoch_t0, 1),
        }
        if smoke_capped:
            record["smoke_max_steps"] = args.max_steps
        if surfaces:
            record["proxy_readout"] = proxy_readout(model, tok, surfaces, sl)

        with metrics_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        runlog.epoch(record)

        # ── checkpoint (fp32 model + tokenizer + optimizer) ──
        ckpt = args.out_dir / f"epoch{epoch}"
        ckpt.mkdir(parents=True, exist_ok=True)
        model.save_pretrained(ckpt)
        tok.save_pretrained(ckpt)
        torch.save(optimizer.state_dict(), ckpt / "optimizer.pt")
        # rolling optimizer deletion (keep-latest; r7 precedent): after THIS
        # checkpoint landed, the previous epoch's optimizer is unreachable by
        # the resume machinery (it loads only start_epoch-1)
        prev_opt = args.out_dir / f"epoch{epoch - 1}" / "optimizer.pt"
        if epoch - 1 >= 1 and prev_opt.exists():
            prev_opt.unlink()
            runlog.event("rolling_opt_delete", epoch=epoch - 1)

        full = record.get("proxy_readout", {}).get("full_1024", {})
        print(
            f"epoch {epoch} [FT]: loss={record['avg_loss']:.4f} lr={record['lr_last']:.2e} "
            + " ".join(f"{k}={v}" for k, v in full.items())
            + f" wall={record['epoch_wall_sec'] / 60:.1f}min",
            flush=True,
        )
        runlog.event("epoch_done", epoch=epoch, avg_loss=record["avg_loss"])
        epochs_trained += 1

    final = {
        "kind": "embed-r8-stage1-final",
        "epochs_completed_this_session": epochs_trained,
        "epochs_on_record": len(prev_records) + epochs_trained if prev_records else epochs_trained,
        "stop_reason": "epoch cap reached" if epochs_trained else "no epochs this session",
    }
    (args.out_dir / "train_final.json").write_text(json.dumps(final, indent=1) + "\n")
    runlog.event("stage1_end", epochs_on_record=final["epochs_on_record"])
    print("stage-1 training done.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
