"""Refine worker — ADR-0019 Phase B2a: async refinement of pending rows.

The optimistic-publication semantics (ADR-0019): an entry is visible the
moment it passes the ingest gate; THIS worker owns the async half — it
picks rows by ``pipeline_state='pending'`` (plus ``failed`` rows with
retry budget left), claims one atomically, produces a refined projection
and swaps it in place on the SAME row (same id, stable rowid) in a
single ``update_fields`` transaction.

Outcome lanes (§5):

* ``refined``       — an artifact exists: transactionally swapped
  (``content`` ← projection, ``clean_content`` reset so the swap is not
  masked by ``effective_content()``, ``swap_key`` idempotency by analogy
  with ``synthesis_cache_key``). The vector re-embed happens AFTER the
  commit, outside the transaction; a failed upsert is healed later by
  the idempotent sweeper (``MemoryManager.heal_stale_embeddings``).
* ``refined-noop``  — the deterministic stub synthesis has nothing
  meaningful to add (a lone entry with no admissible cluster context):
  the row transitions pending→refined WITHOUT touching the content — an
  honest "checked, nothing to improve", never a swap of the raw text
  for a placeholder.
* ``failed``        — lane (a): quality/infra error. The entry STAYS
  visible raw (status untouched); retry with exponential backoff, the
  attempt counter in ``metadata`` (the schema is closed since B1 — no
  new column), at most ``REFINE_MAX_ATTEMPTS`` attempts, then stable.
* ``quarantined``   — lane (b): a POSITIVE danger-detector signal on the
  PROCESSED projection (a secret or injection introduced by the
  processing itself is caught exactly here), or detector/scanner
  ambiguity — fail-closed in both directions (§5: ambiguity quarantines,
  it does not retry). Terminal until ``release_quarantine``.

The legacy RAW→PROCESSING clustering flow is NOT this queue: NULL
pipeline_state rows keep their pre-ADR-0019 semantics (Phase D decides
their retirement).

B2b §2 (curated visibility): both completion points (the swap and the
honest noop) hand the row to ``MemoryManager._curated_publish`` — in
``curated`` deployments the row is still RAW there, and the refined
projection receives its visibility through the same publication gate
(clean ⇒ PUBLISHED + embed; refusal ⇒ lane-(b) quarantine). Immediate
deployments skip it (visibility was granted at ingest).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from vesma.danger_detectors import detect
from vesma.models import MemoryStatus, PipelineState

if TYPE_CHECKING:
    from vesma.manager import MemoryManager
    from vesma.models import Memory

logger = logging.getLogger(__name__)

#: Retry budget for lane (a) — after this many attempts the row is
#: stably ``failed`` (it stays visible raw; the Phase C stuck-raw metric
#: counts exactly these forever-not-refined rows).
REFINE_MAX_ATTEMPTS = 3

#: Exponential backoff base for lane (a) retries (same convention as the
#: DLQ's ``backoff_sec``): attempt n waits ``base * 2**(n-1)`` seconds,
#: capped at ``REFINE_BACKOFF_CAP_SEC``.
REFINE_BACKOFF_BASE_SEC = 60
REFINE_BACKOFF_CAP_SEC = 3600

#: Version tag of the deterministic processing itself — part of the
#: ``swap_key``, so bumping the processor produces a different artifact
#: hash and re-swaps rows refined by an older version.
REFINE_PROCESSING_VERSION = "stub-v1"

#: Issue #170 (ADR-0019 Phase C) — lease timeout of a ``processing``
#: claim. A worker crash between the CAS claim and the outcome write
#: strands the row; the sweeper reclaims rows whose ``updated_at`` (the
#: lease clock, stamped at the claim) is older than this. Single-worker
#: deployment today: 10 minutes covers any digest run with a wide
#: margin. Configurable by design — a multi-worker or LLM-backed
#: producer that can legitimately run longer raises it instead of
#: relying on the sweeper not firing.
REFINE_LEASE_TIMEOUT_SEC = 600

#: Outcome codes (audit vocabulary: ``outcome=…``).
OUTCOME_REFINED = "refined"
OUTCOME_REFINED_NOOP = "refined-noop"
OUTCOME_FAILED = "failed"
OUTCOME_QUARANTINED = "quarantined"
#: Issue #170 — the sweeper's lease-reclaim audit outcome (see
#: :meth:`MemoryManager.reclaim_stale_refinements`).
OUTCOME_LEASE_RECLAIMED = "lease-reclaimed"

#: Quarantine reason for detector/scanner AMBIGUITY (§5: an error of the
#: detector while scanning the processed projection quarantines
#: fail-closed — it is NOT a lane-(a) retry).
QUARANTINE_REASON_DETECTOR_ERROR = "detector-error"


def _swap_key(cluster_context: str, processed_content: str) -> str:
    """Idempotency key of one swap — by analogy with ``synthesis_cache_key``.

    ``hash(cluster-context : processing-version : processed-hash)`` — a
    second run producing the SAME artifact derives the SAME key and the
    swap collapses to a no-op (no marker_version bump, no rewrite).
    """
    processed_hash = hashlib.sha256(processed_content.encode()).hexdigest()[:24]
    payload = f"{cluster_context}:{REFINE_PROCESSING_VERSION}:{processed_hash}"
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def _retry_backoff_sec(attempt: int) -> int:
    """Exponential backoff for lane (a), capped (DLQ convention)."""
    return int(min(REFINE_BACKOFF_BASE_SEC * (2 ** (attempt - 1)), REFINE_BACKOFF_CAP_SEC))


def _revision_hash(text: str) -> str:
    """Short content revision hash for audit lines (§Swap audit contract).

    ``swap_committed`` carries old/new revision HASHES — never the raw
    content — so the audit trail stays safe to log while still letting
    an operator correlate which projection a row served before/after.
    """
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _produce_refined_projection(mgr: MemoryManager, memory: Memory) -> str | None:
    """Deterministic stub of the refinement synthesis (the artifact seam).

    "Artifact or not" is decided HERE and only here — the rest of the
    cycle is mechanics that must not care. Today's deterministic stub
    mirrors :mod:`vesma.pipeline.synthesize`'s placeholder convention:

    * a lone entry (no cluster context, or a cluster whose other members
      are not themselves visible) has nothing the stub can meaningfully
      improve → ``None`` → the honest ``refined-noop`` outcome (the task
      of this phase is the mechanics, not a fake projection);
    * a cluster of >= 2 context-admissible entries produces a digest
      projection of the cluster (the same shape the synthesis stub
      writes into NEW rows — here it becomes the swapped projection of
      the SAME row).

    When a real LLM provider lands (the ``llm/`` TODO in synthesize.py),
    this function is the single replacement point.

    Idempotency note: the digest is cut from each member's
    ``raw_content or effective_content()`` — the immutable source when
    it exists. Building it from ``effective_content`` alone would make
    the projection self-referential (the swapped row is itself a
    member), so every re-run would produce a different digest, miss the
    ``swap_key`` match and inflate ``marker_version`` forever.
    """
    if not memory.cluster_id:
        return None
    from vesma.models import is_context_admissible

    mates = [m for m in mgr.sqlite.list_by_cluster(memory.cluster_id) if is_context_admissible(m)]
    if len(mates) < 2:
        return None
    parts = [f"# Refined digest of cluster {memory.cluster_id[:8]}", ""]
    parts.extend(f"- {(m.raw_content or m.effective_content())[:200]}" for m in mates)
    return "\n".join(parts)


def refine_single(mgr: MemoryManager, memory_id: str) -> str:
    """Run one full refine cycle on a row. Returns the outcome code.

    The caller (``refine_pending`` / the daemon) selects candidates; this
    function re-validates eligibility through the atomic claim — a row
    that lost the race (or is not intake-eligible) is a ``lost-race``
    no-op, never an error.
    """
    memory = mgr.sqlite.get(memory_id)
    if memory is None:
        return "missing"

    # ── Atomic grab: intake-eligible → processing (CAS; a concurrent ──
    # ── second worker gets False and treats the row as a no-op).     ──
    if not mgr.sqlite.claim_for_refinement(memory_id, max_attempts=REFINE_MAX_ATTEMPTS):
        return "lost-race"

    attempt = int(memory.metadata.get("pipeline_retry_count") or 0) + 1

    # ── Produce the artifact (seam — see _produce_refined_projection) ──
    try:
        cluster_context = memory.cluster_id or f"solo:{memory.id}"
        artifact = _produce_refined_projection(mgr, memory)
    except Exception as exc:  # lane (a): quality/infra error of the processing
        _record_failure(mgr, memory, attempt, reason=f"producer-error:{type(exc).__name__}")
        return OUTCOME_FAILED

    if artifact is None:
        # Honest "checked, nothing to improve": state transition only —
        # no content mutation, no swap_key, marker_version unchanged.
        # ISO string: sqlite3 3.12 deprecates the default datetime adapter.
        now = datetime.now(UTC).isoformat()
        mgr.sqlite.update_fields(memory_id, pipeline_state=PipelineState.REFINED, processed_at=now)
        logger.info(
            "pipeline: id=%s outcome=%s reason=no-artifact attempt=%d",
            memory_id[:8],
            OUTCOME_REFINED_NOOP,
            attempt,
        )
        # B2b §2: curated completion of a noop cycle — the (unchanged)
        # projection still owes its publication verdict; clean ⇒ the row
        # becomes visible now, refused ⇒ lane-(b) quarantine. Immediate
        # mode: skipped (the row is already published).
        mgr._curated_publish(memory_id)
        return OUTCOME_REFINED_NOOP

    # ── Lane (b) gate on the PROCESSED projection: a secret/injection ──
    # ── introduced by the processing itself is caught exactly here.  ──
    detection = detect(artifact, memory.title)
    if detection.error is not None:
        # §5 ambiguity: a detector error quarantines fail-closed — it is
        # not a lane-(a) retry (an unreliable scanner must not keep
        # re-serving the row as visible-raw).
        mgr.quarantine_entry(
            memory_id, reason=QUARANTINE_REASON_DETECTOR_ERROR, source="refine-ambiguity"
        )
        logger.warning(
            "pipeline: id=%s outcome=%s reason=%s attempt=%d error=%s",
            memory_id[:8],
            OUTCOME_QUARANTINED,
            QUARANTINE_REASON_DETECTOR_ERROR,
            attempt,
            detection.error,
        )
        return OUTCOME_QUARANTINED
    if detection.positive:
        reason = ",".join(sorted({f.detector_class for f in detection.findings}))
        mgr.quarantine_entry(memory_id, reason=reason, source="refine-detector")
        logger.warning(
            "pipeline: id=%s outcome=%s reason=%s attempt=%d patterns=%s — raw values not logged",
            memory_id[:8],
            OUTCOME_QUARANTINED,
            reason,
            attempt,
            detection.patterns_by_class(),
        )
        return OUTCOME_QUARANTINED

    # ── The swap (§6): ONE update_fields call = one transaction. The ───
    # ── AFTER UPDATE trigger reindexes the SAME rowid (no FTS rebuild)─
    swap_key = _swap_key(cluster_context, artifact)
    # ISO string: sqlite3 3.12 deprecates the default datetime adapter.
    now = datetime.now(UTC).isoformat()
    if memory.swap_key == swap_key:
        # Idempotent re-run with the SAME artifact: no content rewrite,
        # no marker_version bump — only the lifecycle columns converge.
        mgr.sqlite.update_fields(
            memory_id,
            pipeline_state=PipelineState.REFINED,
            processed_at=now,
            swap_key=swap_key,
        )
    else:
        fields: dict[str, Any] = {
            "content": artifact,
            # Reset the filter projection: a stale clean_content would
            # mask the swapped content in effective_content().
            "clean_content": None,
            "pipeline_state": PipelineState.REFINED,
            "processed_at": now,
            "swap_key": swap_key,
        }
        # Zero-loss: if the immutable source payload was never
        # materialised, the pre-swap served projection becomes it now
        # (first write — the models.py invariant forbids later writes).
        if memory.raw_content is None:
            fields["raw_content"] = memory.effective_content()
        # marker_version grows ONLY on a real content change of the
        # served projection (consumers desync-detect through it).
        if artifact != memory.effective_content():
            fields["marker_version"] = memory.marker_version + 1
        try:
            mgr.sqlite.update_fields(memory_id, **fields)
        except Exception as exc:  # lane (a): infra error of the swap write
            _record_failure(mgr, memory, attempt, reason=f"swap-error:{type(exc).__name__}")
            return OUTCOME_FAILED
        # ADR-0019 §Swap audit — swap_committed at the commit point, with
        # the OLD and NEW content revision hashes (never the content).
        logger.info(
            "swap_committed: id=%s old_revision=%s new_revision=%s",
            memory_id[:8],
            _revision_hash(memory.effective_content()),
            _revision_hash(artifact),
        )

    # ── ADR-0019 B2b §2: curated completion — the swapped projection ──
    # goes through the SAME publication gate; clean ⇒ PUBLISHED (the
    # record receives the refined content and its visibility in the same
    # completion event), refusal ⇒ lane-(b) quarantine per the B2a
    # convention. Immediate mode: skipped — visibility was granted at
    # ingest and nothing here may demote it.
    curated_outcome = mgr._curated_publish(memory_id)

    # ── Vector: re-embed AFTER the commit, outside the transaction ────
    # (ADR §Swap: a vector-store outage must not become a SQLite
    # read-path outage; the idempotent sweeper heals a failed upsert).
    # A curated publication embeds the fresh row itself (one embed per
    # upsert); an invisible row (RAW — e.g. curated-refused, quarantined
    # by the gate above) gets NO embed at all.
    swapped = mgr.sqlite.get(memory_id)
    if (
        swapped is not None
        and swapped.status == MemoryStatus.PUBLISHED
        and curated_outcome != "published"
    ):
        try:
            mgr.upsert_embedding(swapped)
        except Exception as exc:
            logger.warning(
                "refine: post-swap vector upsert failed for %s (sweeper will heal): %s",
                memory_id[:8],
                exc,
            )

    logger.info(
        "pipeline: id=%s outcome=%s reason=swapped attempt=%d marker_version=%s",
        memory_id[:8],
        OUTCOME_REFINED,
        attempt,
        swapped.marker_version if swapped is not None else "?",
    )
    return OUTCOME_REFINED


def _record_failure(mgr: MemoryManager, memory: Memory, attempt: int, *, reason: str) -> None:
    """Lane (a): visible-raw failure with bounded backoff retry (§5)."""
    next_retry_at: str | None
    if attempt < REFINE_MAX_ATTEMPTS:
        delay = _retry_backoff_sec(attempt)
        next_retry_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
    else:
        # Budget exhausted: stable failure — the row stays visible raw,
        # the intake predicate's ``< max`` check stops re-claiming it.
        next_retry_at = None
    mgr.sqlite.record_refine_failure(memory.id, attempt=attempt, next_retry_at=next_retry_at)
    logger.warning(
        "pipeline: id=%s outcome=%s reason=%s attempt=%d next_retry=%s",
        memory.id[:8],
        OUTCOME_FAILED,
        reason,
        attempt,
        next_retry_at or "stable",
    )


def refine_pending(
    mgr: MemoryManager,
    *,
    project: str | None = None,
    agent: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Drain up to ``limit`` refine-intake rows (daemon pickup, §10-B).

    Returns a summary dict for observability / CLI output — the same
    convention as ``MemoryManager.run_pipeline``.
    """
    candidates = mgr.sqlite.list_refine_intake(
        limit=limit, project=project, agent=agent, max_attempts=REFINE_MAX_ATTEMPTS
    )
    counts = {
        OUTCOME_REFINED: 0,
        OUTCOME_REFINED_NOOP: 0,
        OUTCOME_FAILED: 0,
        OUTCOME_QUARANTINED: 0,
        "lost-race": 0,
    }
    refined_ids: list[str] = []
    for mem in candidates:
        outcome = refine_single(mgr, mem.id)
        if outcome in counts:
            counts[outcome] += 1
        if outcome == OUTCOME_REFINED:
            refined_ids.append(mem.id)

    summary = {
        "considered": len(candidates),
        "refined": counts[OUTCOME_REFINED],
        "refined_noop": counts[OUTCOME_REFINED_NOOP],
        "refine_failed": counts[OUTCOME_FAILED],
        "quarantined": counts[OUTCOME_QUARANTINED],
        "lost_race": counts["lost-race"],
        "refined_ids": refined_ids,
    }
    if candidates:
        logger.info(
            "refine cycle: considered=%d refined=%d noop=%d failed=%d quarantined=%d lost_race=%d",
            summary["considered"],
            summary["refined"],
            summary["refined_noop"],
            summary["refine_failed"],
            summary["quarantined"],
            summary["lost_race"],
        )
    return summary
