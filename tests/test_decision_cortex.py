"""W5d — VesmaProvider: the bundled vesma-cortex-v1 artifact under test.

Covers the brief W5d §2.6 acceptance surface: bundle pin (byte
identity), load/validation (spec §6 steps 1-8), error codes (spec §7),
the frozen feature contract, fail-open degradation (broken artifact →
DeterministicProvider + ``CORTEX-E-LOAD``), the off-by-default flag, and
an integration smoke verdict on REAL corpus records with a similarity
MEASURED by the real bundled embedder (the minting-flow shape: the
retrieval leg measures, the provider consumes — never re-measures).

Load-time fail-open is exercised through the seam factory
``resolve_decision_provider``; per-request INFER/SCHEMA degradation is
exercised directly on the provider.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import asdict
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any, Final

import numpy as np
import pytest

from vesmaro.config import EmbeddingConfig, VesmaConfig
from vesmaro.decision_jev import resolve_decision_provider
from vesmaro.decision_provider import (
    CORTEX_ARTIFACT_DIR,
    CORTEX_DUPLICATE_PROBABILITY_THRESHOLD,
    CORTEX_FEATURE_NAMES,
    CORTEX_MAX_ARTIFACT_BYTES,
    CORTEX_MODEL_NAME,
    QUESTION_IS_DUPLICATE,
    SPECTRUM_RECORD_QUALITY,
    CanonRecordView,
    CanonState,
    CortexError,
    CortexMetaError,
    CortexPinError,
    CortexSchemaError,
    DeterministicProvider,
    GoalOverlapRequest,
    IsDuplicateRequest,
    MissingEvidenceError,
    Noul,
    RecordQualityRequest,
    Score,
    UnsupportedPrimitiveError,
    VesmaProvider,
    assert_cortex_embedder_pin,
    cortex_feature_set_sha256,
    cortex_pair_features,
    validate_cortex_metadata_props,
)
from vesmaro.embeddings import NanoProvider, config_fingerprint

from .test_decision_provider import load_corpus

# ── Frozen pins (B1 revision beb0a65d, recalled-safe; verdict #480) ───────────

#: The bundled artifact's weights sha256 — the recalibration identity
#: (a new sha = new weights = recalibration event, spec §8).
WEIGHTS_SHA256: Final[str] = "beb0a65da14f9ed2554951aa8850f68db47fd1212af1577955239a34ab04a091"

#: The artifact's embedder pin — exactly the default engine vintage
#: (``config_fingerprint(EmbeddingConfig())`` → ``nano:sha256:<hash>``).
EMBEDDER_PIN: Final[str] = (
    "nano:sha256:3b752e0671a50da5c108cb50e49630a66c160f7683afedcf879e1880d84317ba"
)

#: sha256 of the ``\\n``-joined frozen feature names (spec §4).
FEATURE_SET_SHA256: Final[str] = "dd86228f8c634f28d8a15b2d8279da1b99735d68e309be02d30f1c24698fa6af"

#: The artifact's train-corpus fingerprint (B1 manifest + ONNX metadata).
CORPUS_FINGERPRINT: Final[str] = "edd940730a471e3d8c84461b7c3ca35dd86d691f354af0fcf4c498745d1aff61"

#: The frozen 13-feature contract — literal pin (cortex repo, A3a freeze).
FROZEN_FEATURE_NAMES: Final[tuple[str, ...]] = (
    "cos_target",
    "char3_jaccard",
    "char4_jaccard",
    "char5_jaccard",
    "char3_containment",
    "char4_containment",
    "char5_containment",
    "tag_jaccard",
    "title_len_delta",
    "body_len_delta",
    "tag_count_delta",
    "type_match",
    "lang_match",
)

_VALID_METADATA: Final[dict[str, str]] = {
    "name": CORTEX_MODEL_NAME,
    "version": "1",
    "embedder_pin": EMBEDDER_PIN,
    "corpus_fingerprint": "beea9355e855fcf041d8689bfbecd3b2f7be8e1fe65a2c524f9b35d1fc5ad278",
    "trained_at": "2026-10-01T03:20:07+00:00",
    "candidate": "d-boost",
    "features": "\n".join(FROZEN_FEATURE_NAMES),
    "feature_set_sha256": FEATURE_SET_SHA256,
}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def provider() -> VesmaProvider:
    """The real bundled provider with the matching (default) pin."""
    return VesmaProvider(embedder_fingerprint=EMBEDDER_PIN)


@pytest.fixture(scope="module")
def embedder() -> NanoProvider:
    """The real bundled embedder — the minting leg's similarity measurer."""
    return NanoProvider()


