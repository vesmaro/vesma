"""Quality gate — M4: score / confidence / source_coverage thresholds.

Evaluates a processed memory against configurable thresholds.
Returns QualityResult with pass/fail and a short rationale.

The gate is intentionally conservative: a memory must meet ALL
thresholds to pass.  Failed memories stay in status=processed and
can be retried or manually reviewed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from vesma.models import MemoryStatus, QualityResult

if TYPE_CHECKING:
    from vesma.manager import MemoryManager

logger = logging.getLogger(__name__)


def evaluate_quality(
    mgr: MemoryManager,
    memory_id: str,
    *,
    min_quality: float | None = None,
    min_confidence: float | None = None,
    min_source_coverage: int | None = None,
) -> QualityResult:
    """Run quality gates on a processed memory.

    Args:
        mgr: MemoryManager instance.
        memory_id: The processed memory to evaluate.
        min_quality: Override config threshold (0-1).
        min_confidence: Override config threshold (0-1).
        min_source_coverage: Override config minimum sources.

    Returns:
        QualityResult with passed=True only if ALL thresholds met.
    """
    memory = mgr.sqlite.get(memory_id)
    if memory is None:
        return QualityResult(
            passed=False,
            memory_id=memory_id,
            failures=["memory not found"],
            rationale="Memory does not exist.",
        )

    if memory.status != MemoryStatus.PROCESSED:
        return QualityResult(
            passed=False,
            memory_id=memory_id,
            failures=[f"status is {memory.status.value}, expected processed"],
            rationale="Only processed memories can pass quality gates.",
        )

    # Resolve thresholds from config or overrides.
    # Defaults are conservative-but-achievable: the placeholder synthesis
    # (no real LLM wired yet) assigns quality_score=0.5, confidence=0.5.
    # The previous defaults (0.6/0.6/2) guaranteed every placeholder draft
    # failed the gate, causing the processing queue to grow unbounded (P0-1).
    # When a real LLM provider is wired, raise these via config.
    cfg = mgr.settings
    mq = min_quality if min_quality is not None else getattr(cfg, "min_quality", 0.4)
    mc = min_confidence if min_confidence is not None else getattr(cfg, "min_confidence", 0.4)
    msc = (
        min_source_coverage
        if min_source_coverage is not None
        else getattr(cfg, "min_source_coverage", 1)
    )

    failures: list[str] = []
    qs = memory.quality_score or 0.0
    cf = memory.confidence or 0.0
    sc = memory.source_coverage or 0

    if qs < mq:
        failures.append(f"quality_score {qs:.2f} < {mq}")
    if cf < mc:
        failures.append(f"confidence {cf:.2f} < {mc}")
    if sc < msc:
        failures.append(f"source_coverage {sc} < {msc}")

    passed = not failures
    rationale = ("All thresholds met." if passed else "; ".join(failures))[:200]

    result = QualityResult(
        passed=passed,
        memory_id=memory_id,
        quality_score=qs,
        confidence=cf,
        source_coverage=sc,
        failures=failures,
        rationale=rationale,
    )

    logger.info(
        "quality_gate: %s %s — %s",
        memory_id[:8],
        "PASS" if passed else "FAIL",
        rationale,
    )
    return result
