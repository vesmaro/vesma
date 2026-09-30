# Getting Started

**🌐 Language / Язык:** English · [Русский](../../ru/user/getting-started.md)

> Complete first-run guide for Vesma — from a one-line install to your first memory, first search, and a connected agent harness.

Vesma is on PyPI — no cloning, no building, no venv knowledge required. This page walks you through the whole first run. Every command is runnable on a clean Linux / macOS / WSL2 box.

For higher-level context, see [architecture overview](../architecture/overview.md). For every CLI subcommand, see [cli-reference.md](cli-reference.md). For every MCP tool, see [mcp-tools.md](mcp-tools.md). For every HTTP endpoint, see [http-api.md](http-api.md).

---

## Install

Vesma ships on PyPI as **`vesma`** (the bare slot is ours as of the rebrand). Pick the line that matches how you will use it:

| You want… | Install with | You get |
|-----------|--------------|---------|
| **Everything** — the usual case: the server plus the MCP surface your agent harness talks to | `pip install vesma` | server + `vesma` CLI + REST API + the MCP server |
| The `vesma` command on `PATH`, project environments untouched | `uv tool install vesma` — or `pipx install vesma` | same as above, isolated |
| External LLM enrichment as well | `pip install "vesma[ollama]"` — also `openai`, `anthropic`, `gemini` | + the chosen provider SDK |

> **One package, nothing extra.** Since 4.1.0 the MCP SDK is a core dependency (ADR-0023) — the base
> install serves agent harnesses out of the box, and the legacy `[mcp]` extra survives as an empty
> no-op alias so older commands and snippets keep resolving. The `mnema-embed-v1` embedding model
> (~30 MB) is bundled inside the wheel: search works fully offline, on CPU, with no downloads and
> no API keys.

> ⚠️ **Names.** The product and CLI are `vesma` (`pip install vesma`). The pre-rebrand package
> `mnemos-memory-server` remains live until deprecation and installs the same server
> (`pip install "mnemos-memory-server[ollama]"` keeps working across the dual-period). The bare
> `pip install vesma` is an unrelated third-party project — do not use it.

### Scripted variant (zero decisions)

The installer creates an isolated venv at `~/.mnemos/venv`, drops a `vesma` launcher into `~/.local/bin`, and offers to wire VS Code MCP and deploy the integration pack right in the same run:

```bash
curl -fsSL https://raw.githubusercontent.com/vesmaro/vesmaro/main/scripts/install.sh | bash
```

### Pinning and other channels

| Method | Command |
|--------|---------|
| Pin a version | `pip install vesma==4.3.0` (*pre-rebrand pin: `mnemos-memory-server==4.1.0` stays installable until deprecation*) |
| Container one-liner | `… install.sh \| bash -s -- --container` — see [container-deployment.md](../admin/runbooks/container-deployment.md) |
| From source (contributors) | `git clone https://github.com/vesmaro/vesmaro && cd vesmaro && uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"` — see [CONTRIBUTING.md](../../../CONTRIBUTING.md) |

<details>
<summary><strong>Released wheel and pre-built container image</strong> — version-pinned channels</summary>

**Released wheel** (pin a specific version):

<!-- version:pip -->
```bash
pip install https://github.com/vesmaro/vesmaro/releases/download/v4.3.0/mnemos_memory_server-4.3.0-py3-none-any.whl
```
<!-- /version:pip -->

<!-- deprecated-note: 5.x releases ship as the `vesma` wheel (bare PyPI slot); the legacy
mnemos_memory_server-*.whl artifact name covers the 4.x line until deprecation. -->

**Pre-built image** (published at `ghcr.io/vesmaro/vesmaro`; `docker` works too — swap `podman` for `docker`):

```bash
export VESMARO_API__TOTP_MASTER_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
# 4.x images additionally accept the legacy MNEMOS_API__TOTP_MASTER_KEY spelling (deprecated)
podman run -d --name vesma \
  -p 8787:8787 \
  -v vesma-data:/data \
  -v vesma-vault:/vault \
  -e VESMARO_API__TOTP_MASTER_KEY="${VESMARO_API__TOTP_MASTER_KEY}" \
<!-- version:image -->
  ghcr.io/vesmaro/vesmaro:4.3.0
<!-- /version:image -->

curl -s http://localhost:8787/health | jq
```

