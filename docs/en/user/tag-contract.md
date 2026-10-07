# Tag Contract

**🌐 Language / Язык:** English · [Русский](../../ru/user/tag-contract.md)

Vesma enforces a structured tag schema on every memory entry. This document
describes the contract, the set of valid values, and migration guidance.

---

## Why a tag contract?

Memory entries without consistent structure become unsearchable noise.
The tag contract:

- Pins every entry to exactly **one project** and **one agent**
- Optionally narrows the entry to **one task scope** (`task:`, ADR-0027)
- Classifies the entry with at least **one `mnemos:` subtype** (cognitive category)
- Enables per-agent recall (M3) and project-scoped cleanup
- Prevents ambiguous dual-project entries (a common source of context pollution)

---

## Storage prefix: `mnemos:` vs the `vesma:` input alias

`mnemos:` is the canonical storage prefix, stable by contract; `vesma:` is
accepted as an input alias everywhere.

- **You type** `vesma:<subtype>` — at the CLI (`--tags`), in HTTP API tag
  filters and in MCP add calls. Vesma normalizes it to `mnemos:<subtype>`
  before anything is written or matched.
- **Storage keeps `mnemos:*`.** The prefix is a frozen data format
  (6.0 decision, ArchCom 2026-10-03): exports, stores and federation
  traffic stay byte-stable across the rebrand.
- **Unknown subtypes are refused loudly** in either spelling: `vesma:bogus`
  fails with `invalid vesma: alias ...`, exactly like `mnemos:bogus` does.
- `vesma:no-federate` normalizes too — the exclusion marker is always
  stored as `mnemos:no-federate` (a byte-stable trust marker, see below).
- Legacy input keeps working unchanged: `mnemos:*` passes through as-is,
  and old `gcw:*` tags still migrate on validation.

---

## Required tags (must be present on all new entries)

| Tag | Format | Cardinality | Purpose |
|-----|--------|-------------|---------|
| `project:<slug>` | `[a-z0-9][a-z0-9\-_]*` | **exactly 1** | Binds entry to a codebase / initiative |
| `agent:<slug>` | `[a-z0-9][a-z0-9\-_]*` | **exactly 1** | Agent that authored the memory |
| `mnemos:<subtype>` | see table below | **at least 1** | Cognitive category (a legacy prefix — the format-stable namespace; a valid tag is `mnemos:decision`, not `vesma:decision`). |

### `mnemos:` subtypes

| Subtype | When to use |
|---------|-------------|
| `session` | Session continuity checkpoints |
| `bug-pattern` | Recurring failure modes, root-cause patterns |
| `learning` | New facts acquired during a task |
| `decision` | Explicit architectural / product decisions |
| `rule` | Hard constraints and invariants (from instructions files, etc.) |
| `open-question` | Unresolved questions requiring future investigation |
| `checkpoint` | Mid-session snapshots for compaction survival |
| `synthesized` | Records synthesised by the knowledge pipeline (the synthesis worker, not an authoring agent) |
| `legacy` | Migrated entries from ai-brain or pre-contract stores |
| `no-federate` | **Exclusion marker** — record is excluded from all external exchange (batch export + mediated pull). See `mnemos:no-federate` below. |

---

## Optional tags

These are accepted but not required. Unknown prefixes not listed here are
rejected in strict mode.

| Tag | Format | Purpose |
|-----|--------|---------|
| `task:<slug>` | `[a-z0-9][a-z0-9\-_]*` (max 64) | Binds the entry to one task scope (see below) |
| `source:<slug>` | any string | Origin of the entry (chat, file, url, …) |
| `applyTo:<glob>` | file glob | Scope a `rule` to specific file paths |
| `milestone:<id>` | any string | Links entry to a project milestone |
| `domain:<slug>` | any string | Domain sub-classifier within a project |

---
## `task:` — task scope (multi-context memory, ADR-0027 Phase 0)

`task:<slug>` is an **optional scope tag** (at most **one** per entry,
format `^task:[a-z0-9_-]{1,64}$`) introduced by the multi-context memory
decision (ADR-0027, epic #308, Phase 0 — composition without schema
migration). It lets one agent run several concurrent tasks over one mixed
corpus: entries written while working on a task carry that task's tag;
entries without a `task:` tag imply **no global task** — they belong to
the enclosing project/agent scope only.

