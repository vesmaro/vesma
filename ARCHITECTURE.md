# Vesma — architecture

> Companion to [PLAN.md](PLAN.md). PLAN is the *how* (phases, tasks, ordering). ARCHITECTURE is the *what* (components, interfaces, data, decisions).

## 1. System overview

Vesma is a single-tenant memory/knowledge service for AI agents (primarily Copilot agents in VS Code, via MCP). It is forked from `ai-brain` and retains its core stack:

- **Runtime**: Python 3.11+, FastAPI HTTP API, Typer CLI, MCP server (stdio).
- **Storage**: SQLite (FTS5) for raw + processing + processed, SQLite + NumPy vector store (`vectors.db`) only for `published` knowledge units, Obsidian-compatible vault on disk for human-readable mirror.
- **Embeddings**: bundled `vesma-embed-v1` ONNX model (default `nano` provider, shipped at `src/mnemos/models/vesma-embed-v1/`) — privacy + offline, no external vector DB.
- **Packaging**: rootless `podman` container; systemd quadlet units; user-level install option.

### Conceptual layers

```mermaid
flowchart TB
    subgraph CLIENTS["Clients"]
        C1(["VS Code · Copilot\n(stdio MCP)"])
        C2(["CLI — vesma …"])
        C3(["HTTP API client"])
    end

    subgraph IFACE["Interface Layer"]
        MCP["mcp_server.py"]
        FAPI["api/main.py\nFastAPI"]
        TYPER["cli/main.py\nTyper"]
    end

    MGR(["MemoryManager\nmanager.py"])

    subgraph PROC["Processing Subsystems"]
        CF["Context Filter\nfilter/"]
        PP["Knowledge Pipeline\npipeline/"]
        RE["Recall Engine\nrecall/"]
        PE["Policy Engine\npolicy/"]
    end

    subgraph BG["Background Services"]
        WA["Watchers\nwatchers/"]
        AC["Auto-collect\nauto_collect.py"]
    end

    subgraph STORE["Storage Layer"]
        SQ[("SQLite\nFTS5 · traces · projects")]
        VS[("Vector Store\nnumpy + SQLite")]
        VLT[("Obsidian Vault\nmarkdown mirror")]
    end

    C1 -->|"stdio"| MCP
    C2 --> TYPER
    C3 --> FAPI

    MCP --> MGR
    TYPER --> MGR
    FAPI --> MGR

    MGR --> CF
    MGR --> PP
    MGR --> RE
    MGR --> SQ
    MGR --> VS
    MGR --> VLT

    CF -.->|"raw + clean"| SQ
    PP -->|"status transitions"| SQ
    PP -->|"published upsert"| VS
    RE -->|"FTS5 MATCH"| SQ
    RE -->|"cosine search"| VS

    PE -->|"schedule / trigger"| MGR
    WA -->|"file events"| MGR
    AC -.->|"checkpoint reminder"| MCP
```

## 2. Core data model

### `Memory` (single unified table, status-driven)

| field | type | notes |
|---|---|---|
| `id` | uuid | primary key |
| `content` | text | markdown body |
| `tags` | array<string> | validated by `TagContract` |
| `project` | string | denormalised from `project:*` tag |
| `agent` | string | denormalised from `agent:*` tag |
| `status` | enum | `raw \| processing \| processed \| published \| archived` |
| `quality_score` | float? | populated by synthesis / quality-gate |
| `confidence` | float? | populated by synthesis |
| `source_coverage` | int? | distinct source URLs / paths in cluster |
| `cluster_id` | string? | set during clustering |
| `derived_from` | array<uuid> | provenance for `processed`/`published` |
| `embedding_id` | string? | Vector id in `vectors.db` when published |
| `raw_content` | text? | immutable source payload (logs/stdout/html/etc.) |
| `clean_content` | text? | filtered projection used for recall/model input |
| `filter_profile` | string? | `log|terminal|code|docs|web|default` |
| `filter_stats` | json? | token + dedup reduction stats |
| `filter_version` | string? | filter pipeline version used for this record |
| `created_at` | datetime | |
| `updated_at` | datetime | |

### `TagContract`

