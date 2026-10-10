# Runbook: Резервное копирование и восстановление

**🌐 Language / Язык:** [English](../../../en/admin/runbooks/backup-restore.md) · Русский

- **Канонический стор** — `~/.vesma/` (с 6.0.0): БД `~/.vesma/data/vesma.db` (SQLite, WAL)
  и Obsidian-совместимое зеркало `~/.vesma/vault/`. Стор эпохи 5.x `~/.mnemos/` —
  легаси: сначала перенесите его командой `vesma migrate-store`
  ([migration-6-0.md](../../user/migration-6-0.md)) и только потом опирайтесь на эти пути.
- Для штатного экспорта/переноса данных используйте утилиту (`vesma export` /
  `vesma import`), а не ручной обход SQLite: она умеет шифрование, фильтры и
  идемпотентный merge.
- Текущее состояние установки: `vesma doctor paths` печатает таблицу фактических путей.

## Резервное копирование

### Полная резервная копия

```bash
# Данные Vesma + vault
tar czf vesma-backup-$(date +%Y%m%d).tar.gz \
  ~/.vesma/data \
  ~/.vesma/vault
```

Перед таром остановите пишущие процессы (`vesma processor stop`; если работает
сервис — `vesma service stop`), чтобы не поймать рассогласованный WAL-срез.

### Портативный экспорт средствами Vesma

`vesma export` пишет переносимый JSON или полный снапшот SQLite, опционально
со сжатием и шифрованием AES-256-GCM:

```bash
# Полный JSON-экспорт (дефолт): метаданные + содержимое
vesma export --output vesma-export.json

# Полный снапшот БД, сжатие zstd, шифрование парольной фразой из файла
vesma export --format sqlite --compress zstd --encrypt \
  --passphrase-file ~/.secrets/backup-passphrase \
  --output vesma-snapshot.db.zst

# Обзор того, что попадёт в бэкап, без записи файла
vesma export --dry-run
```

Полезные фильтры: `--project`, `--agent`, `--status`, `--tags`, `--since`/`--until`.
Полный список — `vesma export --help`.

### Автоматизация (cron)

```bash
# Ежедневное резервное копирование в 02:00
0 2 * * * tar czf ~/backups/vesma-$(date +\%Y\%m\%d).tar.gz ~/.vesma/data ~/.vesma/vault
```

## Восстановление

Остановите пишущие процессы (`vesma processor stop`, `vesma service stop`)
и только потом восстанавливайте:

```bash
# Распаковать резервную копию
tar xzf vesma-backup-20260115.tar.gz -C ~

# Или выборочное восстановление
cp vesma-backup-20260115/.vesma/data/vesma.db ~/.vesma/data/
rsync -a vesma-backup-20260115/.vesma/vault/ ~/.vesma/vault/
```

> Бэкап эпохи 5.x распаковывается в `.mnemos/` (имя БД — `mnemos.db`) — восстановите
> его во временный дом и перенесите вперёд командой `vesma migrate-store`
> ([migration-6-0.md](../../user/migration-6-0.md)), а не поверх `~/.vesma/`.

### Восстановление из экспорта Vesma

`vesma import` — парный к `vesma export` путь. `--mode merge` апсёртит по id
записи (идемпотентен, безопасен для повторного запуска); `--mode restore`
ЗАМЕНЯЕТ весь стор и требует явно `--confirm`. Импорт из недоверенного файла
срезает серверно-минтованные canon-ключи — снимайте это только флагом
`--trusted-restore` на доверенном самобэкапе:

```bash
# Идемпотентный merge из JSON-экспорта
vesma import vesma-export.json --mode merge

# Полное восстановление из снапшота (деструктивно; авто-бэкап в --backup-dir)
vesma import vesma-snapshot.db.zst --mode restore --confirm \
  --passphrase-file ~/.secrets/backup-passphrase \
  --backup-dir ~/.vesma/data/pre-restore
```

После восстановления проверьте состояние: `vesma stats`, `vesma search "probe"`,
`vesma doctor`.

## Восстановление на момент времени

Vesma автоматически создаёт резервные копии БД перед миграциями схемы:

```bash
ls ~/.vesma/data/*.backup-*
# ~/.vesma/data/vesma.db.backup-20260115-143022

cp ~/.vesma/data/vesma.db.backup-20260115-143022 ~/.vesma/data/vesma.db
```

## Импорт сторонних записей

Импорт стороннего JSON-контента — через `vesma ingest file PATH` (один файл →
одна запись) или API `POST /memories`. Для массового переноса хранилищ это не
путь — используйте `vesma import` (выше) или [миграцию с ai-brain](migrate.md).

## См. также

- [migrate.md](migrate.md) — миграция с ai-brain и канонизация тегов
- [security.md](../security.md) — шифрование экспорта, хранение парольных фраз
- [getting-started.md](../../user/getting-started.md) — venv от install-флоу и сервис