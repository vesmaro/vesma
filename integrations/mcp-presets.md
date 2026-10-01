# Connect Mnemos to any harness — one-line MCP presets

**🌐 Language / Язык:** English · [Русский](../docs/ru/user/integration-guide.md#однострочные-mcp-пресеты)

Every MCP-capable harness connects to Mnemos over the same stdio wire
(ADR-0017 D1): command `mnemos`, args `["mcp-server"]`. Below — one line
(or one paste block) per harness. Pick yours and you are done.

**Prerequisite — install Mnemos (one command):**

```bash
pip install mnemos-memory-server
```

> The MCP SDK is a core dependency (ADR-0023) — nothing extra to install.
> Isolated variant (installs the `mnemos` CLI on `PATH` without touching your
> project environments): `uv tool install mnemos-memory-server` or
> `pipx install mnemos-memory-server`.

> ⚠️ The PyPI name is **`mnemos-memory-server`**. `pip install mnemos` installs
> an unrelated project that owns the `mnemos` name on PyPI.

No environment variables are required: the server defaults to
`~/.mnemos/data` (store) and `~/.mnemos/vault` (Obsidian mirror) and creates
both on first run. See [Tuning](#tuning) for custom locations.

---

## Which connection path for my harness?

Three levels — pick the strongest one your harness supports:

| Level | Harnesses | How |
|-------|-----------|-----|
| **1 · Native target** | VS Code Copilot, Cursor, Claude Code, Codex, Windsurf, Hermes Agent, ZCode, pi, any `~/.agents`-standard tool | `mnemos integration setup --target <name>` — deploys the skill pack *and* registers the MCP server in one pass |
| **2 · One-line preset** | OpenCode, VS Code (or any Level-1 harness without the CLI installed) | paste one block from this page |
| **3 · Adapter template** | anything else that speaks MCP stdio | [adapter-template.md](adapter-template.md) — Connect / Expose / Configure |

The full behavioral-deployment mechanics (instructions, skills, prompt mode,
agent wiring) live in the
[integration guide](../docs/en/user/integration-guide.md).

---

## Cursor

Native target (deploys the pack and registers MCP in one pass):
`mnemos integration setup --target cursor`. Manual path below.

Config file: `~/.cursor/mcp.json`. Paste this one line inside the `mcpServers`
object (create the file if it is your first server):

```json
"mnemos": { "type": "stdio", "command": "mnemos", "args": ["mcp-server"] }
```

Fresh setup — create the whole file in one shell line (overwrites an
existing `mcp.json`; otherwise paste the line above into its `mcpServers`):

```bash
echo '{"mcpServers":{"mnemos":{"type":"stdio","command":"mnemos","args":["mcp-server"]}}}' > ~/.cursor/mcp.json
```

Then restart Cursor (or reload the window). The `mnemos_*` tools appear in the
tools list.

## Claude Code

Native target (deploys the always-on block into `~/.claude/CLAUDE.md` and
merges the server into `~/.claude.json` at user scope — preserving Claude
Code's own state keys): `mnemos integration setup --target claude-code`.
Manual path below.

One shell line (registers at user scope — available in every project):

```bash
claude mcp add --scope user mnemos -- mnemos mcp-server
```

Equivalent manual config — `~/.claude.json`, top-level `mcpServers`:

```json
{
  "mcpServers": {
    "mnemos": { "type": "stdio", "command": "mnemos", "args": ["mcp-server"] }
  }
}
```

Verify with `claude mcp list`. Restart running sessions to pick the server up.

## Codex

Native target (injects the always-on block into `~/.codex/AGENTS.md` and
merges the `[mcp_servers.vesma]` table into `config.toml`, byte-preserving
everything else): `mnemos integration setup --target codex`. Manual path
below.

Config file: `~/.codex/config.toml`. Paste this block (note the underscore
key — `mcp_servers`, not `mcp.servers`):

```toml
[mcp_servers.mnemos]
command = "mnemos"
args = ["mcp-server"]
```

One shell line for a fresh setup:

```bash
mkdir -p ~/.codex && printf '\n[mcp_servers.mnemos]\ncommand = "mnemos"\nargs = ["mcp-server"]\n' >> ~/.codex/config.toml
```

## Windsurf

Native target (MCP-only — Windsurf's built-in `memories/` directory and its
UI-managed workspace rules are never touched):
`mnemos integration setup --target windsurf`. Manual path below.

Config file: `~/.codeium/windsurf/mcp_config.json` (reachable from the Cascade
toolbar: hammer icon → Configure). Paste inside `mcpServers`:

```json
"mnemos": { "type": "stdio", "command": "mnemos", "args": ["mcp-server"] }
```

Fresh setup — create the whole file in one shell line (overwrites an
existing `mcp_config.json`; otherwise paste the line above into `mcpServers`):

```bash
mkdir -p ~/.codeium/windsurf && echo '{"mcpServers":{"mnemos":{"type":"stdio","command":"mnemos","args":["mcp-server"]}}}' > ~/.codeium/windsurf/mcp_config.json
```

## OpenCode

Config file: `~/.config/opencode/opencode.json` (global) or `opencode.json`
in the project root. Paste inside the top-level `mcp` object (note the
`"local"` type and the command **array** — OpenCode's own dialect):

```json
"mnemos": { "type": "local", "command": ["mnemos", "mcp-server"] }
```

Fresh setup — create the whole file in one shell line (overwrites an existing
config; otherwise paste the line above into `mcp`):

```bash
mkdir -p ~/.config/opencode && echo '{"$schema":"https://opencode.ai/config.json","mcp":{"mnemos":{"type":"local","command":["mnemos","mcp-server"]}}}' > ~/.config/opencode/opencode.json
```

Restart OpenCode — the `mnemos_*` tools appear in the tools list.

## Pi

[Pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
(npm `@earendil-works/pi-coding-agent`) has **no built-in MCP client by
design** — tools arrive via TypeScript extensions, so there is no JSON
config to paste. Mnemos ships a bridge extension that spawns the server over
stdio (the same `mnemos mcp-server` wire) and registers every `mnemos_*`
tool as a native Pi tool:

```bash
mnemos integration setup --target pi
```

That deploys:

- `~/.pi/agent/extensions/vesma-mcp.ts` — the MCP bridge (this IS the
  registration; Pi loads extensions from that directory automatically)
- `~/.pi/agent/skills/<name>/SKILL.md` — the skill pack, nested layout

Restart Pi (or run `/reload` inside a session) and the `mnemos_*` tools
appear; `/vesma` reconnects the bridge on demand (legacy alias: `/mnemos`).
Manual fallback — copy
`integrations/extensions/vesma-mcp.ts` from the repo into
`~/.pi/agent/extensions/`. Override the server binary with the
`MNEMOS_BIN` environment variable when `mnemos` is not on `PATH`.

Note: Pi also reads `~/.agents/skills/`; when both the `pi` and `agents`
targets are deployed, prefer `--target pi` to avoid duplicate skill
listings.

## VS Code Copilot

Scripted path — merges into user- or workspace-scope `mcp.json` safely, never
overwrites your other servers:

```bash
curl -fsSL https://raw.githubusercontent.com/vesmaro/vesmaro/main/scripts/mcp-setup.sh | bash
```

Then **reload the VS Code window** (`Ctrl+Shift+P → Reload Window`).

Manual path — add to VS Code **User** `mcp.json` (`~/.config/Code/User/mcp.json`
on Linux/macOS) or the workspace `.vscode/mcp.json`:

```jsonc
{
  "servers": {
    "mnemos": { "type": "stdio", "command": "mnemos", "args": ["mcp-server"] }
  }
}
```

The `mnemos_*` tools appear in the Copilot Chat tools picker.

## ZCode · Claude Code · Codex · Cursor — the `~/.agents` standard

One install covers every harness that reads the standard locations:

```bash
mnemos integration setup --target agents   # skills + MCP for the ~/.agents standard
mnemos integration setup --target zcode    # ZCode-native skills + MCP config
```

`agents` deploys the skill pack to `~/.agents/skills/<name>/SKILL.md` and
registers the MCP server in `~/.agents/mcp.json` (additive merge — existing
servers survive). The `zcode` target does the same for `~/.zcode/`.

## Hermes Agent

Hermes does not use the stdio preset — it embeds Mnemos **in-process** through
a native `MemoryProvider` plugin (no server process at all):

```bash
pip install mnemos-memory-server
mnemos integration setup --target hermes
```

Full walkthrough: [Hermes section of the integration
guide](../docs/en/user/integration-guide.md#hermes-agent).

---

## Tuning

Optional environment variables on the server entry (defaults shown). The
names are the canonical `pydantic-settings` form (`MNEMOS_` prefix +
`MNEMOS` section + `__` + field). The shorter variants that
`scripts/mcp-setup.sh` writes (`MNEMOS_DATA_DIR`, `MNEMOS_VAULT__VAULT_PATH`)
work again as compatibility aliases since the #139 fix; the
canonical names remain the documented form and win when both are set:

| Variable | Default | Purpose |
|---|---|---|
| `VESMARO_MNEMOS__DATA_DIR` | `~/.mnemos/data` | SQLite store location |
| `VESMARO_MNEMOS__VAULT_PATH` | `~/.mnemos/vault` | Obsidian vault mirror |

Example — Claude Code with an explicit store path (expanded by your shell):

```bash
claude mcp add --scope user mnemos \
  --env VESMARO_MNEMOS__DATA_DIR="$HOME/.mnemos/data" \
  --env VESMARO_MNEMOS__VAULT_PATH="$HOME/.mnemos/vault" \
  -- mnemos mcp-server
```

No secrets belong in these entries — Mnemos on loopback needs no API key.

## Verify the connection

Probe the wire directly (harness-independent — talks stdio):

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0.0.0"}}}\n' | mnemos mcp-server
```

A JSON-RPC reply with `"serverInfo":{"name":"mnemos"...}` means the server
answers. Then ask your agent: *“use mnemos_add to save a memory”* — a valid
roundtrip needs the [tag contract](../docs/en/user/tag-contract.md):
one `project:<slug>`, one `agent:<slug>`, at least one `mnemos:<subtype>`.

For any harness not listed here, copy the
[adapter template](adapter-template.md) (Connect / Expose / Configure +
acceptance checklist).
