<!-- vesma-integration: v2.0.0 -->
# Федерация — пакетная синхронизация (Phase 0)

**🌐 Language / Язык:** English · [Русский](./sync.md)

> Кураторская, офлайн, cron-управляемая пакетная синхронизация между
> двумя инстансами vesma. Сам vesma не делает сетевых вызовов —
> перенос файла выполняется оператором (rsync / scp / общая директория
> через `scripts/sync-peers.sh`).

---

## Обзор

Пакетная синхронизация позволяет двум инстансам vesma обмениваться
записями проектов из курируемого списка **shared_projects**. Поток:

1. **Экспорт** — `vesma sync export` собирает `vesmaro.federation.v1`
   compact-payload из записей проектов в `shared_projects`, пропускает
   каждую через moderation-pipeline и записывает результат в файл
   (опционально AES-256-GCM шифрование).
2. **Перенос** — оператор копирует файл на целевой инстанс (rsync / scp
   / cp через общую директорию). `scripts/sync-peers.sh` — cron-ready
   шаблон, объединяющий все три шага.
3. **Импорт** — `vesma sync import` читает compact-payload
   (расшифровывая при необходимости), валидирует каждую запись и
   мержит идемпотентно по `id` записи.

Это **Phase 0** roadmap'а федерации (ArchCom 2026-07-17 контракт §3.1):
офлайн, оператор-управляемый, без живого сетевого протокола. Phase 2
(mediated pull) строится поверх того же compact-формата и moderation.

---

## Конфигурация

Пакетная синхронизация управляется секцией `federation` в `config.yaml`:

```yaml
federation:
  shared_projects:
    - project-umbra
    - project-vesma
  moderation_mapping_ttl_hours: 24   # TTL in-memory mapping таблицы
  moderation_refuse_threshold: 0.8   # >80% redacted → refuse
```

| Поле | По умолчанию | Назначение |
|------|--------------|------------|
| `shared_projects` | `[]` (пусто) | Whitelist slug'ов проектов, доступных для синхронизации. Пусто = ничего не синхронизируется. |
| `moderation_mapping_ttl_hours` | `24` | TTL per-run mapping таблицы moderation (только in-memory, не персистится). |
| `moderation_refuse_threshold` | `0.8` | Доля контента, которая должна быть redacted/anonymized для вердикта `refuse`. |

Можно переопределить `shared_projects` на один запуск через
`--shared-projects` (через пробел или запятую) — CLI-значение важнее
конфига.

---

## Экспорт — `vesma sync export`

```bash
vesma sync export \
  --output /var/tmp/vesma-sync.json \
  --shared-projects "project-umbra project-vesma"
```

Опции:

| Опция | По умолчанию | Назначение |
|-------|--------------|------------|
| `--output` / `-o` | `vesma-sync.json` | Путь выходного файла (рекомендуется абсолютный). Родительские директории создаются. |
| `--encrypt` | выкл | Шифровать payload через AES-256-GCM. Пароль читается из `VESMARO_EXPORT_PASSPHRASE` (легаси-алиас `MNEMOS_EXPORT_PASSPHRASE` принимается до 6.0). |
| `--shared-projects` | config `federation.shared_projects` | Список slug'ов через пробел/запятую (переопределяет конфиг). |
| `--dry-run` | выкл | Собрать payload и вывести сводку; файл НЕ записывать. |
| `--config` / `-c` | discovery | Путь к `config.yaml`. |

Что делает экспорт:

1. Разрешает `shared_projects` (CLI > конфиг; оба пусты → ошибка).
2. Запрашивает записи: `project` в `shared_projects`, исключает
   `mnemos:no-federate` и `archived`.
3. Вызывает `build_compact_payload()` — прогоняет moderation-pipeline
   (Layer 3) на каждой записи: `allow` → оригинальный контент,
   `redact` → sanitized-контент, `refuse` → запись исключается и
   учитывается в счётчике.
4. Записывает compact-payload
   (`{"schema": "vesmaro.federation.v1", "records": [...], "stats": {...}}`)
   в `--output`, опционально зашифрованным.

Сводка вывода:

```
✓ Exported: 12 records
  refused: 1
  secrets_redacted: 3
  pii_anonymized: 2
  encrypted: false
  shared_projects: project-umbra, project-vesma
  path: /var/tmp/vesma-sync.json
```

### Шифрование

`--encrypt` читает пароль из переменной окружения
`VESMARO_EXPORT_PASSPHRASE` (легаси-алиас `MNEMOS_EXPORT_PASSPHRASE`
принимается до 6.0) — никогда из CLI-аргумента (аргументы попадают
в список процессов и историю shell). Если переменная не задана, файл не
записывается, команда завершается с ошибкой.

