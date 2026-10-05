"""Key-token guard v0 — the VesmaProvider policy-v1.1 override under test.

Policy: vesma-cortex ``docs/specs/labeling-policy-b2.md`` §8 (ratified
2026-10-05) — a KEY-CONTENT difference (figures, sums, names, versions)
means NOT-duplicate; envelope/cosmetic differences are duplicates;
disputed cases resolve to NOT-duplicate. The hybrid split: the bundled
artifact answers semantics, the GUARD adds a digit-token check when the
model says duplicate inside the high-cosine unreliability band
(cos ≥ :data:`KEY_TOKEN_GUARD_COS`, 0.95 — the razor zone, confirmed by
the p0-export-desync investigation).

Model verdicts here come from a stub ORT session answering a FIXED
probability (0.75 — exact in float32) — the guard's trigger/classification
logic must be deterministic; the real artifact's grading surface is
pinned by ``test_decision_cortex.py`` and is NOT this file's job.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import numpy as np
import pytest

from vesmaro.decision_provider import (
    KEY_TOKEN_GUARD_COS,
    QUESTION_IS_DUPLICATE,
    CanonRecordView,
    CanonState,
    IsDuplicateRequest,
    Noul,
    VesmaProvider,
    key_token_diff,
    key_token_guard_override,
)

from .test_decision_cortex import EMBEDDER_PIN

#: Exact-in-float32 stub probability above the 0.5 application cut —
#: every override arithmetic in this file is exact binary arithmetic.
STUB_DUPLICATE_PROBABILITY: Final[float] = 0.75

#: Its exact flip (1 - 0.75) — the overridden not-duplicate probability.
STUB_OVERRIDDEN_PROBABILITY: Final[float] = 0.25


# ── Fixtures / stubs (the test_decision_cortex.py patterns) ──────────────────


@pytest.fixture(scope="module")
def provider() -> VesmaProvider:
    """The real bundled provider with the matching (default) pin."""
    return VesmaProvider(embedder_fingerprint=EMBEDDER_PIN)


class _FixedSession:
    """Stub ORT session answering one fixed probability."""

    def __init__(self, probability: float) -> None:
        self._probability = probability

    def run(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return [np.array([self._probability], dtype=np.float32)]


class _BoomSession:
    """Stub that fails every ORT run (the deterministic-degradation stand-in)."""

    def run(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("boom")


def _swap_session(provider: VesmaProvider, stub: Any) -> Any:
    """Install a stub ORT session; returns a restore closure (the provider
    is module-scoped — every test must leave the real session behind)."""
    real = provider._session
    provider._session = stub
    return lambda: setattr(provider, "_session", real)


def _state(record: CanonRecordView, candidate: CanonRecordView, similarity: float) -> CanonState:
    return CanonState(record=record, candidate=candidate, similarity=similarity)


# ── Pure core: normalization mirror + v0 classification ──────────────────────


def test_key_token_diff_normalization_mirrors_the_features() -> None:
    """The guard diffs the FEATURES' text: case/whitespace edits are eaten
    by the normalization; punctuation is KEPT (the cortex contract) and
    survives only as non-digit tokens; tags are never consulted."""
    base = CanonRecordView(title="Weekly report", body="ship 5.4.0", tags=("a",))
    cased = CanonRecordView(title="weekly   REPORT", body="ship 5.4.0", tags=("b",))
    punctuated = CanonRecordView(title="Weekly report!", body="ship 5.4.0", tags=("a",))
    assert key_token_diff(base, cased) == frozenset()  # case/ws: eaten
    # punct: kept — the symmetric difference carries BOTH spellings.
    assert key_token_diff(base, punctuated) == frozenset({"report", "report!"})


def test_guard_override_identity_diff_is_none() -> None:
    """Identical texts → empty diff → the model verdict stands."""
    view = CanonRecordView(title="t", body="release 5.4.0")
    assert key_token_guard_override(view, view, 0.9) is None


def test_guard_override_digit_diff_flips_and_truncates() -> None:
    """A digit-bearing diff flips the verdict (1 - p) and the log detail is
    the sorted diff tokens truncated to the 120-char line budget."""
    a = CanonRecordView(title="t", body="release 5.4.0")
    b = CanonRecordView(title="t", body="release 5.5.0")
    override = key_token_guard_override(a, b, 0.75)
    assert override == (0.25, "5.4.0 5.5.0")
    long_a = CanonRecordView(title="t", body=" ".join(f"fig{i}={i}000000" for i in range(30)))
    long_b = CanonRecordView(title="t", body=" ".join(f"fig{i}={i}111111" for i in range(30)))
    _, detail = key_token_guard_override(long_a, long_b, 0.75)  # type: ignore[misc]
    assert len(detail) == 120


def test_guard_override_mixed_diff_still_flips() -> None:
    """KEY + non-KEY (mixed) diff → flip («спорное → не дубликат»)."""
    a = CanonRecordView(title="t", body="budget 100 approved")
    b = CanonRecordView(title="t", body="budget 200 approved today")
    assert key_token_guard_override(a, b, 0.75) == (0.25, "100 200 today")


def test_guard_override_numbers_as_words_are_not_caught_v0() -> None:
    """The documented v0 gap, pinned as a test: word-written numbers and
    alphabetic-only names/versions are NOT the KEY class in v0 — the gap
    closes with a policy-aware classifier, not heuristics smuggled here."""
    a = CanonRecordView(title="t", body="deadline moved to friday")
    b = CanonRecordView(title="t", body="deadline moved to monday")
    assert key_token_guard_override(a, b, 0.75) is None


# ── Provider surface: trigger band, override, fail-open ──────────────────────


def test_identity_pair_passes_without_override(
    provider: VesmaProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """An identity twin inside the band: the guard is consulted, the diff
    is empty, the model verdict passes untouched (no override marker)."""
    view = CanonRecordView(title="Weekly report", body="ship 5.4.0 today")
    restore = _swap_session(provider, _FixedSession(STUB_DUPLICATE_PROBABILITY))
    try:
        with caplog.at_level(logging.INFO, logger="vesmaro.decision_provider"):
            decision = provider.evaluate(IsDuplicateRequest(), _state(view, view, 0.99))
    finally:
        restore()
    assert decision == Noul(question=QUESTION_IS_DUPLICATE, probability=STUB_DUPLICATE_PROBABILITY)
    assert all("override=" not in r.message for r in caplog.records)


def test_fact_edit_overrides_to_not_duplicate(
    provider: VesmaProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """Policy v1.1 core case: the model says duplicate at cos≈0.99, but a
    release digit changed — the guard flips the verdict to not-duplicate."""
    record = CanonRecordView(title="release", body="release 5.4.0 ships the sync fix")
    candidate = CanonRecordView(title="release", body="release 5.5.0 ships the sync fix")
    restore = _swap_session(provider, _FixedSession(STUB_DUPLICATE_PROBABILITY))
    try:
        with caplog.at_level(logging.INFO, logger="vesmaro.decision_provider"):
            decision = provider.evaluate(IsDuplicateRequest(), _state(record, candidate, 0.99))
    finally:
        restore()
    assert decision == Noul(question=QUESTION_IS_DUPLICATE, probability=STUB_OVERRIDDEN_PROBABILITY)
    assert any(
        "override=key-token" in r.message and "diff=5.4.0 5.5.0" in r.message
        for r in caplog.records
    )


def test_cosmetic_only_diff_passes_the_model_verdict(provider: VesmaProvider) -> None:
    """Punctuation-only twin inside the band: the diff is non-digit →
    unmarked → the model verdict stands as-is (policy: cosmetic = dup)."""
    record = CanonRecordView(title="Weekly report", body="ship 5.4.0 today")
    candidate = CanonRecordView(title="Weekly report!", body="ship 5.4.0 today")
    restore = _swap_session(provider, _FixedSession(STUB_DUPLICATE_PROBABILITY))
    try:
        decision = provider.evaluate(IsDuplicateRequest(), _state(record, candidate, 0.99))
    finally:
        restore()
    assert decision == Noul(question=QUESTION_IS_DUPLICATE, probability=STUB_DUPLICATE_PROBABILITY)


@pytest.mark.parametrize(
    ("similarity", "fires"),
    [
        pytest.param(KEY_TOKEN_GUARD_COS, True, id="at-the-band-edge"),
        pytest.param(KEY_TOKEN_GUARD_COS - 0.0001, False, id="just-below-the-band"),
    ],
)
def test_guard_band_boundary(provider: VesmaProvider, similarity: float, fires: bool) -> None:
    """The band is inclusive at 0.95 (the razor zone the policy names) and
    closed below it — the calibrated model is trusted as measured there."""
    record = CanonRecordView(title="t", body="sum 100 paid")
    candidate = CanonRecordView(title="t", body="sum 200 paid")
    restore = _swap_session(provider, _FixedSession(STUB_DUPLICATE_PROBABILITY))
    try:
        decision = provider.evaluate(IsDuplicateRequest(), _state(record, candidate, similarity))
    finally:
        restore()
    expected = STUB_OVERRIDDEN_PROBABILITY if fires else STUB_DUPLICATE_PROBABILITY
    assert decision == Noul(question=QUESTION_IS_DUPLICATE, probability=expected)


def test_below_the_cos_band_guard_is_not_consulted(provider: VesmaProvider) -> None:
    """A digit diff at a mid cosine changes nothing: the guard exists only
    for the high-closeness blindness band, not as a general reranker."""
    record = CanonRecordView(title="t", body="sum 100 paid")
    candidate = CanonRecordView(title="t", body="sum 200 paid")
    restore = _swap_session(provider, _FixedSession(0.9))
    try:
        decision = provider.evaluate(IsDuplicateRequest(), _state(record, candidate, 0.5))
    finally:
        restore()
    assert isinstance(decision, Noul)
    assert decision.probability == pytest.approx(0.9)  # float32 decode, not exact


def test_model_not_duplicate_verdict_is_not_guarded(provider: VesmaProvider) -> None:
    """The guard only flips DUPLICATE verdicts — a below-the-cut model
    answer passes even with a KEY diff in the band (policy: the model owns
    semantics; the guard never manufactures a not-duplicate from nothing)."""
    record = CanonRecordView(title="t", body="sum 100 paid")
    candidate = CanonRecordView(title="t", body="sum 200 paid")
    restore = _swap_session(provider, _FixedSession(0.3))
    try:
        decision = provider.evaluate(IsDuplicateRequest(), _state(record, candidate, 0.99))
    finally:
        restore()
    assert isinstance(decision, Noul)
    assert decision.probability == pytest.approx(0.3)  # float32 decode, not exact


def test_guard_exception_fails_open(
    provider: VesmaProvider, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any guard exception → the MODEL verdict stands with a machine-
    parseable ``code=CORTEX-E-GUARD class=verdict-class`` warn — the guard
    never becomes a new way to block the prod path."""
    import vesmaro.decision_provider as dp

    def _boom(*_args: Any, **_kwargs: Any) -> tuple[float, str]:
        raise RuntimeError("guard blew up")

    monkeypatch.setattr(dp, "key_token_guard_override", _boom)
    record = CanonRecordView(title="t", body="sum 100 paid")
    candidate = CanonRecordView(title="t", body="sum 200 paid")
    restore = _swap_session(provider, _FixedSession(STUB_DUPLICATE_PROBABILITY))
    try:
        with caplog.at_level(logging.WARNING, logger="vesmaro.decision_provider"):
            decision = provider.evaluate(IsDuplicateRequest(), _state(record, candidate, 0.99))
    finally:
        restore()
    assert decision == Noul(question=QUESTION_IS_DUPLICATE, probability=STUB_DUPLICATE_PROBABILITY)
    fails = [r.message for r in caplog.records if "CORTEX-E-GUARD" in r.message]
    assert fails
    assert all("class=verdict-class" in line for line in fails)


