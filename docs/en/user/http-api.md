# HTTP API Reference

**🌐 Language / Язык:** English · [Русский](../../ru/user/http-api.md)

> Complete reference for the Vesma HTTP API — memory CRUD, search, pipeline, DLQ, context filter, traces, path-scoped rules, the project graph (ADR-0032), and the A2A Sessions API (M16).

The HTTP server is a FastAPI app served by Uvicorn. Start it with:

```bash
vesma serve --host 127.0.0.1 --port 8000
```

| Resource | URL |
|----------|-----|
| Swagger UI | `http://HOST:PORT/docs` |
| ReDoc | `http://HOST:PORT/redoc` |
| OpenAPI 3.1 schema | `http://HOST:PORT/openapi.json` |

> **Default bind is `127.0.0.1`.** Do not expose this port to a public network without putting a reverse proxy with authentication in front. See [security.md](../admin/security.md) for the threat model.

> **Authentication** — when `api.auth_enabled=true`, all routes except `/health`, `/auth/login`, `/auth/verify`, `/docs`, `/redoc`, and `/openapi.json` require a valid session token (either `Authorization: Bearer <session>` header or the `mnemos_session` cookie; the header takes precedence). Use `POST /auth/login` to obtain a session. See the [Authentication](#authentication) section below.

> **CORS** — disabled by default. When `api.cors_enabled=true`, the CORS middleware is registered as the outermost layer so OPTIONS preflight requests are answered before auth. Set `cors_allow_origins` to an explicit list of allowed origins; combining `["*"]` with `cors_allow_credentials=true` is rejected at startup.

For the same capabilities over other transports, see [mcp-tools.md](mcp-tools.md) (MCP) and [cli-reference.md](cli-reference.md) (CLI). For higher-level context, see [architecture overview](../architecture/overview.md). The A2A contract is also documented in [a2a-sessions.md](../architecture/a2a-sessions.md) (link to the design rationale).

---

## Conventions

- All bodies are JSON unless noted.
- All timestamps are ISO 8601 in UTC (`2026-06-15T10:42:00+00:00`).
- IDs are UUIDs unless noted (A2A sessions use `conv-YYYY-MM-DD-<short>`).
- Errors are JSON of shape `{"detail": "..."}`.
- Standard HTTP status codes only — no custom error codes.
- `200 OK` and `201 Created` carry a JSON body. `204 No Content` is used for deletes that return no body.

---

## Status codes

| Code | Meaning in this API |
|------|---------------------|
| `200` | OK (default success) |
| `201` | Created (POST that inserts a row) |
| `400` | Bad request (e.g. trying to publish a non-`processed` memory; project-graph budget refusal) |
| `404` | Not found (memory_id, cluster_id, dlq_id, session_id, turn_id) |
| `401` | Unauthorised (missing or invalid session token; only when `api.auth_enabled=true`) |
| `403` | Forbidden — policy refusal: project-graph confinement (path escapes the registered root, or unregistered project, PG2) on `/graph/*` and `/watch/start` |
| `413` | Payload too large — project-graph index limit breach (fail-closed, PG7) on `/graph/index` |
| `422` | Unprocessable entity (Pydantic validation failure on the request body) |
| `500` | Internal server error (see server logs) |
| `503` | Service unavailable — auth not initialised (fail-closed AuthMiddleware), or the project-graph master flag `code_graph.enabled` is off (operator gate on `/graph/*` and `/watch/start`) |

---

## Authentication

> **Gated by `api.auth_enabled`.** All four endpoints are mounted at `/auth`. When `api.auth_enabled=false` (default) these routes still exist but the middleware does not enforce credentials on other routes.

The auth model uses **opaque bearer tokens** (prefix `mnk_`) with optional TOTP 2FA. Tokens are stored as PBKDF2-HMAC-SHA256 digests; the plaintext is shown once at `vesma auth token create` and never again. Sessions are issued after a successful login (+ TOTP verify when `api.totp_enabled=true`) and carry the same `Authorization: Bearer <session>` shape.

### `POST /auth/login` — begin session

**Request body**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `token` | string | **yes** | Opaque bearer token (`mnk_...`). |

**Response 200 — TOTP disabled**

```json
{
  "session": "mnk_session_...",
  "expires_at": "2026-06-18T02:00:00+00:00"
}
```

**Response 200 — TOTP enabled**

```json
{
  "challenge_id": "chal_a1b2c3d4",
  "ttl_sec": 120
}
```

When TOTP is enabled the session is **not** issued here; call `POST /auth/verify` next with the `challenge_id` and the 6-digit TOTP code.

**Errors**

| Code | Cause |
|------|-------|
| `401` | Unknown token or disabled token. |
| `429` | Rate limit exceeded (5 req / min per IP or per token hash). |

### `POST /auth/verify` — complete TOTP challenge

**Request body**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `challenge_id` | string | **yes** | From the `POST /auth/login` response. |
| `code` | string | **yes** | 6-digit TOTP code. |

**Response 200**

```json
{
  "session": "mnk_session_...",
  "expires_at": "2026-06-18T02:00:00+00:00"
}
```

Also sets `Set-Cookie: mnemos_session=...; HttpOnly; Secure; SameSite=Strict`.

**Errors**

| Code | Cause |
|------|-------|
| `401` | Invalid or expired challenge, wrong TOTP code. |
| `429` | Rate limit exceeded (5 req / min per challenge). |

### `POST /auth/logout` — invalidate session

Requires a valid session (header or cookie). Deletes the server-side session row and clears the cookie.

**Response 200**

```json
{"ok": true}
```

### `GET /auth/me` — current session info

Requires a valid session.

**Response 200**

```json
{
  "token_id": "tok_...",
  "totp": false,
  "expires_at": "2026-06-18T02:00:00+00:00"
}
```

---

## Health and metrics

### `GET /health`

Liveness probe.

**Response 200**

```json
{"status": "ok"}
```

### `GET /metrics`

Prometheus-style metrics (M5 observability). Currently returns the same shape as `GET /memories` aggregate stats:

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

---

## Tags

### `GET /tags` — list tags with counts

Returns every distinct tag in the memories store with its usage count.

**Response 200** — array of tag objects

| Field | Type | Description |
|-------|------|-------------|
| `tag` | string | Full tag string (e.g. `project:vesma`). |
| `count` | int | Number of memories carrying this tag. |

Sorted by `count` descending; ties are broken by `tag` ascending (alphabetical).

**Example**

```bash
curl -s http://127.0.0.1:8000/tags
```

```json
[
  {"tag": "project:vesma", "count": 142},
  {"tag": "agent:tech-writer", "count": 58},
  {"tag": "mnemos:learning", "count": 41}
]
```

### `POST /tags/rename` — bulk rename a tag prefix

Renames every tag matching `from_prefix:<subtype>` → `to_prefix:<subtype>` (the GCW → vesma migration case). Mirrors the `mnemos_tags_rename` MCP tool and `vesma tags rename` CLI. Safe by construction: plain `UPDATE` (the FTS5 external-content index stays consistent), and `dry_run` defaults to `true` — nothing is written unless the caller passes `dry_run: false`.

**Request body**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `from_prefix` | string | — | Source prefix (e.g. `gcw`). |
| `to_prefix` | string | — | Target prefix (e.g. `vesma`). |
| `subtypes` | string[] \| null | `null` | Optional whitelist of subtypes to rename. |
| `dry_run` | bool | `true` | Preview without writing. |
| `project` / `agent` | string \| null | `null` | Scope the rename to one project / agent. |
| `invalid_subtypes_to_legacy` | bool | `false` | Rename invalid subtypes to `<to_prefix>legacy` instead of skipping. |

**Response 200** — the rename report (preview or applied), including the `changed` count.

---

## Memories CRUD

### `POST /memories` — create memory

M2 tag contract is enforced server-side. The endpoint derives `project` and `agent` from your tags and stores them as denormalised columns for fast filtering.

**Request body** — see [MemoryCreate](../architecture/overview.md#data-model)

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `content` | string | **yes** | — | Primary text. |
| `title` | string | no | auto | Short title. |
| `tags` | string[] | **yes** | — | Must include `project:<slug>`, `agent:<slug>`, and at least one `vesma:<subtype>`. |
| `source` | string | no | `manual` | One of `manual`, `web`, `file`, `mcp`, `obsidian`, `cli`, `rule`, `synthesized`. |
| `source_url` | string | no | — | Origin URL. |
| `memory_type` | string | no | `note` | One of `note`, `fact`, `snippet`, `bookmark`, `conversation`, `session_context`. |
| `status` | string | no | `raw` | One of `raw`, `processing`, `processed`, `published`, `archived`. |
| `filter_profile` | string | no | — | One of `log`, `terminal`, `code`, `docs`, `web`, `default`. |
| `metadata` | object | no | `{}` | Free-form key / value store. One reserved convention (ADR-0027 Phase 0): the doc-grouping triple `{doc_id, chunk_idx, heading_path}` is **all-or-nothing** — a metadata dict carrying any of the three keys must carry all three well-formed, otherwise the request is rejected 422 (write-side validation; a persisted half-triple would split a document once the docs-as-memory reader groups by `doc_id`). |
| `category` | string | no | — | Free-form category label. |

**Response 201** — full [`Memory`](#memory-schema) object.

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/memories \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Use uv, not pip — it resolves transitive CVE closure correctly.",
    "tags": ["project:vesma", "agent:tech-writer", "mnemos:learning"]
  }'
```

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "content": "Use uv, not pip — it resolves transitive CVE closure correctly.",
  "title": "Use uv, not pip",
  "tags": ["project:vesma", "agent:tech-writer", "mnemos:learning"],
  "source": "manual",
  "memory_type": "note",
  "status": "raw",
  "project": "vesma",
  "agent": "tech-writer",
  "created_at": "2026-06-15T10:42:00+00:00",
  "updated_at": "2026-06-15T10:42:00+00:00",
  "raw_content": null,
  "metadata": {}
}
```

**Errors**

| Code | Cause |
|------|-------|
| `422` | Missing required tag (`project:`, `agent:`, or `vesma:`) |
| `500` | SQLite / vault write failure |

### `GET /memories/{memory_id}` — read one

**Path parameters**

| Name | Type | Description |
|------|------|-------------|
| `memory_id` | UUID | Memory identifier. |

**Query parameters**

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `include_raw` | bool | `false` | If true, include `raw_content`. |

**Response 200** — full [`Memory`](#memory-schema) object.

**Response 404** — `{"detail": "Memory <id> not found"}`.

**Example**

```bash
curl -s http://127.0.0.1:8000/memories/550e8400-e29b-41d4-a716-446655440000
```

### `GET /memories` — list recent

**Query parameters**

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `status` | string | — | Filter by `MemoryStatus` enum value. |
| `project` | string | — | Restrict to a project slug. |
| `task` | string | — | ADR-0027 Phase 2 (epic #308): optional task scope — the bare slug (`[a-z0-9_-]{1,64}`, no `task:` prefix). Byte-identical to appending `task:<slug>` to `tags`; composes with `tags` by intersection. Invalid slugs → `400`. |
| `limit` | int | `20` | Max rows. Hard cap `500`. |

**Response 200** — array of [`Memory`](#memory-schema) (without `raw_content`).

**Example**

```bash
curl -s "http://127.0.0.1:8000/memories?project=vesma&limit=10"
```

---

## Workflow lifecycle

Three routes drive a memory's workflow state machine (`open → in-progress → blocked / resolved / done / withdrawn`; `blocked → done` is forbidden, terminal states are final). Every transition is recorded in the audit log.

### `GET /memories/{memory_id}/workflow` — current status

Returns the current workflow status and lock owner. `workflow_status` is normalised to `open` when the memory never had its workflow set. `404` when the memory does not exist.

### `POST /memories/{memory_id}/workflow` — transition status

**Request body**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `to` | string | — | Target status (`open`, `in-progress`, `blocked`, `resolved`, `done`, `withdrawn`). |
| `actor` | string | — | Caller identity (required; recorded in the audit log). |
| `reason` | string | `""` | Free-form reason (required when `force=true`). |
| `force` | bool | `false` | Override a lock held by another actor. |

State-machine / guardrail violations return `409`. The manager is the single source of truth — no validation is duplicated at the route.

### `DELETE /memories/{memory_id}/workflow` — withdraw (cancel)

`DELETE` is a **cancel**: the workflow ends in `withdrawn` — a terminal, irreversible state; the lock is cleared as a side effect, but the memory is not returned to a resumable state.

| Query param | Type | Default | Description |
|-------------|------|---------|-------------|
| `actor` | string | — | Required (`422` when missing). |
| `reason` | string | `"DELETE withdraw"` | Free-form. |
| `force` | bool | `false` | Override a foreign lock. |

---

## Search

RRF fusion of FTS5 and vector legs. Only `published` memories are searched by default.

**Query semantics:** the FTS5 leg treats the WHOLE `query` string as one quoted phrase (adjacent tokens, in order — `_build_fts_query` quotes the entire input). A keyword-set query like `postgres migration` matches only that exact phrase; to find individual keywords, issue separate single-term queries.

**Request body** — see `SearchQuery` in `src/vesmaro/models.py`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `query` | string | **yes** | — | Natural-language search string. Matched as ONE whole phrase by the FTS5 leg (see Query semantics above). |
| `tags` | string[] | no | — | Filter: all of these tags must be present. |
| `project` | string | no | — | Restrict to a project slug. |
| `task` | string | no | — | ADR-0027 Phase 2 (epic #308): optional task scope — the bare slug (`[a-z0-9_-]{1,64}`, no `task:` prefix). Byte-identical to `tags=["task:<slug>"]` (the F1 arm-C surface); composes with `tags` by intersection. Invalid slugs → `400` (normalized first: `My Task` → `my-task`). |
| `limit` | int | no | `20` | Max results. |
| `include_raw` | bool | no | `false` | If true, returns `raw_content` instead of cleaned content. |

**Response 200** — array of result dicts

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Memory id. |
| `title` | string | Auto / explicit title. |
| `content` | string | Cleaned content, or raw if `include_raw=true`. |
| `tags` | string[] | Tag list. |
| `score` | float | RRF score, higher is better. |
| `search_type` | string | Always `hybrid` here. |

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "embedding model", "project": "vesma", "limit": 5}'
```

```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "title": "Use uv, not pip",
    "content": "Use uv, not pip — it resolves transitive CVE closure correctly.",
    "tags": ["project:vesma", "agent:tech-writer", "mnemos:learning"],
    "score": 0.812,
    "search_type": "hybrid"
  }
]
```

---

## Per-agent recall (M3)

### `GET /recall/agent/{name}` — agent recall

Returns the most recent entries for a single agent, optionally filtered by project and / or a sub-query.

**Path parameters**

| Name | Type | Description |
|------|------|-------------|
| `name` | string | Agent slug. |

**Query parameters**

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `project` | string | — | Restrict to a project slug. |
| `q` | string | — | Optional FTS / vector sub-query. |
| `task` | string | — | ADR-0027 Phase 2 (epic #308): optional task scope — the bare slug (`[a-z0-9_-]{1,64}`, no `task:` prefix). Byte-identical to a `task:<slug>` tag filter; narrows the agent's entries to one task scope on both legs. Invalid slugs → `400`. |
| `limit` | int | `20` | Max rows. Hard cap `100`. |

**Response 200** — array of:

| Field | Type |
|-------|------|
| `id` | UUID |
| `title` | string |
| `content` | string |
| `tags` | string[] |
| `created_at` | ISO 8601 string |

**Example**

```bash
curl -s "http://127.0.0.1:8000/recall/agent/cr-security-reviewer?project=vesma&limit=5"
```

---

## Session context (save / recall)

These endpoints mirror the `mnemos_save_context` and `mnemos_recall_context`
plugin tools. They store and retrieve `session_context` memories tagged
`mnemos:checkpoint`, enabling an agent to restore its working state across
sessions or after context compaction.

### `POST /context/save` — save a session checkpoint

Builds structured Markdown from the supplied fields and stores it as a
`SESSION_CONTEXT` memory tagged `mnemos:checkpoint`. Mirrors the
`mnemos_save_context` plugin tool.

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project` | string | **yes** | — | Project slug to scope the checkpoint. |
| `goals` | string | no | — | One-sentence active goal. |
| `completed` | string | no | — | Bullets of completed work. |
| `in_progress` | string | no | — | Immediate next action. |
| `decisions` | string | no | — | Decisions worth surviving. |
| `context` | string | no | — | File paths, architecture notes, gotchas. |
| `agent` | string | no | `"user"` | Agent identity — the validated identity channel (non-empty string when provided, whitespace-only rejected). Must match the server-side session→agent binding when `session` is supplied. |
| `session` | string | no | — | Session id binding the checkpoint to a conversation; first presentation records the session→agent binding server-side (first writer wins). |
| `task` | string | no | — | ADR-0027 Phase 2 (epic #308): optional task scope — the bare slug (`[a-z0-9_-]{1,64}`, no `task:` prefix). Mints the `task:<slug>` tag on the checkpoint at the save boundary (at most one task per record); recall it with `task=` on `POST /context/recall` / `POST /search` / `GET /memories`. A dedup hit returns the first-minted row with ITS task scope. Invalid slugs → `400`. |

**Response 201**

```json
{
  "status": "saved",
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "title": "Session checkpoint — 2026-07-07T12:00:00+00:00",
  "duplicate": false
}
```

`duplicate` is `true` when an identical checkpoint for the same `(project, agent)` already exists: the existing id is returned and nothing new is stored (the response shape is additive — older clients may ignore the flag).

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/context/save \
  -H "Content-Type: application/json" \
  -d '{
    "project": "vesma",
    "goals": "Finish HTTP API docs for all 15 tools",
    "completed": "- Updated plugin.yaml\n- Updated prompt mode",
    "in_progress": "Document /context/save and /context/recall",
    "decisions": "- CCR endpoints go before Knowledge pipeline section",
    "context": "See docs/en/user/http-api.md"
  }'
```

**Errors**

| Code | Cause |
|------|-------|
| `400` | Identity validation failed (empty/whitespace `agent` or `session`), an unsalvageable `task` slug, or trivial-reject: all five payload fields are empty — nothing is stored |
| `409` | `session` is already bound to a different agent (server-side session→agent binding, first writer wins) |
| `422` | Missing required `project` field (Pydantic validation) |
| `500` | SQLite / vault write failure |

### `POST /context/recall` — recall session context

Returns the most recent checkpoint memories for a project, optionally
filtered by a sub-query. Mirrors the `mnemos_recall_context` plugin tool.

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project` | string | **yes** | — | Project slug to recall. |
| `query` | string | no | — | Optional FTS / vector sub-query to focus results. |
| `task` | string | no | — | ADR-0027 Phase 2 (epic #308): optional task scope — the bare slug (`[a-z0-9_-]{1,64}`, no `task:` prefix). Byte-identical to the checkpoint tag filter plus `task:<slug>` (the F1 arm-C surface): returns only checkpoints saved under that task. Invalid slugs → `400`. |
| `limit` | int | no | `5` | Max checkpoints to return. |

**Response 200 — with prior context**

```json
{
  "project": "vesma",
  "checkpoints": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "title": "Session checkpoint — 2026-07-07T12:00:00+00:00",
      "content": "# Session checkpoint — 2026-07-07T12:00:00+00:00\n\n## Goals\nFinish HTTP API docs for all 15 tools\n",
      "tags": ["project:vesma", "agent:user", "mnemos:checkpoint"],
      "created_at": "2026-07-07T12:00:00+00:00"
    }
  ]
}
```

**Response 200 — no prior context**

```json
{
  "project": "vesma",
  "checkpoints": [],
  "message": "No context found. Start by saving context with POST /context/save."
}
```

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/context/recall \
  -H "Content-Type: application/json" \
  -d '{"project": "vesma", "limit": 3}'
```

---

## Provider contract (ADR-0017 D1)

### `POST /context/assemble` — assemble the pre-LLM-call context block

Mirrors the `mnemos_assemble_context` MCP tool over the same manager path
(vesma #125, Wave 1). Fixed pipeline: hybrid RRF recall (entry-invariant
status gate — only `published`/`processed` surface) → optional CCR marker
expansion → context filter → **mandatory** secret scan (per-block redaction
counts; refuse mode drops the block) → CacheAligner → token budget. Every
injected block carries a provenance line
`[mnemos:<id> project=<slug> status=<status> origin=<source> pipeline=<phase> v=<n> retrieved=<iso>]`
(`pipeline=` is omitted on legacy rows with NULL pipeline_state).
`retrieved=` is session-scoped (#282): stamped on the session's first
assembly and stable across all later assemblies of the same session, so
the block prefix is byte-stable for harness-side KV caching.

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `session` | string | **yes** | — | Caller's session identifier (echoed in the result). |
| `project` | string | **yes** | — | Project slug scoping recall and CCR redemption. |
| `agent` | string | no | `null` | Caller agent slug — pairs with `session` as the issuer context: with it, CCR marker expansion runs under strict validation; without it a strict deployment skips expansion of issuer-stamped markers (legacy NULL-issuer rows still expand). |
| `file` | string | no | `null` | File path: recall query terms + applyTo rule pinning. |
| `budget` | integer | no | `2048` | Token budget (`1..1_000_000`). |
| `mode` | string | no | `sync` | `sync` / `async` (store + handle) / `code` / `prose` (contentType filter). |
| `expand_ccr` | boolean | no | `false` | Enable the optional CCR expansion stage. |
| `async_handle` | string | no | `null` | Fetch (once) a result stored by `mode="async"`. Session-bound: only the assembling session may redeem; mismatch → 422, handle not consumed. |

**Responses**

- `200` — the ContextBlock: `text` (provenance-wrapped blocks), `blocks[]`
  (per-block provenance, redaction counts, token counts), `tokens`
  (`budget`, `estimated`), `stats` (per-stage telemetry incl. the verbatim
  stage order; `stats.recall.query_source` is `explicit` when a query/
  `context_hint` drove recall and `derived` otherwise, and
  `stats.ccr.skipped_refused` counts markers whose strict-mode validation
  refused the expansion). For `mode="async"`: a handle envelope only.
- `422` — boundary validation failure (invalid `session`/`project`/`mode`/
  `budget`, unknown `async_handle`, or an `async_handle` owned by a
  different session).

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/context/assemble \
  -H "Content-Type: application/json" \
  -d '{"session": "sess-42", "project": "vesma", "file": "src/manager.py", "budget": 1024}'
```

Full field-by-field documentation: [`mcp-tools.md` → `mnemos_assemble_context`](mcp-tools.md#mnemos_assemble_context).

### `POST /context/rewrite` — report a context rewrite (ADR-0018)

Mirrors the `mnemos_context_rewrite` MCP tool over the same manager path
(vesma #125, Wave 2). The harness reports that it rewrote a block of its
working context; the original is stored to LTM through the normal
knowledge pipeline (enters `raw`, context-reachable only after the
pipeline advances it), idempotent by content-addressed event key,
version-less (replacement lineage is an optional `supersedes` edge).

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `content` | string | **yes** | — | Original text of the replaced block — the source of truth, stored unchanged. |
| `project` | string | **yes** | — | Project slug (tag `project:<slug>`). |
| `agent` | string | **yes** | — | Agent slug (tag `agent:<slug>`). |
| `session` | string | no | `null` | Session id — provenance metadata, part of the idempotency key. |
| `supersedes` | string | no | `null` | Memory id of the replaced block — creates the `supersedes` edge new → old (must exist). |
| `diff` | string | no | `null` | Advisory was→becomes diff — stored as metadata, never load-bearing. |
| `include_marker` | boolean | no | `false` | Also return the CCR compress marker for the original. |

**Responses**

- `200` — event receipt: `status` (`stored` / `deduplicated` — the event is
  idempotent, so re-delivery is 200, not 201), `memory_id`,
  `memory_status` (`raw` on first delivery), `event_key`, echoed
  `project`/`agent`/`session`, `supersedes`
  (`{"to_memory_id", "edge_created"}` or `null`), and `ccr_marker` when
  `include_marker=true`. No version or ordering fields — by design.
- `429` — write-quota exceeded: more than
  `mnemos.context_rewrite_rate_limit_per_minute` (default 30) STORED
  events for this `(project, session)` in the last minute. Deduplicated
  re-deliveries consume no quota.
- `422` — boundary validation failure (empty required fields, blank
  optional strings, size-cap violation — `content` > 1 MiB / `diff` >
  256 KiB by default, tag-contract violation in strict mode, or a
  `supersedes` target not found in this project — the message does not
  distinguish "not found" from "another project's memory").

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/context/rewrite \
  -H "Content-Type: application/json" \
  -d '{"content": "<original block text>", "project": "vesma", "agent": "zcode",
       "session": "sess-42", "supersedes": "3f2a…", "diff": "was: v1 → became: v2",
       "include_marker": true}'
```

Full field-by-field documentation: [`mcp-tools.md` → `mnemos_context_rewrite`](mcp-tools.md#mnemos_context_rewrite).

---

## Lifecycle hooks (ADR-0017 D1 / ADR-0018, #125 Wave 3)

One parametric route over the three lifecycle hooks — the REST twin of the
grouped `mnemos_hooks` MCP tool (both call the same `dispatch_hook` router).
Identity (`session`, `project`, `agent`) is mandatory on every call; for
`post_tool_call` it is the A2 register N2 mandate — the compress call always
threads the caller's `(agent, session)` onto the cache row so strict marker
validation can later prove provenance.

### `POST /hooks/{action}` — run one lifecycle hook

`action` ∈ `pre_llm_call` | `on_session_start` | `post_tool_call` (unknown →
404). Per-action fields are validated by the hooks boundary (`ValueError` →
422): `context_hint`/`file`/`budget`/`task` for `pre_llm_call`, `limit` for
`on_session_start`, `tool_name`/`output_text`/`auto_compress`/`profile` for
`post_tool_call`. `output_text` is capped at `hooks.max_output_chars`
(default 1,048,576 chars, the context-rewrite caps convention; `0` disables)
— an over-cap payload is rejected 422 BEFORE any write.

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `session` | string | **yes** | — | Caller session id. |
| `project` | string | **yes** | — | Project slug. |
| `agent` | string | **yes** | — | Caller agent slug (issuer identity). |
| `context_hint` | string | no | — | `pre_llm_call`: explicit recall query (what the model call is about). FTS5 whole-phrase semantics: the hint is matched as ONE quoted phrase (adjacent tokens in order), not a keyword set. |
| `file` | string | no | — | `pre_llm_call`: optional file path. |
| `task` | string | no | — | `pre_llm_call` (ADR-0027 Phase 0): optional task scope — the bare task slug (`[a-z0-9_-]{1,64}`, no `task:` prefix). Narrows recall to entries tagged `task:<slug>` (intersection doctrine; tail-only — pinned prefixes and the provenance format are untouched). |
| `budget` | integer | no | `2048` | `pre_llm_call`: token budget. |
| `limit` | integer | no | `5` | `on_session_start`: checkpoint count. |
| `tool_name` | string | `post_tool_call` | — | The tool that produced the output. |
| `output_text` | string | `post_tool_call` | — | The raw tool output to compress. |
| `auto_compress` | boolean | no | knob | `post_tool_call`: per-call override of `hooks.auto_compress` (default `false`). |
| `include_awareness` | boolean | no | `false` | `pre_llm_call`/`on_session_start` (vesma #254): compose the awareness delta section AND the swarm v0a/v0b operational picture (observed counts/ids/timestamps plus each peer's claimed task — a self-reported `[unverified]`-labeled claim) — appended LAST, never pinnable; the awareness cursor advances on `pre_llm_call` only. Off (default) = byte-identical output. Rate-capped per `(project, agent)` (`vesmaro.awareness_picture_rate_limit_per_minute`, default 30) — over-limit degrades to a rate-limit line, never a 5xx. |
| `profile` | string | no | auto | `post_tool_call`: filter profile hint. |

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/hooks/post_tool_call \
  -H "Content-Type: application/json" \
  -d '{"session": "sess-42", "project": "vesma", "agent": "zcode",
       "tool_name": "bash", "output_text": "<60+ lines of build log>",
       "auto_compress": true}'
```

Responses per action: `pre_llm_call` → the assembled ContextBlock (same shape
as `POST /context/assemble`, sync delivery pinned); `on_session_start` →
`{checkpoints: [{id, content, created_at, redactions, …}], redactions}`
(content issuance-scanned on this channel); `post_tool_call` → the CCR
envelope with `compressed_text`/`marker` to substitute (or the off-envelope
`{auto_compress: false, compressed: false}` when not enabled). With
`include_awareness=true`, `pre_llm_call` additionally returns an `awareness`
meta key and the rendered awareness + operational-picture sections ride the
`text` tail (see [`mcp-tools.md` → `mnemos_awareness`](mcp-tools.md#mnemos_awareness)).
Full
field-by-field documentation: [`mcp-tools.md` → `mnemos_hooks`](mcp-tools.md#mnemos_hooks).

---

## Reversible compression (CCR)

These endpoints implement **Compress-Cache-Retrieve (CCR)** — zero-data-loss
compression for large content (logs, JSON, command output). The original is
cached keyed by its SHA-256 hash; a short parseable marker is embedded in
the compressed text so the full original can be retrieved on demand.
Typical reduction is 70–90% on logs.

### `POST /compress` — compress content

Compresses `text` via CCR, caches the original, and returns the compressed
text plus a marker containing the hash. Content shorter than ~500 chars is
returned as-is (not cached). Mirrors the `mnemos_compress` plugin tool.

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `text` | string | **yes** | — | Content to compress. ≥500 chars to cache. |
| `profile` | string | no | auto | Filter profile: `log`, `terminal`, `code`, `docs`, `web`, `default`. Auto-detected if omitted. |
| `project` | string | no | `""` | Project slug to scope the cache entry. |
| `agent` | string | no | — | **A2 issuer ledger:** caller agent slug, recorded on the cache row as the marker issuer (for strict marker provenance). |
| `session` | string | no | — | **A2 issuer ledger:** caller session id, stored alongside `agent`. |

**Response 200**

```json
{
  "compressed_text": "[compressed: a1b2c3... 8.2KB → 0.9KB (89%)]\n## Top 5 lines by frequency\nERROR Connection refused (×42)\n...",
  "hash": "a1b2c3d4e5f6...",
  "original_size": 8200,
  "compressed_size": 900,
  "reduction_pct": 89.0,
  "marker": "[compressed: a1b2c3... 8.2KB → 0.9KB (89%)]",
  "cached": true,
  "profile": "log"
}
```

When `cached` is `false` (content < ~500 chars), `hash` is empty and
`compressed_text` equals the input `text`.

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/compress \
  -H "Content-Type: application/json" \
  -d '{"text": "<8KB of log output>", "profile": "log", "project": "vesma"}'
```

### `POST /retrieve` — retrieve a CCR-cached original

Retrieves the original uncompressed content for a CCR marker hash. If
`query` is omitted, returns the full original. If `query` is provided,
returns FTS5-ranked snippets from within the cached original — useful when
the original is large and only a few lines are relevant. Mirrors the
`mnemos_retrieve` plugin tool.

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `hash` | string | **yes** | — | SHA-256 hash from a `[compressed: ...]` marker. |
| `query` | string | no | — | Search query for snippet retrieval. |
| `snippet_count` | int | no | `5` | Number of snippets when `query` is provided. |
| `project` | string | no | — | Project slug: scope the lookup to this project's entries — a hash cached under another project is reported as not found. |
| `validate_marker` | boolean | no | `ccr.validate_markers` | **A2 strict mode:** validate the marker (existence + `original_chars` integrity + issuer provenance) before any content is issued. |
| `original_chars` | int | no | — | `N` from the `[compressed: <hash> | N→M chars]` marker — enables the integrity check. |
| `agent` | string | no | — | Caller agent slug — the trusted issuer context for the provenance check. |
| `session` | string | no | — | Caller session id, paired with `agent` as the trusted issuer context. |

**A2 strict marker validation.** A request is *marker-shaped* when it carries any of `original_chars` / `agent` / `session`. In strict mode a marker-shaped request must pass: (1) **existence** under `(project, hash)` — the `project` scope is REQUIRED in strict mode: `validate_marker=true` (or the knob) without `project` is refused with `reason="marker validation failed: existence: project scope required for marker validation"` and no content; (2) **integrity** — the marker's `N` equals the stored original's character length; (3) **provenance** — the cache row's issuer ledger (recorded at compress time) matches the caller's `(agent, session)` pair (`null` session matches only a `null` issuer session). A failed check returns:

```json
{
  "hash": "a1b2c3d4e5f6...",
  "found": true,
  "refused": true,
  "reason": "marker validation failed: provenance: issuer mismatch: ...",
  "redactions": 0,
  "redacted_patterns": {}
}
```

— no `original`/`snippets` content (fail-closed), and `retrieval_count` is not bumped. Reasons are fixed non-oracle strings — stored lengths and issuer pairs are never echoed. **Hash-only closure (review F2):** in strict mode a hash-only retrieve of an issuer-stamped row is refused with `reason="marker validation required"` (no content); legacy NULL-issuer rows stay redeemable hash-only with a WARNING. Plain hash-only retrieves on knob-off deployments are unaffected. `POST /context/assemble` with `expand_ccr=true` accepts `agent` (paired with `session`) so the expansion runs under the caller's issuer context; without a full identity a strict deployment skips expansion of issuer-stamped markers (the marker stays; the CCR stage stats carry `skipped_refused`). Residual: same-project seeding by a trusted compress caller is accepted (ADR-0018 residual register, single-operator).

**Response 200 — full retrieval (no `query`)**

```json
{
  "hash": "a1b2c3d4e5f6...",
  "found": true,
  "original": "<full original text>",
  "size_bytes": 8200,
  "retrieval_count": 1
}
```

**Response 200 — snippet retrieval (with `query`)**

```json
{
  "hash": "a1b2c3d4e5f6...",
  "found": true,
  "query": "Connection refused",
  "snippets": [
    "ERROR Connection refused (×42)",
    "WARN Retrying after Connection refused on host:3306"
  ],
  "retrieval_count": 1
}
```

**Response 200 — not found**

```json
{
  "hash": "000000000000...",
  "found": false,
  "retrieval_count": 0
}
```

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/retrieve \
  -H "Content-Type: application/json" \
  -d '{"hash": "a1b2c3d4e5f6...", "query": "Connection refused", "snippet_count": 3}'
```

---

## Auto-collect signal

### `GET /auto-collect` — compaction signal vector

Returns the in-process call counter / elapsed-time signals plus
client-populated heuristic slots. The `recommendation` field is
`"save_checkpoint"` when either the call counter or the elapsed-time signal
exceeds its threshold, else `"ok"`. Mirrors the `mnemos_auto_collect_status`
plugin tool.

**Response 200**

```json
{
  "auto_collect_enabled": true,
  "signals": {
    "call_counter": {
      "calls_since_save": 12,
      "threshold": 25,
      "triggered": false
    },
    "elapsed_secs": {
      "value": 3600,
      "threshold": 1800,
      "triggered": true
    },
    "context_size_heuristic": {"value": null, "note": "populated by client"},
    "summary_marker_detected": {"value": null, "note": "populated by client"},
    "reference_drop_heuristic": {"value": null, "note": "populated by client"}
  },
  "recommendation": "save_checkpoint",
  "next_reminder_in_calls": 13
}
```

**Example**

```bash
curl -s http://127.0.0.1:8000/auto-collect
```

---

## URL ingest

### `POST /ingest-url` — fetch and save a web page

Fetches a web page, extracts its main text (via trafilatura), and saves it
as a `RAW` memory. Credentials embedded in the URL are stripped before
storage (OWASP A02). Tags are validated through the project's tag contract.
Mirrors the `mnemos_ingest_url` plugin tool.

**Request body**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | **yes** | HTTP/HTTPS URL to fetch. |
| `tags` | string[] | **yes** | Must include `project:<slug>`, `agent:<slug>`, and at least one `vesma:<subtype>`. |

**Response 201**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "title": "FastAPI dependency injection guide",
  "url": "https://fastapi.tiangolo.com/tutorial/dependencies/"
}
```

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/ingest-url \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://fastapi.tiangolo.com/tutorial/dependencies/",
    "tags": ["project:vesma", "agent:tech-lead", "mnemos:learning"]
  }'
```

**Errors**

| Code | Cause |
|------|-------|
| `422` | Missing required tag (`project:`, `agent:`, or `vesma:`) or missing `url` |
| `500` | Fetch failure, extraction failure, or SQLite / vault write failure |

---

## Document ingest (ADR-0027 Phase 3)

### `POST /ingest-document` — ingest a document as chunked, quarantined rows

Docs-as-memory: the document text is split structure-preservingly into
heading-scoped chunks (each row carries the `{doc_id, chunk_idx,
heading_path}` metadata convention) and every chunk enters memory **born
quarantined** — ingested documents are untrusted content (ADR-0027
invariant 8), invisible to every recall/assembly path until the
danger-sweep at ingest-completion clears them.

**Lifecycle** (the ADR-0019 §5 discipline): born-quarantine → the Phase A
danger detector sweeps every chunk (clean chunks release to ordinary
`published` rows; flagged chunks stay quarantined with the detector class
codes in the operator-side reason; scanner errors fail closed) →
chunk-atomic release semantics (a clean chunk releases even if a sibling
is flagged) → issuance stays the last line (a released chunk contaminated
after release is still refused/redacted by the repeat secret scan on every
content-echoing channel — ADR-0027 invariant 7).

**Re-ingest** (ADR-0027 invariant 4): re-ingesting the same `doc_id`
replaces the document's chunk rows and bumps the doc-chunk `ccr_cache`
version key **in the same SQLite transaction**. Honest scope: the key is a
**consumer-facing invalidation counter** (exposed in `GET /stats` as
`doc_chunk_cache_version`, the same posture as `graph_epoch`); any
assembly-cache consumer must read it. No in-repo consumer keys on it yet.

Mirrors the `mnemos_ingest_document` plugin tool. `POST /ingest-url`
keeps its single-row pre-Phase-3 semantics — the boundary is deliberate.

**Request body**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | **yes** | Full document text to chunk and ingest. |
| `doc_id` | string | **yes** | Logical document identity; stable across re-ingest. |
| `tags` | string[] | **yes** | Must include `project:<slug>`, `agent:<slug>`, and at least one `vesma:<subtype>`. |
| `title` | string | no | Optional document title. |
| `source_url` | string | no | Optional provenance URL. |

**Response 201**

```json
{
  "doc_id": "dep-guide",
  "chunks_total": 3,
  "released": 2,
  "quarantined": 1,
  "chunk_ids": ["550e8400…", "550e8401…", "550e8402…"],
  "reingest": false,
  "cache_version": 0,
  "truncated": false
}
```

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/ingest-document   -H "Content-Type: application/json"   -d '{
    "text": "# Deploy\n\nRun the rollout.\n\n# Rollback\n\nRestore the previous release.",
    "doc_id": "dep-guide",
    "title": "Deployment Guide",
    "tags": ["project:vesma", "agent:tech-lead", "mnemos:learning"]
  }'
```

**Errors**

| Code | Cause |
|------|-------|
| `422` | Missing required tag, missing `text`/`doc_id`, or an empty `doc_id` |
| `500` | SQLite / vault write failure |

---

## Project graph (ADR-0032)

The `/graph/` namespace mirrors the ten [`vesma_*graph*` MCP tools](mcp-tools.md#project-graph-tools-adr-0032) over a registered project's code graph (symbols, outlines, snippets — [ADR-0032](../../project/adr/0032-project-graph.md)).

> **On by default (owner decision 2026-09-28).** Set `code_graph.enabled: false` and every endpoint answers `503`. The REST surface stays fully manual — indexing fires only on `POST /graph/index`; the native auto-indexing of wave PG-0.5 (auto-registration + background index on the first MCP/hook contact) is an MCP/hooks-side path and never triggers through HTTP.

**Error codes for the whole namespace** — `503` disabled (operator gate), `403` confinement (unregistered project or a path escaping the registered root, PG2), `413` index limit breach (fail-closed, PG7), `400` bad request (budget cannot fit one row, malformed arguments). Every call — read or write — is audited per agent (PG7); the `agent` field is therefore required everywhere.

### `POST /graph/index` — index a registered project

Full or incremental indexation of the registered project root. Serialized per project; PG7 limits are fail-closed.

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | **yes** | — | Registered project id or unique name. |
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `session` | string | no | — | Session id for the audit trail. |
| `incremental` | bool | no | `true` | Skip work when nothing changed. |
| `reason` | string | no | — | Audit reason. |

**Response 200**

```json
{
  "status": "indexed",
  "nodes": 2143,
  "edges": 5107,
  "files_indexed": 312,
  "files_skipped": 88,
  "poisoned": [],
  "unpoisoned": [],
  "parse_errors": {},
  "duration_sec": 4.212,
  "incremental": true,
  "staleness": { "total_files": 400, "fresh_percent": 100.0, "changed_files": [], "last_indexed_at": "2026-09-28T12:00:04+00:00" }
}
```

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/graph/index \
  -H "Content-Type: application/json" \
  -d '{"project_id": "vesma", "agent": "ci-runner"}'
```

### `GET /graph/status/{project_id}` — project graph status

Volumes, freshness, parse failures and the poisoned count. `agent` is a required query parameter; `session` optional.

**Response 200**

```json
{
  "project": "vesma",
  "nodes": 2143,
  "edges": 5107,
  "files": 400,
  "parse_errors": {},
  "parse_error_count": 0,
  "poisoned_count": 0,
  "staleness": { "total_files": 400, "fresh_percent": 97.5, "changed_files": ["src/vesmaro/manager.py"], "last_indexed_at": "2026-09-28T12:00:04+00:00" }
}
```

**Example**

```bash
curl -s "http://127.0.0.1:8000/graph/status/vesma?agent=ci-runner"
```

### `POST /graph/search` — search the project graph

Ranked name/qname/path search (exact > prefix > substring), token-contract windowed.

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | **yes** | — | Registered project id or unique name. |
| `query` | string | **yes** | — | Name / qname / path substring. |
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `session` | string | no | — | Session id for the audit trail. |
| `kind` | string | no | — | Node-kind filter (`Project`, `File`, `Module`, `Class`, `Function`, `Method`, `Type`). |
| `limit` | int | no | `50` | Max ranked rows per page. |
| `cursor` | int | no | `0` | Page cursor from the previous call. |
| `max_output_tokens` | int | no | `3200` | Output budget (128–1M). |
| `include_signature` | bool | no | `false` | Include signature shapes (opt-in). |

**Response 200**

```json
{
  "project": "vesma",
  "query_kind": null,
  "results": [
    { "score": 3, "id": "vesma#src/vesmaro/codegraph/service.py#window_rows#158", "project": "vesma", "kind": "Function", "name": "window_rows", "qname": "vesmaro.codegraph.service.window_rows", "path": "src/vesmaro/codegraph/service.py", "start_line": 158, "end_line": 190, "lang": "python", "signature": "def window_rows(rows, max_output_tokens, cursor)" }
  ],
  "total_matches": 1,
  "cursor": 0,
  "has_more": false,
  "last_indexed_at": "2026-09-28T12:00:04+00:00"
}
```

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/graph/search \
  -H "Content-Type: application/json" \
  -d '{"project_id": "vesma", "query": "window_rows", "agent": "ci-runner"}'
```

### `POST /graph/trace` — trace the path from a symbol

BFS over project edges from one symbol (resolve by qname; ambiguous refusals name the search). Depth ≤ 2, fanout and total-work caps.

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | **yes** | — | Registered project id or unique name. |
| `qname` | string | **yes** | — | Symbol qualified name (exact or unique tail). |
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `session` | string | no | — | Session id for the audit trail. |
| `depth` | int | no | `2` | BFS depth, 1–2. |
| `max_output_tokens` | int | no | `3200` | Output budget (128–1M). |

**Response 200**

```json
{
  "project": "vesma",
  "start": "vesmaro.codegraph.service.window_rows",
  "depth": 2,
  "nodes": [ { "id": "vesma#…#window_rows#158", "qname": "vesmaro.codegraph.service.window_rows", "kind": "Function", "path": "src/vesmaro/codegraph/service.py", "start_line": 158, "end_line": 190, "depth": 0 } ],
  "edges": [ { "from": "vesma#…#window_rows#158", "to": "vesma#…#resolve_token_budget#135", "kind": "CALLS", "provenance": "tree-sitter" } ],
  "truncated": false,
  "cursor": 0,
  "has_more": false,
  "last_indexed_at": "2026-09-28T12:00:04+00:00"
}
```

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/graph/trace \
  -H "Content-Type: application/json" \
  -d '{"project_id": "vesma", "qname": "vesmaro.codegraph.service.window_rows", "agent": "ci-runner"}'
```

### `POST /graph/outline` — symbol outline of one file

Symbol outline of one indexed file — shapes, never bodies (PG1). Repo-relative path, confined to the registered root (PG2).

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | **yes** | — | Registered project id or unique name. |
| `path` | string | **yes** | — | Repo-relative file path. |
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `session` | string | no | — | Session id for the audit trail. |
| `cursor` | int | no | `0` | Page cursor. |
| `max_output_tokens` | int | no | `3200` | Output budget (128–1M). |

**Response 200**

```json
{
  "project": "vesma",
  "path": "src/vesmaro/codegraph/service.py",
  "lang": "python",
  "outline": [ { "kind": "Function", "name": "window_rows", "qname": "vesmaro.codegraph.service.window_rows", "start_line": 158, "end_line": 190, "signature": "def window_rows(rows, max_output_tokens, cursor)" } ],
  "parse_error": null,
  "cursor": 0,
  "has_more": false,
  "last_indexed_at": "2026-09-28T12:00:04+00:00"
}
```

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/graph/outline \
  -H "Content-Type: application/json" \
  -d '{"project_id": "vesma", "path": "src/vesmaro/codegraph/service.py", "agent": "ci-runner"}'
```

### `POST /graph/snippet` — secret-scanned line range from disk

Read a line range FROM DISK (PG4): poisoned refusal (permanent) → confinement → indexed check → mtime+size+sha256 freshness → issuance secret scan (any hit refuses fail-closed) → whole-line token window. No snippet cache.

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | **yes** | — | Registered project id or unique name. |
| `path` | string | **yes** | — | Repo-relative file path. |
| `start_line` | int | **yes** | — | First line (1-based). |
| `end_line` | int | **yes** | — | Last line (inclusive). |
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `session` | string | no | — | Session id for the audit trail. |
| `max_output_tokens` | int | no | `3200` | Output budget (128–1M); whole-line drops. |

**Response 200**

```json
{
  "project": "vesma",
  "path": "src/vesmaro/codegraph/service.py",
  "start_line": 158,
  "end_line": 172,
  "content": "def window_rows(\n    rows: list[dict[str, Any]],\n    ...",
  "total_file_lines": 1081,
  "has_more": true,
  "next_start_line": 173,
  "scanned": true,
  "stale": false
}
```

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/graph/snippet \
  -H "Content-Type: application/json" \
  -d '{"project_id": "vesma", "path": "src/vesmaro/codegraph/service.py", "start_line": 158, "end_line": 172, "agent": "ci-runner"}'
```

### `POST /graph/coverage` — batch coverage check

Per-path verdict: `indexed` / `stale` / `parse-error` / `unindexed` / `missing` (path does not exist under the project root, #452) / `poisoned`. Coverage honesty — verify content with `POST /graph/snippet`.

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | **yes** | — | Registered project id or unique name. |
| `paths` | string[] | **yes** | — | Repo-relative paths to check. |
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `session` | string | no | — | Session id for the audit trail. |

**Response 200**

```json
{
  "project": "vesma",
  "coverage": [
    { "path": "src/vesmaro/manager.py", "verdict": "stale" },
    { "path": "src/vesmaro/codegraph/service.py", "verdict": "indexed" }
  ]
}
```

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/graph/coverage \
  -H "Content-Type: application/json" \
  -d '{"project_id": "vesma", "paths": ["src/vesmaro/manager.py"], "agent": "ci-runner"}'
```

### `GET /graph/schema` — graph contract card

Node/edge kinds, token contract, limits, schema version; an optional `project_id` query parameter adds that project's volumes.

**Response 200**

```json
{
  "schema_version": 1,
  "node_kinds": ["Project", "File", "Module", "Class", "Function", "Method", "Type"],
  "edge_kinds": ["CONTAINS_FILE", "DEFINES", "IMPORTS", "CALLS", "INHERITS", "TESTS", "USES"],
  "token_contract": { "max_output_tokens_default": 3200, "max_output_tokens_min": 128, "max_output_tokens_max": 1000000, "bytes_per_token": 4 },
  "limits": { "index_max_files": 20000, "index_max_source_mb": 500 },
  "trace": { "max_depth": 2, "fanout_cap": 32, "total_work_cap": 512 }
}
```

**Example**

```bash
curl -s "http://127.0.0.1:8000/graph/schema?agent=ci-runner&project_id=vesma"
```

### `GET /graph/projects` — list graph projects

Registered projects joined with their index status; registered-but-never-indexed stays visible.

**Response 200**

```json
{
  "projects": [
    { "project": "vesma", "registered": true, "has_root": true, "nodes": 2143, "edges": 5107, "files": 400, "poisoned": 0, "last_indexed_at": "2026-09-28T12:00:04+00:00" }
  ],
  "has_more": false,
  "cursor": 0
}
```

**Example**

```bash
curl -s "http://127.0.0.1:8000/graph/projects?agent=ci-runner"
```

### `DELETE /graph/projects/{project_id}` — drop a graph index

Drop the project's graph INDEX (sidecar data). A **live** registration keeps its project entity; a **ghost** registration (root missing on disk) is removed ENTIRELY — index and registration row — behind the evidence gate: body `confirm=true` plus `confirm_name` echoing the project name (a gate-less attempt answers `403 confinement-refused`). The ONLY operation that clears the poisoned set (PG3). `agent` is a required query parameter; the request body carries optional `session`, `reason`, `confirm`, `confirm_name`.

**Response 200**

```json
{ "project": "vesma", "deleted_nodes": 2143, "status": "deleted", "ghost": false, "deregistered": false }
```

**Example**

```bash
curl -s -X DELETE "http://127.0.0.1:8000/graph/projects/vesma?agent=operator" \
  -H "Content-Type: application/json" \
  -d '{"reason": "reindex from scratch"}'
```

---

## Watch poll (project graph, ADR-0032 §3.2)

These endpoints manage the in-process watch poll: a single cooperative thread checks the project's indexed files by mtime+size on an adaptive interval and reindexes on actual changes. They mirror the `mnemos_watch_start`, `mnemos_watch_stop`, and `mnemos_watch_status` MCP tools.

> **Changed.** The former directory-watcher form (`paths=` / `scan=` / `include_rules=`) was an unimplemented stub and is gone — those fields are no longer accepted. `POST /watch/start` requires the graph flags (on by default since 2026-09-28) plus an existing index; an explicit `code_graph.enabled: false` yields `503`, an unregistered project `403`.

### `POST /watch/start` — register the project-graph watch poll

**Request body**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `project_id` | string | **yes** | — | Registered project id to watch. |
| `agent` | string | **yes** | — | Caller identity (PG7). |
| `session` | string | no | — | Session id for the audit trail. |

**Response 200**

```json
{
  "status": "registered",
  "project": "vesma",
  "project_id": "vesma",
  "root": "/home/you/projects/vesma",
  "agent": "ci-runner",
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

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/watch/start \
  -H "Content-Type: application/json" \
  -d '{"project_id": "vesma", "agent": "ci-runner"}'
```

### `POST /watch/stop` — stop watch registrations

Idempotent. The optional `project_id` query parameter stops one registration; without it, ALL registrations stop.

**Response 200**

```json
{"status": "stopped", "stopped": 1}
```

**Example**

```bash
curl -s -X POST "http://127.0.0.1:8000/watch/stop?project_id=vesma"
```

### `GET /watch/status` — watch poll status

Active registrations and the last poll outcome per project.

**Response 200**

```json
{
  "running": true,
  "watch_enabled": true,
  "cap": 8,
  "registrations": [
    { "project": "vesma", "project_id": "vesma", "root": "/home/you/projects/vesma", "agent": "ci-runner", "session": null, "registered_at": "2026-09-28T12:00:00+00:00", "interval_sec": 5.0, "runs": 3, "reindexes": 1, "last_run_at": "2026-09-28T12:00:15+00:00", "last_result": "fresh", "last_error": null }
  ]
}
```

**Example**

```bash
curl -s http://127.0.0.1:8000/watch/status
```

---

## Knowledge pipeline (M4)

### `POST /process` — run end-to-end pipeline

Cluster → synthesize → quality gate → publish. Heavy operation; can take seconds to minutes for a large backlog.

**Query parameters**

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `project` | string | — | Scope to one project. |
| `agent` | string | — | Scope to one agent. |
| `limit` | int | `100` | Max raw entries to consider. Hard cap `500`. |

**Response 200**

```json
{
  "status": "ok",
  "clustered": 8,
  "synthesized": 7,
  "published": 6,
  "quality_rejected": 1,
  "errors": 0
}
```

**Example**

```bash
curl -s -X POST "http://127.0.0.1:8000/process?project=vesma&limit=200"
```

### `POST /synthesize` — synthesize one cluster

**Query parameters**

| Name | Type | Description |
|------|------|-------------|
| `cluster_id` | string | Cluster identifier. |

**Response 200**

```json
{
  "status": "ok",
  "draft_id": "dr-...",
  "cluster_id": "cl-...",
  "source_coverage": 0.92,
  "model_used": "qwen2.5:3b"
}
```

**Response 404** — `{"detail": "Cluster <id> not found or empty"}`.

### `POST /publish/{memory_id}` — publish a processed memory

Moves a single `processed` memory to `published` and indexes it in the vector store.

**Path parameters**

| Name | Type | Description |
|------|------|-------------|
| `memory_id` | UUID | Memory identifier. |

**Response 200**

```json
{
  "status": "published",
  "memory_id": "550e8400-e29b-41d4-a716-446655440000",
  "vector_indexed": true
}
```

**Response 400** — `{"detail": "Publish failed for <id> (status=raw)"}` (only `processed` memories can be published).

---

## Dead-Letter Queue (M5)

The DLQ holds tasks the automation layer could not complete (LLM timeout, transient embedder failure, etc.). Three endpoints manage the queue.

### `GET /dlq` — list DLQ entries

**Query parameters**

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `task_label` | string | — | Filter by task label. |
| `ready_only` | bool | `false` | If true, only entries whose next-retry time has elapsed. |
| `limit` | int | `50` | Max rows. Hard cap `500`. |

**Response 200** — array of DLQ row dicts (see `SQLiteStore.dlq_list` for the exact shape).

### `POST /dlq/{dlq_id}/retry` — schedule a retry

**Path parameters**

| Name | Type |
|------|------|
| `dlq_id` | string |

**Response 200** — `{"status": "retry_scheduled", "entry": { ... }}`

### `DELETE /dlq/{dlq_id}` — discard a DLQ entry

**Response 200** — `{"status": "discarded", "dlq_id": "..."}`

**Response 404** — `{"detail": "DLQ entry <id> not found"}`.

---

## Context filter (M10)

### `POST /filter/{memory_id}` — apply the 5-stage context filter

Issuance-gated (final review M1): only `published`/`processed` memories are
filterable into context (`raw`/`processing`/`archived` → 422), an optional
caller `project` fails closed on mismatch (→ 403), and the echoed
`clean_content` is secret-scanned (redacted copy + `redactions` counts;
refuse mode → 403 with no content).

**Path parameters**

| Name | Type | Description |
|------|------|-------------|
| `memory_id` | UUID | Target memory. |

**Request body** — see `FilterRequest` in `src/vesmaro/models.py`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `profile` | string | no | auto | One of `log`, `terminal`, `code`, `docs`, `web`, `default`. |
| `budget` | int | no | — | Token / char budget. |
| `project` | string | no | — | Caller project slug — the memory must belong to it (mismatch → 403). Omit for explicit operator semantics. |

**Response 200** — `FilterResult` (see `manager.issue_context_filter`; adds `redactions` and, when non-zero, `redacted_patterns`).

**Response 404** — `{"detail": "..."}` (memory not found). **422** — status gate. **403** — project mismatch or issuance refusal.

---

## Traces (M6)

### `GET /traces` — list pipeline traces

The trace layer is the explainability hook. Every pipeline step writes a trace row.

**Query parameters**

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `task_label` | string | — | Filter by task label (e.g. `pipeline`, `synthesize`, `publish`). |
| `limit` | int | `50` | Max rows. Hard cap `500`. |

**Response 200** — array of trace row dicts.

---

## Path-scoped rules ingest (M8)

### `POST /rules/ingest` — ingest `.instructions.md` files

**Request body** — see `RuleIngestRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `rules_dir` | string | **yes** | Directory to scan recursively. |
| `project` | string | no | Project slug to tag. |
| `agent` | string | no | Agent slug to tag. |
| `pattern` | string | no | Glob (default `*.instructions.md`). |

**Response 200**

```json
{
  "status": "ok",
  "processed": 7,
  "results": [
    {"file_path": ".github/instructions/communication-language.instructions.md", "memory_id": "..."}
  ]
}
```

**Example**

```bash
curl -s -X POST http://127.0.0.1:8000/rules/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "rules_dir": "/home/you/vesma/.github/instructions",
    "project": "vesma",
    "agent": "tech-writer"
  }'
```

### `DELETE /rules/ingest` — remove a rule memory

**Request body** — see `RuleRemoveRequest`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file_path` | string | **yes** | Absolute or vault-relative path of the rule. |

**Response 200** — `{"status": "removed", "removed": true, "memory_id": "..."}`

**Response 404** — `{"detail": "Rule for <path> not found"}`.

---

## A2A Sessions API (M16)

> Mounted under `/v1`. The contract is described in detail in [a2a-sessions.md](../architecture/a2a-sessions.md); this section is the HTTP surface.

All five endpoints share the same error semantics:

- `422` — Pydantic validation failure.
- `404` — `SessionNotFoundError` or `TurnNotFoundError`.
- `500` — any other server-side failure (see logs).

### `POST /v1/sessions` — create a session

**Request body** — `SessionCreate`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `user_id` | string | no | `""` | Up to 256 chars, whitespace stripped. |
| `metadata` | object | no | `{}` | Free-form key / value store. |
| `ttl_expires_at` | string (ISO 8601) | no | — | Optional client hint for TTL. Server may clamp. |

**Response 201** — `SessionRead`

```json
{
  "session_id": "conv-2026-06-15-a1b2c3d4",
  "user_id": "user-42",
  "created_at": "2026-06-15T10:42:00+00:00",
  "updated_at": "2026-06-15T10:42:00+00:00",
  "turns_count": 0,
  "metadata": {},
  "ttl_expires_at": null
}
```

`session_id` format is `conv-YYYY-MM-DD-<8 hex>` (UTC date).

### `GET /v1/sessions/{session_id}` — read a session

**Response 200** — `SessionRead`.

**Response 404** — `{"detail": "..."}`

### `POST /v1/sessions/{session_id}/turns` — append a turn

Idempotent on `message_id`: a repeat POST with the same `message_id` returns the existing turn (status code is still `201`) instead of duplicating.

**Request body** — `TurnCreate`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `role` | string | **yes** | — | One of `user`, `agent`, `a2a_message`, `system`. |
| `content` | string | **yes** | — | 1 to 1 000 000 chars. |
| `from` | string | no | — | Sender identifier. Up to 256 chars. |
| `to` | string | no | — | Recipient identifier. |
| `message_id` | string | no | — | Truncated to 256 chars. Idempotency key. |
| `outcome` | string | conditional | auto | One of `delivered`, `rejected`, `budget-exhausted`, `loop-detected`. Required when `role=a2a_message`; rejected otherwise. |
| `tags` | string[] | no | `[]` | Deduplicated, max 32 entries. |

**Response 201** — `TurnRead`

```json
{
  "turn_id": "tr-...",
  "session_id": "conv-2026-06-15-a1b2c3d4",
  "step_number": 1,
  "role": "user",
  "from": "user-42",
  "to": null,
  "summary": null,
  "key_decisions": [],
  "content": "Hello, Vesma.",
  "outcome": null,
  "tags": [],
  "context_pointer": "ctx-...",
  "message_id": "msg-001",
  "created_at": "2026-06-15T10:42:00+00:00"
}
```

### `GET /v1/sessions/{session_id}/turns/{turn_id}` — read one turn

**Query parameters**

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `mode` | string | `summary` | `summary` returns the cheap path (omits `content`); `full` returns raw content. |

**Response 200** — `TurnRead`.

### `POST /v1/sessions/{session_id}/turns/range` — bulk range

**Request body** — `TurnRangeRequest`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `from_step` | int | **yes** | — | Inclusive lower bound. |
| `to_step` | int | **yes** | — | Inclusive upper bound. Must be ≥ `from_step`. |
| `mode` | string | no | `summary` | `summary` or `full`. |

**Response 200** — `TurnRangeResponse`

```json
{
  "turns": [ { "...TurnRead..." } ],
  "total": 12,
  "mode": "summary"
}
```

Result is sorted by `step_number` ascending. `total` is the number of turns actually returned (not the whole session).

---

## Memory schema

The `Memory` Pydantic model (defined in `src/vesmaro/models.py`) is returned by `POST /memories`, `GET /memories/{id}`, and `GET /memories`.

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID | Server-assigned. |
| `content` | string | Cleaned / effective content. |
| `raw_content` | string \| null | Original pre-filter content. Omitted unless `include_raw=true`. |
| `title` | string \| null | Auto-generated if not provided. |
| `tags` | string[] | Must satisfy the M2 contract. |
| `source` | string | One of `manual`, `web`, `file`, `mcp`, `obsidian`, `cli`, `rule`, `synthesized`. |
| `source_url` | string \| null | Origin URL. |
| `memory_type` | string | One of `note`, `fact`, `snippet`, `bookmark`, `conversation`, `session_context`. |
| `status` | string | One of `raw`, `processing`, `processed`, `published`, `archived`. |
| `project` | string | Denormalised from the `project:` tag. |
| `agent` | string | Denormalised from the `agent:` tag. |
| `category` | string \| null | Free-form. |
| `quality_score` | float \| null | Set by the M4 quality gate. |
| `confidence` | float \| null | Set by the M4 quality gate. |
| `cluster_id` | string \| null | M4 cluster pointer. |
| `derived_from` | string[] | Parent memory IDs (synthesis lineage). |
| `file_path` | string \| null | Vault-relative path. |
| `metadata` | object | Free-form. |
| `filter_profile` | string \| null | M10 filter profile applied. |
| `created_at` | ISO 8601 | UTC. |
| `updated_at` | ISO 8601 | UTC. |

---

## OpenAPI / Swagger

The full machine-readable schema is available at `/openapi.json` (3.1.0) and rendered as a UI at `/docs` (Swagger) and `/redoc` (ReDoc). These are generated by FastAPI from the route decorators in `src/vesmaro/api/main.py` and `src/vesmaro/sessions/api.py`, so the schema never drifts from the running code.

If you need to generate a static client, fetch the schema and run [`openapi-generator`](https://openapi-generator.tech/) against it:

```bash
curl -s http://127.0.0.1:8000/openapi.json -o vesma-openapi.json
npx @openapitools/openapi-generator-cli generate \
  -i vesma-openapi.json -g typescript-fetch -o ./mnemos-client
```

---

## See also

- [mcp-tools.md](mcp-tools.md) — same capabilities over MCP
- [cli-reference.md](cli-reference.md) — same capabilities over the CLI
- [architecture overview](../architecture/overview.md) — system shape and data model
- [a2a-sessions.md](../architecture/a2a-sessions.md) — A2A contract and design rationale
- [tag-contract.md](tag-contract.md) — M2 schema enforced by `POST /memories`
- [security.md](../admin/security.md) — SSRF guard, secrets hygiene, auth model, request-boundary rules

---

_Last updated: 2026-07-07_
