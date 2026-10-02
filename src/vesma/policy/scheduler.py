"""Scheduler — M5: APScheduler periodic tasks.

Runs background jobs:
  - auto_cluster   : every N seconds, cluster raw memories
  - auto_synthesize: every M seconds, synthesize ready clusters
  - auto_publish   : every K seconds, publish memories that passed quality gates
  - dlq_retry      : every R seconds, retry ready DLQ entries

All intervals are configurable via AutomationConfig.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from vesma.models import MemoryStatus

if TYPE_CHECKING:
    from vesma.manager import MemoryManager

logger = logging.getLogger(__name__)


def auto_cluster(mgr: MemoryManager) -> None:
    """Periodic task: cluster raw memories."""
    cfg = mgr.settings.automation
    if not cfg.enabled:
        return
    raw_count = mgr.sqlite.count_by_status().get("raw", 0)
    if raw_count < cfg.min_raw_to_trigger:
        logger.debug("scheduler: raw=%s < min=%s, skip cluster", raw_count, cfg.min_raw_to_trigger)
        return
    results = mgr.cluster()
    if results:
        logger.info("scheduler: clustered %s groups", len(results))


def auto_synthesize(mgr: MemoryManager) -> None:
    """Periodic task: synthesize clusters with status=processing."""
    cfg = mgr.settings.automation
    if not cfg.enabled:
        return
    # Find clusters that have processing members but no processed draft yet
    processing = mgr.sqlite.list_all(status=MemoryStatus.PROCESSING, limit=100)
    seen_clusters: set[str] = set()
    for mem in processing:
        cid = mem.cluster_id
        if not cid or cid in seen_clusters:
            continue
        seen_clusters.add(cid)
        # Check if a processed draft already exists for this cluster
        existing = [
            m for m in mgr.sqlite.list_by_cluster(cid) if m.status == MemoryStatus.PROCESSED
        ]
        if existing:
            continue
        result = mgr.synthesize(cid)
        if result:
            logger.info("scheduler: synthesized %s", result.draft_id[:8])


def auto_publish(mgr: MemoryManager) -> None:
    """Periodic task: publish processed memories that pass quality gates."""
    cfg = mgr.settings.automation
    if not cfg.enabled:
        return
    processed = mgr.sqlite.list_all(status=MemoryStatus.PROCESSED, limit=50)
    published = 0
    for mem in processed:
        qg = mgr.quality_gate(mem.id)
        if qg.passed:
            pub = mgr.publish(mem.id)
            if pub.published:
                published += 1
    if published:
        logger.info("scheduler: auto-published %s memories", published)


def dlq_retry_scheduler(mgr: MemoryManager) -> None:
    """Periodic task: retry DLQ entries whose next_retry_at has passed."""
    from vesma.policy.dlq import dlq_discard, dlq_list, dlq_retry

    ready = dlq_list(mgr, ready_only=True, limit=20)
    for entry in ready:
        # `dlq_list` returns dict[str, object] (untyped column values).
        # The two ints we need (attempt_count, max_attempts) are guaranteed
        # ints by the SQLite schema (dlq rows). `int(...)` does not accept
        # the bare `object` per mypy --strict, so we cast the value to int
        # explicitly (the schema is the single source of truth).
        dlq_id = str(entry["id"])
        attempt = int(cast(int, entry["attempt_count"]))
        max_attempts = int(cast(int, entry["max_attempts"]))
        if attempt >= max_attempts:
            logger.warning("scheduler: dlq %s max retries reached, discarding", dlq_id[:8])
            dlq_discard(mgr, dlq_id)
            continue
        # Retry: increment attempt and let next scheduler tick try again
        dlq_retry(mgr, dlq_id)
        logger.info(
            "scheduler: dlq %s retry scheduled (attempt %s/%s)",
            dlq_id[:8],
            attempt + 1,
            max_attempts,
        )
