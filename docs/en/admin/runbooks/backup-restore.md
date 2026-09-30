# Runbook: Backup & Restore

**🌐 Language / Язык:** English · [Русский](../../../ru/admin/runbooks/backup-restore.md)

## Backup

### Full backup

```bash
# Vesma data + vault
tar czf mnemos-backup-$(date +%Y%m%d).tar.gz \
  ~/.mnemos/data \
  ~/.mnemos/vault
```

### Automated (cron)

```bash
# Daily backup at 02:00
0 2 * * * tar czf ~/backups/vesma-$(date +\%Y\%m\%d).tar.gz ~/.mnemos/data ~/.mnemos/vault
```

## Restore

```bash
# Stop MCP server / API if running

# Extract backup
tar xzf mnemos-backup-20260115.tar.gz -C /

# Or selective restore
cp mnemos-backup-20260115/.mnemos/data/mnemos.db ~/.mnemos/data/
rsync -a mnemos-backup-20260115/.mnemos/vault/ ~/.mnemos/vault/
```

## Point-in-time recovery

Vesma creates automatic DB backups before migrations:

```bash
ls ~/.mnemos/data/*.backup-*
# ~/.mnemos/data/mnemos.db.backup-20260115-143022

cp ~/.mnemos/data/mnemos.db.backup-20260115-143022 ~/.mnemos/data/mnemos.db
```

## Export / Import

### Export to JSON

```bash
python -c "
import json, os, sqlite3
conn = sqlite3.connect(os.path.expanduser('~/.mnemos/data/mnemos.db'))
rows = conn.execute('SELECT * FROM memories').fetchall()
print(json.dumps([dict(r) for r in rows], indent=2, default=str))
" > mnemos-export.json
```

### Import from JSON

Use `vesma add --file` or API `POST /memories` for bulk import.
