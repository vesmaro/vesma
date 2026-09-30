# Feature Map

**🌐 Language / Язык:** English · [Русский](../ru/features.md)

> Vesma is a memory layer for AI agents: connect it once, and the harness gets
> long-term memory — structured, searchable, protected — that survives sessions,
> restarts, and context compression.

One page, three honest sections: what works out of the box, what is partial,
and what is planned. Statuses reflect the v4.0.0 codebase (owner-approved map
2026-08-31, updated 2026-09-05: packaging shipped, benchmark stands landed).

---

## Works out of the box

Connect — and it is there. No extra wiring required for anything in this table.

| Capability | What you get |
|------------|--------------|
| **Universal connectivity** | MCP server (38 tools, stdio) + REST API — any harness with MCP support connects in one line. Details: [mcp-tools.md](user/mcp-tools.md), [http-api.md](user/http-api.md) |
| **Ready integrations** | zcode, the `~/.agents` standard (Claude Code, Codex, Continue, Qwen, and others), and pi — via `vesma integration`: universal deploy targets, one-line MCP presets, and a multi-harness doctor (`vesma doctor` checks MCP registration across known harnesses). See the [integration guide](user/integration-guide.md) |
| **Skill pack** | 14+ memory skills deployed into your harnesses alongside the tools |
| **Flexible memory** | Hybrid search (full-text + vector, rank fusion), the [tag contract](user/tag-contract.md), memory scoped per agent and per project, a [context filter](user/context-filter.md) with content-aware filter profiles (code / docs / web / logs …), and CCR compression — marker in context, original in memory, 70–90% token savings |
| **Dynamic context assembly** | `assemble_context`: a search → compression → filter → secret scan → cache alignment → token budget pipeline, with provenance on every block |
| **Harness context bridge** | `on_context_rewrite`: when the harness compacts its history, the original is preserved losslessly and available on demand ([ADR-0018](../project/adr/0018-context-rewrite-ltm-bridge.md)) |
| **Lifecycle hooks** | `pre_llm_call` (context injection before the model call), `on_session_start`, `post_tool_call` (auto-compression of tool outputs) |
| **Publication model v3.0.0** | An entry is visible immediately after save; background refinement swaps in the refined version seamlessly; dangerous content is quarantined with a neutral retraction ([ADR-0019](../project/adr/0019-optimistic-publication-async-refinement.md)) |
| **Self-protection** | Injection and secret detectors on input and publication; every output is scanned; a full audit trail tied to each entry |
| **Auto-pipeline** | A background processor: clustering, deduplication, quality gate, publication |
| **Bundled embedding model** | `mnema-embed-v1` (~30 MB int8 ONNX) ships inside the wheel — hybrid vector search works fully offline, on CPU, with no downloads and no API keys |
| **Packaging & delivery** | PyPI [`vesma`](https://pypi.org/project/vesma/) — pre-rebrand wheel `mnemos-memory-server` stays live until deprecation; (wheel bundles the integration pack and the model), npm `pi-vesma` + aliases, GHCR image `ghcr.io/vesmaro/vesmaro`, one-line installer script, benchmark framework S1–S4 in-repo |

## Partial — core exists, completeness in progress

| Area | Status |
|------|--------|
| **Autonomy (~90%)** | Storage, search, context assembly, and protection run on their own. Hook discipline in an arbitrary harness comes from the deployed instructions and skills (soft automation), not hard wiring. Hard wiring exists in the Hermes adapter and partially in zcode; a generic hook mechanism for any harness is planned |
| **Enrichment by an LLM ("brain")** | The pipeline runs; clustering, deduplication, and the quality gate are real. Qualitative text enrichment is currently a deterministic stub; an LLM provider plugs into the reserved interface point (planned) |

## Planned

| Feature | Scope |
|---------|-------|
| **Memory graph** | Links between entries, citation cascades ([ADR-0017](../project/adr/0017-memory-system-evolution-roadmap.md)) |
| **Cross-device federation** | A persistent exchange channel between devices; today the exchange is batch, via files ([ADR-0017](../project/adr/0017-memory-system-evolution-roadmap.md)) |
| **Multi-principal** | Memory of several owners with isolation |
| **Meta-level: retrieval lanes** | Deterministic lanes (rules / decisions / knowledge) as a recall sub-stage of `assemble_context` — rolls out only if a pre-registered A/B/B0 experiment beats the trivial baseline ([ADR-0025](../project/adr/0025-memory-meta-level-lanes.md)) |
| **Collapse cascade — "collapse with checks"** | Session → project → cross-project synthesis over clusters: automatic by default, never unconditional in authority — derivatives are born without `applyTo:`/`severity:`, nothing pins without an operator, every result traceable to its sources ([ADR-0025](../project/adr/0025-memory-meta-level-lanes.md)) |
| **Awareness — presence & delta** | What each agent is working on and what changed since your last look, with conflict hints — built from server-observed facts; a nervous system, not a conductor ([ADR-0025](../project/adr/0025-memory-meta-level-lanes.md)) |
| **Operational picture (swarm v0a/v0b)** | Same-project peers as one line each — agent id, last activity, record count in the window, checkpoint presence; counts/ids/timestamps only, strictly project-scoped, rate-capped, never pinnable. Swarm v0b adds each peer's CLAIMED task (`task:<slug>`, ADR-0027) — a self-reported label: issuance-scanned fail-closed, rendered in a labeled `[unverified]` sub-section, never in the observed facts or blocks |

Roadmap invariant for the meta-level track: **zero silent losses** — a fact is either retained, or
its loss is visible in a report; each rung above opens only after measurements confirm it.

---

## Further reading

| Topic | Source |
|-------|--------|
| Provider contract, retrieval pipeline, memory graph, distribution | [ADR-0017](../project/adr/0017-memory-system-evolution-roadmap.md) |
| Context rewrite and the LTM bridge (`on_context_rewrite`) | [ADR-0018](../project/adr/0018-context-rewrite-ltm-bridge.md) |
| Publication model v3.0.0 (optimistic publication, async refinement) | [ADR-0019](../project/adr/0019-optimistic-publication-async-refinement.md) |
| Benchmark framework (S1–S4) | [ADR-0020](../project/adr/0020-benchmark-framework.md) |
| Wiring a specific harness | [integration-guide.md](user/integration-guide.md) |
| All ADRs | [adr/](../project/adr/README.md) |

---

_Source: owner-approved feature map (2026-08-31), cross-checked against the
v4.0.0 codebase — 26 tools registered at the time, skill pack
in `integrations/skills/`, pipeline stages in `src/vesmaro/pipeline/`,
benchmark stands in `benchmarks/`. Updated 2026-09-05; meta-level roadmap rows
added 2026-09-13 per ADR-0025; tool count refreshed 2026-09-28 — 38 tools in
`src/vesmaro/mcp_server.py` (PG-0 project graph, ADR-0032, on by default)._

_Last updated: 2026-09-28_
