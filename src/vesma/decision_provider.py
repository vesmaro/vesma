"""ADR-0004 decision-provider seam — the typed «semantic if» interface.

The FIRST implementation of the interface ratified in vesma-canon
[ADR 0004](https://github.com/vesmaro/vesma-canon/blob/main/docs/decisions/0004-decision-provider.md):
a decision provider answers TYPED questions (``Choice`` / ``Score`` /
``Noul``) over PREPARED canon state, never raw memory dumps. Three
implementations are planned on this one interface — deterministic
engine heuristics (this module, now), a local vesma router (goal,
local-first) and an opt-in external Jev adapter (owner flag + key,
off by default forever). The local router leg landed as the bundled
``vesma-cortex-v1`` artifact (W5d, :class:`VesmaProvider` below).

W4b SCOPE GUARD — seam only, zero wiring. This module is the contract
plus its first provider. NO production call site routes a decision
through it yet (the wire-in is a later-wave decision; grep-diff proof
ships with the W4b report). Nothing here mutates store state: the
module is read-only over its inputs.

── Rules inherited from the ADR (binding here) ──────────────────────

1. **Policy lives in product code, not in the provider** — the
   questions, the ``Choice`` option sets and the ``Score`` scales are
   fixed by the engine (the request dataclasses below). A provider
   answers the question it is given; it never invents what to ask.
   Swapping providers must not change policy.
2. **Input is prepared canon state only** (:class:`CanonRecordView` —
   envelope fields + body + title). Raw memory dumps are never passed:
   they degrade accuracy (context rot), open an instruction-injection
   surface and would dominate transfer cost. ``from_memory`` projects a
   :class:`~vesma.models.Memory` onto the view; store-only fields
   (quality scores, traces, embedding ids) stay behind the boundary.
3. **Privacy gate is universal.** This seam ADDS no outbound channel:
   the deterministic provider performs no I/O at all. Any future
   provider with an outbound leg (Jev only, by the ADR) must run the
   engine's secret/danger detectors over the state BEFORE the call and
   abort the whole call on any finding — enforced at that provider's
   level, never negotiated per call site.
4. **Calibration before trust.** No implementation participates in
   product decisions until calibrated against the deterministic
   baseline under the pre-registered methodology (canon repo,
   ``docs/experiments/provider-calibration.md``). The baseline runner
   (:func:`run_streaming_baseline`) is the shared instrument every
   future provider is measured with — same runner, same corpus policy.

── Confidence semantics (honesty contract) ──────────────────────────

The deterministic provider does not predict — it FORMALIZES existing
engine heuristics. Its confidence fields therefore carry two distinct,
honestly documented meanings:

* *Measured computation* (goal-overlap, the is-duplicate threshold
  verdict): the answer is a pure function of the inputs, so confidence
  1.0 means "this is exactly what the engine's rule computes" —
  certainty about the computation, not about the world.
* *Placeholder estimate* (record-quality): the engine's synthesis
  pipeline assigns a hardcoded quality constant
  (:data:`RECORD_QUALITY_PLACEHOLDER`, awaiting a real signal). The
  honest confidence is the maximum-entropy 0.5 — the ADR's rule:
  the confidence of an uncalibrated provider is decoration, and the
  placeholder's own honesty level is the pre-registered bound any
  future provider must beat.

The is-duplicate ``Noul.probability`` is the THRESHOLD VERDICT
({0.0, 1.0}) of the existing near-duplicate rule — a step function of
the measured cosine, not a graded likelihood. The continuous cosine
rides :attr:`CanonState.similarity` as evidence; grading it into a
probability would be a new heuristic, which this slice refuses to
invent (formalize as-is, ADR implementation (a)).
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any, Final, Protocol, cast, runtime_checkable

import numpy as np

from vesma.awareness import conflict_hints
from vesma.graph_minting import AUTO_DEDUPE_SIMILARITY_THRESHOLD
from vesma.models import Memory

logger = logging.getLogger(__name__)

# ── Primitive decisions (ADR-0004: the three answers) ────────────────────────


@dataclass(frozen=True)
class Choice:
    """Pick one option from a product-fixed option set.

    No engine heuristic maps to a ``Choice`` today (candidate selection
    at deduplication is deliberately NOT one: similarity does not prove
    replacement, ADR-0030). The deterministic provider therefore
    declines every ``Choice`` question with a typed error instead of
    fabricating a verdict.
    """

    options: tuple[str, ...]
    verdict: str
    confidence: float


@dataclass(frozen=True)
class Score:
    """A value on an ordered, product-fixed spectrum."""

    spectrum: str
    value: float
    confidence: float


@dataclass(frozen=True)
class Noul:
    """A calibrated yes/no: ``probability`` is P(answer = yes)."""

    question: str
    probability: float


DecisionPrimitive = Choice | Score | Noul

# ── Typed errors (explicit refusals — the provider never fabricates) ─────────


class UnsupportedPrimitiveError(ValueError):
    """The provider has no heuristic for this question class.

    Raised, not defaulted: a fabricated answer would poison the
    calibration baseline this seam exists to produce.
    """


class MissingEvidenceError(ValueError):
    """The question is known but the state lacks the measured evidence.

    E.g. an is-duplicate request without the retrieval leg's cosine in
    :attr:`CanonState.similarity` — the provider does not embed, fetch
    or guess; measurement belongs to the caller's retrieval machinery.
    """


# ── Prepared canon state (ADR-0004 rule 2 — never raw dumps) ─────────────────


@dataclass(frozen=True)
class CanonRecordView:
    """Prepared canon record view: envelope fields + body + title.

    This is the ENTIRE input surface a provider may see. Projections
    from a store row go through :meth:`from_memory` and stop here —
    raw content variants, quality scores, traces and embedding ids are
    deliberately not carried.
    """

    title: str
    body: str
    tags: tuple[str, ...] = ()
    language: str | None = None
    record_type: str | None = None

    def embedding_text(self) -> str:
        """The engine's embedding-text shape over the view's fields.

        Mirrors ``MemoryManager._embedding_text`` (title + effective
        content + tags, joined by newlines, capped at
        :data:`EMBEDDING_TEXT_MAX_CHARS`) so calibration similarity is
        measured over the SAME text the vector leg embeds. Kept in sync
        manually with that method; the drift is pinned by the W4b
        baseline test (a shape change there changes this slice's
        corpus cosines).
        """
        parts: list[str] = []
        if self.title:
            parts.append(self.title)
        parts.append(self.body)
        if self.tags:
            parts.append(" ".join(self.tags))
        return "\n".join(parts)[:EMBEDDING_TEXT_MAX_CHARS]

    @classmethod
    def from_memory(cls, memory: Memory) -> CanonRecordView:
        """Project a store row onto the prepared view (ADR-0004 rule 2).

        The envelope is read from ``metadata.canon`` when present; a
        pre-canon row yields ``language=None`` / ``record_type=None``
        — the view stays honest about what the envelope does not say.
        """
        canon = memory.metadata.get("canon")
        envelope = canon if isinstance(canon, dict) else None
        return cls(
            title=memory.title or "",
            body=memory.effective_content(),
            tags=tuple(memory.tags),
            language=envelope.get("language") if envelope else None,
            record_type=envelope.get("type") if envelope else None,
        )


#: Cap applied by ``MemoryManager._embedding_text`` — reproduced so the
#: view's embedding text is byte-identical to the engine's.
EMBEDDING_TEXT_MAX_CHARS: Final[int] = 4096


@dataclass(frozen=True)
class CanonState:
    """The state a provider evaluates one request against.

    ``record`` is the subject record. ``candidate`` carries the second
    record for pairwise questions. ``similarity`` carries MEASURED
    evidence for pairwise questions — on the engine's minting path this
    is the vector-leg cosine the retrieval produced; providers consume
    it, they never re-measure.
    """

    record: CanonRecordView
    candidate: CanonRecordView | None = None
    similarity: float | None = None


# ── Requests (policy side: product-fixed questions, ADR-0004 rule 1) ─────────


@dataclass(frozen=True)
class RecordQualityRequest:
    """Score «record-quality» — the canon quality score of the record."""


@dataclass(frozen=True)
class IsDuplicateRequest:
    """Noul «is-duplicate» — is ``state.record`` a near-duplicate of the
    candidate under the engine's existing threshold?"""

    threshold: float = AUTO_DEDUPE_SIMILARITY_THRESHOLD