```bash
export VESMARO_EXPORT_PASSPHRASE="your-passphrase-here"
vesma sync export --output sync.enc --encrypt
```

Зашифрованный файл несёт magic-заголовок `MNEMOS1` (историческая строка —
формат-стабильная, не бренд), чтобы сторона импорта могла его
автоматически определить.

---

## Импорт — `vesma sync import`

```bash
vesma sync import /var/tmp/vesma-sync.json
```

Опции:

| Опция | По умолчанию | Назначение |
|-------|--------------|------------|
| `--passphrase-env` | `VESMARO_EXPORT_PASSPHRASE` | Имя переменной окружения с паролем для расшифровки (**имя**, не значение). `MNEMOS_EXPORT_PASSPHRASE` — легаси-алиас (до 6.0). |
| `--dry-run` | выкл | Провалидировать payload и вывести отчёт; НЕ записывать. |
| `--config` / `-c` | discovery | Путь к `config.yaml`. |

Что делает импорт:

1. Читает файл. Если зашифрован (magic-заголовок или расширение `.enc`)
   — читает пароль из переменной, названной `--passphrase-env` (fallback
   на `VESMARO_EXPORT_PASSPHRASE`).
2. Парсит JSON, проверяет `schema == "vesmaro.federation.v1"`, парсит
   каждую запись в `CompactRecord`.
3. Валидирует каждую запись (переиспользует #86 import validation —
   длина контента, tag contract, длина title, schema drift,
   prompt-injection warnings). При любой ошибке **весь батч
   отвергается** (без частичных записей).
4. Мержит идемпотентно по `id` записи
   (`fed:<source_agent>:<uuid>`): существующие записи **пропускаются**
   (не перезаписываются); новые создаются с `MemorySource.MCP`.

Сводка вывода:

```
✓ Imported: 11 records
  skipped: 1
  format_version: vesmaro.federation.v1
```

### Идемпотентность

Повторный импорт того же файла безопасен: каждая запись несёт
`fed:<source_agent>:<uuid>` id. Второй импорт находит каждую запись уже
существующей и пропускает — без дубликатов, без перезаписей. Это делает
cron-синхронизацию безопасной для повторных запусков.

---

## `scripts/sync-peers.sh` — cron-шаблон

Cron-ready shell-шаблон, объединяющий экспорт → перенос → импорт. Задай
переменные окружения и запусти. Без конфигурации не исполняется.

Обязательные переменные (при отсутствии любой скрипт завершается с кодом 2):

| Переменная | Назначение |
|------------|------------|
| `VESMARO_SYNC_PEER_HOST` | Host peer-узла B (цель). |
| `VESMARO_SYNC_PEER_SSH_KEY` | Приватный ключ ed25519 на A для rsync-отправки. |
| `VESMARO_SYNC_PEER_IMPORT_SSH_KEY` | Приватный ключ ed25519 на A для запуска импорта. |
| `VESMARO_SYNC_LOCAL_EXPORT_DIR` | Локальная директория, куда пишется экспорт. |
| `VESMARO_SYNC_REMOTE_IMPORT_DIR` | Директория на B, куда rsync доставляет payload. |
| `VESMARO_SYNC_SHARED_PROJECTS` | Slug'и проектов для синхронизации, через запятую. |
| `VESMARO_SYNC_ENCRYPT` | `true` / `false`. |
| `VESMARO_SYNC_PASSPHRASE_ENV` | ИМЯ переменной окружения с парольной фразой. |

Опциональные переменные:

| Переменная | По умолчанию | Назначение |
|------------|--------------|------------|
| `VESMARO_SYNC_PEER_USER` | `mnemos-sync` (легаси-дефолт) | ssh-пользователь на B. |
| `VESMARO_SYNC_DRY_RUN` | — | `1` — только логировать команды, без записей и ssh. |
| `VESMARO_SYNC_SOURCE_CONFIG` | discovery | Путь к `config.yaml` на A. |
| `VESMARO_SYNC_REMOTE_FILE` | `mnemos-sync-<ts>.json` (легаси-дефолт) | Имя файла payload на B. |
| `VESMARO_SYNC_MNEMOS_BIN` | auto-discover | Путь к CLI `vesma` на A (наследованное имя переменной, задаёт именно CLI `vesma`). |

Деплойменты до 5.0 (и старые `/etc/mnemos/sync.env`) задавали префикс
`MNEMOS_SYNC_*`; скрипт мапит каждый незаданный `VESMARO_SYNC_*` из его
`MNEMOS_SYNC_*`-парника — старые env-файлы работают до 6.0 без правки.