def _cosine(embedder: NanoProvider, a: CanonRecordView, b: CanonRecordView) -> float:
    """Measured cosine over the views' embedding text (the caller's leg
    measures; the provider under test consumes, never re-measures)."""
    va = embedder.embed(a.embedding_text())
    vb = embedder.embed(b.embedding_text())
    dot = sum(x * y for x, y in zip(va, vb, strict=True))
    return dot / (math.sqrt(sum(x * x for x in va)) * math.sqrt(sum(x * x for x in vb)))


def _corpus_view(key_suffix: str) -> CanonRecordView:
    for key, view in load_corpus():
        if key.endswith(key_suffix):
            return view
    raise AssertionError(f"corpus record not found: {key_suffix}")


def _swap_session(provider: VesmaProvider, stub: Any) -> Any:
    """Install a stub ORT session; returns a restore closure (the provider
    is module-scoped — every test must leave the real session behind)."""
    real = provider._session
    provider._session = stub
    return lambda: setattr(provider, "_session", real)


# ── Bundle pin (byte identity of the shipped artifact) ────────────────────────


def test_bundle_onnx_is_byte_identical_to_the_adopted_artifact() -> None:
    onnx_path = Path(str(resource_files("vesmaro") / "models" / CORTEX_ARTIFACT_DIR / "model.onnx"))
    payload = onnx_path.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == WEIGHTS_SHA256, (
        "bundled model.onnx sha256 drifted from the adopted B1 artifact "
        "(recalled-safe, verdict #480) — a new sha is a RECALIBRATION "
        "EVENT (spec §8), never a silent swap"
    )
    assert len(payload) <= CORTEX_MAX_ARTIFACT_BYTES


def test_bundle_manifest_pins_the_same_weights() -> None:
    manifest = json.loads(
        Path(
            str(resource_files("vesmaro") / "models" / CORTEX_ARTIFACT_DIR / "manifest.json")
        ).read_text(encoding="utf-8")
    )
    assert manifest["name"] == "vesma-cortex-v1"
    # Current manifest contract: the weights hash lives under `sha256`
    # (pre-B2 manifests keyed it `weights_sha256`).
    assert manifest["sha256"] == WEIGHTS_SHA256
    assert manifest["embedder_pin"] == EMBEDDER_PIN
    assert manifest["corpus_fingerprint"] == CORPUS_FINGERPRINT
    assert manifest["feature_set_sha256"] == FEATURE_SET_SHA256


# ── Load + validation (spec §6 steps 1-8) ────────────────────────────────────


def test_provider_loads_and_passes_the_pin(provider: VesmaProvider) -> None:
    assert provider.name == "vesma-cortex"
    assert provider.weights_sha256 == WEIGHTS_SHA256
    assert provider.embedder_pin == EMBEDDER_PIN
    assert provider.corpus_fingerprint == CORPUS_FINGERPRINT  # train-corpus id


def test_provider_loads_with_the_doctor_side_fingerprint() -> None:
    """The resolver's fingerprint source (config_fingerprint — no session
    is built) equals the live pin: the wiring path loads without booting
    an embedder."""
    wired = VesmaProvider(embedder_fingerprint=config_fingerprint(EmbeddingConfig()))
    assert wired.weights_sha256 == WEIGHTS_SHA256


def test_feature_contract_is_frozen() -> None:
    assert CORTEX_FEATURE_NAMES == FROZEN_FEATURE_NAMES
    assert len(CORTEX_FEATURE_NAMES) == 13
    assert cortex_feature_set_sha256() == FEATURE_SET_SHA256


def test_pin_mismatch_is_a_loud_refusal() -> None:
    with pytest.raises(CortexPinError) as excinfo:
        VesmaProvider(embedder_fingerprint="ollama:llama3")
    assert excinfo.value.code == "CORTEX-E-PIN"
    assert "recalibration" in str(excinfo.value)


def test_empty_fingerprint_refuses_the_pin() -> None:
    with pytest.raises(CortexPinError):
        VesmaProvider(embedder_fingerprint="")