@dataclass(frozen=True)
class GoalOverlapRequest:
    """Score «goal-overlap» — lexical overlap between two prepared goal
    texts (``state.record.body`` = my goal, ``state.candidate.body`` =
    the neighbor's goal; extracting checkpoint goal lines stays the
    caller's product policy, e.g. awareness' ``checkpoint_goal_title``)."""


DecisionRequest = RecordQualityRequest | IsDuplicateRequest | GoalOverlapRequest

#: Spectrum/question identifiers (machine-parseable, telemetry-stable).
SPECTRUM_RECORD_QUALITY: Final[str] = "record-quality"
QUESTION_IS_DUPLICATE: Final[str] = "is-duplicate"
SPECTRUM_GOAL_OVERLAP: Final[str] = "goal-overlap"

# ── Provider protocol (one interface, ADR-0004) ──────────────────────────────


@runtime_checkable
class DecisionProvider(Protocol):
    """The «semantic if»: prepared state in, typed decision out."""

    #: Implementation identity for telemetry (primitive, implementation,
    #: confidence per answer — ADR-0004 Consequences). Read-only by
    #: contract: an identity is assigned by the implementation, never
    #: rewritten by a call site (W4c — property form; structural type
    #: refinement only, runtime isinstance semantics unchanged).
    @property
    def name(self) -> str: ...

    def evaluate(self, request: DecisionRequest, state: CanonState) -> DecisionPrimitive: ...


