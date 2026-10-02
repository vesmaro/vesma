"""S1 stand smoke tests (ADR-0020 BF-1, epic #169).

Guards the stand itself, on top of the golden tripwires
(``tests/golden``):

* the canonical baseline JSON is schema-valid and its fingerprint
  matches the corpus as it exists on disk;
* a fresh measurement is deterministic (two complete passes agree
  exactly) and PASSES the gate against the recorded baseline;
* ``BASELINE.md`` on disk is exactly what the generator produces from
  the JSON (no hand-edits, no staleness);
* the render-neutrality invariant checker CATCHES a leak — the
  mutation test breaks ``render_retraction`` into carrying the
  quarantine reason and asserts the invariant fails (the checker is
  itself under test, so a silent pass-through cannot ship).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from benchmarks.baselines.generate_md import render_baseline_md
from benchmarks.stands.s1_quality import run as s1_run
from benchmarks.stands.s1_quality.scenarios import (
    check_render_neutrality,
    scenario_refuse_render,
)

from tests._seeded_ids import seeded_memory_ids

_BASELINE_KEYS = {
    "baseline_version",
    "stand_version",
    "corpus_fingerprint",
    "created",
    "metrics",
    "environment",
}


def _load_baseline() -> dict:
    assert s1_run.BASELINE_PATH.exists(), (
        "benchmarks/baselines/s1.json is missing — run `make bench-s1-record`"
    )
    return json.loads(s1_run.BASELINE_PATH.read_text())


def test_baseline_json_schema_and_fingerprint() -> None:
    baseline = _load_baseline()
    assert set(baseline) >= _BASELINE_KEYS
    assert baseline["baseline_version"] == 1
    assert baseline["stand_version"] == s1_run.STAND_VERSION
    fingerprint = baseline["corpus_fingerprint"]
    assert isinstance(fingerprint, str) and len(fingerprint) == 64
    int(fingerprint, 16)  # sha256 hex
    assert baseline["environment"]["deterministic_embedder"] is True
    assert baseline["metrics"], "baseline carries no metrics"
    # the fingerprint pins the corpus as it exists on disk right now —
    # a corpus edit without a re-record fails here (and in the gate)
    assert fingerprint == s1_run.corpus_fingerprint()


def test_measurement_deterministic_and_gate_green() -> None:
    # Seeded harness ids (TL decision 2026-09-14, #280): the Phase-1 id
    # tiebreak in MemoryManager.search orders equal-score groups by
    # Memory.id, which is uuid4 in production — two fresh stores draw
    # different ids per run, so cross-run byte-identity needs a FIXED id
    # draw. Each call restarts the seed sequence; ranking semantics are
    # untouched (tests/_seeded_ids.py).
    with seeded_memory_ids("s1-determinism"):
        first = s1_run.run_measurement()
    with seeded_memory_ids("s1-determinism"):
        second = s1_run.run_measurement()
    # The reference contour (BLAKE2b pipeline metrics) is byte-exact
    # deterministic. The S1m section is NOT compared byte-exact: the
    # production ONNX embedder is not guaranteed bit-identical across
    # fresh sessions (ADR-0021 determinism rules — per-arch corridors,
    # cross-run bit-identity explicitly not required); it is checked
    # for the same status/fingerprint and corridor-stable metrics.
    first_ref = {k: v for k, v in first.items() if k != "s1m"}
    second_ref = {k: v for k, v in second.items() if k != "s1m"}
    assert first_ref == second_ref, (
        f"S1 measurement is not deterministic across fresh runs:\n{first_ref!r}\n{second_ref!r}"
    )
    assert first["s1m"]["status"] == second["s1m"]["status"]
    assert first["s1m"]["fingerprint"] == second["s1m"]["fingerprint"]
    verdict = s1_run.gate_check(first, _load_baseline())
    assert verdict["pass"], f"S1 gate failed against the recorded baseline: {verdict['failures']}"


def test_baseline_md_is_generated_not_stale() -> None:
    baseline = _load_baseline()
    assert s1_run.MARKDOWN_PATH.exists(), "BASELINE.md missing next to s1.json"
    assert s1_run.MARKDOWN_PATH.read_text() == render_baseline_md(baseline), (
        "BASELINE.md does not match s1.json — regenerate with "
        "`make bench-s1-record`; never hand-edit the generated summary"
    )


def test_render_neutrality_invariant_catches_reason_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: a reason-bearing retraction render must fail the invariant.

    Patches the render the way a would-be "transparency improvement"
    would leak it (reason embedded in the render) and asserts the S1
    checker flags it — the ADR-0019 §5 compromise is enforced by a
    checker that demonstrably catches the defect class.
    """
    import vesma.manager as manager_mod

    def leaky_render(memory: object) -> str:
        from vesma.models import render_retraction

        reason = getattr(memory, "quarantine_reason", "secret")
        return f"{render_retraction(memory)[:-1]} reason={reason}]"  # type: ignore[arg-type]

    monkeypatch.setattr(manager_mod, "render_retraction", leaky_render)
    with tempfile.TemporaryDirectory() as tmp:
        refuse = scenario_refuse_render(Path(tmp))
    renders = refuse.pop("_retraction_renders")
    reasons = refuse.pop("_quarantine_reasons")
    result = check_render_neutrality({"none": []}, renders, reasons)
    assert not result["ok"], "reason-leaking retraction render passed the sweep"
    assert any(v["kind"] in {"reason-leak", "format", "class-token"} for v in result["violations"])


