# Mnemos Documentation (English)

**🌐 Language / Язык:** English · [Русский](../ru/index.md)

> Mnemos is a standalone memory & knowledge server for AI agents. It gives every agent real long-term memory — structured, searchable, governed by a strict tag contract — that persists across sessions, restarts, and context compression. One local server, three control surfaces (MCP / CLI / HTTP), and any MCP-capable harness connects in one line.

---

## Install (one command)

Mnemos is on **PyPI**:

```bash
pip install mnemos-memory-server
```

The default embedding model (`mnema-embed-v1`, ~30 MB) is bundled in the wheel — search works fully offline, on CPU, no API keys, nothing downloaded. Isolated variant: `uv tool install mnemos-memory-server` or `pipx install mnemos-memory-server`.

> ⚠️ The PyPI name is **`mnemos-memory-server`** — `pip install mnemos` installs an unrelated project.

npm (pi extension): `pi-mnemos` · `mnemos-pi` · `@korrlabs/mnemospi` · `@korrlabs/mnemos-pi`. Container: `ghcr.io/vesmaro/vesmaro`.

---

## Connect your harness

**MCP is the primary integration surface.** Any MCP-capable harness talks to Mnemos over the same stdio wire. The full copy-paste instructions for every harness live on one page: **[Connect Mnemos to any harness](../../integrations/mcp-presets.md)**.

| Harness | Fastest path |
|---------|--------------|
| VS Code Copilot | `curl -fsSL …/scripts/mcp-setup.sh \| bash` — or the one-paste `mcp.json` block on the presets page |
| Claude Code | `claude mcp add --scope user mnemos -- mnemos mcp-server` |
| Cursor | one line into `~/.cursor/mcp.json` |
| OpenCode | one block into `~/.config/opencode/opencode.json` |
| Codex | one TOML block into `~/.codex/config.toml` |
| Windsurf | same JSON line as Cursor, in `~/.codeium/windsurf/mcp_config.json` |
| ZCode / pi | `mnemos integration setup --target zcode` / `--target pi` |
| Hermes Agent | native in-process plugin — `mnemos integration setup --target hermes` |
| Anything else | [adapter-template.md](../../integrations/adapter-template.md) — Connect / Expose / Configure |

To also deploy the **behavioral layer** (memory instructions, 14+ skills, prompt mode, agent wiring) so agents *know when and how* to use memory:

```bash
mnemos integration setup
```

Targets, flags, and the full deploy map: [integration-guide.md](user/integration-guide.md).

### MCP server properties

| Property | Value |
|----------|-------|
| Protocol | MCP over **stdio JSON-RPC 2.0** |
| Server name | `mnemos` |
| Transport | stdio — no TCP port |
| Tool prefix | `mnemos_` |
| Start it | `mnemos mcp-server` |

### Auto-collect mode

Set `MNEMOS_AUTO_COLLECT=1` in the server's `env` block to make Mnemos prompt your agent to call `mnemos_save_context` after every ~6 tool calls (proactive checkpoint nagging). See [mcp-tools.md#auto-collect-mode](user/mcp-tools.md#auto-collect-mode) for trade-offs.

### 38 MCP tools (`mnemos_` prefix)

