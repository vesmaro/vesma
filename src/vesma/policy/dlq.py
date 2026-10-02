"""DLQ — Dead-Letter Queue for failed pipeline stages (M5).

Provides high-level operations over the SQLite dlq table:
  - add:      move a failed memory into DLQ
  - list:     enumerate entries with optional retry-ready filter
  - retry:    attempt re-processing with exponential backoff
  - discard:  permanently remove from DLQ

Retry policy: exponential backoff with jitter cap (max 24h).
Max attempts default = 3 (configurable per-entry).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vesma.manager import MemoryManager

logger = logging.getLogger(__name__)


def dlq_add(
    mgr: MemoryManager,
    memory_id: str,
    *,
    cluster_id: str | None = None,
    task_label: str = "synthesize",
    error_message: str = "",
    max_attempts: int = 3,
) -> None:
    """Add a failed memory to the DLQ."""
    mgr.sqlite.dlq_add(
        memory_id,
        cluster_id=cluster_id,
        task_label=task_label,
        error_message=error_message,
        max_attempts=max_attempts,
    )
    logger.warning("dlq: added %s (%s) — %s", memory_id[:8], task_label, error_message[:80])


def dlq_list(
    mgr: MemoryManager,
    *,
    task_label: str | None = None,
    ready_only: bool = False,
    limit: int = 100,
) -> list[dict[str, object]]:
    """List DLQ entries."""
    return mgr.sqlite.dlq_list(task_label=task_label, ready_only=ready_only, limit=limit)


def dlq_retry(
    mgr: MemoryManager,
    dlq_id: str,
    *,
    backoff_sec: int = 60,
) -> dict[str, object]:
    """Increment attempt count and schedule next retry.

    Returns the updated entry dict.
    """
    mgr.sqlite.dlq_increment_attempt(dlq_id, backoff_sec=backoff_sec)
    # Return fresh state
    rows = mgr.sqlite.dlq_list(limit=1)
    return rows[0] if rows else {}


def dlq_discard(mgr: MemoryManager, dlq_id: str) -> bool:
    """Permanently remove a DLQ entry."""
    ok = mgr.sqlite.dlq_remove(dlq_id)
    if ok:
        logger.info("dlq: discarded %s", dlq_id[:8])
    return ok