# ── S1m — the production-embedder model contour (ADR-0021 NM-0) ────────────────


from benchmarks.stands.s1_quality import model_contour as s1m_contour  # noqa: E402

_FP_A: dict = {
    # Self-contained round-2-era literal (a mutation-fixture baseline for
    # _FP_B), NOT the shipped round-3 weights hash — do not "fix" it.
    "provider": "nano",
    "model": "mnema-embed-v1",
    "weights_sha256": "fc61e7977c2a49f2bd5603605123fa3c4c407d43462d161884705d7fa16c540e",
    "opset": None,
}
_FP_B: dict = {
    "provider": "nano",
    "model": "mnema-embed-v1",
    "weights_sha256": "deadbeef" * 8,  # the silent weights swap NM-0 must catch
    "opset": None,
}

_S1M_METRICS: dict = {
    "precision_at_5": 0.29,
    "precision_at_10": 0.15,
    "recall_at_5": 0.98,
    "recall_at_10": 0.99,
    "mrr": 1.0,
    "ndcg_at_5": 0.98,
    "ndcg_at_10": 0.99,
    "ci95": {f"{m}": 0.01 for m in s1m_contour.CORRIDOR_METRICS},
    "judged_queries": 47,
}


def _measured_contour(fingerprint: dict | None = _FP_A, metrics: dict | None = None) -> dict:
    return s1m_contour.run_model_contour([("q-stub", [])], dimension=384) | {
        "fingerprint": fingerprint,
        "metrics": metrics if metrics is not None else dict(_S1M_METRICS),
    }


def test_model_fingerprint_schema() -> None:
    """model_fingerprint is null-or-object with the four NM-0 fields."""
    baseline = _load_baseline()
    fp = baseline.get("model_fingerprint")
    if fp is None:  # pre-NM-0 / no-provider baseline: documented migration
        return
    assert isinstance(fp, dict), "model_fingerprint must be null or an object"
    assert set(fp) == {"provider", "model", "weights_sha256", "opset"}, (
        f"model_fingerprint schema drifted: {sorted(fp)}"
    )
    assert isinstance(fp["provider"], str) and fp["provider"]
    assert isinstance(fp["model"], str) and fp["model"]
    assert fp["weights_sha256"] is None or (
        isinstance(fp["weights_sha256"], str) and len(fp["weights_sha256"]) == 64
    )
    assert fp["opset"] is None or isinstance(fp["opset"], int)


def test_fingerprint_shape_of_live_probe() -> None:
    """The live probe yields the schema — identifier-only or hashed."""
    fp = s1m_contour.model_fingerprint()
    if fp is None:
        pytest.skip("production embedding provider not probeable here")
    assert set(fp) == {"provider", "model", "weights_sha256", "opset"}
    assert fp["provider"] and fp["model"]