Путь к CLI `vesma` на B (`VESMARO_SYNC_REMOTE_MNEMOS_BIN` — тоже
наследованное имя переменной) задаётся на B в
`/etc/vesma/sync.env` — на A он не нужен, обёртка `vesma-import-wrapper` на B
находит бинарник сама. Парольная фраза никогда не передаётся в командной строке:
на A она читается из переменной, имя которой задано в `VESMARO_SYNC_PASSPHRASE_ENV`,
на B независимо прописывается в окружении systemd.

Пример crontab (почасовая зашифрованная синхронизация на peer-хост):

```cron
0 * * * * VESMARO_SYNC_PEER_HOST=peer.example.com \
          VESMARO_SYNC_PEER_SSH_KEY=/etc/vesma/sync_ed25519 \
          VESMARO_SYNC_PEER_IMPORT_SSH_KEY=/etc/vesma/sync_import_ed25519 \
          VESMARO_SYNC_LOCAL_EXPORT_DIR=/var/lib/vesma/sync \
          VESMARO_SYNC_REMOTE_IMPORT_DIR=/var/lib/vesma/incoming \
          VESMARO_SYNC_SHARED_PROJECTS="project-umbra,project-vesma" \
          VESMARO_SYNC_ENCRYPT=true VESMARO_SYNC_PASSPHRASE_ENV=VESMARO_EXPORT_PASSPHRASE \
          /opt/vesma/scripts/sync-peers.sh >> /var/log/vesma-sync.log 2>&1
```

Перенос — rsync поверх ssh, на B ограничен обёрткой `rsync-wrapper.sh`; запуск
импорта на B защищён обёрткой `vesma-import-wrapper.sh` (обе — в
`contrib/systemd/`). Тот же скрипт — это `ExecStart` юнита
`contrib/systemd/vesma-sync.service`, который подхватывает `/etc/vesma/sync.env`.

---

## Audit-лог

Каждый `vesma sync export` и `vesma sync import` дописывает одну
JSONL-запись в `~/.mnemos/logs/sync-audit.jsonl`. Лог append-only —
`tail -f` для мониторинга, `jq` для агрегатов, или отправка в SIEM.

Формат записей (только **счётчики** — без сырого контента, секретов, PII):

```json
{"timestamp": "2026-07-19T10:00:00Z", "action": "sync-export", "output": "/var/tmp/vesma-sync.json", "records_exported": 12, "records_refused": 1, "secrets_redacted": 3, "pii_anonymized": 2, "encrypted": false, "shared_projects": ["project-umbra", "project-vesma"]}
{"timestamp": "2026-07-19T10:05:00Z", "action": "sync-import", "source": "/var/tmp/vesma-sync.json", "records_imported": 11, "records_skipped": 1, "errors": [], "warnings": [], "encrypted": false, "format_version": "vesmaro.federation.v1"}
```

Audit-лог — операционный след: какие проекты синхронизировались, сколько
записей экспортировано / refused / redacted, какие импорты упали. **Сырой
контент и значения секретов никогда не попадают в audit-лог** — только
счётчики, пути и статус-флаги.

---

## Исключение `mnemos:no-federate`

Записи с тегом `mnemos:no-federate` целиком исключаются из экспорта
синхронизации. Тег автоматически добавляется при записи сканером Layer 1
(#86), когда детектируется секретный паттерн; владелец может снять его
с явным подтверждением через `MemoryManager.remove_no_federate()`. См.
[Tag Contract — `mnemos:no-federate`](./tag-contract.md#mnemosno-federate-маркер-исключения-из-федерации)
для полного lifecycle.

Даже без тега moderation-pipeline (Layer 3) прогоняет каждую запись при
экспорте и отказывает записям, чей контент почти полностью
secrets/PII — defence-in-depth, чтобы один пропущенный слой не утёк
секрет. См. [Security — Federation defence-in-depth](../admin/security.md#11-federation-defence-in-depth).
---

## См. также

- [Export & Import](./export-import.md) — полные бэкапы (JSON / SQLite).
- [Security — Federation defence-in-depth](../admin/security.md#11-federation-defence-in-depth) — трёхслойная модель.
- [Tag Contract — `mnemos:no-federate`](./tag-contract.md#mnemosno-federate-маркер-исключения-из-федерации) — маркер исключения.
- [MCP Tools](./mcp-tools.md) — `mnemos_export` / `mnemos_import` MCP-инструменты (MCP-поверхность для полного export/import).