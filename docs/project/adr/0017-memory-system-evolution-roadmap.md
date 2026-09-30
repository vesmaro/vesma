# ADR 0017: Memory System Evolution Roadmap

**Status:** Accepted (Architectural Committee, 2026-08-21)
**Deciders:** Tech Lead (chair), Product Architect, Analytics Lead, Senior Security Engineer, Senior System Engineer
**Scope:** provider contract, retrieval pipeline, memory graph, storage strategy, distribution

## Context

Vesma is a single-tenant, local-first memory server exposing three equivalent
surfaces (CLI, HTTP API, MCP) over one core (`MemoryManager`): hybrid recall
(FTS5 + vector, RRF fusion), a status-driven knowledge pipeline
(`raw → processing → processed → published`) with quality gates and DLQ, a
context filter, reversible compression (CCR), prefix cache alignment
(CacheAligner), compaction detection, path-scoped rules, an Obsidian vault
mirror, and multi-node federation.

Five engineering gaps constrain the system's next stage of growth:

1. **Context delivery is adapter-private.** The only automated pre-LLM context
   assembly lives inside a single host adapter (Hermes). Any other harness gets
   memory *tools* but no standardized way to obtain a relevance-assembled
   context block before a model call. There is no provider contract.
2. **Retrieval is flat.** Memories are independent rows: no typed relations
   between entries (supersedes / contradicts / relates), no expansion from
   seed results through related entries, no relevance feedback from consumers,
   and no signal about contexts where retrieval found nothing relevant.
   Injected-context quality is not measured.
3. **Distribution is manual.** No published package, no platform presets,
   no integration template; first successful use requires installing from
   source and editing configuration.
4. **Vector storage is uncompressed float32 with row-wise similarity** —
   storage and CPU grow linearly and cheaply avoidable.
5. **No published quality benchmarks** exist for recall quality, so
   improvements cannot be demonstrated.

## Decision

Adopt a phased evolution roadmap. Six decisions define it:

### D1. Provider contract `assemble_context`

One API assembles the model-facing context block:

```
assemble_context(session, project, file?, budget, mode) -> ContextBlock
```

Fixed pipeline, in order:

1. **Recall** — hybrid RRF (FTS5 + vector), per-agent / applyTo boosts.
2. **Graph expansion** (from Phase 2) — bounded traversal over memory edges.
3. **Context filter** — mandatory noise/secret reduction stage.
4. **Secret scan** — mandatory; no credential-shaped content is ever injected.
5. **CacheAligner** — relocate dynamic content to the block tail so provider
   KV caches keep hitting.
6. **Token budget** — assemble the block under the caller's budget.

Lifecycle integration: `pre_llm_call` (inject block), `on_session_start`
(session state), `post_tool_call` (capture results as memories, opt-in).
Modes: `sync` (default, blocking before the call) and `async` (result
delivered on the next turn, for latency-sensitive harnesses).

Wire protocol: **MCP** (stdio/SSE). No proprietary protocol is introduced;
the HTTP API remains the full server surface. A thin SDK facade
(`remember / recall / forget / stats / assemble_context`) and a published
~100-line integration template (Connect / Expose / Configure + acceptance
checklist) complete the provider surface.

Stages 3–4 are not optional (security requirement): assembled blocks enter
prompts and must never carry noise or secrets. Injected entries carry
provenance.

### D2. Memory graph and learning loop

Additive edge layer over existing rows:

- `memory_edges(src_id, dst_id, kind, weight, created_at, provenance)` with
  `kind ∈ {relates, supersedes, contradicts, derived}`; indexed adjacency.
- **Graph expansion** in recall: seeds = top-K of RRF; traversal depth ≤ 2,
  node-visit cap and latency cap per query; configuration off-switch;
  degradation = skip the stage.
- **Feedback loop:** authenticated, rate-limited endpoint
  `POST /context/feedback {injection_id, used[], rejected[]}`; used entries
  gain confidence (+0.05) and strength (+1); rejected lose confidence (−0.1;
  background maintenance −0.02).
- **Confidence decay** by subtype half-life: long-lived kinds (rules,
  decisions) decay slowly (≈365d); volatile kinds (checkpoints, session
  notes) decay fast (7–30d); formula
  `initial × e^(−age/half_life) × (1 + 0.1·log(access+1)) × trust`.
- **Gap detection:** contexts yielding no relevant entries are logged
  (embedding + snippet, no credentials) and become extraction candidates for
  the knowledge pipeline.
- Edge sources: co-relevant entries observed post-retrieval (background),
  duplicate/contradiction handling at write time (Phase 3), pipeline
  cluster derivation.

Destructive consolidation (merging, pruning) is **out of scope** until a
deferred phase, gated by policy rules.

