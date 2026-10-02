# Runbook: Install Vesma

**🌐 Language / Язык:** English · [Русский](../../../ru/admin/runbooks/install.md)

## Prerequisites

- Python 3.11+ (the wheel is pure Python plus the bundled ONNX model — no build step)
- `pip` (or `uv` / `pipx` for isolated installs)
- Optional: `ollama` for external LLM enrichment (never needed for storage or search)

## Quick install (PyPI)

```bash
pip install vesma
```

- The MCP server ships in the base package — `vesma mcp-server` works out of the box (ADR-0023).
- The embedding model (`vesma-embed-v1`) is bundled: no downloads, works offline.

Isolated variant (installs the `vesma` CLI on `PATH`, project environments untouched):

```bash
uv tool install vesma
# or
pipx install vesma
```

Scripted variant (venv at `~/.mnemos/venv` + launcher in `~/.local/bin` + optional VS Code wiring):

```bash
curl -fsSL https://raw.githubusercontent.com/vesmaro/vesmaro/main/scripts/install.sh | bash
```

> ⚠️ **Names.** The PyPI package is `vesma` (bare slot, ours). Pre-rebrand `mnemos-memory-server` stays live until deprecation; the bare `pip install vesma` is an unrelated third-party project.

## Configuration

Default config lives at `~/.mnemos/config.yaml` (optional — the defaults are fine). Minimal:

```yaml
mnemos:
  data_dir: ~/.mnemos/data
  vault_path: ~/.mnemos/vault
  strict_tag_contract: true
embedding:
  provider: nano  # vesma-embed-v1 — bundled local model, works offline; or onnx, ollama
```

Store: `~/.mnemos/data/mnemos.db` (SQLite, WAL). Vault mirror: `~/.mnemos/vault/` (Obsidian-compatible markdown; the `~/.mnemos/` paths are the shipped defaults — 5.x keeps this layout during the dual period).

## Start MCP server

Add to your VS Code **User** or **Workspace** `mcp.json`:

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

Per-harness presets (Claude Code, Cursor, OpenCode, Codex, Windsurf, ZCode, pi, Hermes):
[`integrations/mcp-presets.md`](../../../../integrations/mcp-presets.md). Behavioral pack (instructions
/ skills / prompts): `vesma integration setup`.

## Start HTTP API

```bash
vesma serve  # uvicorn on 127.0.0.1:8787
```

## Container

For full container deployment (compose, Kubernetes, systemd quadlet), see
[container-deployment.md](container-deployment.md).

Quick single-container start using the released image:

```bash
podman run -d -v vesma-data:/data -v vesma-vault:/vault -p 8787:8787 \
  --env VESMA_API__TOTP_MASTER_KEY=<your-key> ghcr.io/vesmaro/vesmaro:4.3.0  # 6.0.0: the only honoured spelling (older prefixes retired)
```

Or with compose from the repo root:

```bash
podman-compose up -d
```

## Upgrade

```bash
pip install --upgrade vesma
```

The store schema is migrated automatically on first start of the new version. Back up
`~/.mnemos/data/` before major upgrades — see [backup-restore.md](backup-restore.md).

## Verify

```bash
vesma add "Hello Vesma" --tags "project:test,agent:manual,mnemos:learning"
vesma search "Hello"
vesma recall --agent manual --project test
```