def test_fingerprint_equal_semantics() -> None:
    """Equivalence rules: strict on weights, lenient on unreadable opset."""
    assert s1m_contour.fingerprint_equal(_FP_A, dict(_FP_A))
    # the SILENT WEIGHTS SWAP — different hash must never compare equal
    assert not s1m_contour.fingerprint_equal(_FP_A, _FP_B)
    # provider / model swaps
    assert not s1m_contour.fingerprint_equal(_FP_A, _FP_A | {"provider": "onnxhub"})
    assert not s1m_contour.fingerprint_equal(_FP_A, _FP_A | {"model": "bge-small"})
    # absent side never equals a live one (the migration case, not a pass)
    assert not s1m_contour.fingerprint_equal(None, _FP_A)
    assert not s1m_contour.fingerprint_equal(_FP_A, None)
    # opset readability is an environment property, not a model property
    assert s1m_contour.fingerprint_equal(_FP_A, _FP_A | {"opset": 13})
    assert not s1m_contour.fingerprint_equal(_FP_A | {"opset": 13}, _FP_A | {"opset": 14})
    # identifier-only (API model: no local weights) compares by identity
    api = {"provider": "ollama", "model": "nomic", "weights_sha256": None, "opset": None}
    assert s1m_contour.fingerprint_equal(api, dict(api))
    assert not s1m_contour.fingerprint_equal(api, api | {"model": "other"})


def test_gate_fail_loud_on_embedder_substitution() -> None:
    """Mutation (THE NM-0 acceptance): swapped weights → gate RED.

    An old s1.json whose fingerprint does not match the live one must
    fail with the explicit re-baseline message — the silent embedder
    substitution hole. Tested at BOTH levels: the pure S1m gate and the
    integrated gate_check (with the REAL recorded pipeline baseline so
    only the S1m half can fire).
    """
    current = _measured_contour(fingerprint=_FP_B)
    baseline = _s1m_test_baseline()
    verdict = s1m_contour.gate_model_contour(current, baseline)
    assert not verdict["pass"]
    assert any(
        "production embedder changed" in f and "re-baseline required" in f
        for f in verdict["failures"]
    ), f"fail-loud message missing from: {verdict['failures']}"

    # integrated level: real recorded metrics with a mutated fingerprint
    # → only the S1m half can fire (the pipeline halves match themselves)
    real = _load_baseline()
    mutated = dict(real)
    mutated["model_fingerprint"] = dict(mutated["model_fingerprint"] or _FP_A) | {
        "weights_sha256": "0" * 64
    }
    real_metrics = dict(real["metrics"]) | {"s1m": current}
    assert not s1_run.gate_check(real_metrics, mutated)["pass"]


def test_gate_green_on_identical_fingerprint_and_metrics() -> None:
    current = _measured_contour(fingerprint=_FP_A)
    baseline = _s1m_test_baseline()
    assert s1m_contour.gate_model_contour(current, baseline)["pass"]


def test_gate_red_on_corridor_breach_same_fingerprint() -> None:
    """Same model, degraded quality → self-comparison corridor fails."""
    degraded = dict(_S1M_METRICS)
    degraded["recall_at_10"] = 0.50  # far below baseline 0.99 - max(0.02; ci)
    current = _measured_contour(fingerprint=_FP_A, metrics=degraded)
    baseline = _s1m_test_baseline()
    verdict = s1m_contour.gate_model_contour(current, baseline)
    assert not verdict["pass"]
    assert any("s1m recall_at_10" in f and "below corridor" in f for f in verdict["failures"])


def test_gate_migration_old_baseline_without_fingerprint() -> None:
    """Pre-NM-0 s1.json (no fingerprint): informational PASS, not a hole.

    There is nothing recorded to silently diverge from; the next
    --record pins the first fingerprint (documented ADR-0021 migration).
    """
    current = _measured_contour(fingerprint=_FP_B)  # even a DIFFERENT model
    baseline = _s1m_test_baseline(fingerprint=None)
    verdict = s1m_contour.gate_model_contour(current, baseline)
    assert verdict["pass"], verdict["failures"]
    assert not verdict.get("failures"), "migration must be a silent informational pass"


