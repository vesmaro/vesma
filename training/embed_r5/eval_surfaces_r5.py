#!/usr/bin/env python3
"""Round-5 shared evaluation surfaces (train-time monitor == gate recipe).

One construction of every read-only evaluation surface, shared by
train_r5.py (per-epoch monitoring, prereg §4.3) and export_r5.py
(export-form measurement + the single-shot gate pass):

  * gate set cb367577 (180 pairs): texts, canonical order, pair sides —
    sha-asserted on every load;
  * burned 144 pairs (corpus-v43-redo translated-dup/duplicate);
  * distractor pool 240/lang (build_pool verbatim = run_phase0 recipe,
    pool source = the r4 corpus train slice — the population the floors
    were measured on), identity-checkable against the phase-0 npz;
  * frozen val relevance (teacher-top5 at 384) + per-slice S1m metrics;
  * proxy/readout evaluation over one embedded vector matrix.

Measured numbers only — verdict logic lives in the gate runner.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np

R4_WT = Path("/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r4-corpus")
REDO_WT = Path("/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/corpus-v43-redo")
GATE_FILE = R4_WT / "data" / "embed-r4" / "r5_gate" / "gate_set.jsonl"
R4_CORPUS_TRAIN = R4_WT / "data" / "embed-r4" / "corpus" / "train.jsonl"
BURNED_FILE = REDO_WT / "data" / "stage2" / "v43" / "train.jsonl"
CHARACT_NPZ = Path("/tmp/r5-charact/r5_phase0_embeddings.npz")

EXPECTED_GATE_SHA = "cb36757769fb88f2806065f50348f5fd8383cfdb0c19c66e6335eac8ace01123"
RANK_K = 5

PILOT_DIR = Path(
    "/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r5-corpus/training-pilot"
)
if str(PILOT_DIR) not in sys.path:
    sys.path.insert(0, str(PILOT_DIR))  # audit_kd_teachers (build_pool/evaluate_space, pilot-proven)


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ── statistics (run_phase0 conventions) ──────────────────────────────────────


def proxy_stats(x: np.ndarray) -> dict:
    return {
        "n": int(x.size),
        "S_cal_share_ge_085": round(float((x >= 0.85).mean()), 4),
        "count_ge_085": int((x >= 0.85).sum()),
        "M_cal_median": round(float(np.median(x)), 4),
        "P10": round(float(np.percentile(x, 10)), 4),
        "mean": round(float(np.mean(x)), 4),
        "min": round(float(np.min(x)), 4),
        "max": round(float(np.max(x)), 4),
        "P90": round(float(np.percentile(x, 90)), 4),
    }


def subset_stats(x: np.ndarray, mask) -> dict:
    m = np.array(mask, dtype=bool)
    return proxy_stats(x[m]) if m.any() else {"n": 0}


def rank_stats(ranks: np.ndarray) -> dict:
    return {
        "n": int(ranks.size),
        "R@5": round(float((ranks <= RANK_K).mean()), 4),
        "R@1": round(float((ranks == 1).mean()), 4),
        "median_rank": float(np.median(ranks)),
        "mean_rank": round(float(np.mean(ranks)), 2),
        "max_rank": int(ranks.max()),
    }


# ── surfaces ─────────────────────────────────────────────────────────────────


def load_gate_set() -> dict:
    sha = sha256_file(GATE_FILE)
    if sha != EXPECTED_GATE_SHA:
        raise SystemExit(f"GATE SET SHA MISMATCH: {sha}")
    rows = [json.loads(l) for l in open(GATE_FILE, encoding="utf-8") if l.strip()]
    assert len(rows) == 180, f"expected 180 gate pairs, got {len(rows)}"
    pair_sides = [
        {"src": (sha256_text(r["text_src"]), r["lang_src"]),
         "dst": (sha256_text(r["text_dst"]), r["lang_dst"])}
        for r in rows
    ]
    uniq_texts: dict[str, str] = {}
    for r in rows:
        for side in ("src", "dst"):
            uniq_texts.setdefault(sha256_text(r["text_" + side]), r["text_" + side])
    gate_order = sorted(uniq_texts)
    return {
        "rows": rows,
        "pair_sides": pair_sides,
        "gate_order": gate_order,
        "gate_texts": [uniq_texts[s] for s in gate_order],
        "sha256": sha,
    }


def load_burned144() -> dict:
    rows = [json.loads(l) for l in open(BURNED_FILE, encoding="utf-8") if l.strip()]
    sel = [r for r in rows if r.get("stratum") == "translated-dup" and r.get("label") == "duplicate"]
    assert len(sel) == 144, f"expected 144 burned pairs, got {len(sel)}"
    texts: list[str] = []
    index: dict[str, int] = {}
    for r in sel:
        for k in ("record", "candidate"):
            body = str(r[k].get("body") or "").strip()
            if body and body not in index:
                index[body] = len(texts)
                texts.append(body)
    return {"sel": sel, "index": index, "texts": texts}


def load_pool(gate_rows: list[dict]) -> dict:
    """240/lang hard-negative pool — build_pool verbatim (run_phase0 recipe),
    source = the r4 corpus train slice (floor population)."""
    from audit_kd_teachers import build_pool  # phase-0.5 module (pilot-proven)

    r4_rows = [json.loads(l) for l in open(R4_CORPUS_TRAIN, encoding="utf-8") if l.strip()]
    pool = build_pool(gate_rows, r4_rows)
    texts = {"en": [r["text"] for r in pool["en"]], "ru": [r["text"] for r in pool["ru"]]}
    identity = "npz not checked"
    if CHARACT_NPZ.exists():
        npz = np.load(CHARACT_NPZ, allow_pickle=True)
        assert list(npz["pool_en_sha"]) == [r["_sha"] for r in pool["en"]], "en pool mismatch vs npz"
        assert list(npz["pool_ru_sha"]) == [r["_sha"] for r in pool["ru"]], "ru pool mismatch vs npz"
        identity = "identical to phase-0 characterization npz"
    return {"texts": texts, "identity": identity}


def burned_median(burned_sel: list[dict], burned_index: dict, burned_vec: np.ndarray) -> float:
    cos = [
        float(np.dot(burned_vec[burned_index[str(r["record"].get("body") or "").strip()]],
                     burned_vec[burned_index[str(r["candidate"].get("body") or "").strip()]]))
        for r in burned_sel
    ]
    return round(float(np.median(np.array(cos))), 4)


def build_surfaces() -> dict:
    """All proxy surfaces in one call (asserts every input fingerprint)."""
    gate = load_gate_set()
    burned = load_burned144()
    pool = load_pool(gate["rows"])
    return {"gate": gate, "burned": burned, "pool": pool}


def proxy_texts_and_slices(surfaces: dict) -> dict:
    """Flattened text list + block offsets for a single embed pass."""
    g, b, p = surfaces["gate"], surfaces["burned"], surfaces["pool"]
    texts = g["gate_texts"] + b["texts"] + p["texts"]["en"] + p["texts"]["ru"]
    return {
        "texts": texts,
        "n_gate": len(g["gate_texts"]),
        "n_burned": len(b["texts"]),
        "n_en": len(p["texts"]["en"]),
        "n_ru": len(p["texts"]["ru"]),
    }


def evaluate_proxy(vec: np.ndarray, surfaces: dict) -> dict:
    """Full proxy readout over one embedded matrix (gate+burned+pool layout)."""
    from audit_kd_teachers import evaluate_space

    g, b, p = surfaces["gate"], surfaces["burned"], surfaces["pool"]
    n_gate, n_burned = len(g["gate_texts"]), len(b["texts"])
    n_en = len(p["texts"]["en"])
    out = evaluate_space(
        vec, g["gate_order"], g["rows"], g["pair_sides"], p["texts"],
        {"en": vec[n_gate + n_burned : n_gate + n_burned + n_en],
         "ru": vec[n_gate + n_burned + n_en :]},
    )
    bmed = burned_median(b["sel"], b["index"], vec[n_gate : n_gate + n_burned])
    out["burned144_median"] = bmed
    return out


# ── S1m-val (frozen protocol) ────────────────────────────────────────────────


def frozen_val_relevance(val_vecs384: np.ndarray) -> list[set[int]]:
    v = val_vecs384 / np.maximum(np.linalg.norm(val_vecs384, axis=1, keepdims=True), 1e-9)
    sims = v @ v.T
    np.fill_diagonal(sims, -np.inf)
    return [set(int(j) for j in row) for row in np.argsort(-sims, axis=1)[:, :5]]


def s1m_slice_metrics(
    student: np.ndarray, teacher: np.ndarray, relevant: list[set[int]]
) -> dict:
    """S1m formulas on ONE (already normalised) slice."""
    n = student.shape[0]
    sims = student @ teacher.T
    np.fill_diagonal(sims, -np.inf)
    order = np.argsort(-sims, axis=1)[:, :10]

    def dcg(gains: list[float]) -> float:
        return sum(g / math.log2(i + 2) for i, g in enumerate(gains))

    r5, r10, mrrs, nd5, nd10 = [], [], [], [], []
    for i in range(n):
        rel = relevant[i]
        ranked = [int(j) for j in order[i] if sims[i, int(j)] > -np.inf]
        top5, top10 = ranked[:5], ranked[:10]
        h5 = len(set(top5) & rel)
        r5.append(h5 / len(rel) if rel else 0.0)
        r10.append(len(set(top10) & rel) / len(rel) if rel else 0.0)
        first = next((p + 1 for p, j in enumerate(ranked) if j in rel), 0)
        mrrs.append(1.0 / first if first else 0.0)
        d5 = dcg([1.0] * min(len(rel), 5))
        nd5.append(dcg([1.0 if j in rel else 0.0 for j in top5]) / d5 if d5 else 0.0)
        d10 = dcg([1.0] * min(len(rel), 10))
        nd10.append(dcg([1.0 if j in rel else 0.0 for j in top10]) / d10 if d10 else 0.0)
    return {
        "recall_at_5": round(float(np.mean(r5)), 6),
        "recall_at_10": round(float(np.mean(r10)), 6),
        "mrr": round(float(np.mean(mrrs)), 6),
        "ndcg_at_5": round(float(np.mean(nd5)), 6),
        "ndcg_at_10": round(float(np.mean(nd10)), 6),
    }


def per_slice_val_metrics(
    val_emb: np.ndarray, val_vecs: np.ndarray, relevant: list[set[int]], mrl_dims: list[int]
) -> dict:
    """Per-MRL-slice S1m-val: student truncated+renormalised vs teacher slice."""
    out: dict[str, dict] = {}
    for d in mrl_dims:
        sv = val_emb[..., :d]
        sv = sv / np.maximum(np.linalg.norm(sv, axis=1, keepdims=True), 1e-9)
        tv = val_vecs[..., :d]
        tv = tv / np.maximum(np.linalg.norm(tv, axis=1, keepdims=True), 1e-9)
        out[str(d)] = s1m_slice_metrics(sv, tv, relevant)
    return out
