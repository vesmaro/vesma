---
applyTo: '**'
description: Mnemos tag contract — required tag composition for every mnemos_add call
---

# Mnemos Tag Contract

## Applies to / Применяется ко всем агентам

Every `mnemos_add` and `mnemos_ingest_url` call must carry a valid tag set.
Tags are the searchability backbone of the memory store — without them,
memory is unstructured noise.

---

## WHY

Tags = searchability. Without consistent tags:

- `mnemos_search` cannot filter by project or agent.
- `mnemos_agent_recall` cannot find agent-scoped entries.
- Project-scoped cleanup becomes impossible.
- Memory degrades into an unstructured log.

The tag contract enforces structure so that memory stays useful as it grows.

---

## WHAT — required composition

Every `mnemos_add` / `mnemos_ingest_url` call must include:

| Tag | Format | Cardinality | Purpose |
|-----|--------|-------------|---------|
| `project:<slug>` | `[a-z0-9][a-z0-9\-_]*` | **exactly 1** | Binds entry to a codebase / initiative |
| `agent:<slug>` | `[a-z0-9][a-z0-9\-_]*` | **exactly 1** | Agent that authored the memory (use `agent:user` for user-authored) |
| `mnemos:<subtype>` | see table below | **at least 1** | Cognitive category |

### Mnemos subtypes (whitelist)

| Subtype | When to use |
|---------|-------------|
| `mnemos:session` | Session continuity snapshots |
| `mnemos:checkpoint` | Mid-session compaction-resilient checkpoints |
| `mnemos:bug-pattern` | Recurring failure modes, root-cause patterns |
| `mnemos:learning` | Non-obvious facts acquired during a task |
| `mnemos:decision` | Explicit architectural / product decisions + rationale |
| `mnemos:rule` | Hard constraints and invariants |
| `mnemos:open-question` | Unresolved questions requiring future investigation |
| `mnemos:legacy` | Migrated entries from ai-brain or pre-contract stores |

### Optional tags (accepted, not required)

| Tag | Format | Purpose |
|-----|--------|---------|
| `source:<slug>` | any string | Origin of the entry (chat, file, url, …) |
| `applyTo:<glob>` | file glob | Scope a `mnemos:rule` to specific file paths |
| `milestone:<id>` | any string | Links entry to a project milestone |
| `domain:<slug>` | any string | Domain sub-classifier within a project |
| `severity:<level>` | `low\|medium\|high\|critical` | Severity for bug-patterns |
| `stack:<slug>` | any string | Technology stack (e.g. `stack:python`) |

Unknown prefixes not listed here are **rejected** in strict mode.

---

## Enforcement

The server enforces this contract when `strict_tag_contract=true` (default
for new installs):

- Missing any required tag → `TagContractError`, write rejected.
- Multiple `project:` or `agent:` tags → `TagContractError` (always ambiguous).
- Invalid `mnemos:<subtype>` (not in whitelist) → `TagContractError`.
- Malformed slug (bad characters) → `TagContractError`.

In lax mode (`strict_tag_contract=false`, for migrations), missing required
tags emit a warning but do not raise. Multiple `project:` / `agent:` tags
always raise.

---

## Example

```text
mnemos_add(
  content="FTS5 query planner mishandles leading wildcards on large tables. Use trailing wildcards only.",
  tags=[
    "project:mnemos",
    "agent:tech-lead",
    "mnemos:bug-pattern",
    "severity:medium",
    "stack:sqlite"
  ],
  title="fts5-leading-wildcard-planner-issue"
)
```

---

## Discipline

- **Never omit required tags.** If you do not know the project or agent,
  determine it before calling `mnemos_add`. Do not guess.
- **Do not invent new `mnemos:` subtypes.** If you need a category that does not
  exist, propose it via PR to the tag contract — do not use an ad-hoc value.
- **One `project:` per entry.** If a learning spans projects, write one entry
  per project, or use `project:shared` if it is genuinely cross-project.
- **`agent:user` for user-authored content.** When the user provides a fact
  or decision directly, tag it `agent:user` — do not attribute it to the
  agent that happened to be running.

---

## See also

- `mnemos-session-lifecycle.instructions.md` — when to recall and checkpoint
- `mnemos-memory-ops.instructions.md` — when to search and add
- `canon-records.instructions.md` — canon record standards (envelope + body sections)
- Skill `mnemos-tag-contract` — full tag schema reference
- [Tag Contract (user docs)](../../docs/en/user/tag-contract.md)
