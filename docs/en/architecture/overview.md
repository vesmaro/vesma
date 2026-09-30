# Vesma — System Architecture

**🌐 Language / Язык:** English · [Русский](../../ru/architecture/overview.md)

## Overview

Vesma is a hybrid long-term memory system: a personal knowledge base and
RAG store for AI agents. Primary access surfaces: CLI, HTTP API, MCP server,
and an Obsidian-compatible vault. A Web UI is planned as a separate project
(mnemos-eyes).

## Core Principles

- **Markdown-first**: human-readable notes in Obsidian format (YAML frontmatter + markdown)
- **Semantic search**: vector embeddings over text data
- **Hybrid search**: full-text search + vector similarity, relevance-ranked results
- **Modularity**: core decoupled from interfaces; each interface is a thin adapter
- **Local-first**: everything works locally, no mandatory cloud dependencies
- **Extensibility**: plugin system for data sources and interfaces

---

## Architecture (Layers)

```
┌─────────────────────────────────────────────────────────┐
│                    INTERFACES                            │
│  ┌─────┐  ┌──────────────────────┐  ┌──────┐  ┌─────┐  │
│  │ CLI │  │  Web UI              │  │ API  │  │ MCP │  │
│  │Typer│  │  (planned,           │  │ REST │  │ Srv │  │
│  │     │  │   mnemos-eyes)       │  │      │  │     │  │
│  └──┬──┘  └──────────┬───────────┘  └──┬───┘  └──┬──┘  │
│     │               │               │          │       │
├─────┴───────────────┴───────────────┴──────────┴───────┤
│                    FastAPI (Core API)                    │
│            GET/POST /memories, /search, /ingest         │
├─────────────────────────────────────────────────────────┤
│            CORE (MemoryManager, manager.py)             │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐ │
│  │MemoryManager │ │ SearchEngine │ │IngestionPipeline │ │
│  │  CRUD ops    │ │ hybrid search│ │  parse & embed   │ │
│  └──────┬───────┘ └──────┬───────┘ └────────┬─────────┘ │
│         │                │                   │           │
├─────────┴────────────────┴───────────────────┴──────────┤
│                    STORAGE                               │
│  ┌────────────────┐  ┌───────────────┐  ┌─────────────┐ │
│  │ Obsidian Vault │  │  Vector index │  │  SQLite     │ │
│  │ (markdown)     │  │  (vectors.db) │  │  (metadata) │ │
│  └────────────────┘  └───────────────┘  └─────────────┘ │
├─────────────────────────────────────────────────────────┤
│                    EMBEDDING                             │
│ mnema-embed-v1 (bundled) / onnx / Ollama / sentence-tr. │
└─────────────────────────────────────────────────────────┘
```

---

## Components

### 1. Storage Layer

| Component | Purpose | Technology |
| --- | --- | --- |
| Obsidian Vault | Human-readable notes, markdown + frontmatter | Filesystem |
| Vector index | Vector embeddings for semantic search | SQLite (`vectors.db`, local) |
| SQLite | Metadata, tags, relationships, history, cache | SQLite + aiosqlite |

**Obsidian compatibility**:
- Each "memory" is a markdown file with YAML frontmatter (tags, source, created, etc.)
- Supports `[[wiki-links]]` and `#tag` notation
- Vault directory is configurable
- File watcher monitors changes and re-indexes

### 2. Embedding Layer

- **Default**: `mnema-embed-v1` — bundled local model (~30 MB, int8 ONNX, RU+EN, 384d), works offline
- Current weights (round 3, 2026-09-09): distilled from **Qwen/Qwen3-Embedding-0.6B** (Apache-2.0), `weights_sha256 3b752e06…`, MRL dimensions 64/128/256/384, opset 15; training corpus ~100k text→teacher-vector pairs including 8086 real store entries (RU 41.9%)
- **Embedder swaps are vintage-tracked**: every vector stores the fingerprint of the embedder that produced it (`weights_sha256` for the bundled model); vectors cut by a different fingerprint are re-embedded automatically by the background heal sweeper, and `vesma doctor` reports the remaining vintage-mismatch count
- **External providers still available**: `onnx` (any HF model), Ollama, `sentence-transformers`
- Embedding provider configured via config file
- Embedding caching to avoid repeated computation

### 3. Core (`MemoryManager`, `manager.py`)