<!-- version:tags -->
Tags: `:4.3.0` (pinned) · `:latest` (rolling).
<!-- /version:tags -->

Full guide: [container-deployment.md](../admin/runbooks/container-deployment.md).

</details>

<details>
<summary><strong>Optional extras</strong> — external LLM providers, only if you need them</summary>

Vesma calls external LLMs for pipeline synthesis (M4) and enrichment — never for storing or searching. Install only what you need:

```bash
uv pip install "vesma[ollama]"      # local Ollama (default provider)
uv pip install "vesma[openai]"      # OpenAI / Azure OpenAI
uv pip install "vesma[anthropic]"   # Anthropic Claude
uv pip install "vesma[gemini]"      # Google Gemini
```

The default provider is `ollama` pointing at `http://localhost:11434`. See [config.example.yaml](../../../config.example.yaml) for the full provider list.

</details>

### Prerequisites

| Tool | Version | Why |
|------|---------|-----|
| Python | ≥ 3.11 | Runtime floor (`pip` handles it — no manual venv needed) |
| `uv` or `pipx` | latest | Optional, for the isolated tool install |

> **OS notes.** Vesma is developed on Linux (Arch, Fedora, Ubuntu 22.04+) and is regularly smoke-tested on macOS. Windows works through WSL2. The systemd unit in `contrib/systemd/` is Linux-only.

> **Hardware.** The bundled `mnema-embed-v1` runs comfortably on a single CPU core. No GPU. A 2 vCPU / 2 GB VM is enough for personal use.

---

## First memory (CLI)

```bash
vesma add "Hello world" --tags project:test agent:getting-started mnemos:learning
```

Expected output:

```text
✓ Saved: Hello world (550e8400-e29b-41d4-a716-446655440000)
```

Vesma automatically:

1. **Wrote the entry to SQLite** at `~/.mnemos/data/mnemos.db` (created on first run).
2. **Mirrored it to your Obsidian vault** at `~/.mnemos/vault/` as a markdown file with YAML frontmatter.
3. **Validated the tag contract** — `project:test` + `agent:getting-started` + `mnemos:learning` is a valid trio. Skip one and you get `❌ Tag contract violation: ...` instead.

The tag contract is documented in [tag-contract.md](tag-contract.md). The short version: every memory needs **exactly one** `project:<slug>`, **exactly one** `agent:<slug>`, and **at least one** `vesma:<subtype>` (e.g. `mnemos:learning`, `mnemos:bug-pattern`, `mnemos:decision`).

> **Note.** Newly added memories start in the `raw` state. The background processor (running in both MCP and HTTP API modes) automatically clusters, synthesises, quality-gates, and publishes them. The vector search index only includes `published` memories. To rebuild it manually: `vesma reindex` (CLI) or `POST /reindex` (HTTP API).

---

## First search

Hybrid search combines SQLite FTS5 full-text with vector similarity and merges the rankings using Reciprocal Rank Fusion (RRF):

```bash
vesma search "hello"
```

Useful flags:

| Flag | Effect |
|------|--------|
| `--limit N` / `-l N` | Max results (default 10) |
| `--project P` / `-p P` | Restrict to a project slug |

