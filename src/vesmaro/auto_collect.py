"""Compaction detection signals for Vesma. (M7)

Weighted signals for auto-checkpoint triggers:
  1. call_counter     — N tool calls since last checkpoint
  2. context_size     — client-reported token estimate > 80% of model limit
  3. summary_marker   — regex on recent messages for <conversation-summary>/<compacted>
  4. reference_drop   — agent stops citing earlier identifiers in last N calls

Configuration: ~/.mnemos/auto_collect.yaml
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Regex patterns for compaction marker detection (M7)
_COMPACTION_MARKERS = re.compile(
    r"<conversation[\-_]summary>|<compacted>|<context[\-_]compressed>",
    re.IGNORECASE,
)


@dataclass
class CompactionSignals:
    """Current state of all compaction-detection signals."""

    # Signal 1: call counter
    calls_since_save: int = 0
    call_threshold: int = 6

    # Signal 2: context-size heuristic (populated by MCP client)
    context_tokens: int | None = None
    context_limit: int | None = None  # model's max context

    # Signal 3: summary-marker detection (populated by MCP client)
    summary_marker_detected: bool = False

    # Signal 4: reference-drop heuristic (populated by MCP client)
    reference_drop_detected: bool = False

    # Per-signal weights for composite score
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "call_counter": 0.4,
            "context_size": 0.3,
            "summary_marker": 0.2,
            "reference_drop": 0.1,
        }
    )

    @property
    def call_counter_triggered(self) -> bool:
        return self.calls_since_save >= self.call_threshold

    @property
    def context_size_triggered(self) -> bool:
        if self.context_tokens is None or self.context_limit is None:
            return False
        return self.context_tokens / self.context_limit >= 0.80

    @property
    def composite_score(self) -> float:
        """Weighted composite compaction risk score [0.0 - 1.0]."""
        signals = {
            "call_counter": 1.0 if self.call_counter_triggered else 0.0,
            "context_size": 1.0 if self.context_size_triggered else 0.0,
            "summary_marker": 1.0 if self.summary_marker_detected else 0.0,
            "reference_drop": 1.0 if self.reference_drop_detected else 0.0,
        }
        return sum(self.weights[k] * v for k, v in signals.items())

    @property
    def recommendation(self) -> str:
        if self.composite_score >= 0.4 or self.summary_marker_detected:
            return "save_checkpoint"
        return "ok"


def detect_summary_marker(text: str) -> bool:
    """Return True if the text contains a known compaction marker."""
    return bool(_COMPACTION_MARKERS.search(text))