Required composition for any `mnemos_add`:
- exactly one `project:<slug>` tag
- exactly one `agent:<slug>` tag (or `agent:user` for human-authored)
- ≥1 tag from `mnemos:*` namespace (`mnemos:session`, `mnemos:bug-pattern`, `mnemos:learning`, `mnemos:decision`, `mnemos:rule`, `mnemos:open-question`, `mnemos:checkpoint`, `mnemos:legacy`)
- Optional whitelisted prefixes: `severity:`, `stack:`, `applyTo:`, `source:`

Enforcement: at MCP layer when `strict_tag_contract=true` (default for new installs). Lax mode tags legacy records `mnemos:legacy` + `agent:unknown` automatically.

### `Trace`

Per-pipeline-step audit row (M6):
`task_label, project, step, item_id, llm_called, llm_done, cache_hit, fallback_used, latency_ms, tokens_in/out, tokens_per_sec, rationale_summary (≤200 chars, NO chain-of-thought)`.

### Data model diagram

```mermaid
classDiagram
    class Memory {
        +str id
        +str content
        +list~str~ tags
        +str project
        +str agent
        +MemoryStatus status
        +float quality_score
        +float confidence
        +int source_coverage
        +str cluster_id
        +list~str~ derived_from
        +str raw_content
        +str clean_content
        +str filter_profile
        +datetime created_at
        +effective_content() str
        +auto_title() str
    }
    class MemoryStatus {
        <<enumeration>>
        RAW
        PROCESSING
        PROCESSED
        PUBLISHED
        ARCHIVED
    }
    class TagContract {
        +list~str~ tags
        +bool strict
        +str project
        +str agent
        +list~str~ mnemos_subtypes
    }
    class Trace {
        +str id
        +str task_label
        +str project
        +str step
        +int latency_ms
        +int tokens_in
        +int tokens_out
        +str rationale_summary
        +datetime created_at
    }
    class Project {
        +str id
        +str name
        +list~str~ paths
        +datetime created_at
    }

    Memory --> MemoryStatus : has status
    Memory ..> TagContract : validated by
    Memory "0..*" --> "1" Project : belongs to
    Trace "0..*" --> "1" Project : logged for
```

## 3. Interfaces

### MCP tools (stable names — integration plugins reference these)

The MCP surface is **38 tools** (`mcp_server.py` `list_tools()`). The v1-era table of 11 is superseded; the current contract, grouped:

| Group | Tools |
|---|---|
| Memory operations | `mnemos_add`, `mnemos_search`, `mnemos_agent_recall`, `mnemos_recall_context`, `mnemos_save_context`, `mnemos_list_recent`, `mnemos_list_tags`, `mnemos_ingest_url` |
| Context assembly & hooks | `mnemos_assemble_context`, `mnemos_context_rewrite`, `mnemos_hooks`, `mnemos_filter`, `mnemos_compress`, `mnemos_retrieve`, `mnemos_align_prefix` |
| Tags & workflow | `mnemos_tags`, `mnemos_tags_rename`, `mnemos_workflow` |
| Import / export | `mnemos_export`, `mnemos_import` |
| Watch poll (project graph, ADR-0032 §3.2) | `mnemos_watch_start`, `mnemos_watch_stop`, `mnemos_watch_status` |
| Project graph (ADR-0032, on by default) | `mnemos_index_project`, `mnemos_project_graph_status`, `mnemos_search_graph`, `mnemos_trace_path`, `mnemos_get_file_outline`, `mnemos_get_code_snippet`, `mnemos_check_graph_coverage`, `mnemos_get_graph_schema`, `mnemos_list_graph_projects`, `mnemos_delete_graph_project` |
| Stats & pipeline | `mnemos_stats`, `mnemos_auto_collect_status`, `mnemos_reprocess` |

Full per-tool reference — input schemas, output shapes, JSON-RPC examples: [docs/en/user/mcp-tools.md](docs/en/user/mcp-tools.md).

### HTTP API

Mirrors MCP tools (`POST /memories`, `GET /recall/agent/{name}`, `POST /search`, etc.) plus pipeline endpoints `POST /process`, `POST /synthesize`, `POST /publish/{memory_id}`, `GET /memories?status=`, `GET /traces`, `GET /metrics`, the project-graph namespace `/graph/*`, and the watch poll `/watch/*`.

### CLI

`vesma add`, `vesma search`, `vesma recall --agent <x>`, `vesma tags validate`, `vesma migrate from-ai-brain`. Pipeline and DLQ operations (cluster, synthesize, publish, dlq retry/discard) are exposed over HTTP (`POST /process`, `POST /synthesize`, `POST /publish/{id}`, `/dlq/*`) and via `vesma processor run`, not as dedicated CLI verbs.