def test_skip_semantics_green_by_default_red_when_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider unavailable → SKIP is green locally, red under the CI flag."""
    reason = "ModuleNotFoundError: No module named 'onnxruntime'"
    monkeypatch.delenv(s1m_contour.REQUIRED_ENV, raising=False)
    skipped = s1m_contour.run_model_contour(None, skip_reason=reason)
    assert skipped["status"] == "skipped"
    assert reason in skipped["reason"]
    assert skipped["gate"]["pass"], "a skip must be green in the default posture"

    monkeypatch.setenv(s1m_contour.REQUIRED_ENV, "1")
    skipped_required = s1m_contour.run_model_contour(None, skip_reason=reason)
    assert not skipped_required["gate"]["pass"]
    assert any("VESMA_BENCH_S1M_REQUIRED" in f for f in skipped_required["gate"]["failures"])

    # the gate honours the skip verdict (and the flag) end-to-end
    monkeypatch.delenv(s1m_contour.REQUIRED_ENV, raising=False)
    assert s1m_contour.gate_model_contour(skipped, None)["pass"]
    monkeypatch.setenv(s1m_contour.REQUIRED_ENV, "1")
    verdict = s1m_contour.gate_model_contour(skipped, None)
    assert not verdict["pass"]

    # fingerprint pinned + skip + required → cannot verify the embedder
    # (gate_check reads the flag at CALL time, so the env set above holds)
    pinned = _s1m_test_baseline()
    assert not s1_run.gate_check(_s1_run_metrics_with(skipped), pinned)["pass"]

    # green skip against a pinned fingerprint WITHOUT the flag is already
    # pinned at the pure level: gate_model_contour(skipped, None) passed
    # above under delenv — the integrated gate_check adds no new logic
    # there (its synthetic baseline stubs the pipeline half).


def test_s1m_metrics_aggregation_on_stub_rankings() -> None:
    """recall/precision@k, MRR, nDCG over stub rankings — hermetic math.

    Two judged golden queries with known rankings pin every metric
    formula without touching the network or the real model.
    """
    from benchmarks.corpus.queries import GOLDEN_QUERIES

    judged = [q for q in GOLDEN_QUERIES if q.expected][:3]
    q1, q3 = judged[0], judged[2]  # |expected| 1 and 2
    # query 1 (au-01): its single expected slug at rank 1 → perfect row
    # query 3 (au-03): both expected slugs MISSING from top-10 → zero row
    other = sorted(q3.expected)
    rankings: list[tuple[str, list[str]]] = [
        (q1.qid, [*sorted(q1.expected), *other[:9]]),
        (q3.qid, [*sorted(q1.expected)] * 10),
    ]
    metrics = s1m_contour.measure_production_embedder(rankings)

    assert metrics["judged_queries"] == 2
    # recall@5: (1/1 + 0/2) / 2 ; precision@5: (1/5 + 0/5) / 2
    assert metrics["recall_at_5"] == pytest.approx(0.5)
    assert metrics["precision_at_5"] == pytest.approx(0.1)
    # MRR: (1/1 + 0/2→miss=0) / 2
    assert metrics["mrr"] == pytest.approx(0.5)
    # nDCG@5: query 1 gains [1,0,0,0,0] vs ideal [1] → 1.0; query 2 → 0
    assert metrics["ndcg_at_5"] == pytest.approx(0.5)
    assert metrics["ndcg_at_10"] == pytest.approx(0.5)
    assert set(metrics["ci95"]) == set(s1m_contour.CORRIDOR_METRICS)
    # probe queries (no judgement) are excluded from every denominator
    probe = next(q for q in GOLDEN_QUERIES if not q.expected)
    with_probe = [*rankings, (probe.qid, [])]
    assert s1m_contour.measure_production_embedder(with_probe)["judged_queries"] == 2


def _s1_run_metrics_with(s1m: dict) -> dict:
    """A hermetic metrics dict carrying ONLY the s1m section for gate_check.

    The S1m gate paths read exactly: ``metrics["s1m"]`` (current side)
    and ``baseline["model_fingerprint"]`` / ``baseline["s1m"]``
    (recorded side). Every OTHER gate half (retrieval corridors,
    invariants, rewrite, scenarios, McNemar) is stubbed with values that
    cannot add noise failures, so the S1m assertions are isolated:
    a RED here means the S1m logic fired, a GREEN means it held.
    """
    stub_scenario = {"pass": True}
    return {
        "retrieval": {"judged_queries": 47},
        "invariants": {},
        "rewrite": {},
        "scenarios": {"write_find": stub_scenario, "refuse_render": stub_scenario},
        "a9": {"delta_recall10_current_vs_pre_a9": 0.0},
        "s1m": s1m,
    }


def _s1m_test_baseline(fingerprint: dict | None = _FP_A, metrics: dict | None = None) -> dict:
    """A synthetic recorded s1.json shape (only the keys S1m reads)."""
    return {
        "model_fingerprint": fingerprint,
        "s1m": {
            "status": "measured",
            "metrics": metrics if metrics is not None else dict(_S1M_METRICS),
        },
    }
