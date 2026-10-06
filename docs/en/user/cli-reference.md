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
| [`ingest`](#ingest) | Ingest external content: `ingest url URL` / `ingest file PATH` |
| [`search`](#search) | Hybrid FTS5 + vector search |
| [`recall`](#recall) | List recent memories; `recall agent` scopes to one agent |
| [`tags validate`](#tags-validate) | Validate the tag contract across a vault |
| [`tags audit`](#tags-audit) | Scan for tag-contract non-conformance; `--apply` heals additively |
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
| [`doctor`](#doctor) | Diagnose the installation (`fix` / `paths` subcommands; checks: config, database, vault, …) |
| [`update`](#update) | Check for updates / update the user-site install |
| [`export`](export-import.md) | Export memories to a JSON / SQLite backup (dedicated page) |
| [`import`](export-import.md) | Import memories from an export file (dedicated page) |
| [`logs`](#logs) | View pipeline traces |
| [`sync`](sync.md) | Federation batch sync export / import (dedicated page) |
| [`meta-poll`](#meta-poll) | Federation metadata poll: run one poller pass manually (S2 phase 2) |
| [`scanner`](#scanner) | Background secrets scanner: `run` / `status` |
| [`awareness`](#awareness) | Native awareness heartbeat: `get` / `set` (mode switch) / `stats` (gate metrics) |

> The `tags` group also provides `tags normalize` and `tags rename` (bulk prefix rename with dry-run); `migrate tags` is a deprecated alias for `vesma tags rename --from gcw: --to mnemos: --no-dry-run`. The `mnemos:` prefix in tag namespaces is a data contract unchanged by the rebrand (6.0 decision) — renames of the project namespace do not touch it.

---

## Global options

Most subcommands accept a `--config / -c` flag pointing at a YAML file. Search order is:

1. `--config` argument (if present)
2. `$VESMA_CONFIG` env var (canonical since 5.3; the 5.0–5.2 spelling `VESMARO_CONFIG` stays accepted until 6.0; the 4.x spelling `MNEMOS_CONFIG` is deprecated)
3. `./config.yaml` in the current working directory
4. `~/.mnemos/config.yaml`

```bash
vesma --help
vesma add --help
```

The only other global flags are `--version / -V` (print the version) and `--verbose / -v` (DEBUG logging for `vesma serve` and `vesma mcp-server`). To change the log level permanently, set `logging.level` in the config file or the corresponding env var:

```bash
VESMA_LOGGING__LEVEL=DEBUG vesma serve      # canonical (5.3+)
# 5.0–5.2 read VESMARO_LOGGING__LEVEL; 4.x images read MNEMOS_LOGGING__LEVEL (both deprecated)
```

---

## Environment variables

All settings are env-overridable via the `VESMA_` prefix (canonical since 5.3). Nested keys use `__` as the delimiter.

| Variable (canonical) | Default | Purpose |
|----------|---------|---------|
| `VESMA_CONFIG` | — | Path to `config.yaml` |
| `VESMA_MNEMOS__DATA_DIR` | `~/.mnemos/data` | SQLite DB + vector index (canonical form) |
| `VESMA_MNEMOS__VAULT_PATH` | `~/.mnemos/vault` | Obsidian mirror directory (canonical form) |
| `VESMA_MNEMOS__STRICT_TAG_CONTRACT` | `true` | Enforce M2 tag schema |
| `VESMA_API__HOST` | `127.0.0.1` | Default for `vesma serve` |
| `VESMA_API__PORT` | `8787` | Default for `vesma serve` |
| `VESMA_SEARCH__HYBRID_ALPHA` | `0.5` | Vector weight in RRF fusion |
| `VESMA_EMBEDDING__PROVIDER` | `nano` | `nano` (vesma-embed-v1, bundled) / `onnx` / `ollama` / `sentence-transformers` |
| `VESMA_LLM__PROVIDER` | `ollama` | LLM for synthesis + context filter |
| `VESMA_LLM__MODEL` | `qwen2.5:3b` | LLM model name |
| `VESMA_AUTO_COLLECT` | `0` | Set `1` to enable MCP auto-collect mode |
| `VESMA_LOGGING__LEVEL` | `INFO` | Python logging level |

> **Deprecated spellings: `VESMARO_*` (5.0–5.2) and `MNEMOS_*` (4.x).** The table above lists the canonical `VESMA_*` names (rebrand train 5.3.0; ADR-0031 dual-prefix contract). When the `VESMA_` twin is absent, the deprecated `VESMARO_*` spelling is still honoured — it retires no earlier than 6.0, so existing deployments keep working unchanged; when both are set, `VESMA_` wins. The 4.x-era `MNEMOS_*` spellings (`MNEMOS_CONFIG`, `MNEMOS_API__HOST`, `MNEMOS_API__PORT`, `MNEMOS_SEARCH__HYBRID_ALPHA`, `MNEMOS_EMBEDDING__PROVIDER`, `MNEMOS_LLM__PROVIDER`, `MNEMOS_LLM__MODEL`, `MNEMOS_AUTO_COLLECT`, `MNEMOS_LOGGING__LEVEL`) are no longer read. The short forms `VESMA_DATA_DIR` / `VESMA_VAULT__VAULT_PATH` (deprecated `VESMARO_DATA_DIR` / `VESMARO_VAULT__VAULT_PATH` spellings likewise accepted until 6.0) are #139 compatibility aliases for the nested canonical names — canonical env wins on conflict.

> **Legacy aliases.** The short forms predate the nested naming and are kept for compatibility (#139). Both forms work. On conflict the canonical env name — and an explicit value in the config file — wins over the legacy alias; the alias only fills the gap that would otherwise fall through to the default.

---

## `add`

Create a new memory entry (quick-capture). File and URL ingest are subcommands now — see [`ingest`](#ingest) (CLI-architecture rework W3: a subcommand names the function, a flag only configures it).

```text
vesma add [CONTENT] [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `CONTENT` (positional) | — | Text to remember. If omitted, reads from stdin. |
| `--title / -t` | auto | Short title. Auto-generated from content if omitted. |
| `--tags / -T` | `""` | Comma-separated tags (e.g. `project:test,agent:me,mnemos:learning`). |
| `--source / -s` | `cli` | Memory source enum: `manual`, `web`, `file`, `mcp`, `obsidian`, `cli`, `rule`, `synthesized`. |
| `--type` | `note` | Memory type: `note`, `fact`, `snippet`, `bookmark`, `conversation`, `session_context`. |
| `--dry-run` | `false` | Validate tags and preview context-filter stats without saving. With `--file` (deprecated), previews the file's text. |
| `--config / -c` | — | Path to `config.yaml`. |

> **Deprecated flag forms: `--file / -f`, `--url / -u`.** The old flag forms still work as hidden deprecated aliases — identical behavior, plus a one-line `[deprecated]` hint on stderr. Use `vesma ingest file PATH` / `vesma ingest url URL` instead; the flags are not removed before 6.0.

> **Tag contract.** Every entry must have `project:<slug>`, `agent:<slug>`, and at least one `vesma:<subtype>`. The CLI enforces this in strict mode (the default). See [tag-contract.md](tag-contract.md) for the full schema.

### Examples

```bash
# Inline content
vesma add "Use uv, not pip" --tags project:vesma agent:tech-writer mnemos:learning

# With a title
vesma add "Always validate SQL with parameterized queries" \
  --title "SQL safety rule" \
  --tags "project:vesma,agent:security,mnemos:rule,severity:high"

# From stdin
echo "Pinned CVE-2026-45829 in chromadb 1.5.9" \
  | vesma add --tags project:vesma agent:sre mnemos:bug-pattern,severity:medium
```

---

## `ingest`

Ingest external content into the memory store: a web page or a local file's text. These subcommands carry the former `add --url` / `add --file` behavior (CLI-architecture rework W3).

### `ingest url`

Fetch a web page, extract the main text, and save it as a memory.

```text
vesma ingest url URL [OPTIONS]
```

| Argument / Option | Default | Description |
|-------------------|---------|-------------|
| `URL` (positional) | — | URL to fetch, extract, and save. |
| `--tags / -T` | `""` | Comma-separated tags. Required (tag contract). |
| `--config / -c` | — | Path to `config.yaml`. |

### `ingest file`

Save a local file's text content as a memory.

```text
vesma ingest file PATH [OPTIONS]
```

| Argument / Option | Default | Description |
|-------------------|---------|-------------|
| `PATH` (positional) | — | File whose text content is saved. |
| `--title / -t` | auto | Short title. Auto-generated from content if omitted. |
| `--tags / -T` | `""` | Comma-separated tags. |
| `--source / -s` | `cli` | Memory source enum (same values as `add`). |
| `--dry-run` | `false` | Validate tags and preview context-filter stats without saving. |
| `--config / -c` | — | Path to `config.yaml`. |

### Examples

```bash
# From a URL (fetches, extracts, saves)
vesma ingest url https://example.com/article --tags "project:research,agent:user,mnemos:learning"

# From a file
vesma ingest file ~/notes/architecture.md --tags "project:vesma,agent:tech-lead,mnemos:decision"

# Preview the filter stats for a file without saving
vesma ingest file ~/notes/architecture.md --dry-run --tags "project:vesma,agent:tech-lead,mnemos:decision"
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

List recent memories, optionally scoped to a project. Per-agent recall is the `recall agent` subcommand (CLI-architecture rework W2: a subcommand names the function, a flag only configures it).

```text
vesma recall [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--project / -p` | — | Project slug to filter on. |
| `--limit / -l` | `10` | Maximum results. |
| `--config / -c` | — | Path to `config.yaml`. |

The legacy `--agent / -a` flag still works as a hidden deprecated alias — identical behavior, plus a one-line `[deprecated]` hint on stderr. Use `vesma recall agent` instead; the flag is not removed before 6.0.

### `recall agent`

Per-agent recall (M3): one agent's entries, optionally query-matched. This is the same data the MCP tool [`mnemos_agent_recall`](mcp-tools.md#mnemos_agent_recall) returns.

```text
vesma recall agent SLUG [QUERY] [OPTIONS]
```

| Argument / Option | Default | Description |
|-------------------|---------|-------------|
| `SLUG` | — | Agent slug to recall for. |
| `QUERY` | — | Optional query — hybrid search scoped to the agent's entries (raw included). Without it: the N most recent entries for the agent, ordered by `created_at desc`. |
| `--project / -p` | — | Project slug to filter on. |
| `--limit / -l` | `10` | Maximum results. |
| `--config / -c` | — | Path to `config.yaml`. |

### Examples

```bash
# Most recent 10 entries for any agent
vesma recall

# Per-agent recall (M3)
vesma recall agent tech-writer

# Query-scoped per-agent recall
vesma recall agent sre "deploy checklist"

# Combined
vesma recall agent sre --project vesma --limit 25
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

## `tags audit`

Scan the SQLite store for tag-contract non-conformance and optionally heal. Every entry needs at least one `project:*`, one `agent:*` and one `mnemos:*` tag (the same contract the doctor's "Tag contract" check enforces); entries whose tags JSON is unparseable are flagged too.

```text
vesma tags audit [--apply] [--limit N] [--json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--apply` | `false` | Heal: add the missing contract prefixes to every non-conformant entry. Additive only — existing tags are never removed. Default is a dry-run report. |
| `--limit / -l` | `0` | Cap the number of LISTED rows (`0` = list all). The scan always covers the whole store. |
| `--json` | `false` | Emit the report as JSON (for scripting / CI). |

Heal policy (idempotent by construction — a second `--apply` run heals nothing): missing `mnemos:*` → `mnemos:legacy`; missing `agent:*` → `agent:user`; missing `project:*` → the row's `project` column value slug-normalized (lowercase, spaces → hyphens), or `project:unsorted` when the column is empty.

### Example

```bash
# Report only (default)
vesma tags audit

# Heal the store
vesma tags audit --apply
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

FTS5 index maintenance — a subcommand group with one verb: `rebuild`.

```text
vesma fts rebuild
```

The former positional form (`vesma fts ACTION`) is unchanged — the `rebuild` spelling is identical. Bare `vesma fts` shows the subcommand help; an unknown verb is a usage error (exit 2).

### Example

```bash
vesma fts rebuild
# ✓ FTS5 index rebuilt: 142 rows indexed
```

---

## `processor`

Background processor (knowledge pipeline) control — a subcommand group: inspect the queue, run a manual pass, or start / stop the background loop.

```text
vesma processor status|run|start|stop
```

| Subcommand | Description |
|------------|-------------|
| `status` | Queue depth, last processed timestamp, running flag. |
| `run` | One synchronous pipeline pass (cluster → synthesize → quality gate → publish). |
| `start` | Start the background processor. |
| `stop` | Stop it. |

The `run` summary reports `clusters`, `synthesized`, `published`, and `failed_quality_gate` counts.

The former positional form (`vesma processor ACTION`) is unchanged — the verb spellings are identical. Bare `vesma processor` shows the subcommand help; an unknown verb is a usage error (exit 2).

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
    mesh_config_path: /etc/vesma/mesh.yaml  # REQUIRED when enabled (passed as --config to the CLI)
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

# With auto-collect mode (4.x env spelling; canonical 5.3+: VESMA_AUTO_COLLECT)
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

Install shell completion for the `vesma` CLI. Vesma ships its own completion engine (the hidden `vesma __complete` command): the installer writes per-shell scripts that introspect the live command tree, so commands, nested subcommands (any depth), option names and option/enum values all complete **with descriptions**. Descriptions are rendered by zsh and fish; bash's readline cannot render descriptions at all, so bash completes values only.

With no arguments it auto-detects the current shell from `$SHELL`, writes the completion script to `~/.mnemos/completion/vesma.<shell>`, and adds a single guarded `source` line to your rc file (`~/.bashrc` / `~/.zshrc` — put it after `compinit`; fish auto-sources its completions directory). The scripts are bound to the program name you invoked (`vesma`) plus legacy aliases that exist on PATH (`vesmaro`, and `mnemos` when installed), so Tab works for every way you call the binary. Idempotent — every run rewrites the scripts and keeps exactly one canonical source line, migrating away ALL legacy forms: old `eval "$(… --show-completion …)"` lines, pre-rebrand `mnemos.bash`/`vesmaro.bash` one-liners and `if [ -f … ]; then source …; fi` blocks, and stale marker comments.

Integrity guarantees: rc edits are block-aware and validated. The legacy migration operates on whole shell constructs — a matched `if …; then` line removes the entire if/then(/else)/fi block, and orphaned control lines (`fi`, `then`, `else`, `done`) left behind by older partial edits are cleaned up too, so a half-removed legacy block can no longer abort parsing of the rest of your rc (a non-parsing rc silently disables everything below the break, completion included). After every rc write the result is checked with `bash -n` (or `zsh -n` when a zsh binary exists; fish needs no check) and the original content is restored verbatim if the file would not parse, with the installer exiting non-zero. `vesma doctor` reports the same damage as a Completion warning with the exact failing line number and text.

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
# ✓ Installed bash completion → /home/you/.mnemos/completion/vesma.bash
#   Source line added to /home/you/.bashrc
#   Restart your shell or run: source /home/you/.bashrc
```

After restarting the shell (zsh shown — descriptions render in the menu):

```zsh
vesma tags <TAB>
# validate  -- Validate tag contract across an existing vault.
# normalize -- Normalize project:/agent: tag case to lowercase across all memories.
# rename    -- Bulk rename tags matching `--from <prefix>` → `--to <prefix>`.
# audit     -- Scan for tag-contract non-conformance; optionally heal.
```

> Verify the installation anytime with `vesma doctor` — the "Completion" check passes when the script file exists, the rc file carries the canonical source line, and the script binds the primary program name; otherwise it warns with the exact fix command.

---

## `doctor`

Run Vesma health checks: config, data dir, vault, SQLite DB, vector store, MCP server registration, integration layer, shell completion, agent wiring, tag contract.

```text
vesma doctor [OPTIONS]
vesma doctor fix [--dry-run] [--json]
vesma doctor paths [--json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--json` | `false` | Emit results as JSON (for scripting / CI) instead of a table. |

Exit codes: `0` = all checks pass (checks reported as `skip` — not applicable to this machine — count as pass), `1` = one or more checks failed, `2` = warnings only.

> `doctor` does not take `--config`; it reads the config from `$VESMA_CONFIG` (deprecated spellings: `VESMARO_CONFIG` until 6.0, `MNEMOS_CONFIG` on 4.x) or the default search path (`./config.yaml`, `~/.mnemos/config.yaml`).

### `doctor paths`

Shows every path Vesma uses, resolved from config and environment:

```bash
vesma doctor paths
# Root:         ~/.mnemos
# Data dir:     ~/.mnemos/data
# Vault:        ~/.mnemos/vault
# Logs:         ~/.mnemos/logs/vesma.log
# Cache:        ~/.mnemos/cache
# Completion:   ~/.mnemos/completion
# MCP config:   ~/.config/Code/User/mcp.json
```

Use this to verify the consolidated `~/.mnemos/` layout after upgrade or migration. With `--json`, the paths object is emitted for scripting.

### `doctor fix`

Auto-repairs WARN-level checks in place (stale integration → `integration update`, unwired agents → `vesma integration setup`, missing MCP registration → MCP setup); the affected checks are then re-run and the new status reported. `--dry-run` previews the fixes without executing them. `--json` reports the `fixed` / `fix_skipped` lists in the JSON payload.

```bash
# Preview only
vesma doctor fix --dry-run

# Apply fixes
vesma doctor fix

# CI: machine-readable verdict, no fixes
vesma doctor --json
```

> **Deprecated flag forms.** `vesma doctor --fix` and `vesma doctor --paths` still work (scripts may depend on them) but are hidden aliases of the `fix` / `paths` subcommands and print a one-line deprecation hint on stderr. Move to the subcommand spellings.

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

One command family for updating Vesma. Plain `vesma update` keeps its 5.2.0 behavior: report every update surface found on this machine and — in an interactive terminal, when a pip update is pending — ask `Apply update? [y/N]` and apply on confirmation. In non-interactive contexts (pipes, CI) it stays check-only and prints `apply with: vesma update apply`. The distinct operations are SUBCOMMANDS (standing design rule: flags do not replace subcommands); the old flag forms remain as hidden deprecated aliases — identical behavior plus a one-line stderr hint, so scripts and the shipped systemd unit (`vesma update --yes --scope=user`) keep working.

```text
vesma update                     # report + interactive apply prompt (5.2.0 behavior)
vesma update check
vesma update apply [OPTIONS]
vesma update timer install|uninstall|status
vesma update components [--json]
```

### Subcommands

| Subcommand | Description |
|------------|-------------|
| `check` | Report surfaces only — never applies, never prompts. Safe in pipes and CI. |
| `apply` | The apply path: `pip install --user --upgrade` plus the global npm package, best-effort. Never prompts (invoking `apply` IS the confirmation); `-y/--yes` is accepted as a no-op. `--to <version>` pins a (rollback) version; `--scope` — only `user` exists; `--verbose` prints the full pip output. |
| `timer install` | Install and enable the weekly systemd user timer (`Persistent=true`). Inside a distrobox/container this REFUSES with exit 1 by default — units installed there would be dead (#468). Run it on the host, or pass `--force` for the intentional box-aware install (units land in the HOST home, one ExecStart line per box). |
| `timer uninstall` | Disable and remove the timer; from a box it removes only this box's ExecStart line from the HOST units. |
| `timer status` | Unit presence, enabled state and last trigger (`--json` for scripting); inside a box it also reports the HOST units and names the box. |
| `components` | The component inventory (below) — local state only, no network. |

Deprecated flag aliases (each prints `use: vesma update …` once on stderr; stdout stays clean): `--check` → `check`; `--yes`/`-y` → `apply`; `--to`/`--scope` → `apply --to`/`apply --scope`; `--install-timer`/`--uninstall-timer` → `timer install`/`timer uninstall`. Options placed before the subcommand word are ignored with an explicit stderr note.

The check is cached for 24h; if the installed version is newer than the cached `latest` (right after a self-upgrade), the cache is re-checked once synchronously. When the installed version is still newer than everything published, the report says `newer than published latest (local build?)`.

### The pip alias family row

`vesma` and `vesma-memory-server` are canonical PyPI names of the SAME codebase (`mnemos-memory-server` is the deprecated legacy mirror — noted here, not in the table). The report collapses the aliases into ONE row, `pip: vesma (family)`: Installed = the detected dist and version, Latest = the family max over the aliases' published versions (fetched per alias, cached in the shared `update-check.json`), and the Note names the alias actually installed — e.g. `up to date (installed as vesma-memory-server — same codebase, alias package)`. With nothing installed the row reads `not installed — pip install --user vesma`.

### Component inventory

`vesma update components` shows what is installed and how each piece updates — every row is read from local state (files, sqlite, `systemctl --user`, `npm ls`), never from the network. Honest `-` where a component is absent or its version is not cheaply readable.

| Component | Installed | Update path |
|-----------|-----------|-------------|
| pip dist | `<dist> <version>` (first of `vesma-memory-server` / `vesma` found) | `vesma update apply` |
| integration pack | pack version + aggregate stale/missing across detected targets | `vesma integration update` (`setup` when files are missing) |
| cortex bundle | `vesma-cortex-v1` name + weights revision from the shipped manifest | ships with the wheel |
| embedder | model id + fingerprint (vector-store vintage when readable; `VINTAGE MISMATCH` on a stale index) | `vesma reindex` after a model switch |
| npm package `@vesmaro/vesma` | global version or `-` | `vesma update apply` (npm leg, best-effort) |
| update timer | enabled / installed (disabled) / not installed — plus the box note | `vesma update timer install` |
| prod venvs | comma-separated venv names, ONE row | MANUAL GATE — upgrade runbook |
| go binaries | comma-separated names, report only | goreleaser releases (checksums) |

### Example

```bash
# Report all update surfaces, then ask to apply (in a terminal)
vesma update

# Check only — canonical spelling of the old `--check`
vesma update check

# Apply without any prompt (scripts, CI; the old `--yes --scope=user`)
vesma update apply

# Apply with the full pip output
vesma update apply --verbose

# Roll back to a pinned version
vesma update apply --to 5.1.1

# Weekly auto-update of the user-site (run ON THE HOST — #468)
vesma update timer install

# What is installed, and what updates it
vesma update components
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

## `awareness`

Operator surface for the native awareness heartbeat (ADR-0035): view and
switch the delivery mode, and read the wave-0 gate metrics from the metrics
sidecar — no manual YAML edit, no raw SQL.

### `awareness get`

Show `awareness.native_heartbeat_mode` twice: the RAW value as written in
the resolved config file, and the EFFECTIVE value (the settings the next
server start will load — env overrides included). When the canonical env
override `VESMA_AWARENESS__NATIVE_HEARTBEAT_MODE` is set, the output names
it (`config file > env` per the dual-source precedence).

```bash
vesma awareness get
#   config file: /home/you/.mnemos/config.yaml
#   awareness.native_heartbeat_mode: shadow
#   effective: shadow
```

### `awareness set`

Switch the heartbeat mode end-to-end:

```bash
vesma awareness set shadow   # off | shadow | canary | on
```

The value is validated against the born-final mode ladder — an unknown
value is refused with the allowed list and nothing is written. The write
is atomic (tmp + rename) and preserves every other mapping in the file
(missing `awareness:` section is created additively). Because a running
server reads the config once at startup, the command always prints the
restart note; when the `vesma service` supervisor answers on its control
socket, the output names the exact restart verb.

| Mode | Meaning (ADR-0035) |
|------|--------------------|
| `off` *(default)* | The kill switch — the contour is fully inert. |
| `shadow` | Wave 0: compose + events in the metrics sidecar, nothing rendered. |
| `canary` | Wave 1: the envelope renders as the last `TextContent`. |
| `on` | Wave 2: full delivery. |

### `awareness stats`

Print the wave-0 gate metrics read from the `awareness_events` table of the
metrics sidecar (`<data_dir>/metrics.sqlite`) through the sink's own
connection — no raw SQL:

| Output | Meaning |
|--------|---------|
| `tool_call (denominator)` | Every dispatched MCP call — the funnel denominator. |
| `peer_write` | Write-class calls (the freshness numerator's start stamp). |
| `delta_available` / `heartbeat_delivery` | Probe hits and delivered tails, with the calm/delta split. |
| `heartbeat_suppressed` | Deliveries suppressed, broken down by reason (`rate_cap`, `probe_error`, …). |
| `tail token cost` | Sum and mean estimated tail tokens over the window's deliveries (budget input). |

| Option | Default | Description |
|--------|---------|-------------|
| `--window-hours / -w` | `24` | Window in hours (1..2160). |
| `--project / -p` | — | Scope the funnel to one project slug. |
| `--config / -c` | — | Path to `config.yaml`. |

### Example

```bash
vesma awareness stats
# awareness heartbeat — wave-0 funnel (window 24h)
#   tool_call (denominator): 812
#   peer_write: 23
#   delta_available: 9
#   heartbeat_delivery: 9
#     — state: calm 2 / delta 7
#   heartbeat_suppressed: 0
#   conflict_hint_emitted: 1
#   tail token cost: sum 640 over 9 deliveries (mean ~71)
#   sidecar: /home/you/.mnemos/data/metrics.sqlite
```

A missing sidecar prints a hint line and exits 0 (a broken metrics plane
must not make console reads fatal). Output is aggregates-only — identity
slugs never print (zero peer content, ADR-0035 CWE-359 posture).

### Related

- Mode ladder semantics: [mcp-tools.md](mcp-tools.md#native-awareness-heartbeat-adr-0035)
- Decision record: [ADR-0035](../../project/adr/0035-native-awareness-delivery.md)

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

_Last updated: 2026-10-06_