| Tool | Purpose |
|------|---------|
| `mnemos_search` | Hybrid FTS5 + vector search with Reciprocal Rank Fusion (published memories by default) |
| `mnemos_add` | Create a memory — **enforces the Mnemos tag contract** |
| `mnemos_filter` | Run or refresh the context filter on an existing memory (e.g. with another profile) |
| `mnemos_agent_recall` | Per-agent recall (M3) — most recent entries for a single agent |
| `mnemos_save_context` | Persist a session checkpoint |
| `mnemos_recall_context` | Restore the latest checkpoint for a project |
| `mnemos_list_recent` | List the most recent memory entries |
| `mnemos_list_tags` | List all tags with their counts |
| `mnemos_tags_rename` | Bulk rename a tag prefix across existing memories (dry-run by default) |
| `mnemos_tags` | Bulk tag operations: rename a prefix, remove or add tags |
| `mnemos_ingest_url` | Fetch a web page and save it as a memory |
| `mnemos_ingest_document` | Ingest a document as chunked, born-quarantined rows (ADR-0027 Ф3) |
| `mnemos_watch_start` | Register the project-graph watch poll (ADR-0032 §3.2; default-off operator flags) |
| `mnemos_watch_stop` | Stop one or all watch registrations |
| `mnemos_watch_status` | Report watch registrations and last poll outcome |
| `mnemos_index_project` | Index a registered project root into the project graph (ADR-0032, default-off) |
| `mnemos_project_graph_status` | Project-graph volumes, freshness, parse failures, poisoned count |
| `mnemos_search_graph` | Ranked search over the graph with the token contract |
| `mnemos_trace_path` | BFS over project edges from one symbol (depth ≤ 2) |
| `mnemos_get_file_outline` | Symbol outline of one indexed file (shapes, never bodies) |
| `mnemos_get_code_snippet` | Secret-scanned line range read from disk (PG4) |
| `mnemos_check_graph_coverage` | Per-path coverage verdicts: indexed / stale / parse-error / unindexed / poisoned |
| `mnemos_get_graph_schema` | The graph contract card for agents |
| `mnemos_list_graph_projects` | Registered projects joined with index status |
| `mnemos_delete_graph_project` | Drop the graph index (sidecar only); clears the poisoned set |
| `mnemos_auto_collect_status` | Compaction-detection signal vector (M7) |
| `mnemos_stats` | Health counters and key paths |
| `mnemos_reprocess` | Manually run the knowledge pipeline over queued entries |
| `mnemos_compress` | CCR: compress large content with zero data loss — original cached, marker returned |
| `mnemos_retrieve` | Fetch the original content back by CCR marker hash |
| `mnemos_align_prefix` | CacheAligner (P1-5): relocate dynamic content to the tail for KV-cache hits |
| `mnemos_assemble_context` | Assemble the model-facing context block: search → compress → filter → secret scan → cache align → token budget |
| `mnemos_context_rewrite` | `on_context_rewrite` (ADR-0018): preserve the lossless original when the harness rewrites its history |
| `mnemos_hooks` | Lifecycle hooks: `pre_llm_call` / `on_session_start` / `post_tool_call` actions |
| `mnemos_export` | Export memories to a file on disk |
| `mnemos_import` | Import memories from an export file |
| `mnemos_workflow` | Workflow lifecycle state for a memory (open → in-progress → done, blocked / …) |

Full catalogue with input schemas, examples, and HTTP equivalents: **[user/mcp-tools.md](user/mcp-tools.md)**

---

## Where to start

| If you are… | Read |
|-------------|------|
| Setting Mnemos up for the first time | [user/getting-started.md](user/getting-started.md) |
| Connecting a specific harness | [Connect Mnemos to any harness](../../integrations/mcp-presets.md) |
| Looking for a specific command / flag | [user/cli-reference.md](user/cli-reference.md) |
| Looking for a specific MCP tool | [user/mcp-tools.md](user/mcp-tools.md) |
| Building an HTTP client | [user/http-api.md](user/http-api.md) |
| Trying to understand the system shape | [architecture/overview.md](architecture/overview.md) |
| Diagnosing a problem | [user/getting-started.md#troubleshooting](user/getting-started.md#troubleshooting) |

---

## User docs

- [Feature Map](features.md) — what works out of the box, what is partial, what is planned (v4.0.0).
- [Getting Started](user/getting-started.md) — install → first memory → first search → connect your harness.
- [Integration Guide](user/integration-guide.md) — behavioral layer, deploy targets, agent MCP wiring, Hermes plugin.
- [MCP Tools Reference](user/mcp-tools.md) — every `mnemos_*` tool.
- [HTTP API Reference](user/http-api.md) — every endpoint, request / response shape, error code.
- [CLI Reference](user/cli-reference.md) — every `mnemos` subcommand with flags, defaults, and examples.
- [Tag Contract](user/tag-contract.md) — the M2 schema enforced on every memory (`project:`, `agent:`, `mnemos:`).
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
