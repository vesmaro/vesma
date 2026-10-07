# Vesma Documentation (English)

**🌐 Language / Язык:** English · [Русский](../ru/index.md)

> Vesma is a standalone memory & knowledge server for AI agents. It gives every agent real long-term memory — structured, searchable, governed by a strict tag contract — that persists across sessions, restarts, and context compression. One local server, three control surfaces (MCP / CLI / HTTP), and any MCP-capable harness connects in one line.

The codebase becomes memory too: the **[project graph](user/project-graph.md)** (ADR-0032, on by default) turns a registered project into a searchable symbol map — file outlines, ranked symbol search, call tracing, secret-scanned line snippets — with zero source bytes stored on the server.

---

## Install (one command)

Vesma is on **PyPI** (bare slot, ours as of the rebrand):

```bash
pip install vesma
```

The default embedding model (`vesma-embed-v1`, ~30 MB) is bundled in the wheel — search works fully offline, on CPU, no API keys, nothing downloaded. Isolated variant: `uv tool install vesma` or `pipx install vesma`.

> ⚠️ **Names.** The PyPI package is **`vesma`** (the bare slot has been ours since the rebrand — the primary channel). The pre-rebrand package `mnemos-memory-server` stays live until deprecation (`pip install mnemos-memory-server` installs the same server).

npm (pi extension): `pi-vesma` · `vesma-pi` · `@korrlabs/vesmapi` · `@korrlabs/vesma-pi`. Container: `ghcr.io/vesmaro/vesma`.

---

## Connect your harness

**MCP is the primary integration surface.** Any MCP-capable harness talks to Vesma over the same stdio wire. **Manual MCP registration is cancelled — the utility is the only path.** Copy-paste fallbacks for non-standard harnesses live on one page: **[Connect Vesma to any harness](../../integrations/mcp-presets.md)**.

