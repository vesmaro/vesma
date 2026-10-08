"""Round-5 decisive trainer — per-slice KD + translation-contrastive (prereg §1).

LINEAGE: round-4 F-leg trainer (training/embed_r4/train_r4.py @ 507bfda)
with the prereg'd round-5 additions and NOTHING else changed:
  * KD stream UNCHANGED (round-4 loop): MSE-on-cosine KD to frozen
    precomputed teacher vectors, MRL dims [64,128,256,384], weights
    4,2,1,1 (normalised 0.5/0.25/0.125/0.125), temperature 0.05, batch 32;
  * translation-contrastive term (phase-0.5 pilot mechanics VERBATIM:
    symmetric in-batch InfoNCE, pair-adjacent batches of 16 pairs,
    partner = i^1, cosine logits / tau, tau=0.05, lambda=1.0, on the full
    384-d projected L2-normalised embedding) applied in JOINT batches
    (KD + lambda*InfoNCE) over the r5 pair pool;
  * PARTITION design (run_config.json, committed before the run): each
    epoch = stream (a) KD-only batches over non-pair train texts, then
    stream (b) joint pair-adjacent batches over the pool — every train
    text trains exactly once per epoch, pair texts get the pilot joint
    loss, per-slice KD coverage stays the full train slice;
  * epochs 16-20, early stop: min-over-slices val R@5 improves <0.001
    for 4 consecutive evals (prereg §1.4; the early-stop metric IS the
    selection criterion — stops only when the selected quantity stalls);
  * per-epoch: S1m-val on the full val slice (1261 queries, relevance =
    frozen teacher-top5 at 384), per-slice R@5 via truncation+renorm;
    + gate-proxy monitoring (prereg §4.3: seed-dup/llm/real/twin medians,
    mono control, burned144) — RECORDED, never used for selection;
  * checkpoint EVERY epoch: HF save + projector.pt + mrl_dims.json +
    optimizer.pt (resume discipline) + epoch record;
  * tracking: metrics.jsonl (JSON) + runs.db (sqlite), wandb banned.

Selection happens AFTER the run (select_checkpoint_r5.py), on the val
column only. The judged corpus c2ce056d is never read here.
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

from training.distill import (  # noqa: E402
    load_encoder,
    l2_normalise,
    mean_pool,
    mrl_kd_loss,
    parse_mrl_dims,
    parse_mrl_weights,
    set_determinism,
)
from training.embed_r5 import eval_surfaces_r5 as es5
from training.embed_r4.train_r4 import assert_fresh_sanity  # noqa: E402

MAX_LENGTH = 256
EMBED_DIM = 384
MRL_WEIGHTS_SPEC = "4,2,1,1"
KD_TEMPERATURE = 0.05
LAMBDA = 1.0
CT_TAU = 0.05
PAIRS_PER_BATCH = 16
EARLY_STOP_PATIENCE = 4
EARLY_STOP_MIN_IMPROVEMENT = 0.001

PILOT_DIR = Path("/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r5-corpus/training-pilot")
R4_CORPUS_TRAIN = Path(
    "/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r4-corpus/data/embed-r4/corpus/train.jsonl"
)
REDO_BURNED = Path(
    "/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/corpus-v43-redo/data/stage2/v43/train.jsonl"
)
EXPECTED_FP = "e64fdbea65f0e6a4f2830a42ea6bd0d74a2974854d381427c4d5ba2ea4901c62"


# ── tracking (JSON + sqlite; wandb banned) ───────────────────────────────────


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


# ── contrastive ───────────────────────────────────────────────────────────────


def contrastive_infonce(emb: torch.Tensor, tau: float) -> torch.Tensor:
    """Symmetric in-batch InfoNCE over a pair-adjacent batch (pilot verbatim).

    Batch layout [s1, d1, s2, d2, ...]: partner of i is i^1; cosine
    logits scaled by 1/tau; CE toward the partner. NOTE (mechanics
    parity): the diagonal is NOT masked — identical to the phase-0.5
    pilot code that won the grid; the loss therefore floors near
    log(2)≈0.69 (self competes with the partner), which is expected and
    not a defect.
    """
    logits = (emb @ emb.T) / tau
    labels = torch.arange(logits.shape[0], device=logits.device) ^ 1
    return torch.nn.functional.cross_entropy(logits, labels)


# ── main ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="round-5 decisive trainer")
    ap.add_argument("--corpus-dir", required=True, type=Path)
    ap.add_argument("--teacher-vectors", required=True, type=Path)
    ap.add_argument("--pairs", required=True, type=Path, help="pairs_r5.json from build_pair_pool")
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--epochs", type=int, default=20, help="hard cap (prereg: 16-20)")
    ap.add_argument("--start-epoch", type=int, default=1, help="resume: first epoch to TRAIN")
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--mrl-dims", default="64,128,256,384")
    ap.add_argument("--student-init", default="cointegrated/rubert-tiny2")
    ap.add_argument("--skip-proxy", action="store_true", help="skip the per-epoch gate-proxy monitor")
    args = ap.parse_args(argv)

    torch.set_num_interop_threads(1)
    torch.set_num_threads(args.threads)
    set_determinism(args.seed)
    device = "cpu"
    args.out_dir.mkdir(parents=True, exist_ok=True)
    runlog = RunLog(args.out_dir)
    runlog.event("launch", **vars(args))

    mrl_dims = parse_mrl_dims(args.mrl_dims, full_dim=EMBED_DIM)
    mrl_weights = parse_mrl_weights(MRL_WEIGHTS_SPEC, len(mrl_dims))

    # ── corpus in lockstep with the npz (train rows then val rows) ──
    digest = hashlib.sha256()
    texts_all: list[str] = []
    n_val_texts = 0
    for path in (args.corpus_dir / "train.jsonl", args.corpus_dir / "val.jsonl"):
        digest.update(path.read_bytes())
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
    import os

    if digest.hexdigest() != EXPECTED_FP and not os.environ.get("VESMA_R5_SMOKE"):
        raise SystemExit(f"CORPUS FINGERPRINT MISMATCH: {digest.hexdigest()}")
    if os.environ.get("VESMA_R5_SMOKE"):
        print("SMOKE MODE: corpus fingerprint assert bypassed", flush=True)
    val_slice_start = len(texts_all) - n_val_texts

    npz = np.load(args.teacher_vectors, allow_pickle=True)
    npz_texts = [str(x) for x in npz["texts"]]
    if npz_texts != texts_all:
        raise SystemExit("teacher-vectors npz texts != corpus files (lockstep violated)")
    vectors = npz["vectors"]
    print(f"corpus lockstep OK: {len(texts_all)} texts, vectors {vectors.shape}", flush=True)

    # ── frozen val relevance: teacher-top5 at 384 (round-4 protocol) ──
    val_indices = list(range(val_slice_start, len(texts_all)))
    val_texts = [texts_all[i] for i in val_indices]
    val_vecs = vectors[val_slice_start:]
    v384 = val_vecs[..., :EMBED_DIM]
    v384 = v384 / np.maximum(np.linalg.norm(v384, axis=1, keepdims=True), 1e-9)
    sims = v384 @ v384.T
    np.fill_diagonal(sims, -np.inf)
    order_n = np.argsort(-sims, axis=1)[:, :5]
    relevant = [set(int(j) for j in row) for row in order_n]
    print(f"S1m-val: {len(val_indices)} queries, relevance = frozen teacher-top5 (384)", flush=True)

    # ── pair pool → partition indices ──
    R4_SCRIPTS = str(R4_CORPUS_TRAIN.parents[3] / "scripts")
    if R4_SCRIPTS not in sys.path:
        sys.path.insert(0, R4_SCRIPTS)
    from gen_embed_r4_texts import text_hash  # r4 scripts (read-only import, pilot-proven)

    pool_doc = json.loads(args.pairs.read_text())
    pairs = pool_doc["pairs"]
    hash_to_idx: dict[str, int] = {}
    for i in range(val_slice_start):
        hash_to_idx[text_hash(texts_all[i])] = i
    pair_train_indices: list[tuple[int, int]] = []
    for p in pairs:
        i_s, i_d = hash_to_idx.get(p["sha_src"]), hash_to_idx.get(p["sha_dst"])
        if i_s is None or i_d is None:
            raise SystemExit(f"pair side not in train slice: {p['pair_key']}")
        pair_train_indices.append((i_s, i_d))
    pair_texts_used = {i for a, b in pair_train_indices for i in (a, b)}
    kd_only_indices = [i for i in range(val_slice_start) if i not in pair_texts_used]
    print(
        f"pair pool: {len(pair_train_indices)} pairs ({len(pair_texts_used)} texts); "
        f"kd-only texts: {len(kd_only_indices)}; total {val_slice_start}",
        flush=True,
    )
    flat_pair_idx = [i for a, b in pair_train_indices for i in (a, b)]
    flat_targets = torch.tensor(
        vectors[flat_pair_idx][..., :EMBED_DIM], dtype=torch.float32
    )

    # ── per-epoch proxy surfaces (§4.3 monitoring; recipe = run_phase0 verbatim) ──
    proxy_setup: dict = {}
    if not args.skip_proxy:
        surfaces = es5.build_surfaces()
        slices = es5.proxy_texts_and_slices(surfaces)
        proxy_setup = {"surfaces": surfaces, "slices": slices}
        print(
            f"proxy surfaces: gate={slices['n_gate']} burned={slices['n_burned']} "
            f"pool={slices['n_en'] + slices['n_ru']} "
            f"(pool identity: {surfaces['pool']['identity']})", flush=True,
        )

    # ── student ──
    tokenizer_s, student = load_encoder(args.student_init, MAX_LENGTH, device=device)
    probe = student(torch.zeros((1, MAX_LENGTH), dtype=torch.long))
    student_dim = probe.last_hidden_state.shape[-1]
    assert student_dim == 312, f"unexpected student dim {student_dim}"
    projector = torch.nn.Linear(student_dim, EMBED_DIM, bias=False).to(device)
    print(f"projection head: {student_dim} -> {EMBED_DIM} (fresh, seed {args.seed})", flush=True)
    assert_fresh_sanity(student, None, tokenizer_s, device)

    params = list(student.parameters()) + list(projector.parameters())
    optimizer = torch.optim.AdamW(params, lr=args.lr)

    # resume: load the last completed checkpoint (epoch < start_epoch)
    last_done = args.start_epoch - 1
    if last_done > 0:
        from transformers import AutoModel

        ck = args.out_dir / f"epoch{last_done}"
        # load INTO the live modules (never rebind): the optimizer holds
        # references to the existing parameter tensors
        student.load_state_dict(AutoModel.from_pretrained(ck).state_dict())
        projector.load_state_dict(torch.load(ck / "projector.pt", map_location="cpu", weights_only=True))
        optimizer.load_state_dict(torch.load(ck / "optimizer.pt", map_location="cpu", weights_only=True))
        print(f"resume: epoch{last_done} state loaded (backbone+projector+optimizer)", flush=True)

    metrics_path = args.out_dir / "metrics.jsonl"
    prev_records: list[dict] = []
    if metrics_path.exists():
        prev_records = [json.loads(l) for l in metrics_path.read_text().splitlines() if l.strip()]
        prev_records = [r for r in prev_records if r["epoch"] <= last_done]

    best_criterion = None
    patience = 0
    if prev_records:
        best_criterion = max(r["selection_criterion"] for r in prev_records)
        n_since_best = len(prev_records) - max(
            i for i, r in enumerate(prev_records) if r["selection_criterion"] == best_criterion
        )
        patience = max(0, n_since_best - 1)
        print(f"resume: best min-over-slices R@5 so far {best_criterion}, patience {patience}/4", flush=True)

    order_rng = random.Random(args.seed)
    stop_reason = None
    epochs_trained = 0
    for epoch in range(max(1, args.start_epoch), args.epochs + 1):
        if (args.out_dir / "STOP").exists():
            stop_reason = "STOP flag"
            print("stop-flag detected — exiting cleanly", flush=True)
            break
        epoch_t0 = time.perf_counter()
        student.train()
        projector.train()
        ep_kd, ep_ct, n_batches = 0.0, 0.0, 0

        # stream (a): KD-only over non-pair texts (round-4 loop, unchanged)
        order_a = list(kd_only_indices)
        order_rng.shuffle(order_a)
        for start in range(0, len(order_a), args.batch_size):
            chunk_idx = order_a[start : start + args.batch_size]
            chunk = [texts_all[i] for i in chunk_idx]
            enc = tokenizer_s(chunk, padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="pt")
            enc = {k: v.to(device) for k, v in enc.items()}
            hidden = student(**enc).last_hidden_state
            student_pooled = mean_pool(hidden, enc["attention_mask"])
            teacher_target = torch.tensor(
                vectors[chunk_idx][..., :EMBED_DIM], dtype=torch.float32, device=device
            )
            loss = mrl_kd_loss(
                student_pooled, teacher_target, mrl_dims, KD_TEMPERATURE,
                projector=projector, weights=mrl_weights,
            )
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()
            lv = float(loss.detach())
            if not math.isfinite(lv):
                print(f"error: non-finite KD loss epoch {epoch} batch {n_batches}", file=sys.stderr)
                runlog.event("fatal", reason="non_finite_kd_loss", epoch=epoch, batch=n_batches)
                return 4
            ep_kd += lv
            n_batches += 1

        # stream (b): joint KD + contrastive, pair-adjacent (pilot mechanics)
        order_b = list(range(len(pair_train_indices)))
        order_rng.shuffle(order_b)
        for start in range(0, len(order_b), PAIRS_PER_BATCH):
            idx = order_b[start : start + PAIRS_PER_BATCH]
            flat_pos = [j for i in idx for j in (2 * i, 2 * i + 1)]
            chunk = [texts_all[flat_pair_idx[j]] for j in flat_pos]
            vec_chunk = flat_targets[flat_pos].to(device)
            enc = tokenizer_s(chunk, padding=True, truncation=True, max_length=MAX_LENGTH, return_tensors="pt")
            enc = {k: v.to(device) for k, v in enc.items()}
            hidden = student(**enc).last_hidden_state
            pooled = mean_pool(hidden, enc["attention_mask"])
            kd = mrl_kd_loss(
                pooled, vec_chunk, mrl_dims, KD_TEMPERATURE,
                projector=projector, weights=mrl_weights,
            )
            emb = l2_normalise(projector(pooled))
            ct = contrastive_infonce(emb, CT_TAU)
            loss = kd + LAMBDA * ct
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()
            lv = float(loss.detach())
            if not math.isfinite(lv):
                print(f"error: non-finite joint loss epoch {epoch} batch {n_batches}", file=sys.stderr)
                runlog.event("fatal", reason="non_finite_joint_loss", epoch=epoch, batch=n_batches)
                return 4
            ep_kd += float(kd.detach())
            ep_ct += float(ct.detach())
            n_batches += 1

        # ── eval: student embeds val (+ proxy surfaces) in one pass ──
        student.eval()
        projector.eval()

        def embed(texts: list[str]) -> np.ndarray:
            out = []
            with torch.no_grad():
                for s in range(0, len(texts), args.batch_size):
                    enc = tokenizer_s(
                        texts[s : s + args.batch_size], padding=True, truncation=True,
                        max_length=MAX_LENGTH, return_tensors="pt",
                    )
                    hs = student(**enc).last_hidden_state
                    emb = projector(mean_pool(hs, enc["attention_mask"]))
                    out.append(torch.nn.functional.normalize(emb, p=2, dim=-1).numpy())
            return np.vstack(out).astype(np.float64)

        val_emb = embed(val_texts)
        by_slice = es5.per_slice_val_metrics(val_emb, val_vecs[..., :EMBED_DIM], relevant, mrl_dims)
        criterion = min(m["recall_at_5"] for m in by_slice.values())
        v384n = v384  # already normalised teacher-384
        cos_teacher = float(np.mean(np.sum((val_emb * v384n), axis=1)))

        record = {
            "epoch": epoch,
            "avg_kd_loss": round((ep_kd + ep_ct) / max(1, n_batches), 6),
            "avg_kd_component": round(ep_kd / max(1, n_batches), 6),
            "avg_contrastive_component": round(ep_ct / max(1, n_batches), 6),
            "epoch_wall_sec": round(time.perf_counter() - epoch_t0, 1),
            "s1m_val_by_slice": by_slice,
            "selection_criterion": criterion,
            "val_cosine_vs_teacher_384": round(cos_teacher, 6),
            "mrl_dims": mrl_dims,
            "mrl_weights": [round(w, 4) for w in mrl_weights],
        }

        # gate-proxy monitor (§4.3) — same harness as phase-0.5
        if proxy_setup:
            slices = proxy_setup["slices"]
            pvec = embed(slices["texts"])
            ev = es5.evaluate_proxy(pvec, proxy_setup["surfaces"])
            record["proxy_monitor"] = {
                "seed_dup_median": ev["proxy"]["by_class"]["seed-dup"]["M_cal_median"],
                "llm_median": ev["proxy"]["by_class"]["llm"]["M_cal_median"],
                "real_median": ev["proxy"]["by_class"]["real"]["M_cal_median"],
                "twin_median": ev["proxy"]["by_class"]["twin"]["M_cal_median"],
                "mono_control_median": ev["retrieval"]["mono"]["overall"]["median_best_distractor_cos"],
                "cross_R@5": ev["retrieval"]["cross"]["overall"]["R@5"],
                "burned144_median": ev["burned144_median"],
            }

        with metrics_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        runlog.epoch(record)
        epochs_trained += 1

        # ── checkpoint (weights + tokenizer + projector + MRL + optimizer) ──
        ckpt = args.out_dir / f"epoch{epoch}"
        ckpt.mkdir(parents=True, exist_ok=True)
        student.save_pretrained(ckpt)
        tokenizer_s.save_pretrained(ckpt)
        torch.save(projector.state_dict(), ckpt / "projector.pt")
        torch.save(optimizer.state_dict(), ckpt / "optimizer.pt")
        (ckpt / "mrl_dims.json").write_text(
            json.dumps({"embed_dim": EMBED_DIM, "mrl_dims": mrl_dims,
                        "mrl_weights": mrl_weights}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (ckpt / "epoch_record.json").write_text(json.dumps(record, sort_keys=True) + "\n")

        mon = record.get("proxy_monitor", {})
        slice_txt = ", ".join("{}={:.4f}".format(d, by_slice[str(d)]["recall_at_5"]) for d in mrl_dims)
        mon_txt = (
            "sd={seed_dup_median} llm={llm_median} mono={mono_control_median} ".format(**mon)
            if mon else ""
        )
        print(
            f"epoch {epoch}: kd={record['avg_kd_component']:.3f} ct={record['avg_contrastive_component']:.3f} "
            f"R@5@384={by_slice['384']['recall_at_5']:.4f} min-slices={criterion:.4f} "
            f"({slice_txt}) " + mon_txt + f"wall={record['epoch_wall_sec'] / 60:.1f}min",
            flush=True,
        )

        # ── early stop: min-over-slices R@5 improves <0.001 for 4 consecutive evals ──
        if best_criterion is None or criterion >= best_criterion + EARLY_STOP_MIN_IMPROVEMENT:
            best_criterion = criterion
            patience = 0
        else:
            patience += 1
        record["best_so_far"] = best_criterion
        record["patience"] = f"{patience}/{EARLY_STOP_PATIENCE}"
        runlog.event("epoch_done", epoch=epoch, criterion=criterion, best=best_criterion, patience=patience)
        if patience >= EARLY_STOP_PATIENCE:
            stop_reason = f"early stop: no >= {EARLY_STOP_MIN_IMPROVEMENT} improvement for {EARLY_STOP_PATIENCE} evals"
            print(stop_reason, flush=True)
            break

    final = {
        "kind": "embed-r5-train-final",
        "epochs_completed_this_session": epochs_trained,
        "epochs_on_record": len(prev_records) + epochs_trained,
        "stop_reason": stop_reason or "epoch cap reached",
        "best_min_over_slices_r5": best_criterion,
    }
    (args.out_dir / "train_final.json").write_text(json.dumps(final, indent=1) + "\n")
    runlog.event("train_end", epochs_on_record=final["epochs_on_record"],
                 stop_reason=final["stop_reason"], best=final["best_min_over_slices_r5"])
    print("done.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
