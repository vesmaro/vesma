"""ADR-0030 A0 (issue #322) — deterministic ``relates_to`` auto-minting.

The server mints the graph itself, from write traffic that already flows
(ADR-0030 Decision 2): after every ``MemoryManager.add`` the minting leg
runs ONE synchronous retrieval over the EXISTING legs (no new index, no
LLM, no external calls) and turns the top-1..3 near-duplicate candidates
above the similarity threshold into ``relates_to`` edges with a raised
weight and the provenance marker ``auto-dedupe`` (the #336 schema:
``weight`` / ``provenance`` / ``scope_*`` columns).

Similarity signal — measured decision, recorded here for the A0-review:
the threshold applies to the VECTOR leg's raw cosine similarity
(``VectorStore.search`` — existing machinery, the A9 project predicate
included). The committee contract says "through the EXISTING FTS+vector
legs"; the FTS leg is deliberately NOT consulted for qualification
because its v2 AND semantics (``fts_query_v2``: per-token prefix AND,
OR-fallback only when the AND query yields ZERO rows) make it a
token-superset filter that structurally misses the re-add direction —
the new text's extra tokens fail the AND against the older near-dup,
and the new row's own self-match keeps the AND non-empty so the OR
fallback never fires. A fused RRF score was rejected as the threshold
basis for the same measured reason: RRF is rank-based, so a threshold
on it means different things at 10 rows and 10M rows and admits
everything in a sparse corpus — not calibratable. Cosine is absolute,
corpus-independent, and the standard near-dup measure; the A0-review
owns its live-corpus calibration. Candidate space note: only
published/processed rows are admissible by the status gates, and
published rows are embedded by construction (``upsert_embedding`` on
publish, healed by the sweeper), so the vector leg covers the admissible
space; processed-but-unembedded rows are a documented residual.

No supersede decisions anywhere on this path — similarity does not prove
replacement; ``relates_to`` is the honest weaker claim (Security veto,
ADR-0030 "Alternatives considered"). Minting never mutates visibility or
status.

Fuel policy (#322 review M2, TL decision): minting fuel is ORGANIC USER
WRITES only. Internal machine-driven ``MemoryManager.add`` callers —
checkpoint saves (checkpoints mechanically re-state the content they
cite), mesh ingest and migrate import (bulk re-statements of a corpus) —
pass ``mint_relates_to=False`` and never mint; their high-cosine
self-citation would flood the graph with infra churn instead of
near-duplicate signal. User surfaces (MCP / REST / SDK / CLI add), the
``context_rewrite`` channel (the fuel target of ADR-0030) and the file
watchers mint normally. The mint hook additionally validates the NEW
memory from-side (review M1): a ``mnemos:no-federate`` row (including a
write the scanner just auto-tagged) and a non-admissible row
(raw/processing/quarantined) mint nothing — the endpoint exclusions bind
BOTH sides of an edge.

This module holds the CONSTANTS and the PURE candidate selector so the
exclusion set (invariants I4 / §5 / intra-project / admissibility) is
unit-testable without a store. The orchestration (retrieval, edge
insert, telemetry) lives in ``MemoryManager._mint_relates_to_edges``;
the flag gate (``mnemos.graph_auto_mint``, default ON since the
owner decision of 2026-09-28) lives at that call site.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from vesmaro.models import (
    NO_FEDERATE_TAG,
    Memory,
    SearchResult,
    is_context_admissible,
    is_quarantined,
)

# The provenance marker for minted edges (ADR-0030 Decision 2; the #336
# ``provenance`` column contract: 'declared' for explicit callers, the
# auto-dedupe rule id for minted edges).
AUTO_DEDUPE_PROVENANCE: Final[str] = "auto-dedupe"

# Edge kind minted by this leg (the #336 ``_EDGE_KINDS`` whitelist).
AUTO_DEDUPE_EDGE_KIND: Final[str] = "relates_to"

# Raised weight for minted near-dup edges: the store default (and the
# declared-edge contract weight) is 1.0 — a cosine-qualified near-dup
# match is a stronger relate signal than an explicit declared edge, so
# it enters ranking at 2x the default. Rank-only by contract (I3:
# weights participate in ranking AFTER gate filtering, never restore
# eligibility); feedback factors multiply at read time, never here.
# Recalibrated together with the threshold at A0-review.
AUTO_DEDUPE_EDGE_WEIGHT: Final[float] = 2.0

# Near-duplicate similarity threshold on the VECTOR-leg cosine
# similarity (see the module docstring for why cosine and not the fused
# RRF score). 0.92 is an expert estimate: text pairs that are edits of
# one another typically embed at cosine >= ~0.90 (a +1-token edit of a
# 12-token block measures ~0.95 on a bag-of-tokens embedder), while
# topically different pairs land well under 0.85.
#
# CALIBRATION NOTE (named for A0-review, committee contract §6 open
# question): this value is set WITHOUT live minting data; threshold
# calibration on the live corpus is an A0-review item. The threshold is
# embedder-relative — recalibrate after an embedder swap (ADR-0021
# fingerprint change), not just on volume. Do not tune ad hoc outside
# that review.
AUTO_DEDUPE_SIMILARITY_THRESHOLD: Final[float] = 0.92

# Top-1..3 candidates per write (ADR-0030 Decision 2: "the top-1..3
# candidates above the similarity threshold"). The cap bounds hub
# fan-out from day one (the ADR-0029 headroom gate bounds the walk; a
# write-side cap bounds the minting side).
AUTO_DEDUPE_MAX_CANDIDATES: Final[int] = 3

# Retrieval depth for the candidate pool: the new memory itself
# cosines at 1.0 with its own query and is excluded by id, so the pool
# must cover self + the top-3 candidates + headroom for rows the
# selector will exclude (no-federate / quarantined / cross-project /
# non-admissible). Deterministic regardless of pool depth; only recall
# of the 4th-and-beyond candidate is at stake.
AUTO_DEDUPE_CANDIDATE_POOL: Final[int] = 10


def select_auto_dedupe_candidates(
    memory: Memory,
    results: Sequence[SearchResult],
) -> list[SearchResult]:
    """Pick the near-duplicate minting candidates from retrieval results.

    Pure and deterministic GIVEN the input sequence: survivors are
    re-sorted by ``(score desc, id asc)`` — the ADR-0028 id-tiebreak —
    so the retrieval side's tie order never leaks into the choice, and
    the output is a function of ``(memory, results)`` alone.
    Qualification (review L2): the CALLER's pool is truncated at the
    retrieval top-k (``VectorStore.search`` argpartition) with no id
    tiebreak beyond the pool boundary — with more than
    ``AUTO_DEDUPE_CANDIDATE_POOL`` equal-score candidates (an
    exact-duplicate flood) the pool membership itself is position-
    dependent. The residual is bounded by construction: at most the
    pool size is ever considered and at most
    ``AUTO_DEDUPE_MAX_CANDIDATES`` edges mint.
    ``result.score`` carries the VECTOR-leg cosine similarity on the
    minting path.

    Exclusion set (each is a hard ADR-0030/issue #322 requirement, and
    each is asserted here even where the retrieval side already gates it
    — the selector is the single auditable place the candidate contract
    lives, defended against future retrieval-side changes):

    * SELF — the new memory never links to itself (store-level
      self-edge rejection is the backstop).
    * ``via_graph`` rows — candidates must come from the retrieval
      legs, not from edge expansion: an expansion row is not a
      similarity signal (kept as a guard even though the minting
      retrieval has no graph leg today).
    * ``mnemos:no-federate`` (I4, generation side) — a no-federate node
      never becomes an edge endpoint through minting.
    * §5 quarantine (ADR-0019) — absolute, composed via the shared
      ``is_quarantined`` predicate.
    * Non-admissible statuses (raw/processing/archived) — the general
      gates, via the shared ``is_context_admissible`` predicate.
    * Cross-project rows — intra-project only (authoritative
      ``(project or "")`` equality; ``""`` is a project for this
      purpose, so an unscoped write links only to unscoped rows).

    Then the similarity threshold and the top-1..3 cap.
    """
    survivors: list[SearchResult] = []
    for result in results:
        cand = result.memory
        if cand.id == memory.id:
            continue  # self
        if result.via_graph:
            continue  # edge-sourced rows are not similarity candidates
        if NO_FEDERATE_TAG in cand.tags:
            continue  # I4 — generation side
        if is_quarantined(cand):
            continue  # ADR-0019 §5 — absolute
        if not is_context_admissible(cand):
            continue  # raw/processing/archived are not near-dup fuel
        if (cand.project or "") != (memory.project or ""):
            continue  # intra-project only
        if result.score < AUTO_DEDUPE_SIMILARITY_THRESHOLD:
            continue  # below the near-duplicate threshold
        survivors.append(result)
    survivors.sort(key=lambda r: (-r.score, r.memory.id))
    return survivors[:AUTO_DEDUPE_MAX_CANDIDATES]
