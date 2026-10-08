#!/usr/bin/env python3
"""Round-5 export forms + measurement + choice (prereg §2 correction 2).

Export forms (all from the SAME selected checkpoint, static 1x256 graph,
mean-pool + projector + L2 baked, opset 15):

  fp32         — round-4 export path verbatim (exactness reference;
                 round-4 evidence: fp32-torch == fp32-ONNX exactly)
  fp16         — onnxconverter_common float16 (keep_io_types=True)
  int8-static  — CALIBRATED QDQ: per-channel weight quantization, MinMax
                 calibration over 512 deterministic stratified r5-train
                 texts (the round-4 failure was 200 val samples,
                 per-channel=False — corrected on both axes)
  int8-dynamic — weight-only dynamic (activations quantized at runtime)

Every form is MEASURED (prereg: «выбор по замеру»):
  * val per-slice R@5 (frozen relevance; min-over-slices);
  * gate-proxy readout: seed-dup/llm/real/twin, mono control, cross R@5,
    burned144 (NanoHarness recipe, batch=1, static padding);
  * per-text cosine vs the fp32-torch reference;
  * size + a context latency probe (plain ORT, intra=4, §A.2 texts —
    the OFFICIAL g6a number comes later from gate_latency.py on the
    chosen form, engine runtime, VESMARO_ORT_THREADS=4).

Choice (run_config.json, fixed before measurement): forms with probe
mean <= 45 ms pass size/latency candidacy; hard guards |Δmono vs prod
0.8982| <= 0.005 and val min-over-slices R@5 within 0.005 of fp32; among
survivors max composite seed_dup + llm + burned144; tie-break smaller
|Δmono|, then form priority fp16 > int8-dynamic > int8-static.

Form choice uses the gate-set texts as a QUALITY READOUT only (registered
calibration); the single-shot GATE pass runs once, afterwards, on the
chosen artifact. Judged corpus c2ce056d is never read here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import sys
import time
from pathlib import Path

import numpy as np

TRAIN_DIR = Path(__file__).resolve().parents[1]
if str(TRAIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(TRAIN_DIR.parent))

from training.embed_r5 import eval_surfaces_r5 as es5  # noqa: E402

CORPUS_DIR = Path(
    "/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r5-corpus/data/embed-r5/corpus"
)
EXPECTED_FP = "e64fdbea65f0e6a4f2830a42ea6bd0d74a2974854d381427c4d5ba2ea4901c62"
MAX_SEQ = 256
PROD_MONO_MEDIAN = 0.8982  # phase-0 characterization, production 3b752e06
CALIB_SAMPLES = 512
FORM_PRIORITY = {"fp16": 0, "int8-dynamic": 1, "int8-static-calibrated": 2, "fp32": 9}

sys.path.insert(0, str(TRAIN_DIR / "runs" / "embed-r5" / "pylibs"))


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def load_val_and_relevance(npz_path: Path):
    npz = np.load(npz_path, allow_pickle=True)
    texts = [str(x) for x in npz["texts"]]
    digest = hashlib.sha256()
    val_texts: list[str] = []
    for name in ("train.jsonl", "val.jsonl"):
        p = CORPUS_DIR / name
        digest.update(p.read_bytes())
        if name == "val.jsonl":
            with open(p, encoding="utf-8") as fh:
                val_texts = [json.loads(l)["text"] for l in fh if l.strip()]
    if digest.hexdigest() != EXPECTED_FP:
        raise SystemExit(f"CORPUS FINGERPRINT MISMATCH: {digest.hexdigest()}")
    assert texts[len(texts) - len(val_texts):] == val_texts, "npz lockstep violated"
    val_vecs = npz["vectors"][len(texts) - len(val_texts):][..., :384]
    relevant = es5.frozen_val_relevance(val_vecs)
    return val_texts, val_vecs, relevant


def stratified_calib_texts(n: int) -> list[str]:
    """Deterministic stratified calibration set from the r5 TRAIN slice.

    Stratification: group train rows by `source` family (family = source
    up to the trailing class), sort within group by text_hash, round-robin
    take 1 per group per cycle until n — every family represented, no RNG.
    """
    r4_scripts = str(Path(
        "/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r4-corpus/scripts"
    ))
    if r4_scripts not in sys.path:
        sys.path.insert(0, r4_scripts)
    from gen_embed_r4_texts import text_hash

    rows = [json.loads(l) for l in open(CORPUS_DIR / "train.jsonl", encoding="utf-8") if l.strip()]
    groups: dict[str, list[str]] = {}
    for r in rows:
        fam = r["source"].rsplit("-", 1)[0] if r["source"].count("-") > 1 else r["source"]
        groups.setdefault(fam, []).append(r["text"])
    for fam in groups:
        groups[fam].sort(key=text_hash)
    out: list[str] = []
    cycle = 0
    while len(out) < n:
        took = False
        for fam in sorted(groups):
            if cycle < len(groups[fam]):
                out.append(groups[fam][cycle])
                took = True
                if len(out) >= n:
                    break
        if not took:
            break
        cycle += 1
    return out[:n]


# ── embedders ────────────────────────────────────────────────────────────────


def torch_reference_embed(ckpt: Path, texts: list[str]) -> np.ndarray:
    import torch
    from transformers import AutoModel, AutoTokenizer

    torch.set_num_threads(8)
    model = AutoModel.from_pretrained(ckpt).eval()
    tok = AutoTokenizer.from_pretrained(ckpt)
    proj_sd = torch.load(ckpt / "projector.pt", map_location="cpu", weights_only=True)
    proj = torch.nn.Linear(proj_sd["weight"].shape[1], proj_sd["weight"].shape[0], bias=False)
    proj.load_state_dict(proj_sd)
    proj.eval()
    out = []
    with torch.no_grad():
        for s in range(0, len(texts), 32):
            enc = tok(texts[s : s + 32], padding=True, truncation=True, max_length=MAX_SEQ, return_tensors="pt")
            hs = model(**enc).last_hidden_state
            mask = enc["attention_mask"].unsqueeze(-1).to(hs.dtype)
            pooled = (hs * mask).sum(1) / torch.clamp(mask.sum(1), min=1e-9)
            emb = torch.nn.functional.normalize(proj(pooled), p=2, dim=-1)
            out.append(emb.numpy())
    return np.vstack(out).astype(np.float64)


class Harness:
    """NanoProvider recipe verbatim (run_phase0): Tokenizer.from_file,
    truncation+padding 256, ORT CPU intra=4/inter=1, batch=1, in-graph L2."""

    def __init__(self, form_dir: Path):
        from tokenizers import Tokenizer
        import onnxruntime as ort

        self.weights_sha256 = sha256_file(form_dir / "model.onnx")
        self.size_mb = round((form_dir / "model.onnx").stat().st_size / (1024 * 1024), 2)
        self._tokenizer = Tokenizer.from_file(str(form_dir / "tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=MAX_SEQ)
        self._tokenizer.enable_padding(length=MAX_SEQ)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4
        opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(form_dir / "model.onnx"), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._inputs = {i.name for i in self._session.get_inputs()}

    def embed_all(self, texts: list[str], progress: bool = False) -> np.ndarray:
        vecs = []
        t0 = time.perf_counter()
        for i, t in enumerate(texts):
            e = self._tokenizer.encode_batch([t])[0]
            ids = np.array([e.ids], dtype=np.int64)
            mask = np.array([e.attention_mask], dtype=np.int64)
            inputs = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._inputs:
                inputs["token_type_ids"] = np.zeros_like(ids)
            v = self._session.run(None, inputs)[0][0]
            n = float(np.linalg.norm(v))
            if abs(n - 1.0) > 1e-3:
                v = v / n
            vecs.append(v)
            if progress and i % 400 == 0:
                print(f"    embed {i}/{len(texts)} ({time.perf_counter()-t0:.0f}s)", flush=True)
        return np.vstack(vecs).astype(np.float64)

    def latency_probe(self, texts: list[str], warmup: int = 5, passes: int = 20) -> dict:
        for _ in range(warmup):
            for t in texts:
                self.embed_all([t])
        per_text = []
        for _ in range(passes):
            for t in texts:
                t0 = time.perf_counter()
                self.embed_all([t])
                per_text.append((time.perf_counter() - t0) * 1000.0)
        per_text.sort()
        return {
            "n": len(per_text),
            "mean_ms": round(statistics.fmean(per_text), 2),
            "p50_ms": round(per_text[len(per_text) // 2], 2),
            "p95_ms": round(per_text[int(len(per_text) * 0.95) - 1], 2),
            "note": "context probe (plain ORT intra=4); official g6a = gate_latency.py on the chosen form",
        }


# ── export forms ─────────────────────────────────────────────────────────────


def export_forms(ckpt: Path, out_root: Path) -> dict[str, Path]:
    from training.export_onnx import export_onnx_fp32, export_tokenizer_json
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(ckpt)
    forms: dict[str, Path] = {}

    fp32_dir = out_root / "fp32"
    fp32_dir.mkdir(parents=True, exist_ok=True)
    if not (fp32_dir / "model.onnx").exists():
        export_onnx_fp32(ckpt, fp32_dir / "model.onnx", tok)
    export_tokenizer_json(tok, fp32_dir / "tokenizer.json")
    forms["fp32"] = fp32_dir

    import onnx

    # fp16
    from onnxconverter_common import float16 as of16

    fp16_dir = out_root / "fp16"
    fp16_dir.mkdir(parents=True, exist_ok=True)
    if not (fp16_dir / "model.onnx").exists():
        m = onnx.load(str(fp32_dir / "model.onnx"), load_external_data=True)
        m16 = of16.convert_float_to_float16(m, keep_io_types=True)
        onnx.save(m16, str(fp16_dir / "model.onnx"))
        del m, m16
    (fp16_dir / "tokenizer.json").write_text((fp32_dir / "tokenizer.json").read_text(), encoding="utf-8")
    forms["fp16"] = fp16_dir

    # calibration reader (static 1x256, text-by-text — round-4 reader recipe)
    from onnxruntime.quantization import CalibrationDataReader, QuantFormat, QuantType, quantize_static, quantize_dynamic

    calib_texts = stratified_calib_texts(CALIB_SAMPLES)

    class TextCalibReader(CalibrationDataReader):
        def __init__(self, texts: list[str]):
            import numpy as _np

            self._inputs = []
            for t in texts:
                e = tok([t], padding="max_length", truncation=True, max_length=MAX_SEQ, return_tensors="np")
                batch = {
                    "input_ids": e["input_ids"].astype("int64"),
                    "attention_mask": e["attention_mask"].astype("int64"),
                }
                if "token_type_ids" in e:
                    batch["token_type_ids"] = e["token_type_ids"].astype("int64")
                self._inputs.append(batch)
            self._idx = 0

        def get_next(self):
            if self._idx >= len(self._inputs):
                return None
            out = self._inputs[self._idx]
            self._idx += 1
            return out

    # int8 static, calibrated (QDQ, per-channel weights)
    st_dir = out_root / "int8-static-calibrated"
    st_dir.mkdir(parents=True, exist_ok=True)
    if not (st_dir / "model.onnx").exists():
        consolidated = onnx.load(str(fp32_dir / "model.onnx"), load_external_data=True)
        onnx.save_model(consolidated, str(fp32_dir / "model.onnx"))
        quantize_static(
            str(fp32_dir / "model.onnx"),
            str(st_dir / "model.onnx"),
            calibration_data_reader=TextCalibReader(calib_texts),
            quant_format=QuantFormat.QDQ,
            per_channel=True,
            weight_type=QuantType.QInt8,
            activation_type=QuantType.QInt8,
        )
    (st_dir / "tokenizer.json").write_text((fp32_dir / "tokenizer.json").read_text(), encoding="utf-8")
    forms["int8-static-calibrated"] = st_dir

    # int8 dynamic (weight-only)
    dy_dir = out_root / "int8-dynamic"
    dy_dir.mkdir(parents=True, exist_ok=True)
    if not (dy_dir / "model.onnx").exists():
        quantize_dynamic(
            str(fp32_dir / "model.onnx"),
            str(dy_dir / "model.onnx"),
            weight_type=QuantType.QInt8,
            per_channel=True,
        )
    (dy_dir / "tokenizer.json").write_text((fp32_dir / "tokenizer.json").read_text(), encoding="utf-8")
    forms["int8-dynamic"] = dy_dir

    return forms


# ── measurement + choice ─────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--epoch", required=True, type=int)
    ap.add_argument("--skip-latency", action="store_true")
    args = ap.parse_args()

    from training.embed_r4.gate_latency import TEXTS as A2_TEXTS

    ckpt = args.run_dir / f"epoch{args.epoch}"
    if not ckpt.exists():
        raise SystemExit(f"checkpoint missing: {ckpt}")
    sel = json.loads((args.run_dir / "selection.json").read_text())
    if sel["chosen_epoch"] != args.epoch:
        raise SystemExit(f"selection.json chose epoch {sel['chosen_epoch']}, not {args.epoch}")
    val_texts, val_vecs, relevant = load_val_and_relevance(args.run_dir / "teacher_vectors.npz")
    surfaces = es5.build_surfaces()
    sl = es5.proxy_texts_and_slices(surfaces)

    print(f"[export] forms from {ckpt}", flush=True)
    forms = export_forms(ckpt, args.run_dir / "export")
    print(f"[export] forms ready: {sorted(forms)}", flush=True)

    print("[ref] fp32-torch reference embedding...", flush=True)
    proxy_texts = sl["texts"]
    all_texts = proxy_texts + val_texts
    ref = torch_reference_embed(ckpt, all_texts)
    ref_proxy, ref_val = ref[: len(proxy_texts)], ref[len(proxy_texts):]

    results: dict[str, dict] = {}
    for name in ("fp32", "fp16", "int8-static-calibrated", "int8-dynamic"):
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
                "mono_delta_vs_prod": round(ev["retrieval"]["mono"]["overall"]["median_best_distractor_cos"] - PROD_MONO_MEDIAN, 4),
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
            f"sd={p['seed_dup_median']} llm={p['llm_median']} monoΔ={p['mono_delta_vs_prod']} "
            f"burned={p['burned144_median']} R@5={p['cross_R@5']} size={res['size_mb']}MB"
            + (f" mean={res['latency_probe']['mean_ms']}ms" if "latency_probe" in res else "")
            + f" ({time.time()-t0:.0f}s)",
            flush=True,
        )
        del h, vec
        import gc
        gc.collect()

    # ── choice per the committed rule ──
    fp32 = results["fp32"]
    candidates: dict[str, dict] = {}
    for name, res in results.items():
        if name == "fp32":
            continue
        checks = {
            "latency_mean_le_45": (res.get("latency_probe", {}).get("mean_ms", 1e9) <= 45.0) if not args.skip_latency else None,
            "mono_delta_guard": abs(res["proxy"]["mono_delta_vs_prod"]) <= 0.005,
            "val_guard": (fp32["min_over_slices_r5"] - res["min_over_slices_r5"]) <= 0.005,
        }
        composite = round(res["proxy"]["seed_dup_median"] + res["proxy"]["llm_median"] + res["proxy"]["burned144_median"], 4)
        res["choice_checks"] = checks
        res["composite"] = composite
        if all(v is None or v for v in checks.values()):
            candidates[name] = {"composite": composite, "abs_mono_delta": abs(res["proxy"]["mono_delta_vs_prod"]),
                                "priority": FORM_PRIORITY[name]}
    if candidates:
        ranked = sorted(candidates.items(), key=lambda kv: (-kv[1]["composite"], kv[1]["abs_mono_delta"], kv[1]["priority"]))
        chosen, why = ranked[0][0], "max composite among guarded survivors"
    else:
        # no form passes the guards: fall back to the fp32 reference form
        chosen, why = "fp32", "no quantised/fp16 form passed the guards — fp32 reference form stands"
    choice = {
        "kind": "embed-r5-export-form-choice",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rule": "run_config.export.choice_rule_fixed_before_measurement",
        "prod_mono_median_reference": PROD_MONO_MEDIAN,
        "results": results,
        "eligible": sorted(candidates),
        "chosen_form": chosen,
        "why": why,
    }
    out = args.run_dir / "export" / "measure_forms.json"
    out.write_text(json.dumps(choice, indent=1, ensure_ascii=False))
    (args.run_dir / "export" / chosen / "CHOSEN.txt").write_text(
        f"chosen export form: {chosen}\nwhy: {why}\n", encoding="utf-8")
    print(f"[choice] {chosen} — {why}; eligible={sorted(candidates)}")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
