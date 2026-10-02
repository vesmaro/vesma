"""S1m — the production-embedder model contour (ADR-0021 NM-0, epic #197).

The BLAKE2b reference (``run.py`` phase A) pins the retrieval PIPELINE —
its corridors stay the single source for pipeline mechanics forever
(ADR-0021, "replacing the BLAKE2b S1 reference" rejected). S1m pins the
THING THAT EMBEDS: the SAME judged corpus and the SAME golden queries,
run through the PRODUCTION embedder (the provider the shipped default
config builds — since NM-1c the bundled vesma-embed ONNX artifact),
measuring the semantic retrieval quality of that model:

    recall@k, precision@k (k ∈ {5, 10}), MRR, nDCG@k

Gating is SELF-comparison per ADR-0021 NM-0: the corridor for the
production embedder is ``its own recorded baseline - max(0.02; 95% CI)``
— never a comparison against the BLAKE2b numbers (the reference
measures retrieval mechanics, the model semantic quality; they are
different meters). An embedder change (weights, provider, model id) is
a re-baseline trigger enforced FAIL-LOUD by the ``model_fingerprint``
baseline field: a gate run whose live fingerprint differs from the
recorded one fails with an explicit "explicit re-baseline required"
message — the silent embedder substitution that passed before NM-0 can
no longer pass.

Network / heavy-init grace (the production embedder initializes an ONNX
runtime session over its bundled artifact): when the provider cannot be
built in the run environment (artifact missing, optional dependency
absent), the S1m section reports ``status: "skipped"`` with the concrete
reason — NOT red. A skipped S1m is a gate failure only when the operator
asked for it to be mandatory via ``VESMA_BENCH_S1M_REQUIRED=1`` (CI
nightlies); in the default local ``make verify`` posture the skip is
tolerated.
"""

from __future__ import annotations

import math
import os
import platform
from pathlib import Path
from typing import Any

from benchmarks.corpus.queries import GOLDEN_QUERIES

K_VALUES: tuple[int, ...] = (5, 10)

#: Skip is tolerated locally; VESMA_BENCH_S1M_REQUIRED=1 (CI nightlies)
#: makes an unavailable production embedder a hard gate failure.
REQUIRED_ENV = "VESMA_BENCH_S1M_REQUIRED"

#: S1m metrics under the self-comparison corridor (ci95 keys are 1:1,
#: mirroring the S1 pipeline contour).
CORRIDOR_METRICS: tuple[str, ...] = (
    "precision_at_5",
    "precision_at_10",
    "recall_at_5",
    "recall_at_10",
)

#: ADR-0020 corridor rule for the model contour too.
CORRIDOR_FLOOR = 0.02


# ── production embedder construction ─────────────────────────────────────────


def production_config() -> Any:
    """The shipped default embedding config (``EmbeddingConfig()``).

    Instantiated directly — pydantic-settings env binding happens at
    the ``Settings`` root, so this is the *shipped production default*
    provider/model pair, which is exactly what S1m must measure: the
    contour answers "what does the default install embed with".
    """
    from mnemos.config import EmbeddingConfig

    return EmbeddingConfig()


def build_production_embedder() -> Any:
    """Build the production embedder, or raise (→ the skip path).

    Construction initializes the ONNX runtime session over the bundled
    vesma-embed artifact — callers keep this inside the skip guard.
    """
    from mnemos.embeddings import create_embedding_provider

    return create_embedding_provider(production_config())


# ── model fingerprint ─────────────────────────────────────────────────────────


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _onnx_opset(onnx_path: Path) -> int | None:
    """Opset of a local ONNX artifact, via the ``onnx`` checker only.

    A hand-rolled protobuf scan is deliberately NOT shipped: an
    unreadable artifact must yield ``None`` (honest), while a
    mis-parsed one would yield a WRONG integer — a false fingerprint
    mismatch and a spurious red gate. ``onnx`` is not a runtime
    dependency, so ``None`` is the expected value in most environments;
    ``fingerprint_equal`` treats a null either-side opset as "not
    readable here", not as a model property.
    """
    try:
        import onnx

        model = onnx.load(str(onnx_path), load_external_data=False)
        imports = model.opset_import
        return int(imports[0].version) if imports else None
    except Exception:
        return None


