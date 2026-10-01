"""MemoryManager — core CRUD and search orchestrator for Vesma.

Backed by:
  - SQLiteStore  : all memories (raw/processing/processed/published) + traces
  - VectorStore  : embeddings for published memories only
  - VaultManager : Obsidian-compatible markdown mirror
  - EmbeddingProvider : configurable local ONNX (default) / Ollama / ST
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    # Annotation-only (the dataclass fields are lazy strings under
    # ``from __future__ import annotations``); the scanner is imported
    # lazily at the call sites to keep the module import light.
    from vesmaro.codegraph.autoindex import AutoIndexer
    from vesmaro.codegraph.service import CodeGraphService
    from vesmaro.codegraph.watch import GraphWatchScheduler
    from vesmaro.secrets_detector import SecretFinding

from vesmaro import __version__
from vesmaro.canon_validate import (
    CanonViolationError,
    canon_envelope_is_client_authored,
    validate_canon_record,
)
from vesmaro.config import Settings
from vesmaro.danger_detectors import DetectionResult, detect

# ADR-0027 Ф3 (epic #308) — the docs-as-memory document-ingest
# authority. Imported as an alias to avoid the module↔manager name
# collision (the function IS ``ingest_document`` on both sides; the
# manager's method is the public surface, the module's function is the
# single implementation).
from vesmaro.docs_ingest import (
    DOC_CHUNK_CACHE_VERSION_META_KEY,
    DOC_SWEEP_STAMP_KEYS,
    DocIngestResult,
)
from vesmaro.docs_ingest import ingest_document as docs_ingest_document
from vesmaro.embeddings import EmbeddingProvider, create_embedding_provider
from vesmaro.graph_minting import (
    AUTO_DEDUPE_CANDIDATE_POOL,
    AUTO_DEDUPE_EDGE_KIND,
    AUTO_DEDUPE_EDGE_WEIGHT,
    AUTO_DEDUPE_PROVENANCE,
    select_auto_dedupe_candidates,
)
from vesmaro.models import (
    CHECKPOINT_FIELDS,
    CHECKPOINT_PLACEHOLDERS,
    CHECKPOINT_SECTION_TITLES,
    CHECKPOINT_STAMP_KEYS,
    CONTEXT_ADMISSIBLE_STATUSES,
    NO_FEDERATE_TAG,
    AgentRecallQuery,
    Memory,
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    MemoryType,
    MemoryUpdate,
    PipelineState,
    SearchResult,
    checkpoint_canon_envelope,
    doc_grouping_from_metadata,
    is_context_admissible,
    is_quarantined,
    render_retraction,
)
from vesmaro.pipeline import (
    ClusterResult,
    PublishResult,
    QualityResult,
    SynthesisResult,
)
from vesmaro.policy.engine import PolicyAction
from vesmaro.storage.sqlite_store import (
    EDGE_STATS_KINDS,
    FTS_SNIPPET_ELLIPSIS,
    FTS_SNIPPET_END_MARK,
    FTS_SNIPPET_START_MARK,
    SQLiteStore,
)
from vesmaro.storage.vault import VaultManager
from vesmaro.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)

# Hard cap on redirect hops for per-hop SSRF re-validation (v2 posture).
# Each hop is validated by _validate_url before the next request is issued.
_MAX_REDIRECTS: int = 5


def _derive_feedback_event_id(
    event_id: str, memory_id: str, *, kind: str, project: str, agent: str
) -> str:
    """Deterministic per-row event id from the caller's logical report id.

    ADR-0030 A0 (issue #323, I5 idempotency): length-prefixed SHA-256
    over ``(event_id, memory_id, kind, project, agent)`` — injective
    for arbitrary caller strings (the ``compute_event_key`` pattern
    from context_rewrite: a delimiter scheme could be spoofed by an id
    containing the delimiter). Retrying the same report re-derives the
    same row ids, so ``INSERT OR IGNORE`` drops them — an agent retry
    contributes no double weight. The hash also keeps the raw caller
    string out of the PK namespace: a hostile caller cannot squat a
    predictable id shape.

    Review #338 N1: the preimage includes the reporting PRINCIPAL
    ``(project, agent)`` and the ``kind``. Hashing only
    ``(event_id, memory_id)`` let two different principals sharing one
    logical report id collide — the second agent's legitimate event was
    silently dropped as a "duplicate" — and likewise a ``used`` then
    ``rejected`` report under one caller id. The full tuple keeps
    idempotency EXACT: same (caller event, memory, principal, kind) →
    same row id (retry-stable); anything else → a distinct row.
    ``project``/``agent`` here are the NORMALIZED values that land in
    the row (``""`` for the explicit global mode) — the hash must be
    stable across processes, and the stored columns are the stable
    form.

    Compat (deliberate no-migration): rows stored before this change
    hashed the two-field preimage, so a retry of a pre-upgrade report
    inserts one fresh row instead of deduping. Accepted — the flag is
    default-off and the table is days old; capture-only, one inert
    clamped row.
    """
    canonical = (
        f"{len(event_id)}:{event_id}"
        f"{len(memory_id)}:{memory_id}"
        f"{len(kind)}:{kind}"
        f"{len(project)}:{project}"
        f"{len(agent)}:{agent}"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# heal_stale_embeddings (review P2): a dead/erroring embedder must not
# walk the whole refined set in one background tick. `limit` bounds the
# ATTEMPTS, and this cutoff stops the pass early when that many rows
# fail CONSECUTIVELY — the embedder is presumed down and one summary
# warning replaces the per-row flood.
HEAL_CONSECUTIVE_FAILURE_CUTOFF: Final[int] = 10

# ADR-0019 B2b (review #163 follow-up F7) — INTERNAL lifecycle metadata
# keys. The store writes them server-side (``json_set`` in
# ``record_refine_failure`` / ``clear_refine_retry``); an external
# ``update(metadata=...)`` replaces the dict wholesale, so the manager
# MERGES these keys back — a caller must never be able to reset a retry
# counter or forge backoff bookkeeping by rewriting metadata. The set is
# enumerated (not a ``pipeline_*`` prefix rule) so membership is
# auditable in one place; a new internal key must be listed here.
# mnemos #251 security review (P1): the server-minted checkpoint stamps
# (CHECKPOINT_STAMP_KEYS) join this set — once ``save_checkpoint`` minted
# them, no update path can rewrite or drop them. vesmaro-canon v1.0.0:
# ``"canon"`` (the metadata.canon envelope key) joined
# CHECKPOINT_STAMP_KEYS, so the envelope is equally server-minted —
# an external update cannot forge, rewrite or drop it either.
#
# NOT here, by design: ``"canon_warnings"`` (cascade review SEC P2-1 /
# QA P1-1). That key's SINGLE WRITER is ``_canon_gate`` — it attaches the
# fresh violation list on every add/update pass and REMOVES the key when
# the fresh list is empty (a fixing edit clears stale warnings). It is
# therefore never merged back on update (the gate re-derives it), and
# client-supplied copies are stripped on both the create and update
# paths together with the forged stamps — a client must neither forge
# warnings onto a clean record nor delete honest ones from a violating
# record; only an actual content/metadata fix may clear them.
INTERNAL_METADATA_KEYS: frozenset[str] = (
    frozenset(
        {
            "pipeline_retry_count",  # lane-(a) attempt counter (refine)
            "pipeline_retry_at",  # lane-(a) backoff gate (refine)
        }
    )
    | CHECKPOINT_STAMP_KEYS
)

# Cascade review SEC P2-2 (TL ruling: engine returns to ratified canon §2).
# ADR-0003 obligation 3 was read over-broad in W2-S1: canon §2 defines
# CLIENT-AUTHORED envelopes for the non-checkpoint types — task/decision/
# report carry extras (owner_slug/priority/size, reversible, period) the
# server cannot know, so those envelopes are client data and MUST persist
# through the generic create/update paths (the W3a pack docs, ADR-0004
# fixtures and the neighbor intake contract all assume they do). Only the
# CHECKPOINT envelope stays server-minted (save_checkpoint is its single
# minter), and a MALFORMED canon value (non-dict) is stripped like a
# forged stamp. The strip is therefore TYPE-CONDITIONAL at the call sites
# (canon_envelope_is_client_authored, defined in canon_validate so the
# JSON import strip shares the same type rule); the ``"canon"`` key stays
# inside CHECKPOINT_STAMP_KEYS for the checkpoint case and the malformed
# case (see models.py).


# mnemos #251 D0 — a session id is already bound to a different agent.
# Subclasses ValueError so every caller that already maps ValueError to a
# client error keeps working; the REST twin uses the subclass to answer
# 409 specifically.
class SessionAgentMismatchError(ValueError):
    """mnemos #251 D0 — a session id is already bound to a different agent."""


# mnemos #251 review (P3) — identity hygiene for the checkpoint channel.
# Agent: a slug (the bare-agent charset of the agent:<slug> tag contract,
# models._AGENT_RE, without the prefix). Session: bounded print-safe
# ASCII token (no whitespace/control chars) so a session id can never
# smuggle control characters or unbounded payloads into logs, the meta
# binding key, or metadata stamps.
_CHECKPOINT_AGENT_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_-]{1,64}$")
_CHECKPOINT_SESSION_RE: Final[re.Pattern[str]] = re.compile(r"^[\x21-\x7E]{1,128}$")


class _SSRFRejectionError(Exception):
    """Internal sentinel wrapping a ``ValueError`` from ``_validate_url``.

    Distinguishes an SSRF guard rejection (must be re-raised, never stored
    in memory) from operational ``ValueError``s raised inside the fetch
    loop (too-many-redirects, redirect-loop, missing Location) which are
    legitimate network errors and degrade to placeholder content.
    """

    def __init__(self, original: ValueError) -> None:
        super().__init__(str(original))
        self.original = original


def _lock_is_stale(locked_at_iso: str, threshold_hours: int) -> bool:
    """Return True if a workflow lock is older than ``threshold_hours``.

    Used by guardrail 2 (stale-lock auto-release). Tolerant of malformed
    timestamps — a value that fails to parse is treated as NOT stale so a
    corrupt ``locked_at`` cannot enable a silent takeover.
    """
    try:
        locked_at = datetime.fromisoformat(locked_at_iso)
    except ValueError:
        return False
    if locked_at.tzinfo is None:
        locked_at = locked_at.replace(tzinfo=UTC)
    age = datetime.now(UTC) - locked_at
    return age > timedelta(hours=threshold_hours)


# A9 (ArchCom 2026-08-27) — vector-leg over-fetch factor. The store is
# queried ``limit * 4`` deep so the pre-RRF project/status predicates
# (which drop candidates at resolve time, BEFORE fusion) still leave
# enough in-scope survivors to fill the leg's ``limit * 2`` RRF
# contribution depth. Depth compensation only — not a ranking change;
# tuning moves to the W4 D5 golden-set baseline per the committee
# decision (rejected alternative: post-RRF filter, which silently
# under-fills the top-N).
VECTOR_LEG_OVERFETCH_FACTOR: Final[int] = 4

# mnemos #282 — cap for the session-keyed first-assembly timestamp registry
# (``MemoryManager._retrieval_iso``). FIFO past the cap: the registry exists
# so a harness pinning an assembled block gets a byte-stable
# ``retrieved=<iso>`` across one session's assemblies; past 10_000 distinct
# session ids entries are evicted by FIRST-ASSEMBLY order (FIFO, not LRU —
# re-hits do not refresh position). A still-active old session evicted by a
# flood of new ones re-stamps on its next assembly — a mid-session prefix
# change of the same failure class as a process restart, accepted (the
# registry is in-memory and single-process anyway). dict insertion order is
# FIFO for free — no LRU bookkeeping.
RETRIEVAL_ISO_REGISTRY_CAP: Final[int] = 10_000

# ── ADR-0030 A1-S1 — the walk mechanics module (#325) ───────────────────────
# Committee-ratified constants (ArchCom 2026-09-27, verdict (a)/(c)): the
# composition rule ONLY applies on the flag-on path; the flag-off graph
# leg (the unconditional supersedes expansion) is byte-identical to A0.


