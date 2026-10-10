# Runbook: Backup & Restore

**🌐 Language / Язык:** English · [Русский](../../../ru/admin/runbooks/backup-restore.md)

- **Canonical store** — `~/.vesma/` (since 6.0.0): DB at `~/.vesma/data/vesma.db` (SQLite, WAL)
  and the Obsidian-compatible mirror at `~/.vesma/vault/`. A 5.x-era
  `~/.mnemos/` store is legacy — migrate it with `vesma migrate-store`
  ([migration-6-0.md](../../user/migration-6-0.md)) before relying on these paths.
- For routine export/transfer use the utility (`vesma export` / `vesma import`), not
  a hand-rolled SQLite walk: the utility brings encryption, filters and an
  idempotent merge.
- Current installation state: `vesma doctor paths` prints the actual paths table.

## Backup

### Full backup

```bash
# Vesma data + vault
tar czf vesma-backup-$(date +%Y%m%d).tar.gz \
  ~/.vesma/data \
  ~/.vesma/vault
```

Stop the writing processes first (`vesma processor stop`; `vesma service stop`
when the service is running) so you do not capture a torn WAL snapshot.

### Portable export via Vesma itself

`vesma export` writes a portable JSON file or a full SQLite snapshot, optionally
compressed and AES-256-GCM encrypted:

```bash
# Full JSON export (default): metadata + content
vesma export --output vesma-export.json

# Full DB snapshot, zstd compression, encryption with a file-held passphrase
vesma export --format sqlite --compress zstd --encrypt \
  --passphrase-file ~/.secrets/backup-passphrase \
  --output vesma-snapshot.db.zst

# Review what would go into the backup without writing anything
vesma export --dry-run
```

Useful filters: `--project`, `--agent`, `--status`, `--tags`, `--since`/`--until`.
Full list: `vesma export --help`.

### Automated (cron)

```bash
# Daily backup at 02:00
0 2 * * * tar czf ~/backups/vesma-$(date +\%Y\%m\%d).tar.gz ~/.vesma/data ~/.vesma/vault
```

## Restore

Stop the writing processes first (`vesma processor stop`, `vesma service stop`),
then restore:

```bash
# Extract backup
tar xzf vesma-backup-20260115.tar.gz -C ~

# Or selective restore
cp vesma-backup-20260115/.vesma/data/vesma.db ~/.vesma/data/
rsync -a vesma-backup-20260115/.vesma/vault/ ~/.vesma/vault/
```

> A 5.x-era backup unpacks under `.mnemos/` (db name `mnemos.db`) — restore it
> into a scratch home and move it forward with `vesma migrate-store`
> ([migration-6-0.md](../../user/migration-6-0.md)), not directly over `~/.vesma/`.

### Restore from a Vesma export

`vesma import` is the counterpart of `vesma export`. `--mode merge` upserts by
record id (idempotent, safe to re-run); `--mode restore` REPLACES the whole store
and therefore requires explicit `--confirm`. Imports from untrusted files have
server-minted canon keys stripped — lift that only with `--trusted-restore` on a
trusted self-backup:

```bash
# Idempotent merge from a JSON export
vesma import vesma-export.json --mode merge

# Full restore from a snapshot (destructive; auto-backup goes to --backup-dir)
vesma import vesma-snapshot.db.zst --mode restore --confirm \
  --passphrase-file ~/.secrets/backup-passphrase \
  --backup-dir ~/.vesma/data/pre-restore
```

After restoring, check state: `vesma stats`, `vesma search "probe"`, `vesma doctor`.

## Point-in-time recovery

Vesma creates automatic DB backups before schema migrations:

```bash
ls ~/.vesma/data/*.backup-*
# ~/.vesma/data/vesma.db.backup-20260115-143022

cp ~/.vesma/data/vesma.db.backup-20260115-143022 ~/.vesma/data/vesma.db
```

## Importing third-party records

Bulk import of external JSON content goes through `vesma ingest file PATH` (one
file → one record) or the API `POST /memories`. For moving whole stores that is
not the path — use `vesma import` (above) or the
[ai-brain migration](migrate.md).

## See also

- [migrate.md](migrate.md) — ai-brain migration and tag canonization
- [security.md](../security.md) — export encryption, passphrase handling
- [getting-started.md](../../user/getting-started.md) — install-flow venv and the service