### D3. Storage optimizations

- Embedding compression: int8 (default) / bit modes above float32, with an
  automatic degradation ladder ending in lexical-only recall when no
  embedding provider is available.
- Vectorized batch similarity: matrix cosine (SIMD) instead of row-wise.
- Tiered compression of aged entries (compress / summarize beyond an age
  threshold), compatible with CCR retrieval of originals.

### D4. Columnar OLAP as primary store — rejected

A columnar analytical engine as the memory's primary store inverts the
workload profile: hot-path operations are top-K retrieval (served by
inverted and vector indexes, both immature in such engines) and frequent
small updates with status transitions (such engines optimize append-only);
strict read-your-writes semantics are required by the tag contract and
policy engine; the payoff zone of columnar layout begins orders of magnitude
above realistic single-tenant memory volumes; and graph traversal — random
point reads without locality — is the worst case for columnar layout.
An **analytics plane** (embedded columnar engine for traces, retrieval
metrics, token accounting) is deferred with explicit triggers (see phases).

### D5. Metric gates on every phase

A golden evaluation set and metrics (precision@k, recall@k,
injection-acceptance rate from the feedback loop) are built in Phase 1
**before** any retrieval change. Each phase exits only with: no regression
against the recorded baseline and a measured improvement on that phase's
target axis.

### D6. Zero-config profile is loopback-only

The no-configuration startup path binds to loopback exclusively. The
existing startup guard (auth + TOTP + TLS required for non-loopback binds)
extends to every packaged surface unchanged.

## Phases

| Phase | Scope | Size | Exit gate |
|---|---|---|---|
| 0 — Distribution | Published package + CI wheels; zero-config loopback profile; MCP presets for major harnesses; published integration template | S | Install → serve on loopback → first add/search in < 5 min, zero config edits |
| 1 — Provider contract | `assemble_context` (stages 1, 3–6); lifecycle hooks; SDK facade; Hermes adapter migration; golden set + baseline metrics | M | Hermes e2e on contract; baseline recorded; security review of injection path passed |
| 2 — Graph + learning loop | `memory_edges`; bounded graph expansion (stage 2); feedback endpoint; gap detection → pipeline | M | Metrics ≥ baseline; acceptance rate measured; caps verified under load |
| 3 — Retrieval intelligence | Optional LLM verification (token-budgeted, off by default); confidence decay; write-time contradiction handling | M | Verification measured on golden set; decay tuned; contradictions logged losslessly |
| 4 — Storage | int8/bit compression; vectorized similarity; tiered compression | S–M | Storage reduction measured; recall regression ≤ 1% |

Adapter waves: wave 1 (in Phase 1) migrates the existing Hermes adapter and
ships MCP presets; wave 2 (native adapters for additional harnesses) starts
only after the Phase 1 gate.

Deferred with revisit triggers: embedded-analytics plane (trigger: event
volume > 10M rows or analytics load affecting the hot path); full-graph
consolidation (trigger: post-Phase-3 soak plus justified contradiction
volume); temporal fact store (trigger: structured-fact demand from graph
data); public benchmark publication (trigger: Phase 2 stable).

## Alternatives considered

- **Columnar OLAP primary store** — rejected (D4): workload inversion across
  query type, update pattern, consistency needs, scale zone, and graph
  traversal; its *principles* are adopted instead (D3).
- **Proprietary provider wire protocol** — rejected: MCP is the de facto
  standard for tool-facing integration; a second protocol adds cost without
  network effect.
- **Monolithic rewrite** — rejected: phased evolution keeps every working
  surface (federation, pipeline, filter, auth) shippable throughout.
- **Pivot to embedded single-file library** — rejected: abandons the
  multi-client server value (REST, UI, CI, federation, tenancy posture)
  that differentiates the system.

## Consequences

- **Positive:** any MCP-capable harness gains pre-LLM context assembly;
  retrieval quality becomes measurable and self-improving (feedback-driven
  confidence); distribution reaches a five-minute first-run; storage costs
  drop with bounded quality risk.
- **Negative / costs:** new authenticated surface (feedback endpoint) to
  maintain; graph expansion adds a capped latency stage; optional
  verification introduces configurable LLM cost; packaging adds release
  engineering duties.
- **Risk mitigations:** injection path hardening is mandatory in the
  contract (filter + secret scan + provenance); every phase is gated on
  metrics against a pre-recorded baseline; graph expansion is bounded and
  switchable; the zero-config path cannot weaken the auth posture.

## References

- Architectural Committee decision record: mnemos entry
  `403d3949-143d-4481-83e9-7da3357c7d35` (tags: `committee`, `mnemos:decision`).
- Session protocol and full architectural contract are archived with the
  committee records (team-local, not part of this repository).