def test_missing_artifact_is_a_load_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import vesmaro.decision_provider as dp

    monkeypatch.setattr(dp, "CORTEX_ARTIFACT_DIR", "no-such-bundle")
    with pytest.raises(CortexError) as excinfo:
        VesmaProvider(embedder_fingerprint=EMBEDDER_PIN)
    assert excinfo.value.code == "CORTEX-E-LOAD"


def _fake_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, payload: bytes
) -> None:
    """Redirect the provider's resource resolution at a tmp models tree."""
    import vesmaro.decision_provider as dp

    fake = tmp_path / "models" / name
    fake.mkdir(parents=True)
    (fake / "model.onnx").write_bytes(payload)
    monkeypatch.setattr(dp, "CORTEX_ARTIFACT_DIR", name)
    monkeypatch.setattr(dp, "resource_files", lambda _pkg: tmp_path)


def test_oversized_artifact_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ≤5 MB gate fires before any ORT session is built."""
    _fake_bundle(monkeypatch, tmp_path, "oversized-cortex", b"\0" * (CORTEX_MAX_ARTIFACT_BYTES + 1))
    with pytest.raises(CortexError) as excinfo:
        VesmaProvider(embedder_fingerprint=EMBEDDER_PIN)
    assert excinfo.value.code == "CORTEX-E-SIZE"


def test_corrupt_artifact_is_a_load_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_bundle(monkeypatch, tmp_path, "corrupt-cortex", b"definitely not protobuf")
    with pytest.raises(CortexError) as excinfo:
        VesmaProvider(embedder_fingerprint=EMBEDDER_PIN)
    assert excinfo.value.code == "CORTEX-E-LOAD"


def test_garbage_threads_env_is_wrapped_as_load_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary wrap: an unparseable VESMARO_ORT_THREADS is a
    CORTEX-E-LOAD (fail-open-able), not an escaping ValueError."""
    monkeypatch.setenv("VESMARO_ORT_THREADS", "not-a-number")
    with pytest.raises(CortexError) as excinfo:
        VesmaProvider(embedder_fingerprint=EMBEDDER_PIN)
    assert excinfo.value.code == "CORTEX-E-LOAD"


class _DriftedMetaSession:
    """Stub ORT session with the right graph contract but drifted
    metadata_props (the tampered-bundle stand-in)."""

    def __init__(self, props: dict[str, str], *_args: Any, **_kwargs: Any) -> None:
        self._props = props

    def get_inputs(self) -> list[Any]:
        return [type("_Input", (), {"name": "features"})()]

    def get_modelmeta(self) -> Any:
        return type("_Meta", (), {"custom_metadata_map": self._props})()


@pytest.mark.parametrize("drift", [{"feature_set_sha256": "0" * 64}, {"version": "2"}])
def test_metadata_drift_refuses_through_the_real_load_path(
    monkeypatch: pytest.MonkeyPatch, drift: dict[str, str]
) -> None:
    """#459: validate_cortex_metadata_props fires THROUGH _load — a
    session whose metadata carries wrapper/graph drift raises the typed
    CORTEX-E-META on construction, not only on the pure-function surface."""
    import onnxruntime as ort

    monkeypatch.setattr(
        ort,
        "InferenceSession",
        lambda *_a, **_kw: _DriftedMetaSession(dict(_VALID_METADATA, **drift)),
    )
    with pytest.raises(CortexMetaError) as excinfo:
        VesmaProvider(embedder_fingerprint=EMBEDDER_PIN)
    assert excinfo.value.code == "CORTEX-E-META"


# ── Metadata validation (spec §6 steps 2 + 8 — pure unit surface) ────────────


def test_metadata_validation_accepts_the_v1_contract() -> None:
    validate_cortex_metadata_props(_VALID_METADATA)


def test_metadata_validation_accepts_in_v1_weight_refreshes() -> None:
    """Spec §8: a weights refresh inside v1 bumps version to 1.<n> —
    still major-1 compatible."""
    validate_cortex_metadata_props(dict(_VALID_METADATA, version="1.4"))


@pytest.mark.parametrize(
    "override",
    [
        {"name": "some-other-model"},
        {"version": "2"},
        {"version": ""},
        {"version": "two"},
        {"features": "cos_target\nchar3_jaccard"},
        {"features": ""},
        {"feature_set_sha256": "0" * 64},
        {"feature_set_sha256": ""},
    ],
)
def test_metadata_validation_rejects_drift(override: dict[str, str]) -> None:
    props = dict(_VALID_METADATA)
    props.update(override)
    with pytest.raises(CortexMetaError) as excinfo:
        validate_cortex_metadata_props(props)
    assert excinfo.value.code == "CORTEX-E-META"


