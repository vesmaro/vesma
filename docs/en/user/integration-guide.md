<!-- mnemos-integration: v2.0.0 -->
# Integration Guide

**🌐 Language / Язык:** English · [Русский](../../ru/user/integration-guide.md)

The Vesma integration layer is a set of **behavioral triggers** that make
agents actually *use* the memory tools, not just have them available. Without
these triggers, agents forget to recall at session start, skip checkpoints
before compaction, and omit required tags.

---

## What is the integration layer?

Three surfaces, each with a different strength:

| Surface | What it is | How it works | Example |
|---------|------------|--------------|---------|
| **Instructions** | `*.instructions.md` with `applyTo: '**'` | Passive rules — loaded into every agent's context unconditionally. State WHEN and HOW. | "Recall at session start, before reading files" |
| **Skills** | `SKILL.md` files | Workflow guides — step-by-step procedures loaded on-demand. | "How to recall effectively: narrow → broaden" |
| **Prompt mode** | `*.prompt.md` | Active mode — a stronger contract that reshapes the agent's behavior for memory-heavy work. | `mnemos-memory` mode with mandatory recall + checkpoint |

### Instructions vs skills vs prompts

- **Instructions** are always-on rules. They say *when* to act. Every agent
  with `vesma/*` tools gets them.
- **Skills** are on-demand workflows. They say *how* to act. The agent loads
  them when it needs the procedure.
- **Prompt mode** is an opt-in contract. It says *you are now a memory agent*.
  Use it for sessions where memory continuity is critical.

---

## What's in the package

```text
integrations/
├── instructions/
│   ├── vesma-memory-ops.instructions.md           # memory-operations canon: gates G1–G4, ops, tag contract, degradation
│   └── canon-records.instructions.md              # canon record standards (envelope + sections)
├── agents_md/
│   └── vesma-always-on.md                         # always-on behavioral block (G1–G4) injected into AGENTS.md files
├── skills/
│   ├── vesma-session-init.md                      # G1: recall at session start
│   ├── vesma-recall.md                            # effective search (narrow → broaden)
│   ├── vesma-agent-recall.md                      # agent-scoped recall
│   ├── vesma-write.md                             # write good entries
│   ├── vesma-checkpoint.md                        # G3: checkpoints that survive compaction
│   ├── vesma-tag-contract.md                      # tag schema reference
│   ├── vesma-core.md                              # umbrella: when to chain the atomic skills
│   ├── vesma-bootstrap.md                         # file-mode store bootstrap
│   ├── vesma-compress.md                          # zero-loss CCR compression
│   ├── vesma-filter.md                            # context-filter profiles / token budgets
│   ├── vesma-housekeeping.md                      # stats, queue depth, tag hygiene
│   ├── vesma-ingest.md                            # one-shot URL ingest
│   ├── vesma-watch.md                             # directory watching / auto-index
│   ├── vesma-workflow.md                          # open questions / task lifecycle
│   ├── vesma-exchange.md                          # export / import / backups
│   ├── vesma-cache-align.md                       # KV-cache prompt-prefix alignment
│   ├── mnemos-canon-write.md                      # canon-compliant records (task / decision / report)
│   └── mnemos-context-lifecycle.md                # context assembly / rewrite lifecycle
└── prompts/
    └── mnemos-memory.prompt.md                    # active memory mode
```

