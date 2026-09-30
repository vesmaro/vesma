"""Knowledge pipeline for Vesma.

Stages: raw → [cluster] → processing → [synthesize + quality_gate]
        → processed → [publish] → published

ADR-0019 B2a adds the orthogonal async-refinement queue on TOP of that
flow (published rows, ``pipeline_state`` lifecycle):
  pending → [refine] → refined (§6 same-row swap) | failed (lane a,
  retry) | quarantined (lane b, terminal)

Submodules:
  cluster      — M4: group raw entries by embedding similarity
  synthesize   — M4: LLM draft synthesis for a cluster
  quality_gate — M4: score / confidence / source_coverage thresholds
  publish      — M4: status=processed→published + vector upsert;
                 ADR-0019: fail-closed danger gate + pipeline entry
  refine       — ADR-0019 B2a: async refinement of pending rows
                 (claim → artifact → §6 swap / failure lanes)

Key invariant: only status="published" ever enters the vector index.
"""

# Result types live in vesmaro.models (shared between the pipeline package
# and the rest of the app). The pipeline workers import them from there
# too; we re-export at the package level so `from vesmaro.pipeline import
# ClusterResult` works without forcing callers to know the inner layout.
from vesmaro.models import (
    ClusterResult,
    PublishResult,
    QualityResult,
    SynthesisResult,
)

__all__ = [
    "ClusterResult",
    "PublishResult",
    "QualityResult",
    "SynthesisResult",
]