def test_pin_assert_pure_function() -> None:
    assert_cortex_embedder_pin(EMBEDDER_PIN, EMBEDDER_PIN)
    with pytest.raises(CortexPinError) as excinfo:
        assert_cortex_embedder_pin(EMBEDDER_PIN, "nano:sha256:deadbeef")
    assert excinfo.value.code == "CORTEX-E-PIN"


# ── Feature builder (frozen mirror of cortex.features.pair) ───────────────────


def test_features_identical_pair() -> None:
    view = CanonRecordView(
        title="Fix auth", body="token refresh missing", tags=("bug",), language="en"
    )
    values = cortex_pair_features(view, view, 0.93)
    assert len(values) == 13
    # Identical sides: every set measure 1.0, every delta 0.0;
    # lang_match 1.0 (same non-null "en"), type_match 0.0 (double-null —
    # the frozen rule: only a same NON-NULL value matches).
    expected = [0.93] + [1.0] * 7 + [0.0] * 4 + [1.0]
    assert values == pytest.approx(expected)


def test_features_hand_computed_pair() -> None:
    a = CanonRecordView(title="ab", body="cd", tags=("x", "y"), language="ru", record_type="note")
    b = CanonRecordView(title="ab", body="ce", tags=("x", "z"), language="en", record_type="note")
    values = cortex_pair_features(a, b, 0.5)
    # Normalized texts "ab cd" vs "ab ce": 3-grams {ab␣,b␣c,␣cd} vs {ab␣,b␣c,␣ce}
    # → jaccard 2/4, containment 2/3; 4-grams → j 1/3, c 1/2; 5-grams → j 0, c 0;
    # tags {x,y} vs {x,z} → jaccard 1/3; lengths equal; type matches; lang differs.
    expected = [0.5, 0.5, 1 / 3, 0.0, 2 / 3, 0.5, 0.0, 1 / 3, 0.0, 0.0, 0.0, 1.0, 0.0]
    assert values == pytest.approx(expected)


