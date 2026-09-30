# A2A Sessions API (M16)

**🌐 Language / Язык:** English · [Русский](../../ru/architecture/a2a-sessions.md)

> **Status**: Implemented in Vesma M16
> **Audience**: AI agents / MCP orchestrator
> **Base URL**: `http://localhost:8787/v1/` (loopback by default)
> **Source spec**: [ADR-0007 — A2A Sessions API v1](../../project/adr/0007-a2a-sessions-api-v1.md)

Vesma exposes 5 HTTP endpoints for the A2A routing layer. They give
agents a persistent backend for conversation sessions and per-step turn
history, so multi-step agent chains survive restarts and cross-session
context is searchable.

If Vesma is unavailable, the MCP layer falls back to a file-based log
(`~/.gcw/a2a-messages.jsonl`) — Vesma is **not** a single point of
failure for agents.

---

## Endpoints at a glance

| # | Method | Path                                            | Purpose |
|---|--------|-------------------------------------------------|---------|
| 1 | POST   | `/v1/sessions`                                  | Create a new session |
| 2 | GET    | `/v1/sessions/{session_id}`                     | Read session metadata + turn count |
| 3 | POST   | `/v1/sessions/{session_id}/turns`               | Append a turn (idempotent on `message_id`) |
| 4 | GET    | `/v1/sessions/{session_id}/turns/{turn_id}`     | Lazy-load one turn (summary or full) |
| 5 | POST   | `/v1/sessions/{session_id}/turns/range`         | Bulk load a contiguous step range |

OpenAPI schema is auto-generated and available at:

- `GET /openapi.json` — machine-readable spec
- `GET /docs`         — Swagger UI
- `GET /redoc`        — ReDoc UI

---

## 1. `POST /v1/sessions`

Create a new conversation session. Server mints the `session_id` in the
contract format `conv-YYYY-MM-DD-<short-uuid>`.

### Request

```http
POST /v1/sessions
Content-Type: application/json
```

```json
{
  "user_id": "abyss",
  "metadata": {
    "started_by": "vscode",
    "workspace": "/var/home/abyss/LABs/Projects/Reserching/GithubCopilotWorkflow"
  },
  "ttl_expires_at": null
}
```

| Field           | Type   | Required | Notes |
|-----------------|--------|----------|-------|
| `user_id`       | string | no       | Empty string allowed. Stripped. |
| `metadata`      | object | no       | Free-form JSON. |
| `ttl_expires_at`| string | no       | ISO-8601. Optional. |

### Response — 201

```json
{
  "session_id": "conv-2026-06-15-eca03ad4",
  "user_id": "abyss",
  "created_at": "2026-06-15T20:28:33.279446Z",
  "updated_at": "2026-06-15T20:28:33.279446Z",
  "turns_count": 0,
  "metadata": { "started_by": "vscode" },
  "ttl_expires_at": null
}
```

---

## 2. `GET /v1/sessions/{session_id}`

Fetch a session's metadata plus the current `turns_count`.

### Request

```http
GET /v1/sessions/conv-2026-06-15-eca03ad4
```

### Response — 200

```json
{
  "session_id": "conv-2026-06-15-eca03ad4",
  "user_id": "abyss",
  "created_at": "2026-06-15T20:28:33.279446Z",
  "updated_at": "2026-06-15T20:30:11.123Z",
  "turns_count": 7,
  "metadata": { "started_by": "vscode" },
  "ttl_expires_at": null
}
```

### Errors

- `404` — session not found.

---

## 3. `POST /v1/sessions/{session_id}/turns`

Append a turn to a session. **Idempotent** via `message_id`: a repeat
POST with the same id returns the existing turn instead of duplicating.

### Request

```http
POST /v1/sessions/conv-2026-06-15-eca03ad4/turns
Content-Type: application/json
```

```json
{
  "role": "a2a_message",
  "from": "gcw-senior-system-engineer",
  "to":   "gcw-senior-dba",
  "message_id": "msg-x7y8z9",
  "content": "Need migration for orders.archived_at column. DECISION: add column, not table. - [x] write migration plan",
  "outcome": "delivered",
  "tags": ["migration", "schema", "orders"]
}
```

| Field        | Type     | Required             | Notes |
|--------------|----------|----------------------|-------|
| `role`       | enum     | yes                  | One of `user`, `agent`, `a2a_message`, `system` |
| `content`    | string   | yes                  | Max 1,000,000 chars |
| `from`       | string   | no                   | Agent / user that authored the turn |
| `to`         | string   | no                   | Recipient agent |
| `message_id` | string   | no (recommended)     | Idempotency key. Truncated to 256 chars. |
| `outcome`    | enum     | yes (for `a2a_message`) | One of `delivered`, `rejected`, `budget-exhausted`, `loop-detected`. Defaults to `delivered` when role is `a2a_message`. |
| `tags`       | string[] | no                   | Free-form. Capped at 32 unique entries. |