def _mnema_fingerprint(provider: str, model: str) -> dict[str, Any] | None:
    """Fingerprint the vesma-embed model's local ONNX artifact.

    The hash is over the REAL resolved ``model.onnx`` (bundled artifact or
    the explicit path from ``EmbeddingConfig.model``) — a weights swap on
    disk (the "silent substitution" hole) changes it even when every
    provider string stays identical.
    """
    try:
        from mnemos.embeddings import mnema_artifact_onnx_path
    except Exception:  # mnemos import failure in the stand environment
        return None
    try:
        onnx_path = mnema_artifact_onnx_path(model)
    except Exception:  # unresolvable model spec → identifier-only below
        return None
    if not onnx_path.is_file():
        return None
    return {
        "provider": provider,
        "model": model,
        "weights_sha256": _sha256_file(onnx_path),
        "opset": _onnx_opset(onnx_path),
    }


def model_fingerprint() -> dict[str, Any] | None:
    """Fingerprint the embedder the production default config builds.

    Enrichment order: local artifact hash when the provider keeps one
    on disk (nano — bundled or explicit path), else identifier-only
    (API / lazy-download providers carry no local weights to hash — the
    pinned model id is the identity). Never raises; the skip machinery
    owns the fail/skip decision, not this probe.
    """
    try:
        cfg = production_config()
        provider = str(cfg.provider).lower()
        model = str(cfg.model)
    except Exception:
        return None
    if provider in ("chromadb", "chroma", "default"):
        # NM-1c migration: the factory degrades legacy values to nano with
        # a deprecation warning — the fingerprint must pin the EFFECTIVE
        # embedder, or every legacy-config gate run would false-RED on the
        # provider field alone. The legacy default MODEL degrades with it
        # (review #221 F1): the factory swaps it for the bundled
        # vesma-embed artifact, so the fingerprint must record that swap.
        provider = "nano"
        try:
            from mnemos.embeddings import MNEMA_EMBED_MODEL

            if model.strip().lower() == "all-minilm-l6-v2":
                model = MNEMA_EMBED_MODEL
        except Exception:
            pass
    if provider == "nano":
        return _mnema_fingerprint("nano", model) or {
            "provider": "nano",
            "model": model,
            "weights_sha256": None,
            "opset": None,
        }
    return {
        "provider": provider,
        "model": model,
        "weights_sha256": None,
        "opset": None,
    }


def fingerprint_label(fingerprint: dict[str, Any] | None) -> str:
    """Compact human label for gate messages and markdown."""
    if not fingerprint:
        return "none"
    parts = [str(fingerprint.get("provider")), str(fingerprint.get("model"))]
    if fingerprint.get("weights_sha256"):
        parts.append(f"sha256:{fingerprint['weights_sha256'][:12]}…")
    if fingerprint.get("opset") is not None:
        parts.append(f"opset:{fingerprint['opset']}")
    return " ".join(parts)


def fingerprint_equal(recorded: dict[str, Any] | None, current: dict[str, Any] | None) -> bool:
    """Fingerprint equivalence for the fail-loud gate.

    ``None`` on either side never equals a live fingerprint — the
    absent-recorded case is the documented migration (first ``--record``
    pins it), not a silent pass. The opset participates only when BOTH
    sides read it: its readability depends on the optional ``onnx``
    package being importable in the run environment, which is an
    environment property, not a model property — a strict comparison
    would red-gate identical models across environments. The
    weights hash has no such ambiguity (hashing a local file is
    deterministic wherever the file exists) and compares strict.
    """
    if recorded is None or current is None:
        return False
    if recorded.get("provider") != current.get("provider"):
        return False
    if recorded.get("model") != current.get("model"):
        return False
    if recorded.get("weights_sha256") != current.get("weights_sha256"):
        return False
    recorded_opset = recorded.get("opset")
    current_opset = current.get("opset")
    opsets_both_readable = recorded_opset is not None and current_opset is not None
    return not (opsets_both_readable and recorded_opset != current_opset)