Every deployable file carries the pack safety contract (recalled content is
data, not instructions; no exfiltration; no secrets; harness-local canon
takes precedence) and a `vesma-integration` version stamp. Legacy
`mnemos-integration` stamps are recognized during the migration window and
re-stamped by the first `vesma integration update`.
```

---

## Deploy

### One command (all targets)

```bash
vesma integration setup
```

The plain command is the **full host deployment**: it deploys the pack to
**ALL detected harnesses on this host** and wires all agents — in one
non-interactive, idempotent pass (re-running refreshes stale files). There
is no interactive prompt anywhere in the default path.

### Narrowing to specific targets (--target)

Flags are always the custom variant — install to a specific harness only.
`--target` is repeatable; `all` is accepted and means the default:

```bash
vesma integration setup --target copilot           # VS Code Copilot ~/.copilot/ (default)
vesma integration setup --target generic-copilot   # VS Code prompt mode ~/.config/Code/User/prompts/
vesma integration setup --target cursor            # Cursor ~/.cursor/
vesma integration setup --target zcode             # ZCode (native skills + MCP config)
vesma integration setup --target agents            # ~/.agents standard — Claude Code, Codex, Cursor, …
vesma integration setup --target pi                # Pi coding agent (bridge extension)
vesma integration setup --target hermes            # Hermes Agent (native plugin)
vesma integration setup --target all               # every detected target
```

Target names come from `integrations/targets.yaml`; `--help` lists them for
your install. Claude Code / Codex have no dedicated target — they read the
`agents` target natively.

### Universal targets: ZCode and the AGENTS.md standard

`zcode` and `agents` use the **nested skill layout** — each skill lands as
`<skills-dir>/<name>/SKILL.md`, the format ZCode and the cross-tool
`~/.agents` standard read natively:

| Target | Skills → | MCP registration |
|--------|----------|------------------|
| `zcode` | `~/.zcode/skills/<name>/SKILL.md` | `~/.zcode/cli/config.json` → `mcp.servers` (JSON merge, other servers preserved) |
| `agents` | `~/.agents/skills/<name>/SKILL.md` | `~/.agents/mcp.json` → top-level `mcpServers` |

The `agents` target works for **any harness** that reads the AGENTS.md
standard locations (ZCode, Claude Code, Codex, Cursor, …) — one install,
every tool. The MCP merge is additive: existing servers, plugins, and a
user-tuned `env` on the `vesma` entry are never overwritten.

### Pi coding agent

The `pi` target covers the [Pi coding agent](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
(npm `@earendil-works/pi-coding-agent`). Pi has no built-in MCP client —
tools arrive via TypeScript extensions — so the MCP registration IS a file:
the shipped bridge `integrations/extensions/mnemos-mcp.ts` is deployed
(stamped) into `~/.pi/agent/extensions/`, where Pi auto-loads it. On session
start the bridge spawns `vesma mcp-server` over stdio and registers every
`vesma_*` tool natively; `/reload` hot-reloads, `/vesma` reconnects.

```bash
vesma integration setup --target pi
```

Skills deploy in the nested layout Pi reads natively
(`~/.pi/agent/skills/<name>/SKILL.md`). Because Pi also reads
`~/.agents/skills/`, prefer `--target pi` over deploying both to avoid
duplicate skill listings. `vesma integration uninstall --target pi`
removes only the stamped bridge and skills — user extensions are never
touched.

### Cross-environment installs (--home)

Deploy into another environment's home (a container, a dotfiles checkout)
without rewriting targets.yaml:

```bash
vesma integration setup --target zcode \
  --home /var/home/you/.distrobox/other-box/home \
  --vesma-bin /path/to/mnemos-wrapper \
  --no-wire-agents