### Response — 201

```json
{
  "turn_id": "turn-2",
  "session_id": "conv-2026-06-15-eca03ad4",
  "step_number": 2,
  "role": "a2a_message",
  "from": "gcw-senior-system-engineer",
  "to": "gcw-senior-dba",
  "summary": "Need migration for orders.archived_at column. DECISION: add column, not table. - [x] write migration plan",
  "key_decisions": [
    "DECISION: add column, not table",
    "- [x] write migration plan"
  ],
  "content": "Need migration for orders.archived_at column. ...",
  "outcome": "delivered",
  "tags": ["migration", "schema", "orders"],
  "context_pointer": "memory://conv-2026-06-15-eca03ad4#step-2",
  "message_id": "msg-x7y8z9",
  "created_at": "2026-06-15T20:30:11.123Z"
}
```

### Idempotency contract

- Same `message_id` → returns the **existing** turn (same `turn_id`,
  `step_number`, `context_pointer`).
- Different `message_id` → creates a new turn, increments `step_number`.
- No `message_id` → not idempotent; each call appends a new turn.

### Errors

- `404` — session not found.
- `422` — invalid `role`, missing `content`, outcome used with non-A2A role, etc.

---

## 4. `GET /v1/sessions/{session_id}/turns/{turn_id}`

Lazy-load one turn in `summary` (default) or `full` mode.

### Request

```http
GET /v1/sessions/conv-2026-06-15-eca03ad4/turns/turn-2?mode=summary
```

| Query | Default     | Notes |
|-------|-------------|-------|
| `mode` | `summary`   | `summary` returns up to 200-char extract + key_decisions, **no** `content`. `full` returns everything including `content`. |

### Response — 200 (`mode=summary`)

```json
{
  "turn_id": "turn-2",
  "session_id": "conv-2026-06-15-eca03ad4",
  "step_number": 2,
  "role": "a2a_message",
  "from": "gcw-senior-system-engineer",
  "to": "gcw-senior-dba",
  "summary": "Need migration for orders.archived_at column. DECISION: add column, not table. - [x] write migration plan",
  "key_decisions": [
    "DECISION: add column, not table",
    "- [x] write migration plan"
  ],
  "outcome": "delivered",
  "tags": ["migration", "schema", "orders"],
  "context_pointer": "memory://conv-2026-06-15-eca03ad4#step-2",
  "message_id": "msg-x7y8z9",
  "created_at": "2026-06-15T20:30:11.123Z"
}
```

`mode=full` adds the `content` field with the full original message.

### Errors

- `404` — session or turn not found.
- `422` — invalid `mode` value.

---

## 5. `POST /v1/sessions/{session_id}/turns/range`

Bulk-load a contiguous range of turns, ordered by `step_number` ascending.

### Request

```http
POST /v1/sessions/conv-2026-06-15-eca03ad4/turns/range
Content-Type: application/json
```

```json
{
  "from_step": 1,
  "to_step":   5,
  "mode":      "summary"
}
```

| Field       | Type | Range           | Notes |
|-------------|------|-----------------|-------|
| `from_step` | int  | 1..10,000,000   | Inclusive lower bound. |
| `to_step`   | int  | 1..10,000,000   | Inclusive upper bound. Must be ≥ `from_step`. |
| `mode`      | enum | `summary` / `full` | Same semantics as single-turn GET. |

The window is capped at 1000 turns per call (response size guard).

### Response — 200

```json
{
  "turns": [
    { "turn_id": "turn-1", "step_number": 1, "summary": "..." },
    { "turn_id": "turn-2", "step_number": 2, "summary": "..." },
    { "turn_id": "turn-3", "step_number": 3, "summary": "..." }
  ],
  "total": 3,
  "mode": "summary"
}
```

`total` is the count of turns actually returned in the range (not the
size of the whole session). An out-of-bounds range returns
`{"turns": [], "total": 0, "mode": "summary"}` with status 200 — it is
not an error.

### Errors

- `404` — session not found.
- `422` — `to_step < from_step` or range > 1000 turns.

---

## Storage model

Vesma uses the same SQLite file as the rest of the project. WAL mode is
enabled, so the A2A store and the main memory store can both read while
one of them writes. Two new tables live alongside the existing ones:

