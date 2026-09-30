# 0004. Context Filter is a mandatory v1 feature (raw + clean dual storage)

*Historical artifact — English only.*

- **Status**: Accepted
- **Date**: 2026-05-15
- **Deciders**: abyss, GCW Tech Lead

## Context

MCP-originated content is noisy: terminal output (progress bars, ANSI escapes, long
stdout), logs (timestamps, stack traces, duplicates), web pages (boilerplate, ads).
Without pre-LLM filtering, Vesma ships that noise to the model, blowing up token cost
and degrading response quality.

The team's instinct: filter aggressively. The risk: filter is destructive — if the
filter is wrong, we lose the original data.

## Decision

The Context Filter is **mandatory in v1**, with two architectural commitments:

1. **Dual storage**: every filtered record stores both `raw_content` (immutable
   original) and `clean_content` (the filtered projection used for model input).
   Drill-down to `raw_content` is always possible via `?include_raw=true` on recall.
2. **5-stage pipeline** with profiles (`log | terminal | code | docs | web | default`):
   `dedup → noise → extract → compress → tokens`. Profiles are user-configurable
   (`~/.mnemos/filter_profiles.yaml`).
3. **Fail-safe**: if any stage throws, `clean_content = raw_content` and a trace
   warning is recorded. The filter never silently drops data.

## Consequences

**Positive**

- Median 50% token reduction on terminal/log inputs (KPI target, measured in
  `docs/architecture.md` § Context Filter).
- Drill-down rate ≤ 1% (target): if users need raw often, the filter profile is too
  aggressive and we tune it.
- 0% data loss guarantee: `raw_content` is never deleted by the filter.

**Negative**

- Every record carries ~2× the storage cost (raw + clean). For 10k memories at
  ~5KB average, this is ~100MB extra. Acceptable; SQLite WAL handles it.
- Filter pipeline version is per-record (`filter_version`). Re-running a record
  through a new filter version is a manual `vesma filter reprocess --id ...`.

**Neutral**

- The filter does not call an LLM in v1 (purely extractive, regex-based). An LLM
  reranker is a v2 feature (M11-adjacent, deferred).

## Alternatives considered

- **Optional filter, opt-in per record.** Rejected: most callers will forget to
  opt in; signal-to-noise regresses.
- **Filter at recall time, not at ingest.** Rejected: re-running the filter on every
  search call is expensive; pre-filtered `clean_content` is cheaper.
- **LLM-based filter.** Rejected: cost (latency + tokens) is too high for hot path.
  An LLM reranker on top of the extractive filter is a v2 enhancement.

## References

- `PLAN.md` §"Phase M10 — Context Filter (mandatory v1)"
- `ARCHITECTURE.md` §1 (Context Filter layer)
- `src/vesma/filter/` — `dedup.py`, `noise.py`, `extract.py`, `compress.py`, `tokens.py`
- `tests/test_context_filter.py` — 32 tests
- `docs/architecture.md` § Context Filter (KPI targets)
