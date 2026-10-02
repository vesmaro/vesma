<!-- vesma-adapter-template: v1 -->
# Vesma adapter template — Connect / Expose / Configure

Copy this template to wire **any** MCP-capable agent harness to Vesma
(ADR-0017 D1: MCP stdio is the wire — no proprietary protocol). It is
harness-agnostic: wherever your tool reads server configs from, the entry
below is the whole contract. Target size: one screen; keep it that way.

Prerequisite: `vesma` on `PATH` (one-line install):
`curl -fsSL https://raw.githubusercontent.com/vesmaro/vesmaro/main/scripts/install.sh | bash`

## 1 · Connect — point your harness at the server

Register exactly this stdio server entry wherever your harness reads MCP
configs. Common locations:

| Harness | Config location | Shape |
|---|---|---|
| Cursor | `~/.cursor/mcp.json` | JSON, `mcpServers` map |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | JSON, `mcpServers` map |
| Claude Code | `claude mcp add` / `~/.claude.json` | JSON, `mcpServers` map |
| Codex | `~/.codex/config.toml` | TOML, `[mcp_servers.<name>]` |
| VS Code | `mcp.json` (user / workspace scope) | JSON, `servers` map |

The entry itself (JSON form for `mcpServers` maps; TOML form below for Codex):

```json
"vesma": { "type": "stdio", "command": "vesma", "args": ["mcp-server"] }
```

```toml
[mcp_servers.vesma]
command = "vesma"
args = ["mcp-server"]
```

Ready-made one-liners for major harnesses: [mcp-presets.md](mcp-presets.md).
No env vars are required; optional `VESMA_MNEMOS__DATA_DIR` /
`VESMA_MNEMOS__VAULT_PATH` tune store locations — the shorter
`VESMA_DATA_DIR` / `VESMA_VAULT__VAULT_PATH` forms work as #139
compatibility aliases, canonical names preferred
(loopback needs no API key — never put secrets in the entry).

## 2 · Expose — grant the tools to the agent

Connection is not exposure: most harnesses hide MCP tools until granted.
Add the `vesma_*` tools to the agent's tool list / allow-list / `tools:`
frontmatter. Minimum read-write core:

| Tool | Purpose |
|---|---|
| `vesma_search` | hybrid FTS5 + vector search |
| `vesma_add` | write a memory (tag contract enforced) |
| `vesma_recall_context` | relevance-assembled context block |
| `vesma_agent_recall` | per-agent recall at session start |
| `vesma_save_context` | checkpoint before compaction / session end |
| `vesma_stats` | store health at a glance |

Full surface (26 tools): [mcp-tools.md](../docs/en/user/mcp-tools.md).

Then give the agent one behavioural rule (adapt the wording to your
harness's instruction channel — system prompt, rules file, AGENTS.md):

```text
At session start call vesma_agent_recall before reading files.
Before writing any memory, compose tags: exactly one project:<slug>,
exactly one agent:<slug>, at least one mnemos:<subtype>.
Before compaction or session end, call vesma_save_context.
```

## 3 · Configure — slugs and the tag contract

Pick two slugs once and reuse them everywhere (keeps recall scoped):

- `project:<slug>` — the repo/product this harness works on (e.g. `project:vesma`)
- `agent:<slug>` — this harness's identity (e.g. `agent:cursor`, or `agent:user`)

Every write must carry exactly one of each plus at least one
`mnemos:<subtype>` (`decision`, `rule`, `session`, `checkpoint`, `learning`, …).
Full schema: [tag-contract.md](../docs/en/user/tag-contract.md). The server
rejects contract-breaking writes, so a failed `vesma_add` means bad tags,
not a broken connection.

## Acceptance checklist

Run through this list after wiring; every item must pass. Item 5 uses this
wire probe (works for every harness — it talks stdio directly):

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0.0.0"}}}\n' | vesma mcp-server
```

- [ ] The Connect entry is pasted verbatim: `command = "vesma"`, `args = ["mcp-server"]`, stdio.
- [ ] The harness restarted and lists the `vesma` server as connected/healthy.
- [ ] `vesma_*` tools from the Expose table are visible to the agent.
- [ ] `vesma_agent_recall` returns (possibly empty) results at session start.
- [ ] The wire probe above replies with a JSON-RPC result whose `serverInfo.name` is `vesma`.
- [ ] A test write roundtrips: `vesma_add` with `project:test,agent:<slug>,mnemos:learning`,
      then `vesma_search "test"` finds it.
- [ ] A write missing `project:` is rejected — the tag contract is active.
- [ ] `vesma doctor` (in a shell) reports no FAIL-level checks.
- [ ] The behavioural rule from Expose is present in the agent's instruction channel.

When all boxes tick, your harness is on the Vesma wire. Drift between this
template and the repo is guarded by `tests/test_mcp_presets.py`.
