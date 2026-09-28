---
name: mnemos-tag-contract
description: Canonical tag schema for Mnemos memory entries — required composition, whitelisted prefixes, subtypes
---

# Mnemos Tag Contract (skill reference)

All memory entries — via `mnemos_add` or `mnemos_ingest_url` — must use this
tag vocabulary. Stability of these names matters: migration and search depend
on it.

## WHEN

- **Before every `mnemos_add` call** — validate the tag set.
- **Before every `mnemos_ingest_url` call** — same requirement.
- **When reviewing a migration** — check that legacy entries have valid
  tags or `mnemos:legacy`.

## Required tags (mandatory on all new entries)

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

## Optional tags (accepted, not required)

| Tag | Format | Purpose |
|-----|--------|---------|
| `source:<slug>` | any string | Origin of the entry (chat, file, url, …) |
| `applyTo:<glob>` | file glob | Scope a `mnemos:rule` to specific file paths |
| `milestone:<id>` | any string | Links entry to a project milestone |
| `domain:<slug>` | any string | Domain sub-classifier within a project |
| `severity:<level>` | `low\|medium\|high\|critical` | Severity for bug-patterns |
| `stack:<slug>` | any string | Technology stack (e.g. `stack:python`) |

Unknown prefixes not listed here are **rejected** in strict mode.

## Enforcement modes

| Mode | Setting | Behaviour |
|------|---------|-----------|
| **Strict** (default) | `strict_tag_contract=true` | Missing/malformed required tags → `TagContractError`, write rejected. |
| **Lax** (migrations) | `strict_tag_contract=false` | Missing required tags → warning, write succeeds. Multiple `project:`/`agent:` always raise. |

## STEPS

1. **Identify the project** — the codebase or initiative this entry belongs
   to. If unknown, determine it before calling `mnemos_add`.

2. **Identify the agent** — the agent slug that authored this entry. Use
   `agent:user` for user-provided content.

3. **Choose the subtype** — pick exactly one `mnemos:<subtype>` from the
   whitelist. If none fits, do not invent one — propose a new subtype via
   PR.

4. **Add optional tags** as needed — `severity:` for bug-patterns,
   `applyTo:` for rules, `stack:` for stack-specific learnings.

5. **Assemble and write**:

   ```text
   mnemos_add(
     content=<body>,
     tags=[
       "project:<slug>",
       "agent:<slug>",
       "mnemos:<subtype>",
       "<optional>:<value>"
     ]
   )
   ```

## DISCIPLINE

- **Never omit required tags.** If you do not know the project or agent,
  determine it before writing. Do not guess.
- **Do not invent new `mnemos:` subtypes.** Propose additions via PR to the tag
  contract.
- **One `project:` per entry.** If a learning spans projects, write one
  entry per project, or use `project:shared` if genuinely cross-project.
- **`agent:user` for user-authored content.** Do not attribute user-provided
  facts to the agent that happened to be running.
- **Slugs are lowercase.** `[a-z0-9][a-z0-9\-_]*` — no uppercase, no spaces.

## See also

- Instruction `mnemos-tag-contract.instructions.md`
- Skill `mnemos-canon-write` — canon-compliant records (task / decision / report)
- [Tag Contract (user docs)](../../docs/en/user/tag-contract.md)
