<!-- mnemos-integration: v2.0.0 -->
# Export & Import

**🌐 Language / Язык:** English · [Русский](../../ru/user/export-import.md)

> Backup, migrate, and restore Vesma memories via the CLI or HTTP API.
> JSON exports carry metadata (vectors regenerate on import); SQLite
> exports are complete snapshots. Traces are never exported — they are
> audit logs, not memory data.

---

## Overview

The export/import subsystem lets you:

- **Back up** the memory store to a portable file (JSON or SQLite).
- **Migrate** memories between Vesma instances.
- **Restore** a previous state after data loss or a bad import.
- **Filter** what gets exported (by project, agent, status, tags, date range).
- **Encrypt** exports with a passphrase (AES-256-GCM, PBKDF2 key derivation).
- **Compress** exports with gzip to save space.
- **Run incremental** backups with `--since` for periodic snapshots.

Two surfaces are available: the `vesma export` / `vesma import` CLI
commands, and the `POST /api/v1/export` / `POST /api/v1/import` HTTP
endpoints. Both share the same underlying logic.

---

## Export formats

| Format | What it contains | Vectors | Traces | Best for |
|--------|------------------|---------|--------|----------|
| **JSON** (`--format json`) | Memory metadata + projects | Regenerated on import | Never | Partial export, migration, inspection |
| **SQLite** (`--format sqlite`) | Raw `mnemos.db` + `vectors.db` in a `.tar.gz` | Included (full snapshot) | Never | Fastest full backup / restore |

### JSON — metadata only

A JSON export contains every memory's metadata (content, tags, status,
timestamps, project, agent) and the project registry. **Vectors are not
included** — they are regenerated on import by re-embedding the published
memories. This keeps the export file small and portable.

### SQLite — complete snapshot

A SQLite export copies the raw `mnemos.db` and `vectors.db` files into a
`.tar.gz` archive. This is the fastest way to produce a full backup and
the fastest way to restore one (the DB files are replaced directly).
Filters (`--project`, `--agent`, etc.) do **not** apply to SQLite exports
— a SQLite export is always a full snapshot.

### What is never exported

- **Traces** — the `traces` table holds pipeline audit logs (cluster,
  synthesize, publish, recall steps with latency and LLM flags). These
  are operational logs, not memory data, and are excluded from every
  export format by design.

---

## Partial export — filters

Filters apply to **JSON exports only**. SQLite exports are always full
snapshots.

| Filter | Flag | Matches |
|--------|------|--------|
| Project | `--project <slug>` | Memories with `project:<slug>` |
| Agent | `--agent <slug>` | Memories with `agent:<slug>` |
| Status | `--status <value>` | `raw`, `processing`, `processed`, `published`, `archived` |
| Tags | `--tags a,b,c` | Memories containing all listed tags (AND logic) |
| Since | `--since 2026-06-01` | Memories created/updated after this ISO date |
| Until | `--until 2026-06-20` | Memories created/updated before this ISO date |

Example — export only published memories from the `vesma` project:

```bash
vesma export \
  --format json \
  --project vesma \
  --status published \
  --output vesma-published.json
```

---

## Compression

```bash
vesma export --format json --compress gzip --output backup.json.gz
```

| Mode | Flag | Notes |
|------|------|------|
| None | `--compress none` (default) | No compression |
| Gzip | `--compress gzip` | Standard library, universally available |
| Zstd | `--compress zstd` | Reported as a future enhancement when the optional dependency is absent |

---

## Encryption

```bash
vesma export --format json --encrypt --output backup.enc
# Passphrase: ******** (prompted, hidden, confirmed)
```

Encryption uses **AES-256-GCM** with a key derived from the passphrase via
**PBKDF2** (200 000 iterations, 16-byte salt). The salt and nonce are
prepended to the ciphertext so the file is self-contained for decryption.

| Passphrase source | Flag | When to use |
|-------------------|------|-------------|
| Interactive prompt | (default when `--encrypt` is set) | Manual backups |
| File | `--passphrase-file /path/to/key` | CI / scripting |

For the HTTP API, the passphrase is sent in the `X-Vesma-Passphrase`
header — never in the request body — so it is not logged as a request
parameter.

---

## Incremental backups

Use `--since` to export only memories created or updated after a given
timestamp. This is the building block for periodic backups:

```bash
# Daily incremental — only memories touched since yesterday
vesma export --format json --since "$(date -u -d 'yesterday' +%Y-%m-%d)" \
  --output daily-$(date -u +%Y-%m-%d).json
```

Combine with `--compress gzip` and `--encrypt` for a compact, protected
daily snapshot.

---

## Import modes

| Mode | Flag | Behaviour | Destructive? |
|------|------|-----------|--------------|
| **merge** | `--mode merge` (default) | Insert memories whose ID is absent; skip existing (or update with `--overwrite`). Projects are merged. Vectors regenerate for published memories. | No — idempotent |
| **restore** | `--mode restore --confirm` | Wipe all memories, vectors, and projects, then import. For SQLite, raw DB files are replaced (after an optional backup). | **Yes — requires `--confirm`** |
| **dry-run** | `--dry-run` | Validate the export file without writing anything. Works with both modes. | No |

### merge — idempotent

```bash
vesma import backup.json --mode merge
```

Memories whose ID already exists in the target store are **skipped** by
default. Pass `--overwrite` to update them with the imported version.
Projects are merged: a project is created if absent, or its paths are
updated if they changed.

### restore — destructive

```bash
vesma import backup.json --mode restore --confirm
```

Restore mode **deletes all existing memories, vectors, and projects**
before importing. This cannot be undone. The command refuses to run
without `--confirm` and prints a warning explaining what will be lost.

