# 0005. Cache Center is deferred to v2 (idempotency from M5 covers v1)

*Historical artifact — English only.*

- **Status**: Accepted (decision: defer)
- **Date**: 2026-05-15
- **Deciders**: abyss, GCW Tech Lead

## Context

The original `ai-brain/PLAN.md` sketched a "Cache Center" — a generic LLM-call cache
that memoises synthesis prompts, embeddings, and search results across sessions. The
motivation: LLM calls are expensive and often redundant (the same cluster synthesised
twice should return the same draft).

The team considered implementing Cache Center as a v1 feature. The decision: **defer
to v2**.

## Decision

Cache Center is **not** a v1 feature. Vesma v1 ships **without** a generic LLM call
cache. The v1-equivalent benefit is achieved by **per-pipeline idempotency keys** in
M5 (policy engine):

- Idempotency key = `hash(cluster_id, prompt_version, model_version)`.
- Repeated synthesis of the same cluster with the same prompt returns the cached
  result.
- The cache is bounded to the synthesis step — not a general LLM cache.

## Consequences

**Positive**

- Smaller v1 surface area — one less subsystem to test, document, secure.
- The synthesis idempotency covers ~80% of the practical benefit (synthesis is the
  hot LLM path; recall and search are not).
- v2 Cache Center can be added **alongside** the idempotency layer without breaking
  v1 callers.

**Negative**

- Repeated `mnemos_search` calls in the same session re-compute embeddings each time.
  Mitigated by `_TTLCache` in `sqlite_store.py` for the FTS5 and COUNT queries.
- A user who explicitly wants to memoise LLM calls across clusters must wait for v2.

**Neutral**

- The `cache_hit: bool` field in the `traces` table is reserved for v2 — it is
  always `false` in v1 but the schema is forward-compatible.

## Alternatives considered

- **Implement Cache Center in v1.** Rejected: scope creep; M5 idempotency covers the
  synthesis hot path; remaining call sites are not on the critical latency path.
- **Skip synthesis idempotency, ship full Cache Center.** Rejected: Cache Center
  is a much larger subsystem; deferring both means cutting the entire feature from
  v1, which is not what we want — synthesis idempotency is small and isolated.

## References

- `PLAN.md` §"Phase M11 — Cache Center (DEFERRED to v2)"
- `ai-brain/docs/knowledge-pipeline-concept.md` (origin of the Cache Center sketch)
- `src/vesma/policy/` — idempotency implementation in M5
- `CHANGELOG.md` 0.1.0 — M11 marked `⏳ v2`