#### MemoryManager
- CRUD for memory entries (create, read, update, delete)
- Automatic embedding generation on create / update
- Sync: markdown file ↔ vector index ↔ SQLite
- Tags, categories, priorities, TTL (entry lifetime)

#### SearchEngine
- **Semantic search**: vector similarity via the local vector index
- **Full-text search**: FTS5 via SQLite
- **Hybrid search**: RRF (Reciprocal Rank Fusion) to combine results
- Filtering by tags, dates, sources, types

#### IngestionPipeline
- Parsing incoming data from different sources
- Chunking long documents (RecursiveCharacterTextSplitter)
- Deduplication (by content hash + cosine similarity)
- Automatic tag and metadata extraction

### Reversible Compression (CCR)

CCR (Compress-Cache-Retrieve) reduces the token cost of large content (tool output, logs, JSON) without losing data. It reuses the existing 5-stage context filter for compression and the existing SQLite store for caching — no separate database, no separate backup.

#### Pipeline

```mermaid
flowchart LR
    A[Raw text] --> B[Compress\n5-stage filter]
    B --> C[Cache original\nSHA-256 keyed]
    C --> D[Embed marker\nin compressed output]
    D --> E[Compressed text\n+ marker]
    F[mnemos_retrieve\nhash] --> G{query?}
    G -->|no| H[Full original]
    G -->|yes| I[FTS5 snippets\nranked]
    C -.-> H
    C -.-> I
```

1. **Compress** — `apply_filter` runs the 5-stage pipeline (profile-aware: `log`, `terminal`, `code`, `docs`, `web`, `default`). Achieves 86–96% reduction on logs and JSON.
2. **Cache** — the original uncompressed text is stored in `ccr_cache` keyed by its SHA-256 hash. Content-addressed: re-compressing the same text is a no-op.
3. **Embed marker** — a short parseable marker is prepended to the compressed output:
   ```text
   [compressed: <hash> | <N>→<M> chars | retrieve via mnemos_retrieve]
   ```
4. **Retrieve** — `mnemos_retrieve(hash)` returns the full original (zero data loss). `mnemos_retrieve(hash, query=...)` returns FTS5-ranked snippets within the cached original.

#### Storage integration

The `ccr_cache` table lives in the same SQLite database as `memories`:

| Table | Purpose | Keyed by |
|-------|---------|----------|
| `ccr_cache` | Original uncompressed content | `hash` (SHA-256, PRIMARY KEY) |
| `ccr_cache_fts` | FTS5 external-content index over `ccr_cache.original` | `rowid` |

FTS5 is kept in sync via `AFTER INSERT/DELETE/UPDATE` triggers — no application-level sync code. Snippet retrieval uses the same FTS5 engine as memory search, scoped to a single cached original by `hash`.

#### Eviction

| Mechanism | Default | When it runs |
|-----------|---------|--------------|
| TTL expiry | 7 days | `ccr_cleanup()` — runs automatically from the background processor on its own interval (T3), or via the CLI |
| LRU eviction | 10000 entries | Opportunistic, on every `compress` call |

Since T3 (v2.9.0), `ccr_cleanup()` is invoked from `_processor_loop` on its own interval (`ccr_cleanup_interval_sec`, default 1200s = 20 min) — not on every processor cycle, to avoid scanning the cache table every `interval_sec`. It is guarded by `ccr.enabled` and wrapped in a try/except so a cleanup failure never crashes the processor loop.

#### Configuration

```yaml
ccr:
  enabled: true                    # master switch
  ttl_days: 7                      # cache entry lifetime
  max_entries: 10000               # LRU eviction threshold
  min_size_chars: 500              # below this, content is returned as-is
  snippet_count: 5                 # snippets returned by retrieve(query=...)
  filter_budget: 4096              # token budget passed to apply_filter
  ccr_cleanup_interval_sec: 1200   # T3: background cleanup interval (60–86400s)
```

Content below `min_size_chars` is returned as-is with `cached=false` and `reduction_pct=0` — tiny content has no token savings.

### CacheAligner (P1-5)

CacheAligner stabilizes the prefix of system-prompt-like text so provider KV caches (Anthropic `cache_control`, OpenAI prefix caching) hit across requests. It extracts dynamic content — ISO timestamps, UUIDs, session ids, short-lived tokens, calendar dates — and relocates each span to a `--- Dynamic context ---` block at the end. The prefix up to the first dynamic span becomes byte-stable; the dynamic values still reach the model, just from the tail.