## 4. Knowledge pipeline (M4) — the core architectural addition

```mermaid
flowchart TD
    ADD["mnemos_add / ingest_url"]
    RAW[("status: raw")]

    subgraph CL["Cluster Worker — pipeline/cluster.py"]
        CL1["group by embedding similarity\nassign cluster_id"]
    end

    PROC[("status: processing")]

    subgraph SY["Synthesize Worker — pipeline/synthesize.py"]
        SY1["LLM draft synthesis\nidempotency: hash(cluster_id, prompt_v, model_v)"]
    end

    PCED[("status: processed")]

    subgraph QG["Quality Gates — pipeline/quality_gate.py"]
        QG1{"quality_score\nconfidence\nsource_coverage"}
    end

    PUB[("status: published")]
    VEC[("Vector Index\nVectorStore")]

    subgraph DLQ_B["DLQ — policy/dlq.py"]
        DLQ["Dead-Letter Queue\nfailed synthesis items"]
    end

    ADD --> RAW
    RAW --> CL
    CL --> PROC
    PROC --> SY
    SY --> PCED
    PCED --> QG
    QG1 -->|"all thresholds pass"| PUB
    QG1 -->|"any threshold fails"| DLQ
    PUB --> VEC
    DLQ -->|"retry (exp. backoff)"| SY
    DLQ -->|"max retries reached"| PCED
```

**Key invariant**: only `status="published"` ever lives in the vector index. This is what makes hybrid recall high-signal: noise is filtered upstream by quality gates, not by ranking heuristics.

## 4a. Context Filter (M10) — pre-LLM token-noise reduction

Context Filter sits between interface input and downstream pipeline/recall so the model receives concise, semantically complete context instead of raw noise.

### Invariant

- Filtering never destroys data.
- `raw_content` is always retained for audit/drill-down.
- `clean_content` is the default payload for retrieval and model-facing flows.

### Pipeline

1. **Dedup** (`dedup.py`) — exact + near-duplicate suppression.
2. **Noise strip** (`noise.py`) — ANSI escape removal, progress bars, repeated separators, timestamp prefixes.
3. **Signal extract** (`extract.py`) — keep errors/warnings/exit-status + informative slices for large outputs.
4. **Compress** (`compress.py`) — semantic compression for repetitive blocks.
5. **Token estimate** (`tokens.py`) — preflight token budgeting and reduction accounting.

### Profiles

Configured in `~/.mnemos/filter_profiles.yaml`:

- `log`
- `terminal`
- `code`
- `docs`
- `web`
- `default`

Selection priority: explicit request → `source:` tag hint → content heuristics → `default`.

### API behavior

- `mnemos_add`: optional `filter_profile`, stores both raw and clean forms.
- recall/search tools return `clean_content` by default.
- `include_raw=true` enables drill-down to source payload.

## 5. Policy engine (M5)

Declarative YAML rules (`~/.mnemos/policies.yaml`):
- Auto-publish thresholds (quality + confidence + source-coverage).
- Defer / archive rules based on age, status, cluster size.
- Per-project overrides.

Reliability primitives:
- **Idempotency** — synthesis is keyed on `hash(cluster_id, prompt_version, model_version)`. Repeats return cached result.
- **DLQ** — failed synthesis lives here; inspected and retried over HTTP (`GET /dlq`, `POST /dlq/{id}/retry`, `DELETE /dlq/{id}`).
- **Retry** — exponential backoff with jitter; capped attempts.

## 6. Recall & ranking

- **FTS5**: SQLite full-text index over `content` + `tags`.
- **Vector**: SQLite + NumPy (`vectors.db`) on `published` only.
- **Fusion**: Reciprocal Rank Fusion (RRF) of the two result lists.
- **Per-agent recall** (M3): pre-filter by `agent:<slug>` (+ optional `project:<slug>`) before search; index covers `(tag_value, project_value)`.
- **File-context boost** (M8): when a `current_file_path` is provided, rules with matching `applyTo:` glob are pinned to the top.
- **Filtered output default** (M10): recall returns `clean_content` unless `include_raw=true` is explicitly requested.

## 7. Compaction detection (M7)