For programmatic access with more options (vector weight, raw content, tag filter), use the HTTP API — see [http-api.md#search](http-api.md#search).

---

## Your codebase can become memory (project graph)

Besides sessions, Vesma can index a project's **code structure**: file
outlines, symbol search, call tracing, secret-scanned snippets — with zero
source bytes stored. This is the [project graph](project-graph.md), on by
default but inert until you point it at a project:

1. Register a root (operator step, Python SDK):
   `mgr.sqlite.save_project(Project(name="myproj", paths=["/abs/path/to/myproj"]))`.
2. Index it: `mnemos_index_project` with `project_id` and `agent`.
3. Check it: `mnemos_project_graph_status` — volumes, freshness, poisoned count.

Don't want the surface at all? One flag turns it off: `code_graph.enabled: false`
in `config.yaml` — every graph call then answers `code: "disabled"`. Full
walkthrough: [project-graph.md](project-graph.md).

---

## Connect your harness (MCP)

The MCP server is the primary integration surface: your agent harness spawns `vesma mcp-server` over stdio and gets the full `vesma_*` tool set. Pick your harness:

> The MCP server is the primary integration surface: your agent harness spawns `vesma mcp-server` over stdio and gets the full `vesma_*` tool set. Pick your harness:

| Harness | Fastest path |
|---------|--------------|
| VS Code Copilot | `curl -fsSL …/scripts/mcp-setup.sh \| bash`, then reload the window |
| Claude Code | `claude mcp add --scope user vesma -- vesma mcp-server` |
| Cursor | paste one line into `~/.cursor/mcp.json` |
| OpenCode | paste one block into `~/.config/opencode/opencode.json` |
| Codex / Windsurf | one TOML / JSON block each |
| ZCode, pi, Hermes Agent | `vesma integration setup --target zcode` / `--target pi` / `--target hermes` |
| Anything else | [adapter-template.md](../../../integrations/adapter-template.md) |

**The full copy-paste instructions for every harness live on one page: [Connect Vesma to any harness](../../../integrations/mcp-presets.md).** The behavioral layer — instructions, skills, and prompt mode that make agents actually *use* the memory — is a separate one-pass step:

```bash
vesma integration setup
```

See the [integration guide](integration-guide.md) for targets and flags.

Manual VS Code reference — user- or workspace-scope `mcp.json`:

```jsonc
{
  "servers": {
    "vesma": {
      "type": "stdio",
      "command": "vesma",
      "args": ["mcp-server"]
    }
  }
}
```

> **Registry-key note.** The `"vesma"` key in MCP config files is the server *registry name* the integration layer reads and manages (`servers["vesma"]`) — it is untouched by the rebrand; the *command* is `vesma mcp-server`.

> **Tip — auto-collect mode.** Set `VESMARO_AUTO_COLLECT=1` ( legacy spelling: `MNEMOS_AUTO_COLLECT`, deprecated) in the server's `env` block to make Vesma nudge your agent to call `mnemos_save_context` every ~6 tool calls. See [mcp-tools.md#auto-collect-mode](mcp-tools.md#auto-collect-mode) for the trade-offs.

---

## Run the HTTP API (optional)

For non-MCP clients, dashboards, and A2A traffic:

```bash
vesma serve --host 127.0.0.1 --port 8787
```

| Endpoint | Purpose |
|----------|---------|
| `http://127.0.0.1:8787/health` | Liveness check |
| `http://127.0.0.1:8787/metrics` | Stats (Prometheus-style) |
| `http://127.0.0.1:8787/docs` | Swagger UI |
| `http://127.0.0.1:8787/v1/sessions` | A2A sessions API (M16) |

> **Security.** The default bind is `127.0.0.1`. Do not expose this port without a reverse proxy with authentication in front — see [security.md](../admin/security.md).

Smoke-test it:

```bash
curl -s http://127.0.0.1:8787/health | jq
# {"status":"ok"}
```

---

## Verify your installation

```bash
vesma doctor
```

runs health checks over the store, config, MCP transport, and known harness registrations — and prints one PASS/WARN/FAIL line per check. `vesma doctor --fix` auto-resolves the common warnings (stale integration files, unwired agents, missing MCP registration).

To run the full development gate (contributors only): clone the repo, `uv pip install -e ".[dev,mcp]"`, then `make verify` — ruff + mypy `--strict` + bandit + pip-audit + the test suite. If `pip-audit` complains about a pinned CVE, see the [dependency-updates runbook](../admin/runbooks/dependency-updates.md).

---

## Migrate from legacy ai-brain

If you have an existing legacy `ai-brain` install (`~/.ai-brain/ai_brain.db` + `~/brain-vault/`), Vesma imports it in one command. Dry-run first:

```bash
vesma migrate from-ai-brain --dry-run
```

Read the summary, then run for real:

```bash
vesma migrate from-ai-brain
```

The migrator translates legacy source types, patches the tag contract (`project:legacy`, `agent:unknown`, `mnemos:legacy`), preserves entry statuses, and migrates the `content_ru` / `content_en` columns into `metadata` (no data loss). Use `--source PATH` and `--vault PATH` for non-default locations.

---

## Configuration

Vesma reads `config.yaml` from the current directory or `~/.mnemos/config.yaml`. See [config.example.yaml](../../../config.example.yaml) for the full schema. The most useful knobs:

| Setting | Default | Purpose |
|---------|---------|---------|
| `mnemos.data_dir` | `~/.mnemos/data` | SQLite store + vector index |
| `mnemos.vault_path` | `~/.mnemos/vault` | Obsidian mirror |
| `mnemos.strict_tag_contract` | `true` | Enforce the tag contract (set `false` only for legacy imports) |
| `embedding.provider` | `nano` | `nano` (mnema-embed-v1, bundled) / `onnx` / `ollama` / `sentence-transformers` |
| `search.hybrid_alpha` | `0.5` | Weight of the vector leg in RRF (0.0 = pure FTS, 1.0 = pure vector). Default re-tuned 0.7 → 0.5: leg balance stops vector dominance from drowning FTS-rank-1 matches (issue #300) |
| `api.host` / `api.port` | `127.0.0.1` / `8787` | `vesma serve` defaults |
| `llm.provider` / `llm.model` | `ollama` / `qwen2.5:3b` | Pipeline synthesis & context filter |

Any of these can be overridden by env vars (`VESMARO_*`, with `__` for nesting; the 4.x `MNEMOS_*` spelling is deprecated):

```bash
VESMARO_SEARCH__HYBRID_ALPHA=0.7 vesma search "deployment"
```

### Logging

Vesma logs to `~/.mnemos/logs/mnemos.log` by default (rotating, 10 MB × 3 files):

```yaml
logging:
  level: INFO                    # DEBUG | INFO | WARNING | ERROR
  log_file: ~/.mnemos/logs/mnemos.log
  max_file_size_mb: 10
  backup_count: 3
```

CLI: `vesma --verbose serve` for DEBUG level, `vesma serve --log-file /path/to/log` to override.

---

## Troubleshooting

### `vesma` command not found

If you installed with plain `pip` into a venv, the venv must be active. Prefer the isolated install (`uv tool` / `pipx` / `install.sh`) — it puts `vesma` on `PATH` in every shell (`~/.local/bin`; add it to `PATH` if your distro does not).

### `vesma mcp-server` fails with an import error about `mcp`

The install is broken, or a foreign `mcp` 1.x SDK shadows the bundled core one:
`pip install --force-reinstall mnemos-memory-server` (the SDK is a core dependency since
4.1.0 — ADR-0023; `vesma doctor` confirms the transport afterwards).

### Search returns only "raw" entries

The vector index only includes `published` memories; new entries start `raw` and are published by the background processor. To publish immediately, set `status: "published"` on creation via the HTTP API, or let the pipeline run.

### `sqlite3.OperationalError: database is locked`

Another `vesma` process (CLI, MCP, or HTTP) holds the write lock. SQLite uses WAL mode but only one writer is allowed at a time. Close the other process, or wait for its transaction to commit (default busy-timeout is 5 s). For multi-harness setups, give each harness its own data dir — see the one-owner-per-store note in the [integration guide](integration-guide.md).

### MCP server runs but no tools appear in the harness

1. Check the harness config parses (valid JSONC / TOML, no trailing commas).
2. Restart the harness after editing its config.
3. Probe the wire directly: `printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0.0.0"}}}\n' | vesma mcp-server` — a JSON-RPC reply with `"serverInfo":{"name":"vesma"...}` means the server side is fine. (The serverInfo name stays `vesma` across the dual-prefix period — it is part of the MCP registration contract.)
4. Run `vesma doctor` — the MCP transport and registration checks point at the broken link.

---

## Where to go next

| If you want to… | Read |
|-----------------|------|
| Connect a specific harness (VS Code, Claude Code, Cursor, OpenCode, Codex, Windsurf, pi, Hermes…) | [Connect Vesma to any harness](../../../integrations/mcp-presets.md) |
| Deploy the behavioral pack (instructions / skills / prompts / agent wiring) | [integration-guide.md](integration-guide.md) |
| See every CLI subcommand | [cli-reference.md](cli-reference.md) |
| See every MCP tool | [mcp-tools.md](mcp-tools.md) |
| See every HTTP endpoint | [http-api.md](http-api.md) |
| Understand the system shape | [architecture overview](../architecture/overview.md) |
| Read the tag schema | [tag-contract.md](tag-contract.md) |
| Run an operational task | [admin/runbooks/install.md](../admin/runbooks/install.md) |
| Review security boundaries | [security.md](../admin/security.md) |
| See why a decision was made | [project/adr/](../../project/adr/) |

---

_Last updated: 2026-09-05_