| Harness | Fastest path |
|---------|--------------|
| VS Code Copilot | `vesma integration setup --target copilot` |
| Claude Code | `vesma integration setup --target claude-code` |
| Cursor | `vesma integration setup --target cursor` |
| Codex | `vesma integration setup --target codex` |
| Windsurf | `vesma integration setup --target windsurf` |
| ZCode / pi | `vesma integration setup --target zcode` / `--target pi` |
| Hermes Agent | native in-process plugin — `vesma integration setup --target hermes` |
| OpenCode | one block into `~/.config/opencode/opencode.json` (no native target — [preset](../../integrations/mcp-presets.md#opencode)) |
| Anything else | [adapter-template.md](../../integrations/adapter-template.md) — Connect / Expose / Configure |

`vesma integration setup` without flags deploys everything at once — and also deploys the **behavioral layer** (memory instructions, 14+ skills, prompt mode, agent wiring) so agents *know when and how* to use memory:

```bash
vesma integration setup
```

Targets, flags, and the full deploy map: [integration-guide.md](user/integration-guide.md).

### MCP server properties

| Property | Value |
|----------|-------|
| Protocol | MCP over **stdio JSON-RPC 2.0** |
| Server name | `vesma` (registry key in harness configs: `vesma` — dual-prefix contract) |
| Transport | stdio — no TCP port |
| Tool prefix | `vesma_` |
| Start it | `vesma mcp-server` |

### Auto-collect mode

Set `VESMA_AUTO_COLLECT=1` in the server's `env` block to make Vesma prompt your agent to call `vesma_save_context` after every ~6 tool calls (proactive checkpoint nagging). See [mcp-tools.md#auto-collect-mode](user/mcp-tools.md#auto-collect-mode) for trade-offs.

### 38 MCP tools (`vesma_` prefix)

| Tool | Purpose |
|------|---------|
| `vesma_search` | Hybrid FTS5 + vector search with Reciprocal Rank Fusion (published memories by default) |
| `vesma_add` | Create a memory — **enforces the Vesma tag contract** |
| `vesma_filter` | Run or refresh the context filter on an existing memory (e.g. with another profile) |
| `vesma_agent_recall` | Per-agent recall (M3) — most recent entries for a single agent |
| `vesma_save_context` | Persist a session checkpoint |
| `vesma_recall_context` | Restore the latest checkpoint for a project |
| `vesma_list_recent` | List the most recent memory entries |
| `vesma_list_tags` | List all tags with their counts |
| `vesma_tags_rename` | Bulk rename a tag prefix across existing memories (dry-run by default) |
| `vesma_tags` | Bulk tag operations: rename a prefix, remove or add tags |
| `vesma_ingest_url` | Fetch a web page and save it as a memory |
| `vesma_ingest_document` | Ingest a document as chunked, born-quarantined rows (ADR-0027 Ф3) |
| `vesma_watch_start` | Register the project-graph watch poll (ADR-0032 §3.2; graph flags on by default since 2026-09-28) |
| `vesma_watch_stop` | Stop one or all watch registrations |
| `vesma_watch_status` | Report watch registrations and last poll outcome |
| `vesma_index_project` | Index a registered project root into the project graph (ADR-0032, on by default) |
| `vesma_project_graph_status` | Project-graph volumes, freshness, parse failures, poisoned count |
| `vesma_search_graph` | Ranked search over the graph with the token contract |
| `vesma_trace_path` | BFS over project edges from one symbol (depth ≤ 2) |
| `vesma_get_file_outline` | Symbol outline of one indexed file (shapes, never bodies) |
| `vesma_get_code_snippet` | Secret-scanned line range read from disk (PG4) |
| `vesma_check_graph_coverage` | Per-path coverage verdicts: indexed / stale / parse-error / unindexed / poisoned |
| `vesma_get_graph_schema` | The graph contract card for agents |
| `vesma_list_graph_projects` | Registered projects joined with index status |
| `vesma_delete_graph_project` | Drop the graph index (sidecar only); clears the poisoned set |
| `vesma_auto_collect_status` | Compaction-detection signal vector (M7) |
| `vesma_stats` | Health counters and key paths |
| `vesma_reprocess` | Manually run the knowledge pipeline over queued entries |
| `vesma_compress` | CCR: compress large content with zero data loss — original cached, marker returned |
| `vesma_retrieve` | Fetch the original content back by CCR marker hash |
| `vesma_align_prefix` | CacheAligner (P1-5): relocate dynamic content to the tail for KV-cache hits |
| `vesma_assemble_context` | Assemble the model-facing context block: search → compress → filter → secret scan → cache align → token budget |
| `vesma_context_rewrite` | `on_context_rewrite` (ADR-0018): preserve the lossless original when the harness rewrites its history |
| `vesma_hooks` | Lifecycle hooks: `pre_llm_call` / `on_session_start` / `post_tool_call` actions |
| `vesma_export` | Export memories to a file on disk |
| `vesma_import` | Import memories from an export file |
| `vesma_workflow` | Workflow lifecycle state for a memory (open → in-progress → done, blocked / …) |

Full catalogue with input schemas, examples, and HTTP equivalents: **[user/mcp-tools.md](user/mcp-tools.md)**

---

## Where to start

| If you are… | Read |
|-------------|------|
| Setting Vesma up for the first time | [user/getting-started.md](user/getting-started.md) |
| Connecting a specific harness | [Connect Vesma to any harness](../../integrations/mcp-presets.md) |
| Looking for a specific command / flag | [user/cli-reference.md](user/cli-reference.md) |
| Looking for a specific MCP tool | [user/mcp-tools.md](user/mcp-tools.md) |
| Making a codebase navigable for agents | [user/project-graph.md](user/project-graph.md) |
| Building an HTTP client | [user/http-api.md](user/http-api.md) |
| Trying to understand the system shape | [architecture/overview.md](architecture/overview.md) |
| Diagnosing a problem | [user/getting-started.md#troubleshooting](user/getting-started.md#troubleshooting) |

---

## User docs

- [Feature Map](features.md) — what works out of the box, what is partial, what is planned (v4.0.0).
- [Getting Started](user/getting-started.md) — install → first memory → first search → connect your harness.
- [Integration Guide](user/integration-guide.md) — behavioral layer, deploy targets, agent MCP wiring, Hermes plugin.
- [MCP Tools Reference](user/mcp-tools.md) — every `vesma_*` tool.
- [Project Graph](user/project-graph.md) — the codebase as memory: indexing, the ten tools, security guardrails, configuration.
- [HTTP API Reference](user/http-api.md) — every endpoint, request / response shape, error code.
- [CLI Reference](user/cli-reference.md) — every `vesma` subcommand with flags, defaults, and examples.
- [Tag Contract](user/tag-contract.md) — the M2 schema enforced on every memory (`project:`, `agent:`, `vesma:`).
- [Context Filter](user/context-filter.md) — the five-stage noise stripper (dedup, noise, extract, compress, tokens) with profiles and auto-filter.

---

## Admin / Ops

- [Runbooks — Install](admin/runbooks/install.md) — first-run operational checklist.
- [Runbooks — Container Deployment](admin/runbooks/container-deployment.md) — build, push, compose, podman, Kubernetes, quadlet.
- [Runbooks — Migrate](admin/runbooks/migrate.md) — import from legacy `ai-brain`.
- [Runbooks — Backup & Restore](admin/runbooks/backup-restore.md) — backup, point-in-time recovery.
- [Runbooks — Dependency Updates](admin/runbooks/dependency-updates.md) — CVE triage + weekly review.
- [Runbooks — CI/CD](admin/runbooks/ci-cd.md) — GitHub Actions pipeline operation.
- [Runbooks — PyPI Publish](admin/runbooks/pypi-publish.md) — name availability, wheel pipeline, version gates, first-publish procedure.
- [Security Model](admin/security.md) — threat model, SSRF guard, secrets hygiene, auth model.

---

## Architecture

- [System Overview](architecture/overview.md) — layered design, data model, state machines, security boundaries, operational concerns.
- [Knowledge Pipeline](user/http-api.md#knowledge-pipeline-m4) — how a memory moves from `raw` → `processing` → `processed` → `published` (M4).
- [A2A Sessions](architecture/a2a-sessions.md) — the agent-to-agent conversation contract (M16).

---

## Project (historical, English only)

- [Architecture Decision Records](../project/adr/README.md) — 22 ADRs covering the M1 → M16 evolution and the v4.0.0 foundation.
- [Milestones](../project/milestones.md) — milestone ledger with status legend.
- [Phase completion reports](../project/reports/) — final reports per completed roadmap phase (Phase 0–1: PR #135–#157).
- [Code Review 2026-06](../project/code-review-2026-06.md) — final code review findings and fixes.
- [Sessions](../project/sessions/) — orchestration session documents.

---

## Repo root

- [README](../../README.md) — top-level project page.
- [CHANGELOG](../../CHANGELOG.md) — release notes.
- [PLAN](../../PLAN.md) — phased implementation plan.
- [ARCHITECTURE](../../ARCHITECTURE.md) — high-level architecture summary (one-pager; see [architecture/overview.md](architecture/overview.md) for the full version).

---

_Last updated: 2026-09-05_