Inspired by headroom's CacheAligner (https://github.com/headroomlabs-ai/headroom, Apache 2.0). Original implementation — no headroom code is imported.

#### Extraction pipeline

```mermaid
flowchart LR
    A[System-prompt text] --> B[Extract dynamic spans
ordered by specificity]
    B --> C[Remove spans from body
collapse spaces]
    C --> D[Append '--- Dynamic context ---'
block with kind: value lines]
    D --> E[Aligned text
byte-stable prefix]
```

Pattern kinds, in order of specificity (most specific first so an ISO timestamp is matched as a timestamp, not a bare date):

| Kind | Pattern | Example |
|------|---------|---------|
| `timestamp` | ISO 8601 with optional tz | `2026-07-17T10:30:00Z` |
| `uuid` | canonical 8-4-4-4-12 hex | `550e8400-e29b-41d4-a716-446655440000` |
| `session_id` | `sess-*`, `session:*`, `sid-*` | `sess-abc123def456` |
| `date` | calendar dates | `2026-07-17`, `2026/07/17` |
| `token` | bare 20+ char opaque tokens | `a1b2c3d4e5f6789012345` |

Overlapping matches are resolved by earliest start, then longest match — a timestamp wins over a bare date overlapping its tail.

#### Profile behaviour

| Profile | Skips | Why |
|---------|-------|-----|
| `default` (or omitted) | nothing | extract all kinds |
| `code` | `token` | bare 20+ char tokens would mangle long identifiers / hashes in code |
| `docs` | `token` | prose rarely contains real tokens; avoids mangling long hyphenated words |

The profile's skip set merges (union) with per-kind toggles from `CacheAlignerConfig` — disabling a kind in config widens what a profile already skips.

#### Configuration

```yaml
cache_aligner:
  enabled: true               # master switch
  extract_timestamps: true   # ISO 8601 timestamps
  extract_uuids: true        # canonical 8-4-4-4-12 UUIDs
  extract_session_ids: true  # sess-*, session:*, sid-*
  extract_dates: true        # calendar dates 2026-07-17 / 2026/07/17
  extract_tokens: true       # bare 20+ char opaque tokens
```

