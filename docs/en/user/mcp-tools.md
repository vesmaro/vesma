# MCP Tools Reference

**🌐 Language / Язык:** English · [Русский](../../ru/user/mcp-tools.md)

> Complete reference for the `vesma_*` tools exposed by the Vesma MCP server (`vesma mcp-server`).

Vesma speaks the [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) over **stdio JSON-RPC 2.0**. VS Code Copilot and any MCP-aware client can call the tools listed here.

The server is defined in `src/vesma/mcp_server.py`. Every tool below is registered with the `@server.list_tools()` decorator and dispatched by `call_tool()`.

For a quick start on wiring it into VS Code, see [getting-started.md#run-the-mcp-server](getting-started.md#connect-your-harness-mcp). For programmatic access, the same capabilities are also available over HTTP — see [http-api.md](http-api.md). For the tag schema enforced by most tools, see [tag-contract.md](tag-contract.md).

---

## Transport

| Property | Value |
|----------|-------|
| Protocol | MCP (JSON-RPC 2.0 over stdio) |
| Server name | `vesma` |
| Default transport | stdio (no TCP) |
| Tool prefix | `vesma_` |
| Encoding | UTF-8, JSON |

The server does not bind any port. Stop it with `Ctrl+C` or by sending EOF on stdin.

---

## Tool catalogue (summary)

| Tool | Purpose | Tags required |
|------|---------|---------------|
| [`vesma_add`](#vesma_add) | Create a new memory entry | yes |
| [`vesma_search`](#vesma_search) | Hybrid FTS + vector search | no |
| [`vesma_agent_recall`](#vesma_agent_recall) | Per-agent recall (M3) | no |
| [`vesma_recall_context`](#vesma_recall_context) | Restore session context for a project | no |
| [`vesma_save_context`](#vesma_save_context) | Persist a session checkpoint | no (auto) |
| [`vesma_list_recent`](#vesma_list_recent) | List recent entries | no |
| [`vesma_list_tags`](#vesma_list_tags) | List all tags with counts | no |
| [`vesma_tags`](#vesma_tags) *(pilot #97)* | Grouped bulk tag ops: rename / remove / add (`action: enum`) | no |
| [`vesma_tags_rename`](#vesma_tags_rename) | Bulk rename tag prefixes across memories (e.g. `gcw:` → `vesma:`); dry-run by default | no |
| [`vesma_workflow`](#vesma_workflow) *(#96)* | Workflow lifecycle: set / get / history (`action: enum`) | no |
| [`vesma_ingest_url`](#vesma_ingest_url) | Fetch and save a web page | yes |
| [`vesma_ingest_document`](#vesma_ingest_document) | Ingest a document as chunked, born-quarantined rows (ADR-0027 Ф3) | yes |
| [`vesma_watch_start`](#vesma_watch_start) | Register the project-graph watch poll (ADR-0032 §3.2) | no |
| [`vesma_watch_stop`](#vesma_watch_stop) | Stop one or all watch registrations | no |
| [`vesma_watch_status`](#vesma_watch_status) | Report watch registrations and last poll outcome | no |
| [`vesma_index_project`](#vesma_index_project) | Index a registered project root into the project graph (ADR-0032, on by default) | no |
| [`vesma_project_graph_status`](#vesma_project_graph_status) | Volumes, freshness, parse failures, poisoned count for one project | no |
| [`vesma_search_graph`](#vesma_search_graph) | Ranked name/qname/path search over the graph, token-contract windowed | no |
| [`vesma_trace_path`](#vesma_trace_path) | BFS over project edges from one symbol (depth ≤ 2) | no |
| [`vesma_get_file_outline`](#vesma_get_file_outline) | Symbol outline of one indexed file (shapes, never bodies) | no |
| [`vesma_get_code_snippet`](#vesma_get_code_snippet) | Secret-scanned line range read FROM DISK (PG4) | no |
| [`vesma_check_graph_coverage`](#vesma_check_graph_coverage) | Per-path verdict: indexed / stale / parse-error / unindexed / missing / poisoned | no |
| [`vesma_get_graph_schema`](#vesma_get_graph_schema) | The graph contract card: kinds, limits, token contract | no |
| [`vesma_list_graph_projects`](#vesma_list_graph_projects) | Registered projects joined with their index status | no |
| [`vesma_delete_graph_project`](#vesma_delete_graph_project) | Drop the graph index (sidecar only); clears the poisoned set | no |
| [`vesma_register_project`](#vesma_register_project) | Register a project root for the graph (#454) — the answer to "not registered" refusals | no |
| [`vesma_auto_collect_status`](#vesma_auto_collect_status) | Compaction signal vector (M7) | no |
| [`vesma_compress`](#vesma_compress) | Reversible compression (CCR) — cache original, embed marker | no |
| [`vesma_retrieve`](#vesma_retrieve) | Retrieve a CCR-cached original or FTS5 snippets | no |
| [`vesma_align_prefix`](#vesma_align_prefix) | CacheAligner — relocate dynamic content for prefix cache stability | no |
| [`vesma_filter`](#vesma_filter) | Run / refresh the context filter on an existing memory (secret-scanned `clean_content`) | no |
| [`vesma_assemble_context`](#vesma_assemble_context) *(#125)* | ADR-0017 D1 — assemble the pre-LLM-call context block (recall → CCR → filter → scan → align → budget) | no |
| [`vesma_context_rewrite`](#vesma_context_rewrite) *(#125)* | ADR-0018 — `on_context_rewrite` lifecycle event: report a context rewrite, the original lands in LTM (idempotent, version-less) | no |
| [`vesma_hooks`](#vesma_hooks) *(#125)* | ADR-0017 D1 / ADR-0018 lifecycle hooks — grouped `action:enum` tool: `pre_llm_call` / `on_session_start` / `post_tool_call` (autocompression, opt-in) | no |
| [`vesma_awareness`](#vesma_awareness) *(#254)* | Awareness pre-flight — server-observed neighbor presence, delta, conflict hints, and the swarm v0a/v0b operational picture (same-project peers: counts/ids/timestamps only + the peer's claimed task, self-reported and labeled) | no |
| [Native awareness heartbeat](#native-awareness-heartbeat-adr-0035) *(ADR-0035)* | The awareness tail that rides every MCP tool response natively (no manual invocation) — gated by `awareness.native_heartbeat_mode`; wave 0 ships `shadow` (measured, not rendered) | — |
| [`vesma_export`](#vesma_export) | Export memories to a file (JSON or SQLite snapshot) | no |
| [`vesma_import`](#vesma_import) | Import memories from an export file (merge or restore) | no |
| [`vesma_reprocess`](#vesma_reprocess) | Manually run the knowledge pipeline over queued raw/processing entries | no |
| [`vesma_stats`](#vesma_stats) | Health counters and key paths | no |

---

## `vesma_add`

Create a new memory entry. The MCP layer enforces the Vesma tag contract ([M2](tag-contract.md)) before writing.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `content` | string | **yes** | — | Text to remember. |
| `title` | string | no | auto | Short title. |
| `tags` | string[] | **yes** | — | Must include `project:<slug>`, `agent:<slug>`, and at least one `mnemos:<subtype>` (subtype namespace — unchanged data contract across the rebrand). |
| `memory_type` | string | no | `note` | One of `note`, `fact`, `snippet`, `bookmark`, `conversation`. |
| `filter_profile` | string | no | auto | One of `log`, `terminal`, `code`, `docs`, `web`, `default`. Drives M10 context filter. |
| `verbosity` | string | no | config default | One of `default`, `terse`, `minimal`. Injects output-style guidance into the tool result framing. See [Output token reduction](#output-token-reduction-p1-7). |
| `effort` | string | no | config default | One of `low`, `medium`, `high`. Injects reasoning-effort hint into the tool result framing. See [Output token reduction](#output-token-reduction-p1-7). |

### Output

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "title": "Use uv, not pip",
  "status": "raw"
}
```

### Example call (JSON-RPC)

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "vesma_add",
    "arguments": {
      "content": "Use uv, not pip",
      "tags": ["project:vesma", "agent:tech-writer", "mnemos:learning"]
    }
  }
}
```

### Errors

| Error | Cause |
|-------|-------|
| `❌ Tag contract violation: ...` | Missing `project:`, `agent:`, or `mnemos:` tag. |
| `❌ Error: ...` | SQLite write failure, vault write failure, or embed failure (the latter is non-fatal — see [architecture overview](../architecture/overview.md#1-storage-layer)). |

### Related

- Tag schema: [tag-contract.md](tag-contract.md)
- HTTP equivalent: [`POST /memories`](http-api.md#post-memories--create-memory)
- CLI equivalent: [`vesma add`](cli-reference.md#add)

---

## `vesma_search`

Hybrid search: FTS5 (full-text) + vector + Reciprocal Rank Fusion. Only `published` memories are searched by default.

**Query semantics:** the FTS5 leg treats the WHOLE `query` string as one quoted phrase (adjacent tokens, in order — `_build_fts_query` quotes the entire input). A keyword-set query like `postgres migration` matches only that exact phrase; to find individual keywords, issue separate single-term queries.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `query` | string | **yes** | — | Natural language search string. Matched as ONE whole phrase by the FTS5 leg (see Query semantics above). |
| `tags` | string[] | no | — | Filter: all of these tags must be present. |
| `project` | string | no | — | Restrict to a project slug. |
| `task` | string | no | — | ADR-0027 Phase 2 (epic #308): optional task scope — the bare slug (`[a-z0-9_-]{1,64}`, no `task:` prefix). Byte-identical to `tags=["task:<slug>"]` (the F1 arm-C surface): narrows results to that task's entries; composes with `tags` by intersection (both must hold). Normalized first (`My Task` → `my-task`); unsalvageable slugs fail loud. Bare-slug note (#455): a bare slug passed in `tags` (not `task`) matches nothing by itself — when such a query returns zero rows and `task:<slug>` entries exist, the search retries once with the exact tag and marks the surfaced rows (`task_tag_fallback`). |
| `limit` | integer | no | `10` | Max results. |
| `include_raw` | boolean | no | `false` | If true, returns `raw_content` instead of cleaned `content`. |
| `verbosity` | string | no | config default | One of `default`, `terse`, `minimal`. Injects output-style guidance into the tool result framing. See [Output token reduction](#output-token-reduction-p1-7). |
| `effort` | string | no | config default | One of `low`, `medium`, `high`. Injects reasoning-effort hint into the tool result framing. See [Output token reduction](#output-token-reduction-p1-7). |

### Output

```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "title": "Use uv, not pip",
    "content": "Use uv, not pip — it's faster and resolves transitive CVE closure correctly.",
    "tags": ["project:vesma", "agent:tech-writer", "mnemos:learning"],
    "score": 0.812,
    "search_type": "hybrid",
    "status": "published"
  }
]
```

### Example call

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "vesma_search",
    "arguments": {
      "query": "how to manage Python dependencies",
      "limit": 5,
      "project": "vesma"
    }
  }
}
```

### Errors

- `❌ Error: ...` — query parsing failure (rare; usually succeeds with an empty result).

### Related

- HTTP equivalent: [`POST /search`](http-api.md#search)
- CLI equivalent: [`vesma search`](cli-reference.md#search)

---

## `vesma_agent_recall`

Per-agent recall (M3). Returns the most recent entries for a single agent, optionally filtered by project and / or sub-query.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `agent` | string | **yes** | — | Agent slug, e.g. `cr-security-reviewer`. |
| `project` | string | no | — | Restrict to a project slug. |
| `task` | string | no | — | ADR-0027 Phase 2 (epic #308): optional task scope — the bare slug (`[a-z0-9_-]{1,64}`, no `task:` prefix). Byte-identical to a `task:<slug>` tag filter (the F1 arm-C surface): narrows the agent's entries to one task scope, on both the recency and the query legs. |
| `query` | string | no | — | Optional FTS / vector query within the agent scope. |
| `limit` | integer | no | `20` | Max entries to return. |

When `query` is omitted, the tool returns recent entries (recency-ordered). When `query` is present, it runs a hybrid search scoped to the agent's tags.

### Output

```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "title": "Bandit B608 hardcoded SQL — flag for triage",
    "content": "Found hardcoded SQL in src/legacy/loader.py:42 ...",
    "tags": ["project:vesma", "agent:cr-security-reviewer", "mnemos:bug-pattern"],
    "created_at": "2026-06-15T10:42:00+00:00",
    "status": "published"
  }
]
```

### Example call

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "vesma_agent_recall",
    "arguments": {
      "agent": "cr-security-reviewer",
      "project": "vesma",
      "query": "bandit SQL injection",
      "limit": 10
    }
  }
}
```

### Errors

- None typical. Returns an empty array if no matches.

### Related

- HTTP equivalent: [`GET /recall/agent/{name}`](http-api.md#get-recallagentname--agent-recall)
- CLI equivalent: [`vesma recall --agent <slug>`](cli-reference.md#recall)

---

## `vesma_recall_context`

Restore the latest session checkpoint for a project. The **first** thing an agent should call at the start of a session, especially after context compaction.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project` | string | no | auto (cwd) | Project name. Auto-detected from the current working directory if omitted. |
| `query` | string | no | — | Optional focus aspect. |
| `task` | string | no | — | ADR-0027 Phase 2 (epic #308): optional task scope — the bare slug (`[a-z0-9_-]{1,64}`, no `task:` prefix). Byte-identical to the checkpoint tag filter plus `task:<slug>` (the F1 arm-C surface): returns only checkpoints saved under that task. |
| `verbosity` | string | no | config default | One of `default`, `terse`, `minimal`. Injects output-style guidance into the tool result framing. See [Output token reduction](#output-token-reduction-p1-7). |
| `effort` | string | no | config default | One of `low`, `medium`, `high`. Injects reasoning-effort hint into the tool result framing. See [Output token reduction](#output-token-reduction-p1-7). |

### Output

A plain-text block formatted as Markdown:

```text
# Context for project 'vesma'

---
# Session checkpoint — 2026-06-15T10:42:00+00:00

## Goals
Ship M15 production hardening.
## Completed
bandit clean, mypy --strict green
## In Progress
pip-audit CVE-2026-45829 ignore
## Decisions
Pin chromadb 1.5.9 with audit
## Context
Active files: src/vesma/manager.py, src/vesma/api/main.py
```

If no checkpoint is found:

```text
No context found for project 'vesma'. Start by saving context with vesma_save_context.
```

In **auto-collect mode** (`VESMA_AUTO_COLLECT=1`), a `## 🔄 Auto-Collect Mode Active` block is appended with mandatory session rules.

### Example call

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "tools/call",
  "params": {
    "name": "vesma_recall_context",
    "arguments": { "project": "vesma" }
  }
}
```

### Related

- `vesma_save_context` — the matching writer
- [architecture.md](../architecture/overview.md)
- HTTP equivalent: [`POST /context/recall`](http-api.md#post-contextrecall--recall-session-context)

---

## `vesma_save_context`

Persist a session checkpoint. Agents should call this **proactively**: after meaningful work, before switching tasks, or when context is large.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project` | string | no | auto (cwd) | Project name. |
| `goals` | string | no | — | Current session goals. |
| `completed` | string | no | — | What has been completed. |
| `in_progress` | string | no | — | What is in progress. |
| `decisions` | string | no | — | Key technical decisions + rationale. |
| `context` | string | no | — | Other context (file paths, architecture, gotchas). |
| `agent` | string | no | `"user"` | Agent identity for the checkpoint — the validated identity channel (non-empty string when provided, whitespace-only rejected). Must match the server-side session→agent binding when `session` is supplied. |
| `session` | string | no | — | Session id binding the checkpoint to a conversation. First presentation records the session→agent binding server-side; later calls with the same session but a different agent are rejected. |
| `task` | string | no | — | ADR-0027 Phase 2 (epic #308): optional task scope — the bare slug (`[a-z0-9_-]{1,64}`, no `task:` prefix). Mints the `task:<slug>` tag on this checkpoint at the save boundary (one mint point, at most one task per record); recall it with `task=` on `vesma_recall_context` / `vesma_search` / `vesma_list_recent`. A dedup hit returns the first-minted row with ITS task scope (the new call's task never rewrites a stored record). |

Vesma synthesises the parts into a single Markdown memory tagged with `project:<slug>`, `agent:<validated-agent>` (`agent:user` when omitted), and `mnemos:checkpoint` — plus the optional `task:<slug>` when `task` is supplied. The validated identity is also stamped into server-controlled metadata (`checkpoint_agent`, `checkpoint_session`) — that metadata is the source of truth for per-agent attribution; tags are display-only.

A checkpoint whose five payload fields are all empty is trivially rejected before any store (zero-loss: the caller is told, nothing is silently dropped). Re-sending an identical payload for the same `(project, agent)` is idempotent: the existing memory id is returned with `duplicate=true` and nothing new is stored.

### Output

```text
✅ Context saved (id=550e8400-...).
✅ Duplicate checkpoint (id=550e8400-..., duplicate=true) — identical checkpoint already stored, nothing new created.
```

### Example call

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "tools/call",
  "params": {
    "name": "vesma_save_context",
    "arguments": {
      "project": "vesma",
      "goals": "Finish M15.1 mypy --strict",
      "completed": "Added None checks in 12 functions",
      "in_progress": "tests/test_api.py:241 type narrowing",
      "decisions": "Use cast() sparingly, prefer TypeGuard"
    }
  }
}
```

### Related

- `vesma_recall_context` — the matching reader
- Auto-collect mode: [mcp-tools.md#auto-collect-mode](#auto-collect-mode)
- HTTP equivalent: [`POST /context/save`](http-api.md#post-contextsave--save-a-session-checkpoint)

---

## `vesma_list_recent`

List the most recent memory entries, oldest-last.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `limit` | integer | no | `10` | Max entries. |
| `tags` | string[] | no | — | Filter: all of these tags must be present (AND). |
| `project` | string | no | — | Restrict to a project slug. |
| `task` | string | no | — | ADR-0027 Phase 2 (epic #308): optional task scope — the bare slug (`[a-z0-9_-]{1,64}`, no `task:` prefix). Byte-identical to appending `task:<slug>` to `tags` (the F1 arm-C surface); composes with `tags` by intersection (both must hold). |

### Output

```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "title": "Use uv, not pip",
    "tags": ["project:vesma", "agent:tech-writer", "mnemos:learning"],
    "status": "raw",
    "created_at": "2026-06-15T10:42:00+00:00"
  }
]
```

### Example call

```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "method": "tools/call",
  "params": {
    "name": "vesma_list_recent",
    "arguments": { "limit": 20, "project": "vesma" }
  }
}
```

### Related

- HTTP equivalent: [`GET /memories`](http-api.md#get-memories--list-recent)
- CLI equivalent: [`vesma recall`](cli-reference.md#recall)

---

## `vesma_list_tags`

List every tag in the memory with its occurrence count.

### Input

None.

### Output

```json
{
  "project:vesma": 142,
  "agent:tech-writer": 23,
  "agent:sre": 41,
  "mnemos:learning": 67,
  "mnemos:bug-pattern": 12,
  "mnemos:decision": 8,
  "mnemos:checkpoint": 14
}
```

### Example call

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "tools/call",
  "params": { "name": "vesma_list_tags", "arguments": {} }
}
```

### Related

- HTTP equivalent: [`GET /tags`](http-api.md#tags)

---

## `vesma_tags`

Grouped bulk tag operations across memories: rename a prefix, remove tags, or add tags. Action-based dispatch — the grouped pilot tool (#97); every action goes through the same safe write path (plain `UPDATE`, so the FTS5 index stays consistent), previews by default (`dry_run: true`) and is idempotent.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `action` | string | **yes** | — | `rename`, `remove`, or `add`. |
| `from_prefix` | string | for `rename` | — | Source prefix, e.g. `gcw:`. Must end with `:`. |
| `to_prefix` | string | for `rename` | — | Target prefix, e.g. `vesma:`. Must end with `:`. |
| `tags` | string[] | for `remove` / `add` | — | Tags to remove or add. Required for those two actions. |
| `subtypes` | string[] | no | — | Optional whitelist of subtypes to rename (`rename` only). |
| `wildcard` | boolean | no | `false` | `remove` only: treat each entry in `tags` as a prefix and strip every matching `prefix*` tag instead of exact matches. `rename` is prefix-based by design. |
| `dry_run` | boolean | no | `true` | Preview without writing. |
| `project` | string | no | — | Scope the scan to a project slug. |
| `agent` | string | no | — | Scope the scan to an agent slug. |
| `invalid_subtypes_to_legacy` | boolean | no | `false` | `rename` only: rename invalid subtypes to `<to_prefix>legacy` instead of skipping them. |

> **Contract safety.** The resulting tag set is re-validated in strict mode per memory: removing the last `project:` / `agent:` / `vesma:` tag (or otherwise breaking the contract) is rejected per memory with an error entry instead of corrupting the store.

### Output

A report dict. `changed` counts memories whose tag set actually changed; `renamed` is kept for back-compat with `vesma_tags_rename` callers and mirrors `changed`:

```json
{
  "action": "remove",
  "scanned": 142,
  "changed": 9,
  "removed_tags": ["severity:high"],
  "wildcard": false,
  "errors": [],
  "dry_run": true
}
```

`rename` returns `{"from_prefix", "to_prefix", "scanned", "renamed", "changed", "skipped_invalid", "errors", "dry_run"}`; `add` returns `{"action", "scanned", "changed", "added_tags", "errors", "dry_run"}`.

### Example call

```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "method": "tools/call",
  "params": {
    "name": "vesma_tags",
    "arguments": {
      "action": "rename",
      "from_prefix": "gcw:",
      "to_prefix": "mnemos:",
      "dry_run": false
    }
  }
}
```

### Errors

| Error | Cause |
|-------|-------|
| `unknown action '<x>'...` | `action` is not `rename` / `remove` / `add`. |
| `action='rename' requires 'from_prefix' and 'to_prefix' ...` | Missing prefixes for the rename action. |
| `tags must be a non-empty list` | `remove` / `add` called with an empty `tags` list. |

### Related

- Grouped sibling: [`vesma_tags_rename`](#vesma_tags_rename) — legacy alias for `action: "rename"`

---

## `vesma_tags_rename`

Bulk rename tags matching `from_prefix:<subtype>` → `to_prefix:<subtype>` across existing memories. Kept as a **non-breaking alias**: calls are dispatched to the same rename path as [`vesma_tags`](#vesma_tags) with `action: "rename"` (a stray `action` key in the arguments is ignored). Safe — the rename goes through a plain `UPDATE` so the FTS5 external-content index stays consistent — and idempotent: a second run with the same arguments renames 0 memories.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `from_prefix` | string | **yes** | — | Source prefix, e.g. `gcw:`. Must end with `:`. |
| `to_prefix` | string | **yes** | — | Target prefix, e.g. `vesma:`. Must end with `:`. |
| `subtypes` | string[] | no | — | Optional whitelist of subtypes to rename. |
| `dry_run` | boolean | no | `true` | Preview without writing. |
| `project` | string | no | — | Scope to a project slug. |
| `agent` | string | no | — | Scope to an agent slug. |
| `invalid_subtypes_to_legacy` | boolean | no | `false` | Rename invalid subtypes to `<to_prefix>legacy` instead of skipping them. |

### Output

```json
{
  "from_prefix": "gcw:",
  "to_prefix": "vesma:",
  "scanned": 142,
  "renamed": 0,
  "changed": 0,
  "skipped_invalid": 3,
  "errors": [],
  "dry_run": true
}
```

### Example call

```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "method": "tools/call",
  "params": {
    "name": "vesma_tags_rename",
    "arguments": {
      "from_prefix": "gcw:",
      "to_prefix": "mnemos:",
      "invalid_subtypes_to_legacy": true
    }
  }
}
```

### Related

- Grouped tool: [`vesma_tags`](#vesma_tags) — `action: "rename"` is the same code path
- HTTP equivalent: `POST /tags/rename` (implemented in the API; not yet covered in [http-api.md](http-api.md))

---

## `vesma_ingest_url`

Fetch a web page, extract its main content (via `trafilatura`), and save it as a memory.

### Input

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | **yes** | HTTP / HTTPS URL to fetch. |
| `tags` | string[] | **yes** | Same M2 contract as `vesma_add`. |

> **SSRF guard.** The MCP layer strips `user:password@` from the URL authority before fetching (defence in depth alongside the in-process guard). Do not bypass this by building the URL from a string.

### Output

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "title": "How to manage Python dependencies",
  "url": "https://example.com/article"
}
```

### Example call

```json
{
  "jsonrpc": "2.0",
  "id": 8,
  "method": "tools/call",
  "params": {
    "name": "vesma_ingest_url",
    "arguments": {
      "url": "https://example.com/article",
      "tags": ["project:research", "agent:user", "mnemos:learning"]
    }
  }
}
```

### Errors

| Error | Cause |
|-------|-------|
| `❌ Error: ...` | Network failure, blocked URL (SSRF guard), or `trafilatura` extraction failure. |

### Related

- CLI equivalent: [`vesma add --url <URL>`](cli-reference.md#add)
- HTTP equivalent: [`POST /memories` with manual content](http-api.md#post-memories--create-memory)
- HTTP equivalent: [`POST /ingest-url`](http-api.md#post-ingest-url--fetch-and-save-a-web-page)
- Security: [security.md](../admin/security.md#2-ssrf-prevention-memorymanager_validate_url)

---

## `vesma_ingest_document`

Ingest a full document as chunked memory rows — **docs-as-memory** (ADR-0027 Phase 3). The document text is split structure-preservingly (heading-scoped chunks carrying the `{doc_id, chunk_idx, heading_path}` metadata convention) and every chunk row enters memory **born quarantined**: ingested documents are untrusted content, invisible to every recall/assembly path until the **danger-sweep** clears them.

### Quarantine lifecycle (ADR-0019 §5 discipline)

1. **Born-quarantine** — every chunk row is created in the terminal danger-lane state (`pipeline_state=quarantined`, reason `doc-ingest-born-quarantine`): not recallable, not admissible to assembly, no vector embed.
2. **Danger-sweep at ingest-completion** — the ADR-0019 Phase A danger detector (the same enumerated positive-signal set that powers the publication gate) runs over every chunk of the document together:
   - a **clean** chunk is **released**: it becomes an ordinary `published` memory row (recallable, task-scopable, task-lens-visible — no special-casing), with the sweep timestamp recorded in its metadata;
   - a chunk with a **positive detector signal** (prompt-injection payload, high-confidence secret) **stays quarantined** with the detector class codes in the operator-side `quarantine_reason` — release is explicit and audited, quarantine is absorbing;
   - a **scanner error** fails closed: the chunk stays quarantined with reason `detector-error`.
3. **Release semantics are chunk-atomic** — a clean chunk is released even if a sibling chunk of the same document is flagged; the flagged chunk stays quarantined. A document is never half-released in the visibility sense: before the sweep no chunk is admissible, after it exactly the clean chunks are.
4. **Issuance remains the last line** (ADR-0027 invariant 7) — a released chunk contaminated *after* release is still subject to the repeat secret scan every content-echoing channel runs at issuance; refuse mode drops the item, redact mode replaces matched spans. The sweep is not the last line, issuance is.

### Re-ingest and the cache version (ADR-0027 invariant 4)

Re-ingesting the same `doc_id` **replaces** the document's chunk rows (a re-fragmentation) and bumps the doc-chunk `ccr_cache` version key **in the same SQLite transaction**. Honest scope: the version key is a **consumer-facing invalidation counter** (exposed in `vesma_stats` / `GET /stats` as `doc_chunk_cache_version`, the same posture as `graph_epoch`) — bumped transactionally on every re-fragmentation; any assembly-cache consumer **must** read it and treat a change as a full invalidation. No in-repo consumer keys on it yet.

### Boundary with `vesma_ingest_url`

`vesma_ingest_url` keeps its pre-Phase-3 semantics: a fetched page saved as ONE memory row through the ordinary visibility policy, no born-quarantine. It is **not** retroactively quarantined — the document path is this separate tool.

### Input

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | **yes** | Full document text to chunk and ingest. |
| `doc_id` | string | **yes** | Logical document identity; stable across re-ingest (replaces the chunks + bumps the cache version). |
| `tags` | string[] | **yes** | Same M2 contract as `vesma_add`. |
| `title` | string | no | Optional document title. |
| `source_url` | string | no | Optional provenance URL. |

### Output

```json
{
  "doc_id": "dep-guide",
  "chunks_total": 3,
  "released": 2,
  "quarantined": 1,
  "chunk_ids": ["…", "…", "…"],
  "reingest": false,
  "cache_version": 0,
  "truncated": false
}
```

### Example call

```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "method": "tools/call",
  "params": {
    "name": "vesma_ingest_document",
    "arguments": {
      "text": "# Deploy\n\nRun the rollout.\n\n# Rollback\n\nRestore the previous release.",
      "doc_id": "dep-guide",
      "tags": ["project:research", "agent:user", "mnemos:learning"],
      "title": "Deployment Guide"
    }
  }
}
```

### Errors

| Error | Cause |
|-------|-------|
| `❌ Error: ...` | Empty/whitespace document (no chunks produced), or a doc_id boundary violation. |

### Related

- HTTP equivalent: [`POST /ingest-document`](http-api.md#post-ingest-document--ingest-a-document-as-chunked-quarantined-rows)
- Single-URL tool: [`vesma_ingest_url`](#vesma_ingest_url) (separate semantics — one row, no born-quarantine)
- ADR: [ADR-0027](../../project/adr/0027-multi-context-memory.md) (Phase 3, invariants 4/7/8); [ADR-0019](../../project/adr/0019-optimistic-publication-async-refinement.md) (§5 quarantine, Phase A danger gate)

---

## `vesma_watch_start`

Register a project's code graph for the in-process watch poll (ADR-0032 §3.2). One cooperative background thread checks the project's indexed files by mtime+size on an adaptive interval and reindexes on actual changes — audited with reason `watch`.

> **Changed.** This is not a directory watcher. The former `paths=` / `scan=` / `include_rules=` form was an unimplemented stub that reported false success; it is gone, and those arguments are now rejected with `bad-request`.

**Prerequisites:** the graph flags (on by default since the owner decision of 2026-09-28; `code_graph.enabled` and `code_graph.watch`), an existing index for the project, and agent attribution (PG7).

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | **yes** | — | Registered project id to watch. |
| `agent` | string | **yes** | — | Caller identity (PG7 per-agent attribution). |
| `session` | string | no | — | Optional session id for the audit trail. |

### Output

```json
{
  "status": "registered",
  "project": "vesma",
  "project_id": "vesma",
  "root": "/home/you/vesma",
  "agent": "tech-writer",
  "session": null,
  "registered_at": "2026-09-28T12:00:00+00:00",
  "interval_sec": 5.0,
  "runs": 0,
  "reindexes": 0,
  "last_run_at": null,
  "last_result": null,
  "last_error": null
}
```

A repeat registration for the same project returns the payload with `"status": "already-registered"`. Registrations live for the process lifetime — a restart drops them and they must be re-registered. The global cap is `code_graph.watch_max_registrations` (default 8).

### Example call (JSON-RPC)

```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "method": "tools/call",
  "params": {
    "name": "vesma_watch_start",
    "arguments": { "project_id": "vesma", "agent": "tech-writer" }
  }
}
```

### Errors

| Error | Cause |
|-------|-------|
| `code: "disabled"` | `code_graph.enabled` or `code_graph.watch` is off. |
| `code: "attribution-required"` | Missing or empty `agent`. |
| `code: "bad-request"` | Missing `project_id`; no index yet (the poll reindexes — it never seeds a first index); registration cap reached; or the rejected legacy form (`paths=` / `scan=` / `include_rules=`). |

### Related

- HTTP equivalent: [`POST /watch/start`](http-api.md#post-watchstart--register-the-project-graph-watch-poll)
- Graph tool family: [Project graph tools (ADR-0032)](#project-graph-tools-adr-0032)

---

## `vesma_watch_stop`

Stop one watch registration (by `project_id`) or ALL of them when the argument is omitted. Idempotent.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | no | — | Project to stop watching; omit to stop all. |

### Output

```text
✅ Watch stopped.
```

### Related

- HTTP equivalent: [`POST /watch/stop`](http-api.md#post-watchstop--stop-watch-registrations)
- Graph tool family: [Project graph tools (ADR-0032)](#project-graph-tools-adr-0032)

---

## `vesma_watch_status`

Report active watch registrations and the last poll outcome per project (ADR-0032 watch poll).

### Input

None.

### Output

```json
{
  "running": true,
  "watch_enabled": true,
  "cap": 8,
  "registrations": [
    {
      "project": "vesma",
      "project_id": "vesma",
      "root": "/home/you/vesma",
      "agent": "tech-writer",
      "session": null,
      "registered_at": "2026-09-28T12:00:00+00:00",
      "interval_sec": 5.0,
      "runs": 3,
      "reindexes": 1,
      "last_run_at": "2026-09-28T12:00:15+00:00",
      "last_result": "fresh",
      "last_error": null
    }
  ]
}
```

### Related

- HTTP equivalent: [`GET /watch/status`](http-api.md#get-watchstatus--watch-poll-status)
- Graph tool family: [Project graph tools (ADR-0032)](#project-graph-tools-adr-0032)

---

## Project graph tools (ADR-0032)

Ten tools over the **project code graph**: symbols and file outlines parsed by tree-sitter, navigation, coverage honesty. Only names, qualified names, line ranges and signature shapes are persisted — zero bytes of source live in the graph (PG1). The design, security invariants and roadmap: [ADR-0032](../../project/adr/0032-project-graph.md).

> **On by default (owner decision 2026-09-28).** Graphs are first-class: ecosystem components build on them, and tree-sitter ships as a core dependency. An operator can still hide the whole surface with `code_graph.enabled: false` — every call then answers `code: "disabled"`. The same gate shapes the [`/graph/` REST namespace](http-api.md#project-graph-adr-0032).

### Operator gate (configuration)

| Key (`code_graph.`) | Default | Meaning |
|---------------------|---------|---------|
| `enabled` | `true` | Master flag for the 10 tools and the `/graph/` REST namespace — ON by default (owner decision 2026-09-28); `false` hides the whole surface. |
| `beacon` | `true` | One tail line in `assemble_context` output advertising graph freshness (effective only when `enabled`). |
| `watch` | `true` | The watch poll (`vesma_watch_start`) is armed by default but INERT until an explicit registration; the master gate applies on top. |
| `auto_index` | `true` | Native auto-indexing (see below) — first contact through MCP/hooks auto-registers and background-indexes; `false` leaves only the manual triggers. |
| `index_max_files` | `20000` | Hard cap on indexed files per project. Fail-closed: a breach refuses the WHOLE index — no partial graph is ever published (PG7). |
| `index_max_source_mb` | `500` | Hard cap on total source bytes per project, MiB (same fail-closed discipline). |
| `watch_max_registrations` | `8` | Global cap on active watch registrations per process. |
| `watch_base_interval_sec` / `watch_interval_per_500_files` / `watch_max_interval_sec` | `5.0` / `1.0` / `60.0` | Adaptive poll interval: base + 1s per 500 indexed files, capped. |
| `auto_reindex_min_interval_sec` | `300.0` | Per-project throttle between consecutive AUTO index actions (the manual path is never throttled). |
| `auto_register_max_projects` | `64` | Global cap on projects the AUTO path may ever create (counted by the `auto-registered by` description marker). Past the cap a hint is a silent skip with an `auto-register-capped` audit row — never an error; root-reuse and operator registrations never count. |

Env overrides follow the canonical settings pattern: `VESMA_CODE_GRAPH__INDEX_MAX_FILES`, `VESMA_CODE_GRAPH__INDEX_MAX_SOURCE_MB`, `VESMA_CODE_GRAPH__AUTO_INDEX`, `VESMA_CODE_GRAPH__AUTO_REINDEX_MIN_INTERVAL_SEC`, `VESMA_CODE_GRAPH__AUTO_REGISTER_MAX_PROJECTS`.

### Native auto-indexing (zero-touch)

Since wave PG-0.5 (owner directive 2026-09-29) the graph indexes itself — **no explicit call, no instruction, no skill**:

- **First contact auto-registers.** Every dispatched MCP tool call and every `pre_llm_call` hook emits a cheap activity hint. When the project is not yet in the projects table and its cwd carries a packaging manifest (`pyproject.toml`, `setup.py`, `package.json`, `go.mod`, `Cargo.toml` — **auto-registration requires a manifest marker; a bare `.git` is not enough**, and `$HOME`/the filesystem root never auto-register even with a manifest present), the project is auto-registered with that cwd as its root — attribution (agent, timestamp) lands in the project description and a PG7 `auto-register` audit row. **One root = one graph**: a name hint over an already-registered root reuses the EXISTING project (`auto-register-reused` audit) instead of creating a duplicate row and re-indexing the same tree; the global cap `auto_register_max_projects` (default 64) bounds how many projects the auto path may ever create, past it a silent skip with an `auto-register-capped` audit row.
- **Then the background work runs.** No index yet → a background first index (audit reason `auto-first`); an existing index → a cheap mtime+size staleness check and, on actual changes, an incremental reindex (audit reason `auto-stale`). All of it rides the same single cooperative scheduler thread as the watch poll; the hinting tool call is never blocked and never fails because of a hint.
- **The beacon appears by itself.** Once an index exists, the `assemble_context` tail line shows up with no action from the agent.
- **Guardrails.** Auto actions are throttled per project (`auto_reindex_min_interval_sec`, default 300s), attributed to the hinting agent (no `agent` → no auto action, PG7), and pass through the same fail-closed PG7 limits as manual runs — a limit breach aborts the whole auto index with an audit row, never a partial graph. A FAILED first auto index suspends the auto path for that project (sidecar flag `auto_suspended`): further hints skip the tree entirely — no disk walk — until a successful manual `vesma_index_project`, a `vesma_delete_graph_project`, or a watch reindex lifts the flag; `auto-stale` runs over a valid existing index never suspend. A multi-path registration indexes `paths[0]` only (v1 limitation).
- **REST is not an auto surface** (no cwd to gate a registration on) — `/graph/*` stays exactly as documented. The manual tools (`vesma_index_project`, `vesma_watch_start`) remain the explicit-control path; `code_graph.auto_index: false` turns the auto path off entirely.

### Token contract

Every windowed tool takes `max_output_tokens` (integer, 128–1,000,000, default 3200):

- The budget is enforced in **bytes = tokens × 4** — a deterministic 4 UTF-8 bytes per token ceiling, never a tokenizer guess.
- Rows are **never split**: a row that no longer fits is dropped WHOLE; snippets drop whole LINES. `has_more: true` and a cursor tell you what remains.
- The cursor **strictly advances** (at least one row is always consumed). A budget that cannot fit even one row is refused (`GraphBudgetError`, HTTP `400`) instead of looping on the same page.
- Detail is opt-in: signatures ride only when `include_signature: true` is passed to `vesma_search_graph`.

Errors shared by the whole group — `disabled` (operator gate), `attribution-required` (missing `agent`, PG7), confinement refusals (unregistered project or a path escaping the registered root, PG2), budget refusals. Every call, read or write, is audited per agent (PG7). REST twins map these to HTTP codes: see the [project graph REST section](http-api.md#project-graph-adr-0032).

---

## `vesma_index_project`

Index a **registered** project root into the shared project graph — full or incremental. Serialized per project: a concurrent call gets `in-progress` status immediately. PG2: only a project registered in the projects table is accepted; arbitrary paths are refused. PG7: limits are fail-closed, the run is audited with your agent id.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | **yes** | — | Registered project id or unique name. |
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `session` | string | no | — | Optional session id for the audit trail. |
| `incremental` | boolean | no | `true` | Skip work when nothing changed. |
| `reason` | string | no | — | Audit reason. |

### Output

```json
{
  "status": "indexed",
  "nodes": 2143,
  "edges": 5107,
  "files_indexed": 312,
  "files_skipped": 88,
  "poisoned": ["deploy/secret.env"],
  "unpoisoned": [],
  "parse_errors": {"legacy/parser.py": "unsupported syntax"},
  "duration_sec": 4.212,
  "incremental": true,
  "staleness": {
    "total_files": 400,
    "fresh_percent": 100.0,
    "changed_files": [],
    "last_indexed_at": "2026-09-28T12:00:04+00:00"
  }
}
```

`status` is `indexed` / `reindexed` / `fresh` / `in-progress`; `staleness` is `null` when nothing changed (no fake freshness). Parse failures ride along as an honesty marker — «clean ≠ proof». Poisoned paths hit the secrets detector at index time and are refused at snippet issuance forever (PG3) — unless allowlisted (`code_graph.secret_allowlist`, #449): `unpoisoned` lists paths this run's allowlist pass removed from the poisoned set (audited as `allowlist-unpoison`).

### Example call (JSON-RPC)

```json
{
  "jsonrpc": "2.0",
  "id": 30,
  "method": "tools/call",
  "params": {
    "name": "vesma_index_project",
    "arguments": { "project_id": "vesma", "agent": "tech-writer" }
  }
}
```

### Related

- HTTP equivalent: [`POST /graph/index`](http-api.md#post-graphindex--index-a-registered-project)
- Security model: [ADR-0032 §6 (invariants PG1–PG7)](../../project/adr/0032-project-graph.md)

---

## `vesma_project_graph_status`

Project-graph status for one registered project: node/edge/file volumes, freshness (fresh %, `last_indexed_at`), parse failures (they stay visible) and the poisoned-file count (PG3). Read-only, audited.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | **yes** | — | Registered project id or unique name. |
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `session` | string | no | — | Optional session id for the audit trail. |

### Output

```json
{
  "project": "vesma",
  "nodes": 2143,
  "edges": 5107,
  "files": 400,
  "parse_errors": {"legacy/parser.py": "unsupported syntax"},
  "parse_error_count": 1,
  "poisoned_count": 1,
  "staleness": {
    "total_files": 400,
    "fresh_percent": 97.5,
    "changed_files": ["src/vesma/manager.py"],
    "last_indexed_at": "2026-09-28T12:00:04+00:00"
  }
}
```

### Related

- HTTP equivalent: [`GET /graph/status/{project_id}`](http-api.md#get-graphstatusproject_id--project-graph-status)

---

## `vesma_search_graph`

Search the project graph by name / qualified name / path (substring). Ranking BEFORE the budget cut: exact hits outrank prefix hits, prefix outranks substring. Token contract applies.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | **yes** | — | Registered project id or unique name. |
| `query` | string | **yes** | — | Name / qname / path substring. |
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `session` | string | no | — | Optional session id for the audit trail. |
| `kind` | string | no | — | Filter by node kind: one of `Project`, `File`, `Module`, `Class`, `Function`, `Method`, `Type`. |
| `limit` | integer | no | `50` | Max ranked rows per page (hard page ceiling 200). |
| `cursor` | integer | no | `0` | Page cursor from the previous call. |
| `max_output_tokens` | integer | no | `3200` | Output budget (128–1M). |
| `include_signature` | boolean | no | `false` | Opt-in detail flag: include signature shapes. |

### Output

```json
{
  "project": "vesma",
  "query_kind": null,
  "results": [
    {
      "score": 3,
      "id": "vesma#src/vesma/codegraph/service.py#window_rows#158",
      "project": "vesma",
      "kind": "Function",
      "name": "window_rows",
      "qname": "vesma.codegraph.service.window_rows",
      "path": "src/vesma/codegraph/service.py",
      "start_line": 158,
      "end_line": 190,
      "lang": "python",
      "signature": "def window_rows(rows: list[dict[str, Any]], max_output_tokens: int, cursor: int) -> tuple[list[dict[str, Any]], bool, int]"
    }
  ],
  "total_matches": 1,
  "cursor": 0,
  "has_more": false,
  "last_indexed_at": "2026-09-28T12:00:04+00:00"
}
```

### Related

- HTTP equivalent: [`POST /graph/search`](http-api.md#post-graphsearch--search-the-project-graph)
- Token contract: [above](#token-contract)

---

## `vesma_trace_path`

BFS over `project_edges` from one symbol, resolved by qname (exact, or a unique dotted-tail match — ambiguous refusals name `vesma_search_graph`). Depth ≤ 2 with a per-node fanout cap and a total-work cap (the ADR-0030 walk discipline). The token contract applies to the `nodes` section; the `edges` section rides outside the token budget, bounded only by the fanout/total caps and honestly marked `truncated` when hit (edge budgeting lands in PG-1, ADR-0032).

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | **yes** | — | Registered project id or unique name. |
| `qname` | string | **yes** | — | Symbol qualified name (exact or unique tail). |
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `session` | string | no | — | Optional session id for the audit trail. |
| `depth` | integer | no | `2` | BFS depth, 1–2. |
| `max_output_tokens` | integer | no | `3200` | Output budget (128–1M). |

### Output

```json
{
  "project": "vesma",
  "start": "vesma.codegraph.service.window_rows",
  "depth": 2,
  "nodes": [
    {
      "id": "vesma#src/vesma/codegraph/service.py#window_rows#158",
      "qname": "vesma.codegraph.service.window_rows",
      "kind": "Function",
      "path": "src/vesma/codegraph/service.py",
      "start_line": 158,
      "end_line": 190,
      "depth": 0
    }
  ],
  "edges": [
    { "from": "vesma#…#window_rows#158", "to": "vesma#…#resolve_token_budget#135", "kind": "CALLS", "provenance": "tree-sitter" }
  ],
  "truncated": false,
  "cursor": 0,
  "has_more": false,
  "last_indexed_at": "2026-09-28T12:00:04+00:00"
}
```

`truncated: true` means a fanout or total-work cap bit — the walk is honest about what it skipped.

### Related

- HTTP equivalent: [`POST /graph/trace`](http-api.md#post-graphtrace--trace-the-path-from-a-symbol)

---

## `vesma_get_file_outline`

Symbol outline of one indexed file: kinds, names, qnames, line ranges, signature shapes — never bodies (PG1). The path is repo-relative and must stay inside the registered root (PG2). Parse failures ride along as an honesty marker. Token contract applies.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | **yes** | — | Registered project id or unique name. |
| `path` | string | **yes** | — | Repo-relative file path. |
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `session` | string | no | — | Optional session id for the audit trail. |
| `cursor` | integer | no | `0` | Page cursor. |
| `max_output_tokens` | integer | no | `3200` | Output budget (128–1M). |

### Output

```json
{
  "project": "vesma",
  "path": "src/vesma/codegraph/service.py",
  "lang": "python",
  "outline": [
    {
      "kind": "Function",
      "name": "window_rows",
      "qname": "vesma.codegraph.service.window_rows",
      "start_line": 158,
      "end_line": 190,
      "signature": "def window_rows(rows, max_output_tokens, cursor)"
    }
  ],
  "parse_error": null,
  "cursor": 0,
  "has_more": false,
  "last_indexed_at": "2026-09-28T12:00:04+00:00"
}
```

### Related

- HTTP equivalent: [`POST /graph/outline`](http-api.md#post-graphoutline--symbol-outline-of-one-file)

---

## `vesma_get_code_snippet`

Read a line range **from disk** for an indexed file. The full PG4 sequence runs on every call: poisoned refusal (permanent) → path confinement → indexed check → mtime+size+sha256 freshness → issuance secret scan (ANY hit refuses the whole range fail-closed) → whole-line token window. There is **no snippet cache** — every call re-reads the file.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | **yes** | — | Registered project id or unique name. |
| `path` | string | **yes** | — | Repo-relative file path. |
| `start_line` | integer | **yes** | — | First line (1-based). |
| `end_line` | integer | **yes** | — | Last line (inclusive). |
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `session` | string | no | — | Optional session id for the audit trail. |
| `max_output_tokens` | integer | no | `3200` | Output budget (128–1M); whole-line drops. |

### Output

```json
{
  "project": "vesma",
  "path": "src/vesma/codegraph/service.py",
  "start_line": 158,
  "end_line": 172,
  "content": "def window_rows(\n    rows: list[dict[str, Any]],\n    ...\n)",
  "total_file_lines": 1081,
  "has_more": true,
  "next_start_line": 173,
  "scanned": true,
  "stale": false
}
```

A file that changed on disk since indexation yields a staleness marker — never content; reindex to refresh. A poisoned file (hit the secrets detector at index time) is refused permanently — only `vesma_delete_graph_project` clears it (PG3).

### Related

- HTTP equivalent: [`POST /graph/snippet`](http-api.md#post-graphsnippet--secret-scanned-line-range-from-disk)

---

## `vesma_check_graph_coverage`

Batch coverage check: per-path verdict `indexed` / `stale` / `parse-error` / `unindexed` / `missing` (path does not exist under the project root, #452) / `poisoned`. Coverage honesty — trust is NOT here; verify with `vesma_get_code_snippet`.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | **yes** | — | Registered project id or unique name. |
| `paths` | string[] | **yes** | — | Repo-relative paths to check (non-empty). |
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `session` | string | no | — | Optional session id for the audit trail. |

### Output

```json
{
  "project": "vesma",
  "coverage": [
    { "path": "src/vesma/manager.py", "verdict": "stale" },
    { "path": "src/vesma/codegraph/service.py", "verdict": "indexed" },
    { "path": "docs/en/user/mcp-tools.md", "verdict": "unindexed" },
    { "path": "deploy/secret.env", "verdict": "poisoned", "reason": "secret-detected (permanent)" }
  ]
}
```

### Related

- HTTP equivalent: [`POST /graph/coverage`](http-api.md#post-graphcoverage--batch-coverage-check)

---

## `vesma_get_graph_schema`

The project-graph contract card for agents: node/edge kinds, the token contract, index and trace limits, schema version. An optional `project_id` adds that project's volumes.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `project_id` | string | no | — | Registered project id or name (adds volumes). |
| `session` | string | no | — | Optional session id for the audit trail. |

### Output

```json
{
  "schema_version": 1,
  "node_kinds": ["Project", "File", "Module", "Class", "Function", "Method", "Type"],
  "edge_kinds": ["CONTAINS_FILE", "DEFINES", "IMPORTS", "CALLS", "INHERITS", "TESTS", "USES"],
  "token_contract": {
    "max_output_tokens_default": 3200,
    "max_output_tokens_min": 128,
    "max_output_tokens_max": 1000000,
    "bytes_per_token": 4
  },
  "limits": { "index_max_files": 20000, "index_max_source_mb": 500 },
  "trace": { "max_depth": 2, "fanout_cap": 32, "total_work_cap": 512 }
}
```

### Related

- HTTP equivalent: [`GET /graph/schema`](http-api.md#get-graphschema--graph-contract-card)

---

## `vesma_list_graph_projects`

Registered projects joined with their index status (volumes, poisoned count, `last_indexed_at`). Registered-but-never-indexed projects stay visible; so do indexed orphans whose project entity was deregistered.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `session` | string | no | — | Optional session id for the audit trail. |

### Output

```json
{
  "projects": [
    {
      "project": "vesma",
      "registered": true,
      "has_root": true,
      "root_missing": false,
      "nodes": 2143,
      "edges": 5107,
      "files": 400,
      "poisoned": 1,
      "last_indexed_at": "2026-09-28T12:00:04+00:00"
    }
  ],
  "has_more": false,
  "cursor": 0
}
```

`root_missing: true` (#450) marks a **ghost**: the registered root is gone on disk (moved/renamed), so indexing is stuck — fix it with `vesma graph repoint <project> <new-root>`.

### Related

- HTTP equivalent: [`GET /graph/projects`](http-api.md#get-graphprojects--list-graph-projects)

---

## `vesma_delete_graph_project`

Drop a project's graph INDEX — the sidecar data only, never the project entity in the main DB. The ONLY operation that clears the poisoned set (PG3 «forever»). Audited with an optional reason.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | **yes** | — | Registered project id or unique name. |
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `session` | string | no | — | Optional session id for the audit trail. |
| `reason` | string | no | — | Audit reason. |

### Output

```json
{ "project": "vesma", "deleted_nodes": 2143, "status": "deleted" }
```

### Related

- HTTP equivalent: [`DELETE /graph/projects/{project_id}`](http-api.md#delete-graphprojectsproject_id--drop-a-graph-index)

---

## `vesma_register_project`

Register a project root for the code graph (#454) — the agent-side answer to `not registered` confinement refusals (previously operator-only, and the auto path covers marker roots only, capped at `auto_register_max_projects`).

The root must exist on disk, be absolute, carry a packaging manifest (`pyproject.toml` / `setup.py` / `package.json` / `go.mod` / `Cargo.toml`) or a `.git`, and not be `$HOME`/the filesystem root. One root = one graph: a root already registered under another project is REUSED (audit `manual-register-reused`), never duplicated. A project name already registered at a DIFFERENT root is refused — moved roots belong to the operator's `vesma graph repoint` (#450). An existing project row without paths (auto-created by memory writes) gets the root attached. **Explicit registration does not count against `auto_register_max_projects`** — that cap bounds the AUTO path only (its provenance marker lives in the description, which manual rows never carry).

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | **yes** | — | Project id or name to register. |
| `root` | string | **yes** | — | Absolute path to the project root on disk. |
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `session` | string | no | — | Optional session id for the audit trail. |

### Output

```json
{ "project": "vesma", "status": "registered", "root": "/home/you/vesma" }
```

`status` is `already-registered` (idempotent, root reuse) or `registered`. CLI twin: `vesma graph register <project> <root>`.

### Related

- [project-graph.md — Turn it on for a project](project-graph.md#turn-it-on-for-a-project)

---

## `vesma_auto_collect_status`

Return the current compaction-detection signal vector (M7). The agent reads this to decide whether to call `vesma_save_context` proactively.

### Input

None.

### Output

```json
{
  "auto_collect_enabled": false,
  "signals": {
    "call_counter": {
      "calls_since_save": 7,
      "threshold": 12,
      "triggered": false
    },
    "elapsed_secs": {
      "value": 312,
      "threshold": 900,
      "triggered": false
    },
    "context_size_heuristic": {
      "value": null,
      "note": "populated by client (M7)"
    },
    "summary_marker_detected": {
      "value": null,
      "note": "populated by client (M7)"
    },
    "reference_drop_heuristic": {
      "value": null,
      "note": "populated by client (M7)"
    }
  },
  "recommendation": "ok",
  "next_reminder_in_calls": 5
}
```

The `recommendation` field is one of:

| Value | Meaning |
|-------|---------|
| `ok` | No checkpoint needed yet. |
| `save_checkpoint` | Save now — you are at or past a threshold. |

### Auto-collect mode

Set `VESMA_AUTO_COLLECT=1` in the server's environment. The reminder thresholds tighten:

| Setting | Normal | Auto-collect |
|---------|--------|--------------|
| Calls since save | 12 | 6 |
| Elapsed seconds | 900 (15 min) | 480 (8 min) |

Tool descriptions also change (with `🔄 [AUTO-COLLECT] MANDATORY:` prefixes) so agents take the hints more seriously. **Recommended for production agents**, not for one-off scripts.

### Related

- HTTP equivalent: [`GET /auto-collect`](http-api.md#get-auto-collect--compaction-signal-vector)

---

## `vesma_stats`

Return Vesma health counters.

### Input

None.

### Output

Same shape as the CLI `vesma stats` command — see [cli-reference.md#stats](cli-reference.md#stats).

```json
{
  "status": "ok",
  "version": "4.0.0",
  "data_dir": "/home/you/.vesma/data",
  "vault_path": "/home/you/.vesma/vault",
  "total": 142,
  "by_status": {"raw": 5, "processing": 0, "processed": 12, "published": 120, "archived": 5},
  "vectors": 120
}
```

### Related

- HTTP equivalent: [`GET /metrics`](http-api.md#get-metrics)
- CLI equivalent: [`vesma stats`](cli-reference.md#stats)

---

## `vesma_reprocess`

Manually trigger the knowledge pipeline to process queued `raw` / `processing` entries into `published` knowledge: cluster → synthesize → quality gate → publish. Use when `vesma_stats` shows a large `queue_depth`, or after bulk import.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project` | string | no | — | Restrict the pass to a project slug. |
| `agent` | string | no | — | Restrict the pass to an agent slug. |
| `limit` | integer | no | `100` | Maximum entries considered. |

### Output

The pipeline summary dict:

```json
{
  "clusters": 3,
  "synthesized": 3,
  "published": 5,
  "failed_quality_gate": 1,
  "single_promoted": 2,
  "stuck_rescued": 0,
  "published_ids": ["550e8400-e29b-41d4-a716-446655440000"],
  "refined": 4,
  "refined_noop": 1,
  "refine_failed": 0,
  "quarantined": 0
}
```

Memories that do not form a cluster are promoted individually (`single_promoted`) so the queue drains even when most entries are unique.

### Example call

```json
{
  "jsonrpc": "2.0",
  "id": 11,
  "method": "tools/call",
  "params": {
    "name": "vesma_reprocess",
    "arguments": { "project": "vesma", "limit": 200 }
  }
}
```

### Related

- HTTP equivalent: [`POST /process`](http-api.md#post-process--run-end-to-end-pipeline)
- CLI equivalent: [`vesma processor run`](cli-reference.md#processor)

---

## `vesma_compress`

Compress large content (tool output, logs, JSON) with **zero data loss**. The original is cached in the `ccr_cache` SQLite table keyed by its SHA-256 hash; the compressed output embeds a short parseable marker so the LLM can call `vesma_retrieve` to fetch the full original back on demand. Achieves 70–90% token reduction on typical logs and JSON.

Content shorter than `min_size_chars` (default 500) is returned as-is — not cached, not compressed (tiny content has no token savings).

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `text` | string | **yes** | — | Content to compress. ≥500 chars to cache. |
| `profile` | string | no | auto | One of `log`, `terminal`, `code`, `docs`, `web`, `default`. Auto-detected if omitted. |
| `project` | string | no | `""` | Project slug to scope the cache entry. |
| `agent` | string | no | — | **A2 issuer ledger:** your agent slug, recorded on the cache row as the issuer so strict marker validation can later prove the marker was minted in your context. |
| `session` | string | no | — | **A2 issuer ledger:** your session id, stored alongside `agent` as the issuer pair. |

### Output

```json
{
  "compressed_text": "[compressed: a1b2... | 30000→900 chars | retrieve via vesma_retrieve]\n...filtered content...",
  "hash": "a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef12345678",
  "original_size": 30000,
  "compressed_size": 900,
  "reduction_pct": 97.0,
  "marker": "[compressed: a1b2... | 30000→900 chars | retrieve via vesma_retrieve]",
  "cached": true,
  "profile": "log"
}
```

### Marker format

```text
[compressed: <sha-256-hash> | <N>→<M> chars | retrieve via vesma_retrieve]
```

The marker is the only overhead added on top of the filtered content. It is short, parseable, and LLM-friendly. The hash is content-addressed, so re-compressing the same text is a no-op (the cache entry is reused). The issuer pair recorded with `agent`/`session` belongs to the FIRST writer of the `(project, hash)` row — a later session re-compressing identical content receives a marker that strict validation binds to that first issuer (fail-closed; harmless, since the re-compressor already holds the content).

### Example

Compress a 30K-line build log → ~900 chars in the context window. When the LLM needs the full traceback, it calls `vesma_retrieve` with the hash from the marker.

### Related

- HTTP equivalent: [`POST /compress`](http-api.md#post-compress--compress-content)

---

## `vesma_retrieve`

Retrieve the original uncompressed content for a CCR marker hash. If `query` is omitted, returns the full original. If `query` is provided, returns FTS5-ranked snippets from within the cached original — useful when the original is large and only a few lines are relevant.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `hash` | string | **yes** | — | SHA-256 hash from a `[compressed: ...]` marker. |
| `query` | string | no | — | Search query for snippet retrieval. |
| `snippet_count` | integer | no | `5` | Number of snippets when `query` is provided. |
| `project` | string | no | — | Project slug: scope the lookup to this project's entries — a hash cached under another project is reported as not found. |
| `validate_marker` | boolean | no | `ccr.validate_markers` | **A2 strict mode:** validate the marker before any content is issued. |
| `original_chars` | integer | no | — | `N` from the `[compressed: <hash> | N→M chars]` marker — enables the integrity check. |
| `agent` | string | no | — | Your agent slug — the trusted issuer context for the provenance check. |
| `session` | string | no | — | Your session id, paired with `agent` as the trusted issuer context. |

### A2 strict marker validation

A request is **marker-shaped** when it carries any of `original_chars` / `agent` / `session` — the metadata a harness parses out of a marker plus its own identity. When strict mode is on (`validate_marker=true`, or the `ccr.validate_markers` config knob), a marker-shaped request must pass three checks BEFORE any content is issued (ArchCom 2026-08-27, decision `archcom-2026-08-27-deferrals-triage`):

1. **existence** — the entry must exist under `(project, hash)`; strict validation REQUIRES the `project` scope: `validate_marker=true` (or the knob) without `project` is refused with `reason="marker validation failed: existence: project scope required for marker validation"` and no content (an unscoped lookup would redeem against the first-stored copy of any project);
2. **integrity** — the marker's `original_chars` must equal the character length of the stored original;
3. **provenance** — the row's issuer ledger (recorded at compress time via `agent`/`session`) must match your `(agent, session)` pair: a `null` session matches only a `null` issuer session, never a wildcard.

Any failed check returns the refused shape with `reason="marker validation failed: <check>: <detail>"` and **no content** (fail-closed). Reasons are FIXED non-oracle strings — they never echo the stored original length or the stored issuer pair (a reason leaking those is a two-call oracle that defeats provenance). Rows stored without issuer identity (legacy migrations, identity-less compress) fail full-shape validation with the distinct `unverifiable legacy marker` reason. **Hash-only closure (review F2):** in strict mode a hash-only retrieve of an issuer-stamped row is refused with `reason="marker validation required"` — stripping the optional args cannot bypass the gate; legacy NULL-issuer rows stay redeemable hash-only with a WARNING (unverifiable by construction; refusing would brick pre-A2 caches). Plain hash-only retrieves on knob-off deployments are unaffected. A refused validation does not bump `retrieval_count`.

For `vesma_assemble_context` with `expand_ccr=true`: pass `agent` alongside `session` so the expansion runs under your issuer context; without a full `(agent, session)` identity a strict deployment SKIPS the expansion of issuer-stamped markers (the marker stays — the model keeps the on-demand handle); legacy NULL-issuer rows still expand. The CCR stage stats carry `skipped_refused` for these.

Residual (accepted, ADR-0018 residual register): a trusted harness with compress access can still seed content inside its own project and redeem the marker from the same identity — single-operator threat model; revisit on the first multi-principal trigger.

### Output (full retrieval)

```json
{
  "hash": "a1b2...",
  "found": true,
  "original": "...full original text...",
  "size_bytes": 30000,
  "retrieval_count": 2
}
```

### Output (snippet retrieval)

```json
{
  "hash": "a1b2...",
  "found": true,
  "query": "Traceback",
  "snippets": [
    {"text": "Traceback (most recent call last):", "rank": 1.0},
    {"text": "  File \"app.py\", line 42, in handler", "rank": 0.8}
  ],
  "retrieval_count": 3
}
```

If the hash is absent from the cache (e.g. evicted by TTL or LRU), `found` is `false` with a `reason` field.

### Related

- HTTP equivalent: [`POST /retrieve`](http-api.md#post-retrieve--retrieve-a-ccr-cached-original)

---

## `vesma_align_prefix`

**CacheAligner (P1-5)** — relocate dynamic content (ISO timestamps, UUIDs, session ids, short-lived tokens, calendar dates) from system-prompt-like text to a `--- Dynamic context ---` block at the end, so the prefix stays byte-identical across requests and provider KV caches (Anthropic `cache_control`, OpenAI prefix caching) hit. Inspired by headroom's CacheAligner (https://github.com/headroomlabs-ai/headroom, Apache 2.0). Original implementation — no headroom code imported.

When CacheAligner is disabled in config, the text is returned unchanged with an empty `extracted` list.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `text` | string | **yes** | — | System-prompt-like text to stabilize. |
| `profile` | string | no | `default` | One of `code`, `docs`, `default`. Toggles which dynamic kinds are extracted. `code` and `docs` skip bare tokens (avoid mangling long identifiers or hyphenated words); `default` extracts all kinds. |

### Output

```json
{
  "aligned_text": "You are a senior engineer.\n\n--- Dynamic context ---\n- timestamp: 2026-07-17T10:30:00Z\n- session_id: sess-abc123def456\n",
  "extracted": [
    {"kind": "timestamp", "value": "2026-07-17T10:30:00Z", "start": 24, "end": 44},
    {"kind": "session_id", "value": "sess-abc123def456", "start": 60, "end": 78}
  ],
  "prefix_stabilized": true,
  "moved_chars": 38
}
```

- `aligned_text` — the input with dynamic spans removed and a `--- Dynamic context ---` block appended at the end, listing each extracted value with its kind.
- `extracted` — the list of extracted spans (`kind`, `value`, `start`, `end` in the *original* text).
- `prefix_stabilized` — `true` when at least one span was extracted from the prefix region (i.e. the aligned prefix is longer than the original prefix up to the first dynamic span).
- `moved_chars` — total characters relocated (sum of span lengths).

### Example

Input:
```text
You are a senior engineer. Today is 2026-07-17T10:30:00Z. Session: sess-abc123def456.
[stable rules follow...]
```

Aligned output (prefix up to the first dynamic span is now byte-stable across requests):
```text
You are a senior engineer. Today is . Session: .
[stable rules follow...]

--- Dynamic context ---
- timestamp: 2026-07-17T10:30:00Z
- session_id: sess-abc123def456
```

### Profile behaviour

| Profile | Skips | Why |
|---------|-------|-----|
| `default` (or omitted) | nothing | extract all kinds |
| `code` | `token` | bare 20+ char tokens would mangle long identifiers / hashes in code |
| `docs` | `token` | prose rarely contains real tokens; avoids mangling long hyphenated words |

The profile's skip set merges (union) with any per-kind toggles from `CacheAlignerConfig` — disabling a kind in config widens what a profile already skips.

### Config

```yaml
cache_aligner:
  enabled: true               # master switch
  extract_timestamps: true   # ISO 8601 timestamps
  extract_uuids: true        # canonical 8-4-4-4-12 UUIDs
  extract_session_ids: true  # sess-*, session:*, sid-*
  extract_dates: true        # calendar dates 2026-07-17 / 2026/07/17
  extract_tokens: true       # bare 20+ char opaque tokens
```

A kind whose toggle is `false` is added to the skip set and stays in-place (not relocated).

### Related

- Architecture: [overview.md#cachealigner-p1-5](../architecture/overview.md#cachealigner-p1-5)
- Config reference: [config.example.yaml](../../../config.example.yaml)

---

## `vesma_filter`

Run or refresh the Context Filter (M10) on an existing memory and return its `clean_content`. Useful when auto-filter was off at ingest, or to re-filter with a different profile.

The tool is the **issuance-gated** twin of the maintenance primitive: only `published` / `processed` memories are filterable into context (`raw` / `processing` / `archived` refuse fail-closed), an optional caller `project` scope fails closed on mismatch, and the returned `clean_content` is secret-scanned — refuse mode drops the content entirely, redact mode returns the redacted copy plus counts.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `memory_id` | string | **yes** | — | ID of the memory to filter. |
| `profile` | string | no | auto-detected | Context Filter profile. See [context-filter.md#profiles](context-filter.md#profiles). |
| `budget` | integer | no | — | Token budget for truncation. |
| `project` | string | no | — | Caller project slug — the memory must belong to it (mismatch fails closed). Omit for operator semantics. |

### Output

```json
{
  "memory_id": "550e8400-e29b-41d4-a716-446655440000",
  "profile": "terminal",
  "clean_content": "...filtered text...",
  "stats": { "...": "filter pipeline stats" },
  "redactions": 0
}
```

When `redactions` > 0 the response also carries `redacted_patterns` (pattern names only — matched values are never echoed).

### Example call

```json
{
  "jsonrpc": "2.0",
  "id": 12,
  "method": "tools/call",
  "params": {
    "name": "vesma_filter",
    "arguments": {
      "memory_id": "550e8400-e29b-41d4-a716-446655440000",
      "profile": "terminal"
    }
  }
}
```

### Errors

Error payloads carry a `reason` field:

| `reason` | Cause |
|----------|-------|
| `not_found` | No memory with this id. |
| `status_gate` | Memory status is not `published` / `processed` (or is quarantined). |
| `project_scope` | Memory does not belong to the caller's `project`. |
| `no_content` | Memory has no content to filter. |
| `refused` | The secret scan refused the echoed content (no content is returned). |

### Related

- [context-filter.md](context-filter.md) — profiles, pipeline stages, auto-filter behaviour (the profile list lives there — not duplicated here)
- HTTP equivalent: [`POST /filter/{memory_id}`](http-api.md#post-filtermemory_id--apply-the-5-stage-context-filter)
- CLI equivalent: [`vesma filter`](cli-reference.md#filter)

---

## Checkpoint reminder (auto-injected)

Every non-save tool call returns its normal payload **plus** an optional reminder string when one of the auto-collect thresholds is hit:

```text
... normal result ...

⚠️ [vesma] 12 tool calls since last checkpoint (970s ago). Consider calling vesma_save_context to preserve your current progress.
```

This is informational; nothing in Vesma blocks the call. Disable by setting `VESMA_AUTO_COLLECT=0` (the default).

---

## Server update notice (auto-injected, once)

A server upgraded under live sessions is invisible to them. On the **first
tool dispatch after process start**, Vesma compares the running version with
the `last_reported_server_version` stamp in the store; when they differ, that
one response carries an extra non-blocking line — `vesma server updated:
<old> → <new>` — and the stamp is rewritten, so the notice appears exactly
once per upgrade, never per call. A store error skips the notice silently
(it is a courtesy, not a failure mode).

---

## Tag contract reminder

The `vesma_add` and `vesma_ingest_url` tools reject calls that violate the M2 contract. The three required tag families are:

| Tag | Format | Cardinality | Purpose |
|-----|--------|-------------|---------|
| `project:<slug>` | `[a-z0-9][a-z0-9\-_]{0,63}` | exactly 1 | Binds to a codebase / initiative |
| `agent:<slug>` | `[a-z0-9][a-z0-9\-_]{0,63}` | exactly 1 | Authoring agent |
| `mnemos:<subtype>` | `[a-z][a-z0-9\-]*` | at least 1 | Cognitive category (namespace — unchanged data contract) |

Valid `mnemos:` subtypes: `session`, `bug-pattern`, `learning`, `decision`, `rule`, `open-question`, `checkpoint`, `legacy`.

Optional scope tag (ADR-0027 Phase 0): `task:<slug>` (`[a-z0-9][a-z0-9\-_]{0,63}`, at most 1) narrows the entry to one task scope — see [tag-contract.md](tag-contract.md#task--task-scope-multi-context-memory-adr-0027-phase-0).

Full reference: [tag-contract.md](tag-contract.md).

---

## Output token reduction (P1-7)

`vesma_add`, `vesma_search`, and `vesma_recall_context` accept two optional parameters that steer the caller's output style without changing what Vesma stores or returns:

| Parameter | Values | What it does |
|-----------|--------|--------------|
| `verbosity` | `default`, `terse`, `minimal` | Injects an output-style guidance suffix into the tool result framing. `terse` asks for brief, no-preamble output; `minimal` asks for facts only. |
| `effort` | `low`, `medium`, `high` | Injects a reasoning-effort hint. `low` flags a routine step (minimal reasoning); `high` asks for deliberate reasoning and verification. |

These are **hints passed through to the caller**, not model config changes. They are inspired by headroom's output token reduction work. Original implementation.

### Backward compatibility

- Both parameters are optional. Omitting them uses the config defaults (`default_verbosity=default`, `default_effort=medium`).
- The defaults (`default` / `medium`) produce an empty guidance suffix — the tool result is byte-identical to the pre-P1-7 output.
- Invalid values (e.g. `"verbose"`, `"turbo"`) are validated against the allowed frozensets, logged at `WARNING`, and fall back to the config default — graceful degradation, never raises.

### Config

```yaml
output_style:
  enabled: true              # master switch; when false, steering is a no-op
  default_verbosity: default # default when caller omits verbosity
  default_effort: medium     # default when caller omits effort
```

When `output_style.enabled` is `false`, both resolvers return the no-op defaults regardless of caller input.

### Example

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "tools/call",
  "params": {
    "name": "vesma_search",
    "arguments": {
      "query": "cache aligner prefix stability",
      "verbosity": "terse",
      "effort": "low"
    }
  }
}
```

The tool result carries the normal payload **plus** a short guidance suffix:

```text
... normal search results ...

---
*Output style: terse. Be brief. No preambles, no restated context, no ceremony. Lead with the result. Omit explanations the caller already has.*
*Effort: low — routine step, minimal reasoning.*
```

---

## `vesma_assemble_context`

**ADR-0017 D1 provider contract (vesma #125, Wave 1)** — one call assembles the model-facing context block for a pre-LLM-call injection. Any MCP-capable harness gains standardized context assembly instead of adapter-private recall.

Fixed pipeline, in order (recorded verbatim in `stats.stages`):

1. **recall** — hybrid RRF (FTS5 + vector) via the standard search path; the entry-invariant status gate means only `published` / `processed` memories surface (`raw` and DLQ content is unreachable). A `file` contributes the recall query and pins applyTo-scoped rule memories to the top.
2. **ccr** *(optional, `expand_ccr=true`)* — inline `[compressed: <hash> | …]` markers found in recalled content are expanded via project-scoped retrieval, budget-aware: an original that would not fit the budget stays compressed (the marker remains; the model can call `vesma_retrieve` on demand).
3. **filter** — the 5-stage context filter per block (auto-detected profile).
4. **scan** *(mandatory)* — every block passes the issuance secret scan; redacted spans (`<REDACTED:<pattern>>`) are counted per block; refuse mode (`ccr.retrieve_refuse_on_secret`) drops the block (fail-closed). Nothing enters the assembled output unscanned.
5. **align** — CacheAligner relocates dynamic content to each block's tail (runs before provenance wrapping so the provenance line stays parseable).
6. **budget** — whole provenance-wrapped blocks are included greedily in rank order under the token budget; blocks that do not fit are skipped whole (never truncated mid-block).

Every injected block carries a provenance line, exact format:

```text
[mnemos:<memory-id> project=<slug> status=<status> origin=<source> pipeline=<phase> v=<n> retrieved=<iso8601>]
```

`pipeline=` is omitted when the row's `pipeline_state` is NULL (legacy rows).
`retrieved=` is session-scoped (#282): stamped on the session's first
assembly and stable across all later assemblies of the same session, so
the block prefix is byte-stable for harness-side KV caching.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `session` | string | **yes** | — | Caller's session identifier (echoed in the result; identifies the assembly, not the memories). |
| `project` | string | **yes** | — | Project slug scoping recall and CCR redemption. |
| `agent` | string | no | — | Caller agent slug — pairs with `session` as the issuer context: with it, the CCR expansion stage runs under strict marker validation; without it a strict deployment skips expansion of issuer-stamped markers (the marker stays; legacy NULL-issuer rows still expand). |
| `file` | string | no | — | File path: contributes the recall query and pins applyTo-matching rule memories to the top. |
| `budget` | integer | no | `2048` | Token budget for the assembled block. |
| `mode` | string | no | `sync` | `sync` (default) / `async` (store the result, return a handle) / `code` / `prose` (sync delivery + filter recall candidates by the content type captured at ingest). |
| `expand_ccr` | boolean | no | `false` | Enable the optional CCR marker-expansion stage. |
| `async_handle` | string | no | — | Fetch (and pop) a result stored by a previous `mode="async"` call. Session-bound: only the assembling session may redeem; mismatch → error, handle not consumed. |

### Output

```json
{
  "session": "sess-42",
  "project": "my-project",
  "file": null,
  "mode": "sync",
  "content_type": null,
  "text": "[mnemos:3f2a… project=my-project status=published retrieved=2026-08-27T10:00:00+00:00]\nDeployment guide…",
  "blocks": [
    {
      "memory_id": "3f2a…",
      "project": "my-project",
      "status": "published",
      "score": 0.0114,
      "search_type": "hybrid",
      "content_type": "prose",
      "provenance": "[mnemos:3f2a… project=my-project status=published retrieved=2026-08-27T10:00:00+00:00]",
      "content": "Deployment guide…",
      "tokens": 96,
      "redactions": 1,
      "redacted_patterns": {"aws-key": 1},
      "ccr_expanded": false,
      "ccr_hashes": []
    }
  ],
  "tokens": {"budget": 2048, "estimated": 96},
  "stats": {
    "stages": ["recall", "ccr", "filter", "scan", "align", "budget"],
    "recall": {"query": "my-project", "query_source": "derived",
                "candidates": 3, "admissible": 3,
                "content_type_filtered": 0,
                "content_type_fallbacks": 1, "applyto_pinned": 0},
    "ccr": {"enabled": false, "markers_found": 0, "expanded": 0,
             "skipped_missing": 0, "skipped_budget": 0, "skipped_refused": 0},
    "filter": {"profiles": {"default": 1, "code": 1}},
    "scan": {"blocks_scanned": 2, "blocks_refused": 0},
    "align": {"blocks_aligned": 1, "moved_chars": 24},
    "budget": {"budget": 2048, "estimated_tokens": 96,
                "blocks_included": 2, "blocks_skipped": 0}
  }
}
```

For `mode="async"` the call returns only a handle envelope (`{"mode": "async", "handle": "<hex>", "status": "ready", "note": …}`); pass `async_handle` on a later call to fetch the stored result (one-shot: a handle can be fetched once, and only by the session that assembled it — a cross-session fetch is denied without consuming the handle).

### Notes

- **Boundary validation** — invalid `session` / `project` / `mode` / `budget`, a non-string `file`, an unknown `async_handle`, or an `async_handle` owned by a different session returns an `{"error": …}` dict (REST twin answers 422).
- **contentType partition** — `mode=code` keeps candidates whose ingest-time `detect_profile` was `code`; `mode=prose` keeps the rest (binary partition). Legacy rows without stored metadata are classified on the fly and counted in `recall.content_type_fallbacks`.
- **Budget partitioning (addendum 2)** — the budget stays monolithic in this wave; an active-state line reserved before recall allocation waits for the D5 baseline corridor.
- **Async registry** — in-memory, per-manager, capped (oldest evicted); entries are session-bound (CWE-863: the handle is a bearer token, so only the assembling session may redeem); a server restart drops pending handles.
- **`ccr_hashes`** — per-block observability: the content-addressed origin hashes of the CCR markers expanded into that block (empty when none). The provenance wrapper format is unchanged — it names the outer memory.

### Related

- REST twin: `POST /context/assemble` (same manager path) — [http-api.md](http-api.md)
- Pipeline rationale: ADR-0017 (D1), ADR-0018 (entry invariant: scan + provenance + status gate on every LTM → context entry)
- CCR: [`vesma_compress`](#vesma_compress) / [`vesma_retrieve`](#vesma_retrieve)

---

## `vesma_context_rewrite`

**ADR-0018 `on_context_rewrite` lifecycle event (vesma #125, Wave 2)** — the harness reports that it *rewrote* a block of its working context. The original of the replaced block is the source of truth: it is stored to long-term memory losslessly through the **normal knowledge pipeline** and becomes rehydratable through the **existing** scanned/gated channels. Harness compaction becomes lossless when originals land in the provider.

Semantics (ADR-0018, verbatim):

- **Idempotent** — re-delivery of the same event performs no duplicate writes. The idempotency key is content-addressed: SHA-256 over the length-prefixed canonical tuple `project/agent/session/supersedes/content`, persisted as `metadata["rewrite_event_key"]` and looked up *before* any write. The advisory `diff` is deliberately excluded from the key — it is not load-bearing, so a re-delivery carrying a different diff is still the same event. Two identical blocks replaced in two different sessions are two events (`session` participates in the key).
- **Version-less** — no ordering promise, no version chains. Replacement lineage is a `supersedes` edge (Phase 1 minimal `memory_edges` surface); traversal/expansion is Phase 2 (ADR-0017 D2).
- **Pipeline entry** — the original enters at `raw` via `MemoryManager.add`; it is context-reachable only after the pipeline advances it to `processed`/`published` (the `CONTEXT_ADMISSIBLE_STATUSES` gate). The Layer-1 write-path secret scan runs on `content` (a hit auto-tags `mnemos:no-federate`; zero-loss — the original is stored unchanged). The advisory diff gets its own Layer-1 verdict (`rewrite_diff_scan_verdict`: clean/hit/unknown) and a hit also tags the record `mnemos:no-federate` — otherwise the advisory payload would federate unflagged through a channel that only scans `content`.
- **Rehydrate = existing channels** — rewrite-stored originals surface through `vesma_retrieve` / `vesma_assemble_context` (scan-at-issuance, provenance, status gate). There is deliberately no new retrieval path.
- **Marker** — the CCR marker stays in the harness window (caller-side). Set `include_marker=true` to also receive the compress marker for the original; rehydrate of that marker goes through `vesma_retrieve` (project-scoped, issuance-scanned).

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `content` | string | **yes** | — | Original text of the replaced context block — the source of truth, stored unchanged. |
| `project` | string | **yes** | — | Project slug (tag `project:<slug>`). |
| `agent` | string | **yes** | — | Agent slug (tag `agent:<slug>`). |
| `session` | string | no | — | Session id — provenance metadata and part of the idempotency key. |
| `supersedes` | string | no | — | Memory id of the block being replaced — creates the `supersedes` edge new → old (must exist; also part of the event key). |
| `diff` | string | no | — | Advisory was→becomes diff — stored as metadata, never load-bearing, never echoed. |
| `include_marker` | boolean | no | `false` | Also return the CCR compress marker for the original. |

### Output

```json
{
  "status": "stored",
  "memory_id": "3f2a…",
  "memory_status": "raw",
  "event_key": "9c1d…",
  "project": "my-project",
  "agent": "my-agent",
  "session": "sess-42",
  "supersedes": {"to_memory_id": "a17b…", "edge_created": true}
}
```

`status` is `stored` (first delivery; `memory_status` is `raw` — the pipeline has not run yet) or `deduplicated` (re-delivery: same `memory_id`, no new writes; the idempotent edge insert reports `edge_created: false`). `ccr_marker` (the full `vesma_compress` result) appears only when `include_marker=true`. The receipt carries **no version or ordering fields** — by design (version-less event).

### Notes

- **Boundary validation** — empty `content`/`project`/`agent`, blank optional strings, a tag-contract violation (strict mode), a size-cap violation (`content` > `mnemos.context_rewrite_max_content_chars`, default 1 MiB; `diff` > `mnemos.context_rewrite_max_diff_chars`, default 256 KiB), or a `supersedes` target **not found in the caller's project** returns an `{"error": …}` dict (REST twin answers 422). The supersedes message deliberately does not distinguish "no such memory" from "memory of another project" — no global existence oracle.
- **Write-surface rate limit** — `mnemos.context_rewrite_rate_limit_per_minute` (default 30, 0 disables) counts STORED events per `(project, session)` in a rolling minute; over-limit returns `{"error": …, "rate_limited": true}` (REST 429). Deduplicated re-deliveries perform no write and consume no quota — retry storms stay harmless.
- **Stored tags** — `project:<slug>`, `agent:<slug>`, `mnemos:session` (closest existing subtype for live session material; a dedicated `mnemos:context-rewrite` subtype is a tag-contract vocabulary change deferred to the committee), plus `mnemos:no-federate` on any secret hit.
- **Provenance metadata** — `metadata["source"] = "context-rewrite"`, `rewrite_session`, `rewrite_event_key`, and (when supplied) `rewrite_diff` + `rewrite_diff_scan_verdict`.
- **Single-tenant trust model** — the harness is trusted software; the provider guarantees storage, scanning, gating and provenance, not replacement policy (pinned zones, budgets and replace-event emission stay harness-side).

### Related

- REST twin: `POST /context/rewrite` (same manager path) — [http-api.md](http-api.md)
- Rationale: ADR-0018 (§"on_context_rewrite": lifecycle event, not a versioned primitive)
- Rehydrate channels: [`vesma_retrieve`](#vesma_retrieve) / [`vesma_assemble_context`](#vesma_assemble_context); marker via [`vesma_compress`](#vesma_compress)

---

## `vesma_hooks`

**Lifecycle hooks (ADR-0017 D1 / ADR-0018, vesma #125 Wave 3)** — the automation integration points, grouped behind `action:enum` (the vesma #97 grouped-tool pattern). Three actions, one tool:

- **`pre_llm_call`** — assemble the context block to **inject before a model call** (thin wrapper over `vesma_assemble_context`, delivery pinned to sync). `context_hint` (what the upcoming call is about) is used as the recall query instead of the derived project/file term. `task` (ADR-0027 Phase 0, epic #308) is the harness-passed task identifier — the bare task slug: it narrows recall to entries tagged `task:<slug>` (intersection doctrine — a task condition only narrows, never widens) and composes the per-call assembled tail only; pinned prefixes and the provenance format are untouched. The ADR-0018 entry invariant — secret scan, provenance, status gate — runs inside the assemble pipeline; the hook adds nothing to it. With `include_awareness=true` (vesma #254, default `false` — off means byte-identical output), the awareness delta section AND the swarm v0a/v0b operational picture (same-project peers: counts/ids/timestamps only, plus each peer's claimed task — swarm v0b, a self-reported `task:<slug>` claim rendered in a labeled `[unverified]` sub-section) are appended LAST, never pinnable, and the awareness cursor advances; the picture renders below the delta section (see [`vesma_awareness`](#vesma_awareness)).
- **`on_session_start`** — recall recent checkpoints for session bootstrap (thin wrapper over the recall path; the echoed content is scanned at issuance on this channel, mirroring `vesma_recall_context`).
- **`post_tool_call`** — the **autocompression entry point** (ADR-0018): when `auto_compress` resolves true (per-call argument, else the `hooks.auto_compress` config knob, default `false`), the tool output is compressed via CCR and the marker-headed `compressed_text` is returned — the caller **substitutes** it for the raw output in its window. Off by default: the envelope says so and nothing is written.

**Identity mandate (A2 register N2, loudly):** `session` + `project` + `agent` are required on EVERY call. For `post_tool_call` this is a security requirement, not ergonomics — the compress call always threads the caller's `(agent, session)` onto the cache row (the A2 issuer ledger), so strict marker validation (`ccr.validate_markers`) can later prove the marker was minted in the redeemer's own context. Identity-less compression would mint NULL-issuer rows that strict validation refuses to redeem — the hook has no identity-less mode.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `action` | string | **yes** | — | `pre_llm_call` / `on_session_start` / `post_tool_call`. |
| `session` | string | **yes** | — | Caller session id. |
| `project` | string | **yes** | — | Project slug. |
| `agent` | string | **yes** | — | Caller agent slug (issuer identity). |
| `context_hint` | string | no | — | `pre_llm_call`: what the upcoming model call is about — the explicit recall query. FTS5 whole-phrase semantics: the hint is matched as ONE quoted phrase (adjacent tokens in order), not a keyword set. |
| `file` | string | no | — | `pre_llm_call`: optional file path (recall terms + applyTo rule pinning). |
| `task` | string | no | — | `pre_llm_call` (ADR-0027 Phase 0): optional task scope — the bare task slug (`[a-z0-9_-]{1,64}`, no `task:` prefix). Narrows recall to entries tagged `task:<slug>`; tail-only. |
| `budget` | integer | no | `2048` | `pre_llm_call`: token budget. |
| `limit` | integer | no | `5` | `on_session_start`: checkpoint count. |
| `tool_name` | string | `post_tool_call` | — | The tool that produced the output. |
| `output_text` | string | `post_tool_call` | — | The raw tool output to compress. |
| `auto_compress` | boolean | no | knob | `post_tool_call`: per-call override of `hooks.auto_compress`. |
| `profile` | string | no | auto | `post_tool_call`: filter profile hint for the compression. |

### Output

`pre_llm_call` returns the full `vesma_assemble_context` result plus `hook`/`injection` keys (inject `text` before the model call). `on_session_start` returns `{hook, session, project, agent, checkpoints: [{id, content, created_at, redactions, redacted_patterns?}], redactions}` — checkpoint content is issuance-scanned; refuse mode drops the checkpoint. `post_tool_call` with autocompression on returns the CCR envelope (`ccr`, `compressed_text`, `marker`, `compressed`, `action: "substitute …"`); with it off, `{auto_compress: false, compressed: false, note}` and no write.

### Notes

- **Config** — two knobs: `hooks.auto_compress` (default `false`) and `hooks.max_output_chars` (default 1,048,576 chars — `post_tool_call` rejects an oversized `output_text` at the boundary BEFORE any write, mirroring the context-rewrite caps convention; `0` disables). The read-only hooks need no enablement; they expose no capability the server surfaces do not already have.
- **Sync only (this wave)** — ADR-0017 D1 names sync/async hook modes; async delivery waits for a consumer that needs it. Harnesses needing `async`/`code`/`prose` assembly modes call `vesma_assemble_context` directly.
- **Memory capture is explicit** — `post_tool_call` does not silently store tool outputs as memories; use `VesmaSDK.remember` (or `vesma_add`/REST) when a result is worth keeping.
- **Errors** — boundary violations return `{"error": …}` (REST twin answers 422; unknown action is 404 there). An over-cap `output_text` is a boundary violation: `{"error": "output_text exceeds hooks.max_output_chars (N > M)"}`, nothing written.

### Related

- REST twin: `POST /hooks/{action}` — [http-api.md](http-api.md)
- Programmatic surface: `VesmaSDK` ([integration-guide.md](integration-guide.md))
- Rationale: ADR-0017 D1 (lifecycle integration), ADR-0018 (post_tool_call autocompression, residual register N2)

---

## `vesma_awareness`

**Awareness pre-flight (vesma #254, R3; swarm v0a — ArchCom 2026-09-27)** — the surface a parallel session calls BEFORE a risky operation (the PR #224 contract: a release closed by an invisible parallel session). Two actions, one tool:

- **`pre_flight`** (read-only) — server-observed neighbor activity: presence (who is active), delta (what changed since your cursor — one line per neighbor agent), lexical conflict hints against your last checkpoint goal, and the **operational picture** (swarm v0a/v0b): an explicit block of same-project peers where each observed line carries the agent id, last observed activity, record count in the 900 s presence window, and checkpoint presence — **counts, agent ids and timestamps only**. No title, no body, no tag of a peer record ever enters the OBSERVED layer. Swarm v0b adds each peer's CLAIMED active task — the `task:<slug>` tag (ADR-0027) of its most recent task-tagged row: a client-supplied claim that rides the two-level-trust machinery exactly like goals (issuance-scanned fail-closed, policy markers stripped, rendered in a separate labeled `self-reported` sub-section with an inline `[unverified]` qualifier, never inside the observed header or the blocks, and named by the picture's disclaimer as a self-reported claim). Strictly project-scoped (`project=None` fails closed; cross-project visibility does not exist — no parameter, no flag). The awareness cursor advances ONLY via `vesma_hooks` `pre_llm_call` with `include_awareness=true` — a pre-flight never marks neighbor entries as consumed.
- **`record_abstention`** — attribute an abstention-on-presence as an ACTION with a reconstructable provenance chain (abstention → delta-block → checkpoint-id → writer-session); pass `basis_checkpoint_id` from the pre-flight response.

**Presence is behavioral metadata** (agent ids, activity timestamps, record counts) — the search gates cover record CONTENT and do not apply to presence. The picture is descriptive only (who / what count / when), never predictive, and it is data, never governance: picture blocks carry no `memory_id`, are never pinnable, and no `applyTo:`/`severity:` semantics ride along. Zero picture-derived records are stored (cursors ride the meta table, actions ride traces); any future awareness-derived record is born `mnemos:no-federate`.

**Rate cap (C9):** picture/awareness queries are capped per `(project, agent)` at `vesma.awareness_picture_rate_limit_per_minute` (default 30, `0` disables). Over-limit DEGRADES to a one-line "rate-limited, retry later" section — the response keeps its shape; never a hard error.

### Input

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | string | **yes** | `pre_flight` / `record_abstention`. |
| `session` | string | **yes** | Caller session id. |
| `project` | string | **yes** | Project slug (fail-closed; strictly same-project). |
| `agent` | string | **yes** | Caller agent slug. |
| `basis_checkpoint_id` | string | `record_abstention` | The neighbor checkpoint id the abstention is based on. |
| `note` | string | no | `record_abstention`: optional free-text note (capped, scanned at issuance). |

### Output

`pre_flight` returns `{action, project, presence, delta, picture, conflict_hints, text, disclaimer, cursor_advanced: false}` — `picture.agents` carries `{agent, last_seen, entries, checkpoint, task}` per same-project peer (capped to 8, most recent first; `agents_capped_from` makes truncation observable; `task` is the peer's claimed task slug or `null` — swarm v0b self-reported layer, dropped fail-closed when the issuance scan refuses or redacts it). The picture rides ONCE, at the top level (#452): `presence` carries its agents summary WITHOUT a nested picture. Conflict hints use a Unicode tokenizer (#451): word characters of any alphabet (Cyrillic included), dotted version tails stay one token (`v4.0.0`), hyphens split (`qa-vesma-5x` → `qa`/`vesma`/`5x`); a minimal RU stopword set rides the EN one. `record_abstention` returns the trace id and the full provenance chain.

### Notes

- **Errors** — boundary violations return `{"error": …}`; the REST twin answers 422.
- The fixed R3 disclaimer frame rides verbatim in every rendered section: presence claims must not defer work without operator coordination.

### Related

- Composition: `vesma_hooks` `pre_llm_call` / `on_session_start` with `include_awareness=true` (the picture renders last, below the delta section)
- REST twin: `POST /hooks/{action}` with `include_awareness` — [http-api.md](http-api.md)

---

## Native awareness heartbeat (ADR-0035)

**The doorbell contour** — awareness of peer activity reaches the agent NATIVELY, without manual invocation and without any change in foreign harnesses: a delta-gated, observed-only awareness tail attached to the responses of ALL MCP tools through a single injection point (the `call_tool` wrapper). Delivery happens on the FIRST tool call after a peer write — cost scales with peer activity, not with call count; the delta check itself is a sub-millisecond `SELECT EXISTS` probe, so quiet stores pay one index lookup per call.

The tail is one appended `TextContent` after the handler (never inline, lane=awareness, tail-LAST per the cache contract). A deny-list of surfaces never carries it: `vesma_assemble_context` (it already composes the full picture — a tail there would mean double render and double cursor advance), `vesma_export` and `vesma_import` (the bulk transfer pair). The REST leg carries no tail in v1.

### Mode ladder (`awareness.native_heartbeat_mode`)

| Mode | Probe/compose | Rendered tail | Events | Notes |
|------|---------------|---------------|--------|-------|
| `off` *(default)* | no | no | no | The kill switch: engine behaviour is byte-identical to the pre-ADR-0035 build (CI-pinned). |
| `shadow` | yes | **no** | yes | Wave 0: the whole contour is computed and logged in the metrics sidecar, nothing reaches the agent. |
| `canary` | yes | yes | yes | Wave 1: team machines only, kill-switch ready. |
| `on` | yes | yes | yes | Wave 2: the default flips only after the wave 0/1 gates close green. |

Canonical env override: `VESMA_AWARENESS__NATIVE_HEARTBEAT_MODE=shadow`. The rate cap knob `awareness.heartbeat_rate_limit_per_minute` (default 30, `0` disables) caps compositions per `(project, agent)` per minute; over-limit suppresses the tail with an event — never an error.

### The envelope (canary/on)

Non-empty peer delta → a fixed-CEILING block (≤120 tokens, enforced by observable truncation): a header, the R3 disclaimer verbatim, at most 8 observed-only lines — one per peer: sanitized agent id, entry count, minute-precision last-seen; no goal text, no record ids, no numeric scores (the ORDER is the relevance signal: my-goal overlap → checkpoint → recency → agent id, deterministic) — and exactly one descriptive flag line pointing at `vesma_awareness` for depth. Empty delta → ONE deterministic calm-line (~10 tokens, timestamp-free): "quiet" is no longer indistinguishable from "the eye is off".

The delivery cursor (`awrh:` namespace, keyed `(project, agent)`, session-free) advances strictly BEFORE the response returns — at-most-once delivery; a retry sees "no delta". Advancements are logged with identity.

### Events (shadow metrics)

The contour writes zero-content events into the metrics sidecar (90-day retention): `peer_write` (write-class verbs), `delta_available`, `heartbeat_delivery` (tool, lines, token estimate, `state: calm|delta`, cursor before/after), `heartbeat_suppressed` (reason: `rate_cap` / `probe_error` / …), and `tool_call` (name, ts, session — the funnel denominator). No peer content ever lands in an event (counts, enums and the caller's identity slugs only).

### Related

- Decision record: [ADR-0035](../../project/adr/0035-native-awareness-delivery.md)
- Depth surface: [`vesma_awareness`](#vesma_awareness); hooks composition: `vesma_hooks` `pre_llm_call` with `include_awareness=true`
- Config: [config.example.yaml](../../../config.example.yaml) — the `awareness` section

---

## `vesma_export`

Export memories to a file on disk. Thin wrapper over the CLI `vesma export` logic. Returns metadata only — the export content is **never** returned inline (the stdio transport cannot carry a binary SQLite tarball or a large JSON blob over the JSON-RPC stdout channel).

Federation defence-in-depth (#86) is inherited automatically because the tool wraps the same `run_export` function as the CLI and HTTP surfaces: records tagged `mnemos:no-federate` are excluded from the export, and detected secrets in passing records are replaced with `<REDACTED:<pattern_name>>`.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `output_path` | string | **yes** | — | Absolute path where the export file is written. |
| `format` | enum `json` \| `sqlite` | no | `json` | `json` = metadata-only export (filters apply); `sqlite` = full `tar.gz` snapshot (filters ignored). |
| `compress` | enum `none` \| `gzip` | no | `none` | Compression mode. (`zstd` is CLI-only.) |
| `project` | string | no | — | Filter by project slug (json only). |
| `agent` | string | no | — | Filter by agent slug (json only). |
| `status` | enum `raw` \| `processing` \| `processed` \| `published` \| `archived` | no | — | Filter by memory status (json only). |
| `tags` | array of string | no | — | Filter by tags (json only). |
| `since` | string (ISO-8601) | no | — | Only memories created on or after this date (json only). |
| `until` | string (ISO-8601) | no | — | Only memories created before this date (json only). |
| `encrypt` | boolean | no | `false` | When `true`, encrypt the output. The passphrase is read from the `VESMA_EXPORT_PASSPHRASE` environment variable. |

### Returns

```json
{
  "path": "/abs/path/to/backup.json",
  "memory_count": 42,
  "format": "json",
  "compress": "none",
  "encrypted": false,
  "bytes": 18234,
  "warnings": []
}
```

### Security note

- **Passphrase via environment, never in arguments.** When `encrypt=true`, the server reads the passphrase from the `VESMA_EXPORT_PASSPHRASE` environment variable. Passing the passphrase value in `output_path` or any other argument would leak it into MCP logs — never do this.
- **No inline content.** The tool writes to `output_path` and returns metadata only. Read the file from disk to inspect the export.
- **`#86` inheritance.** `mnemos:no-federate` records are excluded; secrets in passing records are redacted. No extra configuration needed.

### Example

```json
{
  "jsonrpc": "2.0",
  "id": 8,
  "method": "tools/call",
  "params": {
    "name": "vesma_export",
    "arguments": {
      "output_path": "/tmp/mnemos-backup.json",
      "format": "json",
      "project": "vesma",
      "compress": "gzip"
    }
  }
}
```

For an encrypted full snapshot:

```json
{
  "name": "vesma_export",
  "arguments": {
    "output_path": "/tmp/mnemos-snapshot.tar.gz",
    "format": "sqlite",
    "encrypt": true
  }
}
```

(With `VESMA_EXPORT_PASSPHRASE` set in the server's environment.)

---

## `vesma_import`

Import memories from an export file. Thin wrapper over the CLI `vesma import` logic. Two modes: **merge** (insert new, skip or overwrite existing) and **restore** (wipe all then import — destructive, requires `confirm=true`).

Import validation (#86) is inherited automatically: schema drift, oversized content, invalid tags, and prompt-injection patterns are handled by the same `run_import` function the CLI and HTTP surfaces use.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `source_path` | string | **yes** | — | Absolute path to the export file to import. |
| `mode` | enum `merge` \| `restore` | no | `merge` | `merge` = insert new / skip-or-overwrite existing; `restore` = wipe all then import (requires `confirm=true`). |
| `overwrite` | boolean | no | `false` | Overwrite existing memories (merge mode only). |
| `confirm` | boolean | no | `false` | **Required `true` for `restore` mode** (hard gate — restore wipes all existing data). |
| `dry_run` | boolean | no | `false` | Validate without writing; returns a validation report. |
| `passphrase_env` | string | no | — | Name of the environment variable holding the decryption passphrase (NOT the value). |

### Returns

```json
{
  "mode": "merge",
  "dry_run": false,
  "imported": 12,
  "skipped": 3,
  "updated": 0,
  "errors": [],
  "warnings": [],
  "format_version": "1.0",
  "mnemos_version": "4.0.0"
}
```

### Security note

- **Passphrase via environment variable name, never the value.** `passphrase_env` takes the *name* of the environment variable (e.g. `"MY_IMPORT_PASS"`), and the server reads `os.environ["MY_IMPORT_PASS"]`. Passing the passphrase value as the argument would leak it into MCP logs.
- **Restore requires `confirm=true`.** Without it the tool returns an error and does not touch the live data. Restore wipes all memories, vectors, and projects.
- **`#86` inheritance.** Schema drift is rejected; oversized content (>1 MiB) is rejected; invalid tags raise a tag-contract error; prompt-injection patterns are logged at WARNING (not blocked — content may legitimately discuss injection).

### Example

```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "method": "tools/call",
  "params": {
    "name": "vesma_import",
    "arguments": {
      "source_path": "/tmp/mnemos-backup.json",
      "mode": "merge",
      "overwrite": false
    }
  }
}
```

Restore (destructive) with confirmation:

```json
{
  "name": "vesma_import",
  "arguments": {
    "source_path": "/tmp/mnemos-snapshot.tar.gz",
    "mode": "restore",
    "confirm": true
  }
}
```

Encrypted import (with `VESMA_IMPORT_PASS` set in the server's environment):

```json
{
  "name": "vesma_import",
  "arguments": {
    "source_path": "/tmp/encrypted.bin",
    "mode": "merge",
    "passphrase_env": "VESMA_IMPORT_PASS"
  }
}
```

---

## `vesma_workflow`

Workflow lifecycle management for a memory (vesma #96). Separates mutable **workflow state** (open → in-progress → done, blocked/resolved, terminal states) from the append-only **tag classification** (`project:X`, `mnemos:decision`). The tag layer stays append-only; this layer is the mutable work lifecycle.

Action-based dispatch — the same `action: enum` pattern as `vesma_tags`. The state machine and the five guardrails are enforced **server-side** in `MemoryManager.workflow_set`; this tool (and the REST `POST /memories/{id}/workflow`) are thin wrappers that cannot bypass it.

### States and transitions

```mermaid
stateDiagram-v2
    [*] --> open
    open --> in_progress
    open --> withdrawn
    in_progress --> blocked
    in_progress --> done
    in_progress --> withdrawn
    blocked --> resolved
    blocked --> withdrawn
    resolved --> in_progress
    resolved --> done
    resolved --> withdrawn
    done --> [*]
    withdrawn --> [*]
```

- **`blocked → done` is forbidden** — a stuck dependency must go through `resolved` first (blocked → resolved → done). This is the headline forbidden edge; an agent cannot silently skip a blocker by jumping straight to a terminal state.
- **`done` and `withdrawn` are terminal** — no further transitions are permitted from either.
- A memory that has never had its workflow set (legacy row or freshly created) is treated as `open` for the first transition.

### Input

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `action` | enum `set` \| `get` \| `history` | **yes** | — | `set` transitions the status; `get` returns the current status + lock owner; `history` returns the audit trail. |
| `memory_id` | string | **yes** | — | Target memory id. |
| `to` | enum `open` \| `in-progress` \| `blocked` \| `resolved` \| `done` \| `withdrawn` | `set`: **yes** | — | Target status. `blocked → done` is forbidden. |
| `actor` | string | `set`: **yes** | — | Free-form actor id. **Phase 1 weak identity — NO authn/authz.** |
| `reason` | string | `set` + `force=true`: **yes** | `""` | Human-readable reason. Required when `force=true`. |
| `force` | boolean | no | `false` | Override a lock held by another actor (guardrail 4 — requires `reason`). |
| `limit` | integer | no | `50` | Max history rows (`history` only). |

### Guardrails (enforced server-side)

| # | Guardrail | Behaviour |
|---|-----------|-----------|
| G1 | **Audit log** | Every recorded transition writes a `memory_workflow_history` row (`from`, `to`, `actor`, `reason`, `force_used`, `created_at`). **Rejected transitions** (forbidden edge, lock conflict, rate-limit, force-without-reason) write **no** audit row — the log records state changes, not attempts. |
| G2 | **Stale-lock auto-release** | A lock older than `workflow_stale_lock_threshold_hours` (default `24`) is auto-releasable by a different actor — no `force` needed. Logged at WARNING. |
| G3 | **Idempotent transitions** | Setting `to=X` when the memory is already `X` is a **no-op** (no write, no audit row). Returns `idempotent: true`, `recorded: false`. |
| G4 | **Force-unlock** | `force=true` overrides a foreign lock; `force_used=1` is recorded in the audit log. **`reason` is required** — blank reason is rejected. |
| G5 | **Rate limit** | More than `workflow_rate_limit_per_minute` transitions (default `30`) on one memory in a minute is rejected. The limit is **per-memory, not per-actor** — churn on a single memory is throttled regardless of which actor drives the transitions. |

### Output

**`action: set`** (a transition result):

```json
{
  "memory_id": "01HXYZ...",
  "from_status": "open",
  "to_status": "in-progress",
  "actor": "agent-dba",
  "previous_locked_by": null,
  "locked_by": "agent-dba",
  "locked_at": "2026-07-31T12:00:00+00:00",
  "stale_lock_released": false,
  "force_used": false,
  "idempotent": false,
  "recorded": true,
  "reason": "",
  "terminal": false
}
```

**`action: get`** (current projection — `workflow_status` normalises unset → `open`):

```json
{
  "memory_id": "01HXYZ...",
  "workflow_status": "in-progress",
  "locked_by": "agent-dba",
  "locked_at": "2026-07-31T12:00:00+00:00"
}
```

**`action: history`** (audit trail, newest first):

```json
{
  "memory_id": "01HXYZ...",
  "history": [
    {
      "id": "uuid...",
      "memory_id": "01HXYZ...",
      "from_status": "open",
      "to_status": "in-progress",
      "actor": "agent-dba",
      "reason": "",
      "force_used": 0,
      "created_at": "2026-07-31T12:00:00+00:00"
    }
  ]
}
```

### Errors

- **Missing `memory_id`** → `{"error": "memory_id is required ..."}`.
- **`action: set` missing `to` or `actor`** → `{"error": "action='set' requires 'to' ..."}` / `"... requires 'actor' ..."`.
- **Unknown `action`** → `{"error": "unknown action 'X'. Valid actions: 'set', 'get', 'history'"}`.
- **Forbidden transition / guardrail violation** (e.g. `blocked → done`, lock held by another actor without `force`, force without `reason`, rate limit) → `{"error": "<verbatim manager message>"}`. Over REST these map to HTTP `409`; over the MCP tool they are returned as the `error` field.
- **Memory not found** (`get`) → `{"error": "memory 'X' not found"}`.

### Lock semantics

| Target status | Lock effect |
|---------------|-------------|
| `in-progress` | Acquires the lock (owner = `actor`, timestamp refreshed). |
| `blocked` / `resolved` | Keeps the lock; on a takeover (`force` / stale-release) the owner becomes `actor` and the stale-clock restarts. |
| `open` / `done` / `withdrawn` | Releases the lock (`locked_by` and `locked_at` cleared). |

### Example

Start work on a memory:

```json
{
  "name": "vesma_workflow",
  "arguments": {
    "action": "set",
    "memory_id": "01HXYZ...",
    "to": "in-progress",
    "actor": "agent-dba"
  }
}
```

Hit a blocker, then resolve and finish:

```json
{"name": "vesma_workflow", "arguments": {"action": "set", "memory_id": "01HXYZ...", "to": "blocked", "actor": "agent-dba", "reason": "waiting on upstream spec tag"}}
{"name": "vesma_workflow", "arguments": {"action": "set", "memory_id": "01HXYZ...", "to": "resolved", "actor": "agent-dba"}}
{"name": "vesma_workflow", "arguments": {"action": "set", "memory_id": "01HXYZ...", "to": "done", "actor": "agent-dba"}}
```

Force-override a stale lock held by another actor:

```json
{
  "name": "vesma_workflow",
  "arguments": {
    "action": "set",
    "memory_id": "01HXYZ...",
    "to": "in-progress",
    "actor": "agent-dba",
    "force": true,
    "reason": "previous actor unreachable for >24h"
  }
}
```

### Phase 1 — weak identity

`actor` is a **free-form string with no authn/authz** in Phase 1. Any caller may claim any actor id; the guardrails (stale-lock, force, rate limit) are the only protection. A future phase will bind `actor` to an authenticated principal; until then, treat the workflow layer as advisory coordination, not a security boundary.

### REST equivalent

The same lifecycle is exposed over HTTP, nested under the memory (not a top-level `/status`):

| Method | Path | Maps to |
|--------|------|---------|
| `GET` | `/memories/{memory_id}/workflow` | `workflow_get` (404 if memory missing) |
| `POST` | `/memories/{memory_id}/workflow` | `workflow_set` (body: `to`, `actor`, `reason`, `force`; `409` on guardrail violation) |
| `DELETE` | `/memories/{memory_id}/workflow` | `workflow_set(... to="withdrawn")` — **cancel / withdraw** (terminal, irreversible). Ends the workflow in `withdrawn`; the lock is cleared as a side effect of reaching a terminal state. `actor` query param required; `force` overrides a foreign lock. |

`DELETE` is a **cancel / withdraw** — it ends the workflow in the terminal `withdrawn` state (no further transitions possible). It is **not** a lock-release-to-resumable: the state machine has no edge back to `open`, so the memory is not returned to a resumable state. To **finish** work normally, use `POST` with `to=done`.

### Related

- [http-api.md](http-api.md) — the nested `/memories/{id}/workflow` REST endpoints
- [tag-contract.md](tag-contract.md) — the append-only classification layer (distinct from this mutable lifecycle layer)
- ArchCom 2026-07-18 session 2 — the `action: enum` pattern + nested REST naming decision

---

## See also

- [getting-started.md](getting-started.md) — wiring `mcp.json` and the first call
- [http-api.md](http-api.md) — the same capabilities over HTTP
- [cli-reference.md](cli-reference.md) — the same capabilities over the CLI
- [tag-contract.md](tag-contract.md) — M2 schema enforced by `vesma_add`
- [security.md](../admin/security.md) — SSRF guard, secrets hygiene
- [architecture overview](../architecture/overview.md#mcp-server) — server lifecycle

---

_Last updated: 2026-09-05_
