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
> no-op alias so older commands and snippets keep resolving. The `vesma-embed-v1` embedding model
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
| From source (contributors) | `git clone https://github.com/vesmaro/vesma && cd vesma && uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"` — see [CONTRIBUTING.md](../../../CONTRIBUTING.md) |

<details>
<summary><strong>Released wheel and pre-built container image</strong> — version-pinned channels</summary>

**Released wheel** (pin a specific version):

<!-- version:pip -->
```bash
pip install https://github.com/vesmaro/vesma/releases/download/v4.3.0/mnemos_memory_server-4.3.0-py3-none-any.whl
```
<!-- /version:pip -->

<!-- deprecated-note: 5.x releases ship as the `vesma` wheel (bare PyPI slot); the legacy
mnemos_memory_server-*.whl artifact name covers the 4.x line until deprecation. -->

**Pre-built image** (published at `ghcr.io/vesmaro/vesmaro`; `docker` works too — swap `podman` for `docker`):

```bash
export VESMA_API__TOTP_MASTER_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
podman run -d --name vesma \
  -p 8787:8787 \
  -v vesma-data:/data \
  -v vesma-vault:/vault \
  -e VESMA_API__TOTP_MASTER_KEY="${VESMA_API__TOTP_MASTER_KEY}" \
<!-- version:image -->
  ghcr.io/vesmaro/vesma:5.1.2
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

> **Hardware.** The bundled `vesma-embed-v1` runs comfortably on a single CPU core. No GPU. A 2 vCPU / 2 GB VM is enough for personal use.

---

## First memory (CLI)

```bash
vesma add "Hello world" --tags project:test agent:getting-started vesma:learning
```

Expected output:

```text
✓ Saved: Hello world (550e8400-e29b-41d4-a716-446655440000)
```

Vesma automatically:

1. **Wrote the entry to SQLite** at `~/.mnemos/data/mnemos.db` (created on first run).
2. **Mirrored it to your Obsidian vault** at `~/.mnemos/vault/` as a markdown file with YAML frontmatter.
3. **Validated the tag contract** — `project:test` + `agent:getting-started` + `vesma:learning` is a valid trio. Skip one and you get `❌ Tag contract violation: ...` instead.

The tag contract is documented in [tag-contract.md](tag-contract.md). The short version: every memory needs **exactly one** `project:<slug>`, **exactly one** `agent:<slug>`, and **at least one** `vesma:<subtype>` (e.g. `vesma:learning`, `vesma:bug-pattern`, `vesma:decision`). You always type the `vesma:` alias; `mnemos:` is the canonical storage prefix, stable by contract, and `vesma:` is accepted as an input alias everywhere — stored tags keep the canonical `mnemos:*` form.

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
default — and since PG-0.5 **your project indexes itself**: the first MCP
call (or `pre_llm_call` hook) an agent makes inside a directory carrying a
packaging manifest (`pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`,
`setup.py`) auto-registers and indexes it in the background. No explicit
call, no instruction, no skill.

What happens, in order:

1. Work in your project as usual — an agent calls any MCP tool there.
2. The first index runs in the background (`auto-first`); from then on a
   beacon line in `assemble_context` output reports graph freshness.
3. Check it: `mnemos_project_graph_status` — volumes, freshness, poisoned
   count. (It needs the project's `project_id` — see
   `mnemos_list_graph_projects`.)

Prefer the explicit path? Register a root by hand
(`mgr.sqlite.save_project(Project(name="myproj", paths=["/abs/path/to/myproj"]))`)
and call `mnemos_index_project` with `project_id` and `agent` — that manual
flow always stays available, and a successful manual index also lifts a
suspended auto path.

Don't want it? Two switches in `config.yaml`: `code_graph.auto_index: false`
stops only the background auto path (manual tools keep working);
`code_graph.enabled: false` turns the whole surface off — every graph call
then answers `code: "disabled"`. Full walkthrough:
[project-graph.md](project-graph.md).

---

## Connect your harness (MCP)

The MCP server is the primary integration surface: your agent harness spawns `vesma mcp-server` over stdio and gets the full `vesma_*` tool set. Pick your harness:

> The MCP server is the primary integration surface: your agent harness spawns `vesma mcp-server` over stdio and gets the full `vesma_*` tool set. Pick your harness:

| Harness | Fastest path |
|---------|--------------|
| VS Code Copilot | `curl -fsSL …/scripts/mcp-setup.sh \| bash`, then reload the window |
| Claude Code | `vesma integration setup --target claude-code` (native `CLAUDE.md` + user-scope MCP), or `claude mcp add --scope user vesma -- vesma mcp-server` |
| Cursor | `vesma integration setup --target cursor`, or paste one line into `~/.cursor/mcp.json` |
| OpenCode | paste one block into `~/.config/opencode/opencode.json` |
| Codex / Windsurf | `vesma integration setup --target codex` / `--target windsurf` (native config merge), or one TOML / JSON block each |
| ZCode, pi, Hermes Agent | `vesma integration setup --target zcode` / `--target pi` / `--target hermes` |
| Anything else | [adapter-template.md](../../../integrations/adapter-template.md) |

**The full copy-paste instructions for every harness live on one page: [Connect Vesma to any harness](../../../integrations/mcp-presets.md).** The behavioral layer — instructions, skills, and prompt mode that make agents actually *use* the memory — is a separate one-pass step:

```bash
vesma integration setup
```

The plain command deploys to **all detected harnesses on this host and wires
all agents** — non-interactive and idempotent (re-running refreshes). Use
flags to narrow: `--target <name>` (repeatable) installs to a specific
harness, `--no-wire-agents` skips agent wiring. See the
[integration guide](integration-guide.md) for all targets and flags.

### Memory status

At any point, check how memory is attached to each detected harness:

```bash
vesma memory status
```

A read-only report per harness: pack attachment (stamps), the MCP
registration (server keys only — vesma plus any external memory engines
seen in the harness config), the local store markers (existence/mtime,
never contents) and the active precedence mode (`overlay+mirror` by
default; ADR-0034).

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

> **Tip — auto-collect mode.** Set `VESMA_AUTO_COLLECT=1` in the server's `env` block to make Vesma nudge your agent to call `vesma_save_context` every ~6 tool calls. See [mcp-tools.md#auto-collect-mode](mcp-tools.md#auto-collect-mode) for the trade-offs.

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

## Updates

Vesma tells you when a newer release exists and updates itself with one command (issue #445).

**Update check — on by default, quiet.** Vesma asks PyPI "is there a newer version?" with a single version-manifest GET (3s timeout, no telemetry, nothing posted), caches the answer for 24h in `<data_dir>/update-check.json`, and surfaces it in three places:

| Where | What you see |
|-------|--------------|
| `mnemos_stats` (MCP) / `vesma stats` | the `update_available` object: `{installed, latest, dist, update_available, checked_at}` (or `null`) |
| `vesma --version` | a stderr line: `update available: 5.2.0 (run 'vesma update --check')` |
| Server start (`vesma serve`, `vesma mcp-server`) | one INFO line in the log |

Offline machines are unaffected: a failed check serves the cached answer (marked stale) and never crashes anything. To turn the check off:

```bash
VESMA_UPDATES_CHECK=off vesma serve      # hard env kill switch
```

or in `config.yaml` (env equivalent: `VESMA_UPDATES__CHECK_ENABLED=false`):

```yaml
updates:
  check_enabled: false
