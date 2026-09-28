"""ADR-0004 decision-provider seam — the typed «semantic if» interface.

The FIRST implementation of the interface ratified in vesmaro-canon
[ADR 0004](https://github.com/vesmaro/vesmaro-canon/blob/main/docs/decisions/0004-decision-provider.md):
a decision provider answers TYPED questions (``Choice`` / ``Score`` /
``Noul``) over PREPARED canon state, never raw memory dumps. Three
implementations are planned on this one interface — deterministic
engine heuristics (this module, now), a local mnema router (goal,
local-first) and an opt-in external Jev adapter (owner flag + key,
off by default forever).

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
   :class:`~vesmaro.models.Memory` onto the view; store-only fields
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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from vesmaro.awareness import conflict_hints
from vesmaro.graph_minting import AUTO_DEDUPE_SIMILARITY_THRESHOLD
from vesmaro.models import Memory

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
    #: confidence per answer — ADR-0004 Consequences).
    name: str

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
      (:data:`vesmaro.graph_minting.AUTO_DEDUPE_SIMILARITY_THRESHOLD`,
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

    Every provider — mnema, Jev — is measured with THIS runner on the
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