def test_degraded_deterministic_verdict_is_not_guarded(
    provider: VesmaProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """A KEY diff + in-band cosine, but the ORT run failed: the verdict is
    the DETERMINISTIC step rule (0.96 ≥ 0.92 → 1.0) and it is deliberately
    NOT guarded — the guard corrects the model leg only (class docstring)."""
    record = CanonRecordView(title="t", body="sum 100 paid")
    candidate = CanonRecordView(title="t", body="sum 200 paid")
    restore = _swap_session(provider, _BoomSession())
    try:
        with caplog.at_level(logging.WARNING, logger="vesmaro.decision_provider"):
            decision = provider.evaluate(IsDuplicateRequest(), _state(record, candidate, 0.96))
    finally:
        restore()
    assert decision == Noul(question=QUESTION_IS_DUPLICATE, probability=1.0)
    assert any("CORTEX-E-INFER" in r.message for r in caplog.records)
    assert all("override=" not in r.message for r in caplog.records)


def test_decision_line_carries_the_full_override_shape(
    provider: VesmaProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """The B0 CORTEX-DECISION line shape with the guard suffix: primitive,
    provider, question, probability — then ``override=key-token`` and the
    truncated diff tokens («who took this decision» + the evidence)."""
    record = CanonRecordView(title="release", body="release 5.4.0 ships the sync fix")
    candidate = CanonRecordView(title="release", body="release 5.5.0 ships the sync fix")
    restore = _swap_session(provider, _FixedSession(STUB_DUPLICATE_PROBABILITY))
    try:
        with caplog.at_level(logging.INFO, logger="vesmaro.decision_provider"):
            provider.evaluate(IsDuplicateRequest(), _state(record, candidate, 0.99))
    finally:
        restore()
    lines = [r.message for r in caplog.records if r.message.startswith("decision ")]
    assert any(
        line == "decision primitive=noul provider=vesma-cortex question=is-duplicate "
        "probability=0.25 override=key-token diff=5.4.0 5.5.0"
        for line in lines
    )
