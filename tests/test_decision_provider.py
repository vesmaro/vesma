"""W4b — decision-provider seam: DeterministicProvider + calibration baseline.

Store card vesmaro-canon-w4b-calibration-baseline (vesmaro-canon
[ADR 0004](https://github.com/vesmaro/vesmaro-canon/blob/main/docs/decisions/0004-decision-provider.md)):
the interface dataclasses, the first provider (the engine's existing
heuristics formalized as-is) and the CORPUS BASELINE PIN — the frozen
result any future provider (vesma router, Jev adapter) must be measured
against under the pre-registered methodology
(``vesmaro-canon/docs/experiments/provider-calibration.md``).

Corpus pin protocol (the established drift-pin pattern, cf.
``test_canon_warn_validator._canon_repo_sections``): when the sibling
``vesmaro-canon`` checkout exists, the corpus is read FROM IT and every
file's sha256 must equal the frozen pin — a corpus edit FAILS here and
the baseline must be re-run (that is the point). Without the sibling
(CI), the committed snapshot ``tests/data/w4b_corpus_snapshot.json``
serves as the frozen-sha fallback, verified against the same pins.

The embeddings leg is the REAL bundled embedder (NanoProvider /
vesma-embed-v1, ADR-0021) — the same one the engine's vector leg uses;
the embedder fingerprint is pinned in the baseline artifact, and an
embedder swap (fingerprint change) is a re-baseline event by contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Final, cast

import pytest

from vesmaro.awareness import CONFLICT_HINT_MIN_SHARED_TOKENS
from vesmaro.decision_provider import (
    QUESTION_IS_DUPLICATE,
    RECORD_QUALITY_CONFIDENCE,
    RECORD_QUALITY_PLACEHOLDER,
    SPECTRUM_GOAL_OVERLAP,
    SPECTRUM_RECORD_QUALITY,
    CanonRecordView,
    CanonState,
    DeterministicProvider,
    GoalOverlapRequest,
    IsDuplicateRequest,
    MissingEvidenceError,
    NoFederateRecordError,
    Noul,
    RecordQualityRequest,
    Score,
    UnsupportedPrimitiveError,
    run_streaming_baseline,
)
from vesmaro.embeddings import NanoProvider
from vesmaro.manager import MemoryManager
from vesmaro.models import Memory

from ._canon_sibling import canon_sibling_repo as _canon_sibling_repo

# ── Frozen corpus pins (the canon repo owns the corpus; W4b freezes it) ──────

#: Fixed 21-record corpus: the canon example corpus — examples/after (4),
#: intake fixtures-positive (4), fixtures-negative (6), fixtures-edge (7) —
#: lexicographic within each directory. sha256 of the RAW file bytes.
FROZEN_CORPUS_SHA: Final[dict[str, str]] = {
    "examples/after/checkpoint.json": (
        "66d983b55f0c7fe50d690a5af73dc7a89289ac574c6e9389c72cb99c0ee02e39"
    ),
    "examples/after/decision.json": (
        "c412400419b24e38f0a114a7122fe62e276396652c48f8fddc31b6cfc71217ef"
    ),
    "examples/after/report.json": (
        "3dff06eccf939220cfa2ff91fc4c645481083a1c6ce70824484be49d448075ff"
    ),
    "examples/after/task.json": (
        "f0dc892ffca15c7c84868dec610c875bddddeb17f580bd55b5dff1879fd6dd18"
    ),
    "examples/intake/fixtures-positive/intake-checkpoint-active.json": (
        "fefc4318ddcbbad6a95c5837da24ddf7e49cffa19385e4fc69195b516fbd90db"
    ),
    "examples/intake/fixtures-positive/intake-decision-secret-gate.json": (
        "286e809408d9013451781d52675ccca2f0283f11f2570c8970853d8042e0f0fa"
    ),
    "examples/intake/fixtures-positive/intake-report-week39.json": (
        "9ce7a3a8516700af0f6430ddc9dd720a573fd43f8a8257d1a85b3be51bd1b486"
    ),
    "examples/intake/fixtures-positive/intake-task-draft.json": (
        "4e67f37f54ccfb68cafab35097919b0f0628ce758a46c9fefcfd5fa3d94b7b08"
    ),
    "examples/intake/fixtures-negative/bad-intake-e-date.json": (
        "a3c4d22b79a0df1edde0d9f95020bbe7d8f6090f9d3188cc302c268d9c17b355"
    ),
    "examples/intake/fixtures-negative/bad-intake-e-envelope.json": (
        "a8f172042b19a5328770a39e528678d5216a36aea02006c89b20641e24c22664"
    ),
    "examples/intake/fixtures-negative/bad-intake-e-language.json": (
        "79e11e04617cd5700d2772348c4c4bdd4710468dab6c6f72d7c4dfbb427aa8f8"
    ),
    "examples/intake/fixtures-negative/bad-intake-e-section.json": (
        "bc6c9877a2a657f181ee8dd82cdbe6216317223b6bd35fbaf5b8c14e82e18321"
    ),
    "examples/intake/fixtures-negative/bad-intake-e-status.json": (
        "9df6dbe310a5fed328f9b3f41a2efb93e520f760676546f86420935396af47ad"
    ),
    "examples/intake/fixtures-negative/bad-intake-e-title.json": (
        "7035d4dae300f9f1ff4261c166e385e1e40f5633a161c9f8c0f59163a3271858"
    ),
    "examples/intake/fixtures-edge/edge-date-relative-word.json": (
        "3ad6b5a588474eda8db7bc650748a68bf4000b8983a1f8c874b4b7412981f723"
    ),
    "examples/intake/fixtures-edge/edge-date-version-lookalike.json": (
        "a080fd40f2b2937ef103165cb51a1283242a1ee41c34971e57ab39e8a2ae39d7"
    ),
    "examples/intake/fixtures-edge/edge-placeholder-heavy.json": (
        "1d0a7fa7275cc5759ab7af2077a389b51b589f65b32c89f2047a7118edf61639"
    ),
    "examples/intake/fixtures-edge/edge-resolved-checkpoint.json": (
        "c7a3dc5ec443593bc33003199ce9dfb1fbf68aa81b5c8f426e78f2f940dca4c4"
    ),
    "examples/intake/fixtures-edge/edge-resolved-report.json": (
        "ca045fe5d1381ea2f96b938d3edc09ddcd61fe87ae410f776a87b90aa7ec6ac1"
    ),
    "examples/intake/fixtures-edge/edge-title-exactly-80.json": (
        "f983e003938cc59d70087eb10f0cbf2cd59a47972fdbd4c81fa2af02579ac4e7"
    ),
    "examples/intake/fixtures-edge/edge-title-multiline-crlf.json": (
        "7fe2a434a1ed4b110ee5e04c277d6a80e62bfeee1bc04c302e08cba68f106e34"
    ),
}

#: The frozen deterministic baseline: (corpus path, is-duplicate probability
#: or None when unaskable, candidate path, similarity rounded to 6 decimals).
#: Regenerating this table is a RE-BASELINE event (canon repo, the
#: pre-registered methodology), never a casual test update.
EXPECTED_BASELINE: Final[tuple[tuple[str, float | None, str | None, float | None], ...]] = (
    ("examples/after/checkpoint.json", None, None, None),
    ("examples/after/decision.json", 0.0, "examples/after/checkpoint.json", 0.860652),
    ("examples/after/report.json", 1.0, "examples/after/checkpoint.json", 0.979386),
    ("examples/after/task.json", 1.0, "examples/after/report.json", 0.956769),
    (
        "examples/intake/fixtures-positive/intake-checkpoint-active.json",
        1.0,
        "examples/after/task.json",
        0.931268,
    ),
    (
        "examples/intake/fixtures-positive/intake-decision-secret-gate.json",
        0.0,
        "examples/after/decision.json",
        0.913699,
    ),
    (
        "examples/intake/fixtures-positive/intake-report-week39.json",
        1.0,
        "examples/intake/fixtures-positive/intake-checkpoint-active.json",
        0.96872,
    ),
    (
        "examples/intake/fixtures-positive/intake-task-draft.json",
        1.0,
        "examples/intake/fixtures-positive/intake-checkpoint-active.json",
        0.963904,
    ),
    (
        "examples/intake/fixtures-negative/bad-intake-e-date.json",
        1.0,
        "examples/after/task.json",
        0.949769,
    ),
    (
        "examples/intake/fixtures-negative/bad-intake-e-envelope.json",
        1.0,
        "examples/intake/fixtures-positive/intake-report-week39.json",
        0.923853,
    ),
    (
        "examples/intake/fixtures-negative/bad-intake-e-language.json",
        0.0,
        "examples/intake/fixtures-positive/intake-task-draft.json",
        0.898726,
    ),
    (
        "examples/intake/fixtures-negative/bad-intake-e-section.json",
        1.0,
        "examples/intake/fixtures-positive/intake-report-week39.json",
        0.941945,
    ),
    (
        "examples/intake/fixtures-negative/bad-intake-e-status.json",
        1.0,
        "examples/intake/fixtures-negative/bad-intake-e-section.json",
        0.952403,
    ),
    (
        "examples/intake/fixtures-negative/bad-intake-e-title.json",
        1.0,
        "examples/intake/fixtures-negative/bad-intake-e-language.json",
        0.954565,
    ),
    (
        "examples/intake/fixtures-edge/edge-date-relative-word.json",
        1.0,
        "examples/intake/fixtures-negative/bad-intake-e-date.json",
        0.950373,
    ),
    (
        "examples/intake/fixtures-edge/edge-date-version-lookalike.json",
        1.0,
        "examples/after/task.json",
        0.946381,
    ),
    (
        "examples/intake/fixtures-edge/edge-placeholder-heavy.json",
        1.0,
        "examples/intake/fixtures-negative/bad-intake-e-section.json",
        0.941892,
    ),
    (
        "examples/intake/fixtures-edge/edge-resolved-checkpoint.json",
        1.0,
        "examples/intake/fixtures-negative/bad-intake-e-status.json",
        0.946729,
    ),
    (
        "examples/intake/fixtures-edge/edge-resolved-report.json",
        1.0,
        "examples/intake/fixtures-edge/edge-date-relative-word.json",
        0.951507,
    ),
    (
        "examples/intake/fixtures-edge/edge-title-exactly-80.json",
        1.0,
        "examples/intake/fixtures-negative/bad-intake-e-title.json",
        0.96157,
    ),
    (
        "examples/intake/fixtures-edge/edge-title-multiline-crlf.json",
        1.0,
        "examples/intake/fixtures-edge/edge-title-exactly-80.json",
        0.959406,
    ),
)

#: Rounded to 6 decimals in the pins — the comparison tolerance.
SIMILARITY_TOLERANCE: Final[float] = 1e-6

_SNAPSHOT_PATH: Final[Path] = Path(__file__).parent / "data" / "w4b_corpus_snapshot.json"


def _canon_root() -> Path | None:
    """Sibling canon checkout (same lookup as the S1/canon-warn drift pins).
    Worktree-safe (cascade QA P2-2 — shared helper, issue #433 class)."""
    return _canon_sibling_repo()


def _view_from_record(data: dict[str, Any]) -> CanonRecordView:
    """The corpus JSON shape (title/tags/metadata.canon/body) → view."""
    envelope = data.get("metadata", {}).get("canon")
    envelope = envelope if isinstance(envelope, dict) else {}
    return CanonRecordView(
        title=data.get("title") or "",
        body=data.get("body") or "",
        tags=tuple(data.get("tags") or ()),
        language=envelope.get("language"),
        record_type=envelope.get("type"),
    )


def _verify_pins(files: dict[str, bytes]) -> None:
    """sha256 drift gate — a corpus edit must fail loudly, not pass silently."""
    for path, sha in FROZEN_CORPUS_SHA.items():
        if path not in files:
            raise AssertionError(f"corpus drift: pinned file missing: {path}")
        actual = hashlib.sha256(files[path]).hexdigest()
        if actual != sha:
            raise AssertionError(
                f"corpus drift: {path} sha256 {actual} != frozen {sha} — "
                "the W4b baseline must be re-run (see "
                "vesmaro-canon/docs/experiments/provider-calibration.md)"
            )


def load_corpus() -> list[tuple[str, CanonRecordView]]:
    """The 21-record corpus: sibling checkout first, frozen snapshot fallback.

    Both sources carry the ORIGINAL file bytes and must pass the same
    sha256 pins; the snapshot self-check keeps the fallback honest in CI
    (where the sibling checkout does not exist).
    """
    root = _canon_root()
    raw_files: dict[str, bytes] = {}
    if root is not None:
        for path in FROZEN_CORPUS_SHA:
            file = root / path
            if not file.is_file():
                raise AssertionError(f"corpus drift: sibling file missing: {file}")
            raw_files[path] = file.read_bytes()
    else:
        snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        assert snapshot["schema"] == "w4b-corpus-snapshot/v1"
        for entry in snapshot["files"]:
            raw_files[entry["path"]] = entry["raw"].encode("utf-8")
    _verify_pins(raw_files)
    return [(path, _view_from_record(json.loads(raw_files[path]))) for path in FROZEN_CORPUS_SHA]


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def corpus() -> list[tuple[str, CanonRecordView]]:
    return load_corpus()


@pytest.fixture(scope="module")
def provider() -> DeterministicProvider:
    return DeterministicProvider()


# ── The baseline pin (green = baseline reproducible) ─────────────────────────


def test_baseline_reproducible_on_corpus(
    corpus: list[tuple[str, CanonRecordView]], provider: DeterministicProvider
) -> None:
    """The frozen W4b baseline reproduces byte-for-byte on this machine.

    Same corpus pins, same embedder leg (NanoProvider / vesma-embed-v1 —
    the engine's own vector-leg embedder), same streaming policy. A
    failure here means one of the pinned inputs moved: re-run the
    baseline under the pre-registered methodology and re-freeze BOTH
    the canon artifact and this table — never hand-edit one side.
    """
    embedder = NanoProvider()
    vectors = [embedder.embed(view.embedding_text()) for _, view in corpus]
    index = {id(view): i for i, (_, view) in enumerate(corpus)}

    def cosine(a: CanonRecordView, b: CanonRecordView) -> float:
        va, vb = vectors[index[id(a)]], vectors[index[id(b)]]
        dot = sum(x * y for x, y in zip(va, vb, strict=True))
        na = sum(x * x for x in va) ** 0.5
        nb = sum(x * x for x in vb) ** 0.5
        return dot / (na * nb)

    verdicts = run_streaming_baseline(corpus, provider, cosine)
    assert len(verdicts) == len(EXPECTED_BASELINE) == 21
    for verdict, expected in zip(verdicts, EXPECTED_BASELINE, strict=True):
        key, prob, candidate, similarity = expected
        assert verdict.key == key
        actual_prob = None if verdict.is_duplicate is None else verdict.is_duplicate.probability
        assert actual_prob == prob, f"{key}: is-duplicate drifted: {actual_prob} != {prob}"
        assert verdict.is_duplicate_candidate == candidate, f"{key}: candidate drifted"
        actual_sim = None if verdict.similarity is None else round(verdict.similarity, 6)
        if similarity is None:
            assert actual_sim is None, f"{key}: unexpected similarity {actual_sim}"
        else:
            assert actual_sim is not None, f"{key}: similarity vanished"
            assert abs(actual_sim - similarity) <= SIMILARITY_TOLERANCE, (
                f"{key}: similarity drifted: {actual_sim} != {similarity}"
            )
        assert verdict.record_quality.value == RECORD_QUALITY_PLACEHOLDER
        assert verdict.record_quality.confidence == RECORD_QUALITY_CONFIDENCE


def test_baseline_distribution_matches_frozen_summary() -> None:
    """17 positives / 3 negatives / 1 unaskable — the corpus property that
    dominates the baseline (intake fixture siblings are deliberate
    near-copies, one broken field apart) is pinned here so a corpus
    reshuffle cannot quietly change the acceptance denominator."""
    positives = sum(1 for _, prob, _, _ in EXPECTED_BASELINE if prob == 1.0)
    negatives = sum(1 for _, prob, _, _ in EXPECTED_BASELINE if prob == 0.0)
    unaskable = sum(1 for _, prob, _, _ in EXPECTED_BASELINE if prob is None)
    assert (positives, negatives, unaskable) == (17, 3, 1)


# ── Primitive semantics (happy path / failure mode / boundary) ───────────────


def test_record_quality_is_the_honest_placeholder(
    corpus: list[tuple[str, CanonRecordView]], provider: DeterministicProvider
) -> None:
    """«record-quality» returns the synthesis placeholder constant with the
    maximum-entropy confidence — for EVERY record (it carries no record-
    specific signal; that is the documented honesty, not a bug)."""
    for key, view in corpus:
        decision = provider.evaluate(RecordQualityRequest(), CanonState(record=view))
        assert decision == Score(
            spectrum=SPECTRUM_RECORD_QUALITY,
            value=RECORD_QUALITY_PLACEHOLDER,
            confidence=RECORD_QUALITY_CONFIDENCE,
        ), key


def test_is_duplicate_threshold_boundary(provider: DeterministicProvider) -> None:
    """The engine's near-duplicate rule, as-is: cosine >= threshold → yes
    (the minting leg qualifies AT the threshold — boundary pinned)."""
    a = CanonRecordView(title="T", body="deploy the release gate")
    b = CanonRecordView(title="T2", body="deploy the release pipeline")
    at_threshold = provider.evaluate(
        IsDuplicateRequest(), CanonState(record=a, candidate=b, similarity=0.92)
    )
    assert at_threshold == Noul(question=QUESTION_IS_DUPLICATE, probability=1.0)
    below = provider.evaluate(
        IsDuplicateRequest(), CanonState(record=a, candidate=b, similarity=0.919999)
    )
    assert below.probability == 0.0


def test_is_duplicate_without_evidence_raises(provider: DeterministicProvider) -> None:
    """No measured similarity (or no candidate) → typed refusal, never a
    fabricated verdict (the provider does not embed or fetch)."""
    a = CanonRecordView(title="T", body="body")
    with pytest.raises(MissingEvidenceError):
        provider.evaluate(IsDuplicateRequest(), CanonState(record=a))
    with pytest.raises(MissingEvidenceError):
        provider.evaluate(IsDuplicateRequest(), CanonState(record=a, candidate=a))


def test_choice_is_declined_not_fabricated(
    corpus: list[tuple[str, CanonRecordView]], provider: DeterministicProvider
) -> None:
    """No engine heuristic maps to a Choice — the provider refuses with a
    typed error instead of inventing a verdict (ADR-0004 rule 4)."""
    request = cast(Any, object())  # not a known request class
    with pytest.raises(UnsupportedPrimitiveError):
        provider.evaluate(request, CanonState(record=corpus[0][1]))


def test_goal_overlap_maps_awareness_public_signal(provider: DeterministicProvider) -> None:
    """«goal-overlap» rides awareness' public conflict_hints (zero tokenizer
    drift): a fired hint reports its shared-token count; a sub-threshold
    pair reports 0.0 — the engine-visible signal, never an invented raw
    overlap meter (the module docstring's honesty note)."""
    mine = CanonRecordView(title="", body="ship the release gate checklist")
    fired = CanonRecordView(title="", body="hold the release gate rollout")
    score = provider.evaluate(GoalOverlapRequest(), CanonState(record=mine, candidate=fired))
    assert isinstance(score, Score)
    assert score.spectrum == SPECTRUM_GOAL_OVERLAP
    assert score.confidence == 1.0
    assert score.value >= CONFLICT_HINT_MIN_SHARED_TOKENS  # the hint fired

    single = CanonRecordView(title="", body="ship the docs later")
    quiet = provider.evaluate(GoalOverlapRequest(), CanonState(record=mine, candidate=single))
    assert quiet.value == 0.0  # sub-threshold: engine-visible signal is "no hint"

    with pytest.raises(MissingEvidenceError):
        provider.evaluate(GoalOverlapRequest(), CanonState(record=mine))


# ── Prepared-view boundary (ADR-0004 rule 2) ─────────────────────────────────


def test_view_from_memory_projects_envelope_only() -> None:
    """from_memory carries envelope + body + title — and nothing else: store
    fields (quality scores, embedding ids) never cross the provider seam."""
    memory = Memory(
        content="## Goals\n- ship\n\n## Context\nsecret store row fields",
        title="checkpoint",
        tags=["project:p", "agent:a", "mnemos:checkpoint"],
        quality_score=0.99,
        confidence=0.99,
        embedding_id="vec-123",
        metadata={"canon": {"schema_version": "1", "type": "checkpoint", "language": "en"}},
    )
    view = CanonRecordView.from_memory(memory)
    assert view.title == "checkpoint"
    assert view.body == memory.effective_content()
    assert view.tags == ("project:p", "agent:a", "mnemos:checkpoint")
    assert view.language == "en"
    assert view.record_type == "checkpoint"
    dumped = json.dumps(view.__dict__)
    assert "quality_score" not in dumped and "embedding_id" not in dumped


def test_no_federate_record_never_enters_prepared_state() -> None:
    """DP-05 first line (spec §3.5, review-2): a ``no-federate`` record is
    excluded BEFORE state assembly — the seam refuses the projection, so
    a tagged row can never ride into a provider request (the tag binds
    regardless of provider locality)."""
    memory = Memory(
        content="internal only — never federate",
        title="t",
        tags=["project:p", "agent:a", "mnemos:no-federate"],
    )
    with pytest.raises(NoFederateRecordError):
        CanonRecordView.from_memory(memory)


def test_provider_call_leaves_record_bytes_identical(
    provider: DeterministicProvider,
) -> None:
    """DP-03 (checklist check): the record body is byte-identical before
    and after the provider calls — the provider answers verdicts, it
    never rewrites records."""
    view = CanonRecordView(title="checkpoint", body="body " * 100, tags=("project:p",))
    before = asdict(view)
    provider.evaluate(RecordQualityRequest(), CanonState(record=view))
    provider.evaluate(GoalOverlapRequest(), CanonState(record=view, candidate=view))
    assert asdict(view) == before


def test_every_answer_emits_machine_parseable_telemetry(
    provider: DeterministicProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """DP-09 (spec §3.9): every answer logs ``decision primitive=…
    provider=… question=…`` — «who took this decision» is answerable
    from the log alone, no code access, no record content in the line."""
    view = CanonRecordView(title="t", body="b")
    with caplog.at_level(logging.INFO, logger="vesmaro.decision_provider"):
        provider.evaluate(RecordQualityRequest(), CanonState(record=view))
        provider.evaluate(
            IsDuplicateRequest(), CanonState(record=view, candidate=view, similarity=0.99)
        )
    lines = [r.message for r in caplog.records if r.message.startswith("decision ")]
    assert any(
        "primitive=score" in line
        and "provider=deterministic" in line
        and "question=record-quality" in line
        and "confidence=0.5" in line
        for line in lines
    )
    assert any(
        "primitive=noul" in line and "question=is-duplicate" in line and "probability=1.0" in line
        for line in lines
    )


@pytest.mark.parametrize(
    ("title", "body"),
    [
        pytest.param("the title", "body text", id="short"),
        pytest.param(None, "body text", id="no-title"),
        pytest.param("t", "x" * 5000, id="over-cap"),
    ],
)
def test_view_embedding_text_mirrors_engine_composition(title: str | None, body: str) -> None:
    """The view's embedding text is byte-identical to the engine's
    ``MemoryManager._embedding_text`` — the calibration cosine is measured
    over the SAME text the vector leg embeds. Parametrized over short,
    title-less and over-cap bodies (cascade QA P3-2: a cap/shape change in
    the manager must break this pin LOUDLY, not silently)."""
    memory = Memory(
        content=body,
        title=title,
        tags=["project:p", "agent:a"],
    )
    view = CanonRecordView.from_memory(memory)
    assert view.embedding_text() == MemoryManager._embedding_text(memory)


def test_view_embedding_text_cap_matches_engine() -> None:
    """The 4096-char cap of the engine's embedding text applies to the view
    (the engine truncates; the calibration leg must truncate identically)."""
    view = CanonRecordView(title="t", body="x" * 5000)
    assert len(view.embedding_text()) == 4096


def test_corpus_snapshot_fallback_is_pin_honest() -> None:
    """The committed snapshot (CI fallback) verifies against the same frozen
    pins — the fallback can never go quietly stale."""
    snapshot = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert snapshot["schema"] == "w4b-corpus-snapshot/v1"
    by_path = {entry["path"]: entry for entry in snapshot["files"]}
    assert set(by_path) == set(FROZEN_CORPUS_SHA)
    for path, sha in FROZEN_CORPUS_SHA.items():
        assert by_path[path]["sha256"] == sha, f"snapshot drift: {path}"


def test_corpus_drift_gate_fires_on_tampered_bytes() -> None:
    """A corpus edit must FAIL the pin check with a re-baseline instruction —
    the drift gate is the whole point of the frozen-sha protocol."""
    tampered = dict.fromkeys(FROZEN_CORPUS_SHA, b"tampered")
    with pytest.raises(AssertionError, match=r"corpus drift.*re-run"):
        _verify_pins(tampered)
