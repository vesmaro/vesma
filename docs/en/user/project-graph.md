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
for them answers a confinement refusal (the refusal text names the fix) until
a root is registered. Two ways to register:

- **Agent-side** (#454): the `mnemos_register_project` tool — `project_id`,
  the absolute `root`, and the mandatory `agent` attribution. The root must
  exist, carry a packaging manifest or a `.git`, and not be `$HOME`/the
  filesystem root. Idempotent when the root is already registered.
- **CLI**: `vesma graph register <project> <root>` — the same gates and
  audit trail (`manual-register`), from the terminal.

The raw store write also works (what the tools do underneath):

```python
from vesmaro.models import Project
mgr.sqlite.save_project(Project(name="vesma", paths=["/home/you/vesma"]))
```

Explicit registration does **not** count against `auto_register_max_projects`
— that cap bounds the auto path only.

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

Prefer zero-touch? The next section describes the native auto path — the
manual flow above keeps its full contract either way.

---

## Native auto-indexing (zero-touch)

Since PG-0.5 the graph indexes **by itself**: every MCP tool call and every
`pre_llm_call` hook fires a background *hint*, and a hint can auto-register
and index the project — no `mnemos_index_project` call, no instruction, no
skill. Auto work goes through the same serialization, PG7 limits and audit
trail as manual runs; only the audit `reason` (`auto-first` / `auto-stale`)
and the actor (the hinting agent) differ.

How the first contact plays out:

1. An agent — with an `agent` id; **no agent, no auto action** (PG7) — calls
   any MCP tool inside the project's directory. The dispatcher emits one
   hint: project slug + current working directory. The hint never blocks and
   never breaks the caller; the work runs on a background scheduler thread.
2. If the project is **not yet registered**, the auto path checks the cwd
   for a packaging **manifest**: `pyproject.toml`, `setup.py`, `package.json`,
   `go.mod` or `Cargo.toml` (lockfiles do not count). No manifest, no
   auto-registration: a bare `.git` checkout is deliberately not a marker,
   and `$HOME`/the filesystem root are refused even when a manifest sits
   there.
3. Manifest present → the project is auto-registered on its resolved root
   and the first index runs (`auto-first`). From then on the beacon line
   appears in `assemble_context` output on its own.
4. Already registered and stale → the hint becomes an incremental reindex
   (`auto-stale`), throttled per project (see below).

Guardrails specific to the auto path:

| Guardrail | What you experience |
|-----------|---------------------|
| **One root = one graph** | Before registering anything, the auto path looks for an existing project holding the same resolved root and reuses it (audit `auto-register-reused`) — never a duplicate project row, never a second full index. |
| **Registration cap** | `auto_register_max_projects` (64) bounds how many projects the auto path may ever create; past it, hints are silently skipped with an `auto-register-capped` audit row — never an error to the caller. |
| **Throttle** | `auto_reindex_min_interval_sec` (300 s) gates consecutive auto actions per project. The stamp is written *before* the action (reserve-then-act): a failing auto index is not retried on every subsequent hint. |
| **Suspension** | A failed *first* auto index suspends the auto path for that project: further hints skip it entirely — until a successful manual `mnemos_index_project` (the watch poll rides the same method) or a `mnemos_delete_graph_project` lifts the flag. A failed *stale* reindex never suspends anything. |

v1 boundaries: a call without an `agent` never triggers the auto path; REST
is deliberately not a hint surface; a multi-path registration still indexes
`paths[0]` only. Turn the auto path off alone with
`code_graph.auto_index: false` — the manual tools keep working.

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

## Moved roots: ghosts and `vesma graph repoint` (#450)

A project directory renamed or moved on disk leaves a **ghost**
registration: the auto path no-ops silently, `mnemos_index_project`
refuses confinement ("registered root is missing on disk"), and — before
#450 — the only exit was deleting the graph. Ghosts are visible:
`mnemos_list_graph_projects` marks them `root_missing: true`.

Repair is one operator command:

```bash
vesma graph repoint <project> <new-root>
```

Gates (loud refusals, audited as action `repoint`, reason `graph-repoint`):

- the new root must exist, be absolute, carry a packaging manifest or a
  `.git` (no pointing the graph at an unrelated directory), and not be
  `$HOME`/the filesystem root;
- one root = one graph: a new root already claimed by another project is
  refused;
- the stale index is **purged** (it describes the old tree — derived,
  rebuildable data), the auto-path suspension is lifted, and the next
  index run rebuilds fresh.

---

## The graph tools

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
| `mnemos_list_graph_projects` | Registered projects joined with index status; ghosts marked `root_missing` |
| `mnemos_delete_graph_project` | Drop the index (sidecar only); the only way to clear the poisoned set |
| `mnemos_register_project` | Register a root (#454) — the agent-side answer to "not registered" |

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
| **Poisoned files (PG3)** | A file that trips the secrets detector at index time is poisoned **forever**: its symbols stay in the graph, its snippets are never issued. Two exits only: `mnemos_delete_graph_project`, and the operator's `secret_allowlist` (below) — an allowlisted path skips poison-marking, and a previously-poisoned allowlisted path is un-poisoned on the next index run with an `allowlist-unpoison` audit row (never silent). The per-range issuance scan (PG4) is never waived by the allowlist. |
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
| `auto_index` | `true` | Native auto-indexing (PG-0.5): MCP calls and `pre_llm_call` hints auto-register (manifest-gated) and index projects in the background. `false` keeps the manual tools. |
| `auto_register_max_projects` | `64` | Global cap on auto-registered projects; past it, hints skip silently with an `auto-register-capped` audit row. |
| `auto_reindex_min_interval_sec` | `300.0` | Minimum seconds between background auto (re)index runs per project. |
| `watch` | `true` | Arms the watch poll (`mnemos_watch_start`); inert until an explicit registration. |
| `index_max_files` | `20000` | Hard cap on indexed files per project (fail-closed). |
| `index_max_source_mb` | `500` | Hard cap on total source bytes per project, MiB (fail-closed). |
| `secret_allowlist` | `[]` | Repo-relative path globs (`fnmatch`) whose files skip PG3 poison-marking at index time — the escape hatch for known-fake secret fixtures (test data, docs samples). The file is still indexed normally; a previously-poisoned allowlisted path is un-poisoned on the next index run (audited as `allowlist-unpoison`). The issuance scan (PG4) is never waived. Removing a glob is not retroactive: an un-poisoned file stays clean until its content changes and re-trips the detector at index time. `fnmatch` semantics: `*` also matches `/` (so `tests/*` reaches nested paths too). |
| `watch_max_registrations` | `8` | Global cap on active watch registrations per process. |
| `watch_base_interval_sec` / `watch_interval_per_500_files` / `watch_max_interval_sec` | `5.0` / `1.0` / `60.0` | Adaptive poll interval: base + 1 s per 500 indexed files, capped. |

Environment overrides follow the canonical settings pattern:
`VESMA_CODE_GRAPH__INDEX_MAX_FILES`,
`VESMA_CODE_GRAPH__INDEX_MAX_SOURCE_MB`,
`VESMA_CODE_GRAPH__SECRET_ALLOWLIST` (JSON array, e.g.
`VESMA_CODE_GRAPH__SECRET_ALLOWLIST='["tests/fixtures/**"]'`).

---

## Boundaries & FAQ

- **Why does every call demand `agent`?** PG7 attribution: audit trails and
  per-agent accountability. `mnemos_recall_context` carries no `agent`, so the
  graph surface never rides on it — pass the caller's identity explicitly.
- **My project is a bare `.git` checkout / has no packaging manifest.** For
  the *manual* path that is fine — registration is by project record, not by
  manifest detection: give the project an absolute `paths[0]` and index it.
  The *auto* path is stricter: no manifest in the cwd, no auto-registration.
- **Multiple paths on one project?** The graph indexes the first registered
  path (`paths[0]`) — one root, one graph per project.
- **My project root moved on disk.** The registration goes ghost:
  `mnemos_list_graph_projects` shows `root_missing: true`, indexing refuses.
  Repair with `vesma graph repoint <project> <new-root>` (#450) — the stale
  index is purged and the next index rebuilds fresh.
- **A file changed after indexing.** Snippets come back with a `stale` marker
  instead of content; reindex (or let the watch poll do it) to refresh.
- **How do I turn it all off?** `code_graph.enabled: false` — tools answer
  `disabled`, the beacon hides, nothing indexes. Want to keep the manual
  tools but stop the background auto path? `code_graph.auto_index: false`.
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
`src/vesmaro/codegraph/` (auto path: `autoindex.py`,
`tests/test_codegraph_autoindex.py`); landed in PG-0 wave (#438),
graphs-on-by-default (#440), native auto-indexing PG-0.5 (re-landed
150cdfe). Feature map: [features.md](../features.md)._

_Last updated: 2026-09-30_
