# CLI Reference

**🌐 Language / Язык:** English · [Русский](../../ru/user/cli-reference.md)

> Complete reference for the `vesma` command-line tool.

The CLI is a thin Typer-based wrapper around [`MemoryManager`](../architecture/overview.md#memorymanager). It uses Rich for table / colour output and is the most convenient way to interact with Vesma from a shell.

The full set of subcommands is defined in `src/vesmaro/cli/main.py`. This page mirrors what the source actually exposes — every example here is runnable on a clean install.

For a step-by-step first run, see [getting-started.md](getting-started.md). For programmatic access, see [mcp-tools.md](mcp-tools.md) and [http-api.md](http-api.md).

---

## Synopsis

```text
vesma [GLOBAL-OPTIONS] SUBCOMMAND [SUBCOMMAND-OPTIONS] [ARGS]
```

| Subcommand | Purpose |
|------------|---------|
| [`add`](#add) | Create a new memory entry |
| [`search`](#search) | Hybrid FTS5 + vector search |
| [`recall`](#recall) | List recent memories, optionally per agent / per project |
| [`tags validate`](#tags-validate) | Validate the tag contract across a vault |
| [`workflow`](#workflow) | Memory workflow lifecycle: `get` / `set` / `history` |
| [`stats`](#stats) | Show health counters |
| [`fts`](#fts) | FTS5 index management (`rebuild`) |
| [`processor`](#processor) | Background pipeline control: `status` / `run` / `start` / `stop` |
| [`reindex`](#reindex) | Re-embed all published memories into the vector index |
| [`filter`](#filter) | Run the Context Filter on a memory |
| [`serve`](#serve) | Start the HTTP API server (FastAPI / Uvicorn) |
| [`mcp-server`](#mcp-server) | Start the MCP stdio server for VS Code Copilot |
| [`migrate from-ai-brain`](#migrate-from-ai-brain) | One-shot import from a legacy `ai-brain` install |
| [`auth`](#auth) | API bearer tokens (`auth token`) and TOTP 2FA (`auth totp`) |
| [`integration`](integration-guide.md) | Deploy / verify the integration layer (dedicated page) |
| [`completion`](#completion) | Install shell completion (bash / zsh / fish) |
| [`doctor`](#doctor) | Diagnose the installation (paths, config, database, vault) |
| [`update`](#update) | Check for updates / update the user-site install |
| [`export`](export-import.md) | Export memories to a JSON / SQLite backup (dedicated page) |
| [`import`](export-import.md) | Import memories from an export file (dedicated page) |
| [`logs`](#logs) | View pipeline traces |
| [`sync`](sync.md) | Federation batch sync export / import (dedicated page) |
| [`meta-poll`](#meta-poll) | Federation metadata poll: run one poller pass manually (S2 phase 2) |
| [`scanner`](#scanner) | Background secrets scanner: `run` / `status` |

> The `tags` group also provides `tags normalize` and `tags rename` (bulk prefix rename with dry-run); `migrate tags` is a deprecated alias for `vesma tags rename --from gcw: --to mnemos: --no-dry-run`. The `mnemos:` prefix in tag namespaces is a data contract unchanged by the rebrand (6.0 decision) — renames of the project namespace do not touch it.

---

## Global options

Most subcommands accept a `--config / -c` flag pointing at a YAML file. Search order is:

1. `--config` argument (if present)
2. `$VESMARO_CONFIG` env var (5.x canonical; the 4.x spelling `MNEMOS_CONFIG` is deprecated)
3. `./config.yaml` in the current working directory
4. `~/.mnemos/config.yaml`

```bash
vesma --help
vesma add --help
```

The only other global flags are `--version / -V` (print the version) and `--verbose / -v` (DEBUG logging for `vesma serve` and `vesma mcp-server`). To change the log level permanently, set `logging.level` in the config file or the corresponding env var:

```bash
VESMARO_LOGGING__LEVEL=DEBUG vesma serve      # 5.x canonical
# 4.x images still read the MNEMOS_LOGGING__LEVEL spelling (deprecated)
```

---

## Environment variables

All settings are env-overridable via the `VESMARO_` prefix (the canonical 5.x name). Nested keys use `__` as the delimiter.

| Variable (5.x canonical) | Default | Purpose |
|----------|---------|---------|
| `VESMARO_CONFIG` | — | Path to `config.yaml` |
| `VESMARO_MNEMOS__DATA_DIR` | `~/.mnemos/data` | SQLite DB + vector index (canonical form) |
| `VESMARO_MNEMOS__VAULT_PATH` | `~/.mnemos/vault` | Obsidian mirror directory (canonical form) |
| `VESMARO_MNEMOS__STRICT_TAG_CONTRACT` | `true` | Enforce M2 tag schema |
| `VESMARO_API__HOST` | `127.0.0.1` | Default for `vesma serve` |
| `VESMARO_API__PORT` | `8787` | Default for `vesma serve` |
| `VESMARO_SEARCH__HYBRID_ALPHA` | `0.5` | Vector weight in RRF fusion |
| `VESMARO_EMBEDDING__PROVIDER` | `nano` | `nano` (vesma-embed-v1, bundled) / `onnx` / `ollama` / `sentence-transformers` |
| `VESMARO_LLM__PROVIDER` | `ollama` | LLM for synthesis + context filter |
| `VESMARO_LLM__MODEL` | `qwen2.5:3b` | LLM model name |
| `VESMARO_AUTO_COLLECT` | `0` | Set `1` to enable MCP auto-collect mode |
| `VESMARO_LOGGING__LEVEL` | `INFO` | Python logging level |

> **Deprecated: MNEMOS_\* spelling.** The table above lists the 5.x-canonical `VESMARO_*` names (ADR-0031 dual-prefix contract; 5.x images read `VESMARO_*`). The same variables were shipped as `MNEMOS_CONFIG`, `MNEMOS_API__HOST`, `MNEMOS_API__PORT`, `MNEMOS_SEARCH__HYBRID_ALPHA`, `MNEMOS_EMBEDDING__PROVIDER`, `MNEMOS_LLM__PROVIDER`, `MNEMOS_LLM__MODEL`, `MNEMOS_AUTO_COLLECT`, `MNEMOS_LOGGING__LEVEL` on 4.x images and remain accepted there until deprecation. The two `VESMARO_DATA_DIR` / `VESMARO_VAULT__VAULT_PATH` short forms are #139 compatibility aliases for the nested canonical names — canonical env wins on conflict.

> **Legacy aliases.** The short forms predate the nested naming and are kept for compatibility (#139). Both forms work. On conflict the canonical env name — and an explicit value in the config file — wins over the legacy alias; the alias only fills the gap that would otherwise fall through to the default.

---

## `add`

Create a new memory entry.

```text
vesma add [CONTENT] [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `CONTENT` (positional) | — | Text to remember. If omitted, reads from stdin. |
| `--title / -t` | auto | Short title. Auto-generated from content if omitted. |
| `--tags / -T` | `""` | Comma-separated tags (e.g. `project:test,agent:me,mnemos:learning`). |
| `--file / -f` | — | Import the contents of a file. Mutually exclusive with `CONTENT` and `--url`. |
| `--url / -u` | — | Fetch and ingest a URL. Requires tags. |
| `--source / -s` | `cli` | Memory source enum: `manual`, `web`, `file`, `mcp`, `obsidian`, `cli`, `rule`, `synthesized`. |
| `--type` | `note` | Memory type: `note`, `fact`, `snippet`, `bookmark`, `conversation`, `session_context`. |
| `--dry-run` | `false` | Validate tags and preview context-filter stats without saving. |
| `--config / -c` | — | Path to `config.yaml`. |

> **Tag contract.** Every entry must have `project:<slug>`, `agent:<slug>`, and at least one `vesma:<subtype>`. The CLI enforces this in strict mode (the default). See [tag-contract.md](tag-contract.md) for the full schema.

### Examples

```bash
# Inline content
vesma add "Use uv, not pip" --tags project:vesma agent:tech-writer mnemos:learning

# With a title
vesma add "Always validate SQL with parameterized queries" \
  --title "SQL safety rule" \
  --tags "project:vesma,agent:security,mnemos:rule,severity:high"

# From a file
vesma add --file ~/notes/architecture.md --tags project:vesma agent:tech-lead mnemos:decision

# From a URL (fetches, extracts, saves)
vesma add --url https://example.com/article --tags project:research agent:user mnemos:learning

# From stdin
echo "Pinned CVE-2026-45829 in chromadb 1.5.9" \
  | vesma add --tags project:vesma agent:sre mnemos:bug-pattern,severity:medium
```

---

## `search`

Hybrid search: FTS5 + vector + Reciprocal Rank Fusion.

```text
vesma search QUERY [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `QUERY` (positional) | — | Natural-language search string. |
| `--limit / -l` | `10` | Maximum results. |
| `--project / -p` | — | Restrict to a single project slug. |
| `--tags / -T` | — | Comma-separated tags to filter by. |
| `--include-raw / --published-only` | `--include-raw` | Include `raw`/`processing` entries (default), or restrict to `published` knowledge. |
| `--status` | — | Filter by status (`raw`/`processing`/`processed`/`published`/`archived`); takes precedence over `--include-raw`. |
| `--config / -c` | — | Path to `config.yaml`. |

The score is the fused RRF score, with 0.0 = no match and 1.0 = top hit. By default raw entries are searched too — a just-added memory stays `raw` until the knowledge pipeline publishes it; use `--published-only` to restrict results to the vector-index scope.

### Examples

```bash
# Plain search
vesma search "embedding model"

# With project filter
vesma search "CVE" --project vesma --limit 20

# Wide-net recall
vesma search "decision" --limit 50
```

For richer query power over HTTP, use the API `POST /search` (see [http-api.md#search](http-api.md#search)).

---

## `recall`

List recent memories, optionally scoped to an agent (M3) and / or a project.

```text
vesma recall [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--project / -p` | — | Project slug to filter on. |
| `--agent / -a` | — | Agent slug to filter on. Enables M3 per-agent recall. |
| `--limit / -l` | `10` | Maximum results. |
| `--config / -c` | — | Path to `config.yaml`. |

When `--agent` is passed **without** a query, the result is the N most recent entries for that agent, ordered by `created_at desc`. This is the same data the MCP tool [`mnemos_agent_recall`](mcp-tools.md#mnemos_agent_recall) returns.

### Examples

```bash
# Most recent 10 entries for any agent
vesma recall

# Per-agent recall (M3)
vesma recall --agent tech-writer

# Combined
vesma recall --agent sre --project vesma --limit 25
```

---

## `tags validate`

Validate the Vesma tag contract across an existing Vesma vault directory. Reports entries that violate the M2 schema.

```text
vesma tags validate VAULT_PATH
```

| Argument | Description |
|----------|-------------|
| `VAULT_PATH` (positional) | Path to a Vesma vault directory (markdown mirror). |

> **Status.** The full vault-scan implementation is not yet wired in (`# TODO (M2): scan SQLite + vault markdown files`). For now the command prints a placeholder. Use `vesma stats` and the HTTP API `GET /memories?project=...` to inspect tags via SQLite instead.

### Example

```bash
vesma tags validate ~/.mnemos/vault
```

---

## `workflow`

Manage the workflow lifecycle of a memory through the server-enforced state machine (`open`, `in-progress`, `blocked`, `resolved`, `done`, `withdrawn`). The state machine and its guardrails live in `MemoryManager`; the CLI only translates violations into a red error line and exit code 1.

### `workflow get`

Show the current workflow status and lock owner for a memory.

```text
vesma workflow get MEMORY_ID
```

### `workflow set`

Transition a memory's workflow status.

```text
vesma workflow set MEMORY_ID --to STATUS --actor ACTOR [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `MEMORY_ID` (positional) | — | Target memory id. |
| `--to` | — (required) | Target status: `open`, `in-progress`, `blocked`, `resolved`, `done`, `withdrawn`. |
| `--actor` | — (required) | Free-form actor id (Phase 1 weak identity). |
| `--reason` | `""` | Human-readable reason. Required with `--force`. |
| `--force` | `false` | Override a lock held by another actor (requires `--reason`). |
| `--config / -c` | — | Path to `config.yaml`. |

### `workflow history`

Show the workflow transition audit log for a memory (newest first).

```text
vesma workflow history MEMORY_ID [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `MEMORY_ID` (positional) | — | Target memory id. |
| `--limit` | `50` | Max rows to show (newest first). |
| `--config / -c` | — | Path to `config.yaml`. |

### Example

```bash
ID=550e8400-e29b-41d4-a716-446655440000

vesma workflow set "$ID" --to in-progress --actor tech-writer
vesma workflow get "$ID"
vesma workflow history "$ID" --limit 20
```

### Related

- MCP tool: [`mnemos_workflow`](mcp-tools.md#mnemos_workflow)

---

## `stats`

Show Vesma health counters and key paths.

```text
vesma stats [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--config / -c` | — | Path to `config.yaml`. |

### Output keys

| Key | Meaning |
|-----|---------|
| `status` | Always `ok` (liveness signal) |
| `version` | Vesma version (currently `4.0.0`) |
| `data_dir` | Resolved data directory |
| `vault_path` | Resolved vault directory |
| `total` | Total memory count (any status) |
| `by_status` | Dict of `raw` / `processing` / `processed` / `published` / `archived` |
| `vectors` | Number of vectors in the local vector index (`vectors.db`) |

### Example

```bash
vesma stats
# status: ok
# version: 4.0.0
# data_dir: /home/you/.vesma/data
# vault_path: /home/you/.vesma/vault
# total: 142
# by_status: {'raw': 5, 'processing': 0, 'processed': 12, 'published': 120, 'archived': 5}
# vectors: 120
```

---

## `fts`

FTS5 index management. One action is currently defined: `rebuild`.

```text
vesma fts ACTION
```

| Argument | Description |
|----------|-------------|
| `ACTION` (positional) | `rebuild` — rebuild the FTS5 index and report the number of rows indexed. Any other value exits with an error. |

### Example

```bash
vesma fts rebuild
# ✓ FTS5 index rebuilt: 142 rows indexed
```

---

## `processor`

Background processor (knowledge pipeline) management: inspect the queue, run a manual pass, or start / stop the background loop.

```text
vesma processor ACTION
```

| Argument | Description |
|----------|-------------|
| `ACTION` (positional) | `status` — queue depth, last processed timestamp, running flag. `run` — one synchronous pipeline pass (cluster → synthesize → quality gate → publish). `start` — start the background processor. `stop` — stop it. |

The `run` summary reports `clusters`, `synthesized`, `published`, and `failed_quality_gate` counts.

### Example

```bash
vesma processor run
#   clusters: 3
#   synthesized: 3
#   published: 2
#   failed_quality_gate: 1
```

### Related

- HTTP equivalent: [`POST /process`](http-api.md#post-process--run-end-to-end-pipeline)

---

## `reindex`

Rebuild the vector index for all published memories — re-embeds every `published` entry and upserts it into `vectors.db`. Use after enabling embeddings or switching embedding models.

```text
vesma reindex [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--batch-size / -b` | `100` | Batch size for embedding. |
| `--config / -c` | — | Path to `config.yaml`. |

### Example

```bash
vesma reindex --batch-size 50
#   total: 120
#   indexed: 120
#   failed: 0
```

---

## `filter`

Run the Context Filter (M10) on a memory and print the clean content plus reduction stats. With `--all`, re-runs the filter over every memory and reports aggregate counts.

```text
vesma filter [MEMORY_ID] [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `MEMORY_ID` (positional) | — | Memory to filter. Omit when using `--all`. |
| `--profile / -p` | auto-detected | `log`, `terminal`, `code`, `docs`, `web`, or `default`. |
| `--budget / -b` | — | Token budget for truncation. |
| `--all` | `false` | Re-run the filter on ALL memories; existing `clean_content` is overwritten with fresh filter output. |
| `--config / -c` | — | Path to `config.yaml`. |

> Re-filtering with a different profile produces different `clean_content`. The filter is idempotent only when the same profile is used.

### Example

```bash
vesma filter 550e8400-e29b-41d4-a716-446655440000 --profile terminal
# ✓ Filtered: 550e8400-e29b-41d4-a716-446655440000
#   profile: terminal
#   clean_content:
#   ...
```

### Related

- [context-filter.md](context-filter.md) — profiles, pipeline stages, auto-filter behaviour
- MCP tool: [`mnemos_filter`](mcp-tools.md#mnemos_filter)

---

## `serve`

Start the Vesma HTTP API server (FastAPI / Uvicorn).

```text
vesma serve [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | `settings.api.host` (127.0.0.1) | Bind address. |
| `--port` | `settings.api.port` (8787) | Bind port. |
| `--log-file` | — | Override the config log-file path; passing it enables file logging. |
| `--config / -c` | — | Path to `config.yaml`. |

The server uses `uvicorn[standard]` (HTTP/1.1 + WebSockets). The number of workers comes from `settings.runtime.uvicorn_workers`.

> **Security.** The default bind is `127.0.0.1`. Do not expose this port to a public network without putting a reverse proxy with authentication in front. See [security.md](../admin/security.md).

### Mesh server (native wiring)

When `mesh.enabled` is `true` in the config, `vesma serve` additionally starts the `MnemosCore` gRPC server on the configured Unix socket **in the same process**, next to the HTTP API — the `mnemos-mesh` binary dials that socket. Startup logs one line: `mesh server listening on <path>`. On `SIGINT`/`SIGTERM` uvicorn drains the HTTP side first, then the gRPC server drains (2 s grace) and removes its socket file.

```yaml
mesh:
  enabled: true
  socket_path: /run/vesma/core.sock
  # Group access for shared-volume deployments (Kubernetes fsGroup,
  # compose `user: <uid>:<gid>`): socket 0660 / dir 0770 instead of the
  # owner-only 0600 / 0700, so the mesh binary can dial the socket as a
  # different uid in the same gid.
  socket_group_access: true
```

With `mesh.enabled: false` (the default) the command behaves exactly as before — uvicorn only.

#### Mesh TCP leg (optional, W2.5 dual-mode)

When `mesh.tcp.enabled` is `true` (requires `mesh.enabled: true`), the **same** gRPC server additionally listens on TCP with mesh-CA mTLS (ADR-0019): every caller must present a client certificate chaining to the mesh CA (`RequireAndVerifyClientCert`) — anonymous TLS is rejected at the handshake. Startup logs one line: `mesh tcp leg listening on <bind>:<port>`. A failed bind crashes the process (fail-fast, ADR-0019 amendment 3c — there is deliberately no k8s probe on 8790). The default `bind: 127.0.0.1` is Phase 1 (sidecar); Phase 2 (standalone mesh) opens `0.0.0.0` plus a NetworkPolicy ingress rule as an explicit operator action.

```yaml
mesh:
  enabled: true
  tcp:
    enabled: true          # default false — no TCP port at all when off
    port: 8790             # 0 = ephemeral (tests/diagnostics only)
    bind: 127.0.0.1
    tls:
      # Chart-facing key (ADR-0019 amendment 3d/3e): the name of the
      # k8s Secret holding the mnemos-core identity leaf
      # (mnemos-core-grpc-tls, common mesh CA). Informational for the
      # process itself — it only reads the mounted files below.
      existing_secret: mnemos-core-grpc-tls
      # Mounted PEM paths (from that Secret) — deployment contract:
      cert_file: /etc/vesma/mesh-tls/tls.crt   # mnemos-core identity leaf
      key_file: /etc/vesma/mesh-tls/tls.key
      ca_file: /etc/vesma/mesh-tls/ca.crt      # mesh CA — client-cert trust root
```

Optionally pin the mesh node's client-cert fingerprint per peer (`federation.peers.<id>.mtls_cert_fingerprint`, format `sha256:<hex>` of the DER leaf) — symmetric to the mesh's peer leg; a valid mesh-CA certificate from a different node is then refused with `PERMISSION_DENIED`.

### Examples

```bash
# Default bind
vesma serve

# LAN bind (dev box on your home network)
vesma serve --host 0.0.0.0 --port 8000

# Custom config
vesma serve --host 127.0.0.1 --port 9000 --config /etc/vesma/config.yaml

# Enable file logging without touching the config file
vesma serve --log-file ~/.mnemos/logs/serve.log
```

The full HTTP API surface is documented in [http-api.md](http-api.md). The Swagger UI is served at `http://HOST:PORT/docs`.

---

## `meta-poll`

Run one federation metadata poll pass now (S2 phase 2, poll-first metadata sync). This is the same path the background loop runs per tick — executed once, in the foreground, with a per-peer summary — for manual runs and diagnostics.

```text
vesma meta-poll [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--peer` | all poll targets | Poll only this peer id (must be a configured `meta_poll` target). |
| `--config / -c` | — | Path to `config.yaml`. |

Per peer, the command shells out to the mesh CLI (`mnemos-mesh sync-meta --config <mesh.yaml> --peer <id> --json [--since <rev>]`), parses the JSON page, and imports the records in-process through the gated upsert (`upsert_index_entries` with `sender_peer_id`): no-federate tag, title blocklist, origin-mutation guard and LWW conflict resolution all apply. **Metadata-only**: the poll path touches `federation_index` and the poll watermark table, never `memories` or the pipeline. Each successful pass prints a line like:

```text
✓ peer=mnemos-B fetched=12 accepted=10 rejected_by_gate=1 stale=1 pages=1 latest_rev=47
```

Exit code is `1` when any polled peer failed (non-zero CLI exit, broken JSON, timeout) — the failure is recorded in `federation_poll_state.last_error` and retried on the next pass; the watermark (`since_rev`) only advances on success.

### Background loop (`federation.meta_poll`)

The background poller runs inside `vesma serve` as an asyncio task and is **default-off** — a config without the `meta_poll` key parses unchanged and the process behaves bit-for-bit as before (S1 / S2 phase 1).

```yaml
federation:
  meta_poll:
    enabled: true                      # default false — opt-in
    interval_seconds: 300              # default 300; clamped to [60, 86400]
    peers: all                         # "all" (every federation.peers key) or an explicit list
    mesh_config_path: /etc/vesmaro/mesh.yaml  # REQUIRED when enabled (passed as --config to the CLI)
    mesh_bin: mnemos-mesh              # binary name (PATH) or absolute path
```

Notes:

- `enabled: true` without `mesh_config_path` is a config error (startup fail-fast). An explicit `peers` list referencing an unknown peer id is also a config error — a typo must never become a silently skipped peer.
- The per-peer watermark is persisted after every successfully imported page, so a restart (or crash) mid-peer resumes exactly after the last consumed row; the next request carries `--since <watermark>`. `has_more: true` pages chain immediately (up to 10 pages per peer per tick).
- One peer's failure never stops the loop: it is logged at INFO, written to `federation_poll_state.last_error`, and the peer is retried on the next tick (the interval is the backoff).
- With `runtime.uvicorn_workers > 1` every worker process runs its own poller — imports are idempotent (LWW), so this is redundant fetches, never corruption; `serve` prints a warning.

---

## `mcp-server`

Start the Vesma MCP server over **stdio** for VS Code Copilot (or any MCP-aware client).

```text
vesma mcp-server [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--config / -c` | — | Path to `config.yaml`. |

The server speaks JSON-RPC 2.0 over stdin/stdout. There is no TCP port. The process blocks until EOF or `Ctrl+C`.

### Examples

```bash
# Direct invocation (for debugging)
vesma mcp-server

# With auto-collect mode (4.x env spelling; 5.x: VESMARO_AUTO_COLLECT)
MNEMOS_AUTO_COLLECT=1 vesma mcp-server

# From VS Code (mcp.json snippet)
```

```jsonc
{
  "servers": {
    "vesma": {
      "type": "stdio",
      "command": "vesma",
      "args": ["mcp-server"]
    }
  }
}
```

See [mcp-tools.md](mcp-tools.md) for the full tool list and [getting-started.md#run-the-mcp-server](getting-started.md#connect-your-harness-mcp) for the VS Code wiring.

---

## `migrate from-ai-brain`

One-shot migration from a legacy `ai-brain` install (M13).

```text
vesma migrate from-ai-brain [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--source` | `~/.ai-brain` | Legacy ai-brain data directory (must contain `ai_brain.db`). |
| `--vault` | `~/brain-vault` | Legacy ai-brain vault directory (Obsidian mirror). |
| `--dry-run` | `false` | Show what would be migrated, write nothing. |
| `--config / -c` | — | Path to `config.yaml`. |

The migrator:

- Translates legacy `source` values (e.g. `telegram` → `mcp`).
- **Patches the tag contract** — every legacy entry gets `project:legacy`, `agent:unknown`, `mnemos:legacy` added if missing.
- Preserves the original `status` (`raw` / `processing` / `processed` / `published` / `archived`).
- Migrates `content_ru` / `content_en` columns into `metadata` (no data loss).
- Migrates `parent_ids` into `metadata.parent_ids`.

### Examples

```bash
# Dry run first (recommended)
vesma migrate from-ai-brain --dry-run

# Real run with default paths
vesma migrate from-ai-brain

# From a tarball restore
vesma migrate from-ai-brain --source /tmp/restore/.ai-brain --vault /tmp/restore/brain-vault
```

Output is a one-line summary:

```text
✓ Memories migrated: 1 247
✓ Vault files migrated: 1 247
```

If you see `Errors: N`, the `summary.errors` list (printed to stderr at DEBUG level) tells you which rows failed. They are typically schema-corrupt rows that you can ignore or fix by hand in SQLite.

---

## `auth`

Manage API auth tokens and TOTP 2FA (ADR-0014). Two sub-groups: `auth token` (bearer tokens) and `auth totp` (second factor). Token secrets are stored hashed in the SQLite DB next to your memories.

### `auth token create`

Mint a new bearer token and print it **once**.

| Option | Default | Description |
|--------|---------|-------------|
| `--name / -n` | — | Human-readable label. |
| `--expires / -e` | — | ISO-8601 expiry, e.g. `2027-01-01`. Naive dates are normalised to UTC. |
| `--no-totp` | `false` | Create a token usable directly as a bearer without the login/verify/session flow (sets `totp_required=false`). By default tokens require TOTP. |
| `--config / -c` | — | Path to `config.yaml`. |

### `auth token list`

List all tokens — IDs and metadata only, never secrets.

### `auth token revoke TOKEN_ID`

Permanently revoke a token (positional `TOKEN_ID` argument).

### `auth totp`

| Subcommand | Required options | Purpose |
|------------|------------------|---------|
| `enroll` | `--token-id` | Generate a TOTP secret and print the provisioning URI + optional ASCII QR. Requires `MNEMOS_API__TOTP_MASTER_KEY` to encrypt the secret. |
| `disable` | `--token-id` | Remove the TOTP secret from a token (disables 2FA for it). |
| `test` | `--token-id`, `--code` | Verify a 6-digit code against the enrolled secret (operator smoke-test). |

### Example

```bash
vesma auth token create --name "laptop" --expires 2027-01-01
# ✓ Token created:
#   token_id : 7c9e6679-7425-40de-944b-e07fc1f90ae7
#   bearer   : <plaintext token — store it now, it will not be shown again>
```

---

## `completion`

Install shell completion for the `vesma` CLI. With no arguments it auto-detects the current shell from `$SHELL`, writes the completion script to `~/.mnemos/completion/mnemos.<shell>`, and adds a single guarded `source` line to your rc file (`~/.bashrc` / `~/.zshrc`; fish auto-sources its completions directory). Idempotent — re-running does not duplicate the source line and migrates away the old `eval`-based format.

```text
vesma completion [SHELL] [OPTIONS]
```

| Argument / Option | Default | Description |
|-------------------|---------|-------------|
| `SHELL` (positional) | auto from `$SHELL` | `bash`, `zsh`, or `fish`. |
| `--show-instructions` | `false` | Print manual install steps for all supported shells; no files modified. |

### Example

```bash
vesma completion bash
# ✓ Installed bash completion → /home/you/.mnemos/completion/vesmaro.bash
#   Source line added to /home/you/.bashrc
#   Restart your shell or run: source /home/you/.bashrc
```

---

## `doctor`

Run Vesma health checks: config, data dir, vault, SQLite DB, vector store, MCP server registration, integration layer, agent wiring, tag contract.

```text
vesma doctor [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--json` | `false` | Emit results as JSON (for scripting / CI) instead of a table. |
| `--fix` | `false` | Auto-fix WARN-level checks (stale integration, unwired agents, missing MCP registration). FAIL-level checks are not auto-fixable. |
| `--dry-run` | `false` | With `--fix`: preview what would be fixed without executing. |
| `--paths` | `false` | Print all resolved paths (data dir, vault, logs, cache, completion) and exit. |

Exit codes: `0` = all checks pass, `1` = one or more checks failed, `2` = warnings only.

> `doctor` does not take `--config`; it reads the config from `$VESMARO_CONFIG` (4.x spelling: `MNEMOS_CONFIG`, deprecated) or the default search path (`./config.yaml`, `~/.mnemos/config.yaml`).

### `doctor --paths`

Shows every path Vesma uses, resolved from config and environment:

```bash
vesma doctor --paths
# data_dir:      /home/you/.vesma/data
# vault_path:    /home/you/.vesma/vault
# log_file:      /home/you/.mnemos/logs/mnemos.log
# cache_dir:     /home/you/.vesma/cache
# completion:    /home/you/.vesma/completion
# config_file:   /home/you/.vesma/config.yaml
```

Use this to verify the consolidated `~/.mnemos/` layout after upgrade or migration.

### `doctor --fix` and `--dry-run`

With `--fix`, WARN-level checks are repaired in place (stale integration → `integration update`, unwired agents → `vesma integration setup`, missing MCP registration → MCP setup); the affected checks are then re-run and the new status reported. Combine with `--dry-run` to preview the fixes without executing them. `--json --fix` reports the `fixed` / `fix_skipped` lists in the JSON payload.

```bash
# Preview only
vesma doctor --fix --dry-run

# Apply fixes
vesma doctor --fix

# CI: machine-readable verdict, no fixes
vesma doctor --json
```

---

## `memory status`

Read-only per-harness memory-attachment report (ADR-0034, MS-0). Never
writes, never touches the network; MCP configs are read to enumerate server
KEYS only (never env values or command lines), and store markers are
reported as existence + mtime.

```text
vesma memory status [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--target <name>` | all detected | Narrow to specific harness(es); repeatable. |
| `--home <dir>` | `~` | Inspect an alternate home directory. |

Per harness the table shows: pack attachment (stamps: attached / stale /
missing counts), MCP registration (whether the `vesma` server entry is
present), external memory engines (other server keys seen in the harness
config), the built-in store markers (`data`, `vault`, `mnemos.db` —
existence + mtime) and the active precedence mode (`overlay+mirror`;
`replace`/`off` arrive with MS-1).

### Example

```bash
vesma memory status
# ┌─────────┬──────────────┬─────────┬─────────────────┬─────────────┬───────────────┐
# │ Harness │ Pack         │ MCP     │ External engines │ Vesma store │ Precedence    │
# │ zcode   │ attached (25)│ vesma ✓ │ obsidian-mcp     │ db ✓ 10:01  │ overlay+mirror│
```

Exit codes: `0` when the report was produced (it is a status surface, not a
health gate — `doctor` and `integration verify` govern health).

---

## `update`

One command for the whole update family: report every update surface found on this machine, upgrade the pip user-site install (plus the global npm package, best-effort), pin a version for rollback, or manage the weekly auto-update timer.

```text
vesma update [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--check` | `false` | Report surfaces without changing anything (same as no flags). |
| `--yes` | `false` | Perform the update: `pip install --user --upgrade`; the npm package is updated best-effort. |
| `--scope` | `user` | Update scope. Only `user` exists — prod venvs, Go binaries and containers are never auto-updated. |
| `--to <version>` | — | Pin the pip target version (rollback path), e.g. `--to 5.1.1`. Requires `--yes`. |
| `--install-timer` | `false` | Install and enable the weekly systemd user update timer (`vesma-update.timer`, `Persistent=true`). |
| `--uninstall-timer` | `false` | Disable and remove the timer and its service unit. |

### Report-only surfaces

The default report lists every update surface found on this machine. Only the pip user-site (and npm) is ever changed; prod venvs and Go binaries are report-only by design:

- **pip dist** — the surface `--yes` upgrades (`--break-system-packages` is appended automatically under PEP 668 externally-managed interpreters); every run appends a record to `~/.local/share/vesma/update-history.json`.
- **npm `@vesmaro/vesma`** — upgraded best-effort with `--yes` when installed.
- **prod venvs** — `MANUAL GATE` in the report; update them via the upgrade runbook.
- **Go binaries** (`vesmaro-agent`/`vesma-agent`, `mnemos-mesh`/`vesma-mesh`) — updated via goreleaser releases with checksum verification.
- **container images** — CI release artifacts.

### Example

```bash
# Report all update surfaces
vesma update

# Upgrade the user-site install
vesma update --yes --scope=user

# Roll back to a pinned version
vesma update --yes --to 5.1.1

# Weekly auto-update of the user-site (survives reboot)
vesma update --install-timer
```

Restart running clients (MCP / `serve`) after a successful update to pick up the new version.

---

## `logs`

View pipeline traces (M6 explainability layer) — a compact table over the append-only `traces` table.

```text
vesma logs [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--task / -t` | — | Filter by task label (`cluster`, `synthesize`, `publish`, `recall`). |
| `--project / -p` | — | Filter by project slug. |
| `--limit / -l` | `50` | Maximum number of traces to show. |
| `--since` | — | Only traces after this ISO date (e.g. `2026-06-01`). |
| `--follow / -f` | `false` | Poll every 2 s and print new rows (`tail -f` style). Stop with `Ctrl+C`. |
| `--config / -c` | — | Path to `config.yaml`. |

### Example

```bash
vesma logs --task cluster --project vesma --limit 20

# Watch the pipeline live
vesma logs --follow
```

### Related

- HTTP equivalent: [`GET /traces`](http-api.md#get-traces--list-pipeline-traces)

---

## `scanner`

Background secrets scanner — Layer 2 of the federation defence-in-depth. The scanner periodically re-scans the corpus for secrets missed at write time and auto-tags hits `mnemos:no-federate` so they are excluded from all external exchange. These subcommands are the manual trigger and status view.

### `scanner run`

Run one scanner pass synchronously and print the summary.

| Option | Default | Description |
|--------|---------|-------------|
| `--full` | `false` | Force a full corpus scan (ignore the incremental boundary). Default is incremental: only records modified since the last successful scan. |
| `--config / -c` | — | Path to `config.yaml`. |

The summary reports `records_scanned`, `records_tagged`, `records_skipped`, `duration_sec`, matched pattern names with counts (never raw values), and the timestamp.

### `scanner status`

Print the scanner's current state — enabled, running, configured interval and incremental mode, last scan timestamp, cumulative records tagged, next scheduled run.

### Example

```bash
vesma scanner run --full
# ✓ Scan complete (full)
#   records_scanned: 142
#   records_tagged:   0
#   records_skipped:  2
#   duration_sec:     1.83
#   patterns_matched: (none)
#   timestamp:        2026-09-05T12:00:00+00:00
```

### Related

- [sync.md](sync.md#vesmano-federate-exclusion) — what `mnemos:no-federate` excludes

---

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | User error (missing argument, invalid tag, etc.) |
| 2 | `vesma doctor`: one or more checks warn, nothing is broken |

The CLI does not return non-zero for "no results" — `vesma search` exits 0 with an empty table.

---

## See also

- [getting-started.md](getting-started.md) — first-run walkthrough
- [mcp-tools.md](mcp-tools.md) — the same capabilities exposed over MCP
- [http-api.md](http-api.md) — the same capabilities exposed over HTTP
- [context-filter.md](context-filter.md) — filter profiles used by `add --dry-run` and `filter`
- [tag-contract.md](tag-contract.md) — the tag schema enforced here
- [runbooks/migrate.md](../admin/runbooks/migrate.md) — operational migration guide
- [architecture overview](../architecture/overview.md) — system shape

---

_Last updated: 2026-10-01_
