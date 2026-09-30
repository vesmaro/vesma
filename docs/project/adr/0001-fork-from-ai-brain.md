# 0001. Fork ai-brain into a standalone Vesma product

*Historical artifact — English only.*

- **Status**: Accepted
- **Date**: 2026-05-15
- **Deciders**: abyss (project owner), GCW Chief of Staff

## Context

The user had been running `ai-brain` — a personal FastAPI+Typer+MCP memory server — for the
GCW agent family. As the project grew (per-agent recall, knowledge pipeline, policy engine,
context filter), two structural problems emerged:

1. The original `ai-brain` was a personal scratch project, not a public artefact. Naming
   (`brain`, `brain_*`) was opaque to anyone outside the user's local environment.
2. The GCW team needs a stable contract — `vesma_*` tools, GCW tag schema — that other
   agents in the family can hard-code against. A scratch project does not provide that
   stability.

Vesma is the result. It is a **fork**, not a wrapper: Vesma owns its schema, its
storage, its MCP surface. The upstream `ai-brain` is preserved as a read-only reference.

## Decision

We will:

1. Fork `ai-brain` at commit `95904e6` (v0.3.0, the last autonomous-watcher release).
2. Preserve full git history via `git clone` + rename of the original remote to
   `upstream-ai-brain` (read-only).
3. Mass-rename the Python package `ai_brain` → `vesma`, the CLI entry `brain` → `vesma`,
   every MCP tool `brain_*` → `vesma_*`, every env var `AI_BRAIN_*` → `VESMA_*`.
4. Update default paths (`~/.ai-brain/` → `~/.mnemos/`, `~/brain-vault/` → `~/mnemos-vault/`).
5. Add the Vesma tag contract (`project:*`, `agent:*`, `vesma:*`) as the schema for
   `mnemos_add`, with a `strict_tag_contract` flag for migration.
6. After the rename, treat `ai-brain` as a frozen, archived project with a DEPRECATED
   notice in its README.

## Consequences

**Positive**

- Single source of truth for the GCW memory contract.
- Clean break: legacy `brain_*` clients do not need runtime compatibility.
- Full git history preserved (attribution + cherry-pick path).
- Future schema changes can land in Vesma without affecting the archived `ai-brain`.

**Negative**

- The `ai-brain` codebase is duplicated on disk for the migration period.
- Users with existing `ai-brain` vaults must run `vesma migrate-from-ai-brain` once
  (covered by M13).
- A `strict_tag_contract=true` default means existing `ai-brain` records without
  `agent:` tags are rejected unless migration patches them with `agent:unknown`.

**Neutral**

- Two remote Git refs exist (`origin` = Vesma, `upstream-ai-brain` = original).
- The Obsidian vault layout stays compatible — same directory structure, same frontmatter
  schema (with new pipeline fields).

## Alternatives considered

- **Keep `ai-brain` name, add Vesma as a sibling project.** Rejected: the GCW team would
  still see the old `brain_*` MCP tools and could not rely on the new contract.
- **Wrapper around `ai-brain`.** Rejected: a wrapper hides schema drift, ownership, and
  forces every change to ship in two places.
- **Import `ai_brain` as a module, re-export under `vesma` namespace.** Rejected: leaves
  the user-visible naming inconsistent and prevents Pythonic type-checking across the
  boundary.

## References

- `PLAN.md` §"Phase M1 — Fork & Rebrand"
- `ARCHITECTURE.md` §1
- `CHANGELOG.md` 0.1.0 — M1 entry
- `ai-brain/README.md` — DEPRECATED notice (M14)
