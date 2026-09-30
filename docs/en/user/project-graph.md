# The Project Graph

**🌐 Language / Язык:** English · [Русский](../../ru/user/project-graph.md)

> Vesma can know your codebase the way it knows your sessions: as a searchable
> symbol map. Index a project once, and agents get file outlines, symbol
> search, call tracing and line-range snippets — with secrets filtered out and
> zero source bytes stored.

---

## What it is

The **project graph** is a memory-grade symbol map of a codebase, built by
parsing files with tree-sitter (ADR-0032). It lives in a sidecar SQLite file,
`code_graph.db`, next to the main store in the data dir (`~/.mnemos/data/` by
default).

What is stored — and what is not:

| Stored | Never stored |
|--------|--------------|
| File paths (repo-relative), languages, line ranges | File contents, source bytes (PG1) |
| Names and qualified names of symbols | Docstrings, comments, string literals |
| Signature **shapes** (no default values) | Code bodies |
| Edges between symbols, content hashes | Secrets — those poison the file (see below) |

Because the graph holds no source bytes, it is *derived data*: delete it and
it can always be rebuilt from the sources. No backup policy is needed for the
sidecar — `code_graph.db` is disposable by design.

Nodes and edges:

| Kind | Examples |
|------|----------|
| **Nodes** | `Project`, `File`, `Module`, `Class`, `Function`, `Method`, `Type` |
| **Edges** | `CONTAINS_FILE`, `DEFINES`, `IMPORTS`, `CALLS`, `INHERITS`, `TESTS`, `USES` |

The `USES` edge is the honesty marker of the family: a call is recorded as
`CALLS` only when the parser can prove the target; everything else that looks
like a call is `USES` with `provenance: heuristic`. A retrieved edge never
claims more certainty than it has.

---

## Turn it on for a project

The graph indexes **registered roots only** (PG2). A root is the first
absolute path on a project record (`paths[0]`). Registration is an operator
step — projects auto-created by memory writes carry no paths, so a graph call
for them answers a confinement refusal until you register a root:

```python
from vesmaro.models import Project
mgr.sqlite.save_project(Project(name="vesma", paths=["/home/you/vesma"]))
```

