# ADR 0029: Search v2 Query Semantics — Per-Token Prefix AND, Soft Project Fallback, and the Graph Leg

**Status:** Accepted (independent review 2026-09-15: graph-leg gates F1/F2
fixed in 7d9ca4c with mutation-verified regression tests; ADR amendments
F3/F4 landed; short-token follow-up #314 landed — the guard is described
in «Costs and accepted residuals»)
**Deciders:** Tech Lead (slice assignment), Senior System Engineer (implementation)
**Scope:** the FTS5 MATCH expression built from user input (the single
chokepoint), the FTS OR fallback, the project soft-fallback retry, the
1-hop graph expansion along `memory_edges`, the `embedding_id` write
path + backfill, and the provenance fields callers use to see how a
result surfaced. Vector-store ranking, the RRF fusion weights, and the
Snowball/stemming tokenizer are out of scope (stemming noted as a
follow-up in the issue — prefix terms are the zero-migration fix).
**Preconditions:** ADR-0013 (M15.2 FTS5 escaping hardening — preserved
invariant), ADR-0019 (§4 refined-only, §5 absolute quarantine — must
hold on the new paths), A9 (ArchCom 2026-08-27, pre-RRF project
predicate — untouched), ADR-0018 Phase 1 (`memory_edges`, the
`supersedes` edge kind, one-hop-only groundwork), the live probes
against the production DB (issue #313, 2026-09-15)

## Context

Live probes against the production DB (issue #313) proved four defects
in the M15.2-era search stack — measured, not hypothesised:

1. **[CRITICAL] Multi-token queries were adjacency phrases.**
   `_build_fts_query` stripped the FTS5 syntax chars and wrapped the
   WHOLE input in one double-quoted phrase — `"GWS конвейер"` requires
   the two words ADJACENT and IN ORDER inside one document. Live:
   0 hits; the equivalent per-token AND expression: 4 hits.
2. **[HIGH] Prefix/morphology disabled by the same quoting.** The
   trailing `*` a user would need is stripped as a syntax char, so
   inflected RU/EN forms are invisible: `конвейер` → 49 rows vs
   `конвейер*` → 77 (the 28-row delta is every inflected form:
   конвейера, конвейером, ...).
3. **[MEDIUM] `memories.embedding_id` NULL for 1644/1644 rows.** The
   vector leg resolved by memory id and worked; the column was a
   diagnostic trap — every "is this row embedded?" join lied.
4. **[MEDIUM] Project filter hard, no fallback; `memory_edges`
   unused.** Scope drift (`release-pipeline` vs `releases-pipeline`)
   silently zeroed scoped searches; the `supersedes` graph (ADR-0018
   Phase 1 groundwork) contributed nothing to recall.

The M15.2 hardening (ADR-0013) created defect 1 and 2 as a side effect
of doing the right thing: wrapping user input in one quoted phrase is
the canonical FTS5 injection defence, and it also disables prefix
matching and turns multi-token input into an adjacency requirement.
The v2 contract keeps the defence and removes the side effects.

## Decision

**1. The v2 builder — per-token quoted prefix terms (the single
chokepoint unchanged).** `_build_fts_query` remains THE symbol (the
M15.2 security tests pin it); its semantics become:

- tokenize the user input on whitespace; strip the FTS5 syntax chars
  (`* " ' ( ) :`) PER TOKEN (same character class as M15.2);
- emit each surviving token as `"tok"*` — a one-token quoted phrase
  with the prefix operator. A quoted token cannot pivot into operator
  syntax (no NEAR, no column filters, no unbalanced quotes); the `*`
  is appended by the BUILDER, never taken from user input;
- join terms with ` AND `, capped at 8 terms (a longer conjunctive
  expression can only shrink the result set — truncate instead);
- a token containing a hyphen ALSO gets the de-hyphenated head segment
  as an OR-alternative: `("release-trigger"* OR "release"*)`. The
  `unicode61` tokenizer splits on hyphens, so a bare quoted
  `"release-trigger"*` can never match the split index — the head
  segment covers both the split index and the prose spelling;
- empty sanitisation keeps the M15.2 no-match placeholder phrase
  (`""` is an FTS5 syntax error).

Why a quoted prefix term is injection-safe: the double quotes make the
token a literal phrase with no operator parsing INSIDE it; the star sits
outside the quotes and is ours. This was verified against a live FTS5
table with a hostile-input battery (every hostile input builds an
expression that EXECUTES and contains no un-quoted user text).

**2. OR fallback (ranked, logged, single retry).** When the AND
expression matches 0 rows and there is more than one term, `fts_search`
retries ONCE with the same prefix terms joined by ` OR ` — bm25 still
ranks the wider recall set, so this is a ranked rescue, not a flood.
Still 0 → the remaining legs decide (vector, project soft-fallback).
Single-token queries never OR-retry (OR degenerates to the same term).

**3. Morphology via prefix, not stemming.** The per-token `*` IS the
morphology fix for inflected RU/EN forms — zero migration, zero FTS
rebuild. A Snowball/Porter tokenizer would stem at INDEX time and is
deferred (it changes the FTS5 index shape — a rebuild migration); noted
as the follow-up in the issue. Prefix matching is a superset of exact
matching for single tokens, so single-token recall can only grow
(set-theoretically argued and asserted in the golden suite).

**4. `embedding_id` — write-path stamp + idempotent backfill, no
runtime meaning.** `upsert_embedding` (the single embedding write
point) stamps `memories.embedding_id = memory.id` after the vector
write succeeds (the VectorStore keys embeddings BY memory id — the
stamp records "a live vector row exists"). Existing rows are closed by
`vesma backfill-embedding-ids` (dry-run default, `--apply` to write;
idempotent; rows whose vector is gone stay NULL — the column must never
lie). The stamp failure is non-fatal: the column is diagnostics; the
vector leg resolves by id and does not consult it. The backfill was
NOT run against the production DB in this slice — that is the owner's
operational decision.

**5. Project soft-fallback — a NEW OUTER retry, A9 untouched.** A9's
pre-RRF project predicate stays byte-identical. Above it, `search()`
(the orchestrator; the single pass moves to `_search_core`) retries
ONCE WITHOUT the scope when a scoped search returns zero rows — with
the SAME status/`include_raw`/`refined_only` policy, so junk from other
projects' lanes cannot resurface. Rows that surface only via the retry
carry `project_scope_fallback=True` (new backward-compatible
`SearchResult` field) and the event is audited through a NEW counter
`search_stats()["project_scope_fallback_total"]` — distinct from
`cross_project_requests_total` (a fallback is a drift signal, not an
explicit global-mode request). Explicit `status=` drill-downs are NOT
retried: a drill-down asserts the row's lifecycle, and its zero is
information, not a scope-drift candidate.

**6. Graph leg v1 — 1-hop `supersedes` expansion, deterministic decay.**
After RRF fusion, the top-`limit` fused ids expand 1 hop along
`memory_edges` in BOTH directions (a hit surfaces the newer version
that replaced it AND the older sibling it replaced —
`get_incoming_edges` mirrors `get_direct_edges`). Edge-sourced rows not
already fused are appended with the decay rule:

    weight = (1 - alpha) / (rrf_k + 2 * anchor_rank)

where `anchor_rank` is the neighbour's FIRST anchor's 1-based position
in the fused ranking. The `(1-alpha)` factor is the SAME weight the
FTS-fused rows carry, at a strictly deeper rank position — so an
expansion row can never outrank an anchor that itself carries an
FTS-fused contribution (the naive `1/(rrf_k+2*rank)` WOULD outrank a
fused FTS-only anchor at alpha 0.5: 1/62 > 0.5/61 — caught and fixed
by the golden decay test). **Alpha-scope caveat (review F4):** the
never-outranks-its-anchor invariant is asymmetric — it holds for
anchors with an FTS-fused score at any alpha, but a VECTOR-ONLY rank-1
anchor at a caller-passed `hybrid_alpha < 0.5` can be outranked by its
own neighbour (e.g. alpha 0.3: anchor 0.3/61 < neighbour 0.7/62). The
default `alpha = 0.5` (and any value ≥ 0.5) is safe; a below-0.5 alpha
is an explicit caller trade of FTS weight for vector weight, accepted
residual. The expansion is headroom-gated (runs only when the fused
legs left room — `len(results) < limit`, a full fused page needs no
enrichment) and capped at `limit` extra rows (a search at most
doubles), carries `via_graph=True` provenance, and passes the SAME
gates as the fused rows on EVERY axis: the default status policy AND
the explicit `status=` drill-down (review F1 — an edge never widens an
explicit status request), the A9 authoritative project guard for
scoped searches (review F2 — an edge never widens the scope either;
only the soft-fallback retry may, and it tags), ADR-0019 §5 absolute
quarantine (an edge is never a quarantine side door), §4 refined-only.
No edges → the leg is a no-op; an edge-lookup failure is non-fatal.

## Consequences

**What becomes true:**

- Multi-token queries match words in any order and any distance; the
  live CRITICAL case (`"GWS конвейер"` → 0) is structurally closed.
- Inflected RU/EN forms surface without an index migration; the 28-row
  live delta becomes reachable by the un-starred user query.
- Hyphenated identifiers (`release-trigger`,
  `gcw-git-workflow-specialist`) match both the identifier and the
  de-hyphenated prose spellings.
- Scope drift stops silently zeroing searches; the drift is VISIBLE per
  result (`project_scope_fallback`) and per instance (the new counter)
  — an audit signal the owner asked for.
- A superseded record is reachable through its replacement; the
  ADR-0018 Phase 1 groundwork starts paying off in recall.
- `embedding_id` stops lying: born stamped on new writes, closable for
  old rows by one command, never claiming a vector that is gone.
- The M15.2 injection invariant is PRESERVED and now tested at the
  builder level (executes-always, builder-owned stars, builder-only
  scaffolding) — previously only the end-to-end survives-hostile-input
  test held it.

**Costs and accepted residuals:**

- Prefix terms widen recall, and a degenerate short token (`a`, `the`,
  «как») matched nearly every row — its bm25 idf collapsed to 0, and any
  AND query containing one degraded to `LIMIT` rows ordered by id-tiebreak
  noise. CLOSED by the #314 short-token guard (`_fts_guard_tokens`,
  applied between tokenisation and the term cap in `fts_query_terms`):
  1-char tokens are dropped; RU/EN stopwords are dropped via a
  module-level frozenset unless the token is fully uppercase (acronyms —
  IT, QA, DB, CI, ML, GWS, and an AND-as-literal — survive); digit-bearing
  identifiers (`v2`, `x1`, `p0`) survive structurally (≥2 chars, never
  alpha stopwords — no digit-scanning code); if the guard would drop
  EVERY token the original list is kept (the user's explicit degenerate
  query wins — the builder must not introduce silent zeros); and the cap
  applies to the guarded list, so dropped tokens consume no cap budget.
  REMAINING residual: 3+-char non-stopword high-frequency tokens still
  collapse idf — the "correct" fix is the idf-floor query-time guard
  (option 3 in #314), which needs a corpus-statistics read path that
  does not exist today; it stays unbuilt. OR fallback widens further —
  but only after AND proved empty, and once.
- The soft-fallback can return cross-project rows for a scoped request;
  the tag + counter make it explicit, but a caller ignoring
  `project_scope_fallback` sees foreign slugs. Accepted: the
  alternative (silent zero) was the defect.
- The graph leg doubles worst-case result count; the decay rule keeps
  expansion rows strictly below their anchors, but a hub record with
  many superseded siblings can still fill the expansion cap. Accepted
  for v1; a degree-based cut is the natural follow-up if it bites.
- `embedding_id` is still diagnostics only — the vector leg resolves by
  id. Making the column load-bearing (e.g. delete-vector-by-column) is
  a separate decision.
- Snowball stemming remains undone: prefix terms cover the live-probe
  class (common-prefix inflections) but not irregular forms; the
  follow-up in the issue stands.

**Benchmark and harness effects (measured, registered):**

- S1/S1m baselines re-recorded in the same PR per ADR-0020
  (issuance-path change; precedent: the hybrid_alpha re-tune #302).
  Measured gain from v2 semantics: reference recall@5 0.9030 → 0.9366 /
  recall@10 0.9398 → 0.9503; S1m (production embedder) recall@5
  0.9023 → 0.9285. corpus/model fingerprints unchanged — the deltas are
  the semantics, not the embedder.
- S1/S1m baselines re-recorded again for the #314 short-token guard
  (same ADR-0020 procedure): the guard widens ANDs that contained
  stopword tokens, so recall moved up — reference recall@5
  0.9366 → 0.9409 / recall@10 0.9503 → 0.9642 (planted appearances
  202 → 221); S1m recall@5 0.9275 → 0.9484. Fingerprints unchanged —
  the delta is the guard semantics, not the corpus or the embedder.
  Keeping the pre-guard baseline would have lent the corridor upside
  slack that masks a future regression back down to pre-guard noise.
- The SC-S2 `supersede-refind` gone-probe changed form, not meaning:
  the pre-v2 probe ("weekly Mondays cadence" as one whole-input phrase)
  only held because the phrase semantics hid the token "cadence",
  shared by BOTH projections — a probe asserting "row not findable by
  ANY token" cannot prove "old projection not served" under any
  multi-token FTS with a shared token. The v2 probe queries the
  v1-only token ("Mondays"), the single-term shape that never
  triggers the OR fallback; the contract assertion is unchanged.
- Two harness gaps surfaced by the widened result sets (both latent on
  main — the phrase semantics kept result sets so small the recall
  boundary was never contested): the e3 runner's cross-run
  determinism now runs under the seeded id draw of
  `tests/_seeded_ids.py` (the #280 TL-decision harness pattern —
  equal-score groups tie-break by id, so a fresh uuid4 draw per collect
  could flip the boundary row, observed leg A gg-010-pr); the lanes
  flag-off fixture is re-captured through its own registered
  procedure (frozen clock + seeded ids), provenance chain updated.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Escape user syntax chars instead of stripping them (keep `*`, `NEAR` as user operators) | Re-opens the injection surface M15.2 closed: operator semantics from user text are exactly the pivot the hardening exists to prevent. Stripping + builder-owned operators keeps the invariant with no parser. |
| Parse balanced quoted spans as user phrases (AND-joined phrase terms) | Two quoting grammars to keep safe (user quotes + builder quotes); the live probes showed no case needing adjacency, and the golden corpus covers order-free matching. Deferred; the builder's tokeniser treats quotes as separators. |
| OR-first (recall-maximising) join | Drowns precision on every query to save a retry; AND-first with a single OR fallback keeps precision by default and pays one extra MATCH only on empty. |
| Post-RRF project filter (filter the fused list) | Rejected by A9 already — out-of-project rows must never consume RRF rank slots; the soft-fallback composes ABOVE the unchanged A9 predicate instead. |
| Snowball tokenizer now | Changes the FTS5 index shape → a full rebuild migration; prefix terms deliver the measured morphology gap with zero migration. Follow-up stands. |
| Graph expansion scored by edge recency / similarity | v1 keeps ONE deterministic rule (anchor-rank decay) — a pure function of the fused ranking + edge table (same determinism rationale as the #280 cache-contract tiebreak). Richer scoring waits for evidence. |

## References

- Issue #313 — live probes (production DB, 2026-09-15): `"GWS конвейер"`
  0 vs AND 4; `конвейер` 49 vs `конвейер*` 77; `embedding_id` NULL
  1644/1644; scope drift `release-pipeline`/`releases-pipeline`.
- ADR-0013 (M15.2 FTS5 escaping), ADR-0018 (Phase 1 `memory_edges`),
  ADR-0019 (§4/§5 gates), A9 decision (ArchCom 2026-08-27).
- SQLite FTS5 query syntax: https://www.sqlite.org/fts5.html#fts5_strings
  (quoted strings; prefix operator — the v2 term shape).
- Golden regression suite: `tests/test_search_v2_golden.py` (every
  live-probe defect as a semantic assertion).