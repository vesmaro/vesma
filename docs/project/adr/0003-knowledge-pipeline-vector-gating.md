# 0003. Knowledge Pipeline: gate vector indexing on `status="published"`

*Historical artifact — English only.*

- **Status**: Accepted
- **Date**: 2026-05-15
- **Deciders**: abyss, GCW Tech Lead

## Context

ai-brain stored every memory in both SQLite (FTS5) and ChromaDB (vector) regardless of
maturity. The result: vector search returned raw `brain dump` notes interleaved with
finished articles, drowning signal in noise.

Vesma introduces a 4-state machine for knowledge: `raw → processing → processed →
published`. The product question: at which state should a memory enter the vector
index?

## Decision

Vector indexing happens **only at `status="published"`**.

- `raw`: in SQLite only. The watcher writes here on first ingest.
- `processing`: in SQLite only. The clustering worker assigns `cluster_id` here.
- `processed`: in SQLite only. The synthesis worker has produced a draft article.
- `published`: in SQLite **and** ChromaDB. Quality gates have passed; the memory is
  recalled by `mnemos_search` and `mnemos_agent_recall`.

The state machine is enforced by the policy engine (M5): no direct transition
`raw → published` is allowed.

## Consequences

**Positive**

- Search signal-to-noise ratio is high by construction. Raw `brain dump` notes never
  pollute recall.
- Vector index size is bounded: only the curated subset of memories is embedded.
- Cost (embedding compute, storage) is proportional to quality, not ingestion volume.

**Negative**

- A `mnemos_search` immediately after `mnemos_add` returns nothing — the new record is
  `raw` and not in the vector index yet. Operators must wait for the pipeline
  (`cluster → synthesize → publish`) or force-publish.
- The policy engine (M5) needs idempotency keys and a DLQ to handle failed
  synthesis gracefully.

**Neutral**

- The state machine is a runtime concept, not a SQL constraint. We could add a CHECK
  constraint that disallows vector-row insertion with `status != 'published'` but
  that couples storage to policy, which we want to avoid.

## Alternatives considered

- **Index everything, weight by `status` at search time.** Rejected: weights are
  heuristics; downstream LLMs cannot distinguish "low-quality vector hit" from
  "high-quality vector hit" at the raw embedding level.
- **Index only `published` and `processed`.** Rejected: `processed` is a draft; drafts
  leak. We want a single clean cut.
- **Two separate indexes (vector + BM25) with status-aware fusion.** Rejected:
  marginal quality gain, doubled complexity, harder to debug.

## References

- `PLAN.md` §"Phase M4 — Knowledge Pipeline"
- `ARCHITECTURE.md` §1, §2 (Memory.status field)
- `src/vesma/pipeline/cluster.py`, `synthesize.py`, `quality_gate.py`, `publish.py`
- `tests/test_pipeline.py` — 24 tests