# ── Deterministic provider (ADR-0004 implementation (a) — now) ───────────────


#: The engine's quality placeholder (``pipeline/synthesize.py`` assigns
#: ``quality_score = 0.5`` to every synthesized record until a real LLM
#: provider is wired). Formalized AS-IS with the honest docstring both
#: places carry: this is a placeholder awaiting a real signal, kept in
#: sync manually — the W4b baseline pins its value.
RECORD_QUALITY_PLACEHOLDER: Final[float] = 0.5

#: The placeholder's honest confidence: the maximum-entropy level. The
#: constant carries no signal about any specific record, so claiming
#: more than 0.5 would be decoration (ADR-0004 rule 4). This is ALSO the
#: pre-registered Brier bound: a constant-p=0.5 predictor scores Brier
#: 0.25 on any outcome — the bar every calibrated provider must beat.
RECORD_QUALITY_CONFIDENCE: Final[float] = 0.5


class DeterministicProvider:
    """The engine's existing heuristics behind the ADR-0004 interface.

    Formalization, not invention: each answer reuses an EXISTING engine
    signal with its existing constants —

    * «record-quality» → the synthesis placeholder constant
      (:data:`RECORD_QUALITY_PLACEHOLDER`);
    * «is-duplicate» → the auto-dedupe near-duplicate threshold
      (:data:`vesma.graph_minting.AUTO_DEDUPE_SIMILARITY_THRESHOLD`,
      0.92 vector-leg cosine) as a step verdict over the MEASURED
      similarity the caller supplies;
    * «goal-overlap» → awareness' public ``conflict_hints`` lexical
      overlap (its ≥2-shared-token firing rule stays product policy in
      awareness, exactly where it is today).

    Deterministic by construction: same inputs → same decisions, zero
    I/O, zero external calls. It declines ``Choice`` questions with
    :class:`UnsupportedPrimitiveError` — no engine heuristic maps to
    one, and a fabricated verdict would corrupt the baseline.
    """

    name: Final[str] = "deterministic"

    def evaluate(self, request: DecisionRequest, state: CanonState) -> DecisionPrimitive:
        """Answer one product-fixed question over the prepared state."""
        if isinstance(request, RecordQualityRequest):
            return Score(
                spectrum=SPECTRUM_RECORD_QUALITY,
                value=RECORD_QUALITY_PLACEHOLDER,
                confidence=RECORD_QUALITY_CONFIDENCE,
            )
        if isinstance(request, IsDuplicateRequest):
            return Noul(
                question=QUESTION_IS_DUPLICATE,
                probability=self._is_duplicate_probability(state, request.threshold),
            )
        if isinstance(request, GoalOverlapRequest):
            return self._goal_overlap_score(state)
        raise UnsupportedPrimitiveError(f"no deterministic heuristic for {type(request).__name__}")

    @staticmethod
    def _is_duplicate_probability(state: CanonState, threshold: float) -> float:
        """The existing near-duplicate rule: cosine >= threshold → yes.

        Step verdict by design (see the module docstring): the engine's
        minting leg qualifies or rejects, it never grades. Evidence
        must be measured by the caller's retrieval leg — a state
        without ``similarity``/``candidate`` is unanswerable and raises.
        """
        if state.candidate is None or state.similarity is None:
            raise MissingEvidenceError(
                "is-duplicate requires measured evidence: state.candidate and "
                "state.similarity (vector-leg cosine) must be provided"
            )
        return 1.0 if state.similarity >= threshold else 0.0

    @staticmethod
    def _goal_overlap_score(state: CanonState) -> Score:
        """Map awareness' conflict-hint signal onto the Score primitive.

        Uses the PUBLIC ``conflict_hints`` — zero tokenizer drift with
        awareness. Honesty note (the mapping's one asymmetry): the
        engine only renders hints at/above its firing threshold, so
        sub-threshold overlaps are invisible to it by construction; this
        Score reports the ENGINE-VISIBLE signal (shared-token count for
        a fired hint, 0.0 otherwise) — values strictly between 0 and the
        firing threshold are unobservable, and this provider refuses to
        invent them. Confidence 1.0 = certainty about a deterministic
        computation (module docstring, honesty contract).
        """
        if state.candidate is None:
            raise MissingEvidenceError(
                "goal-overlap is pairwise: state.candidate (the neighbor goal) is required"
            )
        hints = conflict_hints(
            state.record.body,
            {"agents": [{"agent": "candidate", "goal_title": state.candidate.body}]},
        )
        value = float(len(hints[0]["shared_tokens"])) if hints else 0.0
        return Score(spectrum=SPECTRUM_GOAL_OVERLAP, value=value, confidence=1.0)


