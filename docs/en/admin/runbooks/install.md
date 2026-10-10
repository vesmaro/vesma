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

Other channels: npm (`npm install -g @vesmaro/vesma`) and the ghcr container — see the
"Container" section below. Updating an existing Vesma install is done by the utility
itself: `vesma update apply` (the "Upgrade" section below).

> ⚠️ **Names.** The PyPI package is `vesma` (bare slot, ours — the primary channel; `pip install vesma` installs this project). The pre-rebrand `mnemos-memory-server` stays published until deprecation (frozen at 5.2.0), and `vesma-memory-server` is our live mirror alias. Channel table: [PyPI publish runbook](pypi-publish.md).

> **venv — install flow only.** Creating a venv manually is not part of any workflow: the
> service layer expects venvs created by `vesma service install` (`~/.local/share/vesma/venv/`
> and `venvs/<name>/`). Old manual venvs are legacy — their cleanup is covered in
> [getting-started.md](../../user/getting-started.md#cleaning-up-old-installations).

## Configuration

Default config lives at `~/.vesma/config.yaml` (optional — the defaults are fine). Minimal:

```yaml
vesma:
  data_dir: ~/.vesma/data
  vault_path: ~/.vesma/vault
  strict_tag_contract: true
embedding:
  provider: nano  # vesma-embed-v1 — bundled local model, works offline; or onnx, ollama
```

Store: `~/.vesma/data/vesma.db` (SQLite, WAL). Vault mirror: `~/.vesma/vault/` (Obsidian-compatible markdown; the `~/.vesma/` paths are the shipped defaults since 6.0.0 — the 5.x-era `~/.mnemos/` layout moves with `vesma migrate-store`, see [migration-6-0.md](../../user/migration-6-0.md)).

## Service (recommended for long-running hosts)

```bash
vesma service install                         # manifests, data dirs, venvs, the systemd user unit
systemctl --user enable --now vesma.service   # autostart
vesma service status && vesma service health  # live state and the health verdict
```

Without systemd — `vesma service run` (the supervisor in the foreground). The full verb
set (start/stop/restart/logs/uninstall) and installation diagnostics
(`vesma doctor service`) live in
[getting-started.md](../../user/getting-started.md#service-vesma-service).

## Connecting MCP

**Manual MCP setup is cancelled** — do not paste blocks into `mcp.json` or the native
harness configs by hand. The utility is the only path:

```bash
vesma integration setup              # all detected harnesses + agent wiring, idempotent
vesma integration setup -t copilot   # one harness only
```

After a package upgrade, refresh what is deployed: `vesma integration update`.
Copy-paste blocks for non-standard harnesses (a fallback, not the primary path):
[`integrations/mcp-presets.md`](../../../../integrations/mcp-presets.md).

## Start HTTP API

```bash
vesma serve  # uvicorn on 127.0.0.1:8787
```

When the service supervisor is running, the HTTP API core is already embedded in it —
a separate `vesma serve` is not needed.

## Container

For full container deployment (compose, Kubernetes, systemd quadlet), see
[container-deployment.md](container-deployment.md).

Quick single-container start using the released image:

```bash
podman run -d -v vesma-data:/data -v vesma-vault:/vault -p 8787:8787 \
  --env VESMA_API__TOTP_MASTER_KEY=<your-key> ghcr.io/vesmaro/vesma:6.0.0  # 6.0.0: the only honoured spelling (older prefixes retired)
```

Or with compose from the repo root:

```bash
podman-compose up -d
```

## Upgrade

```bash
vesma update check    # reports every update surface, changes nothing
vesma update apply    # pip user-site (+ npm best-effort), prompt-free
```

`apply` touches only the pip user-site (and npm, if installed); prod venvs, Go binaries
and containers are never updated automatically. Weekly automation:
`vesma update timer install` (a systemd user timer). The full subcommand map is in
[getting-started.md](../../user/getting-started.md#the-vesma-update-subcommands).

The store schema is migrated automatically on first start of the new version. Back up
`~/.vesma/data/` before major upgrades — see [backup-restore.md](backup-restore.md).

## Verify

```bash
vesma add "Hello Vesma" --tags "project:test,agent:manual,vesma:learning"
vesma search "Hello"
vesma recall agent manual --project test
vesma doctor          # the health gate: config, store, MCP transport, registrations
vesma doctor service  # the service installation against the layout v1 contract (DR-01…DR-13)
```