```sql
CREATE TABLE sessions ( ... );     -- one row per conversation
CREATE TABLE turns    ( ... );     -- one row per turn; UNIQUE(message_id)
```

(The `turns_fts` FTS5 shadow and its `turns_ai`/`turns_ad`/`turns_au`
triggers were REMOVED — C8, ArchCom 2026-08-27: a dead index with zero
readers, a second plaintext copy of every turn at rest, and write
amplification on the turn path. Turn-level search is not a feature; the
DDL is recoverable from VCS history on demand.)

A2A tables live under the same WAL group as the memories tables, so a
single `journal_mode=WAL` pragma covers both.

### Atomicity guarantees

- A `POST /turns` is wrapped in a single transaction: SELECT for
  idempotency, INSERT, UPDATE `sessions.updated_at`, COMMIT.
- On any `sqlite3.Error` the transaction is rolled back, so a crash
  mid-write cannot leave a half-inserted turn.
- SQLite's default isolation is SERIALIZABLE for write transactions, so
  the check-before-insert idempotency pattern is safe under concurrent
  writers.

### Summary extraction (no LLM in v1)

The `summary` field is computed at write time from the turn's `content`:

1. Take the first paragraph (text up to the first blank line).
2. If it exceeds 200 characters, truncate at the nearest word boundary
   and append `...`.

The `key_decisions` field is extracted via a regex that recognises
two patterns:

- `DECISION: <text>` (case-insensitive).
- `- [x] <text>` (GitHub-flavoured markdown completed task).

Both line-start and sentence-boundary forms (e.g. `… statement. DECISION: x`)
are recognised so single-line A2A payloads work as well as multi-line
ones. Up to 5 decisions are returned, in the order they appear.

---

## Failure modes (agent perspective)

| Vesma behaviour  | What the agent should do |
|-------------------|--------------------|
| `5xx` response    | Retry up to 3 times with exponential backoff, then fall back to file-based log. |
| Connection refused / timeout > 2s | Skip persistence — continue processing without `context_pointer`. |
| `4xx` (validation)| Do NOT retry. Log the error and continue. |
| Successful `2xx`  | Use the returned `context_pointer` to address the turn in later steps. |

Vesma itself does not implement these retries — that's the MCP
layer's job. See [ADR-0007 — A2A Sessions API v1](../../project/adr/0007-a2a-sessions-api-v1.md) for the full contract.

---

## Examples

### Create a session and write one turn (curl)

```bash
# 1. Create
SESSION=$(curl -s -X POST http://localhost:8787/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"abyss","metadata":{"started_by":"vscode"}}' \
  | jq -r '.session_id')

# 2. Write a turn
curl -s -X POST "http://localhost:8787/v1/sessions/$SESSION/turns" \
  -H 'Content-Type: application/json' \
  -d '{
    "role": "a2a_message",
    "from": "gcw-senior-system-engineer",
    "to":   "gcw-senior-dba",
    "message_id": "msg-001",
    "content": "Need migration for orders.archived_at column. DECISION: add column, not table.",
    "outcome": "delivered",
    "tags": ["migration", "orders"]
  }'

# 3. Read the turn back (summary)
curl -s "http://localhost:8787/v1/sessions/$SESSION/turns/turn-1" | jq

# 4. Bulk-load the first 5 steps
curl -s -X POST "http://localhost:8787/v1/sessions/$SESSION/turns/range" \
  -H 'Content-Type: application/json' \
  -d '{"from_step": 1, "to_step": 5, "mode": "summary"}' | jq
```

### Idempotency (Python)

```python
payload = {
    "role": "a2a_message",
    "content": "hello",
    "message_id": "msg-stable-1",
}
r1 = client.post(f"/v1/sessions/{sid}/turns", json=payload)
r2 = client.post(f"/v1/sessions/{sid}/turns", json=payload)
assert r1.json()["turn_id"] == r2.json()["turn_id"]   # same turn returned
```

---

## Migration notes

If you are upgrading from a Vesma build that lacks the A2A tables, no
manual migration step is required: the schema is `CREATE TABLE IF NOT
EXISTS …` and is applied automatically the first time the main
`SQLiteStore` opens the database (which happens on FastAPI startup).

The `turns_fts` virtual table used to be created eagerly even though no
endpoint queried it; it was removed (C8, ArchCom 2026-08-27 — dead index,
second plaintext copy at rest, write amplification). Legacy databases
have the table and its triggers dropped idempotently on the first
`SQLiteStore` connect after the upgrade.
