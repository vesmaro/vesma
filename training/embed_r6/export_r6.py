#!/usr/bin/env python3
"""Round-6 export — FORM FIXED: int8-dynamic (round-6 prereg §1).

fp32 graph is built ONLY as the quantize source + exactness reference
(round-5 recipe verbatim, incl. the Gemm→MatMul normalisation that the
dynamic-quant path requires). int8-dynamic = round-5 recipe verbatim
(weight-only, per-channel QInt8). Static int8 and fp16 are BANNED this
round; the round-5 form-choice fallback does NOT carry over — the final
artifact is int8-dynamic, full stop (the schedule round keeps ONE
variable; a form switch would be post-hoc shopping).

Both forms are MEASURED for the record (val per-slice R@5, gate-proxy
readout, per-text cos vs fp32-torch, size, context latency probe); the
official g6a number comes later from gate_latency.py under the engine
runtime. Judged corpus c2ce056d is never read here.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

TRAIN_DIR = Path(__file__).resolve().parents[1]
if str(TRAIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(TRAIN_DIR.parent))

from training.embed_r5 import eval_surfaces_r5 as es5  # noqa: E402
from training.embed_r5.export_r5 import (  # noqa: E402  (round-5 recipe, proven)
    CALIB_SAMPLES,
    Harness,
    normalize_gemm_for_dynamic_quant,
    stratified_calib_texts,
    torch_reference_embed,
    PROD_MONO_MEDIAN,
)

CORPUS_DIR = Path(
    "/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r5-corpus/data/embed-r5/corpus"
)
EXPECTED_FP = "e64fdbea65f0e6a4f2830a42ea6bd0d74a2974854d381427c4d5ba2ea4901c62"
MAX_SEQ = 256


def load_val_and_relevance(npz_path: Path, corpus_dir: Path):
    npz = np.load(npz_path, allow_pickle=True)
    texts = [str(x) for x in npz["texts"]]
    import hashlib

    digest = hashlib.sha256()
    val_texts: list[str] = []
    for name in ("train.jsonl", "val.jsonl"):
        p = corpus_dir / name
        digest.update(p.read_bytes())
        if name == "val.jsonl":
            with open(p, encoding="utf-8") as fh:
                val_texts = [json.loads(l)["text"] for l in fh if l.strip()]
    import os

    if digest.hexdigest() != EXPECTED_FP and not (
        os.environ.get("VESMA_R6_SMOKE") or os.environ.get("VESMA_R5_SMOKE")
    ):
        raise SystemExit(f"CORPUS FINGERPRINT MISMATCH: {digest.hexdigest()}")
    assert texts[len(texts) - len(val_texts):] == val_texts, "npz lockstep violated"
    val_vecs = npz["vectors"][len(texts) - len(val_texts):][..., :384]
    relevant = es5.frozen_val_relevance(val_vecs)
    return val_texts, val_vecs, relevant


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--epoch", required=True, type=int)
    ap.add_argument("--corpus-dir", type=Path, default=CORPUS_DIR)
    ap.add_argument("--skip-latency", action="store_true")
    args = ap.parse_args()

    from training.embed_r4.gate_latency import TEXTS as A2_TEXTS
    from training.export_onnx import export_onnx_fp32, export_tokenizer_json
    from transformers import AutoTokenizer
    from onnxruntime.quantization import QuantType, quantize_dynamic
    import onnx

    ckpt = args.run_dir / f"epoch{args.epoch}"
    if not ckpt.exists():
        raise SystemExit(f"checkpoint missing: {ckpt}")
    sel = json.loads((args.run_dir / "selection.json").read_text())
    if sel.get("chosen_epoch") != args.epoch:
        raise SystemExit(f"selection.json chose epoch {sel.get('chosen_epoch')}, not {args.epoch}")

    val_texts, val_vecs, relevant = load_val_and_relevance(
        args.run_dir / "teacher_vectors.npz", args.corpus_dir)
    surfaces = es5.build_surfaces()
    sl = es5.proxy_texts_and_slices(surfaces)

    out_root = args.run_dir / "export"
    print(f"[export] forms from {ckpt}", flush=True)

    # ── fp32 (quantize source + exactness reference; round-5 recipe verbatim) ──
    fp32_dir = out_root / "fp32"
    fp32_dir.mkdir(parents=True, exist_ok=True)
    if not (fp32_dir / "model.onnx").exists():
        tok = AutoTokenizer.from_pretrained(ckpt)
        export_onnx_fp32(ckpt, fp32_dir / "model.onnx", tok)
        export_tokenizer_json(tok, fp32_dir / "tokenizer.json")
    n_norm = normalize_gemm_for_dynamic_quant(fp32_dir / "model.onnx")
    if n_norm:
        print(f"[normalize] rewrote {n_norm} Gemm node(s) -> MatMul (bit-exact)", flush=True)

    # ── int8-dynamic (THE artifact form; round-5 recipe verbatim) ──
    dy_dir = out_root / "int8-dynamic"
    dy_dir.mkdir(parents=True, exist_ok=True)
    if not (dy_dir / "model.onnx").exists():
        consolidated = onnx.load(str(fp32_dir / "model.onnx"), load_external_data=True)
        onnx.save_model(consolidated, str(fp32_dir / "model.onnx"))
        quantize_dynamic(
            str(fp32_dir / "model.onnx"),
            str(dy_dir / "model.onnx"),
            weight_type=QuantType.QInt8,
            per_channel=True,
        )
    (dy_dir / "tokenizer.json").write_text((fp32_dir / "tokenizer.json").read_text(), encoding="utf-8")
    forms = {"fp32": fp32_dir, "int8-dynamic": dy_dir}
    print(f"[export] forms ready: {sorted(forms)}", flush=True)

    # ── measurement (both forms, for the record) ──
    print("[ref] fp32-torch reference embedding...", flush=True)
    proxy_texts = sl["texts"]
    all_texts = proxy_texts + val_texts
    ref = torch_reference_embed(ckpt, all_texts)

    results: dict[str, dict] = {}
    for name in ("fp32", "int8-dynamic"):
        t0 = time.time()
        h = Harness(forms[name])
        vec = h.embed_all(all_texts, progress=True)
        proxy_vec, val_vec = vec[: len(proxy_texts)], vec[len(proxy_texts):]
        ev = es5.evaluate_proxy(proxy_vec, surfaces)
        by_slice = es5.per_slice_val_metrics(val_vec, val_vecs, relevant, [64, 128, 256, 384])
        crit = min(m["recall_at_5"] for m in by_slice.values())
        per_text_cos = np.sum(vec * ref, axis=1)
        res = {
            "size_mb": h.size_mb,
            "weights_sha256": h.weights_sha256,
            "val_by_slice": by_slice,
            "min_over_slices_r5": crit,
            "val_r5_at_384": by_slice["384"]["recall_at_5"],
            "proxy": {
                "seed_dup_median": ev["proxy"]["by_class"]["seed-dup"]["M_cal_median"],
                "seed_dup_share": ev["proxy"]["by_class"]["seed-dup"]["S_cal_share_ge_085"],
                "llm_median": ev["proxy"]["by_class"]["llm"]["M_cal_median"],
                "llm_share": ev["proxy"]["by_class"]["llm"]["S_cal_share_ge_085"],
                "real_median": ev["proxy"]["by_class"]["real"]["M_cal_median"],
                "twin_median": ev["proxy"]["by_class"]["twin"]["M_cal_median"],
                "mono_control_median": ev["retrieval"]["mono"]["overall"]["median_best_distractor_cos"],
                "mono_delta_vs_prod": round(
                    ev["retrieval"]["mono"]["overall"]["median_best_distractor_cos"] - PROD_MONO_MEDIAN, 4),
                "cross_R@5": ev["retrieval"]["cross"]["overall"]["R@5"],
                "burned144_median": ev["burned144_median"],
            },
            "per_text_cos_vs_fp32torch": {
                "median": round(float(np.median(per_text_cos)), 4),
                "min": round(float(per_text_cos.min()), 4),
                "p10": round(float(np.percentile(per_text_cos, 10)), 4),
            },
        }
        if not args.skip_latency:
            res["latency_probe"] = h.latency_probe(A2_TEXTS)
        results[name] = res
        p = res["proxy"]
        print(
            f"[{name}] crit={res['min_over_slices_r5']:.4f} "
            f"sd={p['seed_dup_median']}/{p['seed_dup_share']} llm={p['llm_median']}/{p['llm_share']} "
            f"monoΔ={p['mono_delta_vs_prod']} burned={p['burned144_median']} R@5={p['cross_R@5']} "
            f"size={res['size_mb']}MB"
            + (f" mean={res['latency_probe']['mean_ms']}ms" if "latency_probe" in res else "")
            + f" ({time.time()-t0:.0f}s)",
            flush=True,
        )
        del h, vec
        import gc
        gc.collect()

    choice = {
        "kind": "embed-r6-export-record",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rule": "FORM FIXED by round-6 prereg §1: int8-dynamic (round-5 recipe); static int8 + fp16 banned; "
                "no form fallback — gate decides ADOPT/REJECT on this artifact",
        "prod_mono_median_reference": PROD_MONO_MEDIAN,
        "results": results,
        "chosen_form": "int8-dynamic",
        "why": "prereg-fixed artifact form (single-variable round); fp32 kept as quantize source + exactness reference only",
    }
    out = out_root / "measure_forms.json"
    out.write_text(json.dumps(choice, indent=1, ensure_ascii=False))
    (dy_dir / "CHOSEN.txt").write_text(
        "chosen export form: int8-dynamic\nwhy: round-6 prereg §1 — form FIXED before the run (round-5 recipe)\n",
        encoding="utf-8")
    print(f"[choice] int8-dynamic (prereg-fixed); -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