**Scope hierarchy = intersection, never union** (ADR-0027 doctrine):
`project × agent × session × task`. A task tag can only **narrow** the
admissible set, never widen it. A task-scoped query
(`search(..., tags=["task:x"], project=..., agent=...)`) returns only
rows that are inside the project AND the agent AND the task. A scoped
search that returns zero rows is treated as information ("this task has
no matching rows yet"), not as scope drift: the project soft-fallback
retry is deliberately **not** applied to task-scoped queries, so a
task-scoped search can never widen back out of its project scope.

Multiple `task:` tags on one entry are always rejected (strict AND lax
mode): an entry belongs to at most one task — two task tags would make
the record visible in two task scopes (a union), which the intersection
doctrine forbids.

**Task-scoped assembly (ADR-0027 Phase 0, slice 2).** The assembly side
of the contract is the optional `task` parameter of
`assemble_context(..., task=<slug>)` (the `pre_llm_call` hook and the
REST/MCP hook twins thread it; SDK callers pass it through
`VesmaSDK.assemble_context`). It takes the **bare slug** —
`[a-z0-9_-]{1,64}`, no `task:` prefix (the prefix is added internally).
The parameter narrows recall to entries carrying `task:<slug>` — through
the whole six-stage pipeline (knowledge leg, graph edge leg and, when
lanes are enabled, the governance lanes: a task-scoped assembly never
surfaces a row the task-less assembly would not) — and composes the
**per-call assembled tail only**: pinned prefixes, the provenance format
and the CacheAligner prefix-stability contract are untouched (ADR-0027
invariant 1). The result echoes `task` (and `stats.recall.task_scoped`)
only when the parameter was given; without it the output is
byte-identical to the pre-Phase-0 shape.

**Task-switcher parity (ADR-0027 Phase 2, epic #308).** The bare-slug
`task=` parameter now rides EVERY task-scoped surface as a first-class
switcher, byte-identical to the raw `task:<slug>` tag filter (the F1
experiment's arm-C surface — the A==C equivalence doctrine, pinned by
tests on every surface):

- **Reads:** `search(task=)`, `recall_context(task=)`,
  `list_recent(task=)` (REST `GET /memories?task=`), `agent_recall`
  (`task` on `AgentRecallQuery`). Combined with `tags=` the two
  intersect (both must hold).
- **Writes:** `save_checkpoint(task=)` (MCP `mnemos_save_context` /
  REST `POST /context/save`) mints the `task:<slug>` tag at the single
  save boundary — at most one task per record (zero-or-one invariant).
- **Boundary:** the slug is normalized with the same canon as `project`
  (`My Task` → `my-task`); an unsalvageable slug fails loud (MCP error
  string / REST 400 — never a silently-different task namespace).
- **Telemetry:** `search_stats()` exposes `task_param_queries_total`
  vs `task_tag_queries_total` — the owner's standing comparative metric
  watching which switcher form callers use (the F1 doctrine makes the
  two forms semantically identical; the counters watch adoption).

---
## `mnemos:no-federate` — federation exclusion marker

`mnemos:no-federate` is an **exclusion marker**, not a cognitive category.
It lives in the `mnemos:` subtype namespace (so it passes tag-contract
validation without a new prefix; typing `vesma:no-federate` normalizes to
the same stored tag) but its semantics are operational, not
cognitive: a record carrying this tag is **excluded from all external
exchange** — both batch export and mediated pull (federation).

### How it gets added

The tag is added automatically by the **write-path secrets scanner**
(Layer 1 of the federation defence-in-depth, see
[Security — Federation defence-in-depth](../admin/security.md#11-federation-defence-in-depth)).
When `mnemos_add` (or the HTTP `POST /memories`, or `ingest_url`, or
`ingest_path_scoped_rules`) receives content that matches a known secret
pattern (AWS keys, GitHub tokens, Slack tokens, OpenAI/Anthropic keys,
JWTs, PEM private keys, database connection strings, high-entropy
base64 spans), the scanner:

1. Detects the pattern via `mnemos.secrets_detector.detect_secrets`.
2. Appends `mnemos:no-federate` to the tag list (idempotent — if the tag
   is already present, it is not duplicated).
3. Logs the pattern names and counts only — **never** the raw matched
   values.

### How to remove it

Removing the tag re-enables external exchange for the record. Because the
tag is typically auto-added when a secret was detected, removing it
blindly could expose a real secret to federation. The removal path
(`MemoryManager.remove_no_federate`) therefore:

- Requires explicit confirmation (`confirm=True`). Without confirmation
  the record is **not** mutated and a warning is returned.
- Re-scans the content after removal. If a secret is still present, the
  tag is **re-added automatically** and the report records
  `re_detected=True`. The owner must redact the content first (see
  `mnemos.secrets_detector.redact_content`) to permanently remove the tag.

### Difference from the (non-existent) `mnemos:no-export`

There is **no** `mnemos:no-export` tag. The earlier design idea was
renamed to `mnemos:no-federate` because the same exclusion must cover
**both** batch export and mediated pull — a single tag for both paths.
`mnemos:no-federate` supersedes the `no-export` idea entirely.

---
## Validation modes

### Strict mode (`strict_tag_contract=True`, default for new installs)

- All three required tag families must be present.
- `TagContractError` is raised if any required tag is missing, malformed,
  or duplicated.
- Used by `mnemos_add` (MCP tool) and `Memory(strict_tags=True)`.

### Lax mode (`strict_tag_contract=False`, for migrations)

- Missing required tags emit a warning but do **not** raise.
- Multiple `project:` / `agent:` / `task:` tags still raise (always ambiguous).
- An invalid `task:` slug is normalized when salvageable (case, spaces);
  an unsalvageable one is **dropped** — lax mode never mints a fake
  `task:unknown` scope.
- Used by `vesma migrate from-ai-brain` CLI command.

---

## Python API

```python
from vesma.models import validate_tag_contract, TagContract, TagContractError

# Validate a list of tags (strict, raises on violations).
# You type the vesma: alias — the result carries the canonical mnemos: form.
clean_tags = validate_tag_contract(
    ["project:myproject", "agent:copilot", "vesma:learning"],
    strict=True,
)
# clean_tags == ["project:myproject", "agent:copilot", "mnemos:learning"]

# Use TagContract model directly
tc = TagContract(tags=["project:myproject", "agent:copilot", "vesma:decision"])
print(tc.project)       # "myproject"
print(tc.agent)         # "copilot"
print(tc.mnemos_subtypes)  # {"decision"}

# With an (optional) task scope tag
tc = TagContract(
    tags=["project:myproject", "agent:copilot", "vesma:learning", "task:refactor-auth"]
)
print(tc.task)          # "refactor-auth" ("" when the entry carries no task:)

# Pass tags when creating a Memory
from vesma.models import Memory
m = Memory(
    content="Decided to use FTS5 over a dedicated search service.",
    tags=["project:vesma", "agent:tech-lead", "vesma:decision"],
    project="vesma",
    agent="tech-lead",
)
```

---

## MCP usage

```
mnemos_add(
    content="Discovered timing issue in FTS5 query planner.",
    tags=["project:vesma", "agent:copilot", "vesma:bug-pattern"],
    project="vesma",
    agent="copilot",
)
```

---

## Bulk tag rename (`gcw:` → `mnemos:` and other prefix changes)

The `vesma tags rename` command (and the equivalent `mnemos_tags_rename`
MCP tool / `POST /tags/rename` HTTP endpoint) bulk-renames tags matching a
source prefix to a target prefix across existing memories. It is the safe
replacement for the deprecated `vesma migrate tags` subcommand.

```bash
# Dry-run first — preview only, nothing written (default)
vesma tags rename --from gcw: --to mnemos: --dry-run

# Apply the rename
vesma tags rename --from gcw: --to mnemos: --no-dry-run

# Restrict to specific subtypes
vesma tags rename --from gcw: --to mnemos: --subtypes decision --subtypes learning --no-dry-run

# Scope to a single project / agent
vesma tags rename --from gcw: --to mnemos: --project vesma --no-dry-run

# Send invalid subtypes to <to_prefix>legacy instead of skipping them
vesma tags rename --from gcw: --to mnemos: --invalid-to-legacy --no-dry-run
```

**Why this is safe:** the rename goes through `SQLiteStore.update_fields`
(a plain `UPDATE`), so the FTS5 `AFTER UPDATE` trigger fires and the
external-content index stays consistent — unlike the old `migrate tags`
path which used raw `sqlite3` writes and bypassed the trigger. The
operation is **idempotent**: a second run with the same arguments reports
`renamed=0` because the `from_prefix:` tags no longer exist.

**Re-embedding:** vectors are keyed by `memory_id` and the embedded text
is derived from `title + content + tags`. Tags are part of the embedded
text, so a rename *technically* changes the embedding input, but the
contribution is small relative to content. The rename deliberately does
**not** re-embed — semantic search continues to work because the stored
vectors still point to the same memory ids and the FTS5 leg (which reflects
the new tags via the trigger) carries tag-filtered queries. If exact
tag-vector alignment is required, run `vesma reindex` afterwards.

**Audit trail:** each call writes a single row to the trace table with
`step="tags_rename"` recording the prefixes, dry-run flag, and counts.

The report returned (and printed by the CLI) has the shape:

```json
{
  "scanned": 42,
  "renamed": 18,
  "changed": 18,
  "skipped_invalid": 0,
  "errors": [],
  "dry_run": false,
  "from_prefix": "gcw:",
  "to_prefix": "mnemos:"
}
```

---

## Grouped tag tool: `mnemos_tags` (rename / remove / add)

`mnemos_tags` is a **pilot** for consolidating MCP tools via an `action: enum`
dispatch (vesma #97). It groups three bulk tag operations behind one tool
name, selected by the `action` parameter — **not** dot-notation and **not**
`oneOf`/discriminated unions (which MCP clients do not render reliably).
`action: enum` + flat properties was verified to work in VS Code, Claude
Desktop, and Continue (ArchCom 2026-07-18 session 2).

| Action | What it does | Key params |
| --- | --- | --- |
| `rename` | Same as `mnemos_tags_rename` (prefix → prefix) | `from_prefix`, `to_prefix`, `subtypes`, `invalid_subtypes_to_legacy` |
| `remove` | Drop exact tags (or, with `wildcard=true`, prefix-matched tags) | `tags[]`, `wildcard` |
| `add` | Append tags to every memory matching a `project`/`agent` filter | `tags[]`, `project`, `agent` |

All actions accept `dry_run` (default `true`) and `project` / `agent` scope
filters. Every mutation goes through the same shared commit path plus an
FTS5-safe `UPDATE` (the `memories_au` trigger fires), so the external-content
index stays consistent. The contract gate differs by action:

- `rename` validates in **lax** mode — it is a prefix swap (e.g.
  `gcw:` → `mnemos:`) that preserves required tags, so lax is the
  non-corrupting mode there.
- `remove` / `add` validate the resulting tag set in **strict** mode — a
  contract-breaking result (e.g. removing the last `project:` tag, or adding
  an invalid `mnemos:` subtype / a malformed slug) is rejected per memory with
  an entry in `errors` and the write is **skipped** for that memory, rather
  than corrupting the store.

```
mnemos_tags(action="rename", from_prefix="gcw:", to_prefix="mnemos:", dry_run=False)
mnemos_tags(action="remove", tags=["severity:high"], dry_run=False)
mnemos_tags(action="remove", tags=["gcw:"], wildcard=True, dry_run=False)
mnemos_tags(action="add", tags=["severity:high"], project="vesma", dry_run=False)
```

**Reports:** `rename` returns the same shape as `mnemos_tags_rename`
(`scanned`, `renamed`, `changed`, `skipped_invalid`, …), where `changed`
equals `renamed` and is added for a uniform report shape across all actions.
`remove` returns `{action, scanned, changed, removed_tags, wildcard, errors, dry_run}`;
`add` returns `{action, scanned, changed, added_tags, errors, dry_run}`.
All three are idempotent (a repeat run reports `changed=0`).

**Alias (non-breaking):** the legacy `mnemos_tags_rename` MCP tool still
works — internally it routes to `mnemos_tags(action="rename")`, so existing
callers and `.agent.md` references keep working unchanged. No deprecation
notice is added yet (that follows one release after the pilot lands).

**Edge cases:** `remove` and `add` are explicit — neither ever interprets an
empty target as "magic". Because both validate the resulting set in strict
mode, any contract-breaking result is rejected per memory with an entry in
`errors` and the write is skipped for that memory: removing the last
`project:`/`agent:`/`mnemos:` tag, adding a second `project:` tag, adding an
invalid `mnemos:` subtype, adding a malformed slug, or adding a tag without a
`:` prefix. `rename` stays lax (prefix swaps preserve required tags).

---

## Migration guide (from ai-brain)

ai-brain had no required tag schema. Migrating:

1. Run `vesma migrate from-ai-brain` — copies ai-brain SQLite to Vesma store.
2. Existing entries without `project:` / `agent:` get tag `mnemos:legacy` appended
   and are stored with `strict_tags=False`.
3. Check the contract with `vesma tags validate` (vault walk) or
   `vesma tags audit` — a report over every SQLite store entry, with
   `--apply` for additive healing (see
   [cli-reference.md](cli-reference.md#tags-audit)).
4. Edit entries manually to apply best-effort defaults
   (`project:unknown`, `agent:unknown`).
5. Flip `strict_tag_contract=True` in `~/.mnemos/config.yaml` once clean.

---

## TagContractError reference

```
mnemos.models.TagContractError
```

Raised by `validate_tag_contract(..., strict=True)` and `TagContract(strict=True)`.

Common messages:

| Message fragment | Cause |
|-----------------|-------|
| `exactly one project:` | 0 or ≥2 `project:` tags |
| `exactly one agent:` | 0 or ≥2 `agent:` tags |
| `at least one mnemos:` | No `mnemos:` tag present (the validator's real message fragment) |
| `invalid mnemos: subtype` | Subtype not in allowed set |
| `invalid vesma: alias` | A `vesma:`-spelled tag whose subtype is not in the allowed set (see the input alias above) |
| `invalid slug for project:` | Slug contains uppercase or special chars |
| `invalid slug for agent:` | Slug contains uppercase or special chars |
| `at most one task:` | ≥2 `task:` tags (always fatal, strict and lax) |
| `invalid task: tag format` | Task slug contains uppercase, spaces, special chars, or exceeds 64 chars |