When `cache_aligner.enabled` is `false`, `align_prefix()` returns the text unchanged with an empty `extracted` list. The MCP tool `mnemos_align_prefix` is the public surface; see [mcp-tools.md#mnemos_align_prefix](../user/mcp-tools.md#mnemos_align_prefix).

### Output token reduction (P1-7)

Output token reduction steers the caller's output style without changing what Vesma stores or returns. Three tools — `mnemos_add`, `mnemos_search`, `mnemos_recall_context` — accept two optional parameters:

| Parameter | Values | Effect |
|-----------|--------|--------|
| `verbosity` | `default`, `terse`, `minimal` | Injects an output-style guidance suffix into the tool result framing |
| `effort` | `low`, `medium`, `high` | Injects a reasoning-effort hint into the tool result framing |

These are **hints passed through to the caller**, not model config changes. Inspired by headroom's output token reduction work. Original implementation.

#### Backward compatibility

- Defaults (`default` / `medium`) produce an empty guidance suffix — the tool result is byte-identical to the pre-P1-7 output.
- Invalid values are validated against frozensets, logged at `WARNING`, and fall back to the config default — graceful degradation, never raises.
- When `output_style.enabled` is `false`, both resolvers return the no-op defaults regardless of caller input.

#### Configuration

```yaml
output_style:
  enabled: true              # master switch; when false, steering is a no-op
  default_verbosity: default # default when caller omits verbosity
  default_effort: medium     # default when caller omits effort
```

See [mcp-tools.md#output-token-reduction-p1-7](../user/mcp-tools.md#output-token-reduction-p1-7) for the user-facing reference.

---

### 4. Data Sources (Ingestors)

| Source | Method | Format |
| --- | --- | --- |
| Manual input | CLI / API | Text / Markdown |
| Obsidian vault | File watcher (watchdog) | Markdown + frontmatter |
| Web pages | URL → trafilatura/BeautifulSoup | HTML → clean text |
| Files | Upload via API | PDF, TXT, MD, DOCX |
| LLM chats | MCP / export | Dialogues |

### 5. Interfaces

#### CLI (Typer)
```bash
vesma add "Note about something important" --tags project:vesma agent:user mnemos:learning   # quick add
vesma add --file ./document.pdf --tags project:vesma agent:user mnemos:learning              # from a file
vesma add --url https://example.com --tags project:research agent:user mnemos:learning        # ingest a URL
vesma search "how to configure nginx"             # hybrid search (FTS5 + vector + RRF)
vesma search "CVE" --project vesma --limit 20    # project-scoped search
vesma recall --agent tech-writer --limit 20       # recent entries for an agent (M3)
vesma stats                                       # store statistics
vesma serve                                       # start the HTTP API
vesma mcp-server                                  # start the MCP server (stdio)
```

#### REST API (FastAPI)
```
POST   /memories                 — create entry
GET    /memories                 — list (with pagination)
GET    /memories/{id}            — get entry
POST   /search                   — hybrid search
POST   /ingest-url               — ingest / parse a URL
GET    /tags                     — list tags
POST   /reindex                  — re-index
GET    /health                   — healthcheck
```

#### MCP Server
Tools for Copilot / LLM agents (full catalogue in [mcp-tools.md](../user/mcp-tools.md)):
- `mnemos_search` — hybrid semantic + full-text search over memory
- `mnemos_add` — add a new entry
- `mnemos_recall_context` — recall the session context block
- `mnemos_list_tags` — list tags
- `mnemos_ingest_url` — load a web page

#### Web UI (planned — mnemos-eyes)

A separate frontend project. Status: in development. Planned features:
- Dashboard: statistics, recent entries, tag cloud
- Search with filters
- Note editor

---

## Data Model

### Memory (entry)

```python
class Memory:
    id: str              # UUID
    content: str         # main text
    title: str | None    # title (auto or manual)
    tags: list[str]      # tags
    source: str          # source: manual, web, file, mcp, obsidian
    source_url: str | None
    memory_type: str     # note, fact, snippet, bookmark, conversation
    created_at: datetime
    updated_at: datetime
    embedding: list[float] | None
    metadata: dict       # additional data
    file_path: str | None  # path to markdown file in vault
```

### Markdown file (Obsidian)

```markdown
---
id: 550e8400-e29b-41d4-a716-446655440000
title: Configuring nginx reverse proxy
tags: [nginx, devops, linux]
source: web
source_url: https://example.com/nginx-guide
memory_type: note
created: 2026-04-10T12:00:00
updated: 2026-04-10T12:00:00
---

# Configuring nginx reverse proxy

Main note content...
```

---

## Configuration

```yaml
# config.yaml
mnemos:
  vault_path: ~/.mnemos/vault         # Obsidian vault
  data_dir: ~/.mnemos/data            # vector index + SQLite

embedding:
  provider: nano                      # nano (mnema-embed-v1, bundled) | onnx | ollama | sentence-transformers
  model: mnema-embed-v1
  # ollama_url: http://localhost:11434

search:
  default_limit: 20
  hybrid_alpha: 0.5                  # semantic search weight (0=FTS, 1=vector); 0.5 balances the RRF legs — at 0.7 vector dominance drowned FTS-rank-1 matches (issue #300)

api:
  host: 0.0.0.0
  port: 8787

mcp:
  transport: stdio                   # stdio is the only implemented transport
```

---

## Roadmap

> Snapshot of the original plan. Phases 1–2 (except PDF/DOCX parsing) have
> shipped; Vesma is at 4.0.0. The authoritative current plan lives in
> [PLAN.md](../../../PLAN.md).

### Phase 1 — MVP (shipped)
- [x] Architecture and data models
- [x] Core: MemoryManager + SQLite + vector store (`vectors.db`)
- [x] Embedding layer (bundled `mnema-embed-v1`)
- [x] Hybrid search
- [x] CLI (add, search, recall, tags)
- [x] Obsidian vault sync (read/write)
- [x] REST API (FastAPI)

### Phase 2 — Integrations
- [x] MCP server for Copilot
- [x] Web scraping (ingest URLs)
- [ ] PDF/DOCX parsing

### Phase 3 — Advanced features
- [ ] Web UI (mnemos-eyes)
- [ ] Auto-categorisation (LLM-powered)
- [ ] Relationship graph between entries
- [ ] Auto-summarisation of long documents
- [x] Periodic consolidation (merge similar entries)
- [x] Export / Import

### Phase 4 — Scaling
- [ ] Migration to PostgreSQL + pgvector (optional)
- [ ] Multi-user support
- [ ] Storage encryption