```

**`vesma update` — one command per machine.** Without flags it reports every update surface found on this machine — the pip dist it would upgrade (installed vs latest), the global npm package `@vesmaro/vesma`, host prod-venvs, and the Go binaries:

```bash
vesma update            # or: vesma update --check
vesma update --yes --scope=user    # pip install --user --upgrade <dist>, npm -g best-effort
vesma update --to 5.1.1 --yes      # rollback / pin to a specific version
```

`--yes` touches only the pip user-site (and npm, when installed) — it never silently touches the production venvs, the Go binaries, or containers; those are report-only by design. Every run appends a record to `~/.local/share/vesma/update-history.json`. After an update, restart your agent harness / `vesma serve` to pick up the new version.

**Fully automatic (optional).** A weekly systemd user timer runs the same update:

```bash
vesma update --install-timer      # writes ~/.config/systemd/user/vesma-update.{service,timer}, enables weekly + Persistent
vesma update --uninstall-timer    # remove again
```

The unit templates live in [`contrib/vesma-update.service`](../../../contrib/vesma-update.service) / [`.timer`](../../../contrib/vesma-update.timer) — their header comments explain the distrobox adaptation (one `distrobox-enter` ExecStart per box, same pattern as the prod units) and what is never auto-updated.

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
| `embedding.provider` | `nano` | `nano` (vesma-embed-v1, bundled) / `onnx` / `ollama` / `sentence-transformers` |
| `search.hybrid_alpha` | `0.5` | Weight of the vector leg in RRF (0.0 = pure FTS, 1.0 = pure vector). Default re-tuned 0.7 → 0.5: leg balance stops vector dominance from drowning FTS-rank-1 matches (issue #300) |
| `api.host` / `api.port` | `127.0.0.1` / `8787` | `vesma serve` defaults |
| `llm.provider` / `llm.model` | `ollama` / `qwen2.5:3b` | Pipeline synthesis & context filter |

Any of these can be overridden by env vars (`VESMA_*`, with `__` for nesting; the 5.0–5.2 `VESMARO_*` spelling stays accepted until 6.0, the 4.x `MNEMOS_*` spelling is no longer read):

```bash
VESMA_SEARCH__HYBRID_ALPHA=0.7 vesma search "deployment"
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

_Last updated: 2026-10-01_
