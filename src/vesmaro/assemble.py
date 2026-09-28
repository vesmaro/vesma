"""ADR-0017 D1 / ADR-0018 — ``assemble_context`` provider contract (mnemos #125).

One API assembles the model-facing context block:

    assemble_context(session, project, file?, budget, mode) -> ContextBlock

Fixed pipeline, in order (ADR-0017 D1 with the ADR-0018 CCR-stage amendment):

  1. recall     — hybrid RRF via ``MemoryManager.search`` (FTS5 + vector
                  legs, ``CONTEXT_ADMISSIBLE_STATUSES`` gate by default —
                  no ``status``/``include_raw`` is passed, so only
                  ``published``/``processed`` surface). ADR-0025 E1: when
                  ``LanesConfig.enabled`` is on, this stage runs the
                  deterministic lanes SUB-STAGE first (``mnemos/lanes.py``
                  — rules/decisions via ``list_all(tags=...)``, knowledge
                  = the same RRF recall with governance rows excluded);
                  the six-stage order itself is unchanged, and with the
                  flag off (the default) the code path is identical to
                  the pre-E1 pipeline.
  2. ccr        — OPTIONAL (``expand_ccr=True``): expand inline
                  ``[compressed: <hash> | …]`` markers found in recalled
                  content via ``retrieve_content`` (project-scoped, already
                  issuance-scanned), budget-aware: an original that would
                  not fit the caller's budget is left compressed (the
                  marker stays; the model can retrieve on demand).
  3. filter     — the 5-stage context filter (``filter/pipeline``) per
                  block, auto-detected profile. MANDATORY.
  4. scan       — issuance secret scan (``scan_issuance``) per block.
                  MANDATORY: nothing enters the assembled output unscanned;
                  refuse mode drops the block (fail-closed). Redactions
                  are counted per block.
  5. align      — CacheAligner per block (dynamic spans relocated to the
                  BLOCK tail — see the note below on provenance ordering).
  6. budget     — greedy rank-ordered inclusion of whole provenance-wrapped
                  blocks under the caller's token budget.

Entry invariant (ADR-0018): every LTM → context path passes secret scan,
provenance wrapper, and status gate. This module is that path for
pre-LLM-call injection.

Design decisions (flagged for ArchCom ratification in the #125 report):

* **Provenance format** — one prefix line per injected block, exact shape
  ``[mnemos:<memory-id> project=<slug> status=<status> origin=<source>
  pipeline=<phase> v=<n> retrieved=<iso>]`` (ADR-0019 §4 amendment:
  ``pipeline=`` omitted on NULL/legacy pipeline_state; ``v=`` is the
  marker version; the legacy ``retrieved`` timestamp stays last).
  ``origin=`` renders the server-side ``source`` column and is always
  present (ADR-0025 P0: mint provenance) — row-static, so the
  CacheAligner tail ordering above is unaffected. ``retrieved=<iso>``
  is SESSION-scoped (#282): the manager stamps it on the session's
  FIRST assembly and reuses it for every later assembly of the same
  session, so the block prefix is byte-stable across turns
  (KV-cache friendly); different sessions get different stamps.
* **Provenance vs CacheAligner order** — the aligner relocates ISO
  timestamps to the tail, which would gut the ``retrieved=<iso>`` field of
  a provenance line if alignment ran after wrapping. Blocks are therefore
  aligned FIRST and wrapped AFTER: the block *content* gets its dynamic
  tail, the provenance line stays parseable.
* **Budget partitioning (addendum 2, MAY — NOT implemented)** — ``budget``
  stays monolithic. An active-state line reserved before recall allocation
  waits for the D5 baseline corridor (the ~500-token figure has no
  evidence basis).
* **Whole-block budget fill** — blocks that do not fit are SKIPPED, not
  mid-block truncated: a truncated entry whose provenance promises a
  memory the model only half-sees is worse than a clean skip (the model
  can fetch the full entry by id). ``stats.budget.skipped`` counts them.
* **contentType partition** — ``mode=code`` keeps candidates whose
  ``detect_profile`` result at ingest was ``code``; ``mode=prose`` keeps
  the rest (binary partition over log/terminal/docs/web/default — the
  addendum names no finer split; flagged for ratification).
* **Legacy rows** — memories ingested before the ingest-side capture have
  no stored ``content_type``; the filter falls back to on-the-fly
  ``detect_profile`` (same canonical function) and counts the fallback in
  ``stats.recall.content_type_fallbacks``.

Reported defects (found by earlier slices, escalated — NOT worked around
silently):

* ``MemoryManager.search`` passed ``project`` to the FTS leg only; the
  vector leg had no project filter, so a project-scoped search could
  surface other projects' rows via the vector resolve path. FIXED by
  the A9 pre-RRF project predicate (ArchCom 2026-08-27 — native store
  filter + authoritative resolve-time guard in ``MemoryManager.search``);
  this module's interim boundary defence
  (``stats.recall.project_scoped_out``) was removed — the systemic fix
  supersedes the channel patch.

``mode`` semantics (single parameter, two axes):

* delivery: ``sync`` (default) or ``async``. ``async`` runs the same
  pipeline, stores the result in a bounded per-manager registry, and
  returns only a handle envelope; the full result is fetched on a later
  call by passing ``async_handle=<id>``. Handles are session-bound
  (review F1, CWE-863): only the session that assembled may redeem, a
  mismatch raises without consuming the entry. Deliberately minimal —
  no worker threads, no persistence.
* contentType (addendum 1): ``code`` / ``prose`` filter recall candidates
  by stored content type (delivery defaults to sync).

ADR-0027 Phase 0 multi-context parameters (epic #308, both optional,
both TAIL-ONLY — invariant 1: the provenance format, the per-block
CacheAligner alignment and every pinned prefix are untouched; both
narrow the per-call assembled tail and nothing else):

* ``task`` — the bare task slug. Recall narrows to rows carrying
  ``task:<slug>`` via the search ``tags`` filter: the INTERSECTION
  doctrine (project x agent x session x task — a task condition only
  narrows, never widens). The narrowing binds to the RRF knowledge leg,
  the graph edge leg (slice-1 gates) and — symmetrically — the
  governance lanes leg. Task-scoped queries get no project soft
  fallback (slice 1). Default ``None``: no ``task`` keys anywhere, the
  pre-Phase-0 output.
* ``lens`` — a code-defined preset (:mod:`vesmaro.lens`): a
  deterministic QUERY-CONDITIONED projection over the recalled corpus
  that only narrows admissibility (E3 banned query-blind composition as
  a class — a lens decides from the effective query whether it is
  active at all). Never reorders, never pins into the top-of-context
  (#248 open). Definitions are code under review, never caller input;
  default ``None``: identity projection, no ``lens`` keys anywhere.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from vesmaro.ccr import parse_marker
from vesmaro.filter.pipeline import detect_profile, estimate_tokens
from vesmaro.lanes import (
    B0_TYPE_BOOST_FACTOR,
    PRE_LLM_LANE_VALUES,
    PRE_LLM_PINNED_LANES,
    Lane,
    assert_foreign_lanes_tail_only,
    governance_lanes_recall,
    is_governance,
    lane_sort_key,
)
from vesmaro.lens import Lens, lens_active, lens_admits, resolve_lens
from vesmaro.models import TASK_SLUG_RE, Memory

if TYPE_CHECKING:
    from vesmaro.manager import MemoryManager

logger = logging.getLogger(__name__)

#: Valid ``mode`` values — delivery (sync/async) + contentType (code/prose).
VALID_MODES: Final[frozenset[str]] = frozenset({"sync", "async", "code", "prose"})

#: Delivery+contentType values (the two contentType modes deliver synchronously).
_CONTENT_TYPE_MODES: Final[frozenset[str]] = frozenset({"code", "prose"})

#: Default token budget for the assembled block.
DEFAULT_BUDGET: int = 2048

#: Recall depth — hybrid-search limit before contentType filtering.
RECALL_DEPTH: int = 10

#: Async result registry bound (evicts oldest; single-tenant, in-memory).
ASYNC_REGISTRY_CAP: int = 32

#: The fixed pipeline stage order (recorded verbatim in ``stats.stages``).
STAGE_ORDER: Final[tuple[str, ...]] = (
    "recall",
    "ccr",
    "filter",
    "scan",
    "align",
    "budget",
)


# ── Internal candidate model ───────────────────────────────────────────────────


@dataclass(slots=True)
class _Candidate:
    """One recalled memory flowing through the pipeline stages."""

    memory: Memory
    score: float
    search_type: str
    content_type: str
    # Block content, mutated stage by stage.
    content: str
    # CCR stage bookkeeping.
    ccr_expanded: bool = False
    ccr_redactions: int = 0
    ccr_patterns: dict[str, int] = field(default_factory=dict)
    # Origin hashes of the markers expanded into this block (review F2:
    # provenance fidelity — the wrapper names the outer memory only, so
    # the expanded spans' content-addressed origins are recorded here).
    ccr_hashes: list[str] = field(default_factory=list)
    # Scan stage bookkeeping.
    redactions: int = 0
    redacted_patterns: dict[str, int] = field(default_factory=dict)
    # Filter/align stage bookkeeping.
    filter_profile: str | None = None
    align_moved_chars: int = 0
    # ADR-0025 E1 — retrieval lane of this candidate ("knowledge" is the
    # pre-E1 default, so a flag-off assembly needs no lane bookkeeping at
    # all). The ONLY structural model change of the lanes spike.
    lane: str = "knowledge"


# ── Provenance ─────────────────────────────────────────────────────────────────


def build_provenance(memory: Memory, retrieved_iso: str, *, project: str | None = None) -> str:
    """Build the one-line provenance prefix every injected block carries.

    Exact format (ADR-0017 D1 "injected entries carry provenance",
    amended by ADR-0019 §4; the shape is this module's contract, tested
    verbatim):

    ``[mnemos:<memory-id> project=<slug> status=<status> origin=<source>
    pipeline=<phase> v=<n> retrieved=<iso>]``

    The ``pipeline=`` segment renders the row's ``pipeline_state`` and
    is OMITTED when it is NULL (legacy rows written before ADR-0019
    Phase B); ``v=`` is ``marker_version`` (1 from day one, incremented
    on every served-projection swap). The legacy ``retrieved=<iso>``
    timestamp stays the final segment (ADR-0017 D1 contract; the
    CacheAligner ordering decision depends on it) and is SESSION-scoped
    (#282): callers pass the manager's first-assembly stamp for the
    session (``MemoryManager.retrieval_iso``) — stable across the
    session's assemblies, so byte-stable block prefixes — and distinct
    across sessions. ``origin=`` renders the server-side ``source``
    column (ADR-0025 P0: mint provenance; groundwork for origin-aware
    pin guards) — always present, row-static, and never a
    client-mintable tag/metadata value.

    ADR-0019 anti-TOCTOU: the marker is built from the SAME ``Memory``
    snapshot the served projection was cut from — never a re-read of
    the row — so status / pipeline_phase / version cannot desync from
    the issued content within one assembly. This is the single
    construction site of the marker (no other module formats it).
    """
    project_slug = memory.project or project or ""
    segments = [
        f"[mnemos:{memory.id}",
        f"project={project_slug}",
        f"status={memory.status.value}",
        # Server column only — a client-mintable tag/metadata value must
        # never reach the marker (ADR-0025 P0).
        f"origin={memory.source.value}",
    ]
    if memory.pipeline_state is not None:
        segments.append(f"pipeline={memory.pipeline_state.value}")
    segments.append(f"v={memory.marker_version}")
    segments.append(f"retrieved={retrieved_iso}")
    return " ".join(segments) + "]"


# ── Validation ─────────────────────────────────────────────────────────────────


def _validate(session: str, project: str, budget: int, mode: str) -> None:
    """Boundary validation — raises ``ValueError`` with actionable messages."""
    if not session or not session.strip():
        raise ValueError("session is required (non-empty string)")
    if not project or not project.strip():
        raise ValueError("project is required (non-empty string)")
    if mode not in VALID_MODES:
        valid = ", ".join(sorted(VALID_MODES))
        raise ValueError(f"invalid mode {mode!r}; valid values: {valid}")
    if budget < 1:
        raise ValueError(f"budget must be >= 1 token, got {budget}")


# ── Stage 1: recall ────────────────────────────────────────────────────────────


def _content_type_of(memory: Memory, *, fallbacks: list[int]) -> str:
    """Stored content type with an on-the-fly fallback for legacy rows.

    The ingest-side capture (``MemoryManager.add``) persists
    ``metadata["content_type"]``; rows written before that capture (or when
    the capture failed non-fatally) are classified on the fly with the same
    canonical ``detect_profile`` function, and the fallback is counted for
    observability.
    """
    stored = memory.metadata.get("content_type")
    if isinstance(stored, str) and stored in ("code", "prose"):
        return stored
    fallbacks[0] += 1
    return "code" if detect_profile(memory.effective_content()) == "code" else "prose"


def _matches_apply_to(memory: Memory, file: str) -> bool:
    """True when a rule memory's ``applyTo:<glob>`` tag matches ``file``.

    M8 tag shape: ``applyTo:src/**`` embedded in the tags list. Matched
    against the file string as given and its basename (the recall caller
    may pass either form); ``fnmatch`` is not path-separator aware, so
    ``**`` behaves as "anything under" only relative to the prefix — the
    same leniency the M8 rule docs describe.
    """
    base = Path(file).name
    for tag in memory.tags:
        if not tag.startswith("applyTo:"):
            continue
        glob = tag[len("applyTo:") :]
        if fnmatch(file, glob) or fnmatch(base, glob):
            return True
    return False


def _applyto_partition(cands: list[_Candidate], file: str) -> tuple[list[_Candidate], int]:
    """Stable partition — applyTo-matching rules float up (M8 semantics),
    preserving order within the matched/unmatched groups."""
    matched: list[_Candidate] = []
    unmatched: list[_Candidate] = []
    for c in cands:
        (matched if _matches_apply_to(c.memory, file) else unmatched).append(c)
    return matched + unmatched, len(matched)


def _recall_stage(
    mgr: MemoryManager,
    *,
    project: str,
    file: str | None,
    content_type: str | None,
    query: str | None = None,
    lanes_enabled: bool = False,
    type_boost: bool = False,
    task: str | None = None,
    lens: Lens | None = None,
) -> tuple[list[_Candidate], dict[str, Any]]:
    """Hybrid RRF recall (status-gated) + contentType filter + applyTo pinning.

    Derived query (W1): the file stem when ``file`` is given, else the
    project slug. ``fts_search`` wraps the whole query in one FTS5
    phrase, so a multi-term "project stem" join would (almost) never
    match lexically — a single most-content-likely term is the honest
    derived query, and the vector leg carries semantic recall in
    production. W3: an EXPLICIT ``query`` (the ``pre_llm_call`` hook's
    ``context_hint`` — what the upcoming model call is about) overrides
    the derived term on both legs; it is one term richer, not a
    different pipeline.

    ADR-0027 Phase 0 task scoping (``task`` — the bare slug, optional):
    the recall narrows to rows carrying ``task:<slug>`` via the search
    ``tags`` filter — the INTERSECTION doctrine (project x agent x
    session x task): a task tag only narrows, never widens, so rows
    without the task tag (including task-less rows) are inadmissible in
    a task-scoped assembly. The narrowing binds identically to the
    RRF knowledge leg (``MemoryManager.search`` post-fusion tags gate),
    to the graph edge leg (same gate, slice-1 pinned) and — for
    symmetry — to the governance lanes leg below (a task-scoped
    assembly never surfaces a row the task-less assembly would not).
    The project soft fallback does NOT apply to task-scoped queries
    (slice 1): zero rows in a task scope is information, not drift.

    ADR-0027 Phase 0 lens preset (``lens`` — a code-defined
    :class:`~vesmaro.lens.Lens`, optional): a deterministic
    query-conditioned projection applied to the collected candidates
    AFTER both legs — order-preserving, only narrowing, never pinned
    (see ``vesmaro.lens`` for the invariant rationale). Inactive for
    the effective query → identity projection (no filtering).

    A9 (ArchCom 2026-08-27): ``MemoryManager.search`` enforces the project
    predicate pre-RRF on both legs (native store filter + authoritative
    resolve-time guard), so results are already project-pure here — the
    interim boundary drop this channel carried
    (``stats.recall.project_scoped_out``) was removed: the systemic fix
    supersedes the channel patch.

    ADR-0025 E1 lanes SUB-STAGE (``lanes_enabled`` from
    ``LanesConfig.enabled``, default off): before the knowledge leg,
    ``governance_lanes_recall`` runs the deterministic rules/decisions
    queries (``list_all(tags=...)``, the ``recall_context`` pattern —
    no ranking, SQL order is the deterministic order); governance-tagged
    rows are EXCLUDED from the RRF knowledge leg (they surface via their
    own lanes — leaving them in RRF too would duplicate blocks and
    re-create the drowning). applyTo pinning partitions WITHIN each lane
    when lanes are on, so the pinned lane order survives file context;
    with lanes off the partition runs over the whole list exactly as
    before. With the flag off no lane query runs and the stats dict
    carries no ``lanes`` key — the pre-E1 output, byte-identical.

    E0 §1.1 leg B0 (``type_boost`` from ``LanesConfig.type_boost``,
    default off, mutually exclusive with ``lanes_enabled``): governance
    rows keep arriving through the ordinary RRF leg only — no lane
    queries run — but their scores are multiplied by
    ``lanes.B0_TYPE_BOOST_FACTOR`` at recall and the candidate list is
    re-ranked by score (one ranking line, zero meta-level). With both
    flags off the stats dict carries no ``type_boost`` key either.
    """
    derived_query = query if query else (Path(file).stem if file else project)

    # ADR-0027 task scoping rides the EXISTING tags filter — no new
    # search surface, the intersection semantics are the tag contract's.
    search_tags: list[str] | None = [f"task:{task}"] if task else None

    results = mgr.search(query=derived_query, project=project, limit=RECALL_DEPTH, tags=search_tags)

    fallbacks = [0]
    candidates: list[_Candidate] = []
    type_filtered = 0
    governance_excluded = 0
    type_boosted = 0
    task_lane_filtered = 0

    if lanes_enabled:
        lane_hits, lane_counts = governance_lanes_recall(mgr, project=project)
        for memory, lane in lane_hits:
            # Task narrowing binds to the lanes leg too (intersection
            # doctrine, symmetric with the RRF/graph tags gate): a
            # governance row without the assembly's task tag is outside
            # the task's admissibility set. With lanes off (default)
            # this loop never runs; with no task it is a no-op.
            if task and f"task:{task}" not in memory.tags:
                task_lane_filtered += 1
                continue
            ct = _content_type_of(memory, fallbacks=fallbacks)
            if content_type is not None and ct != content_type:
                type_filtered += 1
                continue
            candidates.append(
                _Candidate(
                    memory=memory,
                    score=1.0,
                    search_type="lane",
                    content_type=ct,
                    content=memory.effective_content(),
                    lane=lane.value,
                )
            )

    for r in results:
        if lanes_enabled and is_governance(r.memory):
            governance_excluded += 1
            continue
        ct = _content_type_of(r.memory, fallbacks=fallbacks)
        if content_type is not None and ct != content_type:
            type_filtered += 1
            continue
        score = r.score
        if type_boost and is_governance(r.memory):
            # E0 §1.1 leg B0 — the type boost at recall (one ranking
            # line, zero meta-level): governance rows still arrive via
            # ordinary RRF only; their scores are multiplied by the
            # registered factor. Mutually exclusive with lanes_enabled
            # (enforced at the LanesConfig boundary).
            score *= B0_TYPE_BOOST_FACTOR
            type_boosted += 1
        candidates.append(
            _Candidate(
                memory=r.memory,
                score=score,
                search_type=r.search_type,
                content_type=ct,
                content=r.memory.effective_content(),
            )
        )

    if type_boost:
        # B0's "one ranking line": re-rank candidates by (boosted) score,
        # stable sort preserving recall order among equals — BEFORE the
        # applyTo partition below, so M8 pinning still floats applyTo-
        # matching rules to the absolute top (B0 must not break M8).
        candidates.sort(key=lambda c: -c.score)

    # ── ADR-0027 Phase 0 lens projection (optional) ────────────────────
    # Pure order-preserving filter over the collected candidates (both
    # legs): the lens decides from the EFFECTIVE query whether it is
    # active and then keeps only its target content_type. Runs after
    # candidate collection and before the applyTo partition so it never
    # reorders (invariant: a lens may only narrow — no re-ranking, no
    # pinning; #248 stays open). ``lens=None`` (default) skips the whole
    # block — byte-identical to the pre-Phase-0 output.
    # The projection binds to the LANES leg candidates too — symmetric
    # with the contentType mode filter above (``mode=code`` strips prose
    # governance rows in the lanes loop the same way): an ACTIVE lens
    # strips inadmissible governance rows BEFORE the applyTo partition
    # (a code-shaped query under lanes must not have prose rules drown
    # the code rows — the E3 drowning pattern), while an inactive lens
    # leaves the lanes output untouched (pinned by tests as the exact
    # stripping contract; #360 review item 3).
    lens_filtered = 0
    lens_active_for_query = lens_active(lens, query=derived_query) if lens is not None else False
    if lens is not None and lens_active_for_query:
        kept: list[_Candidate] = []
        for cand in candidates:
            if lens_admits(lens, query=derived_query, content_type=cand.content_type):
                kept.append(cand)
            else:
                lens_filtered += 1
        candidates = kept

    pinned = 0
    if file:
        if lanes_enabled:
            # Per-lane stable partition: file-matching rules float up
            # WITHIN their lane, lane order (rules → decisions → knowledge)
            # stays intact — an M8 pin must not reorder across lanes. Any
            # non-pinned lane value (tail lanes) keeps its candidates after
            # the pinned segments instead of being dropped. NOTE: an
            # applyTo-pinned KNOWLEDGE row loses its top-of-list pin here
            # (the budget stage re-sorts within the knowledge lane);
            # M8 scopes applyTo to rules, so that is off-contract input —
            # follow up with the cascade slice if it ever matters.
            lane_values = [lane.value for lane in PRE_LLM_PINNED_LANES]
            tail_values = [
                v for v in dict.fromkeys(c.lane for c in candidates) if v not in PRE_LLM_LANE_VALUES
            ]
            reordered: list[_Candidate] = []
            for lane_value in lane_values + tail_values:
                segment = [c for c in candidates if c.lane == lane_value]
                seg, seg_pinned = _applyto_partition(segment, file)
                reordered.extend(seg)
                pinned += seg_pinned
            candidates = reordered
        else:
            candidates, pinned = _applyto_partition(candidates, file)

    stats: dict[str, Any] = {
        "query": derived_query,
        "query_source": "explicit" if query else "derived",
        "candidates": len(results),
        "admissible": len(results),
        "content_type_filtered": type_filtered,
        "content_type_fallbacks": fallbacks[0],
        "applyto_pinned": pinned,
    }
    if lanes_enabled:
        stats["lanes"] = {
            "rules": lane_counts.get(Lane.RULES.value, 0),
            "decisions": lane_counts.get(Lane.DECISIONS.value, 0),
            "knowledge": sum(1 for c in candidates if c.lane == Lane.KNOWLEDGE.value),
            "governance_excluded_from_knowledge": governance_excluded,
        }
        if task:
            # Additive only when BOTH flags/params are in play — the
            # lanes-off default never sees this key.
            stats["lanes"]["task_filtered"] = task_lane_filtered
    if type_boost:
        # B0 telemetry — additive only when on, same discipline as the
        # lanes stats key (flag-off stats dicts stay byte-identical).
        stats["type_boost"] = {"boosted": type_boosted, "factor": B0_TYPE_BOOST_FACTOR}
    if task:
        # ADR-0027 task telemetry — additive only when the task scope is
        # active; the default (no task) result dict carries no task keys.
        stats["task_scoped"] = True
    if lens is not None:
        # Lens telemetry — additive only when a lens was selected;
        # ``active`` records whether the effective query activated it
        # (an inactive lens is the identity projection: filtered == 0).
        stats["lens"] = {
            "name": lens.value,
            "active": lens_active_for_query,
            "filtered": lens_filtered,
        }
    return candidates, stats


# ── Stage 2: CCR expansion (optional) ─────────────────────────────────────────


def _ccr_stage(
    mgr: MemoryManager,
    candidates: list[_Candidate],
    *,
    project: str,
    budget: int,
    expand: bool,
    agent: str | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    """Expand inline CCR markers via project-scoped retrieval, budget-aware.

    ``retrieve_content`` already runs the issuance scan (ADR-0018 P0), so
    the returned original is redacted-or-refused there; the assembled-block
    scan stage re-scans it regardless (patterns evolve, belt-and-suspenders
    is cheap). An expansion is adopted only when the resulting block stays
    within the caller's budget — otherwise the compressed form (with the
    marker intact) is kept so the model retains the on-demand handle.

    A2 review F2 — strict-mode composition: when the caller's identity
    (``agent`` + ``session``) is available, the marker's metadata and the
    identity are threaded into ``retrieve_content`` so the strict-mode
    validation gate runs (knob-off deployments validate nothing, exactly
    as before). Without identity the expansion issues a plain retrieve:
    under ``ccr.validate_markers`` an issuer-stamped row is refused
    (``marker validation required``) and the expansion is SKIPPED — the
    marker stays in the block, the model keeps the on-demand handle;
    legacy NULL-issuer rows still expand (WARN-allowed). Refused
    expansions are counted separately from missing ones
    (``skipped_refused``).
    """
    markers_found = 0
    expanded = 0
    skipped_missing = 0
    skipped_budget = 0
    skipped_refused = 0

    if expand:
        for cand in candidates:
            marker = parse_marker(cand.content)
            if marker is None:
                continue
            markers_found += 1
            identity_available = bool(agent and agent.strip()) and bool(session and session.strip())
            if identity_available:
                result = mgr.retrieve_content(
                    str(marker["hash"]),
                    project=project,
                    original_chars=int(marker["original_chars"]),
                    agent=agent,
                    session=session,
                )
            else:
                result = mgr.retrieve_content(str(marker["hash"]), project=project)
            original = result.get("original") if result.get("found") else None
            if result.get("refused"):
                skipped_refused += 1
                continue
            if not result.get("found") or not isinstance(original, str):
                skipped_missing += 1
                continue
            start, end = marker["span"]
            expanded_content = cand.content[:start] + original + cand.content[end:]
            if estimate_tokens(expanded_content) > budget:
                skipped_budget += 1
                continue
            cand.content = expanded_content
            cand.ccr_expanded = True
            cand.ccr_redactions = int(result.get("redactions", 0))
            cand.ccr_hashes.append(str(marker["hash"]))
            patterns = result.get("redacted_patterns")
            if isinstance(patterns, dict):
                cand.ccr_patterns = {str(k): int(v) for k, v in patterns.items()}
            expanded += 1
    else:
        markers_found = sum(1 for c in candidates if parse_marker(c.content) is not None)

    return {
        "enabled": expand,
        "markers_found": markers_found,
        "expanded": expanded,
        "skipped_missing": skipped_missing,
        "skipped_budget": skipped_budget,
        "skipped_refused": skipped_refused,
    }


# ── Stage 3: context filter ────────────────────────────────────────────────────


def _filter_stage(candidates: list[_Candidate]) -> dict[str, Any]:
    """Run the 5-stage context filter per block (auto-detected profile)."""
    from vesmaro.filter.pipeline import apply_filter

    profiles: dict[str, int] = {}
    for cand in candidates:
        filtered = apply_filter(cand.content, profile=None, budget=None)
        cand.content = str(filtered["clean_content"])
        cand.filter_profile = str(filtered["profile"])
        profiles[cand.filter_profile] = profiles.get(cand.filter_profile, 0) + 1
    return {"profiles": profiles}


# ── Stage 4: secret scan (mandatory) ───────────────────────────────────────────


def _scan_stage(mgr: MemoryManager, candidates: list[_Candidate]) -> dict[str, Any]:
    """Scan every block at the issuance boundary; refuse mode drops the block.

    Merges the CCR retrieval redaction counts (the rehydrate channel's own
    scan) with the assembled-block scan counts so per-block ``redactions``
    is the total number of redacted spans attributable to that block.
    """
    survivors: list[_Candidate] = []
    refused = 0
    for cand in candidates:
        scan = mgr.scan_issuance(cand.content, context=f"assemble:context:{cand.memory.id}")
        if scan.refused:
            refused += 1
            continue
        cand.content = scan.text
        cand.redactions = scan.redactions + cand.ccr_redactions
        merged = dict(cand.ccr_patterns)
        for name, count in scan.redacted_patterns.items():
            merged[name] = merged.get(name, 0) + count
        cand.redacted_patterns = merged
        survivors.append(cand)
    candidates[:] = survivors
    return {
        "blocks_scanned": len(candidates) + refused,
        "blocks_refused": refused,
    }


# ── Stage 5: CacheAligner ──────────────────────────────────────────────────────


def _align_stage(mgr: MemoryManager, candidates: list[_Candidate]) -> dict[str, Any]:
    """Relocate dynamic content to each block's tail (prefix stability).

    Runs BEFORE provenance wrapping — see the module docstring note on
    provenance ordering (the aligner would otherwise strip the
    ``retrieved=<iso>`` timestamp out of every provenance line).
    """
    aligned_blocks = 0
    moved_chars = 0
    for cand in candidates:
        result = mgr.align_prefix(cand.content, profile=cand.filter_profile)
        cand.content = str(result["aligned_text"])
        chars = int(result["moved_chars"])
        cand.align_moved_chars = chars
        moved_chars += chars
        if bool(result["prefix_stabilized"]):
            aligned_blocks += 1
    return {"blocks_aligned": aligned_blocks, "moved_chars": moved_chars}


# ── Stage 6: token budget ──────────────────────────────────────────────────────


def _budget_stage(
    candidates: list[_Candidate],
    *,
    budget: int,
    project: str,
    retrieved_iso: str,
    lanes_enabled: bool = False,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Greedy rank-ordered inclusion of whole provenance-wrapped blocks.

    ADR-0025 E1 lane ordering: when lanes are enabled, candidates are
    first sorted by ``(lane order, score desc)`` with a STABLE tiebreak —
    Python's stable sort preserves the deterministic recall order for
    equal (lane, score) pairs, so the block prefix is byte-stable across
    assemblies of one session (the KV-cache hypothesis H2 surface,
    E0 §2.2). Lane order is fixed: rules → decisions → knowledge;
    anything outside the pinned prefix (``synthesized`` today, the
    future awareness lane) sorts into the deterministic tail, and
    ``assert_foreign_lanes_tail_only`` fires if a foreign lane ever
    lands inside the pinned prefix. With lanes off there is NO sort
    (recall order stands, exactly as pre-E1) and blocks carry no
    ``lane`` key — the flag-off output stays byte-identical.
    """
    if lanes_enabled:
        candidates.sort(key=lambda c: (*lane_sort_key(c.lane), -c.score))
        assert_foreign_lanes_tail_only([c.lane for c in candidates])

    included: list[dict[str, Any]] = []
    texts: list[str] = []
    skipped = 0
    remaining = budget

    for cand in candidates:
        mem = cand.memory
        provenance = build_provenance(mem, retrieved_iso, project=project)
        block_text = f"{provenance}\n{cand.content}"
        tokens = estimate_tokens(block_text)
        if tokens > remaining:
            skipped += 1
            continue
        remaining -= tokens
        block: dict[str, Any] = {
            "memory_id": mem.id,
            "project": mem.project or project,
            "status": mem.status.value,
            # ADR-0019 §4 structured marker fields — the bracket string
            # above is a projection of THESE values, not the source of
            # truth (None pipeline_phase = legacy row, segment omitted).
            "origin": mem.source.value,  # ADR-0025 P0: server-side origin column
            "pipeline_phase": mem.pipeline_state.value if mem.pipeline_state else None,
            "marker_version": mem.marker_version,
            "score": cand.score,
            "search_type": cand.search_type,
            "content_type": cand.content_type,
            "provenance": provenance,
            "content": cand.content,
            "tokens": tokens,
            "redactions": cand.redactions,
            "ccr_expanded": cand.ccr_expanded,
            # Observability (review F2): origin hashes of the CCR markers
            # expanded into this block — empty when none. The provenance
            # wrapper format is unchanged (it names the outer memory).
            "ccr_hashes": list(cand.ccr_hashes),
        }
        if lanes_enabled:
            # ADR-0025 E1 — additive lane field; OMITTED entirely when
            # lanes are off so the flag-off block shape is unchanged.
            block["lane"] = cand.lane
        if cand.redactions:
            block["redacted_patterns"] = cand.redacted_patterns
        included.append(block)
        texts.append(block_text)

    stats = {
        "budget": budget,
        "estimated_tokens": budget - remaining,
        "blocks_included": len(included),
        "blocks_skipped": skipped,
    }
    return included, texts, stats


# ── Async result registry (per-manager, bounded) ───────────────────────────────


def _store_async_result(
    mgr: MemoryManager, handle: str, result: dict[str, Any], session: str
) -> None:
    """Store an async result bound to its assembling session, evicting
    the oldest entry past the cap.

    Review F1 (CWE-863): the handle is a bearer token — binding the entry
    to the session that created it stops any other session sharing the
    same manager from redeeming it. The INFO log keeps naming the handle
    (uuid4 — unguessable) but never content.
    """
    with mgr._assemble_async_lock:
        registry: dict[str, tuple[dict[str, Any], str]] = mgr._assemble_async
        registry[handle] = (result, session)
        while len(registry) > ASYNC_REGISTRY_CAP:
            oldest = next(iter(registry))
            del registry[oldest]


def _fetch_async_result(mgr: MemoryManager, handle: str, session: str) -> dict[str, Any]:
    """Pop a stored async result — session-bound, one-shot.

    Unknown handles raise ``ValueError``. A session MISMATCH also raises
    ``ValueError`` but does NOT consume the entry (fail-closed without
    burning the handle for the legitimate session): only a fetch by the
    assembling session pops it.
    """
    with mgr._assemble_async_lock:
        entry = mgr._assemble_async.get(handle)
        if entry is None:
            raise ValueError(f"unknown or already-fetched async_handle: {handle!r}")
        result, owner = entry
        if owner != session:
            logger.warning(
                "assemble_context: async handle=%s fetch denied (session mismatch) "
                "— entry left in place for the owning session",
                handle,
            )
            raise ValueError(
                "async_handle belongs to a different session (CWE-863: handles are session-bound)"
            )
        del mgr._assemble_async[handle]
    return result


# ── Public pipeline entry ──────────────────────────────────────────────────────


def assemble_context(
    mgr: MemoryManager,
    *,
    session: str,
    project: str,
    file: str | None = None,
    budget: int = DEFAULT_BUDGET,
    mode: str = "sync",
    expand_ccr: bool = False,
    async_handle: str | None = None,
    agent: str | None = None,
    query: str | None = None,
    task: str | None = None,
    lens: str | Lens | None = None,
) -> dict[str, Any]:
    """Assemble the model-facing context block (ADR-0017 D1 contract).

    Args:
        mgr: The owning ``MemoryManager`` (stages reuse its search /
            retrieve / scan / align primitives — no parallel retrieval path).
        session: Caller's session identifier (echoed in the result; used
            for assembly-level provenance, not per-block provenance).
        project: Project slug scoping recall and CCR redemption.
        file: Optional file path — contributes recall query terms and pins
            applyTo-scoped rule memories to the top of the candidates.
        budget: Token budget for the assembled block (monolithic — see the
            module docstring for the partitioning decision).
        mode: ``sync`` (default) / ``async`` / ``code`` / ``prose`` — see the
            module docstring for the two-axis semantics.
        expand_ccr: Enable the optional CCR stage (default off).
        async_handle: When given, fetch (and pop) a previously stored
            async result instead of running a new pipeline. The fetch is
            session-bound (review F1): only the session that created the
            handle may redeem it.
        agent: A2 review F2 — caller's agent slug. With ``session`` it
            forms the issuer context threaded into the CCR expansion, so
            under ``ccr.validate_markers`` only markers minted in the
            caller's own ``(agent, session)`` context expand; without it
            a strict deployment skips expansion of issuer-stamped rows
            (the marker stays; legacy NULL-issuer rows still expand).
        query: W3 — explicit recall query overriding the derived term
            (file stem / project slug). The ``pre_llm_call`` lifecycle
            hook threads its ``context_hint`` here: what the upcoming
            model call is about, so recall finds semantically relevant
            entries instead of guessing from the slug. Blank strings are
            rejected at the boundary (pass ``None`` for the derived
            fallback).
        task: ADR-0027 Phase 0 — optional task scope (the BARE slug, the
            ``task:`` prefix is added internally; ``[a-z0-9_-]{1,64}``).
            Narrows recall to rows carrying ``task:<slug>`` (intersection
            doctrine — a task condition only narrows, never widens) and
            composes the per-call assembled tail only: the provenance
            format, the CacheAligner prefix-stability contract and every
            pinned prefix are untouched (ADR-0027 invariant 1). Rows
            without the task tag (including task-less rows) are
            inadmissible in a task-scoped assembly; the project soft
            fallback is suppressed for task-scoped queries (slice 1).
        lens: ADR-0027 Phase 0 — optional code-defined lens preset
            (:class:`~vesmaro.lens.Lens` member or its value string; any
            other name raises). A deterministic query-conditioned
            projection over the recalled corpus that only narrows
            admissibility — never reorders, never pins into the
            top-of-context (#248), never widens. Default ``None`` = no
            lens, byte-identical pipeline.

    Returns:
        The ContextBlock dict: ``text`` (provenance-wrapped blocks joined
        by blank lines), per-``blocks`` detail with provenance + redaction
        counts + expanded CCR origin hashes (``ccr_hashes``), ``tokens``
        stats, and per-``stats`` stage telemetry. ``task`` / ``lens``
        keys are ECHOED only when the respective parameter was given
        (additive-only: the default result dict shape is unchanged). For
        ``mode="async"`` only a handle envelope is returned; the full
        result comes back on the next call with ``async_handle``.

    Raises:
        ValueError: Invalid ``session`` / ``project`` / ``mode`` / ``budget``
        / ``query`` / ``task`` / ``lens``, an unknown ``async_handle``, or
        an ``async_handle`` owned by a different session (boundary
        validation).
    """
    _validate(session, project, budget, mode)
    if query is not None and not query.strip():
        raise ValueError("query must be a non-empty string when provided (None = derived)")
    resolved_lens = resolve_lens(lens)
    if task is not None:
        # Bare-slug boundary validation against the SAME alphabet the
        # tag contract accepts (``TASK_SLUG_RE`` shares its pattern with
        # the ``task:`` tag regex in models.py). A value already carrying
        # the ``task:`` prefix is rejected with an actionable message —
        # the prefix is this boundary's job, not the caller's.
        if task.startswith("task:"):
            raise ValueError(
                f"task must be the bare slug (got {task!r} — pass {task[len('task:') :]!r})"
            )
        if not TASK_SLUG_RE.match(task):
            raise ValueError(f"task must match [a-z0-9_-]{{1,64}} when provided (got {task!r})")

    if async_handle is not None:
        fetched = _fetch_async_result(mgr, async_handle, session)
        fetched["async_handle"] = async_handle
        logger.info(
            "assemble_context: fetched async handle=%s session=%s project=%s",
            async_handle,
            session,
            project,
        )
        return fetched

    delivery = "async" if mode == "async" else "sync"
    content_type: str | None = mode if mode in _CONTENT_TYPE_MODES else None
    # mnemos #282 — session-scoped first-assembly stamp (NOT per-call):
    # stable across this session's assemblies so the block prefix stays
    # byte-identical for harness-side KV caching; distinct across sessions.
    # The bounded registry lives on the manager (MemoryManager.retrieval_iso).
    retrieved_iso = mgr.retrieval_iso(session)
    # ADR-0025 E1 — the ONE switch (LanesConfig.enabled, default False):
    # read once, threaded to the recall sub-stage and the budget stage.
    lanes_enabled = mgr.settings.lanes.enabled
    # E0 §1.1 leg B0 — the trivial type-boost treatment (mutually exclusive
    # with lanes; enforced at the LanesConfig boundary).
    type_boost = mgr.settings.lanes.type_boost
    if lanes_enabled and type_boost:
        # Point-of-use mutex (review P3): the construction-time
        # LanesConfig validator can be bypassed by direct attribute
        # assignment on an already-built manager — assembling with both
        # treatments on would silently run an unregistered fourth leg.
        # Assertion-style guard, the repo's invariant convention
        # (cf. lanes.assert_foreign_lanes_tail_only from the budget stage).
        raise AssertionError(
            "LanesConfig: 'enabled' (E0 §1.1 leg B) and 'type_boost' (leg B0) "
            "are mutually exclusive treatments — a leg is exactly one of "
            "A/B0/B (construction validator bypassed by direct assignment?)"
        )

    # ── Fixed stage order (D1; recorded verbatim in stats) ────────────────
    candidates, recall_stats = _recall_stage(
        mgr,
        project=project,
        file=file,
        content_type=content_type,
        query=query,
        lanes_enabled=lanes_enabled,
        type_boost=type_boost,
        task=task,
        lens=resolved_lens,
    )
    ccr_stats = _ccr_stage(
        mgr,
        candidates,
        project=project,
        budget=budget,
        expand=expand_ccr,
        agent=agent,
        session=session,
    )
    filter_stats = _filter_stage(candidates)
    scan_stats = _scan_stage(mgr, candidates)
    align_stats = _align_stage(mgr, candidates)
    blocks, texts, budget_stats = _budget_stage(
        candidates,
        budget=budget,
        project=project,
        retrieved_iso=retrieved_iso,
        lanes_enabled=lanes_enabled,
    )

    result: dict[str, Any] = {
        "session": session,
        "project": project,
        "file": file,
        "mode": delivery,
        "content_type": content_type,
        "text": "\n\n".join(texts),
        "blocks": blocks,
        "tokens": {"budget": budget, "estimated": budget_stats["estimated_tokens"]},
        "stats": {
            "stages": list(STAGE_ORDER),
            "recall": recall_stats,
            "ccr": ccr_stats,
            "filter": filter_stats,
            "scan": scan_stats,
            "align": align_stats,
            "budget": budget_stats,
        },
    }
    # ADR-0032 §3.5 — the project-graph beacon v1: ONE tail line appended
    # AFTER the budget stage. It rides OUTSIDE the budget blocks (never a
    # block, never counted in ``tokens.estimated`` — the ADR-0027 budget
    # the beacon lives outside) and is never pinned: a plain trailing
    # line. NOT a seventh pipeline stage — no stats, no stage key. Every
    # failure degrades to absence (guest-contract discipline, same as
    # the vitals plane): the beacon must never break the assembly.
    if mgr.settings.code_graph.enabled and mgr.settings.code_graph.beacon:
        beacon: str | None = None
        try:
            graph_service = mgr.get_codegraph_service()
            if graph_service is not None:
                beacon = graph_service.beacon_line(project)
        except Exception:
            logger.debug("codegraph beacon skipped (wiring error)", exc_info=True)
        if beacon:
            result["text"] = f"{result['text']}\n\n{beacon}" if result["text"] else beacon

    # ADR-0027 Phase 0 echoes — ADDITIVE ONLY when the respective
    # parameter was given: the default (no task, no lens) result dict
    # shape is byte-identical to the pre-Phase-0 output (pinned by
    # tests; same discipline as the ``lanes`` / ``type_boost`` keys).
    if task is not None:
        result["task"] = task
    if resolved_lens is not None:
        result["lens"] = resolved_lens.value

    logger.info(
        "assemble_context: session=%s project=%s file=%s mode=%s content_type=%s "
        "candidates=%d blocks=%d refused=%d tokens=%d/%d redactions=%d task=%s lens=%s",
        session,
        project,
        file,
        delivery,
        content_type,
        recall_stats["candidates"],
        len(blocks),
        scan_stats["blocks_refused"],
        budget_stats["estimated_tokens"],
        budget,
        sum(b["redactions"] for b in blocks),
        task,
        resolved_lens.value if resolved_lens is not None else None,
    )

    if delivery == "async":
        handle = uuid.uuid4().hex
        _store_async_result(mgr, handle, result, session)
        logger.info("assemble_context: stored async handle=%s", handle)
        return {
            "mode": "async",
            "handle": handle,
            "status": "ready",
            "note": (
                "result stored; call assemble_context(async_handle=<handle>) "
                "to fetch the assembled block"
            ),
        }

    return result
