"""g6a latency gate — addendum-6 §A.2 method, round-4 candidate.

Method (§A.2, ratified): CPU, batch=1, production static-graph form
(1x256, padded — NanoProvider tokenization: Truncation(256) + Padding(256)),
input sequence input_ids/attention_mask/token_type_ids, 20 note-like
RU/EN texts (7-20 tokens), warmup 5 passes, then 20 measured passes,
time.perf_counter, per-text mean/p50/p95. VESMARO_ORT_THREADS=4.

Threshold (ratified): candidate mean <= 45 ms/text (2x the measured
production-artifact baseline 20.9 ms on core-51).

Optional --context: also measure the bundled production artifact on THIS
host in the same process — a context number only (host delta vs the
core-51 baseline), never a gate.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

#: §A.2 input set: 20 note-like texts, RU/EN, 7-20 tokens each.
TEXTS: list[str] = [
    # RU
    "Деплой vesma-embed на core-51 прошёл без инцидентов, откат не понадобился.",
    "Дедлайн по корпусу round-4 — 8 октября, владелец подтвердил окно.",
    "Пароль от стендового Postgres лежит в ~/.secrets, не в репозитории.",
    "Встреча с АрхКомом перенесена на 14:00, повестка — ADR-0044.",
    "Миграция векторов заняла 3 часа на 8 тыс записей, батч 200.",
    "Баг 480: самопара инвертируется при cos 1.0, чинится фичей identity.",
    "Токен HF обновлён 5 октября, старый отозван 7 октября.",
    "Бэкап ноутбука от 24 сентября — 2654 записи, лучший снимок стора.",
    "Латентность эмбеддера 20 мс на 4 потоках, 29 мс на одном потоке.",
    "Релиз 5.6.4 опубликован, тег и страница релиза созданы, wheel 91 МБ.",
    # EN
    "Deployed the round-4 corpus pipeline on core-51, rollback was not needed.",
    "Deadline for the sealed corpus is October 8, owner confirmed the window.",
    "The standby Postgres password lives in ~/.secrets, never in the repo.",
    "ArchCom moved to 14:00, agenda item one is ADR-0044 store migration.",
    "Vector migration took 3 hours for 8k records at batch size 200.",
    "Bug 480: the self pair inverts at cos 1.0, fixed by the identity feature.",
    "HF token rotated on October 5, the old one revoked on October 7.",
    "The September 24 laptop backup holds 2654 records, the best snapshot.",
    "Embedder latency is 20 ms at four threads, 29 ms single threaded.",
    "Release 5.6.4 is published, tag and release page exist, wheel is 91 MB.",
]

WARMUP_PASSES = 5
MEASURE_PASSES = 20
G6A_MEAN_CEILING_MS = 45.0


def bench_artifact(model_spec: str, label: str) -> dict:
    """Warmup + measured passes over TEXTS with one NanoProvider session."""
    from vesmaro.config import EmbeddingConfig
    from vesmaro.embeddings import create_embedding_provider

    cfg = EmbeddingConfig(provider="nano", model=model_spec)
    provider = create_embedding_provider(cfg)
    provider.embed("mnemos g6a warmup")  # session/lazy warmup
    for _ in range(WARMUP_PASSES):
        for t in TEXTS:
            provider.embed(t)
    per_text_ms: list[float] = []
    for _ in range(MEASURE_PASSES):
        for t in TEXTS:
            t0 = time.perf_counter()
            provider.embed(t)
            per_text_ms.append((time.perf_counter() - t0) * 1000.0)
    per_text_ms.sort()
    n = len(per_text_ms)
    return {
        "label": label,
        "model_spec": model_spec,
        "sha256": provider.weights_sha256,
        "n_texts": len(TEXTS),
        "warmup_passes": WARMUP_PASSES,
        "measure_passes": MEASURE_PASSES,
        "samples": n,
        "mean_ms": round(statistics.fmean(per_text_ms), 3),
        "p50_ms": round(per_text_ms[n // 2], 3),
        "p95_ms": round(per_text_ms[int(n * 0.95) - 1], 3),
        "min_ms": round(per_text_ms[0], 3),
        "max_ms": round(per_text_ms[-1], 3),
        "dimension": provider.dimension,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="g6a latency gate (§A.2) for the round-4 candidate")
    ap.add_argument("--artifact", required=True, help="path to the candidate model.onnx")
    ap.add_argument("--context-production", action="store_true", help="also measure the bundled production artifact (context only)")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)

    # §A.2 stand: 4 ORT threads, pinned BEFORE any session is built.
    os.environ["VESMA_ORT_THREADS"] = "4"
    os.environ["VESMARO_ORT_THREADS"] = "4"

    onnx = Path(args.artifact).expanduser().resolve()
    if not onnx.is_file():
        raise SystemExit(f"error: artifact not found: {onnx}")

    candidate = bench_artifact(str(onnx), "candidate-r4")
    report: dict = {
        "method": "addendum-6 §A.2: CPU batch=1 static 1x256, VESMARO_ORT_THREADS=4, "
        f"{len(TEXTS)} RU/EN note-like texts 7-20 tokens, warmup {WARMUP_PASSES}, "
        f"{MEASURE_PASSES} measured passes, perf_counter",
        "host": {"machine": os.uname().machine, "cores": os.cpu_count()},
        "g6a": {
            "threshold_mean_ms": G6A_MEAN_CEILING_MS,
            "measured_mean_ms": candidate["mean_ms"],
            "measured_p95_ms": candidate["p95_ms"],
            "pass": candidate["mean_ms"] <= G6A_MEAN_CEILING_MS,
        },
        "candidate": candidate,
    }
    if args.context_production:
        prod = bench_artifact("vesma-embed-v1", "production-baseline-context")
        report["production_context"] = prod
        report["production_context"]["note"] = (
            "context only: core-51 TL baseline was mean 20.9 ms (addendum-6 §A.2); "
            "this row measures the same artifact on THIS host"
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
