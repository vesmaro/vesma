"""Explainability / trace layer for Vesma. (M6)

Records per-pipeline-step audit rows in SQLite traces table.

Security note:
  - Only short rationale_summary (≤200 chars) is stored.
  - Raw LLM chain-of-thought is NEVER persisted.
  - Traces table is append-only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from vesmaro.models import Trace

if TYPE_CHECKING:
    from vesmaro.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class TraceRecorder:
    """Records pipeline traces to SQLite (if store provided) or logs only."""

    def __init__(self, store: SQLiteStore | None = None) -> None:
        self.store = store

    @contextmanager
    def record(
        self,
        task_label: str,
        project: str,
        step: str,
        item_id: str | None = None,
    ) -> Generator[Trace, None, None]:
        """Context manager that records a pipeline trace row.

        Usage::
            with recorder.record("synthesize", "myproject", "llm_call", item_id=memory.id) as trace:
                response = await llm.complete(prompt)
                trace.tokens_in = response.tokens_in
                trace.tokens_out = response.tokens_out
                trace.llm_called = True
                trace.llm_done = True
                trace.rationale_summary = "Synthesised 3 raw entries into draft."
        """
        trace = Trace(task_label=task_label, project=project, step=step, item_id=item_id)
        t0 = time.monotonic()
        try:
            yield trace
        finally:
            trace.latency_ms = int((time.monotonic() - t0) * 1000)
            if trace.tokens_out > 0 and trace.latency_ms > 0:
                trace.tokens_per_sec = trace.tokens_out / (trace.latency_ms / 1000)
            logger.debug(
                "trace: task=%s step=%s item=%s latency=%dms",
                trace.task_label,
                trace.step,
                trace.item_id,
                trace.latency_ms,
            )
            if self.store is not None:
                try:
                    self.store.save_trace(trace)
                except Exception as exc:
                    logger.warning("trace: save failed (non-fatal): %s", exc)


# Legacy standalone context manager (store-less, logs only)
@contextmanager
def record_trace(
    task_label: str,
    project: str,
    step: str,
    item_id: str | None = None,
) -> Generator[Trace, None, None]:
    """Standalone trace recorder (logs only, no persistence).

    Deprecated: use TraceRecorder.record() for SQLite-backed traces.
    """
    recorder = TraceRecorder(store=None)
    with recorder.record(task_label, project, step, item_id) as trace:
        yield trace