```

`~` in targets.yaml resolves against `--home`. Pass `--vesma-bin` when the
target environment launches vesma through a wrapper or a different path.

### What gets deployed where

| Target | Instructions → | Skills → | Prompts → |
|--------|----------------|----------|-----------|
| `copilot` | `~/.copilot/instructions/` | `~/.copilot/skills/` | — |
| `generic-copilot` | — | — | `~/.config/Code/User/prompts/` |
| `cursor` | `~/.cursor/rules/` | — | — |
| `hermes` | `~/.hermes/skills/` | `~/.hermes/skills/` (+ plugin in `~/.hermes/plugins/vesma/`) | — |
| `zcode` | — | `~/.zcode/skills/<name>/SKILL.md` | MCP in `~/.zcode/cli/config.json` |
| `agents` | — | `~/.agents/skills/<name>/SKILL.md` | MCP in `~/.agents/mcp.json` |
| `pi` | — | `~/.pi/agent/skills/<name>/SKILL.md` | bridge: `~/.pi/agent/extensions/mnemos-mcp.ts` |

---

## Verify

After deployment, verify that all files landed correctly:

```bash
vesma integration verify
```

Checks:

- All instruction files present with valid frontmatter (`applyTo: '**'`).
- All skill files present with `name:` and `description:`.
- Prompt mode file present with `mode:` and `tools:`.
- Version stamp `<!-- mnemos-integration: v2.0.0 -->` in every file.
- No `ai-brain` references (except the "adapted from" comment in the prompt).

Exit code `0` = all checks passed. Non-zero = missing or malformed files.

---

## Update

When a new version of Vesma ships updated integration content:

```bash
vesma integration update
```

Updates only files that changed. Preserves any local customizations (files
not managed by Vesma are left alone). After update, run `vesma integration verify`.

---

## Uninstall

To remove all Vesma integration files:

```bash
vesma integration uninstall
```

Removes only files deployed by `vesma integration setup`. Local customizations are
preserved. **This is a destructive action** — it deletes files. Confirm when
prompted.

---

## Agent MCP wiring

Deploying instructions and skills tells agents *when* to call memory tools.
**Agent MCP wiring** goes one step further: it adds `vesma/*` to the
`tools:` frontmatter of Copilot agent files (`~/.copilot/agents/*.agent.md`) so
the tools are actually granted to the agent at request time.

Without wiring, an agent may have the behavioural instructions but no
`vesma_*` tools in its frontmatter — the harness won't pass them to the
model. Wiring closes that gap.

### What it does

- Scans `~/.copilot/agents/` for `*.agent.md` files.
- Parses YAML frontmatter and adds `vesma/*` (wildcard) or individual
  `vesma/vesma_*` tool references to the `tools:` array.
- **Only `tools:` is touched** — `model:`, `model_tier:`, `agents:`, and
  other keys are never modified.
- Idempotent — re-running does not duplicate `vesma/*` entries.

### What gets skipped

| Condition | Why |
|-----------|-----|
| Agent already has `vesma/*` or `vesma/vesma_*` in `tools:` | Already wired — no change needed. |
| Agent uses `tool_profile:` instead of `tools:` | Resolved by the Copilot installer (`make install-all`); mutating it would be overwritten on the next install. |
| Agent has no parseable frontmatter | Cannot safely edit — reported as skipped. |

### Usage

Agent wiring is part of the default pass: the plain
`vesma integration setup` wires **all unwired agents** without prompting.
The flags narrow or opt out:

```bash
# Default: wire ALL unwired agents (no prompt, works in CI too)
vesma integration setup

# Wire specific agents by name or filename stem
vesma integration setup --wire-agents --select tech-lead,code-reviewer

# Skip agent wiring entirely
vesma integration setup --no-wire-agents

# Preview what would change without modifying files
vesma integration setup --dry-run
```

| Flag | Description |
|------|-------------|
| *(no flags)* | Wire all unwired agents (the default; `--wire-agents --all` is accepted as a legacy alias and is a no-op) |
| `--wire-agents --select name1,name2` (or just `--select`) | Wire only the named agents (matches `name`, filename stem, or filename) |
| `--no-wire-agents` | Skip agent wiring entirely (explicit opt-out) |
| `--precise` | Use individual `vesma/vesma_*` tool names instead of the `vesma/*` wildcard |
| `--dry-run` | Show what would change without modifying files |

### Wildcard vs precise mode

- **Wildcard** (default): adds a single `vesma/*` entry granting all
  vesma tools. Compact frontmatter, grants everything.
- **Precise** (`--precise`): adds individual `vesma/vesma_*` entries
  (add, search, recall_context, agent_recall, save_context, list_recent,
  list_tags, ingest_url, stats, auto_collect_status). Explicit grant list —
  `watch_*` admin tools are intentionally excluded.

Use precise mode when you want fine-grained control over which tools each
agent gets. Use wildcard mode for convenience when all agents should have
the full vesma toolset.

### Verifying wiring

After wiring, verify the state:

```bash
vesma integration verify
```

The agents section of the verify report shows:

- **Wired** — agents with `vesma/*` or `vesma/vesma_*` in `tools:`.
- **Unwired** — agents without vesma tools (candidates for wiring).
- **Skipped** — agents with `tool_profile:` (managed by the Copilot installer).

`vesma doctor` also includes an agent wiring check (9th check) that
reports the same summary and warns if unwired agents are detected.

---

## Context Filter

The Context Filter is a five-stage pipeline (dedup, noise, extract,
compress, tokens) that strips noise from raw content before it reaches a
model. It runs automatically on every `mnemos_add` when `auto_filter: true`
(the default for new installs).

Key surfaces:

- **Auto-filter on ingest** — `mnemos_add` stores `raw_content` +
  `clean_content` + `filter_stats`. Search and recall return
  `clean_content` when available.
- **`mnemos_filter` MCP tool** — explicit re-filter of an existing memory
  (override profile, set token budget).
- **`vesma filter` CLI** — `vesma filter <id>` for a single memory,
  `vesma filter --all` to backfill unfiltered records.
- **Filter stats in `vesma stats`** — filtered/unfiltered counts, average
  reduction, breakdown by profile.
- **Profiles** — `log | terminal | code | docs | web | default`,
  auto-detected from content heuristics.

For the full guide with stage details, profile table, examples, and
configuration, see [context-filter.md](context-filter.md).

---

## Hooks & SDK for automation

Vesma ships two dedicated surfaces for harness/automation integrations
(ADR-0017 D1 / ADR-0018, mnemos #125 Wave 3):

- **Lifecycle hooks** — the grouped `mnemos_hooks` MCP tool and the REST
  twin `POST /hooks/{action}` with three actions: `pre_llm_call`
  (assemble the context block to inject before a model call — pass
  `context_hint` = what the call is about, and optionally `task` = the
  bare task slug to scope the assembly to one task's entries, ADR-0027
  Phase 0), `on_session_start` (recall
  recent checkpoints), and `post_tool_call` (autocompression: with
  `hooks.auto_compress: true` in the config — or a per-call
  `auto_compress: true` — the tool output is compressed via CCR and the
  marker-headed `compressed_text` is returned to substitute in your
  window). Identity (`session`/`project`/`agent`) is required on every
  hook call. Full reference: [mcp-tools.md → `mnemos_hooks`](mcp-tools.md#mnemos_hooks)
  / [http-api.md → Lifecycle hooks](http-api.md).
- **`MnemosSDK`** (`from mnemos.sdk import MnemosSDK`) — the thin typed
  Python facade over `MemoryManager` for in-process adapters:
  `remember` / `recall` / `forget` / `stats` / `assemble_context` /
  `rewrite`. The domain logic lives in the manager paths (the same
  scans, gates and idempotency the MCP/REST surfaces apply); the two
  channel-boundary duties are the facade's own, mirroring every other
  surfaced channel: `recall` scans every echoed item at issuance
  (content + title, per-item redactions, refuse-mode drop) and
  `remember` validates caller tags against the tag contract before any
  write. Local-first: `MnemosSDK(settings)` builds its own manager,
  `MnemosSDK(manager=…)` reuses yours.

The full adapter documentation for harness integrators is the [Hermes Agent
section below](#hermes-agent) — the reference migration onto the contract.

---

## `vesma integration setup` — default flow

By default, `vesma integration setup` now **prompts for agent wiring**
in the same pass as file deployment and MCP registration. This closes the
gap where instructions were deployed but agents lacked `vesma/*` in
their `tools:` frontmatter.

```bash
vesma integration setup
# → Deploys instructions + skills + prompts
# → Registers the MCP server
# → Prompts: "Wire vesma/* into Copilot agents? [Y/n]"
```

| Flag | Behaviour |
|------|-----------|
| (none, interactive) | Prompts for agent wiring (default) |
| `--wire-agents --all` | Wire all unwired agents without prompting |
| `--wire-agents --select name1,name2` | Wire only the named agents |
| `--no-wire-agents` | Skip agent wiring entirely |
| `--precise` | Use individual `vesma/vesma_*` tool names instead of the wildcard |
| `--dry-run` | Preview what would change without modifying files |

In a non-interactive terminal (CI / pipe), the command defaults to
wiring all unwired agents. See the [Agent MCP wiring](#agent-mcp-wiring)
section above for the full flag reference.

---

## `vesma add --dry-run` — filter preview

Preview how the Context Filter will transform content **before saving**.
Validates the tag contract, runs the five-stage filter pipeline, and
prints stats — without writing anything to the store.

```bash
vesma add "long log output..." --tags "project:vesma,agent:tech-lead,mnemos:trace" --dry-run
```

Output:

```text
[dry-run] Filter preview (no memory saved):
  Input:     320 tokens
  Output:    180 tokens (43.8% reduction)
  Profile:   log (auto-detected)
  Dedup:     2 exact, 0 near-duplicates removed
  Noise:     14 lines cleaned
  Budget:    not set (no truncation)
[dry-run] Memory would be saved with these filter stats.
```

| Field | Meaning |
|-------|---------|
| Input / Output | Estimated token count before and after filtering |
| Profile | Auto-detected content profile (`log`, `terminal`, `code`, `docs`, `web`, `default`) |
| Dedup | Exact and near-duplicate lines removed |
| Noise | ANSI codes, progress bars, timestamps, separators stripped |
| Budget | Token budget if set (truncation); `not set` means no truncation |

> `--dry-run` is not supported with `--url` (content is fetched at ingest
> time). Use it with positional content or `--file`.

---

## `vesma doctor --fix` — auto-fix warnings

`vesma doctor` runs health checks and reports status. With `--fix`, it
**auto-fixes WARN-level checks** — no manual intervention needed for the
common cases.

```bash
vesma doctor          # report only
vesma doctor --fix    # fix warnings, then re-check
vesma doctor --fix --dry-run   # preview what would be fixed
```

| Warning | Auto-fix action |
|---------|-----------------|
| Integration stale | `vesma integration update` — redeploy stale files to current version |
| Agent wiring — unwired agents | `vesma integration setup --wire-agents --all` |
| MCP server not registered | MCP registration via `mcp-setup.sh` |

**FAIL-level checks are not auto-fixable** — they require manual
diagnosis (missing config, broken SQLite DB, missing vault). After
fixes, `doctor` re-runs the affected checks and reports the new status.

Exit codes: `0` = all pass, `1` = one or more failed, `2` = warnings
only.

---

## `vesma logs` — pipeline traces

View the pipeline trace log (the `traces` table) directly from the CLI.
Shows cluster, synthesize, publish, and recall steps with latency, LLM,
cache, and fallback flags.

```bash
vesma logs                       # last 50 traces
vesma logs --task cluster        # only cluster traces
vesma logs --project vesma      # filter by project
vesma logs --limit 100           # more rows
vesma logs --since 2026-06-01    # only traces after this date
vesma logs --follow              # poll for new traces (tail -f)
```

| Flag | Description |
|------|-------------|
| `--task`, `-t` | Filter by task label (`cluster`, `synthesize`, `publish`, `recall`) |
| `--project`, `-p` | Filter by project slug |
| `--limit`, `-l` | Maximum number of traces (default 50) |
| `--since` | Only traces after this ISO date |
| `--follow`, `-f` | Poll for new traces (tail -f style) |
| `--config`, `-c` | Path to config.yaml |

The table columns: Timestamp, Task, Project, Step, Item, Latency, LLM
(called?), Cache (hit?), Fallback (used?). Traces are the audit log for
the knowledge pipeline — see the [Context Filter](context-filter.md) and
[architecture overview](../architecture/overview.md) for the pipeline
stages.

---

## How agents discover the tools

The integration layer assumes the Vesma MCP server is already connected.
The tools (`vesma_*`) appear in the agent's tool list once the MCP server is
registered in the client's MCP configuration. Agent MCP wiring (above)
ensures the `tools:` frontmatter actually grants those tools to each agent.

For VS Code Copilot Chat, see [getting-started.md](getting-started.md#connect-your-harness-mcp)
for MCP server setup. Once connected, the instructions and skills in this
package tell the agent *when* and *how* to call those tools.

### The project graph needs no harness work

The [project graph](project-graph.md) is server-side by design: once the MCP
server is connected, its ten tools are ordinary `vesma_*` tools, and the
recall beacon rides inside the regular `assemble_context` output on its own —
no extra instructions, skills, hooks or config on the harness side. Since
PG-0.5 this extends to indexing itself: a harness needs **nothing** — zero
wrapper scripts, zero scheduled jobs, zero skills. The first agent call
inside a manifest-bearing directory makes the server auto-register and index
the project in the background (the call must carry an `agent` id — the
attribution PG7 requires, which well-behaved harnesses already pass).

---

## One-line MCP presets

For harnesses without a native deploy target, Vesma ships ready-made
one-line MCP presets — the whole connection is a single line (or paste
block) per harness, always the same stdio wire (ADR-0017 D1):
`command "vesma", args ["mcp-server"]`.

| Harness | Config location | Preset |
|---------|-----------------|--------|
| Cursor | `~/.cursor/mcp.json` | `"vesma": { "type": "stdio", "command": "vesma", "args": ["mcp-server"] }` |
| Claude Code | `claude mcp add` | `claude mcp add --scope user vesma -- vesma mcp-server` |
| Codex | `~/.codex/config.toml` | `[mcp_servers.vesma]` TOML block |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | same JSON line as Cursor |
| OpenCode | `~/.config/opencode/opencode.json` | `"vesma": { "type": "local", "command": ["vesma", "mcp-server"] }` inside `mcp` |
| VS Code Copilot | user/workspace `mcp.json` | `mcp-setup.sh` or the `servers` JSON block |
| ZCode / `~/.agents` tools | `vesma integration setup --target zcode` / `--target agents` | scripted, additive merge |

Full copy-paste lines (plus fresh-setup shell one-liners and env tuning):
[`integrations/mcp-presets.md`](../../../integrations/mcp-presets.md).
No environment variables are required — the server defaults to
`~/.mnemos/{data,vault}` and creates both on first run.

## Adapter template

For any harness not covered by a native target or a preset, copy the
published adapter template —
[`integrations/adapter-template.md`](../../../integrations/adapter-template.md):
three sections (**Connect** the MCP wire → **Expose** the `vesma_*` tools →
**Configure** project/agent slugs and the tag contract) plus an acceptance
checklist the template itself passes. If your harness speaks MCP stdio, the
template is the whole integration.

---

## Tag contract

Every `mnemos_add` and `mnemos_ingest_url` call must carry:

- **exactly one** `project:<slug>`
- **exactly one** `agent:<slug>` (or `agent:user`)
- **at least one** `vesma:<subtype>`

See [tag-contract.md](tag-contract.md) for the full schema. The integration
layer reinforces this in three places: the `mnemos-tag-contract` instruction,
the `mnemos-tag-contract` skill, and the `mnemos-memory` prompt mode.

---

## Hermes Agent

Vesma provides a native `MemoryProvider` plugin for [Hermes Agent](https://hermes-agent.nousresearch.com/) by Nous Research. Since the ADR-0017 D1 migration (#125 W5) the plugin runs **in-process on the provider contract**: every memory operation routes through `mnemos.adapters.hermes.HermesMemoryAdapter` — the `MnemosSDK` facade plus the lifecycle hooks (`pre_llm_call` / `on_session_start` / `post_tool_call`) — down to one `MemoryManager`. The legacy bespoke HTTP path (urllib client, TOTP login flow, circuit breaker, auto-publish bypass) is gone.

### Installation

1. Make the `vesma` package importable in the Hermes Python environment:
   ```bash
   pip install vesma   # pre-rebrand spelling: mnemos-memory-server (deprecated)
   ```
   No separate `vesma serve` process is needed anymore.

2. Deploy the integration:
   ```bash
   vesma integration setup --target hermes
   ```
   This copies the plugin to `~/.hermes/plugins/vesma/` and deploys skills/instructions to `~/.hermes/skills/`.

3. Activate via the wizard:
   ```bash
   hermes memory setup
   ```
   Select "vesma" from the provider list and configure the project/agent slugs and store paths.

4. Restart your Hermes session (`/restart` in gateway, or relaunch CLI).

> **One owner per store:** the plugin embeds the memory server — point `data_dir`/`vault_path` at a store no other process writes (SQLite single-writer). To share a store with `vesma serve` or other harnesses, give each its own data dir.

### Tools

The plugin exposes the `vesma_*` tools as native Hermes tools, now backed by the contract verbs (`MnemosSDK.remember` / `recall`, the hooks) instead of raw HTTP. `mnemos_align_prefix` (P1-5 CacheAligner) remains **MCP-only** — the assembly pipeline applies alignment internally, but there is no standalone manager verb.

| Tool | Contract surface |
|------|------------------|
| `mnemos_search` | `MnemosSDK.recall` (issuance-scanned) |
| `mnemos_add` | `MnemosSDK.remember` (tag contract at the channel) |
| `mnemos_recall_context` | checkpoint recall + channel scan |
| `mnemos_save_context` | `MnemosSDK.remember` (`mnemos:checkpoint`) |
| `mnemos_agent_recall` | agent-scoped recall + channel scan |
| `mnemos_list_recent` | `MemoryManager.list_recent` (title-only scan) |
| `mnemos_list_tags` | `MemoryManager.list_tags` |
| `mnemos_stats` | `MnemosSDK.stats` (project slice) |
| `mnemos_auto_collect_status` | in-process call counter (same shape) |
| `mnemos_ingest_url` | `MemoryManager.ingest_url` |
| `mnemos_compress` | `post_tool_call` hook (N2 identity threaded) |
| `mnemos_retrieve` | `MemoryManager.retrieve_content` (agent+session) |
| `mnemos_watch_start` | `MemoryManager.watch_start` |
| `mnemos_watch_stop` | `MemoryManager.watch_stop` |
| `mnemos_watch_status` | `MemoryManager.watch_status` |

### Configuration

Config is stored in `~/.hermes/config.yaml` under `memory.vesma`:

| Key | Default | Description |
|-----|---------|-------------|
| `data_dir` | (empty) | Vesma data dir (empty = vesma default) |
| `vault_path` | (empty) | Obsidian vault path (empty = vesma default) |
| `project` | `hermes` | Default project slug for tag contract |
| `agent` | `hermes-default` | Default agent slug for tag contract |
| `auto_sync` | `true` | Mirror built-in memory writes and sync significant turns |
| `publish_on_write` | `true` | Promote writes to `published` immediately — the LLM-less posture; set `false` when the knowledge pipeline runs |
| `sync_interval` | `10` | Sync every Nth turn |
| `sync_min_user_chars` | `50` | Significance threshold: user-message characters |

**Breaking vs the legacy HTTP plugin:** `base_url` / `api_key` / `totp_secret` are gone — the plugin embeds the server in-process (loopback by construction, ADR-0017 D6; no auth hop to survive).

### Architecture

The plugin implements the Hermes `MemoryProvider` ABC as a thin shim over `HermesMemoryAdapter`:

- **prefetch()** — `pre_llm_call` hook → `assemble_context` (recall → filter → secret scan → align → budget, provenance on every block), run off the turn loop
- **sync_turn()** — `MnemosSDK.remember` (`mnemos:session`) for significant turns (user > 50 chars or every Nth)
- **on_memory_write()** — `MnemosSDK.remember` mirror of MEMORY.md/USER.md writes (`mnemos:learning` / `mnemos:rule`)
- **on_session_end()** — one `mnemos:session` summary per session via `remember`
- **on_pre_compress()** — the ADR-0018 bridge: the to-be-discarded block is reported via `MnemosSDK.rewrite` (`on_context_rewrite`), so the original lands in LTM losslessly
- **Identity threading** — `project`+`agent` fixed at construction (tag-contract-validated up front), `session` bound per Hermes session and threaded onto every verb (incl. the A2 CCR issuer gate and the N2 compress mandate)

Adapter acceptance is pinned in-process by `tests/test_hermes_adapter.py` (the ADR-0017 Phase 1 gate "Hermes e2e on contract").

---

## Versioning

Every file in the integration layer carries a version stamp:

```html
<!-- mnemos-integration: v2.0.0 -->
```

This allows `vesma integration verify` to detect stale files after an update. If
the stamp does not match the installed Vesma version, the file is flagged
for update.