# ── VesmaProvider (W5d — the bundled vesma-cortex-v1 artifact) ────────────────

#: Bundled cortex artifact directory under ``vesma/models/`` — ships in
#: the wheel by the same mechanism as vesma-embed-v1 (package data inside
#: the ``vesma`` package directory; hatchling includes it with the
#: package, no force-include entry needed). Loaded via
#: ``importlib.resources`` per the ``_mnema_artifact_dir`` pattern.
CORTEX_ARTIFACT_DIR: Final[str] = "vesma-cortex-v1"

#: ONNX ``metadata_props.name`` — the model identity WITHOUT a version
#: suffix (inference-v1.md §4); the schema version rides its own key.
CORTEX_MODEL_NAME: Final[str] = "vesma-cortex"

#: Size gate: one self-contained ``model.onnx`` ≤ 5 MB (inference-v1.md
#: §4, ``MAX_ARTIFACT_BYTES``). The bundled v1 artifact is ~96 KB.
CORTEX_MAX_ARTIFACT_BYTES: Final[int] = 5 * 1024 * 1024

#: The frozen ORDERED feature contract of the v1 graph — field-for-field
#: mirror of ``src/cortex/features/pair.py::FEATURE_NAMES`` (cortex repo,
#: A3a freeze). Kept in sync manually: drift is caught loudly by the
#: ``feature_set_sha256`` assert at load, because a silent reorder would
#: permute the graph input (spec §4 «Feature-вход графа»). Field cosines
#: are deliberately NOT here — they are gated OFF for v1 (brief W5d §2.3).
CORTEX_FEATURE_NAMES: Final[tuple[str, ...]] = (
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

#: W5d application policy (spec §8 «Порог применения вердикта выбирает
#: W5d, не артефакт»): the cut that turns the artifact's CALIBRATED
#: probability into a duplicate verdict lives in product code — never in
#: artifact metadata. 0.5 is the pre-registered scoring cut the ADOPT
#: verdict was measured under (sealed holdout 300: sensitivity 1.0,
#: specificity 0.98). A future minting call site applies it to
#: ``Noul.probability`` exactly the way the deterministic leg applies
#: :data:`AUTO_DEDUPE_SIMILARITY_THRESHOLD` to the measured cosine.
CORTEX_DUPLICATE_PROBABILITY_THRESHOLD: Final[float] = 0.5

#: Dtype-agnostic ndarray alias for ORT graph edges — numpy stubs are
#: import-skipped (pyproject mypy overrides), same rationale as
#: ``vesma.embeddings._OrtTensor``.
_OrtTensor = np.ndarray[Any, Any]


class CortexError(RuntimeError):
    """Base of the vesma-cortex refusals (inference-v1.md §7).

    ``code`` is the machine-parseable latin ``CORTEX-E-*`` string the
    fail-open warn carries; the message STARTS with the code so a log
    line is parseable by prefix alone. Fail-open semantics (spec §7):
    the wiring (:func:`vesma.decision_jev.resolve_decision_provider`)
    catches every ``CortexError``, warns with the code and degrades to
    :class:`DeterministicProvider` — the engine path is never blocked.
    """

    code: str = "CORTEX-E-LOAD"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")
        self.detail = detail


class CortexSizeError(CortexError):
    """The artifact file exceeds the 5 MB self-containment gate."""

    code = "CORTEX-E-SIZE"


class CortexMetaError(CortexError):
    """metadata_props missing/invalid: wrong name, incompatible major,
    or a failed ``feature_set_sha256`` (wrapper/graph feature drift)."""

    code = "CORTEX-E-META"


class CortexPinError(CortexError):
    """``embedder_pin`` ≠ the live embedder fingerprint.

    This is a RECALIBRATION EVENT, not routine degradation (spec §7): the
    artifact was calibrated on one embedding geometry and cannot silently
    run on another. The wiring degrades to deterministic AND telegraphs
    the recalibration event in addition to the standard fail-open warn.
    """

    code = "CORTEX-E-PIN"


class CortexSchemaError(CortexError):
    """The pair state violates the canonical input schema at request time
    (e.g. a similarity that is NaN or outside [-1, 1])."""

    code = "CORTEX-E-SCHEMA"


class CortexInferError(CortexError):
    """The ORT run failed, the output did not decode, or the probability
    fell outside [0, 1] (an error — never clipped, spec §5)."""

    code = "CORTEX-E-INFER"


def cortex_feature_set_sha256() -> str:
    """sha256 over the canonical ``\\n``-joined feature-name string.

    The exact digest pinned in the artifact's ``feature_set_sha256``
    metadata prop (spec §4): ``sha256("\\n".join(CORTEX_FEATURE_NAMES))``.
    """
    return hashlib.sha256("\n".join(CORTEX_FEATURE_NAMES).encode("utf-8")).hexdigest()


def validate_cortex_metadata_props(props: Mapping[str, str]) -> None:
    """Spec §6 steps 2 + 8 — identity, schema major, feature contract.

    Raises :class:`CortexMetaError` on: ``name`` ≠ ``vesma-cortex``; a
    ``version`` whose MAJOR part is not ``1`` (weights refreshes inside
    v1 bump ``1.<n>`` — compatible; v2 is a new bundle directory, spec
    §8); a ``features`` list that is not the wrapper's frozen string; or
    a ``feature_set_sha256`` mismatch (the wrapper/graph drift assert).
    """
    name = props.get("name", "")
    if name != CORTEX_MODEL_NAME:
        raise CortexMetaError(f"metadata name {name!r} != {CORTEX_MODEL_NAME!r}")
    version = props.get("version", "")
    major = version.split(".")[0].strip()
    if major != "1":
        raise CortexMetaError(f"metadata version {version!r} is not major-1 compatible")
    features = props.get("features", "")
    expected_features = "\n".join(CORTEX_FEATURE_NAMES)
    if features != expected_features:
        raise CortexMetaError(
            f"feature contract drift: metadata 'features' != wrapper CORTEX_FEATURE_NAMES "
            f"(got {len(features.splitlines())} names, wrapper pins {len(CORTEX_FEATURE_NAMES)})"
        )
    pinned_sha = props.get("feature_set_sha256", "")
    if pinned_sha != cortex_feature_set_sha256():
        raise CortexMetaError(
            f"feature_set_sha256 mismatch: artifact {pinned_sha[:12]!r}… != "
            f"wrapper {cortex_feature_set_sha256()[:12]!r}… (wrapper/graph desync)"
        )


def assert_cortex_embedder_pin(pin: str, live_fingerprint: str) -> None:
    """Spec §6 step 5 — the artifact pin vs the LIVE embedder vintage.

    The caller supplies the live fingerprint (engine wiring uses
    ``vesma.embeddings.config_fingerprint``, the doctor-side twin of
    ``EmbeddingProvider.fingerprint`` — same string without building a
    session). A mismatch is the loud recalibration refusal
    :class:`CortexPinError`, never a graceful degradation by itself.
    """
    if pin != live_fingerprint:
        raise CortexPinError(
            "embedder_pin mismatch — recalibration event, the cortex artifact "
            f"was calibrated on {pin!r} but the live embedder fingerprint is "
            f"{live_fingerprint!r}"
        )


def _cortex_normalized_text(view: CanonRecordView) -> str:
    """Letter-normalized pair text: title + body, lower, ws-collapsed.

    Byte-for-byte mirror of ``cortex.features.pair._normalized_text`` —
    punctuation is deliberately NOT stripped (punctuation-only edits are
    weak-positive corruption transforms and must stay visible to the
    features); tags are NOT part of the n-gram text (they have their own
    feature). Any change here is a feature-contract drift caught by the
    calibration pins, not by tests alone.
    """
    return " ".join(f"{view.title}\n{view.body}".split()).lower()


def _cortex_ngrams(text: str, n: int) -> frozenset[str]:
    """Character n-gram SET (unicode chars, no tokenizer) of order ``n``."""
    if len(text) < n:
        return frozenset()
    return frozenset(text[i : i + n] for i in range(len(text) - n + 1))


def _cortex_jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Intersection size over union size; two empty sets are identical → 1.0."""
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _cortex_containment(a: frozenset[str], b: frozenset[str]) -> float:
    """Intersection size over min(|A|,|B|); both empty → 1.0, one empty → 0.0."""
    if not a and not b:
        return 1.0
    denominator = min(len(a), len(b))
    if denominator == 0:
        return 0.0
    return len(a & b) / denominator


def _cortex_match(value_a: str | None, value_b: str | None) -> float:
    """1.0 iff both sides carry the SAME non-null value, else 0.0."""
    if value_a is None or value_b is None:
        return 0.0
    return 1.0 if value_a == value_b else 0.0


def cortex_pair_features(
    record: CanonRecordView, candidate: CanonRecordView, similarity: float
) -> tuple[float, ...]:
    """Compute the 13 frozen core features — pure python, deterministic,
    zero network (spec §4: features are computed BEFORE the graph, in the
    engine wrapper; the artifact never sees raw text).

    Mirror of ``cortex.features.pair.features(field_cosines=False)``.
    ``record`` is the earlier record (canonical pair JSON side ``record``,
    spec §1); the core vector is symmetric under the (record, candidate)
    swap (absolute deltas, symmetric set measures) — pinned upstream.
    Field cosines are GATED OFF for v1 and are not implemented here.

    Raises :class:`CortexSchemaError` when ``similarity`` is not a real
    number within [-1, 1] (request-time schema violation, spec §7).
    """
    try:
        cos_target = float(similarity)
    except (TypeError, ValueError) as exc:
        raise CortexSchemaError(f"similarity must be a real number, got {similarity!r}") from exc
    if np.isnan(cos_target) or cos_target < -1.0 or cos_target > 1.0:
        raise CortexSchemaError(
            f"similarity must be within [-1, 1] and not NaN, got {cos_target!r}"
        )

    text_a = _cortex_normalized_text(record)
    text_b = _cortex_normalized_text(candidate)
    grams_a = {n: _cortex_ngrams(text_a, n) for n in (3, 4, 5)}
    grams_b = {n: _cortex_ngrams(text_b, n) for n in (3, 4, 5)}

    values: list[float] = [cos_target]
    for n in (3, 4, 5):
        values.append(_cortex_jaccard(grams_a[n], grams_b[n]))
    for n in (3, 4, 5):
        values.append(_cortex_containment(grams_a[n], grams_b[n]))
    values.append(_cortex_jaccard(frozenset(record.tags), frozenset(candidate.tags)))
    values.append(abs(len(record.title) - len(candidate.title)))
    values.append(abs(len(record.body) - len(candidate.body)))
    values.append(abs(len(record.tags) - len(candidate.tags)))
    values.append(_cortex_match(record.record_type, candidate.record_type))
    values.append(_cortex_match(record.language, candidate.language))
    return tuple(float(v) for v in values)


class VesmaProvider:
    """The bundled vesma-cortex-v1 model behind the ADR-0004 interface.

    Implementation (b) of the seam: a gradient-boosting ONNX artifact
    (candidate ``d-boost``, calibration ADOPT 2026-10-01) over the 13
    frozen pair features. Contract: ``inference-v1.md`` §6 steps 1-8 —
    EAGER init (the constructor either raises a typed :class:`CortexError`
    or leaves a session that already answered one smoke inference), the
    ≤5 MB self-containment gate, the weights sha256 into telemetry, the
    ``embedder_pin`` assert against the LIVE embedder fingerprint
    (mismatch = recalibration event, NOT graceful degradation), a CPU ORT
    session honoring ``VESMA_ORT_THREADS`` (fallback
    ``OMP_NUM_THREADS``, default 4, ``inter_op_num_threads=1``) and a
    smoke inference at startup.

    Execution order note: the spec lists metadata validation before the
    size gate, but ``metadata_props`` are only readable THROUGH an ORT
    session — the enforced order is size → load → metadata → pin →
    smoke; every step maps to the same spec §7 code regardless of order.

    Answers:

    * «record-quality» → the same honest 0.5/0.5 placeholder as the
      deterministic provider (NO-DATA discipline of the prereg; the
      cortex calibration covers is-duplicate only — spec §3).
    * «is-duplicate» → the CALIBRATED probability (spec §3: a graded
      likelihood, deliberately NOT the {0.0, 1.0} step of the baseline).
      The application threshold is product policy
      (:data:`CORTEX_DUPLICATE_PROBABILITY_THRESHOLD`), never applied
      here — the request's cosine ``threshold`` is ignored for the same
      reason the Jev adapter ignores it: the semantic leg answers the
      question, it does not re-implement the engine's step function.
    * «goal-overlap» → typed refusal: the artifact has no goal-overlap
      head and a fabricated answer would poison the calibration bar.

    Fail-open at request time (spec §7): a :class:`CortexInferError`
    (ORT run/decode failure) or :class:`CortexSchemaError` (invalid
    similarity) degrades THAT verdict to the deterministic step rule
    with a machine-parseable ``code=`` warn — ingest is never blocked.
    Missing evidence (``state.candidate``/``state.similarity`` is None)
    keeps the engine-wide typed refusal :class:`MissingEvidenceError`:
    the question is unanswerable for EVERY provider (the deterministic
    fallback would raise it too), so degrading would only mask a caller
    bug — the spec's «нет сторон/similarity» row maps onto this existing
    stricter-typed engine refusal (documented reconciliation, spec §10
    style).

    Network isolation (spec §9): this module carries ZERO network
    imports — the provider is local-only by construction, pinned by the
    AST tripwire ``tests/test_cortex_network_isolation.py``.
    """

    name: Final[str] = CORTEX_MODEL_NAME

    def __init__(self, embedder_fingerprint: str) -> None:
        """Eager load (spec §6 step 1) — raise typed or stay ready.

        ``embedder_fingerprint`` is the LIVE engine embedder vintage the
        pin is asserted against (e.g.
        ``config_fingerprint(EmbeddingConfig())`` → ``nano:sha256:…``).
        An empty fingerprint fails the pin assert — an unassertable pin
        is a refusal, not an assumption.
        """
        try:
            self._load(embedder_fingerprint)
        except CortexError:
            raise
        except Exception as exc:  # boundary: env/file/ORT are untrusted
            raise CortexError(f"unexpected load failure: {type(exc).__name__}") from exc

    def _load(self, embedder_fingerprint: str) -> None:
        import onnxruntime as ort

        bundle = resource_files("vesma") / "models" / CORTEX_ARTIFACT_DIR
        # The wheel/source layouts are real directories — the same assumption
        # NanoProvider makes when it stringifies the Traversable for ORT. Take
        # a real Path up front so the size gate can stat without reading.
        onnx_path = Path(str(bundle / "model.onnx"))
        if not onnx_path.is_file():
            raise CortexError(
                f"bundled artifact {CORTEX_ARTIFACT_DIR!r} is missing model.onnx "
                f"(looked at {onnx_path!s})"
            )
        size = onnx_path.stat().st_size
        if size > CORTEX_MAX_ARTIFACT_BYTES:
            raise CortexSizeError(f"artifact is {size} bytes > gate {CORTEX_MAX_ARTIFACT_BYTES}")

        digest = hashlib.sha256()
        with onnx_path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        self.weights_sha256 = digest.hexdigest()

        n_threads = max(
            1,
            int(os.environ.get("VESMA_ORT_THREADS") or os.environ.get("OMP_NUM_THREADS") or "4"),
        )
        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = n_threads
        sess_opts.inter_op_num_threads = 1
        try:
            self._session = ort.InferenceSession(
                str(onnx_path), sess_options=sess_opts, providers=["CPUExecutionProvider"]
            )
        except Exception as exc:
            raise CortexError(f"ORT session init failed: {type(exc).__name__}") from exc

        inputs = self._session.get_inputs()
        if len(inputs) != 1 or inputs[0].name != "features":
            raise CortexMetaError(
                f"graph input contract drift: expected one input 'features', got "
                f"{[inp.name for inp in inputs]}"
            )
        props_raw = getattr(self._session.get_modelmeta(), "custom_metadata_map", None)
        if not isinstance(props_raw, dict):
            raise CortexMetaError("model carries no readable metadata_props")
        props: dict[str, str] = {str(k): str(v) for k, v in props_raw.items()}
        validate_cortex_metadata_props(props)
        self.embedder_pin = props["embedder_pin"]
        self.corpus_fingerprint = props.get("corpus_fingerprint", "")
        assert_cortex_embedder_pin(self.embedder_pin, embedder_fingerprint)

        # Smoke inference (spec §6 step 7): one synthetic valid input
        # (zeros of dim K) must decode to a finite probability in [0, 1].
        self._smoke_infer()

        logger.info(
            "vesma-cortex ready: %s (features=%d, sha256=%s…, pin=ok, corpus=%s…)",
            CORTEX_ARTIFACT_DIR,
            len(CORTEX_FEATURE_NAMES),
            self.weights_sha256[:12],
            self.corpus_fingerprint[:12],
        )

    def _smoke_infer(self) -> None:
        """Startup smoke (spec §6 step 7) — shape/type/range check."""
        probability = self._run_graph(np.zeros(len(CORTEX_FEATURE_NAMES), dtype=np.float32))
        logger.debug("vesma-cortex smoke probability=%.6f", probability)

    def _run_graph(self, features: _OrtTensor) -> float:
        """One ORT pass → decoded probability; :class:`CortexInferError`
        on run failure, undecodable output or a value outside [0, 1]
        (never clipped — spec §5)."""
        try:
            outputs = cast(list[_OrtTensor], self._session.run(None, {"features": features}))
        except Exception as exc:
            raise CortexInferError(f"ORT run failed: {type(exc).__name__}") from exc
        if len(outputs) != 1:
            raise CortexInferError(f"expected one output tensor, got {len(outputs)}")
        tensor = np.asarray(outputs[0])
        if tensor.shape != (1,):
            raise CortexInferError(f"output shape {tensor.shape} != (1,)")
        try:
            probability = float(tensor[0])
        except (TypeError, ValueError) as exc:
            raise CortexInferError(
                f"output tensor is not scalar-decodable: {type(exc).__name__}"
            ) from exc
        if np.isnan(probability) or probability < 0.0 or probability > 1.0:
            raise CortexInferError(
                f"probability {probability!r} outside [0, 1] — decode error, no clip"
            )
        return probability

    def evaluate(self, request: DecisionRequest, state: CanonState) -> DecisionPrimitive:
        """Answer one product-fixed question over the prepared state."""
        if isinstance(request, RecordQualityRequest):
            return Score(
                spectrum=SPECTRUM_RECORD_QUALITY,
                value=RECORD_QUALITY_PLACEHOLDER,
                confidence=RECORD_QUALITY_CONFIDENCE,
            )
        if isinstance(request, IsDuplicateRequest):
            if state.candidate is None or state.similarity is None:
                raise MissingEvidenceError(
                    "is-duplicate requires measured evidence: state.candidate and "
                    "state.similarity (vector-leg cosine) must be provided"
                )
            try:
                values = cortex_pair_features(state.record, state.candidate, state.similarity)
                probability = self._run_graph(np.array(values, dtype=np.float32))
            except (CortexInferError, CortexSchemaError) as exc:
                # Fail-open per verdict (spec §7): machine-parseable warn +
                # the deterministic step rule for THIS request.
                logger.warning(
                    "code=%s vesma-cortex verdict degraded to the deterministic step: %s",
                    exc.code,
                    exc,
                )
                probability = DeterministicProvider._is_duplicate_probability(
                    state, request.threshold
                )
            return Noul(question=QUESTION_IS_DUPLICATE, probability=probability)
        if isinstance(request, GoalOverlapRequest):
            raise UnsupportedPrimitiveError(
                "goal-overlap has no cortex head — the vesma-cortex-v1 artifact answers "
                "is-duplicate only (inference-v1.md §1)"
            )
        raise UnsupportedPrimitiveError(f"no vesma-cortex heuristic for {type(request).__name__}")


# ── Baseline runner (calibration instrument — ADR-0004 rule 4) ───────────────


@dataclass(frozen=True)
class BaselineVerdict:
    """One corpus record's deterministic-baseline answers.

    ``is_duplicate`` is ``None`` when the question is not askable (the
    streaming policy has no prior record to adjudicate against); the
    candidate key and the measured similarity ride alongside for the
    pre-registered acceptance rule.
    """

    key: str
    record_quality: Score
    is_duplicate: Noul | None
    is_duplicate_candidate: str | None
    similarity: float | None


def run_streaming_baseline(
    entries: Sequence[tuple[str, CanonRecordView]],
    provider: DecisionProvider,
    similarity_fn: Callable[[CanonRecordView, CanonRecordView], float],
) -> list[BaselineVerdict]:
    """Stream a corpus through a provider under the W4b baseline policy.

    Pre-registered methodology (canon repo,
    ``docs/experiments/provider-calibration.md``): records arrive in
    the fixed corpus order; record *i* is adjudicated against its
    highest-similarity prior record (the engine's minting flow shape:
    a new write against the existing store, top candidate). Ties break
    to the EARLIEST prior record — a function of the entry sequence
    alone. The first record has no prior → its is-duplicate verdict is
    ``None`` (not askable), never a fabricated answer.

    Every provider — vesma, Jev — is measured with THIS runner on the
    same corpus and similarity leg; only the provider argument changes.
    That is the calibration contract: the comparison is like-for-like
    by construction.
    """
    verdicts: list[BaselineVerdict] = []
    seen: list[CanonRecordView] = []
    seen_keys: list[str] = []
    for key, record in entries:
        quality_decision = provider.evaluate(RecordQualityRequest(), CanonState(record=record))
        if not isinstance(quality_decision, Score):
            raise UnsupportedPrimitiveError("record-quality must answer with a Score")
        dup: Noul | None = None
        dup_candidate: str | None = None
        best_similarity: float | None = None
        if seen:
            best_idx = max(
                range(len(seen)),
                key=lambda i: (similarity_fn(record, seen[i]), -i),
            )
            best_similarity = similarity_fn(record, seen[best_idx])
            state = CanonState(record=record, candidate=seen[best_idx], similarity=best_similarity)
            dup_decision = provider.evaluate(IsDuplicateRequest(), state)
            if not isinstance(dup_decision, Noul):
                raise UnsupportedPrimitiveError("is-duplicate must answer with a Noul")
            dup = dup_decision
            dup_candidate = seen_keys[best_idx]
        verdicts.append(
            BaselineVerdict(
                key=key,
                record_quality=quality_decision,
                is_duplicate=dup,
                is_duplicate_candidate=dup_candidate,
                similarity=best_similarity,
            )
        )
        seen.append(record)
        seen_keys.append(key)
    return verdicts
