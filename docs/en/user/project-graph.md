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
| **Nodes** | `Project`, `File`, `Module`, `Class`, `Function`, `Method`, `Type` — plus `Command` / `Route` on a defining repo (schema v2, [below](#cli-commands-and-rest-routes-in-the-graph-schema-v2)) |
| **Edges** | `CONTAINS_FILE`, `DEFINES`, `IMPORTS`, `CALLS`, `INHERITS`, `TESTS`, `USES` — plus `INVOKES` / `HANDLES` (schema v2) |

The `USES` edge is the honesty marker of the family: a call is recorded as
`CALLS` only when the parser can prove the target; everything else that looks
like a call is `USES` with `provenance: heuristic`. A retrieved edge never
claims more certainty than it has.

---

## Supported languages

| Language | Since | Indexed surface |
|----------|-------|-----------------|
| Python | PG-0 (#438) | `.py` |
| Go (#470) | 2026-10 | `.go` |

Anything else is simply not indexed — an unknown extension never falls
through to a wrong parser. What the graph sees in a Go tree:

- **Symbols** — top-level functions (`Function`), methods with receivers
  (`Method`, qname `pkg.Type.Name`), struct and interface types (`Class`),
  other defined types and aliases (`Type`). Qualified names carry the dotted
  package directory (`pkg.util.Greeter.Greet`); a module node's qname is the
  package directory.
- **Proven `CALLS`** — plain-identifier calls inside one Go package (package
  scope makes the name unique, including across files of the same package)
  and `pkg.Ident(...)` calls through a resolved import. Type conversions
  (`Base(x)`) count as calls to the type, mirroring the Python constructor
  rule.
- **Heuristic `USES`** — method calls on values/pointers (`obj.Method(...)`;
  the receiver's type is not inferred, so a name match stays heuristic) and
  type references from composite literals (`&Server{...}`) and `new`/`make`
  type arguments.
- **`INHERITS`** for struct/interface embedding; **`IMPORTS`** when an import
  path matches an in-repo package directory (the edge lands on the package's
  first sorted file's module; stdlib/external paths resolve to nothing);
  **`TESTS`** from `foo_test.go` to its own package's module.
- **Honestly skipped** — interface satisfaction (implicit in Go; no edge is
  invented), calls through function variables, generic instantiations
  (`F[T](...)`), dot- and blank-imports. The import path string is the only
  string content ever read, and only to resolve imports — it never reaches
  the store (PG1 holds for Go sources the same way it holds for Python).

---

## CLI commands and REST routes in the graph (schema v2)

The engine's own surface is first-class graph data. When the indexed root
**is a vesma checkout** — detected by the presence of the engine's `cli` /
`api` package markers inside the root, never by a hardcoded path — a surface
node-source extension contributes two more node kinds on every full index:

| Kind | One node is | Example |
|------|-------------|---------|
| `Command` | one CLI invocation path: full command name, one-line help, option count, up to 16 compact `param → help` entries | `vesma graph delete` |
| `Route` | one HTTP method + path: one-line description and the implementing endpoint's name | `GET /api/v1/metrics` |

The data comes from the LIVE engine registries — the same typer/click tree
the shell-completion engine walks (`vesma __complete`) and FastAPI's
`app.routes` (every included router: sessions, auth, federation) — so there
is no second parser to drift. Each node binds to its implementing function
with an **`INVOKES`** (Command → handler) or **`HANDLES`** (Route →
endpoint) edge whenever that function resolves in the indexed tree
(import-path match first, then a unique dotted-suffix match for src-layouts;
an unresolved handler gets NO edge — the unresolved-import honesty rule).

Token economics (the reason this exists): "how do I delete a graph index"
is ONE bounded search instead of an ~18.7k-token `--help` discovery:

```
vesma_search_graph(project_id="vesma", query="graph delete", kind="Command")
→ 1 row: "vesma graph delete" · help: "Delete a project's graph index…"
  options: 5 · params: {project, --force, --confirm-name, --agent, --reason}
```

Guards:

- help strings are ONE-LINE only — full help text never rides a node;
- every issued string passes the secrets detector (a hit drops the string,
  never the node);
- a root without the markers — any foreign repo — grows ZERO surface nodes:
  the extension is registered once by the host and decided per-root;
- `vesma_project_graph_status` reports the per-kind `node_kinds` breakdown
  so the contribution is visible, not assumed.

**Cross-project agents:** commands and routes are code of the DEFINING repo.
An agent working in another project queries them with `project_id` of the
vesma repo's own project (the one whose registered root carries the engine),
not of the agent's current project — then follows the `INVOKES`/`HANDLES`
edge into the handler's code.

**Existing indexes:** surface nodes land on the next FULL (re)index — the
incremental classifier rebuilds the whole project on any file change, so
touching a tracked file is enough; or drop the index
(`vesma_delete_graph_project`) and index fresh.

---

## Turn it on for a project

The graph indexes **registered roots only** (PG2). A root is the first
absolute path on a project record (`paths[0]`). Registration is an operator
step — projects auto-created by memory writes carry no paths, so a graph call
for them answers a confinement refusal (the refusal text names the fix) until
a root is registered. Two ways to register:

- **Agent-side** (#454): the `vesma_register_project` tool — `project_id`,
  the absolute `root`, and the mandatory `agent` attribution. The root must
  exist, carry a packaging manifest or a `.git`, and not be `$HOME`/the
  filesystem root (compared on the realpath, so a symlink or `..` spelling
  of a forbidden target is refused too, #464). Idempotent when the root is
  already registered.
- **CLI**: `vesma graph register <project> <root>` — the same gates and
  audit trail (`manual-register`), from the terminal.

> **Threat-model boundary (issue #464).** Registration is a **read-scope
> grant**: once a root is registered, its symbols and snippets are readable
> through the graph tools by every agent on the server, so letting an agent
> register arbitrary roots lets it widen its own read scope over any git
> checkout on the host. The agent call path is therefore gated by
> `code_graph.agent_registration` (default `true` under the single-user
> stdio assumption; set `false` on multi-agent or untrusted-agent
> deployments — a gated attempt is refused and audited as
> `manual-register-refused`). The operator CLI path is never gated. Register
> refusals and repoint refusals are audit-first-class (`manual-register-refused`
> / `repoint-refused`), and `vesma graph repoint` recovers **ghost**
> registrations only — a registration whose root still exists on disk is
> refused (move-root is not repoint).

The raw store write also works (what the tools do underneath):

```python
from vesma.models import Project
mgr.sqlite.save_project(Project(name="vesma", paths=["/home/you/vesma"]))
```

Explicit registration does **not** count against `auto_register_max_projects`
— that cap bounds the auto path only.

Then the flow is three tool calls (every one needs an `agent` — see
[Boundaries & FAQ](#boundaries--faq)):

1. **Index** — `vesma_index_project` with `project_id` and `agent`. The first
   run is a full index; later runs are incremental by default (skip when
   nothing changed, per mtime+size classification).
2. **Check** — `vesma_project_graph_status`: node/edge/file volumes, fresh %,
   parse failures and the poisoned count.
3. **Use** — `vesma_search_graph` for ranked symbol search,
   `vesma_get_file_outline` for a file's shape, `vesma_trace_path` for
   call/navigation walks, `vesma_get_code_snippet` for line ranges.

Prefer zero-touch? The next section describes the native auto path — the
manual flow above keeps its full contract either way.

---

## Hybrid search: the literal fallback (W-H)

The symbol index is shapes-only — string literals (tool names, route
paths, env-var names) are invisible to it. Since W-H, `vesma_search_graph`
is **hybrid**: when the symbol graph returns ZERO hits for a query, a
bounded read-only literal scan of the registered project root answers with
content rows instead of an empty result.

Behavior:

- **Symbol first, always.** Any symbol hit — the answer is symbol rows
  only; the fallback never runs. Every symbol row now carries the additive
  `match_kind: "symbol"`.
- **Literal rows.** On an empty symbol result the scan answers
  `match_kind: "literal"` rows (`path` / `line` / `snippet`, repo-relative,
  line-text trimmed and capped at 240 chars, max 20 rows). They carry no
  node ids — they are file content, not graph nodes. A top-level
  `fallback_used: true` marker appears ONLY when the fallback ran (absent
  otherwise — the shape never carries null placeholders).
  `total_matches` counts the whole answer: when the fallback answers, it
  equals the number of literal rows returned (a non-empty fallback is
  never reported as 0).
- **Bounded.** The scan reuses the indexer's surface denylists
  (`.git`, `.venv`, `node_modules`, vendored trees, dotfiles,
  secret-bearing file names are never opened), never follows symlinks,
  skips binary content and files over 1 MiB, and stops at a hard cap
  (file count, ~2 s wall clock). A capped scan is logged as incomplete.
- **Confined.** The scan never leaves the registered root. An
  unregistered project is refused before any scan.
- **PG4 holds.** Every literal row passes the same secrets detector as
  snippet issuance; a finding DROPS the row (raw content is never issued),
  and poisoned paths (PG3) never issue content at all. A scan that cannot
  complete safely degrades to a no-fallback empty answer — never raw
  content.
- **Knob.** `code_graph.literal_fallback` (default `true`) disables the
  leg; `search_graph` then stays symbol-only. The REST twin
  `POST /graph/search` inherits the whole behavior unchanged.

---

## The walk section: rent the neighborhood (PG-1, ADR-0038)

A structural question — "what calls `X`, one or two hops out" — used to
cost either a `vesma_trace_path` call per symbol or opening whole files.
When `code_graph.search_walk` is on, every symbol-hitting
`vesma_search_graph` call ALSO walks the hits' neighborhood and answers
with a SEPARATE `walk` section:

```json
"walk": {
  "origins": ["Base", "Base.greet", "make_base"],
  "k": 3,
  "nodes": [ { "id": "...", "qname": "Base", "kind": "Class", "path": "pkg/helper.py",
               "start_line": 1, "end_line": 3, "depth": 0 }, "…"],
  "edges": [ { "from": "...", "to": "...", "kind": "CALLS", "provenance": "tree-sitter" } ],
  "truncated": false,
  "epoch": 4,
  "walk_cursor": 0,
  "has_more": false,
  "last_indexed_at": "2026-10-06T00:00:00+00:00"
}
```

How it works:

- **Separate, never mixed.** The walk is its own top-level section;
  `results` keep the pinned search shape unchanged. The section appears
  ONLY when it fired (symbol hits existed) — absent otherwise, never
  null/empty (the `fallback_used` shape precedent). The literal-fallback
  leg never walks (its rows carry no node ids).
- **Origins = the quota.** `k = min(ceil(limit/5), limit//2)` top ranked
  symbol hits become the walk origins (a pure function of `limit`).
- **Two hops, both directions.** Nodes are PG1 metadata rows
  (`id`/`qname`/`kind`/`path`/`start_line`/`end_line`/`depth`) — no
  signatures, repo-relative paths — sorted by `(depth, qname)`. Edges
  keep the trace contract (`from`/`to`/`kind`/`provenance`), walking
  in+out per origin, depth ≤ 2, over the structural kinds
  `CALLS` / `IMPORTS` / `INHERITS` / `USES` / `INVOKES` / `HANDLES`
  (file-shape edges — `CONTAINS_FILE` / `DEFINES` / `TESTS` — stay out).
- **Capped, honestly.** Per origin the shared walker enforces the
  fanout cap (32) and the total-work cap (512 visited nodes)
  INDEPENDENTLY of `k` + a project-boundary guard (an edge leaving the
  project is skipped, not followed); any cap or boundary hit sets
  `truncated: true` — the walk never pretends it showed everything.
- **Fresh on arrival.** The section carries the graph `epoch` —
  consumers invalidate on it; there is no TTL cache of walks.
- **Own budget.** The walk `nodes` ride the same token contract
  (4 bytes/token ceiling, whole-row drops) with their OWN
  `walk_cursor` — paginate the section with `walk_cursor` the same way
  `results` paginate with `cursor`.
- **It prices itself.** Every walk execution writes a `search-walk` row
  into the sidecar `graph_audit` with `out_tokens` (the answer cost) and
  `avoided_bytes` (the source-file bytes behind the visited nodes the
  agent would otherwise have opened whole).
- **Knob.** `code_graph.search_walk` (default `false`) — the flag-off
  period is the measured "search + read" baseline; turn the walk on
  deliberately (env: `VESMA_CODE_GRAPH__SEARCH_WALK`).

---

## Native auto-indexing (zero-touch)

Since PG-0.5 the graph indexes **by itself**: every MCP tool call and every
`pre_llm_call` hook fires a background *hint*, and a hint can auto-register
and index the project — no `vesma_index_project` call, no instruction, no
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
| **Suspension** | A failed *first* auto index suspends the auto path for that project: further hints skip it entirely — until a successful manual `vesma_index_project` (the watch poll rides the same method) or a `vesma_delete_graph_project` lifts the flag. A failed *stale* reindex never suspends anything. |

v1 boundaries: a call without an `agent` never triggers the auto path; REST
is deliberately not a hint surface; a multi-path registration still indexes
`paths[0]` only. Turn the auto path off alone with
`code_graph.auto_index: false` — the manual tools keep working.

---

## Keep it fresh: the watch poll

`vesma_watch_start` registers an indexed project with an in-process poll
thread (no daemon). On an adaptive interval — 5 s base, +1 s per 500 indexed
files, capped at 60 s — it classifies files by mtime+size and reindexes only
on actual changes, audited with reason `watch`.

- Registrations live for the process lifetime; a restart drops them.
- Global cap: 8 active registrations (`code_graph.watch_max_registrations`).
- `vesma_watch_status` shows every registration and its last poll outcome;
  `vesma_watch_stop` stops one or all (idempotent).

---

## Moved roots: ghosts and `vesma graph repoint` (#450)

A project directory renamed or moved on disk leaves a **ghost**
registration: the auto path no-ops silently, `vesma_index_project`
refuses confinement ("registered root is missing on disk"), and — before
#450 — the only exit was deleting the graph. Ghosts are visible:
`vesma_list_graph_projects` marks them `root_missing: true`.

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

A ghost can also be removed outright — `vesma_delete_graph_project` (or
`vesma graph delete <project>` on the CLI) drops the index and, behind the
evidence gate (`confirm=true` + `confirm_name` echoing the project name;
`--force --confirm-name <project>` on the CLI), the registration row
itself. A gate-less attempt is refused and audited (`delete-refused`). A
**live** registration (root exists on disk) keeps its row — the delete
purges only the derived index.

---

## The graph tools

| Tool | What it does |
|------|--------------|
| `vesma_index_project` | Full or incremental index of a registered root; serialized per project |
| `vesma_project_graph_status` | Volumes, freshness, parse errors, poisoned count for one project |
| `vesma_search_graph` | Ranked search by name / qualified name / path; opt-in signatures; hybrid literal fallback on an empty result (W-H) |
| `vesma_trace_path` | BFS over edges from one symbol (depth ≤ 2, honest `truncated` flag); ambiguous bare tails answer a candidate list (W-H) |
| `vesma_get_file_outline` | Symbol outline of one indexed file — shapes, never bodies |
| `vesma_get_code_snippet` | Line range read **from disk**, freshness-checked and secret-scanned |
| `vesma_check_graph_coverage` | Per-path verdict: `indexed` / `stale` / `parse-error` / `unindexed` / `poisoned` |
| `vesma_get_graph_schema` | The contract card: kinds, limits, token contract |
| `vesma_list_graph_projects` | Registered projects joined with index status; ghosts marked `root_missing` |
| `vesma_delete_graph_project` | Drop the index (sidecar only); the only way to clear the poisoned set |
| `vesma_register_project` | Register a root (#454) — the agent-side answer to "not registered" |

Plus the watch family: `vesma_watch_start` / `vesma_watch_stop` /
`vesma_watch_status`.

Every windowed tool takes `max_output_tokens` (128–1,000,000, default 3200)
under a deterministic token contract: rows drop whole, never split, and the
cursor strictly advances. Full input schemas and examples:
[mcp-tools.md, "Project graph tools"](mcp-tools.md#project-graph-tools-adr-0032).

---

## Using the graph day-to-day

The graph earns its keep only when it is the FIRST stop, not the last
resort. The working loop:

1. **Find the symbol** — `vesma_search_graph` by name / qualified name /
   path. Ranked hits with opt-in signatures — narrower than grep noise.
2. **Follow the callers** — `vesma_trace_path` from the resolved
   qualified name. Edge traversal (calls / imports / inheritance) is the
   one answer text search structurally cannot give.
3. **Read exactly what you need** — `vesma_get_file_outline` before
   opening a big file, `vesma_get_code_snippet` for the chosen line
   ranges.

**When grep wins.** The graph indexes symbols only (zero source bytes,
PG1) — it does not see string literals. Tool names, route paths, env var
names, log strings, comments and docs-sample prose stay grep territory.
An unregistered repo has no graph at all — check
`vesma_list_graph_projects` first. And «unindexed» is not «missing»:
`vesma_check_graph_coverage` distinguishes `indexed` / `stale` /
`unindexed` / `poisoned` before you conclude a symbol does not exist.

**Hygiene.** `vesma_project_graph_status` / `vesma_check_graph_coverage`
are the honesty gates: parse failures stay visible («clean ≠ proof»), and
a poisoned count made entirely of test fixtures is an allowlist question,
not a fear question — the status answer says so (`hints`) and
`secret_allowlist` below is the escape hatch.

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
| **Poisoned files (PG3)** | A file that trips the secrets detector at index time is poisoned **forever**: its symbols stay in the graph, its snippets are never issued. Two exits only: `vesma_delete_graph_project`, and the operator's `secret_allowlist` (below) — an allowlisted path skips poison-marking, and a previously-poisoned allowlisted path is un-poisoned on the next index run with an `allowlist-unpoison` audit row (never silent). The per-range issuance scan (PG4) is never waived by the allowlist. |
| **Scan at issue (PG4)** | Snippets are read from disk at request time — there is no snippet cache. A mtime+size+sha256 mismatch yields a `stale` marker, never content; a secret found in the requested range refuses the whole range, fail-closed. |
| **Fail-closed limits (PG7)** | 20,000 files / 500 MB per project. A breach refuses the WHOLE index — no partial graph is ever published. |
| **Export-blind (PG5)** | The code map never leaves the server: export bundles and federation/mesh payloads never carry graph artifacts. Peers index their own local sources. |
| **Attribution & audit (PG7)** | Every call — read or write — is audited per agent; calls without an `agent` are refused with `attribution-required`. |
| **Zero source bytes (PG1)** | Dump the sidecar and you find names, paths, line ranges — no code, no literals, no secrets at rest. |

---

## Excluded directories (ephemeral surfaces)

The indexer never enters ephemeral directories — dependency trees, build
output, and git worktrees checked out INSIDE the registered root. The
live case (defect 2026-10-03): a `wt/` worktree inside a project root was
indexed like first-party sources — the file count doubled (376→746) and
secret fixtures inside the worktree copy were re-poisoned on every index.

Two layers apply, and the first one cannot be turned off:

- **Built-in denylist** (matched at any nesting depth): `.git`, `.hg`,
  `.svn`, `.tox`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`,
  `__pycache__`, `venv`, `.venv`, `node_modules`, `dist`, `build`,
  `target`, `site-packages`, `wt` (the `git worktree add wt/<name>`
  convention), plus vendored trees (`vendor`, `vendored`, `third_party`,
  `3rdparty`).
- **`exclude_globs` knob** (`code_graph.exclude_globs`, default = the
  ephemeral list above): prunes directories ON TOP of the denylist. A
  bare name (`wt`) matches a directory of that name at any depth; an
  entry containing `/` matches the repo-relative directory path
  (`fnmatch` semantics). Setting the field REPLACES the default list —
  but it can never lift the built-in denylist.

```yaml
code_graph:
  exclude_globs:
    - "wt"
    - "gen/**"   # /-bearing globs match repo-relative dir paths
```

Environment override: `VESMA_CODE_GRAPH__EXCLUDE_GLOBS` (JSON array, e.g.
`VESMA_CODE_GRAPH__EXCLUDE_GLOBS='["wt", "gen/**"]'`).

The same exclusion drives the literal-fallback scan and the freshness
beacon — a worktree that lands in the root after indexing never reads as
"stale files" and never auto-triggers a reindex.

---

## Configuration

All flags live under `code_graph:` in `config.yaml` (see `config.example.yaml`).
The surface is **on by default** (owner decision 2026-09-28).

| Key (`code_graph.`) | Default | Meaning |
|---------------------|---------|---------|
| `enabled` | `true` | Master flag for the 10 tools + the `/graph/` REST namespace; `false` hides the whole surface (every call answers `code: "disabled"`). |
| `agent_registration` | `true` | Whether connected MCP agents may register roots via `vesma_register_project` (#464 — registration is a read-scope grant). `false` reserves registration to the operator CLI; a gated attempt is refused and audited (`manual-register-refused`). The `vesma graph register` path is never gated. |
| `beacon` | `true` | One tail line in `assemble_context` output advertising graph freshness («indexed …, N/M files fresh — call vesma_search_graph»). Only when `enabled`. |
| `literal_fallback` | `true` | Hybrid search (W-H): when a symbol search returns ZERO hits, a bounded read-only literal scan of the registered root answers `match_kind: "literal"` rows (path/line/snippet) with a `fallback_used: true` marker — every row PG4-redacted, poisoned paths never issue, scan caps at file count / 1 MiB per file / ~2 s. `false` keeps `search_graph` symbol-only. Env: `VESMA_CODE_GRAPH__LITERAL_FALLBACK`. |
| `search_walk` | `false` | The separate `walk` section of `search_graph` (PG-1 M2, ADR-0038): when a symbol search hits, a two-level neighborhood walk (in+out, depth ≤ 2, quota `k = min(ceil(limit/5), limit//2)` origins, caps fanout 32 / total work 512 independent of `k`) answers the hits' surroundings as PG1 metadata rows in its own token-windowed section with its own `walk_cursor`, the graph `epoch` riding the payload, and a self-priced `search-walk` audit row (`out_tokens` / `avoided_bytes`). Never mixed into `results`; absent-when-empty; flag-off responses are byte-identical to the pre-walk shape. Env: `VESMA_CODE_GRAPH__SEARCH_WALK`. |
| `auto_index` | `true` | Native auto-indexing (PG-0.5): MCP calls and `pre_llm_call` hints auto-register (manifest-gated) and index projects in the background. `false` keeps the manual tools. |
| `auto_register_max_projects` | `64` | Global cap on auto-registered projects; past it, hints skip silently with an `auto-register-capped` audit row. |
| `auto_reindex_min_interval_sec` | `300.0` | Minimum seconds between background auto (re)index runs per project. |
| `watch` | `true` | Arms the watch poll (`vesma_watch_start`); inert until an explicit registration. |
| `index_max_files` | `20000` | Hard cap on indexed files per project (fail-closed). |
| `index_max_source_mb` | `500` | Hard cap on total source bytes per project, MiB (fail-closed). |
| `secret_allowlist` | `[]` | Repo-relative path globs (`fnmatch`) whose files skip PG3 poison-marking at index time — the escape hatch for known-fake secret fixtures (test data, docs samples). The file is still indexed normally; a previously-poisoned allowlisted path is un-poisoned on the next index run (audited as `allowlist-unpoison`). The issuance scan (PG4) is never waived. Removing a glob is not retroactive: an un-poisoned file stays clean until its content changes and re-trips the detector at index time. `fnmatch` semantics: `*` also matches `/` (so `tests/*` reaches nested paths too). |
| `exclude_globs` | `["wt", ".venv", "venv", "node_modules", "dist", "build", ".tox", ".git"]` | Directory globs excluded from the index surface ON TOP of the built-in denylist (see "Excluded directories" above). A bare name matches a directory at any nesting depth; a `/`-bearing entry matches the repo-relative directory path (`fnmatch`). Setting the field replaces the default list but never lifts the built-in denylist (`wt`, `node_modules`, `.git` & co. are never indexed). Env: `VESMA_CODE_GRAPH__EXCLUDE_GLOBS` (JSON array). |
| `watch_max_registrations` | `8` | Global cap on active watch registrations per process. |
| `watch_base_interval_sec` / `watch_interval_per_500_files` / `watch_max_interval_sec` | `5.0` / `1.0` / `60.0` | Adaptive poll interval: base + 1 s per 500 indexed files, capped. |

The product default stays `[]` — PG3 is a security gate, not an
inconvenience. The canonical fixture pattern (fake-secret test data, as
applied in practice 2026-10-03) scopes the hatch to test and benchmark
trees only, never source trees:

```yaml
code_graph:
  secret_allowlist:
    - "tests/**"
    - "benchmarks/**"
```

Environment overrides follow the canonical settings pattern:
`VESMA_CODE_GRAPH__INDEX_MAX_FILES`,
`VESMA_CODE_GRAPH__INDEX_MAX_SOURCE_MB`,
`VESMA_CODE_GRAPH__SECRET_ALLOWLIST` (JSON array, e.g.
`VESMA_CODE_GRAPH__SECRET_ALLOWLIST='["tests/fixtures/**"]'`).

---

## Boundaries & FAQ

- **Why does every call demand `agent`?** PG7 attribution: audit trails and
  per-agent accountability. `vesma_recall_context` carries no `agent`, so the
  graph surface never rides on it — pass the caller's identity explicitly.
- **My project is a bare `.git` checkout / has no packaging manifest.** For
  the *manual* path that is fine — registration is by project record, not by
  manifest detection: give the project an absolute `paths[0]` and index it.
  The *auto* path is stricter: no manifest in the cwd, no auto-registration.
- **Multiple paths on one project?** The graph indexes the first registered
  path (`paths[0]`) — one root, one graph per project.
- **My project root moved on disk.** The registration goes ghost:
  `vesma_list_graph_projects` shows `root_missing: true`, indexing refuses.
  Repair with `vesma graph repoint <project> <new-root>` (#450) — the stale
  index is purged and the next index rebuilds fresh. Not worth repairing?
  Remove the ghost entirely: `vesma graph delete <project> --force
  --confirm-name <project>`.
- **A file changed after indexing.** Snippets come back with a `stale` marker
  instead of content; reindex (or let the watch poll do it) to refresh.
- **How do I turn it all off?** `code_graph.enabled: false` — tools answer
  `disabled`, the beacon hides, nothing indexes. Want to keep the manual
  tools but stop the background auto path? `code_graph.auto_index: false`.
- **Do I need to back up `code_graph.db`?** No. It is a rebuildable sidecar
  in the data dir; `vesma_delete_graph_project` drops only the index, never
  the project entity or its memories.

---

## Related

- Full tool reference: [mcp-tools.md, "Project graph tools"](mcp-tools.md#project-graph-tools-adr-0032)
- REST endpoints: [http-api.md](http-api.md#project-graph-adr-0032)
- Design and invariants: [ADR-0032](../../project/adr/0032-project-graph.md)
- Architecture placement: [ARCHITECTURE.md §8a](../../../ARCHITECTURE.md)

---

_Sources: ADR-0032 (project graph as memory); `docs/en/user/mcp-tools.md`
(Project graph tools), `src/vesma/config.py` (`CodeGraphConfig`),
`src/vesma/codegraph/` (auto path: `autoindex.py`,
`tests/test_codegraph_autoindex.py`); landed in PG-0 wave (#438),
graphs-on-by-default (#440), native auto-indexing PG-0.5 (re-landed
150cdfe). Surface nodes (schema v2): card vesma-graph-command-route-nodes,
`src/vesma/codegraph/node_sources.py` (the seam) +
`src/vesma/graph_surface_ext.py` (the engine's CLI/REST surface).
Feature map: [features.md](../features.md)._

_Last updated: 2026-10-04_
