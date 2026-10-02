"""Publish stage — M4: status=processed→published + vector index upsert.

Only status="published" ever enters the vector index.
This is the key invariant that keeps hybrid recall high-signal.

ADR-0019 Phase A: the stage carries the fail-closed **danger gate** —
the point where entry admissibility is flagged. Before the status flip,
every publication attempt (including the Hermes-style
``skip_quality_check`` bypass — the gate is separate from the quality
check) must pass the enumerated danger detectors
(:mod:`vesma.danger_detectors`) over the served projection and the
title. A detector/scanner error or a positive signal (prompt injection,
high-confidence secret) refuses the publication: the record stays
stored (zero-loss) and invisible. Storage is never blocked — only the
visibility transition is.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from vesma.danger_detectors import detect
from vesma.models import MemoryStatus, PipelineState, PublishResult

if TYPE_CHECKING:
    from vesma.manager import MemoryManager

logger = logging.getLogger(__name__)


def publish_memory(
    mgr: MemoryManager,
    memory_id: str,
    *,
    skip_quality_check: bool = False,
) -> PublishResult:
    """Promote a processed memory to published and index it.

    Args:
        mgr: MemoryManager instance.
        memory_id: The processed memory to publish.
        skip_quality_check: If True, bypass quality gate (use with care).
            Does NOT bypass the ADR-0019 danger gate — that gate is
            separate from the quality check and always runs.

    Returns:
        PublishResult indicating success and whether vector indexing occurred.
        A danger-gate refusal returns ``published=False`` with the record
        left stored and its status unchanged (existing refusal convention
        of this stage — same as the skip-quality-check refusal).
    """
    memory = mgr.sqlite.get(memory_id)
    if memory is None:
        return PublishResult(
            memory_id=memory_id,
            published=False,
            previous_status="",
        )

    previous = memory.status.value

    if memory.status != MemoryStatus.PROCESSED and not skip_quality_check:
        logger.warning(
            "publish: %s status=%s, expected processed — skipping",
            memory_id[:8],
            memory.status.value,
        )
        return PublishResult(
            memory_id=memory_id,
            published=False,
            previous_status=previous,
        )

    # ── ADR-0019 Phase A: fail-closed danger gate ─────────────────────
    # Runs on the served projection (effective_content) and the title,
    # before the status flip. Rules:
    #   (a) detector/scanner error → refuse (fail-closed): the entry is
    #       stored (zero-loss) but stays invisible;
    #   (b) positive danger signal (prompt injection / high-confidence
    #       secret) → refuse with the class/pattern names as the reason;
    #   (c) clean → publish as before.
    # skip_quality_check (the Hermes bypass flag) does NOT exempt this
    # gate — the gate is the admissibility decision, not a quality
    # score. Audit: one structured verdict line per attempt, correlated
    # by memory id; pattern names and counts only — never raw values.
    detection = detect(memory.effective_content(), memory.title)
    if detection.error is not None:
        logger.error(
            "publish gate: %s verdict=refused reason=scanner-error "
            "bypass_quality=%s error=%s — record stays stored and unpublished",
            memory_id[:8],
            skip_quality_check,
            detection.error,
        )
        return PublishResult(
            memory_id=memory_id,
            published=False,
            previous_status=previous,
        )
    if detection.positive:
        logger.warning(
            "publish gate: %s verdict=refused reason=danger-detector "
            "classes=%s patterns=%s bypass_quality=%s — raw values not logged",
            memory_id[:8],
            sorted({f.detector_class for f in detection.findings}),
            detection.patterns_by_class(),
            skip_quality_check,
        )
        return PublishResult(
            memory_id=memory_id,
            published=False,
            previous_status=previous,
        )
    logger.info(
        "publish gate: %s verdict=pass bypass_quality=%s",
        memory_id[:8],
        skip_quality_check,
    )

    # Transition status
    from datetime import UTC, datetime

    memory.status = MemoryStatus.PUBLISHED
    memory.updated_at = datetime.now(UTC)
    mgr.sqlite.save(memory)

    # ── ADR-0019 Phase B2a: entry into the refinement pipeline ───────
    # Optimistic publication: the now-visible row joins the async
    # refinement queue. Existing lifecycle states are NOT clobbered:
    #   * NULL (first publication of this row) → pending;
    #   * failed (manual re-publication) → pending, a fresh cycle (the
    #     retry counter is reset with it);
    #   * refined/pending/processing → untouched (a re-publication that
    #     did not change the content keeps a converged row converged).
    if memory.pipeline_state is None or memory.pipeline_state == PipelineState.FAILED:
        from_state = memory.pipeline_state.value if memory.pipeline_state else "none"
        if memory.pipeline_state == PipelineState.FAILED:
            mgr.sqlite.clear_refine_retry(memory_id)
        mgr.sqlite.update_fields(memory_id, pipeline_state=PipelineState.PENDING)
        logger.info(
            "pipeline: id=%s outcome=enqueued from=%s state=pending",
            memory_id[:8],
            from_state,
        )

    # Upsert to vector index (single embedding write point: stamps the
    # freshness hash + emits the embed_upserted audit event).
    vector_indexed = False
    try:
        mgr.upsert_embedding(memory)
        vector_indexed = True
    except Exception as exc:
        logger.warning("publish: vector upsert failed for %s: %s", memory_id[:8], exc)

    logger.info(
        "publish: %s → published (vector=%s)",
        memory_id[:8],
        vector_indexed,
    )
    return PublishResult(
        memory_id=memory_id,
        published=True,
        vector_indexed=vector_indexed,
        previous_status=previous,
    )