# ── metric aggregation (embedder-agnostic, stub-testable) ─────────────────────


def _dcg(gains: list[float]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def _ndcg(ranked: list[str], expected: frozenset[str], k: int) -> float:
    gains = [1.0 if s in expected else 0.0 for s in ranked[:k]]
    ideal = [1.0] * min(len(expected), k)
    denom = _dcg(ideal)
    return _dcg(gains) / denom if denom else 0.0


def _ci95(values: list[float]) -> float:
    """Half-width of the normal 95% CI of a per-query mean (0 for n<2)."""
    from statistics import NormalDist

    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return NormalDist().inv_cdf(0.975) * (var**0.5) / n**0.5


def measure_production_embedder(
    ranked_results: list[tuple[str, list[str]]],
) -> dict[str, Any]:
    """Aggregate S1m metrics over per-query slug rankings.

    ``ranked_results`` is ``(qid, ranked_slugs)`` pairs in the corpus's
    fixed query order (the same ``_measure_queries`` output shape the
    reference contour uses); ``expected`` comes from the golden query
    by id, so this function is embedder-agnostic and hermetically
    testable. Probe queries (no relevance judgement) are excluded,
    mirroring the reference contour.
    """
    by_id = {q.qid: q for q in GOLDEN_QUERIES}
    per: dict[str, list[float]] = {m: [] for m in CORRIDOR_METRICS}
    mrrs: list[float] = []
    ndcg: dict[int, list[float]] = {5: [], 10: []}
    judged = 0
    for qid, ranked in ranked_results:
        query = by_id.get(qid)
        if query is None or not query.expected:
            continue
        judged += 1
        expected = frozenset(query.expected)
        for k in K_VALUES:
            top = ranked[:k]
            hits = len(set(top) & set(expected))
            per[f"precision_at_{k}"].append(hits / k)
            per[f"recall_at_{k}"].append(hits / len(expected))
            ndcg[k].append(_ndcg(ranked, expected, k))
        first_hit = next((i + 1 for i, s in enumerate(ranked) if s in expected), 0)
        mrrs.append(1.0 / first_hit if first_hit else 0.0)

    metrics: dict[str, Any] = {
        name: round(sum(vs) / len(vs), 6) if vs else 0.0 for name, vs in per.items()
    }
    metrics["mrr"] = round(sum(mrrs) / len(mrrs), 6) if mrrs else 0.0
    metrics["ndcg_at_5"] = round(sum(ndcg[5]) / len(ndcg[5]), 6) if ndcg[5] else 0.0
    metrics["ndcg_at_10"] = round(sum(ndcg[10]) / len(ndcg[10]), 6) if ndcg[10] else 0.0
    metrics["ci95"] = {name: round(_ci95(vs), 6) for name, vs in per.items() if vs}
    metrics["judged_queries"] = judged
    return metrics


# ── contour report + gate ─────────────────────────────────────────────────────


def s1m_required() -> bool:
    """``VESMA_BENCH_S1M_REQUIRED`` in {1,true,yes} → skips are red."""
    return os.environ.get(REQUIRED_ENV, "").strip().lower() in ("1", "true", "yes")


def run_model_contour(
    ranked_results: list[tuple[str, list[str]]] | None,
    *,
    skip_reason: str | None = None,
    dimension: int | None = None,
) -> dict[str, Any]:
    """Assemble the S1m report written into ``s1.json``.

    ``ranked_results is None`` is the explicit skip path: the caller
    could not build/initialize the production embedder in this
    environment and passes the concrete reason. Report shape::

        s1m: {
          "status": "measured" | "skipped",
          "reason": <skip reason or null>,
          "required": <bool — the flag at run time>,
          "fingerprint": {provider, model, weights_sha256, opset} | null,
          "metrics": {...} | null,
          "environment": {"arch": ..., "dimension": ...},
          "gate": {"pass": bool, "failures": [str]}
        }
    """
    required = s1m_required()
    report: dict[str, Any] = {
        "status": "measured",
        "reason": None,
        "required": required,
        "fingerprint": model_fingerprint(),
        "metrics": None,
        "environment": {"arch": platform.machine(), "dimension": dimension},
        "gate": {"pass": True, "failures": []},
    }
    if ranked_results is None:
        report["status"] = "skipped"
        report["reason"] = skip_reason or (
            "production embedding provider unavailable in this environment"
        )
        if required:
            report["gate"] = {
                "pass": False,
                "failures": [
                    "s1m skipped but VESMA_BENCH_S1M_REQUIRED is set — the "
                    "production-embedder contour is mandatory here"
                ],
            }
        return report

    metrics = measure_production_embedder(ranked_results)
    metrics["dimension"] = dimension
    report["metrics"] = metrics
    return report


def gate_model_contour(current: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any]:
    """The S1m half of the gate (ADR-0021 NM-0). ``baseline`` is the
    whole recorded ``s1.json`` (reads ``model_fingerprint`` + ``s1m``).

    Rules, in order:

    * current run skipped → the skip stands (the required flag already
      made it red inside :func:`run_model_contour` and that verdict is
      mirrored out);
    * no recorded production fingerprint (pre-NM-0 baseline, or the
      baseline was recorded in a no-provider environment) →
      informational PASS with a note: the next ``--record`` pins the
      first fingerprint. This is the documented migration, not a hole —
      there is nothing recorded to silently diverge from;
    * recorded fingerprint ≠ live fingerprint → FAIL LOUD, message per
      ADR-0021 ("explicit re-baseline required (--record), same PR").
      This is the hole NM-0 closes;
    * metrics below ``recorded - max(0.02; recorded ci95)`` → FAIL
      (self-comparison corridor; never against the BLAKE2b reference).
    """
    if current.get("status") != "measured":
        gate = current.get("gate") or {}
        if gate and not gate.get("pass"):
            return {"pass": False, "failures": list(gate.get("failures", []))}
        # the required flag is re-read at GATE time too: a report built
        # in a permissive environment can be gated under CI semantics
        # (and vice versa) without re-running the contour.
        if s1m_required():
            return {
                "pass": False,
                "failures": [
                    "s1m skipped but VESMA_BENCH_S1M_REQUIRED is set — the "
                    "production-embedder contour is mandatory here"
                ],
            }
        return {"pass": True, "failures": []}

    recorded_fp = (baseline or {}).get("model_fingerprint") or None
    recorded_s1m = (baseline or {}).get("s1m") or {}
    live_fp = current.get("fingerprint") or None

    if recorded_fp is None:
        return {
            "pass": True,
            "failures": [],
            "note": (
                "s1m: baseline carries no production fingerprint (pre-NM-0 "
                "format or recorded without the provider) — informational "
                "this run; the next --record pins it"
            ),
        }

    if not fingerprint_equal(recorded_fp, live_fp):
        return {
            "pass": False,
            "failures": [
                "production embedder changed "
                f"(old={fingerprint_label(recorded_fp)} "
                f"new={fingerprint_label(live_fp)}) — explicit re-baseline "
                "required (--record), same PR per ADR-0021"
            ],
        }

    failures: list[str] = []
    base_metrics = recorded_s1m.get("metrics") or {}
    if not base_metrics:
        return {
            "pass": True,
            "failures": [],
            "note": (
                "s1m: fingerprint pinned but no recorded metrics corridor — "
                "informational this run; re-record to pin the corridor"
            ),
        }
    cur_metrics = current.get("metrics") or {}
    base_ci = base_metrics.get("ci95", {})
    for metric in CORRIDOR_METRICS:
        if metric not in base_metrics or metric not in cur_metrics:
            failures.append(f"s1m baseline misses metric {metric}")
            continue
        margin = max(CORRIDOR_FLOOR, float(base_ci.get(metric, 0.0)))
        floor = float(base_metrics[metric]) - margin
        if float(cur_metrics[metric]) < floor:
            failures.append(
                f"s1m {metric}={cur_metrics[metric]:.4f} below corridor "
                f"{floor:.4f} (baseline {base_metrics[metric]:.4f} "
                f"- max(0.02; ci {margin:.4f}))"
            )
    return {"pass": not failures, "failures": failures}