Then the flow is three tool calls (every one needs an `agent` — see
[Boundaries & FAQ](#boundaries--faq)):

1. **Index** — `mnemos_index_project` with `project_id` and `agent`. The first
   run is a full index; later runs are incremental by default (skip when
   nothing changed, per mtime+size classification).
2. **Check** — `mnemos_project_graph_status`: node/edge/file volumes, fresh %,
   parse failures and the poisoned count.
3. **Use** — `mnemos_search_graph` for ranked symbol search,
   `mnemos_get_file_outline` for a file's shape, `mnemos_trace_path` for
   call/navigation walks, `mnemos_get_code_snippet` for line ranges.

> **Nothing indexes itself in the current release.** The first index is always
> an explicit call. There is no background auto-registration and no
> first-touch indexing — a registered watch keeps an indexed project fresh,
> but never seeds the first index.

---

## Keep it fresh: the watch poll

`mnemos_watch_start` registers an indexed project with an in-process poll
thread (no daemon). On an adaptive interval — 5 s base, +1 s per 500 indexed
files, capped at 60 s — it classifies files by mtime+size and reindexes only
on actual changes, audited with reason `watch`.

- Registrations live for the process lifetime; a restart drops them.
- Global cap: 8 active registrations (`code_graph.watch_max_registrations`).
- `mnemos_watch_status` shows every registration and its last poll outcome;
  `mnemos_watch_stop` stops one or all (idempotent).

---

## The ten tools

| Tool | What it does |
|------|--------------|
| `mnemos_index_project` | Full or incremental index of a registered root; serialized per project |
| `mnemos_project_graph_status` | Volumes, freshness, parse errors, poisoned count for one project |
| `mnemos_search_graph` | Ranked search by name / qualified name / path; opt-in signatures |
| `mnemos_trace_path` | BFS over edges from one symbol (depth ≤ 2, honest `truncated` flag) |
| `mnemos_get_file_outline` | Symbol outline of one indexed file — shapes, never bodies |
| `mnemos_get_code_snippet` | Line range read **from disk**, freshness-checked and secret-scanned |
| `mnemos_check_graph_coverage` | Per-path verdict: `indexed` / `stale` / `parse-error` / `unindexed` / `poisoned` |
| `mnemos_get_graph_schema` | The contract card: kinds, limits, token contract |
| `mnemos_list_graph_projects` | Registered projects joined with index status |
| `mnemos_delete_graph_project` | Drop the index (sidecar only); the only way to clear the poisoned set |

Plus the watch family: `mnemos_watch_start` / `mnemos_watch_stop` /
`mnemos_watch_status`.

Every windowed tool takes `max_output_tokens` (128–1,000,000, default 3200)
under a deterministic token contract: rows drop whole, never split, and the
cursor strictly advances. Full input schemas and examples:
[mcp-tools.md, "Project graph tools"](mcp-tools.md#project-graph-tools-adr-0032).

---

## REST mirrors

The same surface exists over HTTP ([http-api.md](http-api.md#project-graph-adr-0032)):

- `/graph/index`, `/graph/status/{project_id}`, `/graph/search`,
  `/graph/trace`, `/graph/outline`, `/graph/snippet`, `/graph/coverage`,
  `/graph/schema`, `/graph/projects`, `DELETE /graph/projects/{project_id}`
- `/watch/start`, `/watch/stop`, `GET /watch/status`

---

## Security & guardrails

The security model is ADR-0032 §6 (invariants PG1–PG7); here is what it means
for you:

| Guardrail | What you experience |
|-----------|---------------------|
| **Confinement (PG2)** | Only operator-registered roots are ever touched. Arbitrary paths, symlink escapes and `..` traversal are refused. |
| **Poisoned files (PG3)** | A file that trips the secrets detector at index time is poisoned **forever**: its symbols stay in the graph, its snippets are never issued. Only `mnemos_delete_graph_project` clears the set. |
| **Scan at issue (PG4)** | Snippets are read from disk at request time — there is no snippet cache. A mtime+size+sha256 mismatch yields a `stale` marker, never content; a secret found in the requested range refuses the whole range, fail-closed. |
| **Fail-closed limits (PG7)** | 20,000 files / 500 MB per project. A breach refuses the WHOLE index — no partial graph is ever published. |
| **Export-blind (PG5)** | The code map never leaves the server: export bundles and federation/mesh payloads never carry graph artifacts. Peers index their own local sources. |
| **Attribution & audit (PG7)** | Every call — read or write — is audited per agent; calls without an `agent` are refused with `attribution-required`. |
| **Zero source bytes (PG1)** | Dump the sidecar and you find names, paths, line ranges — no code, no literals, no secrets at rest. |

---

## Configuration

All flags live under `code_graph:` in `config.yaml` (see `config.example.yaml`).
The surface is **on by default** (owner decision 2026-09-28).

| Key (`code_graph.`) | Default | Meaning |
|---------------------|---------|---------|
| `enabled` | `true` | Master flag for the 10 tools + the `/graph/` REST namespace; `false` hides the whole surface (every call answers `code: "disabled"`). |
| `beacon` | `true` | One tail line in `assemble_context` output advertising graph freshness («indexed …, N/M files fresh — call mnemos_search_graph»). Only when `enabled`. |
| `watch` | `true` | Arms the watch poll (`mnemos_watch_start`); inert until an explicit registration. |
| `index_max_files` | `20000` | Hard cap on indexed files per project (fail-closed). |
| `index_max_source_mb` | `500` | Hard cap on total source bytes per project, MiB (fail-closed). |
| `watch_max_registrations` | `8` | Global cap on active watch registrations per process. |
| `watch_base_interval_sec` / `watch_interval_per_500_files` / `watch_max_interval_sec` | `5.0` / `1.0` / `60.0` | Adaptive poll interval: base + 1 s per 500 indexed files, capped. |

Environment overrides follow the canonical settings pattern:
`VESMARO_CODE_GRAPH__INDEX_MAX_FILES`,
`VESMARO_CODE_GRAPH__INDEX_MAX_SOURCE_MB`.

---

## Boundaries & FAQ

- **Why does every call demand `agent`?** PG7 attribution: audit trails and
  per-agent accountability. `mnemos_recall_context` carries no `agent`, so the
  graph surface never rides on it — pass the caller's identity explicitly.
- **My project is a bare `.git` checkout / has no packaging manifest.** Fine —
  registration is by project record, not by manifest detection. Give the
  project an absolute `paths[0]` and index it.
- **Multiple paths on one project?** The graph indexes the first registered
  path (`paths[0]`) — one root, one graph per project.
- **A file changed after indexing.** Snippets come back with a `stale` marker
  instead of content; reindex (or let the watch poll do it) to refresh.
- **How do I turn it all off?** `code_graph.enabled: false` — tools answer
  `disabled`, the beacon hides, nothing indexes.
- **Do I need to back up `code_graph.db`?** No. It is a rebuildable sidecar
  in the data dir; `mnemos_delete_graph_project` drops only the index, never
  the project entity or its memories.

---

## Related

- Full tool reference: [mcp-tools.md, "Project graph tools"](mcp-tools.md#project-graph-tools-adr-0032)
- REST endpoints: [http-api.md](http-api.md#project-graph-adr-0032)
- Design and invariants: [ADR-0032](../../project/adr/0032-project-graph.md)
- Architecture placement: [ARCHITECTURE.md §8a](../../../ARCHITECTURE.md)

---

_Sources: ADR-0032 (project graph as memory); `docs/en/user/mcp-tools.md`
(Project graph tools), `src/vesmaro/config.py` (`CodeGraphConfig`),
`src/vesmaro/codegraph/`; landed in PG-0 wave (#438), graphs-on-by-default
(#440). Feature map: [features.md](../features.md)._

_Last updated: 2026-09-30_