Auto-collect signals (weighted, configurable in `~/.mnemos/auto_collect.yaml`):
1. **Call counter** (inherited from ai-brain): N calls in T seconds → suggest checkpoint.
2. **Context-size heuristic**: client-reported token estimate > 80 % of model limit.
3. **Summary-marker detection**: regex on the most recent inbound messages for `<conversation-summary>` / `<compacted>`.
4. **Reference-drop heuristic**: agent stops citing earlier identifiers in the last N tool calls.

`mnemos_auto_collect_status` returns the per-signal vector + composite recommendation.

## 8. Path-scoped rules ingest (M8)

File watcher on `.github/instructions/*.instructions.md` in configured repos. On change:
- Parse frontmatter (`applyTo:` glob).
- Create / update a `Memory` with `status=published`, tags `mnemos:rule`, `project:<repo>`, `applyTo:<glob>`, `source:path-scoped-rule`.
- On delete → remove memory + vector entry.

This makes path-scoped rules first-class searchable knowledge instead of inert instruction files.

## 8a. Project graph (ADR-0032)

A symbol graph over **registered** project roots, persisted in the sidecar `code_graph.db` next to the main DB — never inside it, never in the vault. Tree-sitter parses sources into nodes (`Class`, `Function`, `Method`, `Type`, `Module`, `File`) and edges (`DEFINES`, `IMPORTS`, `CALLS`, `INHERITS`, `TESTS`, `USES`), indexed fully or incrementally (mtime+size staleness, per-project serialization lock). Ten MCP tools + the `/graph/` REST namespace; the watch poll (`/watch/*`) reindexes on actual changes from one cooperative thread.

- **On by default (owner decision 2026-09-28 «graphs on by default»).** Master flag `code_graph.enabled` (default `true`) gates the whole surface for operators who want it hidden; `code_graph.watch` (default `true`) is armed but inert until an explicit `watch_start` registration; `code_graph.beacon` adds one ≤200-byte freshness line to `assemble_context` output — outside the token budget, never pinned. Native auto-indexing (PG-0.5, owner directive 2026-09-29): the first MCP/hook contact with a marker-carrying cwd auto-registers the project and background-indexes it (`code_graph.auto_index`, default `true`, per-project throttle `auto_reindex_min_interval_sec`) — zero explicit calls.
- **Memory-grade hygiene.** The token contract (budget = `max_output_tokens` × 4 bytes, whole-row/whole-line drops, strictly advancing cursor) and the fail-closed index limits (`index_max_files`, `index_max_source_mb` — a breach refuses the whole index) apply everywhere.
- **Security invariants PG1–PG7** (zero bytes of source persisted, root confinement, denylist/allowlist surface + poisoned-forever set, scan-at-issue snippets with no cache, born-no-federate sidecar, untrusted-origin wrapper, limits + per-agent audit) are mechanisms with tests, not declarations. Details: [ADR-0032](docs/project/adr/0032-project-graph.md).

```mermaid
flowchart LR
    SRC["Registered project root"] -->|"tree-sitter (PG1: shapes, no source)"| IDX["Indexer\n(incremental, fail-closed limits)"]
    IDX --> ATOM["Atomic publish\ncode_graph.db (sidecar)"]
    ATOM --> TOOLS["10 MCP tools + /graph/ REST\n(default-on since 2026-09-28)"]
    ATOM --> POLL["Watch poll /watch/*\n(adaptive interval, reindex on change)"]
    ATOM --> BEACON["Recall beacon in assemble_context\n(one line, outside the budget)"]
    TOOLS --> AG["Agent (attribution + audit, PG7)"]
```

## 9. Security & operational posture

- **Rootless podman** by default. MCP server bound to localhost / unix-socket; HTTP API loopback only unless explicitly bound.
- **Secrets**: provider API keys via env vars (`MNEMOS_LLM__ANTHROPIC_API_KEY`, …) read once at startup; never written to logs.
- **URL ingest sanitisation**: strip credentials from URLs before storing.
- **Explainability**: only short `rationale_summary` (≤200 chars), never raw LLM chain-of-thought.
- **Filter safety**: Context Filter never removes source data; raw payload remains retrievable for audit/debug.
- **Quotas**: per-project soft cap on raw count; alert at 90 %.
- **Audit**: `traces` table is append-only.

## 10. Migration & deprecation

- `vesma migrate from-ai-brain` (M13): SQLite + vault import; lax tag mode for legacy data; backup first; dry-run flag.
- ai-brain (M14): README header marks it `DEPRECATED`; tag `final-v0.2.x`; main branch frozen.

