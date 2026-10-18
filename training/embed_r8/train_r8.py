#!/usr/bin/env python3
"""Round-8 STAGE-2 student trainer — per-slice KD from the FT'd harrier
teacher; NO contrastive anywhere (r8 prereg §1: the cross-lingual geometry
comes from the teacher natively; one variable per round = the teacher
source).

Derived from train_r7.py (round-6 schedule machinery verbatim) with the
contrastive phase REMOVED:

  phase 1 — KD anchor: per-slice KD only (round-4 loop verbatim) over ALL
  train texts. Transition after a phase-1 eval: val R@5 @384 >= 0.50 OR
  epoch >= 8 (cap), whichever first.

  phase 2 — KD-replay: KD only over ALL train texts (identical loop to
  phase 1; the phase label carries the schedule state for selection).
  No mono guard — there is no contrastive to guard (r7 measured the guard
  trip under CT; without CT the r6/r7 observation is that KD-only epochs
  sink mono slowly and selection holds the line).

  early stop — UNCHANGED (round-5 §1.4): min-over-slices val R@5 improves
  <0.001 for 4 consecutive evals. Budget <= 20 epochs total.

Teacher vectors: npz from precompute_teacher_r8 (FT'd harrier, (26069,1024)
fp32); KD target = first 384 dims, temperature 0.05, MRL 64/128/256/384
weights 4,2,1,1 — r5/6/7 recipe VERBATIM.

Resume: reconstruct (phase) from the last epoch record in metrics.jsonl.
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

from training.distill import (  # noqa: E402
    load_encoder,
    mean_pool,
    mrl_kd_loss,
    parse_mrl_dims,
    parse_mrl_weights,
    set_determinism,
)
from training.embed_r5 import eval_surfaces_r5 as es5  # noqa: E402
from training.embed_r4.train_r4 import assert_fresh_sanity  # noqa: E402

MAX_LENGTH = 256
EMBED_DIM = 384
MRL_WEIGHTS_SPEC = "4,2,1,1"
KD_TEMPERATURE = 0.05
EARLY_STOP_PATIENCE = 4
EARLY_STOP_MIN_IMPROVEMENT = 0.001

PHASE1_R5384_TRANSITION = 0.50
PHASE1_EPOCH_CAP = 8

EXPECTED_FP = "ad3d438f89ce91f47d279b63f1e88157ce747cb3a128399db1df707e8e1a3b7e"


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


def kd_step(student, projector, tokenizer_s, texts_all, chunk_idx, vectors,
            mrl_dims, mrl_weights, device, optimizer) -> float:
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
    return float(loss.detach())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="round-8 stage-2 trainer (KD-only -> KD-replay; NO contrastive)")
    ap.add_argument("--corpus-dir", required=True, type=Path)
    ap.add_argument("--teacher-vectors", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--epochs", type=int, default=20, help="hard cap total budget (prereg: <=20)")
    ap.add_argument("--start-epoch", type=int, default=1, help="resume: first epoch to TRAIN")
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--mrl-dims", default="64,128,256,384")
    ap.add_argument("--student-init", default="cointegrated/rubert-tiny2")
    ap.add_argument("--skip-proxy", action="store_true", help="skip the per-epoch gate-proxy monitor")
    args = ap.parse_args(argv)

    import os

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
    SMOKE = bool(os.environ.get("VESMA_R8_SMOKE"))
    if digest.hexdigest() != EXPECTED_FP and not SMOKE:
        raise SystemExit(f"CORPUS FINGERPRINT MISMATCH: {digest.hexdigest()}")
    if SMOKE:
        print("SMOKE MODE: corpus fingerprint assert bypassed", flush=True)
    phase1_cap = int(os.environ.get("VESMA_R8_SMOKE_PHASE1_CAP", PHASE1_EPOCH_CAP)) if SMOKE else PHASE1_EPOCH_CAP
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

    # ── per-epoch proxy surfaces (selection monitors; recipe = run_phase0 verbatim) ──
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

    # ── schedule state: reconstruct from records (carried by metrics.jsonl) ──
    if prev_records:
        phase = prev_records[-1]["phase_next"]
    else:
        phase = 1

    best_criterion = None
    patience = 0
    if prev_records:
        best_criterion = max(r["selection_criterion"] for r in prev_records)
        n_since_best = len(prev_records) - max(
            i for i, r in enumerate(prev_records) if r["selection_criterion"] == best_criterion
        )
        patience = max(0, n_since_best - 1)
        print(f"resume: best min-over-slices R@5 so far {best_criterion}, patience {patience}/4", flush=True)
        print(f"resume schedule state: phase={phase}", flush=True)

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
        ep_kd, n_batches = 0.0, 0
        phase_at_start = phase

        # ── KD-only epoch: phase 1 AND phase 2 (KD-replay) use the SAME loop ──
        order_a = list(range(val_slice_start))
        order_rng.shuffle(order_a)
        for start in range(0, len(order_a), args.batch_size):
            lv = kd_step(student, projector, tokenizer_s, texts_all,
                         order_a[start : start + args.batch_size], vectors,
                         mrl_dims, mrl_weights, device, optimizer)
            if not math.isfinite(lv):
                print(f"error: non-finite KD loss epoch {epoch} batch {n_batches}", file=sys.stderr)
                runlog.event("fatal", reason="non_finite_kd_loss", epoch=epoch, batch=n_batches)
                return 4
            ep_kd += lv
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
        cos_teacher = float(np.mean(np.sum((val_emb * v384), axis=1)))

        record = {
            "epoch": epoch,
            "phase": phase_at_start,
            "avg_kd_loss": round(ep_kd / max(1, n_batches), 6),
            "epoch_wall_sec": round(time.perf_counter() - epoch_t0, 1),
            "s1m_val_by_slice": by_slice,
            "selection_criterion": criterion,
            "val_cosine_vs_teacher_384": round(cos_teacher, 6),
            "mrl_dims": mrl_dims,
            "mrl_weights": [round(w, 4) for w in mrl_weights],
        }

        # gate-proxy monitor (selection inputs — same harness per epoch)
        if proxy_setup:
            slices = proxy_setup["slices"]
            pvec = embed(slices["texts"])
            ev = es5.evaluate_proxy(pvec, proxy_setup["surfaces"])
            record["proxy_monitor"] = {
                "seed_dup_median": ev["proxy"]["by_class"]["seed-dup"]["M_cal_median"],
                "seed_dup_share_ge085": ev["proxy"]["by_class"]["seed-dup"]["S_cal_share_ge_085"],
                "llm_median": ev["proxy"]["by_class"]["llm"]["M_cal_median"],
                "llm_share_ge085": ev["proxy"]["by_class"]["llm"]["S_cal_share_ge_085"],
                "real_median": ev["proxy"]["by_class"]["real"]["M_cal_median"],
                "twin_median": ev["proxy"]["by_class"]["twin"]["M_cal_median"],
                "mono_control_median": ev["retrieval"]["mono"]["overall"]["median_best_distractor_cos"],
                "cross_R@5": ev["retrieval"]["cross"]["overall"]["R@5"],
                "burned144_median": ev["burned144_median"],
            }

        # ── schedule transition (AFTER the eval; no guard — no CT in r8) ──
        transition = None
        phase_next = phase
        if phase_at_start == 1:
            r5384 = by_slice["384"]["recall_at_5"]
            if r5384 >= PHASE1_R5384_TRANSITION:
                transition = {"to_phase": 2, "reason": f"val_R5@384 {r5384:.4f} >= {PHASE1_R5384_TRANSITION}"}
                phase_next = 2
            elif epoch >= phase1_cap:
                transition = {"to_phase": 2, "reason": f"phase-1 epoch cap {phase1_cap} reached"}
                phase_next = 2
        record["transition"] = transition
        record["phase_next"] = phase_next
        phase = phase_next

        with metrics_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        runlog.epoch(record)
        if transition:
            runlog.event("phase_transition", epoch=epoch, **transition)

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
        # rolling optimizer deletion (keep-latest; r7 precedent)
        prev_opt = args.out_dir / f"epoch{epoch - 1}" / "optimizer.pt"
        if epoch - 1 >= 1 and prev_opt.exists():
            prev_opt.unlink()
            runlog.event("rolling_opt_delete", epoch=epoch - 1)

        slice_txt = ", ".join("{}={:.4f}".format(d, by_slice[str(d)]["recall_at_5"]) for d in mrl_dims)
        mon = record.get("proxy_monitor", {})
        mon_txt = (
            "sd={seed_dup_median}/{seed_dup_share_ge085} llm={llm_median}/{llm_share_ge085} "
            "mono={mono_control_median} cross={cross_R@5} ".format(**mon)
            if mon else ""
        )
        print(
            f"epoch {epoch} [P{phase_at_start}]: kd={record['avg_kd_loss']:.3f} "
            f"R@5@384={by_slice['384']['recall_at_5']:.4f} "
            f"min-slices={criterion:.4f} ({slice_txt}) " + mon_txt
            + (f"TRANSITION→P2({transition['reason']}) " if transition else "")
            + f"wall={record['epoch_wall_sec'] / 60:.1f}min",
            flush=True,
        )

        # ── early stop: UNCHANGED round-5 rule (min-over-slices criterion) ──
        if best_criterion is None or criterion >= best_criterion + EARLY_STOP_MIN_IMPROVEMENT:
            best_criterion = criterion
            patience = 0
        else:
            patience += 1
        record["best_so_far"] = best_criterion
        record["patience"] = f"{patience}/{EARLY_STOP_PATIENCE}"
        runlog.event("epoch_done", epoch=epoch, criterion=criterion, best=best_criterion,
                     patience=patience, phase=phase_at_start)
        if patience >= EARLY_STOP_PATIENCE:
            stop_reason = f"early stop: no >= {EARLY_STOP_MIN_IMPROVEMENT} improvement for {EARLY_STOP_PATIENCE} evals"
            print(stop_reason, flush=True)
            break

    final = {
        "kind": "embed-r8-train-final",
        "epochs_completed_this_session": epochs_trained,
        "epochs_on_record": len(prev_records) + epochs_trained,
        "stop_reason": stop_reason or "epoch cap reached",
        "best_min_over_slices_r5": best_criterion,
        "final_phase": phase,
    }
    (args.out_dir / "train_final.json").write_text(json.dumps(final, indent=1) + "\n")
    runlog.event("train_end", epochs_on_record=final["epochs_on_record"],
                 stop_reason=final["stop_reason"], best=final["best_min_over_slices_r5"],
                 final_phase=phase)
    print("done.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