def test_features_empty_texts_follow_the_both_empty_rule() -> None:
    empty = CanonRecordView(title="", body="")
    values = cortex_pair_features(empty, empty, 1.0)
    assert values == pytest.approx(
        [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    )


def test_features_containment_one_empty_side_is_zero() -> None:
    full = CanonRecordView(title="abcdef", body="ghijkl")
    empty = CanonRecordView(title="", body="")
    values = cortex_pair_features(full, empty, 0.0)
    # One side empty → containment 0.0 and jaccard 0.0 (union non-empty).
    assert values[1:7] == pytest.approx([0.0] * 6)


@pytest.mark.parametrize("bad_similarity", [float("nan"), 1.5, -1.01])
def test_features_invalid_similarity_is_a_schema_error(bad_similarity: float) -> None:
    view = CanonRecordView(title="t", body="b")
    with pytest.raises(CortexSchemaError) as excinfo:
        cortex_pair_features(view, view, bad_similarity)
    assert excinfo.value.code == "CORTEX-E-SCHEMA"


def test_features_non_numeric_similarity_is_a_schema_error() -> None:
    view = CanonRecordView(title="t", body="b")
    with pytest.raises(CortexSchemaError):
        cortex_pair_features(view, view, "not-a-number")  # type: ignore[arg-type]


# ── Answers over the seam interface ───────────────────────────────────────────


def test_record_quality_is_the_honest_placeholder(provider: VesmaProvider) -> None:
    decision = provider.evaluate(
        RecordQualityRequest(), CanonState(record=CanonRecordView(title="t", body="b"))
    )
    assert decision == Score(spectrum=SPECTRUM_RECORD_QUALITY, value=0.5, confidence=0.5)


def test_is_duplicate_probability_is_calibrated_not_a_step(provider: VesmaProvider) -> None:
    """Spec §3: a graded likelihood, deliberately not the {0.0, 1.0} step
    of the deterministic baseline."""
    a = CanonRecordView(
        title="Fix auth bug",
        body="The login flow breaks when the token expires; refresh handling missing.",
        tags=("bug", "auth"),
        language="en",
    )
    same = CanonRecordView(
        title="Fix auth bug",
        body="The login flow breaks when the token expires; refresh handling missing.",
        tags=("bug", "auth"),
        language="en",
    )
    other = CanonRecordView(
        title="Meeting notes",
        body="Weekly sync: roadmap discussion, hiring plan, release schedule.",
        tags=("meeting",),
        language="en",
    )
    twin = provider.evaluate(
        IsDuplicateRequest(), CanonState(record=a, candidate=same, similarity=0.93)
    )
    far = provider.evaluate(
        IsDuplicateRequest(), CanonState(record=a, candidate=other, similarity=0.41)
    )
    assert isinstance(twin, Noul) and isinstance(far, Noul)
    assert twin.question == QUESTION_IS_DUPLICATE
    assert 0.0 < twin.probability < 1.0
    assert 0.0 < far.probability < 1.0
    assert twin.probability > far.probability
    # Application policy (product side): the 0.5 cut separates them.
    assert twin.probability >= CORTEX_DUPLICATE_PROBABILITY_THRESHOLD
    assert far.probability < CORTEX_DUPLICATE_PROBABILITY_THRESHOLD


def test_is_duplicate_requires_measured_evidence(provider: VesmaProvider) -> None:
    record = CanonRecordView(title="t", body="b")
    with pytest.raises(MissingEvidenceError):
        provider.evaluate(IsDuplicateRequest(), CanonState(record=record))
    with pytest.raises(MissingEvidenceError):
        provider.evaluate(IsDuplicateRequest(), CanonState(record=record, candidate=record))


def test_goal_overlap_is_declined_not_fabricated(provider: VesmaProvider) -> None:
    state = CanonState(
        record=CanonRecordView(title="t", body="b"),
        candidate=CanonRecordView(title="u", body="c"),
    )
    with pytest.raises(UnsupportedPrimitiveError):
        provider.evaluate(GoalOverlapRequest(), state)


# ── Per-request fail-open (spec §7: INFER / SCHEMA degrade per verdict) ───────


class _BoomSession:
    """Stub that fails every ORT run."""

    def run(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("boom")


class _OutOfRangeSession:
    """Stub whose output decodes to a probability outside [0, 1]."""

    def run(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return [np.array([1.5], dtype=np.float32)]


class _UndecodableSession:
    """Stub whose output has the right (1,) shape but no scalar value."""

    def run(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return [np.array([object()], dtype=object)]


def test_infer_failure_degrades_to_the_deterministic_step(
    provider: VesmaProvider, caplog: pytest.LogCaptureFixture
) -> None:
    state = CanonState(
        record=CanonRecordView(title="t", body="b"),
        candidate=CanonRecordView(title="t", body="b"),
        similarity=0.93,
    )
    restore = _swap_session(provider, _BoomSession())
    try:
        with caplog.at_level(logging.WARNING, logger="vesmaro.decision_provider"):
            decision = provider.evaluate(IsDuplicateRequest(), state)
    finally:
        restore()
    assert isinstance(decision, Noul)
    assert decision.probability == 1.0  # 0.93 >= 0.92 step rule
    assert any("CORTEX-E-INFER" in record.message for record in caplog.records)


def test_out_of_range_probability_degrades_instead_of_clipping(
    provider: VesmaProvider, caplog: pytest.LogCaptureFixture
) -> None:
    state = CanonState(
        record=CanonRecordView(title="t", body="b"),
        candidate=CanonRecordView(title="t", body="b"),
        similarity=0.5,
    )
    restore = _swap_session(provider, _OutOfRangeSession())
    try:
        with caplog.at_level(logging.WARNING, logger="vesmaro.decision_provider"):
            decision = provider.evaluate(IsDuplicateRequest(), state)
    finally:
        restore()
    assert isinstance(decision, Noul)
    assert decision.probability == 0.0  # 0.5 < 0.92 step rule — NOT a clipped 1.0
    assert any("CORTEX-E-INFER" in record.message for record in caplog.records)


def test_undecodable_output_tensor_degrades_not_escapes(
    provider: VesmaProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """The scalar decode sits under the CortexInferError guard (#459): a
    (1,)-shaped tensor with no decodable value fails the request the same
    typed way (never an escaping TypeError past the fail-open seam)."""
    state = CanonState(
        record=CanonRecordView(title="t", body="b"),
        candidate=CanonRecordView(title="t", body="b"),
        similarity=0.5,
    )
    restore = _swap_session(provider, _UndecodableSession())
    try:
        with caplog.at_level(logging.WARNING, logger="vesmaro.decision_provider"):
            decision = provider.evaluate(IsDuplicateRequest(), state)
    finally:
        restore()
    assert isinstance(decision, Noul)
    assert decision.probability == 0.0  # deterministic step rule
    assert any("CORTEX-E-INFER" in record.message for record in caplog.records)


def test_invalid_similarity_degrades_with_a_schema_warn(
    provider: VesmaProvider, caplog: pytest.LogCaptureFixture
) -> None:
    view = CanonRecordView(title="t", body="b")
    state = CanonState(record=view, candidate=view, similarity=float("nan"))
    with caplog.at_level(logging.WARNING, logger="vesmaro.decision_provider"):
        decision = provider.evaluate(IsDuplicateRequest(), state)
    assert isinstance(decision, Noul)
    # NaN similarity fails every threshold comparison → step verdict 0.0.
    assert decision.probability == 0.0
    assert any("CORTEX-E-SCHEMA" in record.message for record in caplog.records)


def test_degradation_warns_carry_the_contract_action_class(
    provider: VesmaProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """DP-08 (spec §3.8, review-2): a per-verdict degradation warn carries
    the implementation-namespace code AND the contractual action class —
    the parse check from the checklist's «Способ проверки» column."""
    state = CanonState(
        record=CanonRecordView(title="t", body="b"),
        candidate=CanonRecordView(title="t", body="b"),
        similarity=0.93,
    )
    restore = _swap_session(provider, _BoomSession())
    try:
        with caplog.at_level(logging.WARNING, logger="vesmaro.decision_provider"):
            provider.evaluate(IsDuplicateRequest(), state)
    finally:
        restore()
    degraded = [r.message for r in caplog.records if "CORTEX-E-INFER" in r.message]
    assert degraded
    assert all("code=" in line and "class=verdict-class" in line for line in degraded)


def test_provider_call_leaves_record_bytes_identical(provider: VesmaProvider) -> None:
    """DP-03 (checklist check): the prepared state's bytes survive the
    provider call — the artifact answers a verdict, records stay as-is."""
    record = CanonRecordView(title="t", body="body " * 100, tags=("project:p",))
    candidate = CanonRecordView(title="c", body="prior " * 100)
    before_record = asdict(record)
    before_candidate = asdict(candidate)
    provider.evaluate(
        IsDuplicateRequest(), CanonState(record=record, candidate=candidate, similarity=0.5)
    )
    assert asdict(record) == before_record
    assert asdict(candidate) == before_candidate


# ── Wiring: the flag, the factory, the load-time fail-open ────────────────────


def test_flag_accepts_vesma_and_stays_off_by_default() -> None:
    assert VesmaConfig().decision_provider == "deterministic"
    assert VesmaConfig(decision_provider="vesma").decision_provider == "vesma"


def test_resolver_wires_the_cortex_provider() -> None:
    wired = resolve_decision_provider(
        VesmaConfig(decision_provider="vesma"),
        embedder_fingerprint=config_fingerprint(EmbeddingConfig()),
    )
    assert isinstance(wired, VesmaProvider)


def test_resolver_fail_open_on_broken_artifact(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Brief W5d §5 acceptance: a broken artifact degrades to
    DeterministicProvider with a machine-parseable CORTEX-E-LOAD warn."""
    import vesmaro.decision_provider as dp

    monkeypatch.setattr(dp, "CORTEX_ARTIFACT_DIR", "no-such-bundle")
    with caplog.at_level(logging.WARNING, logger="vesmaro.decision_jev"):
        wired = resolve_decision_provider(
            VesmaConfig(decision_provider="vesma"),
            embedder_fingerprint=EMBEDDER_PIN,
        )
    assert isinstance(wired, DeterministicProvider)
    degraded = [r.message for r in caplog.records if "CORTEX-E" in r.message]
    assert degraded
    assert all("class=provider-class" in line for line in degraded)  # DP-08: whole-provider class


def test_resolver_pin_mismatch_telegraphs_recalibration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="vesmaro.decision_jev"):
        wired = resolve_decision_provider(
            VesmaConfig(decision_provider="vesma"),
            embedder_fingerprint="ollama:llama3",
        )
    assert isinstance(wired, DeterministicProvider)
    messages = " ".join(record.message for record in caplog.records)
    assert "CORTEX-E-PIN" in messages
    assert "recalibration" in messages
    # The recalibration line itself carries the machine-parseable token
    # (#459): a strict code=-prefix parser must catch exactly this line,
    # and the §3.8 action class marks the whole-provider degradation.
    recalibration = [r for r in caplog.records if "recalibration" in r.message]
    assert recalibration, "expected the recalibration telegraph warn"
    assert all(
        r.message.startswith("code=") and "class=provider-class" in r.message for r in recalibration
    )


def test_resolver_without_fingerprint_refuses_the_pin(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="vesmaro.decision_jev"):
        wired = resolve_decision_provider(VesmaConfig(decision_provider="vesma"))
    assert isinstance(wired, DeterministicProvider)
    refused = [r.message for r in caplog.records if "CORTEX-E-PIN" in r.message]
    assert refused
    assert all("class=provider-class" in line for line in refused)


# ── Integration: smoke verdict on REAL records, measured similarity ───────────


def test_smoke_verdict_on_real_records(provider: VesmaProvider, embedder: NanoProvider) -> None:
    """Real corpus records + the real embedder's measured cosine (the
    minting-flow shape).

    B1 RESTORE (weights ``beb0a65d…``, measured 2026-10-04): the shipped
    B2 revision (``71f0572d…``) was RECALLED (investigation verdict #480 —
    ranking inversion on identity pairs: a record graded BELOW the cut
    against ITSELF) and the bundle swapped back to the recalled-safe B1
    artifact — the vintage the prod cortex registry already runs. The
    #480 inversion is gone: the self-pair grades ~1.0, ABOVE the cut, and
    the verdict ordering is monotone in cosine (self > family > twin);
    the cortex-side adversarial sanity suite passes on the same weights
    (self-pair 1.0, near-boundary 1.0, unrelated 0.0, monotone ladder).
    Measured deviation from the W5d-era surface (weights ``281bd0fd…``)
    is pinned as-is, not hidden: the real-record family pair
    (checkpoint/report, cos ≈ 0.98) grades BELOW the cut (~0.016) — no
    inversion, but the interim B1 readout is sharper than W5d's; the
    retrain wave (#480) will re-address the family band.
    """
    checkpoint = _corpus_view("examples/after/checkpoint.json")
    decision = _corpus_view("examples/after/decision.json")
    report = _corpus_view("examples/after/report.json")

    # A record against itself (measured cosine 1.0): duplicate, graded
    # ~1.0 — the #480 inversion signature (B2 graded ~0.011) is gone.
    self_state = CanonState(
        record=checkpoint,
        candidate=checkpoint,
        similarity=_cosine(embedder, checkpoint, checkpoint),
    )
    self_verdict = provider.evaluate(IsDuplicateRequest(), self_state)
    assert isinstance(self_verdict, Noul)
    assert 0.0 < self_verdict.probability < 1.0  # graded, not a step
    assert self_verdict.probability >= CORTEX_DUPLICATE_PROBABILITY_THRESHOLD
    assert self_verdict.probability == pytest.approx(0.999992, rel=0.05)

    # Template twins (checkpoint/decision, cosine ≈ 0.86): below the cut —
    # the W4c lesson the artifact exists for (B1 grades ~7.5e-06).
    twin_state = CanonState(
        record=checkpoint,
        candidate=decision,
        similarity=_cosine(embedder, checkpoint, decision),
    )
    twin_verdict = provider.evaluate(IsDuplicateRequest(), twin_state)
    assert isinstance(twin_verdict, Noul)
    assert 0.0 < twin_verdict.probability < 1.0
    assert twin_verdict.probability < CORTEX_DUPLICATE_PROBABILITY_THRESHOLD
    assert twin_verdict.probability == pytest.approx(7.54e-06, rel=0.05)

    # Structurally similar family members (checkpoint/report, cosine
    # ≈ 0.98): B1 grades ~0.016 — BELOW the cut (W5d said duplicate).
    # Pinned as measured: the ordering stays monotone (no #480-style
    # inversion), the interim artifact is simply sharper than W5d's.
    family_state = CanonState(
        record=checkpoint,
        candidate=report,
        similarity=_cosine(embedder, checkpoint, report),
    )
    family_verdict = provider.evaluate(IsDuplicateRequest(), family_state)
    assert isinstance(family_verdict, Noul)
    assert 0.0 < family_verdict.probability < 1.0
    assert family_verdict.probability < CORTEX_DUPLICATE_PROBABILITY_THRESHOLD
    assert family_verdict.probability == pytest.approx(0.016314, rel=0.05)