## 11. Module layout (Python)

> **Note**: Uses `src/` layout (inherited from ai-brain) to keep the Python package off `sys.path` by default and prevent accidental shadowing. Tree rebuilt from the filesystem at `src/vesma/` (Vesma 5.0.0 rebrand — `src/mnemos/` is now a compatibility shim, ADR-0031 dual-import window); one-line purposes come from the module docstrings.

```
pyproject.toml
src/
  mnemos/                # compat shim → vesma (dual-import window, ADR-0031)
    __init__.py
  vesma/
    __init__.py
    config.py            # env + YAML settings; legacy env-name aliases (#139)
    models.py            # Memory, TagContract, Trace data models
    manager.py           # MemoryManager — core CRUD + search orchestrator
    mcp_server.py        # MCP server over stdio — 38 mnemos_* tools
    sdk.py               # VesmaSDK — thin typed facade over MemoryManager
    workflow.py          # workflow lifecycle state machine for memories (#96)
    traces.py            # explainability / trace layer (M6)
    auto_collect.py      # compaction detection signals (M7)
    logging_setup.py     # logging configuration
    train_entry.py       # `mnemos-train` console entry point (ADR-0021 NM track)

    api/                 # FastAPI HTTP API
      main.py            #   app + routes (+ /graph/ project-graph namespace, ADR-0032)
      auth.py            #   auth router (T-AUTH, ADR-0014)
      auth_store.py      #   tokens / sessions / challenges storage
      middleware.py      #   ASGI auth middleware
      rate_limit.py      #   slowapi rate-limiter singleton
      client_ip.py       #   trusted client-IP resolution
      federation.py      #   federation mediated-pull endpoint (Phase 2)
    cli/                 # Typer CLI
      main.py            #   entry point + core subcommands
      doctor.py          #   `vesma doctor` health checks + auto-fix
      completion.py      #   `vesma completion` shell completion installer
      integration.py     #   `vesma integration` deployment layer
      agent_wiring.py    #   agent MCP wiring helpers
      agent_token_cmd.py #   `agent-token` issue/rotate/revoke/list (W3-v1, ADR-0018-T §5)
      export.py / export_cmd.py    # export logic + Typer wrapper
      import_.py / import_cmd.py   # import logic + Typer wrapper
      sync.py / sync_cmd.py        # federation batch sync + Typer wrapper
      scanner_cmd.py     #   `vesma scanner` manual trigger + status
      logs.py            #   `vesma logs` trace viewer
      migrate.py         #   ai-brain migration logic
      _manager.py        #   shared get_manager() helper
      util.py            #   shared CLI utilities

    codegraph/           # project code graph (ADR-0032, PG-0)
      languages.py       #   language registry (wave 1: Python)
      file_surface.py    #   PG3 denylist→allowlist file surface
      indexer.py         #   tree-sitter indexer core
      incremental.py     #   mtime+size incremental indexing
      service.py         #   CodeGraphService control layer (10 tools)
      audit.py           #   PG7 audit trail
      watch.py           #   in-process watch poll (`watch_start`)
    storage/             # SQLite, vector store, Obsidian vault
      sqlite_store.py    #   SQLite FTS5 + traces + pipeline state
      vector_store.py    #   SQLite + NumPy vectors, published-only
      vault.py           #   Obsidian markdown mirror
      code_graph_store.py #  sidecar `code_graph.db` store (ADR-0032 PG-0)
    pipeline/            # knowledge pipeline (M4)
      cluster.py         #   embedding-similarity clustering
      synthesize.py      #   LLM draft synthesis (idempotent by hash)
      quality_gate.py    #   publish thresholds
      publish.py         #   publish to published + vector index
      refine.py          #   async refinement of pending rows (ADR-0019 B2a)
    policy/              # declarative rules (M5)
      scheduler.py       #   APScheduler cron / interval
      triggers.py        #   event hooks on status change
      engine.py          #   YAML rule evaluation
      dlq.py             #   Dead-Letter Queue
    recall/              # hybrid recall engine (FTS5 + vector + RRF)
    filter/              # Context Filter (M10)
      pipeline.py        #   5-stage filter: dedup → noise → extract → compress → tokens
    embeddings/          # embedding providers (bundled vesma-embed-v1 nano)
    models/
      vesma-embed-v1/    # bundled ONNX embedding model (model.onnx, tokenizer.json, manifest.json)
    llm/                 # LLM provider abstraction
      base.py            #   provider interface
    metrics/             # vitals metrics sidecar (phase A2: ledger + exposition)
      schema.py          #   born-final sidecar schema (allowlist = contract)
      sink.py            #   MetricsStore write path into metrics.sqlite
      ledger.py          #   universal verb ledger + hourly rollup
      exposer.py         #   Prometheus hybrid exposition
      boundary.py        #   config→store wiring + collection points
    sessions/            # A2A Sessions API (M16)
      api.py             #   FastAPI router
      store.py           #   SQLite-backed session store
      models.py          #   Pydantic models
      summary.py         #   extractive summary helpers
    watchers/            # file watchers
      path_scoped.py     #   .github/instructions/*.instructions.md rules (M8)
    adapters/
      hermes.py          # Hermes Agent adapter (ADR-0017 D1 contract, #125)

    assemble.py          # assemble_context provider contract (ADR-0017 D1 / ADR-0018, #125)
    context_rewrite.py   # on_context_rewrite lifecycle event (ADR-0018, #125)
    hooks.py             # server-side lifecycle hooks (#125)
    ccr.py               # P1-4 CCR reversible compression (compress-cache-retrieve)
    cache_aligner.py     # P1-5 CacheAligner — prefix stabilization for KV caches
    lens.py              # code-defined lens presets (ADR-0027 Ф0, invariant 6)
    lanes.py             # deterministic retrieval lanes (ADR-0025 E1, default-off)
    awareness.py         # Awareness v0 — presence + delta + conflict-hints (#254)
    docs_ingest.py       # docs-as-memory ingest, born-quarantine (ADR-0027 Ф3)
    decision_provider.py # decision-provider seam (ADR-0004)
    decision_jev.py      # external Jev adapter (ADR-0004 (c), default-off)
    canon_validate.py    # write-path canon validator, warn/strict (canon §9)
    graph_minting.py     # relates_to auto-minting on write (ADR-0030 A0)
    danger_detectors.py  # danger detectors — positive-signal set (ADR-0019 Phase A)
    secrets_detector.py  # secret pattern detection (federation Layer 1)
    scanner.py           # background secrets scanner (federation Layer 2)
    scanner_runtime.py   # process-wide scanner singleton
    moderation.py        # moderation pipeline (federation Layer 3)
    compact.py           # compact federation exchange format (Phase 0, #85)
    audit.py             # federation sync audit log (append-only JSONL)
    trigger_codes.py     # federation mediated-pull trigger codes
    http_guard.py        # SSRF guard for outbound HTTP legs (ADR-0009/0012)
    lazy_fetch.py        # S2 lazy fetch — operator-confirmed content fetch
    meta_poller.py       # S2 phase-2 background federation metadata poller
    federation_client.py # federation client (A-side) — mediated pull transport
    federation_server.py # federation server (B-side) — mediated pull endpoint
    federation_a2a.py    # federation A2A handler — mediated pull over A2A
    federation_access_log.py  # federation access log (B-side audit)
    mesh_client.py       # vesma-mesh gRPC client (Unix-socket transport, #105)
    mesh_server.py       # MnemosCore gRPC server on Unix socket (#105)
    agent_tokens.py      # W3-v1 agent-token scheme, Ed25519 (ADR-0018-T)
    _mesh_gen.py         # import shim for gRPC-generated stubs
```