# Reserved walk quota — k = min(ceil(limit/5), limit//2), 0 for limit < 3
# (a PURE function of ``limit``, no store state — the adaptive-floor
# alternative was rejected: it would make the composition a function of
# store state and break the ADR-0028 determinism line). ~20% headroom
# rent: a flag-on search never serves more than k walk-sourced rows at
# the fused block's expense; fused surplus fills any walk shortfall.
def _walk_quota(limit: int) -> int:
    """Reserved walk slots for a flag-on page (ADR-0030 A1-S1, #325)."""
    if limit < 3:
        return 0
    return min(-(-limit // 5), limit // 2)


# Static w_edge decay multiplier per depth layer: a depth-2 neighbour
# ranks at 0.7 of its depth-1 decay (ADR-0030 §3 "Ranking formula" —
# ``(1-alpha)/(rrf_k + 2*pos) x w_edge x 0.7^(depth-1)``). Depth 1
# multiplies by 1.0; the constant bounds the work so BFS-2's second
# layer can never outrank the first.
WALK_DEPTH_DECAY: Final[float] = 0.7

# BFS-2 fanout cap (ADR-0030 §3): per-node neighbour budget for the
# LAYERED expansion (depth-1 anchors expand in ONE batched
# ``WHERE from_memory_id IN (...)`` pass, then the depth-2 layer).
# Guards against hub rows (relates_to hubs with tag-sized fanout) —
# a hub anchor contributes at most this many neighbours per layer.
WALK_FANOUT_CAP: Final[int] = 32

# BFS-2 total-work cap (ADR-0030 §3): the hard ceiling on VISITED-ROW
# work across the whole layered expansion, independent of page size —
# bound the query-time cost as a function of the corpus, not of ``limit``.
WALK_TOTAL_WORK_CAP: Final[int] = 512

# ── ADR-0030 A1-S2 — feedback APPLY constants (#325, ArchCom 2026-09-27) ──────
# The pheromone line: at walk-fill time an edge's effective weight
# multiplies by ``w_eff = w_edge x (1 + min(used, CAP) x SLOPE)`` where
# ``used`` is the clamped per-neighbour `used` counter from the
# append-only edge_stats capture table (I5). SATURATING by construction
# (I6, THE Security residual): the factor plateaus at
# ``1 + CAP x SLOPE = 2.0`` however many events one principal floods —
# a 10k-cap flood moves a walked-block row's rank by at most the
# saturation bound, and rank moves never flip eligibility (gates run
# BEFORE the factor applies; rank-only is I6). CALIBRATION (A1 starting
# point, per the committee: "conservative values, measurable once
# capture flows"): CAP=10 says the FIRST ten real uses carry the
# signal — a citation proven valuable a handful of times is boosted to
# the ceiling; SLOPE=0.1 caps the whole mechanism at x2.0 — a
# used-heavy edge outranks its used-light sibling WITHIN the walked
# block but can never leapfrog the fused ranking, the depth decay, or
# the gate order. Both constants are revisitable at the A1 telemetry
# checkpoint (the junk-edge share measurement the verdict names); they
# change NOTHING structurally — any (CAP, SLOPE) pair keeps f(0)=1.0
# (the neutral factor) and f bounded (the saturation property), the
# two properties the pin tests lock.
FEEDBACK_BOOST_CAP: Final[int] = 10
FEEDBACK_BOOST_SLOPE: Final[float] = 0.1


def _feedback_boost_factor(used: int) -> float:
    """Saturating usage factor (ADR-0030 A1-S2, I6 bounded Δ).

    ``f(used) = 1 + min(used, FEEDBACK_BOOST_CAP) x FEEDBACK_BOOST_SLOPE``
    — f(0)=1.0 (neutral: a zero-capture store reproduces the flag-off
    ranking byte-identically), f monotone non-decreasing, f bounded at
    ``1 + CAP x SLOPE`` (saturation: no amount of capture moves one
    row's walked-block rank beyond the saturation bound). A PURE
    function of the clamped counter — the structural I6 guarantee.
    """
    return 1.0 + min(max(used, 0), FEEDBACK_BOOST_CAP) * FEEDBACK_BOOST_SLOPE


# Depth-2 attribution: a depth-2 neighbour's anchor is its entry
# neighbour's first anchor (first-anchor-wins across BOTH layers) —
# the ADR-0028 (score desc, id asc) tiebreak line holds unchanged.


@dataclass(frozen=True, slots=True)
class _WalkCandidate:
    """One walk-expansion candidate (ADR-0030 A1-S1).

    ``anchor_pos`` is the 1-based position of the candidate's FIRST
    anchor in the fused ranking (first-anchor-wins across both BFS
    layers); ``depth`` is the BFS layer (1 = edge of a fused anchor,
    2 = edge of a depth-1 neighbour); ``weight`` is the w_edge of the
    edge that DISCOVERED the candidate (the first-edge-wins value —
    post-gate ranking fuel only, never eligibility per I3);
    ``via_relates`` records whether the discovery edge was a
    ``relates_to`` edge (telemetry attribution per #324).
    """

    neighbour_id: str
    anchor_pos: int
    depth: int
    weight: float
    via_relates: bool


@dataclass(frozen=True, slots=True)
class IssuanceScan:
    """Outcome of an issuance-boundary secret scan (ADR-0018 P1-b M1).

    ``text`` is the string safe to issue: the redacted copy when
    secrets were found (and refuse mode is off), the input unchanged
    when clean, and ``""`` when ``refused`` — a refused outcome NEVER
    carries content, so callers branch on ``refused`` first.
    """

    text: str
    refused: bool
    # Machine-readable refusal cause: "secret detected" | "scanner error" | None.
    reason: str | None
    redactions: int
    redacted_patterns: dict[str, int]


@dataclass(frozen=True, slots=True)
class IssuanceItemScan:
    """Per-item outcome for ALL strings one result item echoes (P1-b F1).

    Security-review round F1: ``auto_title()`` derives from the first
    line of raw content (or echoes an explicitly-set title) and was
    returned unscanned next to redacted content — the title is now
    scanned as part of the same item. ``content``/``title`` are the
    strings safe to issue (``""`` when refused or when the item echoes
    no such field); ``redactions``/``redacted_patterns`` are the MERGED
    counts across both fields.
    """

    content: str
    title: str
    refused: bool
    reason: str | None
    redactions: int
    redacted_patterns: dict[str, int]


# B5 tier-2 (ArchCom 2026-08-27, W3) — the FTS5 snippet window is cut
# around query matches at a 32-token granularity, so a secret straddling
# the window edge reaches the snippet TRUNCATED and evades detection on
# the snippet text alone. The offset-mapped scan therefore runs on the
# ORIGINAL over the localized snippet window ± this character margin:
# generous by design — a wider margin only finds secrets whose
# intersection with the window is smaller (only intersections are
# redacted), while a narrow one re-opens the truncation gap. 64 chars
# covers the longest detector pattern tails (jwt) at both window edges.
SNIPPET_SCAN_MARGIN_CHARS: Final[int] = 64


@dataclass(frozen=True, slots=True)
class SnippetScanOutcome:
    """B5 tier-2 outcome of the offset-mapped scan for ONE snippet.

    * ``clean`` — no finding intersects the localized snippet window:
      the marked snippet is issued UNCHANGED (highlight marks intact).
    * ``redacted`` (``clean=False``, ``localizable=True``) — at least
      one finding intersects the window: ``text`` is the redacted
      snippet (mark-stripped fragments joined by the FTS ellipsis —
      highlight marks do not survive redaction, presentation-only
      loss), ``findings`` are the intersecting findings only.
    * fallback (``localizable=False``) — the snippet could not be
      uniquely localized in the original (or the original was absent):
      the caller must fall back to the tier-1 behavior and withhold the
      whole snippet (``<REDACTED:snippet>``); offsets are unmappable,
      so the snippet cannot be proven secret-free.
    """

    clean: bool
    localizable: bool
    text: str
    findings: tuple[SecretFinding, ...]


def _offset_mapped_snippet_scan(marked_snippet: str, original: str | None) -> SnippetScanOutcome:
    """B5 tier-2 — scan the ORIGINAL over the localized snippet window.

    Localization reuses the W1 CCR-stage approach (``ccr.parse_marker``
    span semantics): exact-substring localization of the snippet's
    fragment texts inside the original. FTS5's ``snippet()`` emits
    verbatim slices of the original joined by the ellipsis constant
    with query-matched tokens wrapped in highlight marks, so after
    removing ONLY the two highlight marks the ellipsis-split fragments
    are exact substrings (NOTE: unlike ``_snippet_scan_text``, the
    ellipsis is NOT stripped here — it is the fragment separator, and
    flattening it would fuse non-contiguous fragments into a text that
    never occurs in the original). Each fragment must localize UNIQUELY
    (exactly one occurrence in the original) — a repeated fragment is
    ambiguous, its offsets are unmappable, and the snippet falls back
    to tier-1 withholding (fail-closed, same shape as the m2
    marker-split case).

    The scan window is ``min(fragment start) - margin`` …
    ``max(fragment end) + margin`` clamped to the original; the scan and
    the intersection test run in WINDOW coordinates (fragment spans
    shifted by ``-window_start``), so the detector's own finding objects
    are reused verbatim — no offset reconstruction. Only findings
    INTERSECTING a fragment span are redacted (a finding fully inside
    the margin but outside the window is not echoed by the snippet) —
    the intersection is what the snippet actually shows.
    """
    from vesmaro.secrets_detector import detect_secrets

    fallback = SnippetScanOutcome(clean=False, localizable=False, text="", findings=())
    if original is None:
        return fallback

    stripped = marked_snippet.replace(FTS_SNIPPET_START_MARK, "").replace(FTS_SNIPPET_END_MARK, "")
    fragments = [f for f in stripped.split(FTS_SNIPPET_ELLIPSIS) if f]
    if not fragments:
        return fallback

    spans: list[tuple[int, int]] = []
    for fragment in fragments:
        start = original.find(fragment)
        if start < 0 or original.find(fragment, start + 1) >= 0:
            return fallback
        spans.append((start, start + len(fragment)))

    window_start = max(0, min(s for s, _ in spans) - SNIPPET_SCAN_MARGIN_CHARS)
    window_end = min(len(original), max(e for _, e in spans) + SNIPPET_SCAN_MARGIN_CHARS)
    window_spans = [(fs - window_start, fe - window_start) for fs, fe in spans]
    findings = detect_secrets(original[window_start:window_end])

    intersecting = [
        f for f in findings if any(f.start < we and f.end > ws for ws, we in window_spans)
    ]
    if not intersecting:
        clean_outcome = SnippetScanOutcome(
            clean=True, localizable=True, text=marked_snippet, findings=()
        )
        return clean_outcome

    # Redact each fragment's intersecting sub-spans in place. Findings
    # from detect_secrets are disjoint and start-sorted; intersections
    # clamp to the fragment bounds (a straddling secret is redacted only
    # in the portion the snippet actually shows).
    redacted_parts: list[str] = []
    for fragment, (ws, we) in zip(fragments, window_spans, strict=True):
        pieces: list[str] = []
        cursor = 0
        for finding in intersecting:
            if finding.start >= we or finding.end <= ws:
                continue
            isect_start = max(finding.start, ws) - ws
            isect_end = min(finding.end, we) - ws
            pieces.append(fragment[cursor:isect_start])
            pieces.append(f"<REDACTED:{finding.pattern_name}>")
            cursor = max(cursor, isect_end)
        pieces.append(fragment[cursor:])
        redacted_parts.append("".join(pieces))

    redacted = SnippetScanOutcome(
        clean=False,
        localizable=True,
        text=FTS_SNIPPET_ELLIPSIS.join(redacted_parts),
        findings=tuple(intersecting),
    )
    return redacted


class MemoryManager:
    """Central coordinator for all memory operations."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.resolve_paths()
        settings.apply_runtime_env()
        self.sqlite = SQLiteStore(settings.db_path)
        self.vault = VaultManager(settings.mnemos.vault_path)
        self.vectors = VectorStore(settings.mnemos.data_dir)
        self._embedder: EmbeddingProvider | None = None
        # ADR-0032 slice 6 — the project-graph wiring: the sidecar
        # service is built LAZILY on first use (get_codegraph_service,
        # behind the master ``code_graph.enabled`` flag) and the watch
        # poll scheduler on the first ``watch_start`` (behind the
        # ``code_graph.watch`` flag — default ON, inert until an
        # explicit registration, contract §3.2).
        # PG-0.5: the SAME scheduler hosts the native auto-indexer's
        # one-shot drain jobs (one cooperative thread for both).
        self._graph_watch: GraphWatchScheduler | None = None
        self._graph_autoindex: AutoIndexer | None = None
        # In-memory search instrumentation (resets on restart).
        # Accepted trade-off for the dashboard: not persisted, no history.
        self._search_stats: dict[str, Any] = {
            "requests_total": 0,
            "cross_project_requests_total": 0,
            # Search v2 (issue #313): scoped searches that returned 0
            # rows and were retried without the scope (soft fallback).
            # Distinct from cross_project_requests_total: a fallback is a
            # DRIFT signal (the scoped data lives under another slug),
            # not an explicit global-mode request.
            "project_scope_fallback_total": 0,
            # ADR-0030 A0 (issue #324, review fix) — enrichment split
            # by SOURCE LEG, never a single conflated numerator: the
            # unconditional supersedes leg (v1 #315, always on) and the
            # flag-gated relates_to walk (#324) each count their own
            # requests. The A0-review's acceptance share is the WALK
            # counter — a default deployment (flag off) reads 0 there
            # however busy the supersedes leg is. Both count the merged
            # page (a request enriched by both legs counts in both).
            "graph_supersedes_enriched_requests_total": 0,
            "graph_walk_enriched_requests_total": 0,
            # Ф2 (epic #308, ADR-0027 Phase 2) — the owner's standing
            # comparative metric: task-scoped queries SPLIT BY SWITCHER
            # FORM on the search surface. ``task_param`` counts the
            # first-class ``task=`` parameter; ``task_tag`` counts the
            # raw ``tags=["task:…"]`` form (a task= call mints the tag
            # through the same filter and therefore also lands in the
            # tag counter — the PARAM counter is the param-form share,
            # the TAG counter is the total task-scoped volume, and
            # task_tag - task_param is the tag-only share). The F1
            # A==C doctrine pins the two forms equivalent; these
            # counters watch ADOPTION drift between them (which form
            # callers prefer), not semantic drift. Same in-memory
            # trade-off as the graph-leg counters above.
            "task_param_queries_total": 0,
            "task_tag_queries_total": 0,
            # ADR-0030 A1-S2 (#325) — the flag-ON telemetry hook: requests
            # whose walked block used feedback-influenced weights (at
            # least one row got a non-neutral f(used) factor). The S2
            # acceptance observable (committee: "flag-on only after
            # proven capture telemetry" — THIS counter is how capture
            # reaching APPLY is proven later). Flag OFF (or zero-capture
            # store) ⇒ 0 forever — same in-memory trade-off as the
            # graph-leg counters above.
            "feedback_boosted_queries_total": 0,
            "latency_samples_ms": [],
            "results_counts": [],
        }
        self._search_stats_lock = threading.Lock()
        # ADR-0030 A0 (issue #323) — in-memory feedback-capture window
        # counters (operational health of the capture leg: drops,
        # duplicates, errors). The VOLUME signal (captured totals) is
        # durable and read from the edge_stats table itself in
        # feedback_capture_stats() — it must survive restarts for
        # A0-review; these here reset with the process by design.
        self._feedback_stats: dict[str, int] = {
            "reports_total": 0,
            "duplicates_ignored_total": 0,
            "out_of_scope_dropped_total": 0,
            "cap_dropped_total": 0,
            "errors_total": 0,
        }
        self._feedback_stats_lock = threading.Lock()
        self._processor_thread: threading.Thread | None = None
        self._processor_stop: threading.Event | None = None
        # P1-5/T3: CCR cleanup cycle counter — cleanup runs every
        # `ccr_cleanup_interval_sec` (tracked in wall-clock time), not
        # every processor cycle, to avoid scanning the cache table
        # every `interval_sec` (default 120s).
        self._ccr_cleanup_last_ts: float = 0.0
        # ADR-0026 phase A — vitals sidecar (passive assemble metrics).
        # Guest contract: the plane NEVER blocks the server — runtime
        # failures degrade to no-ops, and even a broken deployment
        # (ImportError of the vendored subpackage) costs only a warning.
        try:
            from vesmaro.metrics.boundary import create_vitals_store

            self._vitals_store = create_vitals_store(settings)
        except ImportError:
            logger.warning(
                "vitals: metrics package import failed (broken deployment)"
                " — plane disabled (non-fatal)"
            )
            self._vitals_store = None
        self._vitals_retention_last_ts: float = 0.0
        self._vitals_rollup_last_ts: float = 0.0
        # ADR-0017 D1 (#125) — bounded registry for assemble_context
        # mode="async" results (handle -> (ContextBlock dict, session)).
        # In-memory, single-tenant, capped by
        # vesmaro.assemble.ASYNC_REGISTRY_CAP with oldest-first eviction;
        # entries are session-bound (review F1, CWE-863 — only the
        # assembling session may redeem a handle); a restart drops
        # pending handles (the harness re-asks — async is a latency
        # optimization, not storage).
        self._assemble_async: dict[str, tuple[dict[str, Any], str]] = {}
        self._assemble_async_lock: threading.Lock = threading.Lock()
        # mnemos #282 — session-keyed registry of FIRST-assembly timestamps
        # for the provenance ``retrieved=<iso>`` segment (see
        # :meth:`retrieval_iso`): stable within one session so the block
        # prefix stays byte-identical across the session's assemblies
        # (KV-cache friendly), distinct across sessions. In-memory and
        # single-tenant, capped by RETRIEVAL_ISO_REGISTRY_CAP with
        # oldest-first eviction — mirrors ``_assemble_async`` above; a
        # restart re-stamps every session on its next assembly.
        self._retrieval_iso: dict[str, str] = {}
        self._retrieval_iso_lock: threading.Lock = threading.Lock()
        # ADR-0030 A0 (issue #322) — in-memory relates_to auto-minting
        # instrumentation (same accepted trade-off as ``_search_stats``:
        # not persisted, no history, resets on restart — the minting RATE
        # the A0-review needs is derivable from the deltas). Keyed by
        # project slug ('' for unscoped writes); counts NEW edges only
        # (idempotent re-mints contribute nothing).
        self._graph_mint_stats: dict[str, int] = {}
        self._graph_mint_stats_lock: threading.Lock = threading.Lock()

    @property
    def embedder(self) -> EmbeddingProvider:
        if self._embedder is None:
            self._embedder = create_embedding_provider(self.settings.embedding)
        return self._embedder

    def close(self) -> None:
        self.stop_background_processor()
        autoindex = self._graph_autoindex
        if autoindex is not None:
            autoindex.close()  # drop queued hints before the scheduler stops
        watch = self._graph_watch
        if watch is not None:
            watch.close()  # stops the poll thread BEFORE the stores close
        from vesmaro.codegraph.service import close_graph_service

        # Close the (lazily built, weak-keyed) graph service WITHOUT
        # building one: its sidecar store + audit connections must not
        # live on GC after the manager is closed (review 10173a2a-2).
        close_graph_service(self)
        self.sqlite.close()
        self.vectors.close()
        vitals = self._vitals_store
        if vitals is not None:
            vitals.close()

    # ── helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _normalize_task_boundary(task: str) -> str:
        """Normalize + validate a bare task slug, minting ``task:<slug>``.

        Ф2 (epic #308, ADR-0027 Phase 2) — the ONE boundary authority
        shared by every ``task=`` surface (``search`` / ``recall_context``
        / ``list_recent`` / ``agent_recall`` reads and the
        ``save_checkpoint`` write): the ``task:`` prefix is minted HERE,
        never by the caller, and the slug is normalized through
        :func:`vesmaro.models.normalize_task_slug` (the #407 canon —
        the same normalization the project boundary applies). Fail-loud
        on an unsalvageable slug (a dead ``task:`` tag would be
        unreachable at query time, the #368 class) and on a
        prefix-carrying value (an actionable message points at the bare
        slug, mirroring ``assemble_context``'s Ф0 boundary).
        """
        from vesmaro.models import TASK_SLUG_RE, normalize_task_slug

        if task.startswith("task:"):
            raise ValueError(
                f"task must be the bare slug (got {task!r} — pass {task[len('task:') :]!r})"
            )
        normalized = normalize_task_slug(task)
        if not TASK_SLUG_RE.match(normalized):
            raise ValueError(
                f"task must match [a-z0-9_-]{{1,64}} after normalization (got {task!r})"
            )
        return f"task:{normalized}"

    @staticmethod
    def _embedding_text(memory: Memory) -> str:
        """Text representation used for embedding (title + content)."""
        parts = []
        if memory.title:
            parts.append(memory.title)
        parts.append(memory.effective_content())
        if memory.tags:
            parts.append(" ".join(memory.tags))
        return "\n".join(parts)[:4096]

    def embed_for(self, memory: Memory) -> list[float]:
        """Public helper — embed a memory into a vector.

        Thin wrapper over :meth:`_embedding_text` + :attr:`embedder` so
        callers (e.g. ``cli.sync.run_sync_import``) do not reach into
        the private ``_embedding_text`` symbol. A future refactor of the
        embedding-text shape only needs to update this method, not
        every call site.
        """
        return self.embedder.embed(self._embedding_text(memory))

    # ── ADR-0019 B2a: embed freshness stamping ────────────────────────────

    @staticmethod
    def _embed_content_hash(text: str) -> str:
        """Stable hash of the embedding INPUT text (the freshness key).

        Stamped into the vector-store metadata on every upsert and
        re-derived by :meth:`heal_stale_embeddings` — an embed whose
        stored hash differs from the hash of the row's current
        ``_embedding_text`` was cut from other content and is stale.
        """
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def _embedder_fingerprint(self) -> str:
        """Stable identity of the current embedder (the vector vintage key).

        Delegates to ``EmbeddingProvider.fingerprint`` (ADR-0021 embedder
        swaps: every vector cut by a different fingerprint is stale).
        Test doubles auto-create a non-str ``fingerprint`` attribute
        (MagicMock), so coerce anything non-string to a stable
        class-name identity — metadata must stay JSON-serializable on
        every write path and the comparison must stay well-defined.
        """
        fp = getattr(self.embedder, "fingerprint", None)
        if isinstance(fp, str) and fp:
            return fp
        return f"unidentified:{type(self.embedder).__name__}"

    def _vector_metadata(self, memory: Memory) -> dict[str, Any]:
        """Metadata blob for every vector upsert (project/agent/freshness).

        Single construction site for the embed metadata so the sweeper's
        freshness keys can never drift from what the writers stamp.
        Two freshness keys:

        * ``content_hash`` — the embedding INPUT text (ADR-0019 B2a);
          detects re-embeds needed after content edits.
        * ``model_fingerprint`` — the embedder identity (ADR-0021 round-3
          swap); detects re-embeds needed after a weights swap: content
          is unchanged, but the old-geometry vector no longer matches
          the space the queries are embedded into.
        """
        return {
            "project": memory.project,
            "agent": memory.agent,
            "content_hash": self._embed_content_hash(self._embedding_text(memory)),
            "model_fingerprint": self._embedder_fingerprint(),
        }

    def upsert_embedding(self, memory: Memory) -> None:
        """Embed ``memory`` and upsert it — the SINGLE embedding write point.

        Every path that (re-)embeds a row goes through here (publication,
        published-edit re-embed, the refine post-swap upsert, the heal
        sweeper, ``rebuild_vector_index``), so the ADR-0019 §Swap audit
        event ``embed_upserted`` fires exactly once per actual upsert and
        binds the stamped ``content_hash`` to that fact. Raises on
        embedder/vector failure — the caller owns the degradation policy
        (non-fatal everywhere: the sweeper heals).

        Search v2 (issue #313): the store row's ``embedding_id`` is
        stamped here, after the vector write succeeds. The VectorStore
        keys embeddings by the memory id, so ``embedding_id`` records
        "a live vector row exists for this memory" — the diagnostic
        join the pre-v2 write path never filled (live DB: NULL for
        1644/1644 rows). Non-fatal: a stamp failure is logged, never
        raised — the vector leg itself already resolved by id before
        this fix; the column is diagnostics, not a runtime dependency.
        """
        emb = self.embedder.embed(self._embedding_text(memory))
        metadata = self._vector_metadata(memory)
        self.vectors.upsert(memory.id, emb, metadata)
        logger.info(
            "embed_upserted: id=%s content_hash=%s",
            memory.id[:8],
            metadata["content_hash"],
        )
        try:
            self.sqlite.update_fields(memory.id, embedding_id=memory.id)
            memory.embedding_id = memory.id
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("embedding_id stamp failed (non-fatal) for %s: %s", memory.id[:8], exc)

    @staticmethod
    def _scan_and_tag(tags: list[str], content: str) -> tuple[list[str], dict[str, int] | None]:
        """Run the secrets scanner on ``content`` and auto-add no-federate.

        Federation defence-in-depth (Layer 1, ArchCom 2026-07-17 §2.2.1):
        if :func:`detect_secrets` finds a secret pattern AND the tag
        ``mnemos:no-federate`` is not already in ``tags``, it is appended so
        the record is excluded from all external exchange (batch sync +
        pull). Idempotent — a re-scan with the same secret does not
        duplicate the tag. Only pattern names and counts are logged;
        raw matched values never enter the log.

        Non-fatal: a scanner error returns the tags unchanged so the
        caller's write is never blocked by the scanner (Layer 2
        background scanner will catch it later).

        Args:
            tags: current tags list (not mutated; a new list is returned).
            content: text to scan; empty/None → tags returned unchanged.

        Returns:
            ``(tags, patterns)`` — ``patterns`` is the
            ``{pattern_name: count}`` mapping of the ingest scan verdict
            (``{}`` when clean), or ``None`` when the scanner errored
            (ADR-0019 Phase A ingest audit: scanner status + found
            patterns, correlated by memory id at the call site).
        """
        if not content:
            return list(tags), {}
        result = list(tags)
        try:
            from vesmaro.models import NO_FEDERATE_TAG
            from vesmaro.secrets_detector import detect_secrets, findings_by_pattern

            findings = detect_secrets(content)
            if findings and NO_FEDERATE_TAG not in result:
                result.append(NO_FEDERATE_TAG)
                logger.info(
                    "auto-tagged record with mnemos:no-federate "
                    "(patterns: %s) — raw values not logged",
                    findings_by_pattern(findings),
                )
            return result, dict(findings_by_pattern(findings))
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("Secrets scanner failed (non-fatal): %s", exc)
            return result, None

    # ── CRUD ────────────────────────────────────────────────────────────────

    @staticmethod
    def _publish_gate_detection(
        memory: Memory, *, path: str, content: str | None = None
    ) -> DetectionResult:
        """The Phase A danger gate over one candidate projection.

        Runs :func:`vesmaro.danger_detectors.detect` over the served
        projection and the title, emits the structured '``publish gate: …``'
        audit verdict line (correlated by ``memory_id[:8]``; pattern names
        and counts only, never raw values) and returns the raw
        :class:`~vesmaro.danger_detectors.DetectionResult` so callers that
        need the refusal CLASS (the B2b curated publication → lane-(b)
        quarantine reason) read it from the same single gate point
        instead of re-running ``detect`` themselves.

        Args:
            memory: The candidate snapshot (post-update state).
            path: Audit discriminator — ``direct-seed`` / ``ingest`` /
                ``status-flip`` / ``published-content-edit`` /
                ``curated-publish`` / ``federation-import``.
            content: Override for the text to scan. The update path
                passes the NEW content on a content edit — the row's
                ``clean_content`` is stale at that point (the filter
                has not re-run), so ``effective_content()`` would scan
                the OLD projection.
        """
        text = content if content is not None else memory.effective_content()
        detection = detect(text, memory.title)
        if detection.error is not None:
            logger.error(
                "publish gate: %s verdict=refused path=%s reason=scanner-error "
                "error=%s — record stays stored, publication refused",
                memory.id[:8],
                path,
                detection.error,
            )
        elif detection.positive:
            logger.warning(
                "publish gate: %s verdict=refused path=%s reason=danger-detector "
                "classes=%s patterns=%s — raw values not logged",
                memory.id[:8],
                path,
                sorted({f.detector_class for f in detection.findings}),
                detection.patterns_by_class(),
            )
        else:
            logger.info("publish gate: %s verdict=pass path=%s", memory.id[:8], path)
        return detection

    @staticmethod
    def _publish_gate_verdict(memory: Memory, *, path: str, content: str | None = None) -> bool:
        """ADR-0019 N1 (review #161 follow-up) — the Phase A danger gate,
        routed over every DIRECT publication flip.

        The pipeline's own gate lives in
        :func:`vesmaro.pipeline.publish.publish_memory`; this twin covers
        the paths that reach ``published`` WITHOUT that stage: a
        direct-seed ``MemoryCreate(status=PUBLISHED)``, status flips via
        :meth:`update`, content edits of already-published rows, and —
        since B2b — the ingest-time visibility decision of a
        status-less ``add`` (§2, ``path=ingest``). The gate logic itself
        is NOT duplicated — the verdict comes from the same
        :func:`vesmaro.danger_detectors.detect` over the served
        projection and the title, with the same fail-closed contract and
        the same '``publish gate: …``' audit-line convention (see
        :meth:`_publish_gate_detection`).

        Returns ``True`` when the publication is admissible (clean). On
        refusal the caller keeps the record stored zero-loss — the
        visibility transition is what gets refused, never the write; no
        exception propagates (``detect`` returns scanner errors inside
        its result).
        """
        detection = MemoryManager._publish_gate_detection(memory, path=path, content=content)
        return detection.error is None and not detection.positive

    def _canon_gate(self, memory: Memory, *, create_path: bool) -> None:
        """vesmaro-canon v1.0.0 (canon §9, ADR-0003 obligations 5-6) — the
        write-path canon enforcement gate, shared by ``add`` (create) and
        ``update``.

        Scope rule FIRST (canon §9 transitional — HIGHEST priority): a
        record WITHOUT ``metadata.canon`` is OUT OF CANON SCOPE — no
        validation, no warning, no rejection, in BOTH modes (pre-canon and
        imported/benchmark rows are legacy, not violations). Validation
        applies ONLY to records that carry the envelope — at ``add`` that
        means the trusted ``save_checkpoint`` path (which mints the
        checkpoint type), an internal caller that carries one deliberately,
        or, since cascade review SEC P2-2, a CLIENT-authored
        task/decision/report envelope (canon §2) persisting through the
        generic path.

        Modes (``vesmaro.mnemos.canon_mode`` section):

        * ``"warn"`` (default) — every violation is logged as ONE
          machine-parseable warning line (``canon_violation:`` + memory id
          + code + rule + detail, canon §9 telemetry format) and the
          violation dicts are attached to the stored record as
          ``metadata["canon_warnings"]``. The write ALWAYS succeeds.
          ``_canon_gate`` is the SINGLE writer of that key (cascade
          review SEC P2-1/QA P1-1): when the fresh violation list is
          EMPTY the gate REMOVES a stale ``canon_warnings`` — a fixing
          edit clears the warnings instead of leaving them stuck on the
          row forever.
        * ``"strict"`` — violations REJECT the write on the CREATE path
          ONLY (:class:`CanonViolationError`, a ``ValueError``). Update
          paths are warn-only (canon §10 "new records only" / ADR-0003
          obligation 6: the update of a pre-canon record must never fail
          retroactively).
        * ``"off"`` — no-op.

        The validator itself (:func:`vesmaro.canon_validate.validate_canon_record`)
        never raises; a crash inside it must not take the write down — the
        gate degrades to a non-fatal log line (same discipline as the
        secrets scanner and the embed upsert).
        """
        mode = self.settings.mnemos.canon_mode
        if mode == "off":
            return
        try:
            violations = validate_canon_record(
                content=memory.content,
                title=memory.title,
                metadata=memory.metadata,
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "canon validation failed (non-fatal): id=%s error=%s",
                memory.id[:8],
                exc,
            )
            return
        if not violations:
            # Cascade review SEC P2-1 / QA P1-1: the gate is the SINGLE
            # writer of ``canon_warnings`` — a fresh EMPTY violation list
            # REMOVES a stale key (the fixing-edit lifecycle). Without
            # this the gate early-returned and a corrected record kept
            # its warnings forever; a client could also not clear them
            # (the key is stripped from client metadata below/above).
            if "canon_warnings" in memory.metadata:
                memory.metadata = {
                    k: v for k, v in memory.metadata.items() if k != "canon_warnings"
                }
            return

        for v in violations:
            # Machine-parseable telemetry line (canon §9): the raw material
            # of the future strict-default decision. id + code + rule +
            # detail, one line per violation.
            logger.warning(
                "canon_violation: id=%s create_path=%s code=%s rule=%s detail=%s",
                memory.id[:8],
                create_path,
                v.code,
                v.rule,
                v.detail,
            )

        records = [v.as_dict() for v in violations]
        memory.metadata = {**memory.metadata, "canon_warnings": records}

        if mode == "strict" and create_path:
            raise CanonViolationError(violations)

    def add(
        self,
        data: MemoryCreate,
        *,
        project: str = "",
        agent: str = "",
        trusted_rewrite_provenance: bool = False,
        trusted_checkpoint_stamps: bool = False,
        mint_relates_to: bool = True,
    ) -> Memory:
        """Create a new memory entry.

        The M2 tag contract is enforced by the MCP layer (mcp_server.py).
        MemoryManager trusts validated project/agent passed in kwargs.

        Federation defence-in-depth (Layer 1, ArchCom 2026-07-17 §2.2.1):
        the write-path secrets scanner runs on ``data.content`` before
        persistence. If a secret is detected AND the record does not
        already carry ``mnemos:no-federate``, the tag is auto-added so the
        record is excluded from all external exchange (batch sync + pull).
        Idempotent: a re-add with the same secret does not duplicate the
        tag (the check is "already present → skip"). Only pattern names
        and counts are logged; raw matched values never enter the log.

        ``trusted_rewrite_provenance`` (C10 review round) is set ONLY by
        the rewrite-event path (``context_rewrite``): it lets
        ``SQLiteStore.save`` derive the denormalised
        ``rewrite_source``/``rewrite_session`` quota columns from
        ``data.metadata``. The generic create surfaces (REST/MCP/CLI)
        keep the default ``False`` — client-controlled metadata must
        never mint rewrite quota counters.

        ``trusted_checkpoint_stamps`` (mnemos #251 security review P1)
        is set ONLY by ``save_checkpoint``: the checkpoint identity
        stamps (``CHECKPOINT_STAMP_KEYS``) are server-minted. On every
        other path client-supplied copies are stripped from
        ``data.metadata`` with a warning — a forged
        ``checkpoint_dedup_key`` on a generic create must never satisfy
        a later genuine checkpoint dedup (CWE-346/345 spoofed source).
        vesmaro-canon v1.0.0 (ADR-0003 obligation 3): a CHECKPOINT-type
        ``"canon"`` envelope is equally server-minted — only
        ``save_checkpoint`` mints one, and a client-forged checkpoint
        envelope on a generic create never persists. Cascade review
        SEC P2-2 (canon §2): task/decision/report envelopes are
        CLIENT-authored data (extras the server cannot know) and DO
        persist through the generic paths, flowing into the canon gate
        (warn/strict apply to them). Cascade review SEC P2-1: a
        client-supplied ``canon_warnings`` is stripped on the same
        non-trusted paths — ``_canon_gate`` is the single writer of that
        key (see INTERNAL_METADATA_KEYS above).

        ``mint_relates_to`` (#322 review M2, TL decision) — minting
        fuel is ORGANIC USER WRITES only. Internal machine-driven
        ``add`` callers (checkpoint saves, mesh ingest, migrate import)
        pass ``False``: their content mechanically cites existing
        memories (checkpoints re-state what they reference; bulk
        imports re-state a whole corpus) and would flood the graph with
        high-cosine infra churn instead of near-duplicate signal. Every
        user-facing surface (MCP / REST / SDK / CLI add, the
        ``context_rewrite`` channel — the fuel target of ADR-0030) and
        the file watchers keep the default ``True``.

        ADR-0019 §2 (B2b) — for records created WITHOUT an explicit
        ``status``, the ``vesmaro.visibility`` policy decides the initial
        visibility through the same Phase A gate (see the ingest block
        below): ``immediate`` publishes clean content at once (refused
        content is stored RAW, invisible, zero-loss); ``curated`` holds
        the row RAW+pending until the refine cycle gates the refined
        projection. An explicit ``status=`` keeps the pre-B2b contract.
        """
        # ── ADR-0027 Phase 0 (epic #308, slice-1 review item 1): ────────
        # write-side doc-grouping validation, the AUTHORITATIVE gate.
        # MemoryCreate already validates at the DTO boundary (REST gets a
        # 422 there); this second check is the store-boundary defence that
        # catches every remaining add() caller — a partial
        # ``{doc_id, chunk_idx, heading_path}`` triple must never persist,
        # because the Phase-3 docs-as-memory reader groups by ``doc_id``
        # and a silent split would be untraceable. No convention keys →
        # no-op (the metadata column stays free-form for everything
        # else). Raises ValueError (fail-loud, never normalizes).
        doc_grouping_from_metadata(data.metadata)

        # ── mnemos #251 review P1: strip client-forgeable stamps ────────
        # vesmaro-canon v1.0.0: the CHECKPOINT envelope is a stamp too —
        # a client-forged checkpoint-type metadata.canon must never land
        # on a generic create; only save_checkpoint mints it (canon §2,
        # ADR-0003 obligation 3, the same server-minted-only discipline).
        # Cascade review SEC P2-1: ``canon_warnings`` joins the strip set
        # — the gate is its single writer, so a client cannot forge
        # warnings onto a clean generic create either.
        # Cascade review SEC P2-2 (canon §2): the strip over ``canon`` is
        # TYPE-CONDITIONAL — task/decision/report envelopes are CLIENT
        # data (extras the server cannot know) and PERSIST, flowing into
        # the canon gate for validation/warnings (strict mode is now
        # meaningful for those types on the create path). Malformed canon
        # (non-dict) strips whole, like a forged stamp.
        if not trusted_checkpoint_stamps:
            strip_keys = CHECKPOINT_STAMP_KEYS | {"canon_warnings"}
            if canon_envelope_is_client_authored(data.metadata.get("canon")):
                strip_keys = strip_keys - {"canon"}
            forged = sorted(k for k in strip_keys if k in data.metadata)
            if forged:
                logger.warning(
                    "generic create: stripped client-supplied checkpoint stamps "
                    "(server-minted only, mnemos #251 review P1 + canon v1.0.0): "
                    "keys=%s",
                    forged,
                )
                data.metadata = {k: v for k, v in data.metadata.items() if k not in strip_keys}

        # ── ADR-0027 Ф3 (review round P3-1): the doc-sweep stamp is ────
        # server-minted too — only sweep_document_chunks may write
        # ``doc_swept_at`` (the explicit-release audit trail). A client
        # forging it on a generic create would fabricate a release audit
        # entry; the same #251 strip class. No trusted-caller flag exists:
        # the sweep writes through the STORE's update_fields (server-
        # internal), never through add().
        doc_forged = sorted(k for k in DOC_SWEEP_STAMP_KEYS if k in data.metadata)
        if doc_forged:
            logger.warning(
                "generic create: stripped client-supplied doc-sweep stamps "
                "(server-minted only, ADR-0027 Ф3 review P3-1): keys=%s",
                doc_forged,
            )
            data.metadata = {
                k: v for k, v in data.metadata.items() if k not in DOC_SWEEP_STAMP_KEYS
            }

        # ── Layer 1: write-path secrets scanner ───────────────────────────
        # Run before Memory construction so the tag is part of the persisted
        # record from the first write (no second UPDATE needed). Non-fatal:
        # a scanner error must NOT block the write — the memory is still
        # saved, just without the no-federate marker (Layer 2 background
        # scanner will catch it later).
        tags, ingest_patterns = self._scan_and_tag(list(data.tags), data.content)

        # ADR-0017 D1 / #125 ArchCom addendum 1 — contentType metadata
        # captured at ingest: detect_profile runs once here and the binary
        # partition ("code" vs everything-else "prose") is persisted in
        # metadata so assemble_context(mode=code|prose) filters recall
        # candidates by the stored value instead of re-detecting on the
        # hot path. Non-fatal: on failure the memory is saved without the
        # key and recall falls back to on-the-fly detection (documented in
        # vesmaro.assemble).
        ingest_metadata: dict[str, Any] = dict(data.metadata)
        try:
            from vesmaro.filter.pipeline import detect_profile

            ingest_metadata["content_type"] = (
                "code" if detect_profile(data.content) == "code" else "prose"
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("content_type ingest capture failed (non-fatal): %s", exc)

        memory = Memory(
            content=data.content,
            title=data.title,
            tags=tags,
            source=data.source,
            source_url=data.source_url,
            memory_type=data.memory_type,
            metadata=ingest_metadata,
            category=data.category,
            status=data.status,
            filter_profile=data.filter_profile,
            project=project,
            agent=agent,
        )

        # ── ADR-0019 N1 (explicit flips) + B2b §2 (visibility policy) ────
        # ONE gate point for every publication decision that happens
        # outside the pipeline's publish stage (see
        # _publish_gate_verdict). B2b adds the ingest leg: for records
        # created WITHOUT an explicit ``status`` the ``vesmaro.visibility``
        # policy owns the initial visibility instead of the silent RAW
        # default:
        #   * explicit status=  → the pre-B2b contract is untouched
        #     (only a clean ``PUBLISHED`` direct-seed stays published;
        #     refusal demotes to RAW — zero-loss, fail-closed);
        #   * immediate (default, the owner's model) → the content IS
        #     the would-be served projection, so the SAME gate runs at
        #     ``path=ingest``: clean ⇒ PUBLISHED + pipeline_state=pending
        #     (§2: visible at once, refined asynchronously); refused or
        #     scanner error ⇒ RAW with pipeline_state=NULL (stored
        #     zero-loss, invisible, legacy semantics until someone
        #     re-gates it through a legal path);
        #   * curated ⇒ RAW + pipeline_state=pending — invisible until
        #     the refine cycle completes and the publication gate admits
        #     the refined projection (see _curated_publish).
        if "status" in (data.model_fields_set or set()):
            if memory.status == MemoryStatus.PUBLISHED and not self._publish_gate_verdict(
                memory, path="direct-seed"
            ):
                memory.status = MemoryStatus.RAW
        elif self.settings.mnemos.visibility == "curated":
            memory.status = MemoryStatus.RAW
            memory.pipeline_state = PipelineState.PENDING
        elif self._publish_gate_verdict(memory, path="ingest"):
            memory.status = MemoryStatus.PUBLISHED
            memory.pipeline_state = PipelineState.PENDING

        # ── Canon gate, create leg — BEFORE any persistence (cascade ─────
        # review SEC P3-1). Ordering precedent: the doc-grouping gate at
        # the top of add(). A strict-mode reject must leave NO trace of
        # the rejected content — previously the gate ran after the vault
        # markdown write, so the refused bytes stayed in the Obsidian
        # vault (a file the SQLite row never pointed at). Running it
        # above BOTH sinks (vault + SQLite) also means the gate-attached
        # ``canon_warnings`` land in the vault render together with the
        # stored row — one consistent write, not two views.
        self._canon_gate(memory, create_path=True)

        # Write to Obsidian vault
        try:
            file_path = self.vault.memory_to_file(memory)
            memory.file_path = str(file_path)
        except Exception as exc:
            logger.warning("Vault write failed (non-fatal): %s", exc)

        # Persist to SQLite (trusted_rewrite_provenance gates the C10
        # rewrite-column derivation — see the add docstring).
        self.sqlite.save(memory, trusted_rewrite_provenance=trusted_rewrite_provenance)

        # ADR-0019 Phase A ingest audit — one structured verdict per
        # write, correlated by memory id (correlates with the publish-gate
        # lines in vesmaro.pipeline.publish). Scanner status + found
        # pattern names/counts only; raw values never enter the log.
        logger.info(
            "ingest scan: id=%s scanner=%s patterns=%s — raw values not logged",
            memory.id[:8],
            "error" if ingest_patterns is None else "ok",
            ingest_patterns or {},
        )

        # M10: auto-filter on ingest if enabled. Non-fatal: on failure the
        # memory is still saved with raw content (clean_content stays None).
        if self.settings.mnemos.auto_filter and memory.content:
            try:
                self.apply_context_filter(memory.id, profile=data.filter_profile)
                reloaded = self.sqlite.get(memory.id)
                if reloaded is not None:
                    memory = reloaded
            except Exception as exc:
                logger.warning("Auto-filter failed (non-fatal) for %s: %s", memory.id, exc)

        # Only embed + index published memories in the vector store
        if memory.status == MemoryStatus.PUBLISHED:
            try:
                self.upsert_embedding(memory)
            except Exception as exc:
                logger.warning("Vector embed failed (non-fatal): %s", exc)

        # ── ADR-0030 A0 (issue #322): deterministic relates_to minting ──
        # Best-effort by contract (ADR-0030 Decision 2): the edge is an
        # enhancement, never part of the write contract — a minting
        # failure is logged and swallowed; the write itself MUST succeed.
        # Runs AFTER the embed upsert so the search legs see the new row
        # in the index it was just written to (the row itself is then
        # excluded as a candidate by id). ``mint_relates_to=False``
        # (review M2): internal machine writes never mint.
        if mint_relates_to:
            try:
                self._mint_relates_to_edges(memory)
            except Exception as exc:
                logger.warning("graph auto-mint failed (non-fatal) for %s: %s", memory.id[:8], exc)

        logger.info("add: id=%s project=%s agent=%s", memory.id[:8], project, agent)
        return memory

    def get(self, memory_id: str) -> Memory | None:
        """Read one memory by id — the DIRECT-ACCESS surface (§5 B2b).

        ADR-0019 §5: a quarantined row answers with the RETRACTION
        RENDER instead of its content. The returned object is a COPY —
        the stored row is untouched (zero-loss; the operator-side store
        read ``sqlite.get`` keeps serving the source to internal
        maintenance paths). Everything content-like is stripped from the
        copy (``content`` ← the render, ``clean_content``/``raw_content``
        → ``None`` — the immutable source must not leave through the
        ``include_raw`` drill-down either — and ``title`` → ``None``,
        titles being part of the served projection per the N1 gate
        rationale). Lifecycle metadata stays visible for forensics:
        ``id`` / ``status`` / ``pipeline_state`` / ``quarantine_reason``
        / timestamps. Same-ID semantics: retraction is a state of the
        same record, never a new tombstone row.
        """
        memory = self.sqlite.get(memory_id)
        if memory is None or not is_quarantined(memory):
            return memory
        logger.info(
            "retraction: id=%s reason=%s channel=get — content withheld",
            memory.id[:8],
            memory.quarantine_reason,
        )
        return memory.model_copy(
            update={
                "content": render_retraction(memory),
                "clean_content": None,
                "raw_content": None,
                "title": None,
            }
        )

    def _quarantine_retraction_for(self, payload: str) -> str | None:
        """§5 B2b — the retraction render when ``payload`` IS a
        quarantined row's content.

        Used by the CCR retrieve-original channel: the cache is
        content-addressed (sha256 of the text, no memory-id link), so a
        cached original that byte-matches a quarantined row's stored
        content, immutable source or served projection belongs to that
        row and must not be issued. Returns ``None`` when the payload
        belongs to no quarantined row.
        """
        rows = self.sqlite.list_by_pipeline_state(PipelineState.QUARANTINED, limit=1000)
        for memory in rows:
            if payload == memory.content or payload == memory.effective_content():
                return render_retraction(memory)
            if memory.raw_content is not None and payload == memory.raw_content:
                return render_retraction(memory)
        return None

    def update(self, memory_id: str, data: MemoryUpdate) -> Memory | None:
        memory = self.sqlite.get(memory_id)
        if not memory:
            return None

        previous_status = memory.status
        previous_metadata: dict[str, Any] = dict(memory.metadata)
        update_kwargs: dict[str, Any] = {}
        for field in (
            "content",
            "title",
            "tags",
            "memory_type",
            "metadata",
            "status",
            "category",
            "quality_score",
            "confidence",
            "cluster_id",
        ):
            val = getattr(data, field, None)
            if val is not None:
                setattr(memory, field, val)
                update_kwargs[field] = val

        # ── ADR-0019 B2b (review #163 F7): internal lifecycle keys ────
        # An external ``metadata=`` replaces the dict wholesale; the
        # server-owned pipeline bookkeeping (retry counter / backoff gate,
        # see INTERNAL_METADATA_KEYS) is merged back on top so a caller
        # can neither reset a retry budget nor forge backoff state.
        if "metadata" in update_kwargs:
            # mnemos #251 review P1: checkpoint stamps are server-minted —
            # drop any client-supplied copies BEFORE the merge-back so
            # they cannot land on a row that never had them either.
            # vesmaro-canon v1.0.0: the CHECKPOINT envelope is a stamp
            # too — an external update can neither forge nor drop a
            # minted checkpoint metadata.canon (the merge-back below
            # restores it). Cascade review SEC P2-2 (canon §2): the strip
            # over ``canon`` is TYPE-CONDITIONAL — a task/decision/report
            # envelope is CLIENT data and persists (the corrected
            # envelope of a task row is exactly the client's to send).
            # Cascade review SEC P2-1: ``canon_warnings`` is stripped the
            # same way and is deliberately NOT merged back — the canon
            # gate (update leg, below) is its single writer and re-derives
            # it from the fresh violation list, so a client can neither
            # forge warnings nor delete honest ones; only an actual fix
            # (fresh empty list) clears them.
            strip_keys = CHECKPOINT_STAMP_KEYS | {"canon_warnings"}
            if canon_envelope_is_client_authored(memory.metadata.get("canon")):
                strip_keys = strip_keys - {"canon"}
            forged = sorted(k for k in strip_keys if k in memory.metadata)
            if forged:
                logger.warning(
                    "update: stripped client-supplied checkpoint stamps "
                    "(server-minted only, mnemos #251 review P1 + canon v1.0.0): "
                    "id=%s keys=%s",
                    memory_id[:8],
                    forged,
                )
                memory.metadata = {k: v for k, v in memory.metadata.items() if k not in strip_keys}
            # ADR-0027 Ф3 (review round P3-1): the doc-sweep stamp joins
            # the server-minted class — strip a client-supplied
            # ``doc_swept_at`` (a forged release audit entry); the merge
            # back below restores the row's own minted stamp (a client
            # metadata dict cannot erase it either). The sweep writes
            # through the STORE's update_fields, never through update().
            doc_forged = sorted(k for k in DOC_SWEEP_STAMP_KEYS if k in memory.metadata)
            if doc_forged:
                logger.warning(
                    "update: stripped client-supplied doc-sweep stamps "
                    "(server-minted only, ADR-0027 Ф3 review P3-1): "
                    "id=%s keys=%s",
                    memory_id[:8],
                    doc_forged,
                )
                memory.metadata = {
                    k: v for k, v in memory.metadata.items() if k not in DOC_SWEEP_STAMP_KEYS
                }
            internal = {
                k: previous_metadata[k] for k in INTERNAL_METADATA_KEYS if k in previous_metadata
            }
            # Cascade review SEC P2-2: the ``canon`` merge-back is
            # type-conditional on the ROW's own envelope. Only the
            # server-minted checkpoint envelope is merge-protected (a
            # checkpoint row stays a checkpoint row); when the row itself
            # carries a client-type envelope (task/decision/report), the
            # client's replacement — corrected envelope or absence —
            # stands, exactly like any other client metadata key.
            if "canon" in internal and canon_envelope_is_client_authored(internal["canon"]):
                del internal["canon"]
            memory.metadata = {**memory.metadata, **internal}
            # ADR-0027 Ф3 (review round P3-1): restore the row's own
            # minted doc-sweep stamp on top of the replacement dict —
            # a client metadata dict can neither forge nor erase it
            # (the same merge-protect the checkpoint stamps get).
            memory.metadata = {
                **memory.metadata,
                **{k: previous_metadata[k] for k in DOC_SWEEP_STAMP_KEYS if k in previous_metadata},
            }
            # ADR-0027 Phase 0 (slice-1 review item 1) — the update twin
            # of the add() gate: the REPLACEMENT metadata dict must not
            # persist a partial doc-grouping triple. Validated AFTER the
            # merge-back so the final row state is what passes (internal
            # keys never overlap the convention keys — the check is about
            # the caller's dict, but the row is what would lie to the
            # Phase-3 reader). Raises ValueError before any write.
            doc_grouping_from_metadata(memory.metadata)

        # ── Layer 1: write-path secrets scanner (update path) ──────────────
        # Re-run the scanner when the update payload includes new content so
        # the path-scoped re-ingest path (.instructions.md edited → update
        # with new content) does not bypass the scanner. Idempotent: if the
        # tag is already present, _scan_and_tag does not duplicate it.
        scan_patterns: dict[str, int] | None = None
        if "content" in update_kwargs:
            memory.tags, scan_patterns = self._scan_and_tag(memory.tags, memory.content)

        # ── ADR-0019 N1: direct-flip / published-edit publication gate ────
        # Direct paths that leave the row CONTEXT-ADMISSIBLE without the
        # pipeline's publish stage, all routed through the SAME Phase A
        # danger gate (no duplicated gate logic — see
        # _publish_gate_verdict):
        #   (b) an explicit status flip INTO PUBLISHED — on refusal the
        #       status simply does not change (stays previous). A flip
        #       into PROCESSED is deliberately NOT this gate's business:
        #       that transition is the knowledge pipeline's own advance
        #       (the context-rewrite originals carry redact-at-issuance
        #       secrets by contract — see the rewrite redemption tests);
        #   (c) a content or TITLE edit of an ALREADY-admissible row —
        #       PUBLISHED, and since issue #171 also PROCESSED (the
        #       other member of CONTEXT_ADMISSIBLE_STATUSES: gating only
        #       the published half left the processed half editable
        #       without a re-gate, an asymmetric seam) — on refusal the
        #       new text is still stored (zero-loss) but the status is
        #       demoted to RAW: keeping an admissible status would keep
        #       serving the refused projection. Titles are part of the
        #       served projection (markers/echo paths render them) and
        #       the issuance scan does not screen titles for injections
        #       — the gate is the only guard there.
        # A no-op flip (status=PUBLISHED on an already-published row with
        # no content or title change) does not re-gate: nothing about
        # the served projection changes. Quarantined rows are exempt
        # (is_context_admissible): they are excluded from every issuance
        # path anyway and release_quarantine routes the fresh cycle
        # through the gate.
        status_flipped = data.status is not None and data.status != previous_status
        content_changed = "content" in update_kwargs
        title_changed = "title" in update_kwargs
        was_admissible = previous_status in CONTEXT_ADMISSIBLE_STATUSES
        flip_into_published = status_flipped and memory.status == MemoryStatus.PUBLISHED
        edit_of_admissible = (content_changed or title_changed) and is_context_admissible(memory)
        demoted_for_publication = False
        if flip_into_published or edit_of_admissible:
            path = "status-flip" if flip_into_published else "published-content-edit"
            # On a content edit the NEW content is the projection that
            # would be served — clean_content is reset below before the
            # save, so the gate must not scan the old text. On a
            # title-only edit effective_content() is the current
            # projection (content unchanged) and the NEW title is
            # scanned via the memory snapshot.
            gate_content = memory.content if content_changed else None
            if not self._publish_gate_verdict(memory, path=path, content=gate_content):
                if was_admissible and (content_changed or title_changed):
                    memory.status = MemoryStatus.RAW
                    demoted_for_publication = True
                else:
                    memory.status = previous_status

        # ── ADR-0019 B2b (review #163 F8): a content edit of a REFINED ──
        # row invalidates the artifact — the served projection is no
        # longer the refined output, so the row re-enters the refine
        # intake (pending). Issue #171 extends the same F8 semantics to
        # admissible rows with legacy NULL pipeline_state (never
        # enrolled — e.g. an explicit PROCESSED seed): their projection
        # is equally stale after an edit; non-admissible rows keep the
        # B1 backfill decision (raw/processing/archived stay NULL).
        # ``swap_key`` is deliberately LEFT in place: the new cycle
        # recomputes it from the new artifact, and the stale value is
        # what makes a no-op re-run of the OLD artifact detectable.
        # Only the CLEAN path requeues — a gate-demoted edit writes
        # RAW-status side effects only (the B1 invariant: N1 demotions
        # never touch pipeline_state).
        if (
            content_changed
            and not demoted_for_publication
            and (
                memory.pipeline_state == PipelineState.REFINED
                or (memory.pipeline_state is None and is_context_admissible(memory))
            )
        ):
            from_state = memory.pipeline_state.value if memory.pipeline_state else "none"
            memory.pipeline_state = PipelineState.PENDING
            logger.info(
                "pipeline: id=%s outcome=enqueued from=%s state=pending reason=content-edit",
                memory.id[:8],
                from_state,
            )

        # ── Issue #193: a content replace resets the filter projection ──
        # in the SAME transaction. effective_content() is ``clean_content
        # or content`` — a stale clean_content would keep serving the
        # OLD filtered text after the edit (the B2a swap discipline,
        # applied to the update path). Not re-filtered here on purpose:
        # apply_context_filter derives from ``raw_content or content``,
        # so for a row whose immutable source was materialised a re-run
        # would rebuild the OLD projection through the back door. The
        # next apply_context_filter / sweeper pass recomputes it.
        if content_changed:
            memory.clean_content = None

        memory.updated_at = datetime.now(UTC)
        # Canon gate, update leg — WARN-ONLY even in strict mode: canon §10
        # ("new records only") / ADR-0003 obligation 6 — the update of a
        # pre-canon record must never fail retroactively, and a record that
        # carries an envelope is corrected forward, never refused on edit.
        self._canon_gate(memory, create_path=False)
        self.sqlite.save(memory)

        # N1 demotion hygiene: the formerly-published projection's embed
        # is stale (the row is RAW now). Non-fatal — the resolve-time
        # status guard already keeps the row out of search results.
        if demoted_for_publication:
            try:
                self.vectors.delete(memory_id)
            except Exception as exc:
                logger.warning("Vector delete on publication demotion (non-fatal): %s", exc)

        # ADR-0019 Phase A ingest audit (update path) — same shape as the
        # add() verdict line, correlated by memory id.
        if "content" in update_kwargs:
            logger.info(
                "ingest scan: id=%s scanner=%s patterns=%s — raw values not logged",
                memory.id[:8],
                "error" if scan_patterns is None else "ok",
                scan_patterns or {},
            )

        # Re-embed if now published
        if memory.status == MemoryStatus.PUBLISHED:
            try:
                self.upsert_embedding(memory)
            except Exception as exc:
                logger.warning("Re-embed failed: %s", exc)

        return memory

    def delete(self, memory_id: str) -> bool:
        memory = self.sqlite.get(memory_id)
        if not memory:
            return False
        if memory.file_path:
            self.vault.delete_file(memory.file_path)
        self.vectors.delete(memory_id)
        return self.sqlite.delete(memory_id)

    # ── Workflow lifecycle (mnemos #96) ────────────────────────────────────
    #
    # Server-side enforcement of the workflow state machine. The MCP tool
    # (mnemos_workflow tool route) and the REST endpoints (/memories/{id}/workflow) are
    # thin wrappers over these three methods — the validation MUST live here
    # so no caller can bypass the state machine or the 5 guardrails:
    #   1. Audit log       — every transition recorded in memory_workflow_history
    #   2. Stale-lock      — a lock older than the threshold auto-releases
    #   3. Idempotent      — to == current is a no-op (skip, not error)
    #   4. Force-unlock    — force=true overrides another actor's lock
    #   5. Rate limit      — max N transitions per memory per minute
    #
    # Lock model: a lock (locked_by + locked_at) is acquired on transition
    # to in-progress, persists through blocked/resolved (same actor still
    # owns the work), and is released on transition to open/done/withdrawn.
    # A different actor must either wait for stale-lock release or use force.

    def workflow_set(
        self,
        memory_id: str,
        to: str,
        *,
        actor: str,
        reason: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        """Transition a memory's workflow status, enforcing the state machine.

        Args:
            memory_id: Target memory.
            to: Target ``WorkflowStatus`` value (validated against the enum).
            actor: Free-form actor id (Phase 1 weak identity — NO authn/authz).
            reason: Optional human-readable reason; **required when
                ``force=True``** (guardrail 4).
            force: When True, override a lock held by another actor
                (guardrail 4). ``force_used=1`` is recorded in the audit log.

        Returns:
            Result dict describing the transition (see ``_workflow_result``).

        Raises:
            ValueError: On any guardrail violation (unknown status, locked by
                another actor without force, force without reason, rate-limit
                exceeded) or when the memory does not exist (re-checked at the
                current-state read to close the get()/get_workflow_status()
                TOCTOU window). The caller (MCP / REST) maps these to the
                appropriate client error.

        Audit note:
            Rejected transitions (forbidden edge, lock conflict, rate-limit,
            force-without-reason) write **no** audit row — the audit log
            records state changes, not attempts. Only recorded transitions
            (and the idempotent no-op short-circuit, which also writes
            nothing) touch the history table.
        """
        from vesmaro.workflow import WorkflowStatus, validate_transition

        # ── Input validation ──────────────────────────────────────────────
        if not actor or not actor.strip():
            raise ValueError("actor is required (Phase 1 weak identity — free-form string)")
        actor = actor.strip()
        try:
            to_status = WorkflowStatus(to)
        except ValueError as exc:
            valid = sorted(s.value for s in WorkflowStatus)
            raise ValueError(f"invalid workflow status {to!r}. Valid: {valid}") from exc

        if force and not (reason and reason.strip()):
            raise ValueError("reason is required when force=True (guardrail 4: force-unlock)")

        # ── Memory existence ──────────────────────────────────────────────
        if self.sqlite.get(memory_id) is None:
            raise ValueError(f"memory {memory_id!r} not found")

        # ── Guardrail 5: rate limit ───────────────────────────────────────
        rate_limit = self.settings.mnemos.workflow_rate_limit_per_minute
        minute_ago = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        recent = self.sqlite.count_workflow_transitions_since(memory_id, minute_ago)
        if recent >= rate_limit:
            raise ValueError(
                f"rate limit exceeded: {recent} transitions on memory "
                f"{memory_id!r} in the last minute (limit: {rate_limit}/min, "
                f"guardrail 5). Wait or raise workflow_rate_limit_per_minute."
            )

        # ── Current state ─────────────────────────────────────────────────
        # NOTE: re-checked here (not asserted) because a concurrent delete
        # between the existence check above and this call would otherwise
        # surface as AssertionError (a 500 in REST) instead of the
        # documented ValueError (404/409). An assert would also be stripped
        # under ``python -O``, turning the race into a TypeError.
        current = self.sqlite.get_workflow_status(memory_id)
        if current is None:
            raise ValueError(f"memory {memory_id!r} not found")
        from_str = current["workflow_status"]
        from_status = WorkflowStatus(from_str) if from_str else None
        locked_by = current["locked_by"]
        locked_at_iso = current["locked_at"]

        # ── Guardrail 3: idempotent transitions ───────────────────────────
        # to == current is a no-op: we SKIP (do not write, do not record in
        # history). The audit log stays clean of polluting no-op polls; the
        # returned idempotent=True tells the caller nothing changed.
        if from_status is not None and from_status == to_status:
            return self._workflow_result(
                memory_id=memory_id,
                from_status=from_status,
                to_status=to_status,
                actor=actor,
                locked_by=locked_by,
                locked_at_iso=locked_at_iso,
                reason=reason,
                force_used=False,
                stale_lock_released=False,
                idempotent=True,
                recorded=False,
            )

        # ── State machine enforcement ─────────────────────────────────────
        # validate_transition raises WorkflowTransitionError (a ValueError
        # subclass) on a forbidden edge — e.g. blocked → done, or any edge out
        # of a terminal state. We let it propagate: WorkflowTransitionError IS
        # a ValueError, so the manager's ValueError contract (caught by the
        # MCP tool / REST layer) is honoured and the precise message survives.
        validate_transition(from_status, to_status)

        # ── Lock guardrails (2 + 4) ───────────────────────────────────────
        stale_threshold_h = self.settings.mnemos.workflow_stale_lock_threshold_hours
        force_used = False
        stale_lock_released = False
        previous_locked_by = locked_by
        lock_held_by_other = locked_by is not None and locked_by != actor
        if lock_held_by_other:
            # Guardrail 2: stale-lock auto-release. A lock older than the
            # threshold is treated as releasable — a different actor can
            # take over WITHOUT force. We log a warning and proceed.
            if locked_at_iso is not None and _lock_is_stale(locked_at_iso, stale_threshold_h):
                stale_lock_released = True
                logger.warning(
                    "workflow stale-lock release: memory %s locked by %r at %s "
                    "(>%dh), taken over by %r",
                    memory_id,
                    locked_by,
                    locked_at_iso,
                    stale_threshold_h,
                    actor,
                )
            # Guardrail 4: force-unlock. Explicit override; reason required
            # (validated above). force_used is recorded in the audit log.
            elif force:
                force_used = True
                logger.info(
                    "workflow force-unlock: memory %s lock held by %r overridden by %r (reason=%r)",
                    memory_id,
                    locked_by,
                    actor,
                    reason,
                )
            else:
                raise ValueError(
                    f"memory {memory_id!r} is locked by {locked_by!r}. "
                    f"Use force=True (with a reason) to override, or wait for "
                    f"stale-lock release (>{stale_threshold_h}h). "
                    f"(guardrails 2 + 4)"
                )

        # ── Compute new lock projection ───────────────────────────────────
        new_locked_by, new_locked_at = self._compute_lock_projection(
            to_status=to_status,
            actor=actor,
            current_locked_by=locked_by,
            current_locked_at_iso=locked_at_iso,
            force_used=force_used,
            stale_lock_released=stale_lock_released,
        )

        # ── Persist: columns + audit row ──────────────────────────────────
        now_iso = datetime.now(UTC).isoformat()
        self.sqlite.set_workflow_status(memory_id, to_status.value, new_locked_by, new_locked_at)
        self.sqlite.add_workflow_history(
            {
                "id": str(uuid.uuid4()),
                "memory_id": memory_id,
                "from_status": from_status.value if from_status else None,
                "to_status": to_status.value,
                "actor": actor,
                "reason": reason.strip(),
                "force_used": 1 if force_used else 0,
                "created_at": now_iso,
            }
        )
        logger.info(
            "workflow transition: memory=%s %s->%s actor=%s force=%s stale=%s",
            memory_id,
            from_status.value if from_status else "(unset)",
            to_status.value,
            actor,
            force_used,
            stale_lock_released,
        )

        return self._workflow_result(
            memory_id=memory_id,
            from_status=from_status,
            to_status=to_status,
            actor=actor,
            locked_by=new_locked_by,
            locked_at_iso=new_locked_at,
            reason=reason,
            force_used=force_used,
            stale_lock_released=stale_lock_released,
            previous_locked_by=previous_locked_by,
            idempotent=False,
            recorded=True,
        )

    def workflow_get(self, memory_id: str) -> dict[str, Any] | None:
        """Return the current workflow projection for a memory.

        Returns ``None`` when the memory does not exist. The ``workflow_status``
        is normalised to ``open`` when the memory has never had its workflow
        set (legacy / freshly created), so callers always see a valid state.
        """
        current = self.sqlite.get_workflow_status(memory_id)
        if current is None:
            return None
        status = current["workflow_status"]
        return {
            "memory_id": memory_id,
            "workflow_status": status if status is not None else "open",
            "locked_by": current["locked_by"],
            "locked_at": current["locked_at"],
        }

    def workflow_history(self, memory_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return the workflow transition audit log for a memory (newest first).

        Returns an empty list if the memory has no recorded transitions
        (including when the memory does not exist — history is a projection
        of past events, not a memory existence check).
        """
        return self.sqlite.get_workflow_history(memory_id, limit=limit)

    # ── Workflow helpers ─────────────────────────────────────────────────

    @staticmethod
    def _compute_lock_projection(
        *,
        to_status: Any,
        actor: str,
        current_locked_by: str | None,
        current_locked_at_iso: str | None,
        force_used: bool,
        stale_lock_released: bool,
    ) -> tuple[str | None, str | None]:
        """Compute the new (locked_by, locked_at) for a target status.

        - in-progress acquires the lock (sets owner to ``actor``).
        - blocked / resolved keep the lock (same actor still owns the work);
          on a takeover (force / stale) the owner becomes ``actor`` and the
          timestamp is refreshed so the stale-lock clock restarts.
        - open / done / withdrawn release the lock.

        Returns explicit values for ``set_workflow_status`` to write — never
        relies on the DB to "keep" the old value, because
        ``set_workflow_status`` overwrites both columns unconditionally.
        """
        from vesmaro.workflow import WorkflowStatus

        now_iso = datetime.now(UTC).isoformat()
        if to_status == WorkflowStatus.IN_PROGRESS:
            return actor, now_iso
        if to_status in (WorkflowStatus.BLOCKED, WorkflowStatus.RESOLVED):
            if force_used or stale_lock_released:
                return actor, now_iso
            return current_locked_by, current_locked_at_iso
        # open / done / withdrawn → release
        return None, None

    @staticmethod
    def _workflow_result(
        *,
        memory_id: str,
        from_status: Any,
        to_status: Any,
        actor: str,
        locked_by: str | None,
        locked_at_iso: str | None,
        reason: str,
        force_used: bool,
        stale_lock_released: bool,
        idempotent: bool,
        recorded: bool,
        previous_locked_by: str | None = None,
    ) -> dict[str, Any]:
        """Build the uniform result dict returned by workflow_set."""
        from vesmaro.workflow import is_terminal

        return {
            "memory_id": memory_id,
            "from_status": from_status.value if from_status else None,
            "to_status": to_status.value,
            "actor": actor,
            "previous_locked_by": previous_locked_by,
            "locked_by": locked_by,
            "locked_at": locked_at_iso,
            "stale_lock_released": stale_lock_released,
            "force_used": force_used,
            "idempotent": idempotent,
            "recorded": recorded,
            "reason": reason.strip() if reason else "",
            "terminal": is_terminal(to_status),
        }

    # ── Search ──────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        *,
        tags: list[str] | None = None,
        project: str | None = None,
        agent: str | None = None,
        task: str | None = None,
        status: MemoryStatus | None = None,
        limit: int = 20,
        hybrid_alpha: float | None = None,
        include_raw: bool = False,
        refined_only: bool = False,
    ) -> list[SearchResult]:
        """Hybrid search: FTS5 + vector + Reciprocal Rank Fusion + graph leg.

                Ф2 (epic #308, ADR-0027 Phase 2) — ``task``: the first-class
                task-scope switcher, the bare slug (the ``task:`` prefix is
                minted HERE, at the boundary — a value already carrying it is
                rejected with an actionable message). Semantics are BYTE-IDENTICAL
                to ``tags=["task:<slug>"]`` (the F1 arm-C surface, the A==C
                equivalence doctrine the F1 experiment measured and this wave
                pins): both forms thread the SAME ``tags`` filter through every
                leg, so ``task="x"`` ≡ ``tags=["task:x"]`` on every axis
                (soft-fallback exemption included). Normalized at the boundary
                via :func:`vesmaro.models.normalize_task_slug` (the #407 canon —
                one normalization shared with the save path); an unsalvageable
                slug raises ``ValueError`` fail-loud, never a silently-different
                task scope. Combined with ``tags=`` the two INTERSECT (both must
                hold — the scope-hierarchy doctrine; passing ``task="x"`` and a
                ``tags`` list already carrying ``task:x`` is the identity, any
                OTHER ``task:`` tag in ``tags`` is a strict-AND no row can
                satisfy — a SILENT EMPTY result on this READ path: reads run no
                tag-contract validation; the always-fatal multiple-``task:``
                check binds to the WRITE paths only, where the contract
                validates).

                A9 (ArchCom 2026-08-27) — project scoping is PRE-RRF on BOTH legs:
                the FTS leg passes ``project`` to ``fts_search`` as before, and the
                vector leg now applies a native store-level project predicate
                (embedding metadata) plus an authoritative resolve-time guard on
                the SQLite ``Memory.project``, both BEFORE fusion — out-of-project
                rows never surface and never consume RRF rank slots. ``project``
                of ``None`` (or empty) is the EXPLICIT global mode: the search is
                cross-project by definition and is counted in
                ``search_stats()["cross_project_requests_total"]`` for audit.

                A9-completion (epic #308 Ф1-PREP, #360 review) — the AGENT
                predicate: native on the FTS leg only; the vector resolve loop
                and the graph leg carry the authoritative resolve-time mirror on
                the SQLite ``Memory.agent``, so ``agent=``-scoped searches never
                surface other agents' rows through a stale embed or an edge
                neighbour. No native VectorStore agent predicate by TL decision
                (the store stays project-only; scoping guards resolve here).

                Search v2 (issue #313) — PROJECT SOFT FALLBACK: a scoped search
                that returns ZERO rows is retried ONCE without the scope (the
                A9 pre-RRF predicate is unchanged — the fallback is a NEW OUTER
                retry, not a change to A9 semantics; the retry itself runs in the
                explicit global mode). Results that surface ONLY via the retry
                carry ``project_scope_fallback=True`` so the caller can see they
                are cross-project relative to the original request, and the event
                is counted in ``search_stats()["project_scope_fallback_total"]``.
                Deliberately NOT retried: ``status``-drilled queries — a drill-
                down is the caller asserting the row's lifecycle, and an unscoped
                retry would resurface junk from other projects' lanes (the same
                status policy is KEPT on the retry; the guard here is that a
                scoped ``status=`` query returning zero is information, not a
                scope-drift candidate). ADR-0027 Phase 0 (#308) adds the same
                exemption for TASK-SCOPED queries — a ``tags`` filter carrying a
                ``task:`` tag asserts the task dimension of the scope hierarchy
                (project x agent x session x task, inheritance = INTERSECTION),
                and a task tag may only NARROW, never widen: the retry keeps the
                task tag while dropping the project scope, which would surface
                foreign-project rows into a task view. Zero rows in a task scope
                is information ("this task has no matching rows yet"), not scope
                drift.

                Graph leg v1 (issue #313): after RRF fusion, the top-``limit``
                fused ids are 1-hop expanded along ``memory_edges`` (BOTH
                directions of ``supersedes``); edge-sourced rows not already
                fused are appended with a decayed rank slot and
                ``via_graph=True`` provenance, passing the SAME gates as the
                fused rows on every axis: project (the A9 authoritative guard —
                an edge never widens a scope), agent (the A9-completion
                resolve-time mirror — same rule, Ф1-PREP epic #308), status
                (default set AND the explicit ``status=`` drill-down), quarantine
                (ADR-0019 §5) and refined_only (§4). Headroom-gated: the expansion runs only when
                the fused legs left room (a full fused page needs no
                enrichment); no edges → the leg is a no-op. ADR-0030 A0 (issue
                #324): with ``vesmaro.mnemos.graph_walk`` ON (default ON since the owner
        decision of 2026-09-28) the walk
                additionally expands ``relates_to`` neighbours under the
                identical gates and decay — invariants I1-I3 are pinned by
                tests/test_graph_walk_invariants.py.

                Status filtering precedence:
                  1. Explicit ``status`` — always wins (caller knows what they want).
                  2. ``include_raw=True`` — all statuses EXCEPT ``archived`` are
                     returned. ``archived`` means "intentionally hidden from normal
                     search" and is excluded unless the caller passes
                     ``status=MemoryStatus.ARCHIVED`` explicitly.
                  3. Default (``include_raw=False``, no ``status``) — only
                     ``published`` and ``processed`` memories surface, preserving the
                     documented "Only searches 'published' knowledge units by default"
                     contract.

                ADR-0019 §5 — on every status-filtered leg (2 and 3 above) the
                quarantine predicate composes with the allowed set AND holds
                absolutely on its own: ``pipeline_state='quarantined'`` rows are
                excluded from issuance REGARDLESS of status — an EXPLICIT
                ``status=`` drill-down included (quarantined rows carry
                status='published', and the external payloads carry no
                pipeline_state, so the caller could not even detect the
                contamination). Direct ``get`` by id stays the documented
                residual access until the B2 retraction render. The graph
                expansion path runs the SAME absolute quarantine predicate — an
                edge neighbour is never a quarantine side door.

                ADR-0019 §4 — ``refined_only=True`` additionally keeps only
                entries whose served projection is the refined one
                (``pipeline_state='refined'``); NULL/legacy pipeline_state rows
                never match. Composable with every status mode above.

                mnemos #400 — the QUERY boundary normalizes a non-empty
                ``project`` with :func:`vesmaro.models.normalize_project_slug`
                (single authority: the same normalization the checkpoint save
                boundary and the tag-contract lax mode apply), so a
                ``project='MyProject'`` filter hits the ``myproject`` rows instead
                of silently returning zero in-scope rows and tripping the #313
                soft fallback across projects. ``project=None``/empty stays the
                EXPLICIT global mode — untouched.
        """
        _t0 = time.monotonic()

        if project:
            from vesmaro.models import normalize_project_slug

            project = normalize_project_slug(project)

        # Ф2 (epic #308): mint the task tag at the boundary — the SAME
        # ``tags`` filter the F1 arm-C call used, so task= ≡ tags=["task:x"]
        # holds by construction (one code path, not two). The shared
        # boundary helper normalizes (the #407 canon) and fails loud on
        # an unsalvageable slug: it never becomes a dead ``task:`` tag
        # (unreachable at query time, #368 class).
        task_tag: str | None = None
        if task is not None:
            task_tag = self._normalize_task_boundary(task)
            # Intersection semantics (both must hold): the minted tag
            # joins the caller's tags list; a duplicate of the SAME tag
            # is the identity (all(...) membership). A DIFFERENT task:
            # tag there makes the filter a strict-AND no row can
            # satisfy — a silent EMPTY result on this READ path (reads
            # run no tag-contract validation); the always-fatal
            # multiple-task: check binds to the write paths only,
            # where the contract validates. Never silently resolved
            # here either way.
            tags = [*(tags or []), task_tag]

        # Ф2 comparative telemetry (the owner's standing metric): split
        # by switcher FORM on this surface — the task= parameter vs the
        # raw task-tag-in-tags= form. Same _search_stats pattern as the
        # graph-leg counters (in-memory, resets on restart); the
        # counters watch the two forms' usage share for drift between
        # them (the F1 A==C doctrine makes semantic drift impossible,
        # so what remains observable is ADOPTION drift).
        task_scoped = tags is not None and any(t.startswith("task:") for t in tags)
        with self._search_stats_lock:
            if task is not None:
                self._search_stats["task_param_queries_total"] = (
                    int(self._search_stats["task_param_queries_total"]) + 1
                )
            if task_scoped:
                self._search_stats["task_tag_queries_total"] = (
                    int(self._search_stats["task_tag_queries_total"]) + 1
                )

        scoped = self._search_core(
            query,
            tags=tags,
            project=project,
            agent=agent,
            status=status,
            limit=limit,
            hybrid_alpha=hybrid_alpha,
            include_raw=include_raw,
            refined_only=refined_only,
        )

        results = scoped
        scope_fallback_used = False
        # Search v2 (issue #313): project soft fallback — ZERO in-scope
        # rows on BOTH legs (pre-RRF predicates decided nothing survives)
        # → ONE retry without the scope. The retry keeps the SAME status /
        # include_raw / refined_only policy (no junk resurfacing); only the
        # project scope is dropped. Not triggered for the explicit global
        # mode (project=None is already unscoped) and not for status
        # drill-downs (see the docstring rationale). ADR-0027 Phase 0
        # (#308): also not for task-scoped queries — the retry would keep
        # the task tag while dropping the project scope, widening a task
        # view across projects (intersection doctrine: a task tag only
        # NARROWS, never widens). ``task_scoped`` is computed at the Ф2
        # boundary above — it covers BOTH switcher forms (task= and
        # tags=["task:..."]) identically, the equivalence doctrine.
        if project and not results and status is None and not task_scoped:
            logger.info(
                "search: project=%s scoped search returned 0 rows — "
                "retrying without the scope (soft fallback)",
                project,
            )
            results = self._search_core(
                query,
                tags=tags,
                project=None,
                agent=agent,
                status=status,
                limit=limit,
                hybrid_alpha=hybrid_alpha,
                include_raw=include_raw,
                refined_only=refined_only,
            )
            for r in results:
                r.project_scope_fallback = True
            scope_fallback_used = bool(results)

        # Record search instrumentation (in-memory, resets on restart).
        latency_ms = (time.monotonic() - _t0) * 1000.0
        with self._search_stats_lock:
            self._search_stats["requests_total"] = int(self._search_stats["requests_total"]) + 1
            # A9: ``project=None`` (or empty) is the EXPLICIT global mode —
            # a cross-project search. Surfaced in ``search_stats()`` for
            # audit (every project-scoped call stays out of this counter).
            if not project:
                self._search_stats["cross_project_requests_total"] = (
                    int(self._search_stats["cross_project_requests_total"]) + 1
                )
            if scope_fallback_used:
                self._search_stats["project_scope_fallback_total"] = (
                    int(self._search_stats["project_scope_fallback_total"]) + 1
                )
            # ADR-0030 A0 (issue #324, review fix): the enrichment
            # share SPLIT BY SOURCE LEG — ``via_graph`` alone conflates
            # the unconditional supersedes leg with the flag-gated
            # relates_to walk, so the walk counter would read non-zero
            # on a default deployment whenever supersedes edges enrich
            # pages. The rows carry their first-anchor discovery kind
            # (``via_graph_kind``); each leg counts the requests its
            # own rows enriched.
            if any(r.via_graph and r.via_graph_kind == "supersedes" for r in results):
                self._search_stats["graph_supersedes_enriched_requests_total"] = (
                    int(self._search_stats["graph_supersedes_enriched_requests_total"]) + 1
                )
            if any(r.via_graph and r.via_graph_kind == "relates_to" for r in results):
                self._search_stats["graph_walk_enriched_requests_total"] = (
                    int(self._search_stats["graph_walk_enriched_requests_total"]) + 1
                )
            samples: list[float] = self._search_stats["latency_samples_ms"]
            samples.append(latency_ms)
            # Cap samples to avoid unbounded growth in long-running processes.
            if len(samples) > 1000:
                del samples[: len(samples) - 1000]
            counts: list[int] = self._search_stats["results_counts"]
            counts.append(len(results))
            if len(counts) > 1000:
                del counts[: len(counts) - 1000]
        return results

    def _search_core(
        self,
        query: str,
        *,
        tags: list[str] | None,
        project: str | None,
        agent: str | None,
        status: MemoryStatus | None,
        limit: int,
        hybrid_alpha: float | None,
        include_raw: bool,
        refined_only: bool,
    ) -> list[SearchResult]:
        """One scoped pass of the hybrid search (FTS + vector + RRF + graph).

        ``MemoryManager.search`` is the public surface (soft-fallback
        orchestration + stats); this is the single-pass core it calls —
        with the project scope it was given, unchanged A9 pre-RRF
        predicates. Kept private: the fallback decision belongs to the
        orchestrator, not the core.
        """
        alpha = hybrid_alpha if hybrid_alpha is not None else self.settings.search.hybrid_alpha

        # Resolve the status filter applied to the FTS leg.
        # fts_search treats status=None as "no filter", so we only pass a
        # value when an explicit status was given. The include_raw/default
        # gating is applied post-hoc below (fts_search does not accept a
        # list of allowed statuses, and widening its signature is out of
        # scope for this fix).
        fts_status = status  # explicit status always wins

        # ── FTS leg ────────────────────────────────────────────────────────
        fts_pairs: list[tuple[Memory, float]] = []
        try:
            fts_pairs = self.sqlite.fts_search(
                query,
                limit=limit * 2,
                project=project,
                agent=agent,
                status=fts_status,
            )
        except Exception as exc:
            logger.warning("FTS search failed: %s", exc)

        # Default gating: when no explicit status was requested, restrict
        # FTS hits to the allowed set. ``include_raw`` widens the set to all
        # statuses except ``archived`` (archived = intentionally hidden from
        # normal search). An explicit ``status`` skips this post-hoc filter
        # entirely — ``fts_search`` already filtered on it.
        if status is None and not include_raw:
            # Default: only published + processed (ADR-0018 entry invariant
            # gate — CONTEXT_ADMISSIBLE_STATUSES is the single constant all
            # content-surfacing paths consult).
            allowed: set[MemoryStatus] | None = set(CONTEXT_ADMISSIBLE_STATUSES)
        elif status is None and include_raw:
            # include_raw: all except archived (archived = intentionally hidden)
            allowed = {
                MemoryStatus.RAW,
                MemoryStatus.PROCESSING,
                MemoryStatus.PROCESSED,
                MemoryStatus.PUBLISHED,
            }
        else:
            allowed = None  # explicit status, no post-hoc filter

        if allowed is not None:
            fts_pairs = [(m, s) for m, s in fts_pairs if is_context_admissible(m, statuses=allowed)]

        # ADR-0019 §5 — the quarantine exclusion is ABSOLUTE, not merely
        # part of the allowed-set composition above: quarantined rows
        # carry status='published', so an explicit ``status=`` drill-down
        # (allowed=None, the set filter skipped) would otherwise serve
        # terminal danger-lane content — and the REST/MCP payloads carry
        # no pipeline_state, so the caller could not detect it. Direct
        # ``get`` by id stays the documented residual until B2.
        fts_pairs = [(m, s) for m, s in fts_pairs if not is_quarantined(m)]

        # ADR-0019 §4 — "refined only" query flag: keep exclusively
        # refined projections on the FTS leg (applied after the status
        # gates; NULL/legacy pipeline_state never matches).
        if refined_only:
            fts_pairs = [(m, s) for m, s in fts_pairs if m.pipeline_state == PipelineState.REFINED]

        # ── Vector leg ─────────────────────────────────────────────────────
        # A9 (ArchCom 2026-08-27): pre-RRF project predicate. The store
        # filters candidates by the embedding metadata's ``project``
        # (native, pre-top-k — foreign rows never enter the candidate set),
        # and the resolve loop below re-checks the authoritative SQLite
        # ``Memory.project`` BEFORE any score is fused, so out-of-project
        # rows never consume RRF rank slots. ``project=None`` (or empty) is
        # the explicit global mode. Depth compensation: the 4x over-fetch
        # keeps the leg's ``limit * 2`` contribution depth fillable after
        # the predicate drops candidates (VECTOR_LEG_OVERFETCH_FACTOR).
        vector_pairs: list[tuple[str, float]] = []
        try:
            q_emb = self.embedder.embed(query)
            vector_pairs = self.vectors.search(
                q_emb,
                limit=limit * VECTOR_LEG_OVERFETCH_FACTOR,
                project=project,
            )
        except Exception as exc:
            logger.warning("Vector search failed: %s", exc)

        # A9: resolve + predicate-filter the vector candidates BEFORE the
        # RRF merge (pre-fusion semantics). The authoritative project check
        # consults the SQLite row (source of truth), guarding against
        # vector-metadata drift; status policy mirrors the FTS leg.
        # Survivors are truncated to the leg's ``limit * 2`` RRF
        # contribution depth — unchanged fusion semantics, now filled with
        # in-scope rows instead of being crowded out by foreign ones.
        id_to_memory: dict[str, Memory] = {m.id: m for m, _ in fts_pairs}
        vector_resolved: list[tuple[str, float]] = []
        for mid, vec_score in vector_pairs:
            fetched: Memory | None = id_to_memory.get(mid)
            if fetched is None:
                # SQLite lookups can miss; skip silently if memory was
                # deleted between vector and SQLite indexes.
                candidate: Memory | None = self.sqlite.get(mid)
                if candidate is None:
                    continue
                if project and (candidate.project or "") != project:
                    continue  # A9 authoritative guard (metadata drift)
                # A9-completion (epic #308 Ф1-PREP, #360 review): the
                # agent predicate — the FTS leg filters natively; this
                # resolve-time mirror keeps the vector leg symmetric, so
                # an agent-scoped search never surfaces another agent's
                # row through a stale embed. Deliberately NO native
                # VectorStore agent predicate (TL decision: the guard is
                # authoritative here, the store stays project-only).
                if agent and (candidate.agent or "") != agent:
                    continue
                # Filter vector results by the same status policy as the
                # FTS leg. The vector store only holds published memories
                # in normal operation, but a non-published memory that
                # somehow entered the store could surface here.
                if status is not None and candidate.status != status:
                    continue
                if allowed is not None and not is_context_admissible(candidate, statuses=allowed):
                    continue
                # ADR-0019 §5 — absolute quarantine exclusion (see the
                # FTS-leg note above): an explicit-status query must not
                # resurrect the row through a stale embed either.
                if is_quarantined(candidate):
                    continue
                # ADR-0019 §4 — refined_only guards the vector resolve leg
                # with the same predicate as the FTS leg.
                if refined_only and candidate.pipeline_state != PipelineState.REFINED:
                    continue
                id_to_memory[mid] = candidate
            vector_resolved.append((mid, vec_score))
            if len(vector_resolved) >= limit * 2:
                break

        # ── RRF merge ──────────────────────────────────────────────────────
        rrf_k = 60
        scores: dict[str, float] = {}

        for rank, (mem, _) in enumerate(fts_pairs, start=1):
            scores[mem.id] = scores.get(mem.id, 0.0) + (1 - alpha) / (rrf_k + rank)

        for rank, (mid, _) in enumerate(vector_resolved, start=1):
            scores[mid] = scores.get(mid, 0.0) + alpha / (rrf_k + rank)

        # Search mode: "hybrid" when the vector leg actually contributed a
        # result that survived the predicates and is not already covered by
        # the FTS leg. Vector leg failure (embeddings down) degrades
        # gracefully — RRF still ranks FTS-only results, but callers can see
        # the mode. Tracking contribution (not just raw output) prevents
        # reporting "hybrid" when all vector pairs were filtered out.
        fts_ids = {m.id for m, _ in fts_pairs}
        vector_contributed = any(mid not in fts_ids for mid, _ in vector_resolved)
        search_type = "hybrid" if vector_contributed else "fts_only"

        # Apply tag filter post-hoc.
        #
        # Cache contract Phase-1 (#280): deterministic tiebreak. The final
        # ordering is ``(score desc, id asc)``. A bare ``reverse=True``
        # sort is stable over the ``scores`` dict insertion order, which
        # tracks the FTS/vector leg iteration order — for equal fused
        # scores that order can depend on the vector index state (the
        # store's top-k tie behaviour), leaking non-determinism into the
        # retrieval boundary that feeds ``assemble_context``. The explicit
        # ``id`` key pins equal-score groups to lexicographic order, so
        # the boundary output is a pure function of the per-id fused
        # scores; order between DIFFERENT scores is unchanged. This is
        # the single ranking surface — downstream stages (assemble.py
        # block ordering) only run stable sorts over this order.
        #
        # A1-S1 (#325, flag-ON path only): the fused block fills
        # ``limit - k`` where ``k`` is the reserved walk quota
        # (``_walk_quota`` — a pure function of ``limit``); the sorted
        # candidates list is already materialized, so the SURPLUS beyond
        # the fused cut is retained (fused fill / walk backfill both
        # consume it in (score desc, id asc) order — ADR-0028 holds on
        # every path). Flag-OFF: the fused block fills to ``limit``
        # exactly as before (byte-identical A0 semantics).
        fused_sorted: list[tuple[str, float]] = sorted(
            scores.items(), key=lambda kv: (-kv[1], kv[0])
        )
        fused_fill: int = limit
        if self.settings.mnemos.graph_walk and _walk_quota(limit) > 0:
            fused_fill = limit - _walk_quota(limit)
        results: list[SearchResult] = []
        for mid, score in fused_sorted:
            matched: Memory | None = id_to_memory.get(mid)
            if matched is None:
                continue
            if tags and not all(t in matched.tags for t in tags):
                continue
            results.append(SearchResult(memory=matched, score=score, search_type=search_type))
            if len(results) >= fused_fill:
                break
        fused_surplus: list[tuple[str, float]] = []
        if self.settings.mnemos.graph_walk and _walk_quota(limit) > 0:
            # The candidates BEYOND the fused cut, in the same
            # (score desc, id asc) order: the walk's shortfall backfill
            # consumes them; a starved fused page (I1-I3 fixtures)
            # simply yields a shorter list (nothing beyond the cut).
            # Ids already on the page are excluded — the backfill never
            # re-serves a fused row.
            fused_surplus = [
                (mid, score)
                for mid, score in fused_sorted
                if mid not in {r.memory.id for r in results}
            ]

        # ── Graph leg (v1 issue #313; relates_to walk issue #324; A1-S1 #325) ─
        # Expansion along memory_edges from the fused ids. Kinds walked:
        # ``supersedes`` BOTH directions (v1, the unconditional leg) and,
        # when ``vesmaro.mnemos.graph_walk`` is ON, ``relates_to`` both directions
        # (ADR-0030 A0, issue #324; invariants I1-I3 are pinned by
        # tests/test_graph_walk_invariants.py).
        #
        # ADR-0030 A1-S1 (#325, ArchCom 2026-09-27 — flag-OFF path is
        # BYTE-IDENTICAL to A0): on the flag-ON path the fused page
        # reserves ``k = min(ceil(limit/5), limit//2)`` walk slots
        # (``_walk_quota`` — a PURE function of ``limit``), the walk fills
        # its reserved slots, and the fused SURPLUS backfills any
        # shortfall in the same (score desc, id asc) order — a search
        # with an empty graph loses NOTHING (slots return, the
        # byte-equality pin proves it), a search with graph fuel gets
        # enrichment even when the fused page is full (the A0
        # headroom-gate dead-on-arrival finding). The flag-OFF path
        # keeps A0 semantics exactly: the unconditional supersedes leg
        # runs only on headroom (``len(results) < limit``), quota does
        # not apply — pinned by tests/test_graph_walk_quota.py.
        # Depth: the flag-ON walk is a layered BFS-2 (ADR-0030 §3) —
        # depth 1 = edge of a fused anchor, depth 2 = edge of a
        # depth-1 neighbour; fanout + total-work caps bound hub rows.
        # The flag-OFF leg stays 1-hop (A0 surface, unchanged).
        # Deterministic decay: an expansion row's weight is
        # (1-alpha)/(rrf_k + 2*anchor_pos) x w_edge x 0.7^(depth-1)
        # where anchor_pos is its FIRST anchor's 1-based position in the
        # fused ranking (first-anchor-wins across BOTH layers), so the
        # appended block is a pure function of the fused ranking + the
        # edge table (ADR-0028). w_edge ranks WITHIN the walked block
        # and can never influence eligibility — I3.
        # The expansion passes the SAME gates as the fused rows on EVERY
        # axis and for BOTH kinds: project (the A9 authoritative guard,
        # review F2 / I1), status (the default ``allowed`` set AND the
        # explicit ``status=`` drill-down, review F1 / I1), quarantine
        # (absolute per ADR-0019 §5 — an edge is never a side door, I2)
        # and refined_only (§4). Gate binding is the QUERY's (I1):
        # candidate collection is never filtered by gate — every
        # neighbour gets collected, gated at append. The walk is capped
        # at ``limit`` extra rows (so a search can at most double); no
        # edges in the store → the whole leg is a no-op. Rows already
        # surfaced by the fused legs (by id) are never re-appended via
        # the graph.
        # Each appended row is TAGGED with its first-anchor discovery
        # kind (``via_graph_kind``) so the enrichment telemetry splits by
        # SOURCE LEG (#324 review fix): a neighbour reachable via both
        # kinds from its first anchor counts as supersedes — the
        # unconditional leg reached it; the walk counter claims only what
        # the relates_to leg alone surfaced.
        if self.settings.mnemos.graph_walk:
            # ── Flag-ON: reserved-quota BFS-2 walk + surplus backfill ──
            k = _walk_quota(limit)
            # P1 fix (#415 review): ``seen`` is initialised BEFORE the
            # ``k > 0 and results`` branch — the backfill loop below
            # reads it unconditionally, and a STARVED fused block (a
            # tags filter matching nothing, an empty status drill-down)
            # must not turn the composition rule into an
            # UnboundLocalError on the whole search (the leg is a
            # no-op on ANY failure, not a page crash).
            seen: set[str] = {r.memory.id for r in results}
            if k > 0 and results:
                fused_ids = [r.memory.id for r in results]
                walk_rows = self._collect_walk_candidates(
                    fused_ids,
                    scores=scores,
                )
                appended: list[SearchResult] = []
                # A walk candidate may be gated out at append time (I1/I2/
                # I3 run here, on the query's scope) — the loop walks
                # PAST gated candidates so a gated-out row never consumes
                # a reserved slot (gates bind to the query, never to the
                # reservation).
                # A1-S2 (ADR-0030 §3 APPLY, #325; ArchCom 2026-09-27,
                # I6 — THE Security residual): when the
                # ``vesmaro.mnemos.feedback_apply`` flag is ON, the captured
                # edge_stats ``used`` counters multiply each walked
                # row's weight by the SATURATING factor
                # ``f(used) = 1 + min(used, CAP) x SLOPE`` (bounded at
                # x2.0, the FEEDBACK_BOOST_CAP/SLOPE constants above).
                # RANK-ONLY within the walked block: the factor applies
                # AFTER every gate (the loops below filter on the
                # query's scope exactly as before), never touches the
                # fused block, and never expands the quota — one
                # batched ``used`` lookup per query over the
                # idx_edge_stats_memory index; a store read failure
                # degrades to an empty dict (the factor is neutral,
                # flag-on falls back to S1 semantics — enrichment must
                # never break the search it decorates). A zero-capture
                # store yields f(0)=1.0 for every row: flag-on ≡
                # flag-off byte-identically (pinned).
                used_counts: dict[str, int] = {}
                if self.settings.mnemos.feedback_apply and walk_rows:
                    try:
                        used_counts = self.sqlite.get_edge_stats_used_counts_batch(
                            [c.neighbour_id for c in walk_rows]
                        )
                    except Exception as exc:
                        logger.warning(
                            "feedback apply: used-counter lookup failed (non-fatal): %s", exc
                        )
                        used_counts = {}
                boosted = False
                for cand in walk_rows:
                    if len(appended) >= k:
                        break
                    neighbour = id_to_memory.get(cand.neighbour_id) or self.sqlite.get(
                        cand.neighbour_id
                    )
                    if neighbour is None:
                        continue  # edge to a deleted row — skip silently
                    # A9 authoritative project guard, mirrored from the vector
                    # resolve loop: the edge is stored by id only, so a
                    # cross-project neighbour would otherwise leak into a
                    # scoped search (review F2 — the edge must not widen the
                    # A9 scope, only the soft-fallback retry may, and it tags).
                    if project and (neighbour.project or "") != project:
                        continue
                    # A9-completion agent mirror (epic #308 Ф1-PREP): an
                    # edge must not widen the agent scope either — the same
                    # resolve-time guard as the vector leg above; the FTS
                    # leg filters natively.
                    if agent and (neighbour.agent or "") != agent:
                        continue
                    if tags and not all(t in neighbour.tags for t in tags):
                        continue
                    # Same status policy as the fused rows: the default gate
                    # (``allowed``) AND the explicit ``status=`` drill-down
                    # (review F1 — an edge must not widen an explicit status
                    # request any more than the fused legs do).
                    if status is not None and neighbour.status != status:
                        continue
                    if allowed is not None and not is_context_admissible(
                        neighbour, statuses=allowed
                    ):
                        continue
                    # ADR-0019 §5 — absolute quarantine exclusion on the graph path.
                    if is_quarantined(neighbour):
                        continue
                    # ADR-0019 §4 — refined_only gates the graph leg identically.
                    if refined_only and neighbour.pipeline_state != PipelineState.REFINED:
                        continue
                    # A1-S1 decay (ADR-0030 §3): (1-alpha)/(rrf_k + 2*pos)
                    # x w_edge x 0.7^(depth-1). w_edge ranks WITHIN the
                    # walked block only (post-gate, I3) — it never touches
                    # the fused block, eligibility, or the gate order.
                    decayed = (1.0 - alpha) / (rrf_k + 2 * cand.anchor_pos)
                    decayed *= cand.weight * (WALK_DEPTH_DECAY ** (cand.depth - 1))
                    # A1-S2 (I6 rank-only): the usage factor multiplies the
                    # POST-GATE weight — the row already passed every gate,
                    # so the boost can never restore eligibility (I6), and
                    # the saturating f bounds how far rank can move
                    # (bounded Δ). Non-neutral factors mark the query as
                    # feedback-influenced (the S2 acceptance observable).
                    used = int(used_counts.get(cand.neighbour_id, 0))
                    factor = _feedback_boost_factor(used)
                    if factor != 1.0:
                        boosted = True
                    decayed *= factor
                    appended.append(
                        SearchResult(
                            memory=neighbour,
                            score=decayed,
                            search_type=search_type,
                            via_graph=True,
                            via_graph_kind="relates_to" if cand.via_relates else "supersedes",
                        )
                    )
                # A1-S2 (I6 rank-only, ADR-0028 determinism): when (and
                # only when) feedback_apply=ON AND the boost actually
                # moved something, the walked block RE-SORTS by
                # (score desc, id asc) — the same deterministic tiebreak
                # line the fused block uses; the boost moves rows WITHIN
                # the block only (fused-block order untouched — the block
                # appends after it; eligibility untouched — the cut
                # already happened at ``len(appended) >= k``). Flag OFF,
                # zero-capture store, or an all-neutral pass: NO
                # re-sort, S1 order byte-identical (the S1 block order is
                # the collection order (anchor_pos, depth, id) — a pure
                # function of the fused ranking + edge table; sorting a
                # neutral block could still permute equal-score-free
                # rows away from the S1 order, so the sort is skipped
                # entirely unless a factor moved a row).
                if boosted and len(appended) > 1:
                    appended.sort(key=lambda r: (-r.score, r.memory.id))
                if boosted:
                    with self._search_stats_lock:
                        self._search_stats["feedback_boosted_queries_total"] = (
                            int(self._search_stats["feedback_boosted_queries_total"]) + 1
                        )
                results.extend(appended)
            # Backfill (flag-ON): the fused surplus fills any walk
            # shortfall in the SAME (score desc, id asc) order — the
            # page never ships short when the graph had less fuel than
            # its quota reserved, and an edge-less corpus returns the
            # slots to the fused block (the byte-equality pin,
            # tests/test_graph_walk_quota.py, proves both at once).
            # P2 fix (#415 review, I1 worst-link): the backfill rows
            # pass the SAME tags filter as the fused loop — the query's
            # gates bind to the composition rule, never the other way
            # round; a tags-rejected row must not re-enter the page
            # through the surplus.
            for mid, score in fused_surplus:
                if len(results) >= limit:
                    break
                matched = id_to_memory.get(mid)
                if matched is None or matched.id in seen:
                    continue
                if tags and not all(t in matched.tags for t in tags):
                    continue
                results.append(SearchResult(memory=matched, score=score, search_type=search_type))
        elif len(results) < limit:
            # ── Flag-OFF: the A0 surface, byte-identical ────────────────
            fused_ids = [r.memory.id for r in results]
            anchor_rank: dict[str, int] = {}
            walk_sourced: set[str] = set()
            for pos, anchor_id in enumerate(fused_ids, start=1):
                supersedes_adj = self._graph_adjacent(anchor_id)
                relates_adj: set[str] = set()
                if self.settings.mnemos.graph_walk:
                    relates_adj = self._graph_adjacent(anchor_id, kind="relates_to")
                for neighbour_id in sorted(supersedes_adj | relates_adj):
                    if neighbour_id in anchor_rank or neighbour_id in scores:
                        continue  # first anchor wins; fused rows never re-surface
                    anchor_rank[neighbour_id] = pos
                    if neighbour_id in relates_adj and neighbour_id not in supersedes_adj:
                        walk_sourced.add(neighbour_id)
            for neighbour_id, pos in anchor_rank.items():
                if len(results) >= limit + limit:
                    break
                neighbour = id_to_memory.get(neighbour_id) or self.sqlite.get(neighbour_id)
                if neighbour is None:
                    continue  # edge to a deleted row — skip silently
                # A9 authoritative project guard, mirrored from the vector
                # resolve loop: the edge is stored by id only, so a
                # cross-project neighbour would otherwise leak into a
                # scoped search (review F2 — the edge must not widen the
                # A9 scope, only the soft-fallback retry may, and it tags).
                if project and (neighbour.project or "") != project:
                    continue
                # A9-completion agent mirror (epic #308 Ф1-PREP): an
                # edge must not widen the agent scope either — the same
                # resolve-time guard as the vector leg above; the FTS
                # leg filters natively.
                if agent and (neighbour.agent or "") != agent:
                    continue
                if tags and not all(t in neighbour.tags for t in tags):
                    continue
                # Same status policy as the fused rows: the default gate
                # (``allowed``) AND the explicit ``status=`` drill-down
                # (review F1 — an edge must not widen an explicit status
                # request any more than the fused legs do).
                if status is not None and neighbour.status != status:
                    continue
                if allowed is not None and not is_context_admissible(neighbour, statuses=allowed):
                    continue
                # ADR-0019 §5 — absolute quarantine exclusion on the graph path.
                if is_quarantined(neighbour):
                    continue
                # ADR-0019 §4 — refined_only gates the graph leg identically.
                if refined_only and neighbour.pipeline_state != PipelineState.REFINED:
                    continue
                decayed = (1.0 - alpha) / (rrf_k + 2 * pos)
                results.append(
                    SearchResult(
                        memory=neighbour,
                        score=decayed,
                        search_type=search_type,
                        via_graph=True,
                        via_graph_kind="relates_to"
                        if neighbour_id in walk_sourced
                        else "supersedes",
                    )
                )
        return results

    # ── Graph leg helpers (v1 issue #313; relates_to walk issue #324) ────

    def _graph_adjacent(self, memory_id: str, *, kind: str = "supersedes") -> set[str]:
        """1-hop neighbour ids along ``memory_edges`` (both directions).

        Superseding→superseded (outgoing) and superseded→superseding
        (incoming): a search hit should surface BOTH sides of a
        supersedes pair — the newer version that replaced the hit AND the
        older sibling it replaced. Returns the neighbour ids only (the
        caller excludes / resolves them); a missing memory id simply has
        no rows in memory_edges.

        ``kind`` is the extension point (ADR-0030): issue #324's walk
        consults ``relates_to`` through the SAME primitive — the walk
        loop in ``_search_core`` holds the flag policy (supersedes
        always; relates_to only behind ``vesmaro.mnemos.graph_walk``) and tags
        each appended row with its first-anchor discovery kind.
        """
        neighbours: set[str] = set()
        try:
            for edge in self.sqlite.get_direct_edges(memory_id, kind=kind):
                neighbours.add(str(edge["to_memory_id"]))
            incoming = self.sqlite.get_incoming_edges(memory_id, kind=kind)
            for edge in incoming:
                neighbours.add(str(edge["from_memory_id"]))
        except Exception as exc:
            # The leg is a no-op on ANY failure — graph enrichment must
            # never break the search it decorates.
            logger.warning(
                "graph leg: edge lookup failed (non-fatal) for %s: %s", memory_id[:8], exc
            )
        neighbours.discard(memory_id)  # self-edges are rejected at write time
        return neighbours

    def _graph_adjacent_weighted_batch(
        self, memory_ids: list[str], *, kind: str
    ) -> dict[str, list[tuple[str, float]]]:
        """Layered BFS-2 batch expansion (ADR-0030 A1-S1, #325).

        One outgoing + one incoming ``WHERE ... IN (...)`` query per kind
        for the WHOLE layer (4 indexed point queries per layer — the
        sqlite_store.py:919-920 indexes serve them; NOT a recursive CTE,
        see ADR-0030 Alternatives). Returns ``{node_id: [(neighbour_id,
        w_edge), ...]}`` — the w_edge of the DISCOVERY edge rides along
        so the A1 ranking formula (``x w_edge x 0.7^(depth-1)``) needs no
        second lookup. Per-node fanout is capped at ``WALK_FANOUT_CAP``
        (hub rows contribute a bounded neighbour budget, in
        deterministic (weight, id) order — the cap is a ranking-surface
        decision, so it stays deterministic). A missing memory id simply
        has no rows in memory_edges. The leg is a no-op on ANY failure —
        graph enrichment must never break the search it decorates.
        """
        if not memory_ids:
            return {}
        adjacency: dict[str, list[tuple[str, float]]] = {mid: [] for mid in memory_ids}
        try:
            conn = self.sqlite._get_conn()
            placeholders = ",".join("?" * len(memory_ids))
            outgoing = conn.execute(
                "SELECT from_memory_id, to_memory_id, weight FROM memory_edges "
                f"WHERE from_memory_id IN ({placeholders}) AND kind = ?",
                (*memory_ids, kind),
            ).fetchall()
            incoming = conn.execute(
                "SELECT to_memory_id, from_memory_id, weight FROM memory_edges "
                f"WHERE to_memory_id IN ({placeholders}) AND kind = ?",
                (*memory_ids, kind),
            ).fetchall()
        except Exception as exc:
            # The leg is a no-op on ANY failure — graph enrichment must
            # never break the search it decorates.
            logger.warning("graph walk: batch edge lookup failed (non-fatal): %s", exc)
            return adjacency
        for row in outgoing:
            source = str(row["from_memory_id"])
            if source in adjacency:
                adjacency[source].append((str(row["to_memory_id"]), float(row["weight"])))
        for row in incoming:
            target = str(row["to_memory_id"])
            if target in adjacency:
                adjacency[target].append((str(row["from_memory_id"]), float(row["weight"])))
        for mid, pairs in adjacency.items():
            # Fanout cap (ADR-0030 §3): deterministic cut — heaviest
            # edges first, id tiebreak (the same ADR-0028 rule, applied
            # to the candidate-set cut rather than the final ranking).
            pairs.sort(key=lambda p: (-p[1], p[0]))
            del pairs[WALK_FANOUT_CAP:]
            adjacency[mid] = [(nid, w) for nid, w in pairs if nid != mid]
        return adjacency

    def _collect_walk_candidates(
        self,
        fused_ids: list[str],
        *,
        scores: dict[str, float],
    ) -> list[_WalkCandidate]:
        """Collect flag-on BFS-2 candidates (bounded by the work caps).

        Layer 1: the fused anchors expand along ``supersedes`` then
        ``relates_to`` (batched ``IN`` lookups, one per kind per
        direction); layer 2: the depth-1 neighbours expand along
        ``relates_to`` only (the committee scope — the unconditional
        supersedes leg stays 1-hop; BFS-2 is the walk's mechanics).
        FIRST-ANCHOR-WINS across both layers: a neighbour's anchor is
        its first discoverer's fused position, its discovery weight is
        the FIRST edge that reached it (max across that anchor's
        discovery edges, id-tiebroken — a pure function of the edge
        table), and a ``supersedes`` claim beats a ``relates_to`` claim
        from the same anchor (the #324 attribution rule). Fused rows
        (``scores``) are never re-surfaced. Work is bounded by the
        total-work cap; collection NEVER filters by gate — the gates run
        at append time on the query's scope (I1), never at collection.
        """
        claimed: dict[str, _WalkCandidate] = {}
        on_page = set(scores)

        def _claim(
            neighbour_id: str, *, pos: int, depth: int, weight: float, via_relates: bool
        ) -> None:
            if neighbour_id in claimed or neighbour_id in on_page:
                return  # first anchor wins; fused rows never re-surface
            if len(claimed) >= WALK_TOTAL_WORK_CAP:
                return  # total-work cap (ADR-0030 §3): expansion budget spent
            claimed[neighbour_id] = _WalkCandidate(
                neighbour_id=neighbour_id,
                anchor_pos=pos,
                depth=depth,
                weight=weight,
                via_relates=via_relates,
            )

        # Layer 1 — supersedes first (its attribution claim wins on ties):
        # per anchor, a ``WALK_FANOUT_CAP`` budget (fanout cap, ADR-0030
        # §3) — the fanout order is deterministic (weight, id), so the
        # cut is a pure function of the edge table.
        supers = self._graph_adjacent_weighted_batch(fused_ids, kind="supersedes")
        for pos, anchor_id in enumerate(fused_ids, start=1):
            for neighbour_id, w in supers.get(anchor_id, ())[:WALK_FANOUT_CAP]:
                _claim(neighbour_id, pos=pos, depth=1, weight=w, via_relates=False)
        relates = self._graph_adjacent_weighted_batch(fused_ids, kind="relates_to")
        for pos, anchor_id in enumerate(fused_ids, start=1):
            for neighbour_id, w in relates.get(anchor_id, ())[:WALK_FANOUT_CAP]:
                _claim(neighbour_id, pos=pos, depth=1, weight=w, via_relates=True)
        # Layer 2 — depth-1 entries expand along relates_to; the depth-2
        # neighbour inherits its entry's first anchor (first-anchor-wins
        # attribution across layers, ADR-0030 §3) and the walk-only
        # relates_to attribution.
        if len(claimed) < WALK_TOTAL_WORK_CAP:
            layer1_ids = sorted(claimed)
            layer2 = self._graph_adjacent_weighted_batch(layer1_ids, kind="relates_to")
            for entry_id in layer1_ids:
                entry = claimed[entry_id]
                for neighbour_id, w in layer2.get(entry_id, ())[:WALK_FANOUT_CAP]:
                    _claim(
                        neighbour_id,
                        pos=entry.anchor_pos,
                        depth=2,
                        weight=w,
                        via_relates=True,
                    )
        # Deterministic order (ADR-0028): the collected block is a pure
        # function of the fused ranking + the edge table —
        # (anchor_pos asc, depth asc, id asc); the w_edge factor enters
        # only the DECAY at append time (post-gate, I3).
        return sorted(
            claimed.values(),
            key=lambda c: (c.anchor_pos, c.depth, c.neighbour_id),
        )

    # ── ADR-0030 A0: relates_to auto-minting on write (issue #322) ────────

    def _mint_relates_to_edges(self, memory: Memory) -> int:
        """Mint ``relates_to`` edges from ``memory`` to near-duplicates.

        ADR-0030 Decision 2 — the write-path fuel leg. ONE synchronous
        retrieval over the EXISTING legs (one query embed + one
        cosine-scored, project-scoped ``VectorStore.search`` — the same
        primitives the hybrid search's vector leg uses; the FTS leg is
        deliberately not consulted for qualification, measured decision
        recorded in ``vesmaro.graph_minting``: its AND semantics
        structurally miss the re-add direction). Candidate ids resolve
        through the SQLite authority; the pure selector then applies the
        exclusion set (I4 no-federate, §5 quarantine absolute,
        admissible statuses, intra-project, self, ``via_graph``), the
        cosine similarity threshold and the top-1..3 cap; each survivor
        becomes a ``relates_to`` edge ``memory → candidate`` with the
        raised weight and provenance ``auto-dedupe`` (idempotent per the
        ``(from, to, kind)`` primary key — INSERT OR IGNORE; a re-mint
        of the same memory inserts nothing and counts nothing).

        Determinism (ADR-0028, review L2 qualification): GIVEN the
        retrieved pool, selection is a pure function — the selector
        re-sorts survivors by ``(score desc, id asc)``, so the
        vector store's tie order never leaks INTO the choice. The pool
        cut itself (``VectorStore.search`` top-k via argpartition)
        applies no id tiebreak beyond the pool boundary: with MORE than
        ``AUTO_DEDUPE_CANDIDATE_POOL`` equal-score candidates (an
        exact-duplicate flood) the pool membership is position-
        dependent. The residual is bounded — at most the pool size is
        considered and at most 3 edges mint — and the pool exists to
        bound exactly that flood.

        No supersede decisions, no LLM, no status/visibility mutations —
        similarity only ever mints the weaker ``relates_to`` claim.

        Returns the number of NEW edges inserted (0 when the flag is
        off, the projection is empty, nothing passed the gates, or every
        edge already existed). Retrieval failures propagate to the
        ``add`` call site's best-effort wrapper; insert failures are
        contained per-edge below.
        """
        if not self.settings.mnemos.graph_auto_mint:
            return 0
        # ── Review M1: FROM-side gates. The selector validates the
        # CANDIDATES; these two checks validate the NEW memory as an
        # endpoint — the exclusion contract binds BOTH sides of an edge.
        # (a) I4: a no-federate node is never an endpoint, either side.
        #     The write-path scanner may have auto-tagged this very row
        #     (a secret-positive write) — it must not mint FROM itself.
        if NO_FEDERATE_TAG in memory.tags:
            return 0
        # (b) General gates: a RAW/processing (publish-gate refusal,
        #     explicit raw) or quarantined new row is invisible content —
        #     minting from it would leave permanent graph edges with no
        #     cleanup when the row is later quarantined. Invisible
        #     writes are not fuel.
        if not is_context_admissible(memory):
            return 0
        # The near-duplicate query is ``_embedding_text(memory)`` — the
        # EXACT text the memory itself was embedded with, so the query
        # lives in the stored vectors' space (self-cosine 1.0; a
        # near-dup shares the content AND the tag-contract tokens, and a
        # far row's shared tags alone stay well under the threshold).
        query_text = self._embedding_text(memory)
        if not query_text.strip():
            return 0
        q_emb = self.embedder.embed(query_text)
        vector_pairs = self.vectors.search(
            q_emb,
            limit=AUTO_DEDUPE_CANDIDATE_POOL,
            project=memory.project or None,
        )
        resolved: list[SearchResult] = []
        for mid, cosine in vector_pairs:
            row = self.sqlite.get(mid)
            if row is None:
                continue  # deleted between the index and the resolve
            resolved.append(SearchResult(memory=row, score=cosine, search_type="semantic"))
        minted = 0
        # ── Review L1: orientation-aware mint idempotency. The PK
        # (from, to, kind) treats A→B and B→A as distinct rows, but for
        # MINTED fuel the pair is semantically undirected — a re-mint of
        # the older end (backfill tools, A0-review re-runs) must not
        # duplicate the pair in reverse (#324's bidirectional walk would
        # double-count both orientations). Mint-specific guard: a
        # DECLARED reverse edge stays insertable — direction can be
        # semantic for explicit callers, only the auto-dedupe rule is
        # held to the undirected-pair contract.
        incoming_from: set[str] = {
            str(e["from_memory_id"])
            for e in self.sqlite.get_incoming_edges(memory.id, kind=AUTO_DEDUPE_EDGE_KIND)
        }
        for candidate in select_auto_dedupe_candidates(memory, resolved):
            if candidate.memory.id in incoming_from:
                logger.debug(
                    "graph auto-mint: reverse pair exists, skipping from=%s to=%s",
                    memory.id[:8],
                    candidate.memory.id[:8],
                )
                continue
            try:
                inserted = self.add_memory_edge(
                    memory.id,
                    candidate.memory.id,
                    kind=AUTO_DEDUPE_EDGE_KIND,
                    weight=AUTO_DEDUPE_EDGE_WEIGHT,
                    provenance=AUTO_DEDUPE_PROVENANCE,
                )
            except Exception as exc:
                # Per-edge isolation: a candidate deleted between search
                # and insert (FK miss) or a store hiccup must not abort
                # the remaining candidates — logged, skipped.
                logger.warning(
                    "graph auto-mint: edge insert failed (non-fatal) from=%s to=%s: %s",
                    memory.id[:8],
                    candidate.memory.id[:8],
                    exc,
                )
                continue
            if inserted:
                minted += 1
        if minted:
            with self._graph_mint_stats_lock:
                project_key = memory.project or ""
                self._graph_mint_stats[project_key] = (
                    self._graph_mint_stats.get(project_key, 0) + minted
                )
            logger.info(
                "graph auto-mint: id=%s project=%s new_edges=%d — relates_to (auto-dedupe)",
                memory.id[:8],
                memory.project or "",
                minted,
            )
        return minted

    def agent_recall(self, query: AgentRecallQuery) -> list[SearchResult]:
        """M3 — per-agent recall: recent entries + optional hybrid search.

        Agent recall is about "what has this agent stored", not "what is
        published knowledge" — so the query path passes ``include_raw=True``
        to surface recently-added entries regardless of pipeline status.
        The recency path (no query) already has no status filter.

        mnemos #400 — ``query.project`` is normalized at the boundary
        (single authority): the search leg inherits ``search``'s own
        normalization, the recency leg normalizes here so
        ``list_recent_for_agent`` predicates on the canonical slug.

        Ф2 (epic #308, ADR-0027 Phase 2) — ``query.task``: the optional
        task scope, byte-identical to filtering by ``task:<slug>``
        (the F1 arm-C surface). On the search leg the minted tag rides
        the SAME ``tags`` filter ``search`` applies (one code path, the
        equivalence doctrine); on the recency leg it post-filters the
        agent's feed by exact tag membership. Normalized/fail-loud at
        this boundary via the shared ``_normalize_task_boundary`` helper
        (the #407 canon).
        """
        from vesmaro.models import normalize_project_slug

        if query.project:
            query = query.model_copy(update={"project": normalize_project_slug(query.project)})
        task_tag: str | None = None
        if query.task is not None:
            task_tag = self._normalize_task_boundary(query.task)
        if query.query:
            return self.search(
                query.query,
                agent=query.agent,
                project=query.project,
                task=query.task,
                limit=query.limit,
                include_raw=True,
            )
        # No query → return most recent N for agent. Ф2: a task scope
        # post-filters the recency feed by exact tag membership (the
        # same predicate the search legs apply; list_recent_for_agent
        # has no tags parameter and widening its signature is a store
        # change this wave does not make).
        memories = self.sqlite.list_recent_for_agent(
            query.agent,
            project=query.project,
            limit=query.limit,
        )
        if task_tag is not None:
            # A modest over-fetch would be guesswork; the feed is
            # recency-capped by the caller's limit either way, so the
            # filter runs on what the feed returned — zero rows in a
            # task scope is information, not drift (slice 1).
            memories = [m for m in memories if task_tag in m.tags]
        # ADR-0019 §5 — this is an agent-context issuance path, so the
        # quarantine exclusion applies here as well: the agent's own
        # recency feed must not carry terminally quarantined content.
        # Transparency about its own quarantine stays via direct
        # get-by-id until the B2 retraction render.
        return [
            SearchResult(memory=m, score=1.0, search_type="recency")
            for m in memories
            if not is_quarantined(m)
        ]

    def recall_context(
        self, *, project: str, query: str | None = None, task: str | None = None, limit: int = 5
    ) -> list[Memory]:
        """Return most recent checkpoint memories for a project.

        When ``query`` is provided, a hybrid search scoped to
        ``mnemos:checkpoint`` tags is used to rank checkpoints by relevance,
        then the top ``limit`` are returned. When ``query`` is omitted,
        checkpoints are returned by recency only.

        mnemos #400 — the QUERY boundary normalizes the project slug with
        :func:`vesmaro.models.normalize_project_slug` (the single
        authority, the same normalization the save boundary and the tag
        contract lax mode apply). A checkpoint saved as ``MyProject`` and
        recalled as ``myproject`` is ONE namespace — a read surface can
        never diverge from the write surface. Fail-loud on an
        unsalvageable slug: a contract-invalid project means the caller
        asked for a namespace that cannot exist. An empty project stays
        the pre-existing global recency listing (CLI ``mnemos recall`` — legacy CLI name
        with no ``--project`` on an empty vault relies on it; the MCP
        tool always passes a concrete project via the ``_detect_project``
        fallback).

        Ф2 (epic #308, ADR-0027 Phase 2) — ``task``: the first-class
        task-scope switcher, byte-identical to ``tags=["task:<slug>"]``
        composed with the checkpoint scope (the F1 arm-C surface): the
        minted tag JOINS the ``mnemos:checkpoint`` tag on both legs
        (query and recency), so ``task="x"`` ≡ ``["mnemos:checkpoint",
        "task:x"]`` by construction — the equivalence doctrine pinned by
        tests. Normalized/fail-loud at this boundary exactly like
        ``project`` (the #407 canon); task-scoped recall returns only
        checkpoints of that task — zero rows is information ("no
        checkpoint in this task yet"), not drift.
        """
        from vesmaro.models import _PROJECT_RE, normalize_project_slug

        project = normalize_project_slug(project)
        if project and not _PROJECT_RE.match(f"project:{project}"):
            raise ValueError(
                f"project must be 1-64 characters of [a-z0-9_-] after normalization "
                f"(got {project!r})"
            )
        tags: list[str] = ["mnemos:checkpoint"]
        if task is not None:
            task_tag = self._normalize_task_boundary(task)
            tags.append(task_tag)
        if query:
            # Ф2 review fix 2 (P2, TL decision — attribute at
            # CALLER-INTENT): the query leg threads the PARAM form
            # (``task=``), not the minted tags list, so the comparative
            # counters attribute uniformly — a caller who used the
            # first-class parameter on recall_context(query=, task=)
            # increments task_param_queries_total exactly like the same
            # call on search/agent_recall. The internal translation
            # (minting the tag into ``tags``) must NOT leak into the
            # owner's standing metric: ``task_tag - task_param`` stays
            # the genuine tag-only share, not an implementation shape.
            # The recency leg below keeps the tags form (list_all has no
            # task parameter — the disclosed slice-1 trade-off; list_all
            # paths run no counters, so attribution is unaffected).
            results = self.search(
                query=query,
                tags=tags,
                project=project,
                task=task,
                limit=limit,
            )
            return [r.memory for r in results]

        memories = self.sqlite.list_all(
            limit=limit * 3,
            project=project,
            tags=tags,
        )
        # Invariant: archived checkpoints are retired and must not resurface as fresh context.
        # ADR-0019 §5 — quarantined entries are excluded here too (the
        # is_quarantined predicate; the recency leg's status policy is
        # "everything except archived", so the allowed-set helper does not
        # apply — the quarantine condition itself must not be copy-pasted).
        memories = [
            m for m in memories if m.status != MemoryStatus.ARCHIVED and not is_quarantined(m)
        ]
        # Sort by recency and trim
        memories.sort(key=lambda m: m.created_at, reverse=True)
        return memories[:limit]

    # ── Checkpoint channel identity (mnemos #251 D0) ────────────────────

    def save_checkpoint(
        self,
        fields: Mapping[str, str | None],
        *,
        project: str,
        agent: str | None = None,
        session: str | None = None,
        task: str | None = None,
        memory_type: MemoryType = MemoryType.NOTE,
        language: str | None = None,
    ) -> tuple[Memory, bool]:
        """Store a session checkpoint with validated agent identity (#251 D0).

        Single authority for the checkpoint channel — the MCP tool
        (``mnemos_save_context`` MCP tool) and the REST twin (``POST /context/save``)
        are thin wrappers over this method. Order of operations:

        1. Identity validation (``_require_identity`` semantics: non-empty
           string when provided, whitespace-only rejected). ``agent``
           defaults to ``"user"`` — today's behaviour, so deployed
           instruction packs keep working unchanged.
        2. Trivial-reject: all five fields empty → ValueError BEFORE any
           store (zero-loss — the caller is told, nothing is dropped).
        3. Session→agent binding: the first call presenting a session id
           records the binding server-side (meta table, first writer
           wins); later calls must claim the bound agent or a
           :class:`SessionAgentMismatchError` (a ValueError) is raised.
        4. Issuer-keyed dedup: SHA-256 over the canonical payload
           (project + agent + the five fields, fixed order, None
           normalised to ``""``) — NOT over the rendered markdown, which
           embeds a fresh timestamp and would never collide. A hit
           returns the EXISTING memory with ``duplicate=True`` and stores
           nothing. NOTE (vesmaro-canon v1.0.0): the payload does NOT
           include ``language`` — a dedup hit returns the first-minted
           row with its own envelope, the new call's language never
           rewrites a stored record (canon §10, new-records-only).
           Ф2 (epic #308): the payload does NOT include ``task`` either —
           same canon §10 logic: a dedup hit returns the first-minted row
           with ITS task scope; the new call's task never rewrites a
           stored record.
        5. Store with server-controlled metadata stamps
           (``checkpoint_agent`` / ``checkpoint_session`` /
           ``checkpoint_dedup_key``) PLUS the canon v1.0.0 envelope
           (``metadata.canon``, vesmaro-canon ADR-0003 obligation 3):
           ``schema_version="1"``, ``type="checkpoint"``,
           ``status="active"``, ``language=<the language below>``,
           ``session_ref=<session or null>``. The stamps and the
           envelope are minted ONLY here (``trusted_checkpoint_stamps``
           on ``add``); generic create/update paths strip
           client-supplied copies (review P1 + canon stamp discipline —
           ``"canon"`` joined :data:`CHECKPOINT_STAMP_KEYS`).
           Tags are display-only; these server columns/metadata are the
           source of truth (the awareness read surface arrives with
           #254).

        ``task`` (Ф2, epic #308, ADR-0027 Phase 2) is the WRITE-side
        switcher: the bare slug, normalized at this boundary via the
        shared ``_normalize_task_boundary`` helper (the #407 canon —
        the SAME normalization the read surfaces apply, so a checkpoint
        saved under ``task="My_Task"`` is found by ``search(task="my-task")``
        and by ``tags=["task:my-task"]`` alike). The minted
        ``task:<slug>`` tag joins the record's tags at this ONE save
        boundary — not per-tool. The zero-or-one-per-record invariant
        (ADR-0027) is enforced here: the checkpoint channel mints its
        own tags (the caller never supplies them), so the only way a
        second ``task:`` tag could appear is a caller threading BOTH
        ``task=`` and a task tag — impossible by construction; the tag
        contract's always-fatal multiple-``task:`` check remains the
        invariant's last line of defense on every other write path.

        ``language`` (vesmaro-canon v1.0.0, ADR-0003 obligation 4):
        primary language of the record body — canon §2 enum ``"ru"``
        /``"en"``, NO heuristics. ``None`` (the default) falls back to the
        ``vesmaro.mnemos.checkpoint_language`` config value. Every call MUST land
        on a concrete language: the envelope mint raises ``ValueError``
        on anything outside the canon enum, fail-loud.

        The body render ALWAYS emits the five EN sections in
        :data:`CHECKPOINT_FIELDS` order (canon §3, ArchCom option A —
        ADR-0003 verdict B); an empty field gets the deterministic
        per-language placeholder line from
        :data:`CHECKPOINT_PLACEHOLDERS`. Section H2 titles come from the
        explicit map :data:`CHECKPOINT_SECTION_TITLES` (ADR-0003
        obligation 2 — never ``str.title()``) and are pinned against the
        schemas' ``x-canon-sections`` by a drift test.

        Returns ``(memory, duplicate)``.
        """
        # 1. Identity validation — mirrors vesmaro.hooks._require_identity
        #    (non-empty string when provided, whitespace-only rejected),
        #    plus the review-P3 hygiene: agent is a slug (same charset as
        #    the agent:<slug> tag contract) and session is a bounded
        #    print-safe token, so whitespace-cased or oversize identities
        #    can never become distinct binding identities.
        for label, value in (("agent", agent), ("session", session)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{label} must be a non-empty string when provided")
        resolved_agent = agent if agent is not None else "user"
        if _CHECKPOINT_AGENT_RE.fullmatch(resolved_agent) is None:
            raise ValueError(
                f"agent must be 1-64 characters of [a-z0-9_-] (got {len(resolved_agent)} chars)"
            )
        if session is not None and _CHECKPOINT_SESSION_RE.fullmatch(session) is None:
            raise ValueError(
                "session must be 1-128 printable ASCII characters without spaces "
                f"(got {len(session)} chars)"
            )

        # mnemos #400 — project slug: normalize at the SAVE boundary (the
        # single-authority doctrine of #263, applied to slugs). The MCP tool
        # and the REST twin pass ``project`` directly (NOT through the tag
        # contract like the ``mnemos_add`` tool does), so without this gate a
        # ``MyProject`` checkpoint persisted under a different store key than
        # the ``myproject`` rows written via the tag-contract path — a silent
        # namespace island. ``normalize_project_slug`` is the SAME
        # normalization the tag-contract lax mode applies, so the checkpoint
        # channel can never diverge from the tag channel. Fail-loud on an
        # unsalvageable slug (never mint a silently-different namespace).
        from vesmaro.models import _PROJECT_RE, normalize_project_slug

        project = normalize_project_slug(project)
        if not _PROJECT_RE.match(f"project:{project}"):
            raise ValueError(
                f"project must be 1-64 characters of [a-z0-9_-] after normalization "
                f"(got {project!r})"
            )

        # Ф2 (epic #308) — task slug: normalize at the SAVE boundary via
        # the shared helper (single authority, the #407 canon applied to
        # the task dimension — the SAME normalization every read surface
        # applies, so a saved task scope is always reachable by either
        # switcher form). Fail-loud on an unsalvageable slug.
        task_tag: str | None = None
        if task is not None:
            task_tag = self._normalize_task_boundary(task)

        # 2. Trivial-reject before any store side effect.
        normalized = {f: (fields.get(f) or "") for f in CHECKPOINT_FIELDS}
        if not any(normalized[f] for f in CHECKPOINT_FIELDS):
            raise ValueError(
                "checkpoint rejected: all fields (goals/completed/in_progress/"
                "decisions/context) are empty — nothing to save"
            )

        # 3. Session→agent binding (only when a session id is presented).
        if session is not None:
            bound_agent = self.sqlite.bind_session_agent(session, resolved_agent)
            if bound_agent != resolved_agent:
                logger.warning(
                    "checkpoint binding mismatch (mnemos #251): session=%r bound_agent=%r "
                    "claimed_agent=%r — refused",
                    session,
                    bound_agent,
                    resolved_agent,
                )
                raise SessionAgentMismatchError(
                    f"session {session!r} is already bound to agent {bound_agent!r}; "
                    f"refusing checkpoint claimed by agent {resolved_agent!r}"
                )

        # 4. Issuer-keyed dedup.
        canonical = json.dumps(
            [project, resolved_agent, *(normalized[f] for f in CHECKPOINT_FIELDS)],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        dedup_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        existing = self.sqlite.find_checkpoint_by_dedup_key(
            project=project, agent=resolved_agent, dedup_key=dedup_key
        )
        if existing is not None:
            logger.info(
                "checkpoint dedup hit (mnemos #251): project=%r agent=%r id=%s",
                project,
                resolved_agent,
                existing.id,
            )
            return existing, True

        # 5. Build and store — content format unchanged from the legacy
        # hardcoded-agent surfaces EXCEPT (vesmaro-canon v1.0.0, ArchCom
        # option A): every section is ALWAYS rendered, in
        # CHECKPOINT_FIELDS order, so the stored body satisfies canon §3
        # ("the body must contain all five `^## <Name>$` headers"). An
        # empty field gets the deterministic per-language placeholder —
        # never skipped, and the section order never depends on payload
        # emptiness. H2 titles come from the explicit
        # CHECKPOINT_SECTION_TITLES map (ADR-0003 obligation 2 — no
        # ``str.title()`` re-derivation, drift-pinned to
        # schemas x-canon-sections).
        resolved_language = (
            language if language is not None else self.settings.mnemos.checkpoint_language
        )
        canon = checkpoint_canon_envelope(language=resolved_language, session_ref=session)
        placeholders = {
            f: CHECKPOINT_PLACEHOLDERS[(f, resolved_language)] for f in CHECKPOINT_FIELDS
        }
        parts = [f"# Session checkpoint — {datetime.now(UTC).isoformat()}\n"]
        for field in CHECKPOINT_FIELDS:
            section_body = normalized[field] if normalized[field] else placeholders[field]
            parts.append(f"## {CHECKPOINT_SECTION_TITLES[field]}\n{section_body}\n")
        metadata: dict[str, Any] = {
            "checkpoint_agent": resolved_agent,
            "checkpoint_dedup_key": dedup_key,
            "canon": canon,
        }
        if session is not None:
            metadata["checkpoint_session"] = session
        # Ф2 (epic #308): the minted task tag rides the tags list at the
        # ONE save boundary (never per-tool). The checkpoint channel
        # mints its own tags, so the zero-or-one task invariant holds by
        # construction — exactly one task tag can ever reach this list.
        checkpoint_tags: list[str] = [
            f"project:{project}",
            f"agent:{resolved_agent}",
            "mnemos:checkpoint",
        ]
        if task_tag is not None:
            checkpoint_tags.append(task_tag)
        data = MemoryCreate(
            content="\n".join(parts),
            tags=checkpoint_tags,
            source=MemorySource.MCP,
            memory_type=memory_type,
            metadata=metadata,
        )
        memory = self.add(
            data,
            project=project,
            agent=resolved_agent,
            trusted_checkpoint_stamps=True,
            mint_relates_to=False,  # #322 review M2: checkpoint saves are not fuel
        )
        return memory, False

    def list_recent(
        self,
        *,
        limit: int = 10,
        offset: int = 0,
        tags: list[str] | None = None,
        project: str | None = None,
        agent: str | None = None,
        task: str | None = None,
        status: MemoryStatus | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[Memory]:
        """Most recent memories (REST ``GET /memories``, MCP ``mnemos_list_recent``).

        mnemos #400 — the QUERY boundary normalizes a non-empty
        ``project`` (single authority, same as ``search`` /
        ``recall_context``); ``None``/empty stays the unscoped listing.

        Ф2 (epic #308, ADR-0027 Phase 2) — ``task``: the first-class
        task-scope switcher, byte-identical to appending
        ``"task:<slug>"`` to ``tags`` (the F1 arm-C surface): the
        minted tag joins the caller's tags list, so both switcher forms
        thread the SAME ``tags`` predicate into ``sqlite.list_all``
        (exact-membership ``json_each`` match). Normalized/fail-loud at
        this boundary exactly like ``project`` (the #407 canon).
        """
        if project:
            from vesmaro.models import normalize_project_slug

            project = normalize_project_slug(project)
        if task is not None:
            task_tag = self._normalize_task_boundary(task)
            tags = [*(tags or []), task_tag]
        memories = self.sqlite.list_all(
            limit=limit,
            offset=offset,
            tags=tags,
            project=project,
            agent=agent,
            status=status,
            since=since,
            until=until,
        )
        # ADR-0019 §5 — absolute quarantine exclusion on the listing
        # surface too (REST GET /memories, MCP mnemos_list_recent): the
        # explicit-status drill-down must not serve terminal danger-lane
        # rows here — pipeline_state is not part of the external payload,
        # so the caller could not detect the contamination. Direct
        # get-by-id stays the documented residual until the B2 retraction.
        # Note: post-filtering may under-fill a limit/offset page —
        # accepted for a rare terminal state (same tradeoff as the
        # search legs).
        return [m for m in memories if not is_quarantined(m)]

    def list_tags(self) -> dict[str, int]:
        return self.sqlite.get_all_tags()

    def remove_no_federate(
        self,
        memory_id: str,
        *,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Remove the ``mnemos:no-federate`` tag from a record.

        Per ArchCom 2026-07-17 federation contract §4 КП-6, removing the
        tag re-enables external exchange for the record. Because the tag
        is typically auto-added by the Layer 1 secrets scanner when a
        secret was detected in the content, removing it blindly could
        expose a real secret to federation. This method therefore
        requires explicit confirmation (``confirm=True``).

        If ``confirm`` is False, the method returns a ``"requires_confirmation"``
        report WITHOUT mutating the record. The caller (CLI / HTTP / MCP)
        is responsible for surfacing the warning and re-calling with
        ``confirm=True`` after the user has acknowledged the risk.

        Re-scans the content after removal: if a secret is still present,
        the tag is re-added automatically and the report records
        ``"re_detected"=True``. The owner must redact the content first
        (see ``secrets_detector.redact_content``) before the tag can be
        permanently removed.
        """
        from vesmaro.models import NO_FEDERATE_TAG
        from vesmaro.secrets_detector import detect_secrets, findings_by_pattern

        report: dict[str, Any] = {
            "memory_id": memory_id,
            "removed": False,
            "re_detected": False,
            "requires_confirmation": not confirm,
            "patterns_present": {},
        }

        memory = self.sqlite.get(memory_id)
        if memory is None:
            report["error"] = f"Memory {memory_id} not found"
            return report

        if NO_FEDERATE_TAG not in memory.tags:
            report["note"] = "Record does not carry mnemos:no-federate"
            return report

        if not confirm:
            # Surface the risk without mutating.
            findings = detect_secrets(memory.content) if memory.content else []
            report["patterns_present"] = findings_by_pattern(findings) if findings else {}
            report["warning"] = (
                "Removing mnemos:no-federate re-enables external exchange. "
                "If a secret is still in the content, it will be exported. "
                "Re-call with confirm=True to proceed."
            )
            return report

        new_tags = [t for t in memory.tags if t != NO_FEDERATE_TAG]

        # Re-scan content: if a secret is still present, re-add the tag.
        re_detected_findings = detect_secrets(memory.content) if memory.content else []
        if re_detected_findings:
            new_tags.append(NO_FEDERATE_TAG)
            report["re_detected"] = True
            report["patterns_present"] = findings_by_pattern(re_detected_findings)
            report["warning"] = (
                "Secret still present in content — mnemos:no-federate re-added. "
                "Redact the content first (see secrets_detector.redact_content) "
                "to permanently remove the tag."
            )
        else:
            report["removed"] = True

        # Persist the new tag list (update_fields avoids INSERT OR REPLACE
        # FTS5 drift — see sqlite_store.update_fields).
        self.sqlite.update_fields(memory_id, tags=new_tags)
        return report

    def _commit_tags(
        self,
        mem: Memory,
        new_tags: list[str],
        *,
        dry_run: bool,
        strict: bool = False,
    ) -> tuple[bool, str | None]:
        """Validate and (unless ``dry_run``) persist a new tag set for one memory.

        Shared commit path for ``tags_rename`` / ``tags_remove`` / ``tags_add``
        (the grouped ``mnemos_tags`` MCP tool). Keeping the contract check and
        the ``update_fields`` write in one place guarantees every tag mutation
        goes through the same FTS5-safe ``UPDATE`` (the ``memories_au`` trigger
        fires) and the same ``validate_tag_contract`` gate.

        Args:
            mem: The memory row currently being processed.
            new_tags: The desired tag list for ``mem``.
            dry_run: When ``True`` nothing is written; the caller still counts
                the memory as "would change" so the report reflects intent.
            strict: Contract-validation mode for the resulting tag set.

                ``False`` (default, used by ``tags_rename``) — *lax*: a missing
                ``project:`` / ``agent:`` / ``mnemos:`` tag, an invalid
                ``mnemos:`` subtype, or a malformed slug is auto-patched in the
                returned list rather than rejected. Rename is a prefix swap
                (``gcw:`` → ``mnemos:``) that preserves required tags, so lax is
                the correct, non-corrupting mode there.

                ``True`` (used by ``tags_remove`` / ``tags_add``) — *strict*:
                any contract violation (including the soft ones lax would
                patch) raises ``TagContractError``, which this method reports as
                a per-memory error and skips the write. ``remove`` / ``add`` are
                explicit mutations, so a contract-breaking result (e.g. removing
                the last ``project:``, or adding an invalid ``mnemos:`` subtype)
                is rejected per memory instead of corrupting the store.

        Returns:
            ``(changed, error)``. ``changed`` is ``True`` when ``new_tags``
            differs from ``mem.tags`` AND passes the contract gate (and, when
            not ``dry_run``, the ``UPDATE`` succeeded). ``error`` is the
            ``"<id>: <reason>"`` string to append to the report's ``errors``
            list when validation or the write raised; ``None`` otherwise.

        Note:
            Re-derives the denormalised ``project`` / ``agent`` columns from
            the new tag set so a prefix change that touched ``project:`` /
            ``agent:`` keeps the denormalised columns aligned with the tags
            (otherwise per-project / per-agent queries drift). For the common
            ``gcw:`` → ``mnemos:`` rename these are unchanged.
        """
        from vesmaro.models import validate_tag_contract

        if new_tags == mem.tags:
            return False, None
        try:
            validate_tag_contract(new_tags, strict=strict)
        except Exception as exc:  # report, don't crash the batch
            return False, f"{mem.id}: {exc}"
        if dry_run:
            return True, None
        new_project = next(
            (t[len("project:") :] for t in new_tags if t.startswith("project:")),
            mem.project,
        )
        new_agent = next(
            (t[len("agent:") :] for t in new_tags if t.startswith("agent:")),
            mem.agent,
        )
        try:
            self.sqlite.update_fields(
                mem.id,
                tags=new_tags,
                project=new_project,
                agent=new_agent,
            )
        except Exception as exc:  # record, continue batch
            return False, f"{mem.id}: {exc}"
        return True, None

    def tags_rename(
        self,
        from_prefix: str,
        to_prefix: str,
        *,
        subtypes: list[str] | None = None,
        dry_run: bool = True,
        project: str | None = None,
        agent: str | None = None,
        invalid_subtypes_to_legacy: bool = False,
    ) -> dict[str, Any]:
        """Bulk rename tags matching ``from_prefix:<subtype>`` → ``to_prefix:<subtype>``.

        Replaces the unsafe legacy ``mnemos migrate tags`` path (which used raw
        ``sqlite3`` writes and bypassed the FTS5 ``AFTER UPDATE`` trigger).
        This method goes through ``SQLiteStore.update_fields`` (a plain
        ``UPDATE``), so the FTS5 external-content index stays consistent —
        the ``memories_au`` trigger fires on UPDATE, exactly like
        ``tags_normalize``.

        Args:
            from_prefix: Source prefix without the subtype (e.g. ``"gcw:"``).
            to_prefix: Target prefix without the subtype (e.g. ``"mnemos:"``).
            subtypes: Optional whitelist — only rename these subtypes.
                ``None`` means "all subtypes present on matching tags".
            dry_run: When ``True`` (default) nothing is written; the report
                describes what *would* happen. When ``False`` the rename is
                applied via ``update_fields``.
            project: Scope the scan to a single project slug (pre-filters
                via ``list_all(project=...)`` to reduce rows inspected).
            agent: Scope the scan to a single agent slug.
            invalid_subtypes_to_legacy: When ``False`` (default) a tag whose
                subtype is not in ``VESMARO_TAG_SUBTYPES`` is skipped and
                counted in ``skipped_invalid``. When ``True`` it is renamed
                to ``<to_prefix>legacy`` instead.

        Returns:
            ``{"scanned": N, "renamed": N, "changed": N, "skipped_invalid": N,
            "errors": [...]}``. ``changed`` mirrors ``renamed`` so every
            ``mnemos_tags`` action (rename/remove/add) exposes a ``changed``
            key for a uniform report shape; ``renamed`` is kept for back-compat
            with existing ``mnemos_tags_rename`` callers. In dry-run mode
            ``renamed`` reflects what *would* be renamed; nothing is written.

        Idempotency:
            A second run with the same arguments returns ``renamed=0``
            because the ``from_prefix:`` tags no longer exist.

        Vector store:
            The vector index is keyed by ``memory_id`` (see
            ``VectorStore.upsert``) and the embedded text is derived from
            ``title + content + tags`` (see ``_embedding_text``). Tags ARE
            part of the embedded text, so renaming tags *technically*
            changes the embedding input. However, re-embedding on every
            tag rename is expensive and the tag contribution to semantic
            similarity is small relative to content. We deliberately do
            NOT re-embed here — semantic search continues to work because
            the stored vectors still point to the same memory ids and the
            FTS5 leg (which DOES reflect the new tags via the AFTER UPDATE
            trigger) carries tag-filtered queries. If exact tag-vector
            alignment is required, run ``mnemos reindex`` (CLI legacy name) afterwards.
        """
        from vesmaro.models import VESMARO_TAG_SUBTYPES
        from vesmaro.traces import TraceRecorder

        report: dict[str, Any] = {
            "action": "rename",
            "scanned": 0,
            "renamed": 0,
            "changed": 0,  # alias of renamed for a uniform report shape
            "skipped_invalid": 0,
            "errors": [],
            "dry_run": dry_run,
            "from_prefix": from_prefix,
            "to_prefix": to_prefix,
        }

        # Validate prefix shapes early — must end with ":" so we don't
        # accidentally match `project:` when the caller means `gcw:`.
        if not from_prefix.endswith(":") or not to_prefix.endswith(":"):
            report["errors"].append("prefixes must end with ':' (e.g. 'gcw:', 'mnemos:')")
            return report

        subtype_filter = set(subtypes) if subtypes else None
        page_size = 500
        offset = 0
        # Trace the rename as a single audit row (one per call, not per row).
        recorder = TraceRecorder(store=self.sqlite)

        while True:
            batch = self.sqlite.list_all(
                limit=page_size, offset=offset, project=project, agent=agent
            )
            if not batch:
                break
            offset += len(batch)

            for mem in batch:
                report["scanned"] += 1
                new_tags: list[str] = []
                modified = False
                for tag in mem.tags:
                    if tag.startswith(from_prefix):
                        subtype = tag[len(from_prefix) :]
                        # Apply optional subtype whitelist.
                        if subtype_filter is not None and subtype not in subtype_filter:
                            new_tags.append(tag)
                            continue
                        # Decide target subtype.
                        if subtype in VESMARO_TAG_SUBTYPES:
                            target = to_prefix + subtype
                        elif invalid_subtypes_to_legacy:
                            target = to_prefix + "legacy"
                        else:
                            report["skipped_invalid"] += 1
                            new_tags.append(tag)
                            continue
                        new_tags.append(target)
                        if target != tag:
                            modified = True
                    else:
                        new_tags.append(tag)

                if not modified:
                    continue

                # Shared commit: contract check + FTS5-safe UPDATE. Both the
                # grouped ``mnemos_tags`` tool (action=rename alias) and the
                # legacy ``mnemos_tags_rename`` tool route through here, so
                # the behaviour is byte-identical.
                changed, err = self._commit_tags(mem, new_tags, dry_run=dry_run)
                if err:
                    report["errors"].append(err)
                if changed:
                    report["renamed"] += 1

        # ``changed`` mirrors ``renamed`` so the grouped ``mnemos_tags`` tool
        # exposes a uniform ``changed`` key across rename/remove/add.
        report["changed"] = report["renamed"]

        # Audit trail — one trace row per rename call.
        with recorder.record(
            task_label="tags_rename",
            project=project or "*",
            step="tags_rename",
        ) as trace:
            trace.rationale_summary = (
                f"{from_prefix}→{to_prefix} dry_run={dry_run} "
                f"renamed={report['renamed']} skipped={report['skipped_invalid']}"
            )[:200]

        return report

    def tags_remove(
        self,
        tags: list[str],
        *,
        wildcard: bool = False,
        dry_run: bool = True,
        project: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Remove tags from memories. Explicit removal — never a magic empty target.

        Backs the ``mnemos_tags`` MCP tool with ``action="remove"``. Each tag
        in ``tags`` is matched against every memory's tag set; matches are
        dropped. With ``wildcard=False`` (default) the match is exact; with
        ``wildcard=True`` each entry is treated as a prefix and any tag
        starting with it is removed (e.g. ``["gcw:"]`` strips every ``gcw:*``
        tag without rewriting them).

        Args:
            tags: Tags to remove. Exact match by default; prefix match when
                ``wildcard=True``.
            wildcard: Prefix match (``True``) vs exact match (``False``, default).
            dry_run: Preview only when ``True`` (default).
            project / agent: Scope the scan to a single project / agent slug.

        Returns:
            ``{action, scanned, changed, removed_tags, wildcard, errors,
            dry_run}``. ``changed`` counts memories whose tag set actually
            changed and (when not ``dry_run``) was written.

        Safety:
            Goes through ``_commit_tags`` → ``SQLiteStore.update_fields``
            (plain ``UPDATE``), so the FTS5 ``AFTER UPDATE`` trigger fires and
            the external-content index stays consistent — same path as
            ``tags_rename``. The resulting tag set is validated in **strict**
            mode: removing the last ``project:`` / ``agent:`` / ``mnemos:`` tag
            (or otherwise breaking the contract) is rejected per memory with an
            error entry instead of corrupting the store. Idempotent: a second
            run reports ``changed=0``.
        """
        from vesmaro.traces import TraceRecorder

        report: dict[str, Any] = {
            "action": "remove",
            "scanned": 0,
            "changed": 0,
            "removed_tags": list(tags),
            "wildcard": wildcard,
            "errors": [],
            "dry_run": dry_run,
        }
        if not tags:
            report["errors"].append("tags must be a non-empty list")
            return report

        matchers = list(tags)
        page_size = 500
        offset = 0
        recorder = TraceRecorder(store=self.sqlite)

        def _is_match(tag: str) -> bool:
            if wildcard:
                return any(tag.startswith(m) for m in matchers)
            return tag in matchers

        while True:
            batch = self.sqlite.list_all(
                limit=page_size, offset=offset, project=project, agent=agent
            )
            if not batch:
                break
            offset += len(batch)
            for mem in batch:
                report["scanned"] += 1
                new_tags = [t for t in mem.tags if not _is_match(t)]
                # Strict gate: removing the last project:/agent:/mnemos: tag
                # (or otherwise breaking the contract) is rejected per memory
                # with an error entry instead of corrupting the store. The
                # write is skipped for that memory; the batch continues.
                changed, err = self._commit_tags(mem, new_tags, dry_run=dry_run, strict=True)
                if err:
                    report["errors"].append(err)
                if changed:
                    report["changed"] += 1

        with recorder.record(
            task_label="tags_remove",
            project=project or "*",
            step="tags_remove",
        ) as trace:
            trace.rationale_summary = (
                f"remove tags={tags} wildcard={wildcard} dry_run={dry_run} "
                f"changed={report['changed']}"
            )[:200]
        return report

    def tags_add(
        self,
        tags: list[str],
        *,
        dry_run: bool = True,
        project: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Append tags to every memory matching the project/agent filter.

        Backs the ``mnemos_tags`` MCP tool with ``action="add"``. Each tag in
        ``tags`` is appended (if not already present) to every memory returned
        by the ``project`` / ``agent`` filter. When neither filter is set the
        operation spans all memories — callers should scope it deliberately.

        Args:
            tags: Tags to append. Each must have a prefix shape (contain
                ``":"``); the resulting full tag set is re-validated per
                memory via ``_commit_tags`` so adding a duplicate ``project:``
                (or any contract-breaking tag) errors on that memory instead
                of corrupting the store.
            dry_run: Preview only when ``True`` (default).
            project / agent: Scope the scan to a single project / agent slug.

        Returns:
            ``{action, scanned, changed, added_tags, errors, dry_run}``.
            ``changed`` counts memories whose tag set actually changed and
            (when not ``dry_run``) was written.

        Safety:
            Same ``_commit_tags`` → ``update_fields`` path as rename/remove,
            so FTS5 stays consistent. The full resulting set is validated in
            **strict** mode before any write: an added tag that breaks the
            contract (e.g. an invalid ``mnemos:`` subtype or a malformed slug)
            is rejected per memory with an error entry instead of corrupting
            the store.
        """
        from vesmaro.traces import TraceRecorder

        report: dict[str, Any] = {
            "action": "add",
            "scanned": 0,
            "changed": 0,
            "added_tags": list(tags),
            "errors": [],
            "dry_run": dry_run,
        }
        if not tags:
            report["errors"].append("tags must be a non-empty list")
            return report

        to_add = [t for t in tags if t]
        # Light structural pre-flight: each added tag must carry a prefix.
        # The full per-memory contract check (exactly one project:/agent:,
        # etc.) is delegated to _commit_tags on the resulting set.
        for t in to_add:
            if ":" not in t:
                report["errors"].append(f"tag must have a prefix (contain ':'): {t!r}")
                return report

        page_size = 500
        offset = 0
        recorder = TraceRecorder(store=self.sqlite)

        while True:
            batch = self.sqlite.list_all(
                limit=page_size, offset=offset, project=project, agent=agent
            )
            if not batch:
                break
            offset += len(batch)
            for mem in batch:
                report["scanned"] += 1
                new_tags = list(mem.tags)
                for t in to_add:
                    if t not in new_tags:
                        new_tags.append(t)
                # Strict gate: a tag whose addition breaks the contract (e.g.
                # an invalid mnemos: subtype, a malformed slug, or a tag that
                # leaves the set without exactly one project:/agent:) is
                # rejected per memory with an error entry; the write is skipped
                # for that memory and the batch continues.
                changed, err = self._commit_tags(mem, new_tags, dry_run=dry_run, strict=True)
                if err:
                    report["errors"].append(err)
                if changed:
                    report["changed"] += 1

        with recorder.record(
            task_label="tags_add",
            project=project or "*",
            step="tags_add",
        ) as trace:
            trace.rationale_summary = (
                f"add tags={tags} dry_run={dry_run} changed={report['changed']}"
            )[:200]
        return report

    def search_stats(self) -> dict[str, Any]:
        """Return in-memory search instrumentation (resets on restart).

        ADR-0030 A1-S1 (#325): also carries the per-project
        ``graph_epoch`` durable meta-counters (edge-write generations).
        The in-memory counters above reset on restart; the epochs do not
        — a consumer keying on them survives restarts.
        """
        with self._search_stats_lock:
            samples: list[float] = list(self._search_stats["latency_samples_ms"])
            counts: list[int] = list(self._search_stats["results_counts"])
        avg_latency_ms = round(sum(samples) / len(samples), 2) if samples else 0.0
        avg_results = round(sum(counts) / len(counts), 2) if counts else 0.0
        return {
            "requests_total": int(self._search_stats["requests_total"]),
            # A9: searches that ran in the explicit global mode
            # (``project=None``) — cross-project by definition.
            "cross_project_requests_total": int(self._search_stats["cross_project_requests_total"]),
            # Search v2 (issue #313): scoped searches that returned 0 rows
            # and were retried without the scope — the project-drift
            # (scope slug vs stored slug) audit signal.
            "project_scope_fallback_total": int(self._search_stats["project_scope_fallback_total"]),
            # ADR-0030 A0 (issue #324, review fix): enrichment split by
            # source leg — the unconditional supersedes leg and the
            # flag-gated relates_to walk. The WALK counter over
            # requests is the acceptance share the A0-review reads
            # alongside ``graph_mint_stats``; the supersedes counter is
            # the v1 leg's own (always-on) signal.
            "graph_supersedes_enriched_requests_total": int(
                self._search_stats["graph_supersedes_enriched_requests_total"]
            ),
            "graph_walk_enriched_requests_total": int(
                self._search_stats["graph_walk_enriched_requests_total"]
            ),
            # Ф2 (epic #308, ADR-0027 Phase 2) — the owner's standing
            # comparative metric: task-scoped queries split by switcher
            # form (the first-class task= parameter vs the raw
            # tags=["task:…"] form). See the _search_stats init comment
            # for the share arithmetic (a task= call lands in BOTH).
            "task_param_queries_total": int(self._search_stats["task_param_queries_total"]),
            "task_tag_queries_total": int(self._search_stats["task_tag_queries_total"]),
            # ADR-0030 A1-S2 (#325) — requests whose walked block used
            # feedback-influenced weights (see _search_stats init for the
            # acceptance-observable rationale).
            "feedback_boosted_queries_total": int(
                self._search_stats["feedback_boosted_queries_total"]
            ),
            # ADR-0030 A1-S1 (#325): per-project edge-write generations —
            # the meta-counter any ranking consumer can key on to
            # invalidate graph-derived state (no in-repo cache key
            # exists today; committee note: document keying in the
            # ADR-0030 addendum, not here).
            "graph_epoch_by_project": self._graph_epoch_snapshot(),
            # ADR-0030 A1-S2 (#325; ArchCom 2026-09-27, Security residual
            # 2): per-project FEEDBACK generations — bumped on edge_stats
            # CAPTURE (rows actually appended, captured > 0; idempotent
            # retries bump nothing). Once APPLY is live, an epoch-keyed
            # consumer must invalidate on feedback inserts too — a stale
            # feedback loop otherwise.
            "feedback_epoch_by_project": self._feedback_epoch_snapshot(),
            "avg_latency_ms": avg_latency_ms,
            "avg_results": avg_results,
        }

    def _graph_epoch_snapshot(self) -> dict[str, int]:
        """Read every project's ``graph_epoch`` from the meta table.

        Best-effort: a read failure yields ``{}`` (the search-stats
        surface must never fail on the graph leg's observability ride).
        """
        try:
            conn = self.sqlite._get_conn()
            rows = conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE 'graph_epoch:%'"
            ).fetchall()
        except Exception as exc:
            logger.warning("graph_epoch snapshot failed (non-fatal): %s", exc)
            return {}
        snapshot: dict[str, int] = {}
        for row in rows:
            key = str(row["key"])
            # Key format: graph_epoch:{len}:{project} — split on the
            # length prefix (project slugs may legally contain ':').
            parts = key.split(":", 2)
            if len(parts) != 3 or not parts[1].isdigit():
                continue
            plen = int(parts[1])
            if len(parts[2]) != plen:
                continue  # malformed — skip rather than guess
            try:
                snapshot[parts[2]] = int(str(row["value"]))
            except ValueError:
                continue
        return snapshot

    def _feedback_epoch_snapshot(self) -> dict[str, int]:
        """Read every project's ``feedback_epoch`` from the meta table.

        Mirrors ``_graph_epoch_snapshot`` exactly (the same best-effort
        contract: a read failure yields ``{}`` — the search-stats surface
        must never fail on the feedback leg's observability ride).
        """
        try:
            conn = self.sqlite._get_conn()
            rows = conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE 'feedback_epoch:%'"
            ).fetchall()
        except Exception as exc:
            logger.warning("feedback_epoch snapshot failed (non-fatal): %s", exc)
            return {}
        snapshot: dict[str, int] = {}
        for row in rows:
            key = str(row["key"])
            # Key format: feedback_epoch:{len}:{project} — the SAME
            # length-prefix discipline as graph_epoch (#415 review
            # finding: project slugs may legally contain ':' in other
            # namespaces, so the length prefix, not a bare split, is the
            # parse authority).
            parts = key.split(":", 2)
            if len(parts) != 3 or not parts[1].isdigit():
                continue
            plen = int(parts[1])
            if len(parts[2]) != plen:
                continue  # malformed — skip rather than guess
            try:
                snapshot[parts[2]] = int(str(row["value"]))
            except ValueError:
                continue
        return snapshot

    # ── used/rejected feedback capture (ADR-0030 A0, issue #323) ──────────

    def report_search_feedback(
        self,
        memory_ids: list[str],
        *,
        kind: str,
        project: str | None = None,
        agent: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Report used/rejected feedback for search citations (I5-capture).

        CAPTURE ONLY (ADR-0030 A0, issue #323): the event lands in the
        append-only ``edge_stats`` table and influences NOTHING — no
        ranking, no status, no eligibility. APPLY (rank-only, bounded Δ
        per principal) is slice A1 (#325).

        The report binds to the existing search call by design: the ids
        are the citation ids already riding the search response
        (``SearchResult.memory.id``), so the harness computes nothing
        new. ``used`` is NEVER auto-inferred from a search call itself —
        only an explicit report through this method captures.

        I5 gates (scoped, uniform-404): every id is validated under the
        SAME gates that would surface it in a search — the A9
        authoritative project guard plus the ADR-0018 context-admissible
        default set (published/processed; quarantine excluded absolutely
        by ADR-0019 §5). A nonexistent id, a cross-project id, an
        inadmissible-status id and a quarantined id ALL land in the same
        ``out_of_scope`` bucket: the response carries no per-id reason,
        so it is not an existence oracle.

        Idempotency (I5): pass a retry-stable ``event_id`` — per-row ids
        are derived from ``(event_id, memory, kind, principal)`` (review
        #338 N1), so an agent retrying the same report inserts nothing
        (``duplicates`` counts the idempotent drops), while a DIFFERENT
        principal or kind under the same logical id is a distinct,
        legitimately captured event. Without ``event_id`` each report
        mints fresh uuids (no cross-call idempotency — retrying callers
        must pass one).
        Residual (A0): validation uses the DEFAULT issuance gates;
        feedback on rows received via the documented widenings
        (``include_raw`` / explicit ``status=``) stays out of scope
        until APPLY (#325) needs it.

        Failure isolation: a capture-side failure (store error) never
        propagates — the method returns normally with the partial
        outcome and the error is counted + logged. Only caller bugs
        (unknown ``kind``) raise ``ValueError`` at this boundary.

        Behind the ``vesmaro.search.feedback_capture_enabled`` flag
        (DEFAULT OFF): flag off → zero writes, zero telemetry.

        Returns:
            ``{"kind", "flag_enabled", "reported", "captured",
            "duplicates", "out_of_scope", "cap_dropped"}`` — counts
            only, no per-id detail (uniform-404).
        """
        if kind not in EDGE_STATS_KINDS:
            raise ValueError(
                f"unknown feedback kind {kind!r}; supported kinds: {sorted(EDGE_STATS_KINDS)}"
            )
        # One event row per memory — duplicate ids within one report are
        # the same citation reported twice, not two events.
        unique_ids = list(dict.fromkeys(memory_ids))
        outcome: dict[str, Any] = {
            "kind": kind,
            "flag_enabled": bool(self.settings.search.feedback_capture_enabled),
            "reported": len(unique_ids),
            "captured": 0,
            "duplicates": 0,
            "out_of_scope": 0,
            "cap_dropped": 0,
        }
        if not outcome["flag_enabled"]:
            return outcome
        with self._feedback_stats_lock:
            self._feedback_stats["reports_total"] += 1
        # Normalized principal — the exact values stored on the row (the
        # explicit global mode is the ("", "") bucket). Kept stable so
        # the N1 id preimage (below) and the stored scope columns agree.
        norm_project = project or ""
        norm_agent = agent or ""
        try:
            for mid in unique_ids:
                if not self._feedback_target_visible(mid, project=project):
                    outcome["out_of_scope"] += 1
                    continue
                row_event_id = (
                    _derive_feedback_event_id(
                        event_id, mid, kind=kind, project=norm_project, agent=norm_agent
                    )
                    if event_id
                    else str(uuid.uuid4())
                )
                result = self.sqlite.record_edge_stat_event(
                    row_event_id,
                    mid,
                    kind=kind,
                    project=norm_project,
                    agent=norm_agent,
                )
                if result == "inserted":
                    outcome["captured"] += 1
                elif result == "duplicate":
                    outcome["duplicates"] += 1
                else:  # "cap_dropped" — I5 volume cap, not an error
                    outcome["cap_dropped"] += 1
        except Exception as exc:
            # Failure isolation (I5): a capture-side failure must never
            # fail the caller — count, log, return the partial outcome.
            with self._feedback_stats_lock:
                self._feedback_stats["errors_total"] += 1
            logger.warning("feedback capture failed (non-fatal), kind=%s: %s", kind, exc)
            return outcome
        with self._feedback_stats_lock:
            self._feedback_stats["duplicates_ignored_total"] += int(outcome["duplicates"])
            self._feedback_stats["out_of_scope_dropped_total"] += int(outcome["out_of_scope"])
            self._feedback_stats["cap_dropped_total"] += int(outcome["cap_dropped"])
        # ADR-0030 A1-S2 (#325; ArchCom 2026-09-27, Security residual 2):
        # bump the per-project ``feedback_epoch`` meta-counter ONLY when
        # rows were ACTUALLY APPENDED (captured > 0) — an idempotent
        # retry (all duplicates) or a fully-dropped report (cap/out of
        # scope) bumps nothing: the epoch tracks FEEDBACK-TABLE CHANGE,
        # not call volume (the same discipline as graph_epoch on
        # idempotent edge re-inserts). The bump key is the REPORTING
        # PRINCIPAL's project (norm_project; "" = the explicit global
        # bucket) — per-project by the same committee ruling that made
        # graph_epoch per-project (a global epoch is a cheap fleet-wide
        # cache-DoS lever). Best-effort: a meta-counter failure must
        # never fail the capture it decorates.
        if int(outcome["captured"]) > 0:
            self._bump_feedback_epoch(norm_project)
        if outcome["captured"] or outcome["cap_dropped"]:
            logger.info(
                "feedback capture: kind=%s captured=%d duplicates=%d "
                "out_of_scope=%d cap_dropped=%d",
                kind,
                int(outcome["captured"]),
                int(outcome["duplicates"]),
                int(outcome["out_of_scope"]),
                int(outcome["cap_dropped"]),
            )
        return outcome

    def _feedback_target_visible(self, memory_id: str, *, project: str | None) -> bool:
        """I5 scoping for feedback capture: would a search under the
        caller's scope surface this id?

        Same gates as the fused search legs: the A9 authoritative
        project guard (``project`` None/empty = the explicit global
        mode, mirroring search) and ``is_context_admissible`` — the
        ADR-0018 default issuance set with the ADR-0019 §5 absolute
        quarantine exclusion. Returns a bool ONLY: the caller must not
        learn WHY an id is invisible (uniform-404, no existence oracle).
        """
        mem = self.sqlite.get(memory_id)
        if mem is None:
            return False
        if project and (mem.project or "") != project:
            return False  # A9 authoritative guard — feedback never widens a scope
        return is_context_admissible(mem)

    def feedback_capture_stats(self) -> dict[str, Any]:
        """Feedback-capture telemetry (ADR-0030 A0, issue #323).

        ``events_total`` / ``captured_used_total`` /
        ``captured_rejected_total`` are DURABLE — read from the
        edge_stats table, so the A0-review D-behavioral signal survives
        restarts. Review #338 N3: all three numbers come from ONE
        ``GROUP BY kind`` aggregation (``count_edge_stats_by_kind``) —
        the previous shape issued three separate full-table scans per
        dashboard/stats read. ``since_restart`` is the in-memory window
        breakdown (drops, duplicates, errors): operational health of
        the capture leg, not volume.
        """
        enabled = bool(self.settings.search.feedback_capture_enabled)
        try:
            by_kind = self.sqlite.count_edge_stats_by_kind()
        except Exception as exc:
            logger.warning("feedback capture stats read failed: %s", exc)
            by_kind = {}
        used = int(by_kind.get("used", 0))
        rejected = int(by_kind.get("rejected", 0))
        # Exact, not an approximation: the SQL CHECK on kind guarantees
        # no row exists outside EDGE_STATS_KINDS, so the by-kind counts
        # partition the table.
        events_total = sum(by_kind.values())
        with self._feedback_stats_lock:
            window = dict(self._feedback_stats)
        return {
            "enabled": enabled,
            "events_total": events_total,
            "captured_used_total": used,
            "captured_rejected_total": rejected,
            "since_restart": window,
        }

    def graph_mint_stats(self) -> dict[str, Any]:
        """Return in-memory ``relates_to`` auto-minting instrumentation.

        ADR-0030 A0 (issue #322) — the minting-rate telemetry the
        A0-review reads: per-project NEW-edge counts since restart
        (in-memory like ``search_stats``; resets on restart, the rate is
        derivable from the deltas). ``''`` buckets unscoped writes.
        """
        with self._graph_mint_stats_lock:
            by_project = dict(self._graph_mint_stats)
        return {
            "auto_dedupe_edges_total": sum(by_project.values()),
            "auto_dedupe_edges_by_project": by_project,
        }

    def dashboard_stats(self) -> dict[str, Any]:
        """Structured JSON for the mnemos-eyes dashboard.

        Aggregates volume, filter, pipeline, search, vectors, sessions.
        """
        by_status = self.sqlite.count_by_status()
        filter_stats = self.sqlite.get_filter_stats()
        s_stats = self.search_stats()
        fb_stats = self.feedback_capture_stats()
        g_stats = self.graph_mint_stats()
        sessions = self.sqlite.count_sessions()
        # Pipeline counts derived from status + DLQ.
        processed_total = int(by_status.get("processed", 0)) + int(by_status.get("published", 0))
        return {
            "version": __version__,
            "timestamp": datetime.now(UTC).isoformat(),
            "volume": {
                "memories_total": self.sqlite.count(),
                "by_status": by_status,
                "by_project": self.sqlite.get_project_memory_counts(),
                "by_agent": self.sqlite.count_by_agent(),
                "by_type": self.sqlite.count_by_type(),
            },
            "filter": {
                "auto_filter": self.settings.mnemos.auto_filter,
                "filtered_total": filter_stats["filtered"],
                "unfiltered_total": filter_stats["unfiltered"],
                "avg_reduction_pct": filter_stats["avg_reduction_pct"],
                "by_profile": filter_stats["by_profile"],
            },
            "pipeline": {
                "processed_total": processed_total,
                "failed_total": self.sqlite.dlq_count(),
                "dlq_depth": self.sqlite.dlq_count(),
                "last_run": None,
            },
            "search": {
                "requests_total": s_stats["requests_total"],
                "cross_project_requests_total": s_stats["cross_project_requests_total"],
                "project_scope_fallback_total": s_stats["project_scope_fallback_total"],
                "graph_supersedes_enriched_requests_total": s_stats[
                    "graph_supersedes_enriched_requests_total"
                ],
                "graph_walk_enriched_requests_total": s_stats["graph_walk_enriched_requests_total"],
                "avg_latency_ms": s_stats["avg_latency_ms"],
                "avg_results": s_stats["avg_results"],
            },
            # ADR-0030 A0 (issue #323) — durable captured-event counters
            # (the D-behavioral telemetry the A0-review reads). Compact
            # projection of feedback_capture_stats(); the flag is OFF by
            # default, so the zeros shown before enablement are honest.
            "feedback_capture": {
                "enabled": fb_stats["enabled"],
                "events_total": fb_stats["events_total"],
                "captured_used_total": fb_stats["captured_used_total"],
                "captured_rejected_total": fb_stats["captured_rejected_total"],
            },
            # ADR-0030 A0 (issues #322/#324) — the fuel and walk legs:
            # flag states plus per-project NEW-edge counters (in-memory,
            # since restart). The walk share rides ``search`` above
            # (graph_walk_enriched_requests_total / requests_total —
            # split from the supersedes leg per the #324 review fix).
            "graph": {
                "auto_mint_enabled": self.settings.mnemos.graph_auto_mint,
                "walk_enabled": self.settings.mnemos.graph_walk,
                "feedback_apply_enabled": self.settings.mnemos.feedback_apply,
                "auto_dedupe_edges_total": g_stats["auto_dedupe_edges_total"],
                "auto_dedupe_edges_by_project": g_stats["auto_dedupe_edges_by_project"],
                # ADR-0030 A1-S1 (#325): durable per-project edge-write
                # generations (see search_stats()["graph_epoch_by_project"]).
                "graph_epoch_by_project": s_stats["graph_epoch_by_project"],
                # ADR-0030 A1-S2 (#325): durable per-project FEEDBACK
                # generations (capture events; see
                # search_stats()["feedback_epoch_by_project"]).
                "feedback_epoch_by_project": s_stats["feedback_epoch_by_project"],
                # ADR-0030 A1-S2 (#325): requests whose walked block used
                # feedback-influenced weights — the S2 acceptance
                # observable.
                "feedback_boosted_queries_total": s_stats["feedback_boosted_queries_total"],
            },
            "vectors": {
                "indexed_total": self.vectors.count(),
            },
            "sessions": {
                "active": sessions["active"],
                "total": sessions["total"],
            },
        }

    def timeseries(
        self,
        *,
        metric: str = "memories_added",
        days: int = 30,
        granularity: str = "day",
    ) -> dict[str, Any]:
        """Temporal data for dashboard charts.

        Currently supports ``memories_added`` (daily counts from SQLite).
        Other metrics return an empty series with a note.
        """
        if metric == "memories_added":
            points = self.sqlite.count_by_date(days=days, granularity=granularity)
        else:
            points = []
        return {
            "granularity": granularity,
            "range": f"{days}d",
            "series": [
                {
                    "metric": metric,
                    "points": points,
                }
            ],
        }

    def stats(self) -> dict[str, Any]:
        by_status = self.sqlite.count_by_status()
        filter_stats = self.sqlite.get_filter_stats()
        vector_count = self.vectors.count()
        published_count = int(by_status.get("published", 0))
        # Degraded: published memories exist but none are embedded —
        # vector search is silently unavailable, search degrades to FTS-only.
        degraded = vector_count == 0 and published_count > 0
        queue_depth = int(by_status.get("raw", 0)) + int(by_status.get("processing", 0))
        # ADR-0019 B2a: the processor now drains TWO queues in parallel —
        # the legacy RAW→PROCESSING clustering flow AND the async
        # refinement intake (pending + retry-budgeted failed rows; a
        # stably-failed row is not queued — it does not cycle forever).
        # ``queue_depth`` covers both so the loop's depth check and the
        # dashboards see the whole backlog; the breakdown is separate.
        pipeline_counts = self.sqlite.count_by_pipeline_state()
        refine_depth = self.sqlite.count_refine_intake()
        # Issue #445 — best-effort update advisory next to the version
        # stamp; None when disabled/offline/not pip-installed. Never raises.
        from vesmaro.updates import update_stats_payload

        return {
            "status": "ok",
            "version": __version__,
            "update_available": update_stats_payload(self.settings),
            "data_dir": str(self.settings.mnemos.data_dir),
            "vault_path": str(self.settings.mnemos.vault_path),
            "total": self.sqlite.count(),
            "by_status": by_status,
            "vectors": vector_count,
            "projects": self.sqlite.get_project_memory_counts(),
            "filter": {
                "auto_filter": self.settings.mnemos.auto_filter,
                "filtered_count": filter_stats["filtered"],
                "unfiltered_count": filter_stats["unfiltered"],
                "avg_reduction_pct": filter_stats["avg_reduction_pct"],
                "by_profile": filter_stats["by_profile"],
            },
            "embedding_status": {
                "provider": self.settings.embedding.provider,
                "vectors_indexed": vector_count,
                "degraded": degraded,
            },
            "processor": {
                "queue_depth": queue_depth + refine_depth,
                "legacy_queue_depth": queue_depth,
                "refine_queue_depth": refine_depth,
                "pipeline_states": pipeline_counts,
                # Pipeline runs record their finish time in the meta table;
                # None means the pipeline has never run yet.
                "last_processed_at": self.sqlite.get_meta("pipeline_last_run"),
            },
            # ADR-0027 Ф3 (epic #308, review round P2-2): the doc-chunk
            # ccr_cache invalidation counter — the graph_epoch posture:
            # EXPOSED for any assembly-cache consumer, bumped
            # transactionally on every document re-fragmentation; no
            # in-repo consumer keys on it yet (the counter is the
            # contract, not a wired cache-bypass).
            "doc_chunk_cache_version": self.sqlite.doc_chunk_cache_version(
                DOC_CHUNK_CACHE_VERSION_META_KEY
            ),
            "search_health": {
                "fts_available": True,  # FTS5 is always available (SQLite built-in)
                "vector_available": vector_count > 0,
                "mode": "hybrid" if vector_count > 0 else "fts_only",
                # Orphaned vectors: embeddings exist but no published memories
                # — indicates the vector store drifted out of sync with SQLite
                # (e.g. memories were deleted but vectors were not removed).
                "orphaned_vectors": vector_count > 0 and published_count == 0,
            },
        }

    # ── Path-scoped rules ingest (M8) ───────────────────────────────────────

    def ingest_path_scoped_rules(
        self,
        rules_dir: str | Path,
        *,
        project: str = "",
        agent: str = "",
        pattern: str = "*.instructions.md",
    ) -> list[dict[str, Any]]:
        """Scan a directory for `*.instructions.md` files and ingest them."""
        from vesmaro.watchers.path_scoped import ingest_path_scoped_rules as _ingest

        return _ingest(self, Path(rules_dir), project=project, agent=agent, pattern=pattern)

    def remove_path_scoped_rule(self, file_path: str | Path) -> dict[str, Any]:
        """Remove a single rule by file path."""
        from vesmaro.watchers.path_scoped import remove_path_scoped_rule as _remove

        return _remove(self, Path(file_path))

    # ── Context Filter (M10) ─────────────────────────────────────────────────

    def apply_context_filter(
        self,
        memory_id: str,
        *,
        profile: str | None = None,
        budget: int | None = None,
    ) -> dict[str, Any]:
        """Run the 5-stage context filter on a memory's raw_content.

        Updates memory.clean_content, filter_profile, filter_stats, filter_version.
        """
        from vesmaro.filter.pipeline import apply_filter

        memory = self.sqlite.get(memory_id)
        if memory is None:
            return {"status": "error", "error": f"Memory {memory_id} not found"}

        raw = memory.raw_content or memory.content
        if not raw:
            return {"status": "error", "error": "No content to filter"}

        result = apply_filter(raw, profile=profile, budget=budget)

        memory.clean_content = result["clean_content"]
        memory.filter_profile = result["profile"]
        memory.filter_stats = result["stats"]
        memory.filter_version = result["version"]
        memory.updated_at = datetime.now(UTC)

        # Use targeted UPDATE (not save()/INSERT OR REPLACE) to avoid
        # changing the rowid — FTS5 external-content tables lose sync
        # when INSERT OR REPLACE fires delete+insert triggers.
        # updated_at is set automatically by update_fields().
        self.sqlite.update_fields(
            memory.id,
            clean_content=memory.clean_content,
            filter_profile=memory.filter_profile,
            filter_stats=memory.filter_stats,
            filter_version=memory.filter_version,
        )

        return {
            "status": "ok",
            "memory_id": memory_id,
            "clean_content": result["clean_content"],
            "filter_profile": result["profile"],
            "stats": result["stats"],
        }

    def issue_context_filter(
        self,
        memory_id: str,
        *,
        profile: str | None = None,
        budget: int | None = None,
        project: str | None = None,
        channel: str = "issue_context_filter",
    ) -> dict[str, Any]:
        """Issuance-boundary twin of :meth:`apply_context_filter` (M1 fix).

        ``apply_context_filter`` is the maintenance primitive — it is also
        called internally on ingest (auto-filter) and by ``filter_all``,
        so it cannot carry the context gates. THIS method is what
        content-echoing channels (MCP ``mnemos_filter``, REST
        ``POST /filter/{id}``) must call; it enforces the ADR-0018 entry
        invariant on the echoed ``clean_content``:

        * **status gate** — only ``CONTEXT_ADMISSIBLE_STATUSES``
          (``published`` / ``processed``) memories are filterable into
          context; ``raw`` / ``processing`` / ``archived`` refuse
          (fail-closed) instead of echoing stored raw content. ADR-0019
          §5 composes the quarantine exception into the same predicate:
          ``pipeline_state='quarantined'`` refuses identically
          (terminal danger-lane state, excluded from issuance).
        * **project scope** — with ``project`` given, the memory must
          belong to it; the error deliberately does not distinguish
          "no such memory" from "another project's memory" (same
          wording discipline as the ``supersedes`` check — no global
          existence oracle). Without ``project`` the call is explicit
          operator semantics, mirroring ``GET /memories/{id}`` which
          reads by id without a caller project.
        * **issuance scan** — the returned ``clean_content`` passes
          :meth:`scan_issuance_item`; refuse mode drops the content
          (error shape, no echo) exactly like the other issuance
          channels, redact mode returns the redacted copy plus
          ``redactions`` / ``redacted_patterns`` counts.

        Returns ``{"status": "error", "error": …, "reason": …}`` on any
        gate failure (``reason`` ∈ ``not_found`` / ``status_gate`` /
        ``project_scope`` / ``refused``) and the ``apply_context_filter``
        success shape (plus redaction counts) otherwise. The stored
        filter fields are updated exactly as before — storage stays
        zero-loss; only the echo is scanned.
        """
        memory = self.sqlite.get(memory_id)
        if memory is None:
            return {
                "status": "error",
                "error": f"Memory {memory_id} not found",
                "reason": "not_found",
            }
        if not is_context_admissible(memory):
            logger.warning(
                "issue_context_filter: STATUS GATE memory=%s status=%s "
                "pipeline_state=%s channel=%s",
                memory_id,
                memory.status.value,
                memory.pipeline_state.value if memory.pipeline_state else None,
                channel,
            )
            return {
                "status": "error",
                "error": (
                    f"Memory {memory_id} not filterable into context "
                    f"(status={memory.status.value}; requires published/processed"
                    f"{' and not quarantined' if is_quarantined(memory) else ''})"
                ),
                "reason": "status_gate",
            }
        if project is not None and (memory.project or "") != project:
            logger.warning(
                "issue_context_filter: PROJECT SCOPE memory=%s channel=%s",
                memory_id,
                channel,
            )
            return {
                "status": "error",
                "error": f"Memory {memory_id} not found in project {project!r}",
                "reason": "project_scope",
            }

        result = self.apply_context_filter(memory_id, profile=profile, budget=budget)
        if result.get("status") == "error":
            # The primitive's two error shapes, disambiguated for the
            # channel mapping: not-found vs. no-content-to-filter.
            result["reason"] = (
                "no_content" if "No content" in str(result.get("error")) else "not_found"
            )
            return result

        scan = self.scan_issuance_item(result["clean_content"], context=f"{channel}:{memory_id}")
        if scan.refused:
            return {
                "status": "error",
                "error": f"issuance refused: {scan.reason}",
                "reason": "refused",
            }
        issued = dict(result)
        issued["clean_content"] = scan.content
        issued["redactions"] = scan.redactions
        if scan.redactions:
            issued["redacted_patterns"] = scan.redacted_patterns
        return issued

    def filter_all(
        self,
        *,
        profile: str | None = None,
        budget: int | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """Re-apply the context filter to all (or a batch of) memories.

        Used by `mnemos filter --all`. Iterates memories in batches via
        ``sqlite.list_all`` and calls ``apply_context_filter`` on each.
        Failures on individual memories are non-fatal and counted.

        Returns aggregate stats: total, filtered, failed, skipped.
        """
        total = self.sqlite.count()
        offset = 0
        filtered = 0
        failed = 0
        skipped = 0
        seen = 0
        while seen < total:
            batch = self.sqlite.list_all(limit=limit, offset=offset)
            if not batch:
                break
            for memory in batch:
                seen += 1
                if not (memory.raw_content or memory.content):
                    skipped += 1
                    continue
                try:
                    result = self.apply_context_filter(memory.id, profile=profile, budget=budget)
                    if result.get("status") == "ok":
                        filtered += 1
                    else:
                        failed += 1
                except Exception as exc:
                    logger.warning("filter_all: failed %s: %s", memory.id, exc)
                    failed += 1
            offset += len(batch)
            if len(batch) < limit:
                break
        return {
            "status": "ok",
            "total": total,
            "filtered": filtered,
            "failed": failed,
            "skipped": skipped,
        }

    # ── Ingestion ────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_url(url: str) -> str:
        """Validate URL for SSRF safety. Raises ValueError on blocked schemes or hosts.

        W4c extraction: the guard body moved verbatim to
        :func:`vesmaro.http_guard.validate_url_ssrf` — the Jev decision
        adapter (``vesmaro.decision_jev``) is the engine's second outbound
        HTTP leg, and the guard must be one function, not a per-caller
        copy that drifts. Behavior is byte-identical; the SSRF pin tests
        (``test_security``, ``test_ssrf_redirect``) are unchanged.
        """
        from vesmaro.http_guard import validate_url_ssrf

        return validate_url_ssrf(url)

    def ingest_url(self, url: str, *, tags: list[str], project: str, agent: str) -> Memory:
        """Fetch a URL, extract main text, save as RAW memory.

        Redirects (3xx) are followed manually with a hard cap of ``_MAX_REDIRECTS``
        hops. Every redirect target is passed through ``_validate_url`` before
        the next request is issued (per-hop SSRF guard, v2). This closes the
        open-redirect pivot where a public host returns 30x to an internal or
        metadata endpoint that would otherwise bypass the initial URL check.

        ``httpx.Client(follow_redirects=False)`` is retained so the library
        never follows a redirect without our guard running first.

        SSRF rejections (``ValueError`` from ``_validate_url``) are re-raised
        as hard errors — the blocked URL is NOT stored in memory. Only
        network/operational errors (connection, timeout, HTTP error status,
        too-many-redirects, redirect-loop) degrade to a placeholder.
        """
        # Initial SSRF validation — reject before any fetch attempt.
        # A ValueError here must NOT be swallowed into placeholder content.
        self._validate_url(url)

        try:
            from urllib.parse import urljoin

            import httpx
            import trafilatura

            # Per-hop SSRF re-validation (v2): follow redirects manually so
            # every Location target is checked by _validate_url before the
            # next request is issued. follow_redirects=False on the client
            # ensures httpx never silently skips the guard.
            current_url = url
            visited: set[str] = set()
            redirects = 0

            with httpx.Client(follow_redirects=False) as client:
                resp = client.get(current_url, timeout=30)
                visited.add(current_url)

                while resp.status_code in {301, 302, 303, 307, 308}:
                    redirects += 1
                    if redirects > _MAX_REDIRECTS:
                        raise ValueError(
                            f"Too many redirects fetching {url} (max {_MAX_REDIRECTS})"
                        )
                    location = resp.headers.get("location", "")
                    if not location:
                        raise ValueError(f"Redirect from {current_url} missing Location header")
                    next_url = urljoin(current_url, location)
                    # Core per-hop guard: validate the redirect target BEFORE
                    # following. Catches the pivot: public host -> 169.254.x
                    # or any private/loopback/metadata endpoint. A ValueError
                    # here is an SSRF rejection — wrap it so the outer
                    # ``except Exception`` does not swallow it into a placeholder.
                    try:
                        self._validate_url(next_url)
                    except ValueError as exc:
                        raise _SSRFRejectionError(exc) from exc
                    if next_url in visited:
                        raise ValueError(f"Redirect loop detected at {next_url}")
                    visited.add(next_url)
                    current_url = next_url
                    resp = client.get(current_url, timeout=30)

            resp.raise_for_status()
            content = trafilatura.extract(resp.text) or resp.text[:4000]
        except _SSRFRejectionError as exc:
            # SSRF guard rejected a URL — do NOT store it in memory.
            raise ValueError(f"URL rejected for security reasons: {exc.original}") from exc.original
        except Exception as exc:
            logger.warning("URL fetch failed: %s - using placeholder", exc)
            content = f"URL: {url}\n[fetch failed: {exc}]"

        data = MemoryCreate(
            content=content,
            title=url.split("//")[-1][:80],
            tags=tags,
            source=MemorySource.WEB,
            source_url=url,
        )
        return self.add(data, project=project, agent=agent)

    def ingest_document(
        self,
        text: str,
        *,
        doc_id: str,
        title: str | None,
        tags: list[str],
        project: str,
        agent: str,
        source_url: str | None = None,
    ) -> DocIngestResult:
        """ADR-0027 Ф3 (epic #308) — ingest a document as doc-chunk rows.

        Thin wrapper over :func:`vesmaro.docs_ingest.ingest_document`
        (the single authority for the Ф3 lifecycle: chunk →
        born-quarantine → danger-sweep → release/refuse). The chunks
        are BORN QUARANTINED (ADR-0027 invariant 8 — untrusted
        content) and released per-chunk by the ADR-0019 Phase A danger
        detector at ingest-completion; a re-ingest of the same
        ``doc_id`` replaces the rows and bumps the ccr_cache doc-chunk
        version in the same transaction (invariant 4).

        This is the DOCUMENT surface; the single-URL :meth:`ingest_url`
        keeps its pre-Ф3 semantics (one row, the ordinary visibility
        policy, no born-quarantine) — the boundary is pinned in the Ф3
        tests and documented on both tool surfaces.
        """
        return docs_ingest_document(
            self,
            text,
            doc_id=doc_id,
            title=title,
            tags=tags,
            project=project,
            agent=agent,
            source_url=source_url,
        )

    # ── Project graph (ADR-0032, slice 6 wiring) ────────────────────────────

    def get_codegraph_service(self) -> CodeGraphService | None:
        """The per-manager :class:`CodeGraphService` (lazy, built once,
        weak-keyed in :func:`vesmaro.codegraph.service.get_graph_service`
        — a discarded manager takes its sidecar connections with it).

        Returns ``None`` when the master flag
        ``settings.code_graph.enabled`` is off (the default): every
        caller (beacon, watch, future surfaces) silently skips — the
        graph is a full opt-in mechanism (ADR-0032 wave gate).
        """
        if not self.settings.code_graph.enabled:
            return None
        from vesmaro.codegraph.service import get_graph_service

        return get_graph_service(self)

    def codegraph_activity_hint(
        self,
        project: str | None,
        *,
        cwd: str | None = None,
        agent: str | None = None,
        session: str | None = None,
    ) -> bool:
        """Native auto-index hint (PG-0.5, owner directive 2026-09-29).

        One cheap entry for the MCP dispatcher and the ``pre_llm_call``
        hook: flag-check here, then a NON-BLOCKING queue put on the
        shared-scheduler auto-indexer (marker-gated auto-registration +
        background first-index / stale-reindex, per-project throttled —
        see ``vesmaro.codegraph.autoindex``). The hint NEVER raises and
        never breaks the caller's tool call; returns whether it was
        queued. ``agent`` attribution is mandatory on the auto path
        (PG7 — no agent, no auto action).
        """
        cfg = self.settings.code_graph
        if not (cfg.enabled and cfg.auto_index):
            return False
        if not isinstance(project, str) or not project.strip():
            return False
        if agent is not None and (not isinstance(agent, str) or not agent.strip()):
            agent = None
        if session is not None and (not isinstance(session, str) or not session.strip()):
            session = None
        try:
            service = self.get_codegraph_service()
            if service is None:
                return False
            if self._graph_autoindex is None:
                from vesmaro.codegraph.autoindex import AutoIndexer

                self._graph_autoindex = AutoIndexer(
                    service,
                    self._get_graph_scheduler(service),
                    config=cfg,
                )
            return self._graph_autoindex.hint(
                project.strip(),
                cwd=cwd or os.getcwd(),
                agent=agent,
                session=session,
            )
        except Exception:
            logger.debug("codegraph activity hint skipped (wiring error)", exc_info=True)
            return False

    def _get_graph_scheduler(self, service: CodeGraphService) -> GraphWatchScheduler:
        """The ONE cooperative scheduler thread shared by the watch poll
        and the auto-indexer (built lazily, once per manager)."""
        if self._graph_watch is None:
            from vesmaro.codegraph.watch import GraphWatchScheduler

            cfg = self.settings.code_graph
            self._graph_watch = GraphWatchScheduler(
                service,
                base_interval_sec=cfg.watch_base_interval_sec,
                interval_per_500_files=cfg.watch_interval_per_500_files,
                max_interval_sec=cfg.watch_max_interval_sec,
            )
        return self._graph_watch

    # ── Watchers (project-graph poll, contract §3.2 trigger (b)) ────────────

    def watch_start(
        self,
        project_id: str | None = None,
        *,
        agent: str | None = None,
        session: str | None = None,
        paths: list[str] | None = None,
        scan: bool = True,
        include_rules: bool = False,
    ) -> dict[str, Any]:
        """Register a project for the in-process watch poll (ADR-0032
        §3.2 trigger (b) — the former M8 stub stops being a stub).

        One cooperative poll thread (no daemon, no separate process)
        checks the project's graph files by mtime+size on the adaptive
        interval and reindexes on ACTUAL changes — audited with reason
        ``watch``. Requires ``code_graph.enabled`` AND ``code_graph.watch``
        (default ON — the poll stays inert until an explicit
        registration), agent attribution (PG7), an EXISTING index (the
        poll reindexes — it never seeds a first index) and room under
        the global registration cap.

        The legacy M8 vault-watcher keyword form (``paths=`` /
        ``scan=`` / ``include_rules=``) is refused with an actionable
        error instead of the old silent no-op: it was never implemented,
        and the fake "✅ Watcher started" it produced was a lie.
        """
        if paths is not None or not scan or include_rules:
            raise ValueError(
                "watch_start is the project-graph poll registrar (ADR-0032 §3.2): "
                "pass project_id and agent. The M8 vault-watcher form "
                "(paths=/scan=/include_rules=) was never implemented and the "
                "stub it targeted is gone."
            )
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("watch_start requires a non-empty project_id")
        cfg = self.settings.code_graph
        if not cfg.enabled:
            from vesmaro.codegraph.service import GraphDisabledError

            raise GraphDisabledError(
                "project graph is disabled (settings.code_graph.enabled=false); "
                "the operator must opt in (ADR-0032: default-off until validated)"
            )
        if not cfg.watch:
            from vesmaro.codegraph.service import GraphToolError

            raise GraphToolError(
                "the watch poll is disabled (settings.code_graph.watch=false) — "
                "a sidecar-mutating poll is a separate opt-in from the master flag"
            )
        if not isinstance(agent, str) or not agent.strip():
            from vesmaro.codegraph.service import GraphToolError

            raise GraphToolError("agent attribution is required (PG7): pass the caller's agent id")
        service = self.get_codegraph_service()
        assert service is not None  # enabled ⇒ the lazy build always yields one
        return self._get_graph_scheduler(service).register(
            project_id.strip(),
            agent=agent.strip(),
            session=session.strip() if isinstance(session, str) else None,
            cap=cfg.watch_max_registrations,
        )

    def watch_stop(self, project_id: str | None = None) -> dict[str, Any]:
        """Stop one watch registration (by project id or graph key) or
        ALL of them when no argument is given. Idempotent."""
        scheduler = self._graph_watch
        if scheduler is None:
            return {"stopped": 0}
        return {"stopped": scheduler.stop(project_id)}

    def watch_status(self) -> dict[str, Any]:
        """Active watch registrations + the last poll outcome per
        project (the legacy ``running`` key stays top-level for the
        existing MCP/REST consumers)."""
        cfg = self.settings.code_graph
        scheduler = self._graph_watch
        payload: dict[str, Any] = {
            "running": False,
            "watch_enabled": cfg.enabled and cfg.watch,
            "cap": cfg.watch_max_registrations,
            "registrations": [],
        }
        if scheduler is not None:
            payload.update(scheduler.status())
        return payload

    # ── Pipeline (M4) ───────────────────────────────────────────────────────

    def cluster(
        self,
        *,
        project: str | None = None,
        agent: str | None = None,
        limit: int = 100,
        similarity_threshold: float = 0.82,
        min_cluster_size: int = 2,
    ) -> list[ClusterResult]:
        """Run the cluster worker on raw memories."""
        from vesmaro.pipeline.cluster import cluster_raw_memories

        return cluster_raw_memories(
            self,
            project=project,
            agent=agent,
            limit=limit,
            similarity_threshold=similarity_threshold,
            min_cluster_size=min_cluster_size,
        )

    def synthesize(
        self,
        cluster_id: str,
        *,
        prompt_version: str = "v1",
        force: bool = False,
    ) -> SynthesisResult | None:
        """Run the synthesis worker on a cluster."""
        from vesmaro.pipeline.synthesize import synthesize_cluster

        return synthesize_cluster(self, cluster_id, prompt_version=prompt_version, force=force)

    def quality_gate(self, memory_id: str) -> QualityResult:
        """Run quality gates on a processed memory."""
        from vesmaro.pipeline.quality_gate import evaluate_quality

        return evaluate_quality(self, memory_id)

    def publish(self, memory_id: str, *, skip_quality_check: bool = False) -> PublishResult:
        """Publish a processed memory and index it in the vector store."""
        from vesmaro.pipeline.publish import publish_memory

        return publish_memory(self, memory_id, skip_quality_check=skip_quality_check)

    # ── ADR-0019 Phase B2a: async refinement + quarantine ────────────────

    def refine_pending(
        self,
        *,
        project: str | None = None,
        agent: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Drain the refine intake (§10-B daemon pickup).

        Runs the B2a cycle over rows with ``pipeline_state='pending'``
        (plus ``failed`` rows with retry budget left): atomic claim →
        artifact → §6 swap / noop / lane-(a) failure / lane-(b)
        quarantine. The legacy RAW→PROCESSING clustering queue is a
        SEPARATE flow (``run_pipeline``) and is untouched here.
        """
        from vesmaro.pipeline.refine import refine_pending as _refine_pending

        return _refine_pending(self, project=project, agent=agent, limit=limit)

    def _curated_publish(self, memory_id: str) -> str:
        """B2b §2 curated completion — visibility through the SAME gate.

        Called by the refine cycle when a curated row reaches its
        completion point (``refined`` — with a swapped artifact or as an
        honest noop). In curated mode the row is still RAW (invisible);
        the refined projection now passes the publication gate:

        * clean → ``PUBLISHED`` + vector upsert: the record receives the
          refined content and the visibility in the same completion
          event (§2: "clean → PUBLISHED, the record receives the
          refined content and visibility simultaneously");
        * danger signal or scanner error → the lane-(b) quarantine per
          the B2a convention (terminal, ``quarantine_reason`` carries
          the detector class codes / ``detector-error``, embed removed,
          excluded from issuance).

        Non-curated deployments and already-visible rows are a no-op
        (``skipped``) — in immediate mode visibility was granted at
        ingest and nothing here may demote it.

        Returns the outcome code: ``published`` / ``quarantined`` /
        ``skipped``.
        """
        if self.settings.mnemos.visibility != "curated":
            return "skipped"
        memory = self.sqlite.get(memory_id)
        if memory is None or memory.status != MemoryStatus.RAW:
            return "skipped"
        detection = self._publish_gate_detection(memory, path="curated-publish")
        if detection.error is not None or detection.positive:
            from vesmaro.pipeline.refine import QUARANTINE_REASON_DETECTOR_ERROR

            reason = (
                QUARANTINE_REASON_DETECTOR_ERROR
                if detection.error is not None
                else ",".join(sorted({f.detector_class for f in detection.findings}))
            )
            self.quarantine_entry(memory_id, reason=reason, source="curated-publish")
            return "quarantined"
        self.sqlite.update_fields(memory_id, status=MemoryStatus.PUBLISHED)
        published = self.sqlite.get(memory_id)
        if published is not None:
            try:
                self.upsert_embedding(published)
            except Exception as exc:
                logger.warning(
                    "curated publish: vector upsert failed for %s (sweeper will heal): %s",
                    memory_id[:8],
                    exc,
                )
        logger.info("pipeline: id=%s outcome=curated-published", memory_id[:8])
        return "published"

    def quarantine_entry(self, memory_id: str, *, reason: str, source: str = "manual") -> bool:
        """Lane-(b) quarantine: state+reason only, statuses untouched.

        The row is excluded from every issuance path by the B1
        ``is_quarantined`` predicate; its vector embed is REMOVED (an
        excluded row needs no embed, and a stale one must not keep the
        id warm in the vector leg). Terminal until
        :meth:`release_quarantine`.

        Returns ``False`` when the row does not exist.
        """
        memory = self.sqlite.get(memory_id)
        if memory is None:
            return False
        ok = self.sqlite.update_fields(
            memory_id, pipeline_state=PipelineState.QUARANTINED, quarantine_reason=reason
        )
        if not ok:
            return False
        try:
            self.vectors.delete(memory_id)
        except Exception as exc:
            logger.warning(
                "quarantine: embed removal failed for %s (non-fatal): %s", memory_id[:8], exc
            )
        logger.warning(
            "pipeline: id=%s outcome=quarantined reason=%s source=%s",
            memory_id[:8],
            reason,
            source,
        )
        return True

    def release_quarantine(self, memory_id: str, *, source: str = "manual") -> bool:
        """Manual release of a quarantined row (§5 terminality).

        Returns the row to ``failed`` — NOT to refined/pending: a
        quarantined projection requires human review, and ``failed`` is
        the honest "needs a new cycle" resting state (the daemon retries
        it through the lane-(a) intake with a FRESH retry budget). The
        quarantine reason is cleared and the transition is audited.
        Returns ``False`` when the row is missing or not quarantined.
        """
        memory = self.sqlite.get(memory_id)
        if memory is None or memory.pipeline_state != PipelineState.QUARANTINED:
            return False
        ok = self.sqlite.update_fields(
            memory_id, pipeline_state=PipelineState.FAILED, quarantine_reason=None
        )
        if not ok:
            return False
        self.sqlite.clear_refine_retry(memory_id)
        logger.warning(
            "pipeline: id=%s outcome=quarantine-released to=failed source=%s reason_cleared=%s",
            memory_id[:8],
            source,
            memory.quarantine_reason or "none",
        )
        return True

    def reclaim_stale_refinements(self, *, limit: int = 100) -> dict[str, Any]:
        """Issue #170 (ADR-0019 Phase C) — lease-expired ``processing``
        rows go back to ``pending`` (the idempotent sweeper pass).

        A worker crash between :meth:`refine_single`'s atomic claim and
        its outcome write strands the claimed rows in ``processing``
        with no path back into the queue; this pass is that path. Lease
        expiry is decided on ``updated_at`` against
        :data:`REFINE_LEASE_TIMEOUT_SEC <vesmaro.pipeline.refine.REFINE_LEASE_TIMEOUT_SEC>`
        (the claim stamps the clock; every outcome write refreshes it),
        and the store's per-row CAS makes the reclaim safe under two
        concurrent sweepers — exactly one wins per row. The retry
        counter is deliberately untouched: a lease expiry is not a
        lane-(a) failure and must not eat the row's retry budget.

        Audit: one ``pipeline: id=… outcome=lease-reclaimed age=…s``
        warning per reclaimed row (an expired lease means a worker died
        mid-flight — an anomaly worth surfacing, not a routine event).

        Returns ``{"reclaimed": n, "reclaimed_ids": […],
        "lease_timeout_sec": …}``.
        """
        from vesmaro.pipeline.refine import OUTCOME_LEASE_RECLAIMED, REFINE_LEASE_TIMEOUT_SEC

        now = datetime.now(UTC)
        reclaimed = self.sqlite.reclaim_stale_processing(
            lease_timeout_sec=REFINE_LEASE_TIMEOUT_SEC, limit=limit
        )
        for memory_id, lease_started_iso in reclaimed:
            age_sec = int((now - datetime.fromisoformat(lease_started_iso)).total_seconds())
            logger.warning(
                "pipeline: id=%s outcome=%s age=%ds",
                memory_id[:8],
                OUTCOME_LEASE_RECLAIMED,
                age_sec,
            )
        return {
            "reclaimed": len(reclaimed),
            "reclaimed_ids": [memory_id for memory_id, _ in reclaimed],
            "lease_timeout_sec": REFINE_LEASE_TIMEOUT_SEC,
        }

    def heal_stale_embeddings(self, *, limit: int = 200) -> dict[str, Any]:
        """Idempotent sweeper: re-embed ``refined`` rows with stale vectors.

        ADR-0019 §Swap: the vector upsert happens after the swap commit;
        when it fails (or predates a swap), the embed no longer
        represents the served projection. Two freshness keys decide
        staleness, both stamped on every upsert (see
        :meth:`_vector_metadata`):

        * ``content_hash`` — missing embed, missing key or hash mismatch
          ⇒ the vector was cut from other content ⇒ re-embed.
        * ``model_fingerprint`` — missing key or fingerprint mismatch ⇒
          the vector was cut by another embedder (ADR-0021 weights
          swap) ⇒ re-embed even though the content is unchanged; a
          mixed-space index silently degrades vector search.

        Quarantined rows are skipped absolutely (they never reach
        ``refined`` anyway; the guard is defence in depth). Batch/limit
        semantics: ``limit`` is an ATTEMPT budget — every re-embed
        TRIED, successful or failed, consumes one unit, so one call
        attempts at most ``limit`` re-embeds total and a dead/erroring
        embedder cannot walk the whole refined set inside a single
        background tick. On top of the budget, a run of
        :data:`HEAL_CONSECUTIVE_FAILURE_CUTOFF` consecutive failures
        stops the pass early with a single warning (the embedder is
        presumed down; per-row failure detail lives at debug level).
        The pass PAGES through the whole refined set (not just the head
        window) so a full-corpus migration after an embedder swap —
        where every row is stale regardless of recency — drains
        organically over successive background cycles.
        """
        checked = 0
        healed = 0
        failed = 0
        stale_by_fingerprint = 0
        skipped_quarantined = 0
        current_fingerprint = self._embedder_fingerprint()
        page = max(1, limit)
        offset = 0
        budget_left = limit
        consecutive_failures = 0
        while budget_left > 0:
            rows = self.sqlite.list_by_pipeline_state(
                PipelineState.REFINED, limit=page, offset=offset
            )
            if not rows:
                break  # refined set exhausted
            offset += len(rows)
            admissible = [m for m in rows if not is_quarantined(m)]
            skipped_quarantined += len(rows) - len(admissible)
            metas = self.vectors.get_metadata([m.id for m in admissible]) if admissible else {}
            for mem in admissible:
                checked += 1
                expected = self._embed_content_hash(self._embedding_text(mem))
                meta = metas.get(mem.id)
                if meta is not None:
                    if meta.get("model_fingerprint") != current_fingerprint:
                        # Vintage mismatch (or pre-fingerprint metadata):
                        # the vector was cut by another embedder geometry.
                        stale_by_fingerprint += 1
                    if (
                        meta.get("content_hash") == expected
                        and meta.get("model_fingerprint") == current_fingerprint
                    ):
                        continue
                if budget_left <= 0:
                    break
                try:
                    self.upsert_embedding(mem)
                    healed += 1
                    consecutive_failures = 0
                except Exception as exc:
                    failed += 1
                    consecutive_failures += 1
                    # Per-row detail at debug: the cutoff warning below is
                    # the single operator-facing line for a dying embedder.
                    logger.debug(
                        "heal_stale_embeddings: re-embed failed for %s: %s",
                        mem.id[:8],
                        exc,
                    )
                    if consecutive_failures >= HEAL_CONSECUTIVE_FAILURE_CUTOFF:
                        logger.warning(
                            "heal_stale_embeddings: %d consecutive re-embed "
                            "failures — embedder %r looks dead, stopping the "
                            "pass early (checked=%d healed=%d failed=%d)",
                            consecutive_failures,
                            current_fingerprint,
                            checked,
                            healed,
                            failed,
                        )
                        budget_left = 0
                        break
                # Every ATTEMPT (success or failure) consumes budget — a
                # dead embedder must not walk the whole refined set.
                budget_left -= 1
        if healed or failed:
            logger.info(
                "heal_stale_embeddings: checked=%d healed=%d failed=%d "
                "stale_by_fingerprint=%d skipped_quarantined=%d",
                checked,
                healed,
                failed,
                stale_by_fingerprint,
                skipped_quarantined,
            )
        return {
            "checked": checked,
            "healed": healed,
            "failed": failed,
            "stale_by_fingerprint": stale_by_fingerprint,
            "skipped_quarantined": skipped_quarantined,
        }

    def run_pipeline(
        self,
        *,
        project: str | None = None,
        agent: str | None = None,
        limit: int = 100,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """End-to-end pipeline: cluster → synthesize → quality_gate → publish.

        Single-memory passthrough: memories that don't form a cluster
        (min_cluster_size=2) are promoted individually via a lightweight
        synthesis so they still reach published + vector index. This
        prevents the queue from growing unbounded when most memories are
        unique (P0-1 fix).

        Returns a summary dict for observability / CLI output.
        """
        clusters = self.cluster(project=project, agent=agent, limit=limit, **kwargs)
        synthesized: list[SynthesisResult] = []
        published: list[PublishResult] = []
        failed_qg: list[QualityResult] = []

        # Track which raw memory ids were consumed by clustering
        clustered_ids: set[str] = set()
        for cr in clusters:
            clustered_ids.update(cr.memory_ids)

            syn = self.synthesize(cr.cluster_id)
            if syn is None:
                continue
            synthesized.append(syn)

            qg = self.quality_gate(syn.draft_id)
            if not qg.passed:
                failed_qg.append(qg)
                continue

            pub = self.publish(syn.draft_id)
            published.append(pub)

        # Single-memory passthrough: promote raw memories that were NOT
        # consumed by any cluster. Each becomes its own "synthesis" with
        # quality_score=0.5 (placeholder) so it can pass the gate and reach
        # published + vector index.
        single_promoted = 0
        raw_remaining = self.sqlite.list_all(
            limit=limit,
            status=MemoryStatus.RAW,
            project=project,
            agent=agent,
        )
        for mem in raw_remaining:
            if mem.id in clustered_ids:
                continue
            promoted = self._promote_single_memory(mem.id)
            if promoted is not None and promoted.published:
                published.append(promoted)
                single_promoted += 1

        # Also rescue memories stuck in "processing" status (clustered but
        # never synthesized — e.g. prior runs crashed mid-pipeline). These
        # are promoted directly to published since their cluster may be
        # orphaned and re-synthesizing would create a duplicate draft.
        stuck_processing = self.sqlite.list_all(
            limit=limit,
            status=MemoryStatus.PROCESSING,
            project=project,
            agent=agent,
        )
        stuck_rescued = 0
        for mem in stuck_processing:
            rescued = self._promote_single_memory(mem.id, from_status=MemoryStatus.PROCESSING)
            if rescued is not None and rescued.published:
                published.append(rescued)
                stuck_rescued += 1

        # Record when the pipeline last finished so stats() / dashboards can
        # detect a stuck pipeline (queue growing but last_processed_at stale).
        self.sqlite.set_meta("pipeline_last_run", datetime.now(UTC).isoformat())

        # ── ADR-0019 Phase B2a — the SECOND queue, in parallel to the ───
        # ── legacy flow above (which is NOT modified): async          ───
        # ── refinement of pending rows (already-visible entries).     ───
        # Runs AFTER the publish legs so rows that entered the pipeline
        # in this very cycle (publication enqueues them) are refined in
        # the same pass.
        refine = self.refine_pending(project=project, agent=agent, limit=limit)

        return {
            "clusters": len(clusters),
            "synthesized": len(synthesized),
            "published": len(published),
            "failed_quality_gate": len(failed_qg),
            "single_promoted": single_promoted,
            "stuck_rescued": stuck_rescued,
            "published_ids": [p.memory_id for p in published],
            "refined": refine["refined"],
            "refined_noop": refine["refined_noop"],
            "refine_failed": refine["refine_failed"],
            "quarantined": refine["quarantined"],
        }

    def _promote_single_memory(
        self,
        memory_id: str,
        *,
        from_status: MemoryStatus = MemoryStatus.RAW,
    ) -> PublishResult | None:
        """Promote a single memory directly to published.

        Used when a memory doesn't form a cluster (single-memory passthrough)
        or is stuck in processing. The memory is transitioned to processed
        with placeholder quality scores, then published + vector-indexed.

        This is the graceful fallback for when no real LLM synthesis is
        configured — the memory's own content becomes the "synthesized"
        article (P0-1 fix).
        """
        memory = self.sqlite.get(memory_id)
        if memory is None:
            return None
        if memory.status not in (from_status, MemoryStatus.PROCESSED):
            return None

        # If already processed, just publish
        if memory.status != MemoryStatus.PROCESSED:
            memory.status = MemoryStatus.PROCESSED
            memory.quality_score = 0.5
            memory.confidence = 0.5
            memory.source_coverage = 1
            memory.updated_at = datetime.now(UTC)
            self.sqlite.save(memory)

        return self.publish(memory.id, skip_quality_check=True)

    def rebuild_vector_index(self, *, batch_size: int = 100) -> dict[str, Any]:
        """Rebuild the vector index for all published memories.

        Re-embeds every published memory and upserts into the vector store.
        Used when the embedding pipeline was broken and vectors are missing
        (P0-2 fix). Idempotent: safe to run repeatedly.

        ADR-0019 §5 (B2a): quarantined rows are SKIPPED — they are
        excluded from every issuance path, so an embed would only keep
        a terminally-quarantined id warm in the vector leg. Each upsert
        stamps the ``content_hash`` and ``model_fingerprint`` freshness
        keys the sweeper (:meth:`heal_stale_embeddings`) compares
        against — a full rebuild is therefore also a one-pass vintage
        migration after an embedder swap.
        """
        published = self.sqlite.list_all(
            limit=10000,
            status=MemoryStatus.PUBLISHED,
        )
        eligible = [m for m in published if not is_quarantined(m)]
        skipped_quarantined = len(published) - len(eligible)
        indexed = 0
        failed = 0
        for i in range(0, len(eligible), batch_size):
            batch = eligible[i : i + batch_size]
            for mem in batch:
                try:
                    self.upsert_embedding(mem)
                    indexed += 1
                except Exception as exc:
                    logger.warning("rebuild_vector_index: failed for %s: %s", mem.id[:8], exc)
                    failed += 1

        logger.info(
            "rebuild_vector_index: indexed=%d failed=%d skipped_quarantined=%d total=%d",
            indexed,
            failed,
            skipped_quarantined,
            len(published),
        )
        return {
            "total": len(published),
            "indexed": indexed,
            "failed": failed,
            "skipped_quarantined": skipped_quarantined,
        }

    def backfill_embedding_ids(
        self, *, dry_run: bool = True, batch_size: int = 500
    ) -> dict[str, Any]:
        """Set ``memories.embedding_id`` from the vector store, by id join.

        Search v2 (issue #313): the pre-v2 write path never stamped
        ``memories.embedding_id`` (live DB: NULL for 1644/1644 rows), so
        the column was a diagnostic trap — the vector leg always
        RESOLVED by memory id and worked; the column just never said so.
        This one-off backfill closes the gap for existing rows: every
        memory id that HAS a live vector row gets ``embedding_id = id``
        (the VectorStore keys embeddings by the memory id — see
        ``upsert_embedding``, which stamps new writes from search v2 on).

        Idempotent: re-running matches nothing new; rows whose
        ``embedding_id`` is already set are skipped, rows whose vector
        is gone are left NULL (the column records "live vector exists",
        never a lie). Quarantined rows are still backfilled — the
        column is storage bookkeeping, not an issuance path; the
        quarantine gates live in ``search``/``get`` and are unaffected.

        Args:
            dry_run: report the counts WITHOUT writing (the CLI default —
                an operator runs the real pass explicitly).
            batch_size: UPDATE commit cadence; the join itself is one
                pass over ``vectors.db`` ids.

        Returns:
            ``{"missing": rows to stamp, "stamped": rows actually stamped
            (0 in dry-run), "skipped_already_set": rows already stamped,
            "vectors_total": vector rows in the store}``.
        """
        vector_ids: set[str] = set(self.vectors.all_ids())
        missing: list[str] = []
        skipped = 0
        # One pass over the store; the id-keyed membership check against
        # the vector set is the whole "join".
        for mem in self.sqlite.list_all(limit=1_000_000):
            if mem.embedding_id:
                skipped += 1
                continue
            if mem.id in vector_ids:
                missing.append(mem.id)
        if dry_run:
            logger.info(
                "backfill_embedding_ids: dry_run missing=%d "
                "skipped_already_set=%d vectors_total=%d",
                len(missing),
                skipped,
                len(vector_ids),
            )
            return {
                "missing": len(missing),
                "stamped": 0,
                "skipped_already_set": skipped,
                "vectors_total": len(vector_ids),
            }
        stamped = 0
        for start in range(0, len(missing), batch_size):
            batch = missing[start : start + batch_size]
            for mid in batch:
                if self.sqlite.update_fields(mid, embedding_id=mid):
                    stamped += 1
        logger.info(
            "backfill_embedding_ids: stamped=%d missing=%d skipped_already_set=%d vectors_total=%d",
            stamped,
            len(missing),
            skipped,
            len(vector_ids),
        )
        return {
            "missing": len(missing),
            "stamped": stamped,
            "skipped_already_set": skipped,
            "vectors_total": len(vector_ids),
        }

    # ── Background processor ──────────────────────────────────────────────

    def start_background_processor(self, interval_sec: int = 120) -> None:
        """Start a background thread that periodically runs the pipeline.

        The processor drains the raw/processing queue by running
        cluster → synthesize → quality_gate → publish.  It runs every
        ``interval_sec`` seconds (default 120 = 2 min).

        The default was 300s (5 min) which was too slow to keep up with
        ingest rate, causing the queue to grow unbounded (P0-1 fix).

        Safe to call multiple times — if already running, does nothing.
        """
        if self._processor_thread is not None:
            return
        self._processor_stop = threading.Event()
        self._processor_thread = threading.Thread(
            target=self._processor_loop,
            args=(interval_sec,),
            daemon=True,
            name="mnemos-processor",
        )
        self._processor_thread.start()
        logger.info("Background processor started (interval=%ds)", interval_sec)

    def stop_background_processor(self) -> None:
        """Stop the background processor thread."""
        if self._processor_thread is None or self._processor_stop is None:
            return
        self._processor_stop.set()
        self._processor_thread.join(timeout=10)
        self._processor_thread = None
        self._processor_stop = None
        logger.info("Background processor stopped")

    def _processor_loop(self, interval_sec: int) -> None:
        """Background loop: run pipeline + CCR cleanup periodically.

        Processes in batches of up to 200 memories per cycle to drain
        large backlogs faster (P0-1 fix). The previous default limit=100
        per cycle was insufficient when ingest rate exceeded processing
        rate.

        CCR cleanup (T3): TTL expiry + LRU eviction runs on its own
        interval (``ccr_cleanup_interval_sec``, default 1200s = 20 min)
        — not every cycle — to avoid scanning the cache table every
        ``interval_sec``. Cleanup is guarded by ``ccr.enabled`` and
        wrapped in a try/except so a cleanup failure never crashes the
        processor loop.
        """
        if self._processor_stop is None:
            return
        while not self._processor_stop.is_set():
            try:
                stats = self.stats()
                processor_stats = stats.get("processor", {})
                queue_depth = processor_stats.get("queue_depth", 0)
                if queue_depth > 0:
                    logger.info(
                        "Processor: queue_depth=%d (legacy=%d refine=%d), "
                        "running pipeline (batch=200)",
                        queue_depth,
                        processor_stats.get("legacy_queue_depth", 0),
                        processor_stats.get("refine_queue_depth", 0),
                    )
                    _cycle_t0 = time.monotonic()
                    try:
                        result = self.run_pipeline(limit=200)
                    except Exception as exc:
                        # Vitals boundary #3 (A2) — the failure IS telemetry.
                        self.record_verb_vitals(
                            surface="background",
                            verb="pipeline.cycle",
                            status="error",
                            latency_ms=(time.monotonic() - _cycle_t0) * 1000,
                            meta={"error_type": type(exc).__name__},
                        )
                        raise
                    _cycle_ms = (time.monotonic() - _cycle_t0) * 1000
                    logger.info(
                        "Processor: cycle done — published=%d single=%d stuck=%d "
                        "refined=%d noop=%d quarantined=%d",
                        result.get("published", 0),
                        result.get("single_promoted", 0),
                        result.get("stuck_rescued", 0),
                        result.get("refined", 0),
                        result.get("refined_noop", 0),
                        result.get("quarantined", 0),
                    )
                    # Vitals boundary #3 (A2): the pipeline cycle as a verb.
                    self.record_verb_vitals(
                        surface="background",
                        verb="pipeline.cycle",
                        status="ok",
                        latency_ms=_cycle_ms,
                        meta={
                            "counters": {
                                "published": int(result.get("published", 0)),
                                "refined": int(result.get("refined", 0)),
                                "quarantined": int(result.get("quarantined", 0)),
                            }
                        },
                    )
                # Issue #170 (ADR-0019 Phase C) — idempotent lease-reclaim
                # pass: processing rows whose lease expired (a worker died
                # between the claim and its outcome write) re-enter the
                # pending intake.
                _reclaim_t0 = time.monotonic()
                try:
                    self.reclaim_stale_refinements()
                except Exception as exc:
                    # Vitals boundary #5a (A2) — error leg.
                    self.record_verb_vitals(
                        surface="background",
                        verb="refine.reclaim",
                        status="error",
                        latency_ms=(time.monotonic() - _reclaim_t0) * 1000,
                        meta={"error_type": type(exc).__name__},
                    )
                    raise
                # Vitals boundary #5a (A2).
                self.record_verb_vitals(
                    surface="background",
                    verb="refine.reclaim",
                    status="ok",
                    latency_ms=(time.monotonic() - _reclaim_t0) * 1000,
                )
                # ADR-0019 §Swap — idempotent heal pass: refined rows whose
                # embed is stale/missing (a failed post-swap upsert) get
                # re-embedded; quarantined rows are skipped absolutely.
                _heal_t0 = time.monotonic()
                try:
                    self.heal_stale_embeddings()
                except Exception as exc:
                    # Vitals boundary #5b (A2) — error leg.
                    self.record_verb_vitals(
                        surface="background",
                        verb="embed.heal",
                        status="error",
                        latency_ms=(time.monotonic() - _heal_t0) * 1000,
                        meta={"error_type": type(exc).__name__},
                    )
                    raise
                # Vitals boundary #5b (A2).
                self.record_verb_vitals(
                    surface="background",
                    verb="embed.heal",
                    status="ok",
                    latency_ms=(time.monotonic() - _heal_t0) * 1000,
                )
                # CCR cleanup tick — runs on its own interval, not every cycle.
                self._maybe_run_ccr_cleanup()
                # Vitals rollup tick (A2) — hourly, BEFORE retention.
                self._maybe_run_vitals_rollup()
                # Vitals retention tick (C4) — nightly cadence, own interval.
                self._maybe_run_vitals_retention()
            except Exception:
                logger.exception("Background processor error")
            self._processor_stop.wait(timeout=interval_sec)

    def _maybe_run_ccr_cleanup(self) -> None:
        """Run CCR TTL/LRU cleanup if enough wall-clock time has elapsed.

        Guarded by ``settings.ccr.enabled``. Exceptions are caught and
        logged so the processor loop never crashes on a cleanup failure.
        """
        if not self.settings.ccr.enabled:
            return
        interval = self.settings.ccr.ccr_cleanup_interval_sec
        now = time.monotonic()
        if self._ccr_cleanup_last_ts and (now - self._ccr_cleanup_last_ts) < interval:
            return
        self._ccr_cleanup_last_ts = now
        _ccr_t0 = time.monotonic()
        try:
            result = self.ccr_cleanup()
            # Vitals boundary #4 (A2).
            self.record_verb_vitals(
                surface="background",
                verb="ccr.cleanup",
                status="ok",
                latency_ms=(time.monotonic() - _ccr_t0) * 1000,
                meta={
                    "counters": {
                        "ttl_deleted": int(result.get("ttl_deleted", 0)),
                        "lru_evicted": int(result.get("lru_evicted", 0)),
                    }
                },
            )
            if result["ttl_deleted"] or result["lru_evicted"]:
                logger.info(
                    "CCR cleanup: ttl_deleted=%d lru_evicted=%d",
                    result["ttl_deleted"],
                    result["lru_evicted"],
                )
        except Exception:
            # Vitals boundary #4 (A2) — the failure IS the telemetry.
            self.record_verb_vitals(
                surface="background",
                verb="ccr.cleanup",
                status="error",
                latency_ms=(time.monotonic() - _ccr_t0) * 1000,
                meta={"error_type": "CCRCleanupError"},
            )
            logger.exception("CCR cleanup failed (non-fatal)")

    # ── Vitals (ADR-0026 phase A) ─────────────────────────────────────────

    def record_assemble_vitals(self, result: dict[str, Any]) -> None:
        """Record one assemble call into the vitals sidecar (non-fatal).

        Called by the two collection boundaries AFTER the result exists
        (the MCP ``mnemos_assemble_context`` handler and the
        ``pre_llm_call`` hook) — never from the assemble pipeline
        itself: S2 measures that verb directly and the wrapper must stay
        out of its path. All failure modes are swallowed by the sink.
        """
        store = self._vitals_store
        if store is None:
            return
        if "stats" not in result:
            # mode="async" returns a handle envelope, not an assembly —
            # recording it would poison the corpus with empty rows. The
            # real assembly lands (and is recorded) when the handle is
            # redeemed and returns the full result.
            return
        try:
            store.record_assemble(result)
        except Exception:  # the plane must never surface
            logger.warning("vitals: boundary record failed (non-fatal)", exc_info=True)

    def record_verb_vitals(
        self,
        *,
        surface: str,
        verb: str,
        status: str,
        latency_ms: float,
        status_code: int | None = None,
        project: str | None = None,
        agent: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Record one verb call into the vitals ledger (non-fatal).

        Phase A2: one row per call on any metered surface (MCP dispatch,
        REST middleware, background jobs, CLI). Meta passes the C5
        allowlist gate (``validate_meta``) — a refused meta logs a
        warning and the write is dropped loudly, never fatally.
        """
        store = self._vitals_store
        if store is None:
            return
        try:
            store.record_verb(
                surface=surface,
                verb=verb,
                status=status,
                latency_ms=latency_ms,
                status_code=status_code,
                project=project,
                agent=agent,
                meta=meta,
            )
        except Exception:
            logger.warning("vitals: verb record failed (non-fatal)", exc_info=True)

    def vitals_exposition(self) -> str:
        """Prometheus text for the vitals plane ('' when disabled)."""
        store = self._vitals_store
        if store is None:
            return ""
        try:
            from vesmaro.metrics.exposer import render_exposition

            return render_exposition(store)
        except Exception:
            logger.warning("vitals: exposition render failed (non-fatal)", exc_info=True)
            return ""

    def _maybe_run_vitals_rollup(self) -> None:
        """Hourly rollup tick (A2): aggregate the previous complete hour.

        Runs BEFORE retention in the loop so raw verb rows are never
        deleted unrolled. Same cadence pattern as the CCR cleanup tick.
        """
        store = self._vitals_store
        if store is None:
            return
        now = time.monotonic()
        if self._vitals_rollup_last_ts and (now - self._vitals_rollup_last_ts) < 3600:
            return
        self._vitals_rollup_last_ts = now
        try:
            # catch-up: roll every hour since the last rolled one (capped
            # at a week — older gaps mean the plane was down longer than
            # the raw TTL margin; the retention report surfaces that)
            prev_hour = int(time.time() // 3600) - 1
            first = max(store.last_rolled_hour() + 1, prev_hour - 168)
            rows = 0
            for hour in range(first, prev_hour + 1):
                rows += store.rollup_hourly(hour=hour)
            if rows:
                logger.info("vitals rollup: %d rows", rows)
        except Exception:
            logger.error("vitals rollup failed (A2 gate alert)", exc_info=True)

    def _maybe_run_vitals_retention(self) -> None:
        """Nightly sidecar retention (C4): DELETE per-TTL + VACUUM.

        Mirrors the CCR cleanup cadence pattern. Unlike the collection
        path, retention failure is FAIL-LOUD (error log = the alert the
        nightly-job contract demands), while the processor loop itself
        keeps running.
        """
        store = self._vitals_store
        if store is None:
            return
        interval = self.settings.vitals.retention_interval_sec
        now = time.monotonic()
        if self._vitals_retention_last_ts and (now - self._vitals_retention_last_ts) < interval:
            return
        self._vitals_retention_last_ts = now
        try:
            deleted = store.run_retention()
            if any(deleted.values()):
                logger.info("vitals retention: %s", deleted)
        except Exception:
            logger.error("vitals retention failed (C4 alert)", exc_info=True)

    @property
    def processor_running(self) -> bool:
        """Whether the background processor thread is active."""
        return self._processor_thread is not None and self._processor_thread.is_alive()

    # ── CacheAligner (P1-5) ────────────────────────────────────────────────

    def retrieval_iso(self, session: str) -> str:
        """Session-scoped first-assembly ISO stamp for ``retrieved=<iso>``.

        mnemos #282: the provenance timestamp must be STABLE within one
        session's lifetime (byte-stable block prefixes for harness-side
        KV caching) and DIFFERENT across sessions. The first assembly of
        a session stamps ``now()`` and caches it; every later assembly of
        the same session reuses the cached stamp. Past
        ``RETRIEVAL_ISO_REGISTRY_CAP`` distinct sessions entries are
        evicted FIFO by first-assembly order — an evicted session
        (including a still-active one displaced by a flood of new
        sessions) simply re-stamps on its next assembly, same failure
        class as a process restart. The registry is in-memory and
        PER-PROCESS (mirrors ``_assemble_async``): multi-worker
        deployments stamp independently per worker.
        """
        with self._retrieval_iso_lock:
            iso = self._retrieval_iso.get(session)
            if iso is None:
                iso = datetime.now(UTC).isoformat()
                self._retrieval_iso[session] = iso
                while len(self._retrieval_iso) > RETRIEVAL_ISO_REGISTRY_CAP:
                    oldest = next(iter(self._retrieval_iso))
                    del self._retrieval_iso[oldest]
            return iso

    def align_prefix(self, text: str, *, profile: str | None = None) -> dict[str, Any]:
        """Relocate dynamic content to the end of ``text`` for prefix stability.

        Wraps :func:`vesmaro.cache_aligner.align`. When CacheAligner is
        disabled in config, the text is returned unchanged with an empty
        extracted list.

        Per-kind toggles on ``CacheAlignerConfig`` (``extract_timestamps``,
        ``extract_uuids``, ``extract_session_ids``, ``extract_dates``,
        ``extract_tokens``) are honoured: a kind whose toggle is ``False``
        is added to the skip set and stays in-place. These toggles merge
        (union) with the profile's own skip set — disabling a kind in
        config widens what a profile already skips.

        Args:
            text: System-prompt-like text to stabilize.
            profile: Optional filter profile (``"code"``, ``"docs"``)
                that toggles which dynamic kinds are extracted.

        Returns:
            ``{"aligned_text","extracted","prefix_stabilized","moved_chars"}``.
        """
        from vesmaro.cache_aligner import align

        if not self.settings.cache_aligner.enabled:
            return {
                "aligned_text": text,
                "extracted": [],
                "prefix_stabilized": False,
                "moved_chars": 0,
            }
        cfg = self.settings.cache_aligner
        skip_kinds: set[str] = set()
        if not cfg.extract_timestamps:
            skip_kinds.add("timestamp")
        if not cfg.extract_uuids:
            skip_kinds.add("uuid")
        if not cfg.extract_session_ids:
            skip_kinds.add("session_id")
        if not cfg.extract_dates:
            skip_kinds.add("date")
        if not cfg.extract_tokens:
            skip_kinds.add("token")
        return align(text, profile=profile, skip_kinds=skip_kinds or None)

    # ── ADR-0017 D1: assemble_context provider contract (#125) ─────────────

    def assemble_context(
        self,
        *,
        session: str,
        project: str,
        file: str | None = None,
        budget: int = 2048,
        mode: str = "sync",
        expand_ccr: bool = False,
        async_handle: str | None = None,
        agent: str | None = None,
        query: str | None = None,
        task: str | None = None,
        lens: str | None = None,
    ) -> dict[str, Any]:
        """Assemble the model-facing context block (ADR-0017 D1 contract).

        Thin delegation to :func:`vesmaro.assemble.assemble_context` — the
        fixed pipeline (recall → CCR → filter → scan → align → budget)
        lives in that module; this method keeps the one-core-over-three-
        surfaces pattern (MCP / REST / future SDK all call the manager).
        ``agent`` (A2 review F2) pairs with ``session`` as the issuer
        context for the strict-mode CCR expansion gate. ``query`` (W3)
        overrides the derived recall term — the ``pre_llm_call`` hook's
        ``context_hint``. ``task`` / ``lens`` (ADR-0027 Phase 0, epic
        #308) are the multi-context parameters: the bare task slug
        (intersection-doctrine recall narrowing) and the code-defined
        lens preset name (query-conditioned, narrowing-only projection)
        — both optional, both tail-only, both absent from the result
        dict unless given. Raises ``ValueError`` on invalid ``session`` /
        ``project`` / ``mode`` / ``budget`` / ``query`` / ``task`` /
        ``lens`` or an unknown ``async_handle``.
        """
        from vesmaro.assemble import assemble_context as _assemble

        return _assemble(
            self,
            session=session,
            project=project,
            file=file,
            budget=budget,
            mode=mode,
            expand_ccr=expand_ccr,
            async_handle=async_handle,
            agent=agent,
            query=query,
            task=task,
            lens=lens,
        )

    # ── ADR-0018: on_context_rewrite lifecycle event (#125, Wave 2) ────────

    def context_rewrite(
        self,
        *,
        content: str,
        project: str,
        agent: str,
        session: str | None = None,
        supersedes: str | None = None,
        diff: str | None = None,
        include_marker: bool = False,
    ) -> dict[str, Any]:
        """Handle one ``on_context_rewrite`` event (idempotent, version-less).

        Thin delegation to :func:`vesmaro.context_rewrite.context_rewrite` —
        the event logic lives in that module; this method keeps the
        one-core-over-three-surfaces pattern (MCP / REST / future SDK all
        call the manager). The original enters LTM through the NORMAL
        knowledge-pipeline ``add`` path; rehydrate is the existing
        scanned/gated issuance channels. Raises ``ValueError`` on invalid
        input (incl. size caps), tag-contract violations, or a
        ``supersedes`` target not found in the caller's project; raises
        :class:`vesmaro.context_rewrite.ContextRewriteRateLimitError` when the
        per-(project, session) stored-event quota is exhausted (W2
        review F1 — REST maps it to 429).
        """
        from vesmaro.context_rewrite import context_rewrite as _event

        return _event(
            self,
            content=content,
            project=project,
            agent=agent,
            session=session,
            supersedes=supersedes,
            diff=diff,
            include_marker=include_marker,
        )

    # ── CCR (P1-4) ─────────────────────────────────────────────────────────

    def compress_content(
        self,
        text: str,
        *,
        profile: str | None = None,
        project: str = "",
        agent: str | None = None,
        session: str | None = None,
    ) -> dict[str, Any]:
        """Compress ``text`` via CCR and cache the original in SQLite.

        A2 (ArchCom 2026-08-27) — issuer ledger: ``agent``/``session``
        record the caller identity on the cache row so strict marker
        validation (see :meth:`retrieve_content`) can later prove the
        marker was minted in the redeemer's own context. Callers without
        identity context pass ``None`` (stored NULL → the row's markers
        are unverifiable and strict mode refuses them).

        Returns the CCR result dict (see ``vesmaro.ccr.compress``).
        """
        from vesmaro.ccr import compress

        if not self.settings.ccr.enabled:
            return {
                "compressed_text": text,
                "hash": "",
                "original_size": len(text),
                "compressed_size": len(text),
                "reduction_pct": 0.0,
                "marker": "",
                "cached": False,
                "profile": "disabled",
                # C7: envelope key parity — nothing was sampled, nothing
                # was dropped (out-of-band accounting, never in-band).
                "dropped_items": 0,
            }
        return compress(
            text,
            store=self.sqlite,
            config=self.settings.ccr,
            profile=profile,
            project=project,
            issuer_agent=agent,
            issuer_session=session,
        )

    def validate_marker(
        self,
        h: str,
        *,
        project: str | None,
        original_chars: int | None,
        trusted_issuers: AbstractSet[tuple[str, str | None]],
    ) -> dict[str, Any]:
        """A2 (ArchCom 2026-08-27) — validate a CCR marker before issuance.

        Strong-form gate for the W3 automation rehydrate channel
        (committee decision ``archcom-2026-08-27-deferrals-triage``):
        existence-only validation does NOT catch same-project seeding,
        so all three checks run:

        * **existence** — the row must exist under ``(project, hash)``
          (project-scoped after A1). Strict validation REQUIRES a
          project scope: an unscoped lookup would redeem a marker
          against the first-stored copy of any project.
        * **integrity** — the marker's ``original_chars`` (the N parsed
          from ``[compressed: <hash> | N→M chars | …]``) must equal the
          character length of the stored original. ``None`` fails
          (fail-closed: an unverifiable dimension is a failed
          dimension).
        * **provenance** — the row's issuer ledger
          (``issuer_agent``/``issuer_session``, recorded at store time)
          must match the caller's trusted issuer context: the stored
          ``(agent, session)`` pair must be a member of
          ``trusted_issuers``. The minimal sound spec for W3 automation
          is exactly one pair — its OWN ``(agent, session)`` — so a
          hook redeems only markers minted in its own context; an
          explicit allowlist is the same predicate with more pairs. A
          spec session of ``None`` matches only NULL issuer sessions
          (component-wise equality, never wildcards). Rows stored
          without identity (legacy migration or identity-less callers)
          are UNVERIFIABLE and fail with the distinct reason
          ``unverifiable legacy marker``; an empty spec fails with
          ``no trusted issuer context``.

        Residual (ADR-0018 residual register, accepted, A2 review round
        wording): with strict mode enabled, marker redemption is
        ADVERSARY-RESISTANT for issuer-stamped rows (post-A2 stores carry
        the ledger) — refusal reasons are FIXED non-oracle strings (A2
        review F1: a reason echoing the stored length or issuer pair is a
        two-call oracle that defeats provenance). Legacy NULL-issuer rows
        are unverifiable by construction: full-shape validation refuses
        them, hash-only retrieval under strict mode stays ALLOWED with a
        WARNING (refusing would brick all pre-A2 caches for zero
        marginal adversary resistance — see retrieve_content F2). The
        same-project seeding residual is unchanged: a trusted harness
        with compress access can still seed content inside its own
        project and redeem from the same identity; single-operator
        threat model, revisit on the first multi-principal trigger.

        Args:
            h: SHA-256 hash from the marker (validated by existence).
            project: Project scope; ``None``/empty fails existence
                (strict validation requires the scope).
            original_chars: N from the marker; ``None`` fails integrity.
            trusted_issuers: Set of ``(agent, session | None)`` pairs
                allowed to have minted the marker. Components are
                stripped and empty sessions normalised to ``None``
                (mirroring the ``ccr_store`` issuer normalisation — A2
                review F4); agents must be non-empty strings
                (``ValueError`` otherwise — the caller builds this set
                from its own identity).

        Returns:
            ``{"valid": bool, "reason": str | None, "check": str | None}``
            where ``check`` names the failed dimension (``existence`` /
            ``integrity`` / ``provenance``) for the refusal reason.
            Reasons are FIXED strings carrying no stored values (F1).
            The read is unbumped (``bump=False``): a failed validation
            must not LRU-pin the entry (P1-b review F4 semantics).
        """
        # F4 — normalise spec components exactly like ccr_store stores
        # them, so a padded spec matches the stripped ledger row.
        normalized: set[tuple[str, str | None]] = set()
        for agent_i, session_i in trusted_issuers:
            if not isinstance(agent_i, str) or not agent_i.strip():
                raise ValueError(
                    f"trusted_issuers agents must be non-empty slugs (got {agent_i!r})"
                )
            if session_i is not None and not isinstance(session_i, str):
                raise ValueError(
                    f"trusted_issuers sessions must be strings or None (got {session_i!r})"
                )
            session_n = (session_i.strip() or None) if session_i else None
            normalized.add((agent_i.strip(), session_n))

        def _verdict(valid: bool, check: str | None, reason: str | None) -> dict[str, Any]:
            return {"valid": valid, "reason": reason, "check": check}

        # (a) existence — project-scoped (strict mode requires a scope).
        if not project:
            return _verdict(False, "existence", "project scope required for marker validation")
        entry = self.sqlite.ccr_get(h, project=project, bump=False)
        if entry is None:
            return _verdict(False, "existence", f"hash not in cache under project {project!r}")

        # (b) integrity — marker N vs stored character length. F1: the
        # reason is a FIXED string — echoing marker/stored lengths turns
        # the refusal into a two-call oracle (read the true N, re-call).
        if original_chars is None:
            return _verdict(False, "integrity", "original_chars not provided by the caller")
        if original_chars != len(entry["original"]):
            return _verdict(False, "integrity", "original_chars mismatch")

        # (c) provenance — issuer ledger vs the trusted context. F1: the
        # reason never echoes the stored issuer pair (same oracle class).
        if not normalized:
            return _verdict(False, "provenance", "no trusted issuer context (agent required)")
        issuer_agent = entry.get("issuer_agent")
        if not issuer_agent:
            return _verdict(False, "provenance", "unverifiable legacy marker")
        issuer_pair = (issuer_agent, entry.get("issuer_session"))
        if issuer_pair not in normalized:
            return _verdict(False, "provenance", "issuer mismatch")
        return _verdict(True, None, None)

    def scan_issuance(self, text: str, *, context: str) -> IssuanceScan:
        """Scan one issuance-boundary string and redact/refuse (P1-b M1).

        Single helper for every content-echoing path — MCP
        ``mnemos_search`` / ``mnemos_agent_recall`` / ``mnemos_recall_context``
        and REST ``/search`` / ``/recall/agent`` — so no channel can drift
        from the P0 ``retrieve_content`` semantics: matched spans become
        ``<REDACTED:<pattern>>`` in the returned copy (zero-loss storage —
        the caller's stored model is never mutated), or the whole string
        is refused (no content) when ``ccr.retrieve_refuse_on_secret`` is
        on. A scanner exception is NEVER allowed to degrade into issuing
        unscanned content (fail-closed): it maps to ``refused=True`` with
        ``reason="scanner error"`` (P1-b m5). The ``context`` label names
        the calling channel in log lines (never the content).

        Cost control: scan runs on exactly the string being issued (per
        item, once) — titles/tags/metadata are the caller's business.
        """
        from vesmaro.secrets_detector import (
            detect_secrets,
            findings_by_pattern,
            redact_content,
        )

        try:
            findings = detect_secrets(text)
            if not findings:
                return IssuanceScan(
                    text=text,
                    refused=False,
                    reason=None,
                    redactions=0,
                    redacted_patterns={},
                )
            pattern_counts = findings_by_pattern(findings)
            if self.settings.ccr.retrieve_refuse_on_secret:
                logger.warning(
                    "Issuance refused (%s): redactions=%d patterns=%s — raw values not logged",
                    context,
                    len(findings),
                    pattern_counts,
                )
                return IssuanceScan(
                    text="",
                    refused=True,
                    reason="secret detected",
                    redactions=len(findings),
                    redacted_patterns=pattern_counts,
                )
            redacted = redact_content(text, findings)
            logger.info(
                "Issuance redacted (%s): redactions=%d patterns=%s",
                context,
                len(findings),
                pattern_counts,
            )
            return IssuanceScan(
                text=redacted,
                refused=False,
                reason=None,
                redactions=len(findings),
                redacted_patterns=pattern_counts,
            )
        except Exception as exc:
            # Fail-closed (P1-b m5): any scanner failure refuses the
            # issuance rather than echoing unscanned content. Broad catch
            # is deliberate at this security boundary; the error is
            # logged (context label + exception, never the content).
            logger.error("Issuance scanner error (%s): %s", context, exc)
            return IssuanceScan(
                text="",
                refused=True,
                reason="scanner error",
                redactions=0,
                redacted_patterns={},
            )

    def scan_issuance_item(
        self,
        text: str | None,
        *,
        title: str | None = None,
        context: str,
    ) -> IssuanceItemScan:
        """Scan every string ONE result item echoes (P1-b review F1).

        Composite over :meth:`scan_issuance` for items that echo both a
        content field and a title (``auto_title()`` derives from the
        first line of raw content OR echoes an explicitly-set title —
        either can carry a secret, so scanning only the content leaks
        the title verbatim in the same response). ``text``/``title`` are
        ``None`` when the item does not echo that field (e.g. title-only
        ``mnemos_list_recent``); at least one must be given. Refuse mode
        refuses the item when EITHER field trips; ``redactions`` /
        ``redacted_patterns`` are merged across both fields. The
        ``context`` label should carry the item id (F3 forensics); the
        scanned field is appended here (``<context>:content`` /
        ``<context>:title``).
        """
        content_scan = (
            self.scan_issuance(text, context=f"{context}:content") if text is not None else None
        )
        title_scan = (
            self.scan_issuance(title, context=f"{context}:title") if title is not None else None
        )
        scans = [s for s in (content_scan, title_scan) if s is not None]
        refused = any(s.refused for s in scans)
        reason = next((s.reason for s in scans if s.refused), None)
        redactions = sum(s.redactions for s in scans)
        patterns: dict[str, int] = {}
        for s in scans:
            for name, count in s.redacted_patterns.items():
                patterns[name] = patterns.get(name, 0) + count
        return IssuanceItemScan(
            content=content_scan.text if content_scan is not None and not refused else "",
            title=title_scan.text if title_scan is not None and not refused else "",
            refused=refused,
            reason=reason,
            redactions=redactions,
            redacted_patterns=patterns,
        )

    def retrieve_content(
        self,
        h: str,
        *,
        query: str | None = None,
        snippet_count: int | None = None,
        project: str | None = None,
        validate_marker: bool | None = None,
        original_chars: int | None = None,
        agent: str | None = None,
        session: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve a CCR-cached original (or FTS5 snippets if ``query``).

        A2 (ArchCom 2026-08-27) — strict marker validation. The request
        is MARKER-SHAPED when it carries any of ``original_chars`` /
        ``agent`` / ``session`` (the metadata a harness parses out of a
        ``[compressed: <hash> | N→M chars | …]`` marker plus its own
        identity). When strict mode is on (per-call ``validate_marker``
        override, else the ``ccr.validate_markers`` knob) a marker-shaped
        request must first pass :meth:`validate_marker` — existence
        (project-scoped), ``original_chars`` integrity, and provenance
        against the caller's own ``(agent, session)`` issuer context —
        and any failed check returns the refused shape with
        ``reason="marker validation failed: <check>: <detail>"`` and NO
        content (fail-closed; reasons are FIXED non-oracle strings — A2
        review F1). Review F2 closes the strip-the-args bypass: in
        strict mode a HASH-ONLY retrieve of an ISSUER-STAMPED row is
        refused with ``reason="marker validation required"`` (no
        content); legacy NULL-issuer rows stay redeemable hash-only
        with a WARNING (unverifiable by construction — refusing would
        brick pre-A2 caches; the register wording lives in
        :meth:`validate_marker`). An explicit ``validate_marker=False``
        disables both gates (operator escape hatch). A refused
        validation never bumps the retrieval counter (F4 semantics
        preserved). Review N1: successful responses NEVER echo the
        issuer ledger — ``issuer_agent``/``issuer_session`` are internal
        gate data stripped before any success path returns.

        ADR-0018 P0 — issuance secret scan. Every retrieval is scanned
        with ``detect_secrets`` (patterns evolve and stored records age,
        so a store-time verdict alone would go stale): matched spans in
        the RETURNED payload — full original or FTS5 snippets — are
        replaced with ``<REDACTED:<pattern>>``. The stored original is
        never mutated (zero-loss storage). The response carries
        ``redactions`` (number of redacted spans; ``0`` when clean) and,
        when non-zero, ``redacted_patterns`` (log-safe per-pattern
        counts — raw matched values never leave storage). When
        ``ccr.retrieve_refuse_on_secret`` is enabled, a detection
        returns ``refused=True`` with no content instead of a redacted
        copy. ``found=False`` responses are returned unchanged.

        ADR-0018 P1-a — project scoping: ``project`` scopes the cache
        lookup to that project's entries; a hash cached under another
        project returns ``found=False`` (cross-session marker redemption
        denied, fail-closed). The manager holds no ambient project
        context, so the default is ``None`` = unscoped lookup (legacy
        behavior preserved for callers without project context); the
        explicit parameter is the override. The scan-at-store verdict on
        the row is observability only and never fast-paths this scan.

        ADR-0018 P1-b — issuance hardening:

        * CWE-668 ergonomics (Security findings 1+4): retrieving a
          project-scoped entry with ``project=None`` logs a WARNING; with
          ``ccr.require_project_match`` (default False) the issuance is
          denied instead (``refused=True``, ``reason`` names the scope
          requirement).
        * m2 — FTS5 snippets are scanned on a marker-stripped copy
          (highlight markers split multi-token secrets); when the
          snippet cannot be mapped onto the original, the WHOLE
          snippet is withheld (``<REDACTED:snippet>``) because
          stripped-copy offsets do not map back to the marked snippet.
        * B5 tier-1 (ArchCom 2026-08-27) — verdict-gated snippet refusal:
          entries whose scan-at-store verdict is ``'hit'`` refuse the
          snippet mode entirely (``reason="snippet mode unavailable for
          entries with detected secrets"``, no snippets emitted, no
          retrieval-counter bump) — snippet windows cannot be proven
          secret-free without the tier-2 offset mapping. NULL /
          'unknown' / 'clean' rows snippet as before; the full-original
          retrieve of a hit row stays available and is redacted
          span-wise by the P0 scan below.
        * B5 tier-2 (W3) — offset-mapped snippet scan for NULL /
          'unknown' / 'clean' rows: the snippet's ellipsis-split
          fragments are localized in the cached original (W1 CCR-stage
          exact-substring localization; each fragment must be UNIQUE),
          the original is scanned over the localized window ±
          ``SNIPPET_SCAN_MARGIN_CHARS``, and every finding INTERSECTING
          the window is redacted span-wise in the emitted snippet —
          closing the window-truncation fragment (a secret cut by the
          32-token FTS5 window edge evades snippet-text detection but
          not the original-window scan). A non-localizable snippet
          falls back to the tier-1 m2 withholding above (fail-closed).
        * m5 — a scanner exception maps to the refused shape with
          ``reason="scanner error"`` (fail-closed, observable) instead
          of propagating as a 500 / MCP error.
        * review F4 — the retrieval counter is bumped only when content
          is actually ISSUED (``ccr_touch`` after the decision; the
          entry is read via ``bump=False``): refused/denied issuances no
          longer inflate ``retrieval_count`` or LRU-pin the entry.
        """
        from vesmaro.ccr import retrieve
        from vesmaro.secrets_detector import findings_by_pattern

        # ── A2 (ArchCom 2026-08-27): strict marker validation gate ──────
        # Fail-closed BEFORE any content is read for issuance: a failed
        # check returns the refused shape with no content and never
        # bumps the retrieval counter (the validation read itself is
        # unbumped — validate_marker uses bump=False).
        strict = self.settings.ccr.validate_markers if validate_marker is None else validate_marker
        marker_shaped = original_chars is not None or agent is not None or session is not None
        if strict and marker_shaped:
            trusted: set[tuple[str, str | None]] = set()
            if agent and agent.strip():
                trusted.add((agent.strip(), (session.strip() or None) if session else None))
            verdict = self.validate_marker(
                h,
                project=project,
                original_chars=original_chars,
                trusted_issuers=trusted,
            )
            if not verdict["valid"]:
                refusal_reason = (
                    f"marker validation failed: {verdict['check']}: {verdict['reason']}"
                )
                logger.warning(
                    "CCR issuance refused (marker validation): hash=%s check=%s "
                    "project=%s agent=%s — no content issued",
                    h,
                    verdict["check"],
                    project,
                    agent,
                )
                return {
                    "hash": h,
                    "found": verdict["check"] != "existence",
                    "refused": True,
                    "reason": refusal_reason,
                    "redactions": 0,
                    "redacted_patterns": {},
                }

        result = retrieve(
            h,
            store=self.sqlite,
            config=self.settings.ccr,
            query=query,
            snippet_count=snippet_count,
            project=project,
            bump=False,
        )
        if not result.get("found"):
            return result

        # ── ADR-0019 §5 (B2b): quarantine retraction on the CCR ────────
        # channel. The cache is content-addressed (no memory-id link),
        # so the guard matches the cached payload against the QUARANTINED
        # rows' stored/served content (the lane-(b) population is small
        # and terminal by construction; a corpus-scale link would need
        # an index — deferred with the citation-graph cascade to
        # ADR-0017 D2). The full original is replaced by the retraction
        # render; snippet mode refuses outright (no fragment of a
        # quarantined payload may leak through a window). Neither path
        # bumps the retrieval counter (F4 semantics — no content issued).
        original_payload = result.get("original")
        if isinstance(original_payload, str):
            retraction = self._quarantine_retraction_for(original_payload)
            if retraction is not None:
                logger.warning(
                    "retraction: hash=%s channel=ccr:retrieve-original — "
                    "cached original withheld (quarantined source)",
                    h[:8],
                )
                if query is not None:
                    return {
                        "hash": h,
                        "found": True,
                        "refused": True,
                        "reason": "quarantined source",
                        "redactions": 0,
                        "redacted_patterns": {},
                    }
                return {
                    "hash": h,
                    "found": True,
                    "original": retraction,
                    "retracted": True,
                    "redactions": 0,
                }

        # ── A2 review F2: strict-mode hash-only closure ────────────────
        # Strict deployments are automation contexts by design: an
        # ISSUER-STAMPED row must be redeemed WITH marker metadata so the
        # validation gate above actually runs — a hash-only retrieve
        # would strip the optional args and bypass it. Legacy NULL-issuer
        # rows stay redeemable (unverifiable by construction; refusing
        # would brick all pre-A2 caches for zero marginal adversary
        # resistance) with a WARNING for audit visibility. The explicit
        # per-call validate_marker=False override disables this gate
        # together with the rest of strict mode (operator escape hatch).
        if strict and not marker_shaped:
            if result.get("issuer_agent"):
                logger.warning(
                    "CCR issuance refused (marker validation required): hash=%s "
                    "project=%s — issuer-stamped entry redeemed without marker "
                    "metadata; no content issued",
                    h,
                    project,
                )
                return {
                    "hash": h,
                    "found": True,
                    "refused": True,
                    "reason": "marker validation required",
                    "redactions": 0,
                    "redacted_patterns": {},
                }
            logger.warning(
                "Unvalidated hash-only CCR retrieval of an unverifiable legacy "
                "entry under strict marker validation (A2 review F2): hash=%s "
                "project=%s — allowed, issuer ledger is NULL by construction",
                h,
                project,
            )

        # ── A2 review N1: no issuer echo ───────────────────────────────
        # The issuer pair is an internal ledger datum (the F2 gate just
        # consumed it); echoing it in every successful response would
        # gratuitously disclose session-capability handles on the MCP/REST
        # surfaces. Strip both keys before any success path returns.
        result.pop("issuer_agent", None)
        result.pop("issuer_session", None)

        # A1 (ArchCom 2026-08-27): the entry's own project — under the
        # composite PK the same hash may live in several projects, and
        # the bump must hit exactly the row that was issued. Computed
        # BEFORE _mark_issued is first called.
        entry_project = str(result.get("project") or "")

        def _mark_issued() -> None:
            """Bump the counter post-decision (F4) and reflect it in result."""
            self.sqlite.ccr_touch(h, project=entry_project)
            result["retrieval_count"] = int(result["retrieval_count"]) + 1

        # ── CWE-668 ergonomics: unscoped retrieval of a scoped row ────
        if not project and entry_project:
            if self.settings.ccr.require_project_match:
                logger.warning(
                    "CCR issuance denied (project scope required): hash=%s entry_project=%s",
                    h,
                    entry_project,
                )
                return {
                    "hash": h,
                    "found": True,
                    "refused": True,
                    "reason": (
                        "project-scoped entry requires a matching project "
                        "(ccr.require_project_match)"
                    ),
                    "redactions": 0,
                    "redacted_patterns": {},
                }
            logger.warning(
                "Unscoped CCR retrieval of project-scoped entry (CWE-668): "
                "hash=%s entry_project=%s — pass project=<slug> to scope "
                "the lookup",
                h,
                entry_project,
            )

        # ── Snippet path: scan + redact each snippet in place ───────────
        if "snippets" in result:
            # B5 tier-1 (ArchCom 2026-08-27): verdict-gated snippet
            # refusal. The row's scan-at-store verdict is 'hit' → snippet
            # mode is REFUSED for this entry: FTS5 snippet windows are cut
            # around query matches with no offset mapping back to the
            # original (that mapping is tier-2, W3), so a hit row's
            # snippets cannot be proven secret-free short of withholding
            # them. NO snippet is emitted; the caller falls back to a
            # full-original retrieve, which is scanned unconditionally and
            # redacted span-wise (the full-original path below). NULL /
            # 'unknown' / 'clean' verdicts are unaffected. The refusal
            # sits before ``_mark_issued()`` — no retrieval-counter bump
            # (P1-b review F4 semantics).
            if result.get("secret_scan_verdict") == "hit":
                logger.warning(
                    "CCR snippet mode refused (verdict=hit): hash=%s project=%s "
                    "— full-original retrieve still available (redacted)",
                    h,
                    project,
                )
                return {
                    "hash": h,
                    "found": True,
                    "refused": True,
                    "reason": "snippet mode unavailable for entries with detected secrets",
                    "redactions": 0,
                    "redacted_patterns": {},
                }
            result.pop("secret_scan_verdict", None)
            # B5 tier-2 (W3): the cached original for the offset-mapped
            # scan. INTERNAL datum from ccr.retrieve — popped here, never
            # echoed in the response (the snippet channel issues snippets,
            # not originals).
            cached_original = result.pop("original", None)
            original_for_scan = cached_original if isinstance(cached_original, str) else None
            redactions = 0
            pattern_counts: dict[str, int] = {}
            # W3 review F5: localization failures are counted separately
            # so a refuse-mode refusal names its TRUE cause — "we could
            # not prove this snippet safe" (ambiguous offsets) is not
            # "we detected a secret", and conflating them misleads
            # forensics.
            localization_failures = 0
            try:
                for snippet in result["snippets"]:
                    text = str(snippet.get("snippet", ""))
                    # B5 tier-2: localize the snippet's fragments in the
                    # cached original and scan the ORIGINAL over the
                    # localized window ± margin — snippet-text detection
                    # alone misses secrets truncated at the 32-token FTS5
                    # window edge. Non-localizable snippets (absent or
                    # ambiguous fragments) fall back to the tier-1 m2
                    # behavior: the whole snippet is withheld
                    # (<REDACTED:snippet>) — offsets are unmappable, so
                    # the snippet cannot be proven secret-free.
                    outcome = _offset_mapped_snippet_scan(text, original_for_scan)
                    if outcome.clean:
                        continue
                    if not outcome.localizable:
                        snippet["snippet"] = "<REDACTED:snippet>"
                        redactions += 1
                        localization_failures += 1
                        pattern_counts["snippet"] = pattern_counts.get("snippet", 0) + 1
                        continue
                    snippet["snippet"] = outcome.text
                    redactions += len(outcome.findings)
                    for name, count in findings_by_pattern(list(outcome.findings)).items():
                        pattern_counts[name] = pattern_counts.get(name, 0) + count
            except Exception as exc:
                # m5: fail-closed — never issue unscanned snippets.
                logger.error(
                    "CCR issuance scanner error (snippets): hash=%s error=%s",
                    h,
                    exc,
                )
                return {
                    "hash": h,
                    "found": True,
                    "refused": True,
                    "reason": "scanner error",
                    "redactions": 0,
                    "redacted_patterns": {},
                }
            if redactions and self.settings.ccr.retrieve_refuse_on_secret:
                # W3 review F5: distinct, honest reason per cause. A
                # localization failure withheld snippets that could not
                # be PROVEN secret-free (nothing was detected); that is
                # a different incident from a detection and the refusal
                # forensics must not claim a detection that did not
                # happen. When BOTH occurred, the detection — the more
                # severe, actionable cause — names the refusal. Both
                # remain fail-closed refusals with no content and no
                # bump.
                detected = redactions - localization_failures
                refusal_reason = (
                    "secret detected in retrieved snippets"
                    if detected > 0
                    else "snippet localization failed (ambiguous) — refusing under refuse-mode"
                )
                logger.warning(
                    "CCR issuance refused (%s): hash=%s redactions=%d "
                    "detected=%d localization_failures=%d",
                    refusal_reason,
                    h,
                    redactions,
                    detected,
                    localization_failures,
                )
                return {
                    "hash": h,
                    "found": True,
                    "refused": True,
                    "reason": refusal_reason,
                    "redactions": redactions,
                    "redacted_patterns": pattern_counts,
                }
            _mark_issued()
            result["redactions"] = redactions
            if redactions:
                result["redacted_patterns"] = pattern_counts
                logger.info(
                    "CCR issuance redacted (snippets): hash=%s redactions=%d patterns=%s",
                    h,
                    redactions,
                    pattern_counts,
                )
            return result

        # ── Full-original path: scan + redact the returned copy ─────────
        original = result.get("original")
        if not isinstance(original, str):
            result["redactions"] = 0
            return result
        scan = self.scan_issuance(original, context="ccr:retrieve-original")
        if scan.refused:
            reason = (
                "secret detected in cached original"
                if scan.reason == "secret detected"
                else scan.reason
            )
            logger.warning(
                "CCR issuance refused (%s): hash=%s redactions=%d",
                "secret in original" if scan.redactions else scan.reason,
                h,
                scan.redactions,
            )
            return {
                "hash": h,
                "found": True,
                "refused": True,
                "reason": reason,
                "redactions": scan.redactions,
                "redacted_patterns": scan.redacted_patterns,
            }
        result["original"] = scan.text
        result["redactions"] = scan.redactions
        _mark_issued()
        if scan.redactions:
            result["redacted_patterns"] = scan.redacted_patterns
            logger.info(
                "CCR issuance redacted: hash=%s redactions=%d patterns=%s",
                h,
                scan.redactions,
                scan.redacted_patterns,
            )
        return result

    def ccr_cleanup(self) -> dict[str, int]:
        """Run CCR TTL expiry + LRU eviction. Returns removal counts."""
        from vesmaro.ccr import cleanup

        return cleanup(store=self.sqlite, config=self.settings.ccr)

    def ccr_stats(self) -> dict[str, Any]:
        """Return CCR cache statistics."""
        return {
            "enabled": self.settings.ccr.enabled,
            "entries": self.sqlite.ccr_count(),
            "ttl_days": self.settings.ccr.ttl_days,
            "max_entries": self.settings.ccr.max_entries,
            "min_size_chars": self.settings.ccr.min_size_chars,
        }

    # ── Memory edges (ADR-0018 Phase 1 groundwork) ────────────────────────

    def add_memory_edge(
        self,
        from_memory_id: str,
        to_memory_id: str,
        *,
        kind: str = "supersedes",
        weight: float = 1.0,
        provenance: str = "declared",
        scope_project: str | None = None,
        scope_agent: str | None = None,
    ) -> bool:
        """Record a directed edge ``from_memory_id`` → ``to_memory_id``.

        Thin wrapper over ``SQLiteStore.add_memory_edge`` (validation and
        constraints live there — including the #324 weight validation:
        finite and strictly positive, negative/0/inf/NaN rejected with a
        caller-actionable ValueError). ADR-0030 A0 (issue #321): ``kind``
        now also accepts ``relates_to``, and the edge carries ``weight``
        / ``provenance`` / ``scope_project`` / ``scope_agent`` with the
        contract defaults (1.0 / 'declared' / NULL / NULL). Returns
        ``True`` when inserted, ``False`` when the edge already existed
        (idempotent). Still no MCP surface — minting arrives with the A0
        fuel slice.

        ADR-0030 A1-S1 (#325): a successful insert bumps the
        PER-PROJECT ``graph_epoch`` meta-counter (the awareness-cursor
        precedent — ``set_meta``/``get_meta``, migration-free). Idempotent
        re-inserts bump nothing: the epoch tracks EDGE-TABLE CHANGE,
        not call volume. Per-project by committee ruling (ArchCom
        2026-09-27: a global epoch would be a cheap fleet-wide
        cache-DoS lever); ``scope_project``/the from-row's project is
        the bump key — ``scope_project`` rides the FIRST event per the
        I5 schema; when it is absent the from-row's own project is the
        authoritative key.
        """
        inserted = self.sqlite.add_memory_edge(
            from_memory_id,
            to_memory_id,
            kind=kind,
            weight=weight,
            provenance=provenance,
            scope_project=scope_project,
            scope_agent=scope_agent,
        )
        if inserted:
            project_key = scope_project
            if not project_key:
                row = self.sqlite.get(from_memory_id)
                project_key = (row.project or "") if row is not None else ""
            self._bump_graph_epoch(project_key or "")
        return inserted

    # ── ADR-0030 A1-S1: per-project graph_epoch (issue #325) ─────────────

    def _bump_graph_epoch(self, project: str) -> int:
        """Increment the per-project ``graph_epoch`` meta-counter.

        PER-PROJECT by committee ruling (ArchCom 2026-09-27 — verdict
        (c) condition 2: NOT global, a global epoch is a cheap
        fleet-wide cache-DoS lever). Stored via ``set_meta`` (the
        awareness-cursor precedent, migration-free); the counter is
        durable and monotonic within a project. No in-repo search cache
        key exists today — the epoch is a meta-counter any consumer can
        key on (exposed via ``search_stats()["graph_epoch_by_project"]``).
        """
        key = f"graph_epoch:{len(project)}:{project}"
        try:
            current = self.sqlite.get_meta(key)
            value = int(current) + 1 if current else 1
            self.sqlite.set_meta(key, str(value))
        except Exception as exc:
            # A meta-counter failure must never fail the edge write it
            # decorates — logged, best-effort.
            logger.warning("graph_epoch bump failed (non-fatal): %s", exc)
            return 0
        return value

    def graph_epoch(self, project: str) -> int:
        """Read the per-project ``graph_epoch`` (0 = no edge writes yet)."""
        key = f"graph_epoch:{len(project)}:{project}"
        try:
            current = self.sqlite.get_meta(key)
        except Exception as exc:
            logger.warning("graph_epoch read failed (non-fatal): %s", exc)
            return 0
        return int(current) if current else 0

    # ── ADR-0030 A1-S2: per-project feedback_epoch (#325) ────────────────

    def _bump_feedback_epoch(self, project: str) -> int:
        """Increment the per-project ``feedback_epoch`` meta-counter.

        Bumped ONLY on actual edge_stats row APPENDS (``captured > 0``
        from ``report_search_feedback``); idempotent retries bump
        nothing (ArchCom 2026-09-27, Security residual 2: without this
        component an epoch-keyed consumer serves STALE feedback once
        APPLY is live). Key discipline mirrors ``graph_epoch``
        verbatim — ``feedback_epoch:{len}:{project}``, the length
        prefix making the key colon-safe (the #415 review finding:
        the ``{len}`` segment survives ':' in project slugs).
        Best-effort: a meta-counter failure never fails the capture it
        decorates (logged, returns 0).
        """
        key = f"feedback_epoch:{len(project)}:{project}"
        try:
            current = self.sqlite.get_meta(key)
            value = int(current) + 1 if current else 1
            self.sqlite.set_meta(key, str(value))
        except Exception as exc:
            logger.warning("feedback_epoch bump failed (non-fatal): %s", exc)
            return 0
        return value

    def feedback_epoch(self, project: str) -> int:
        """Read the per-project ``feedback_epoch`` (0 = no captures yet)."""
        key = f"feedback_epoch:{len(project)}:{project}"
        try:
            current = self.sqlite.get_meta(key)
        except Exception as exc:
            logger.warning("feedback_epoch read failed (non-fatal): %s", exc)
            return 0
        return int(current) if current else 0

    def get_memory_edges(
        self,
        from_memory_id: str,
        *,
        kind: str = "supersedes",
    ) -> list[dict[str, Any]]:
        """Return direct outgoing edges (one hop, no expansion)."""
        return self.sqlite.get_direct_edges(from_memory_id, kind=kind)

    # ── Policy / DLQ (M5) ─────────────────────────────────────────────────

    def dlq_list(
        self,
        *,
        task_label: str | None = None,
        ready_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        """List Dead-Letter Queue entries."""
        from vesmaro.policy.dlq import dlq_list

        return dlq_list(self, task_label=task_label, ready_only=ready_only, limit=limit)

    def dlq_retry(self, dlq_id: str, *, backoff_sec: int = 60) -> dict[str, object]:
        """Increment retry attempt for a DLQ entry."""
        from vesmaro.policy.dlq import dlq_retry

        return dlq_retry(self, dlq_id, backoff_sec=backoff_sec)

    def dlq_discard(self, dlq_id: str) -> bool:
        """Permanently remove a DLQ entry."""
        from vesmaro.policy.dlq import dlq_discard

        return dlq_discard(self, dlq_id)

    def evaluate_policy(self, memory_id: str) -> list[PolicyAction]:
        """Evaluate policy rules against a memory and return fired actions."""
        from vesmaro.policy.engine import evaluate_rules, load_rules_from_dict

        mem = self.sqlite.get(memory_id)
        if mem is None:
            return []
        raw = getattr(self.settings, "policies", None)
        rules = load_rules_from_dict(raw) if isinstance(raw, dict) else []
        return evaluate_rules(mem, rules)
