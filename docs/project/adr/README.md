# Architecture Decision Records

*Historical artifact — English only.*

This directory contains the architectural decisions for Vesma, recorded as
ADRs (Architecture Decision Records) following the
[Michael Nygard lightweight template](https://github.com/joelparkerhenderson/architecture_decision_records).

## When to write an ADR

- Choosing among multiple viable options for a structural decision.
- Changing a previously-recorded decision (write a new ADR that **supersedes**
  the old; do not edit history).
- The decision will affect future teammates who didn't see the discussion.

Do **not** write an ADR for trivial choices, project conventions already
documented elsewhere, or anything that fits in a code comment.

## Status legend

- **Proposed**: under discussion, no consensus yet.
- **Accepted**: in force on `main`.
- **Accepted (with conditions)**: in force, but bound to explicit conditions
  (e.g. a pre-registered experiment or an owner green-light must precede a
  named phase).
- **Deprecated**: still on disk, but no longer guiding current decisions.
- **Superseded by ADR-NNNN**: replaced by a later decision; kept for context.

## Index

| ADR | Title | Status | Date |
|---|---|---|---|
| [0001](0001-fork-from-ai-brain.md) | Fork ai-brain into a standalone Vesma product | Accepted | 2026-05-15 |
| [0002](0002-gcw-tag-contract-strict-by-default.md) | Vesma tag contract is strict by default at the MCP layer | Accepted | 2026-05-15 |
| [0003](0003-knowledge-pipeline-vector-gating.md) | Knowledge Pipeline: gate vector indexing on `status="published"` | Accepted | 2026-05-15 |
| [0004](0004-context-filter-mandatory-v1.md) | Context Filter is a mandatory v1 feature (raw + clean dual storage) | Accepted | 2026-05-15 |
| [0005](0005-cache-center-deferred-to-v2.md) | Cache Center is deferred to v2 (idempotency from M5 covers v1) | Accepted | 2026-05-15 |
| [0006](0006-local-onnx-embeddings.md) | Use local ONNX embeddings (privacy + offline by default) | Accepted | 2026-05-15 |
| [0007](0007-a2a-sessions-api-v1.md) | A2A Sessions API: 5 minimal endpoints with summary-mode default | Accepted | 2026-06-15 |
| [0008](0008-sql-injection-via-fstring.md) | SQL injection via f-string is fixed via whitelisted dispatch (M15) | Accepted | 2026-06-15 |
| [0009](0009-ssrf-guard-in-ingest-url.md) | SSRF guard at the `mnemos_ingest_url` boundary | Accepted | 2026-06-15 |
| [0010](0010-fallback-isolation-from-gcw.md) | GCW A2A is a "best-effort, not a hard dependency" backend | Accepted | 2026-06-15 |
| [0011](0011-mypy-strict-is-real-strict.md) | `mypy --strict` is the production-readiness gate, not a check-the-box lint | Accepted | 2026-06-15 |
| [0012](0012-ipv6-ssrf-gap.md) | Fix IPv6 SSRF gap in `_validate_url` (M15) | Accepted | 2026-06-15 |
| [0013](0013-production-hardening-m15.md) | M15 production hardening is the gate to declaring v1 "done" | Accepted | 2026-06-15 |
| [0014](0014-api-auth-threat-model.md) | API authentication & TOTP 2FA — threat model | Accepted | 2026-06-17 |
| [0016](0016-federation-threat-model.md) | Federation threat model | Accepted | 2026-07-21 |
| [0017](0017-memory-system-evolution-roadmap.md) | Memory System Evolution Roadmap | Accepted | 2026-08-21 |
| [0018](0018-context-rewrite-ltm-bridge.md) | Context Rewrite and LTM Bridge | Accepted | 2026-08-22 |
| [0019](0019-optimistic-publication-async-refinement.md) | Optimistic Publication with Async Refinement | Accepted | 2026-08-29 |
| [0020](0020-benchmark-framework.md) | Memory Benchmark Framework | Accepted | 2026-08-30 |
| [0021](0021-nano-model-track.md) | Nano-Model Track (bundled embedder and refiner) | Accepted | 2026-08-31 |
| [0022](0022-licensing-foundation.md) | Licensing Foundation (Apache-2.0 core, open-core monetization, FSL triggers) | Accepted | 2026-09-03 |
| [0023](0023-mcp-core-dependency.md) | MCP SDK moves into core dependencies (`mcp>=2.0,<3.0`) | Accepted | 2026-09-06 |
| [0024](0024-unified-harness-connection.md) | Unified harness connection via `vesma connect` over a harness-profile registry | Accepted | 2026-09-06 |
| [0025](0025-memory-meta-level-lanes.md) | Memory meta-level: retrieval lanes and area manifests (experiment precedes implementation; brain metaphor = narrative only) | Lanes leg FALSIFIED (ratified 2026-09-14); acceptance superseded for the lanes leg | 2026-09-08 |
| [0026](0026-memory-value-observability.md) | Memory-value observability: passive metrics sidecar + S5 replay stand + F8 metric family | Accepted (with conditions) | 2026-09-09 |
| [0027](0027-multi-context-memory.md) | Multi-context memory: compose existing context levels now; task primitive behind a pre-registered experiment gate | Accepted; Phase-2 decision appended 2026-09-28 (F1 PASS + owner arbitration — addendum §A) | 2026-09-14 |
| [0028](0028-cache-contract.md) | Cache contract: prefix-stable content supply, tail discipline, and the harness boundary | Accepted | 2026-09-14 |
| [0029](0029-search-v2-query-semantics.md) | Search v2 query semantics: per-token prefix AND, soft project fallback, graph leg, embedding_id stamp | Accepted | 2026-09-15 |
| [0030](0030-memory-graph-self-fueling.md) | Memory Graph — the self-fueling graph (search graph line continuation) | Accepted; A0-review appended 2026-09-28 (gate re-baseline edges/post-A0-write ≥ 0.5 + S1/S2 — addendum §B) | 2026-09-15 |
| [0031](0031-rebrand-mnemos-to-vesmaro.md) | Rebrand — mnemos → vesmaro (additive dual-prefix; legacy retires in 6.0) | Accepted | 2026-09-15 |
| [0032](0032-project-graph.md) | Project graph as memory — sidecar `code_graph.db`, memory-first, waves PG-0..PG-3 behind gates | Accepted (with conditions — owner green-light per wave) | 2026-09-28 |
| [0033](0033-vesma-harness-layer.md) | Harness memory layer — owned by vesma, delivered via `vesma integration` | Accepted (conditional — two security-major merge gates) | 2026-10-01 |
| [0034](0034-memory-engine-switch-protocol.md) | Memory-engine switch protocol — open vendor-neutral spec, reference implementation in vesma | Accepted (conditional — spec extraction gated by adoption; B1–B4 blocking controls) | 2026-10-01 |

## Themes

**Process / fork** (0001): how Vesma came to be.

**Contract / schema** (0002): how Vesma and GCW agree on data shape.

**Pipeline / data flow** (0003, 0004, 0005, 0006): how knowledge moves through
Vesma — vector gating, context filter, cache deferral, embedding choice.

**External surface** (0007, 0009, 0010, 0012): what Vesma exposes to GCW
(A2A API), to URL ingestion (SSRF guard), and how it tolerates being down
(fallback isolation). 0012 is a tightening of 0009 discovered during M15.

**Quality / hardening** (0008, 0011, 0013): how Vesma earns "production-ready"
— SQL safety, type strictness, M15 as a gate.

## Conventions

- Filenames: `NNNN-short-kebab-case-title.md`, zero-padded.
- Status changes are appended, not in-place mutations.
- "Accepted" requires the deciders listed; consensus or named owner.
- An ADR is only as good as its **Alternatives considered** section — write it.

## Cross-references

- `tasks/AUDIT.md` — current state of the codebase (input to many ADRs).
- `tasks/tech-lead/TL-001-coordination.md` — coordination across the work that
  produced several of these decisions.
- `docs/en/architecture/overview.md` — system architecture; ADRs explain the *why*, this
  document explains the *what*.
- `docs/en/admin/security.md` — threat model; ADR-0009 and 0012 are the SSRF decisions.