`tests/` mirrors the modules; user-facing documentation lives under `docs/en/` and `docs/ru/`.

### M1 Git bootstrap commands (run once at repo creation)

```bash
# Step 1: clone ai-brain history into a temp directory
git clone /var/home/abyss/LABs/AI/ai-brain /tmp/vesma-bootstrap

# Step 2: copy planning docs into temp clone
cp README.md PLAN.md ARCHITECTURE.md /tmp/vesma-bootstrap/

# Step 3: copy .git from temp clone into the repo dir
cp -r /tmp/vesma-bootstrap/.git .

# Step 4: rename origin → upstream-ai-brain (read-only reference)
git remote rename origin upstream-ai-brain
git remote set-url --push upstream-ai-brain DISABLED  # prevent accidental push

# Step 5: stage all changes and commit the fork baseline
git add -A
git commit -m "chore(m1): fork from ai-brain; add Vesma planning documents"

# Step 6: (optional) set a new origin when you have a Vesma repo
# git remote add origin <your-remote-url>
```

## 12. Out of scope for v1 (explicit)

- **Cache Center** (M11) — *shipped under different names.* The original v1 deferral is resolved: reversible compression landed as **CCR** (`src/mnemos/ccr.py` — compress → cache original in `ccr_cache` by SHA-256 → retrieve via marker; tools `mnemos_compress` / `mnemos_retrieve`; `ccr` config section) and prefix stabilization as the **CacheAligner** (`src/mnemos/cache_aligner.py` — relocate dynamic spans for byte-stable prefixes; tool `mnemos_align_prefix`; `cache_aligner` config section). Both are wired into `mnemos_assemble_context` (optional CCR expansion + alignment stage). Nothing of the original Cache Center vision remains open.
- **New Web UI from scratch** — if ai-brain has one, we extend; if not, Swagger + mkdocs only.
- **Multi-tenant / multi-user auth** — Vesma is single-tenant by design.
- **Cloud-managed embeddings** — local ONNX only.
- **Cross-machine sync** — out of scope; v2 if demanded.

