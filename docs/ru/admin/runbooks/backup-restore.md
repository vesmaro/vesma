# Runbook: Резервное копирование и восстановление

**🌐 Language / Язык:** [English](../../../en/admin/runbooks/backup-restore.md) · Русский

## Резервное копирование

### Полная резервная копия

```bash
# Данные Vesma + vault
tar czf vesma-backup-$(date +%Y%m%d).tar.gz \
  ~/.mnemos/data \
  ~/.mnemos/vault
```

### Автоматизация (cron)

```bash
# Ежедневное резервное копирование в 02:00
0 2 * * * tar czf ~/backups/vesma-$(date +\%Y\%m\%d).tar.gz ~/.mnemos/data ~/.mnemos/vault
```

## Восстановление

```bash
# Остановить MCP-сервер / API, если запущены

# Распаковать резервную копию
tar xzf vesma-backup-20260115.tar.gz -C /

# Или выборочное восстановление
cp vesma-backup-20260115/.mnemos/data/mnemos.db ~/.mnemos/data/
rsync -a vesma-backup-20260115/.mnemos/vault/ ~/.mnemos/vault/
```

## Восстановление на момент времени

Vesma автоматически создаёт резервные копии БД перед миграциями:

```bash
ls ~/.mnemos/data/*.backup-*
# ~/.mnemos/data/mnemos.db.backup-20260115-143022

cp ~/.mnemos/data/mnemos.db.backup-20260115-143022 ~/.mnemos/data/mnemos.db
```

## Экспорт / Импорт

### Экспорт в JSON

```bash
python -c "
import json, os, sqlite3
conn = sqlite3.connect(os.path.expanduser('~/.mnemos/data/mnemos.db'))
rows = conn.execute('SELECT * FROM memories').fetchall()
print(json.dumps([dict(r) for r in rows], indent=2, default=str))
" > vesma-export.json
```

### Импорт из JSON

Используйте `vesma add --file` или API `POST /memories` для массового импорта.
