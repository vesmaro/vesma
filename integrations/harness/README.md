# Vesma harness pack — memory layer for any agent harness

This directory is the **harness-facing canon** for working with the Vesma
memory server: 16 `vesma-*` skills plus one portable instruction
(`vesma-memory-ops`) that any agent harness can adopt — ZCode, VS Code
Copilot, Pi, Claude Code, Codex, or any reader of AGENTS.md-style files.

## Layer-separation contract

> **The memory layer is owned by Vesma, not by any agent framework.**

Anything about *working with the memory server and its MCP tools* — skills,
memory-operations rules, tool-name canon — lives here, in the Vesma repo.
Agent frameworks ship at most a short pointer to this pack; they do not keep
their own copy of the canon. Before this split, the skills and the G1–G4
gates canon lived in the GCW (GithubCopilotWorkflow) repo and were deployed
by its installer; that deployment is retired — the GCW copies are
deprecation stubs pointing here.

## What's inside

| Path | What it is |
|------|------------|
| `skills/vesma-*/SKILL.md` | 16 skills, nested layout (one dir per skill) |
| `instructions/memory-ops.md` | The memory-operations canon: session gates G1–G4, memory ops discipline, tag contract, graceful degradation |
| `install.sh` | Idempotent POSIX installer with per-target checksum manifests |
| `README.md` / `README.ru.md` | This document (EN / RU) |

Skills:

| Skill | Purpose |
|-------|---------|
| `vesma-core` | Umbrella / entry point — when to chain the atomic skills |
| `vesma-session-init` | G1 session start: recall prior context before any work |
| `vesma-recall` | Effective search — start narrow, broaden if no hits |
| `vesma-agent-recall` | Agent-scoped recall — your own prior trail |
| `vesma-write` | Writing good entries — tag contract, quality bar |
| `vesma-checkpoint` | G3 compaction-resilient checkpoints |
| `vesma-tag-contract` | Canonical tag schema (required composition, subtypes) |
| `vesma-bootstrap` | File-mode store bootstrap before the MCP server exists |
| `vesma-compress` | Zero-loss CCR compression of huge tool outputs |
| `vesma-filter` | Context Filter profiles and token budgets |
| `vesma-housekeeping` | Stats, queue depth, tag hygiene, reprocessing |
| `vesma-ingest` | One-shot URL ingest into memory |
| `vesma-watch` | Directory watching and auto-indexing |
| `vesma-workflow` | Open questions / task lifecycle tracking |
| `vesma-exchange` | Export / import, backups, migration, federation payloads |
| `vesma-cache-align` | Prompt-prefix stabilization for provider KV caches |

## Naming contract (read this before editing)

- **Tool names are `vesma_*`** (`vesma_search`, `vesma_add`,
  `vesma_recall_context`, ...). This matches the server's brand-primary
  MCP manifest (`VESMA_MCP_BRAND=vesma`). Legacy `mnemos_*` spellings are
  accepted by server builds before 6.0 — mention them only as explicit
  legacy-compat notes.
- **Tag prefixes are `mnemos:<subtype>`** (`mnemos:decision`,
  `mnemos:learning`, ...). These are the STORAGE DATA CONTRACT — they
  predate the product rebrand and are NOT renamed. Do not "fix" them.
- **Skill names are `vesma-*`**; every file carries the pack stamp
  `<!-- vesma-harness-pack: 1.0.0 -->`. The older
  `mnemos-integration` stamp belongs to the server integration pack (see
  below) and never appears in this pack.

## Install

```sh
./install.sh                            # ~/.zcode/skills + ~/.agents/skills
./install.sh --target /path/to/skills   # any custom skills dir (repeatable)
./install.sh --check                    # verify installed files vs manifests
./install.sh --uninstall                # remove exactly what was installed
./install.sh --force                    # override foreign-file / version guards
```

The installer:

- deploys the 16 skills as `<target>/vesma-<name>/SKILL.md` and the
  instruction as the flat file `<target>/vesma-memory-ops.instructions.md`;
- writes a per-target manifest `.vesma-harness-pack.manifest` (pack
  version, timestamp, sha256 per file) — uninstall and `--check` are
  manifest-driven, so it removes/verifies exactly what it installed;
- refuses to overwrite a file at a pack path that it did not install
  (remove it first, or pass `--force`);
- is idempotent: re-running refreshes the files and the manifest.

No network access, no secrets, nothing outside the target dirs.

## How a harness adopts the pack

1. **Skills dirs** — install into the harness skill directory (nested
   `<name>/SKILL.md` layout is the cross-harness standard). Skill
   descriptions are trigger-rich, so the harness picks them up natively.
2. **Always-on rules** — the G1–G4 gates should be visible to every
   agent, not only when a skill triggers:
   - AGENTS.md-standard harnesses: add 2–3 lines summarizing the gates
     and pointing at `vesma-memory-ops.instructions.md`.
   - VS Code Copilot: copy `instructions/memory-ops.md` into
     `.github/instructions/` (frontmatter `applyTo: '**'` is already
     correct).
3. **MCP registration** — register the Vesma server per your harness's
   MCP config (`vesma` server key, `VESMA_MCP_BRAND=vesma` recommended).
   The `vesma serve`/`vesma mcp-server` docs cover per-harness snippets.
4. First session: the agent's harness shows the `vesma-*` skills and the
   gates canon — from the first second it works the Vesma way.

## Relation to the server integration pack

Vesma ships TWO harness-facing artefact sets, with distinct ownership
stamps:

| | Server integration pack (`integrations/skills`, `integrations/instructions`, `vesma util-setup`) | This harness pack (`integrations/harness`) |
|---|---|---|
| Deployed by | the server CLI at server-setup time (detects the harness via `targets.yaml`) | the user / repo setup via `install.sh`, no server required |
| Contents | flat-file skills incl. `mnemos-canon-write`, `mnemos-context-lifecycle`, record-canonical instruction, canon JSON Schemas | the 16 workflow skills + the memory-ops instruction |
| Stamp | `<!-- mnemos-integration: ... -->` | `<!-- vesma-harness-pack: 1.0.0 -->` |
| Uninstall | `vesma util-setup --uninstall` (inline stamp or byte-identity) | `install.sh --uninstall` (manifest) |

They are complementary, not competing: the server pack teaches the
record canon and deploys schemas; this pack teaches the memory workflow.
Both may live in the same skills directory without ownership conflicts.

## Versioning

Pack version lives in `install.sh` (`PACK_VERSION`) and in every file
stamp. Bump both together; the installer refuses to re-own a target
whose manifest was written by a different pack version without
`--force`.