## 13. Open questions for the implementation session

1. Confirm local ONNX embeddings (recommendation: keep).
2. Final list of LLM providers for synthesis at launch (current set: Anthropic + OpenAI + Azure OpenAI + Ollama + Gemini).
3. mcp.json server-name aliasing policy.
4. Git strategy verification: `git clone` + remote-rename approach OK?

## 14. Component diagrams

### Context Filter pipeline

```mermaid
flowchart TD
    IN["Input Content\n(log / terminal / code / docs / web)"]

    subgraph SEL["Profile Selection"]
        P1["① explicit request"]
        P2["② source: tag hint"]
        P3["③ content heuristics"]
        P4["④ default profile"]
        P1 -.- P2 -.- P3 -.- P4
    end

    subgraph PIPE["5-Stage Filter Pipeline — filter/"]
        D["① dedup.py\nexact + near-dup suppression"]
        N["② noise.py\nANSI · progress bars · separators"]
        E["③ extract.py\nerrors · warnings · exit codes"]
        C["④ compress.py\nsemantic block compression"]
        T["⑤ tokens.py\npreflight budget + reduction stats"]
        D --> N --> E --> C --> T
    end

    RAW[("raw_content\n← immutable audit copy")]
    CLEAN[("clean_content\n← default for recall / models")]
    STATS["filter_stats\n{ profile, tokens_before, tokens_after }"];

    IN --> SEL
    SEL --> PIPE
    IN -.->|"always preserved"| RAW
    T --> CLEAN
    T --> STATS
```

### Storage layer

```mermaid
flowchart TD
    MGR["MemoryManager"]

    subgraph SQL["SQLite — storage/sqlite_store.py"]
        MEM[("memories\n(main table)")]
        FTS[("memories_fts\nFTS5 virtual table")]
        TR[("traces\nappend-only audit")]
        PRJ[("projects")]
        MEM <-.->|"triggers AI / AD / AU"| FTS
    end

    subgraph VST["Vector Store — storage/vector_store.py"]
        EMB[("embeddings table\nvectors.db — numpy float32")]
    end

    subgraph VLT_B["Obsidian Vault — storage/vault.py"]
        MD[("*.md files\nvault/{type}/{title}.md\nYAML frontmatter")]
    end

    MGR -->|"save / get / delete / update"| MEM
    MGR -->|"save_trace"| TR
    MGR -->|"save_project"| PRJ
    MGR -->|"upsert / search (published only)"| EMB
    MGR -->|"memory_to_file / scan / delete_file"| MD
```

### Hybrid recall engine

```mermaid
flowchart TD
    Q["Search Query\n{ query, tags, project, agent, limit }"]
    EMBED["Embeddings\nembeddings/__init__.py\nquery → 384-dim vector"]

    subgraph LEGS["Dual-Leg Retrieval"]
        FTS_L["FTS5 Leg — recall/fts.py\nSQLite FTS5 MATCH + filters"]
        VEC_L["Vector Leg — recall/vector.py\ncosine similarity on published"]
    end

    AGENT{"agent_recall?\n(M3)"}
    AFILT["Pre-filter\nagent: + project:"]
    RRF["RRF Fusion — recall/rrf.py\nrrf_k = 60  ·  alpha blend"]
    OUT["SearchResult[]\n{ memory, score, search_type }"]

    Q --> AGENT
    AGENT -->|"yes"| AFILT
    AFILT --> LEGS
    AGENT -->|"no"| LEGS

    Q --> EMBED
    EMBED --> VEC_L
    Q --> FTS_L
    FTS_L & VEC_L --> RRF --> OUT
```