For SQLite restore, you can back up the current DB first:

```bash
vesma import snapshot.tar.gz --mode restore --confirm --backup-dir ./pre-restore
```

### dry-run — validate first

```bash
vesma import backup.json --mode merge --dry-run
vesma import backup.json --mode restore --dry-run
```

Validates the export file (format, schema, readability) and reports how
many memories would be imported / skipped / updated — without writing
anything. Always run this before a `restore`.

---

## Format versioning

Every JSON export carries two version markers in its metadata:

| Field | Meaning |
|-------|---------|
| `format_version` | The export schema version (currently `1.0`). Bumped when the JSON structure changes in a breaking way. |
| `mnemos_version` | The Vesma version that produced the export. |

On import, Vesma checks `format_version` and warns if it does not
recognise the schema. This provides forward compatibility — a future
Vesma can import a `1.0` export even after the schema evolves.

---

## CLI reference

### `vesma export`

```bash
vesma export [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--output`, `-o` | `vesma-export.json` | Output file path |
| `--format`, `-f` | `json` | `json` or `sqlite` |
| `--compress` | `none` | `none`, `gzip`, `zstd` |
| `--encrypt` | off | Encrypt with passphrase (AES-256-GCM) |
| `--passphrase-file` | (prompt) | Read passphrase from this file |
| `--project` | (all) | Filter by project slug |
| `--agent` | (all) | Filter by agent slug |
| `--status` | (all) | Filter by memory status |
| `--tags` | (all) | Comma-separated tags (AND logic) |
| `--since` | (all) | Only memories after this ISO date |
| `--until` | (all) | Only memories before this ISO date |
| `--dry-run` | off | Validate inputs without writing |
| `--config`, `-c` | (auto) | Path to config.yaml |

### `vesma import`

```bash
vesma import SOURCE [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `SOURCE` (argument) | required | Export file to import |
| `--mode`, `-m` | `merge` | `merge` or `restore` |
| `--overwrite` | off | Update existing memories in merge mode |
| `--confirm` | off | Confirm destructive restore (required for `restore`) |
| `--dry-run` | off | Validate without writing |
| `--passphrase-file` | (prompt) | Read decryption passphrase from this file |
| `--backup-dir` | (none) | Back up current DB here before restore |
| `--config`, `-c` | (auto) | Path to config.yaml |

---

## HTTP API

### `POST /api/v1/export`

Stream an export as a file download.

**Request body** (`application/json`):

```json
{
  "format": "json",
  "compress": "gzip",
  "encrypt": false,
  "project": "vesma",
  "agent": null,
  "status": null,
  "tags": null,
  "since": null,
  "until": null
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `format` | string | `"json"` | `json` or `sqlite` |
| `compress` | string | `"none"` | `none`, `gzip`, `zstd` |
| `encrypt` | boolean | `false` | Encrypt with passphrase |
| `project` | string\|null | `null` | Filter by project |
| `agent` | string\|null | `null` | Filter by agent |
| `status` | string\|null | `null` | Filter by status |
| `tags` | array\|null | `null` | Tags to filter by (AND logic) |
| `since` | string\|null | `null` | ISO date lower bound |
| `until` | string\|null | `null` | ISO date upper bound |

**Encryption passphrase** — pass via the `X-Vesma-Passphrase` header.
If `encrypt: true` and the header is missing, the endpoint returns
`400` with `{"detail": "Encryption requested but X-Vesma-Passphrase header is missing."}`.

**Response** — `StreamingResponse` with `Content-Disposition:
attachment; filename="vesma-export.<suffix>"`. The suffix depends on
format + compression + encryption (`json`, `json.gz`, `tar.gz`, `enc`).

### `POST /api/v1/import`

Upload an export file as multipart form data and import it.

| Parameter | Location | Type | Default | Description |
|-----------|----------|------|---------|-------------|
| `file` | form (required) | file | — | The export file |
| `mode` | query | string | `merge` | `merge` or `restore` |
| `overwrite` | query | bool | `false` | Update existing in merge mode |
| `confirm` | query | bool | `false` | Required for `restore` |
| `dry_run` | query | bool | `false` | Validate without writing |
| `X-Vesma-Passphrase` | header | string | (none) | Decryption passphrase |

**Response** (`200 OK`):

```json
{
  "imported": 142,
  "skipped": 3,
  "updated": 0,
  "errors": [],
  "warnings": [],
  "format_version": "1.0",
  "mnemos_version": "4.0.0"
}
```

---

## Common workflows

### Full encrypted backup (CLI)

```bash
vesma export --format sqlite --compress gzip --encrypt \
  --output backup-$(date -u +%Y%m%d).tar.gz.enc
```

### Restore from a backup (CLI)

```bash
# 1. Validate first
vesma import backup-20260620.tar.gz.enc --mode restore --dry-run

# 2. Back up current state, then restore
vesma import backup-20260620.tar.gz.enc --mode restore --confirm \
  --backup-dir ./pre-restore-$(date -u +%Y%m%d)
```

### Migrate a project between instances

```bash
# Source instance
vesma export --format json --project vesma --output vesma-project.json

# Target instance
vesma import mnemos-project.json --mode merge
```

### Periodic incremental backup (cron)

```cron
15 3 * * *  vesma export --format json --compress gzip --since "$(date -u -d 'yesterday' +%Y-%m-%d)" --output /backups/vesma-$(date +\%Y\%m\%d).json.gz
```

---

## See also

- [CLI Reference](cli-reference.md) — all `vesma` subcommands.
- [HTTP API Reference](http-api.md) — every endpoint.
- [Security Model](../admin/security.md) — encryption, secrets hygiene.