### MCP tools → MemoryManager

> Diagram shows the v1 core subset; the current 26-tool surface is grouped in §3 and detailed in [docs/en/user/mcp-tools.md](docs/en/user/mcp-tools.md).

```mermaid
flowchart LR
    subgraph TOOLS["MCP Tools — mcp_server.py"]
        T1["mnemos_add"]
        T2["mnemos_search"]
        T3["mnemos_recall_context"]
        T4["mnemos_agent_recall"]
        T5["mnemos_save_context"]
        T6["mnemos_list_recent"]
        T7["mnemos_list_tags"]
        T8["mnemos_ingest_url"]
        T9["mnemos_watch_*"]
        T10["mnemos_auto_collect_status"]
        T11["mnemos_stats"]
    end

    subgraph MGR_B["MemoryManager — manager.py"]
        M1["add()"]
        M2["search()"]
        M3["recall_context()"]
        M4["agent_recall()"]
        M5["list_recent()"]
        M6["list_tags()"]
        M7["ingest_url()"]
        M8["watch_start/stop/status()"]
        M9["stats()"]
        AC_T["_checkpoint_tracker\nauto_collect.py"]
    end

    T1 -->|"TagContract validation"| M1
    T2 --> M2
    T3 --> M3
    T4 --> M4
    T5 -->|"mnemos:checkpoint → add()"| M1
    T6 --> M5
    T7 --> M6
    T8 --> M7
    T9 --> M8
    T10 --> AC_T
    T11 --> M9
```

### Policy engine

```mermaid
flowchart TD
    YAML["policies.yaml\n~/.mnemos/policies.yaml"]

    subgraph PE_B["Policy Engine — policy/"]
        SCH["scheduler.py\nAPScheduler cron / interval"]
        TRIG["triggers.py\nevent hooks on status change"]
        ENG["engine.py\nYAML rule evaluation"]
        DLQ_P["dlq.py\nDead-Letter Queue"]
    end

    subgraph ACT["Actions"]
        A1["trigger cluster"]
        A2["trigger synthesize"]
        A3["trigger publish"]
        A4["archive"]
        A5["alert — quota 90%"]
    end

    YAML --> ENG
    SCH -->|"fire"| ENG
    TRIG -->|"fire"| ENG

    ENG -->|"auto-publish rule"| A3
    ENG -->|"cluster threshold"| A1
    ENG -->|"low quality"| DLQ_P
    ENG -->|"age / size rule"| A4
    ENG -->|"quota rule"| A5

    DLQ_P -->|"retry (exp. backoff + jitter)"| A2
    DLQ_P -->|"max retries → discard"| A4
```

### Compaction detection signals (M7)

```mermaid
flowchart TD
    subgraph SIG["Auto-Collect Signals — auto_collect.py"]
        S1["① call_counter\ncalls_since_save >= N"]
        S2["② context_size\ntokens / limit >= 0.80"]
        S3["③ summary_marker\nregex on inbound messages"]
        S4["④ reference_drop\nagent stops citing earlier IDs"]
    end

    W["Configurable weights\nauto_collect.yaml"]
    COMP["composite_score\n= sum of weight_i x signal_i"]
    REC{"score >= 0.4\nor summary_marker?"}

    OK["recommendation: ok"]
    SAVE["recommendation:\nsave_checkpoint"]
    REM["warning reminder\nappended to next MCP response"]

    S1 & S2 & S3 & S4 --> COMP
    W --> COMP
    COMP --> REC
    REC -->|"yes"| SAVE --> REM
    REC -->|"no"| OK
```

## 15. References

- ai-brain repo: `/var/home/abyss/LABs/AI/ai-brain/`
- ai-brain knowledge-pipeline concept: `ai-brain/docs/knowledge-pipeline-concept.md` (v0.4 roadmap)
- Hermes Agent plugin: `integrations/hermes/` in this repo
- Vesma tag contract skill: `integrations/skills/vesma-tag-contract.md` in this repo
