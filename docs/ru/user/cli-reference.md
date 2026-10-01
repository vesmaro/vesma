# Справочник CLI

**🌐 Language / Язык:** [English](../../en/user/cli-reference.md) · Русский

> Полная справка по командной строке `vesma`.

CLI — тонкая обёртка на Typer вокруг [`MemoryManager`](../architecture/overview.md#memorymanager). Использует Rich для вывода таблиц с цветами и является наиболее удобным способом работы с Vesma из оболочки.

Полный набор субкоманд определён в `src/vesmaro/cli/main.py`. Эта страница отражает то, что реально экспортирует источник — каждый пример здесь можно выполнить на чистой установке.

Пошаговое первое использование — в [getting-started.md](getting-started.md). Для программного доступа — [mcp-tools.md](mcp-tools.md) и [http-api.md](http-api.md).

---

## Синопсис

```text
vesma [GLOBAL-OPTIONS] SUBCOMMAND [SUBCOMMAND-OPTIONS] [ARGS]
```

| Субкоманда | Назначение |
|------------|------------ |
| [`add`](#add) | Создать новую запись в памяти |
| [`search`](#search) | Гибридный поиск FTS5 + вектор |
| [`recall`](#recall) | Список последних записей, опционально по агенту / проекту |
| [`tags validate`](#tags-validate) | Проверить контракт тегов по всему vault |
| [`workflow`](#workflow) | Жизненный цикл записи: `get` / `set` / `history` |
| [`stats`](#stats) | Показать счётчики состояния |
| [`fts`](#fts) | Управление FTS5-индексом (`rebuild`) |
| [`processor`](#processor) | Управление фоновым конвейером: `status` / `run` / `start` / `stop` |
| [`reindex`](#reindex) | Переиндексация всех published-записей в векторном хранилище |
| [`filter`](#filter) | Запуск контекстного фильтра для записи |
| [`serve`](#serve) | Запустить HTTP API-сервер (FastAPI / Uvicorn) |
| [`mcp-server`](#mcp-server) | Запустить MCP stdio-сервер для VS Code Copilot |
| [`migrate from-ai-brain`](#migrate-from-ai-brain) | Однократный импорт из устаревшей установки `ai-brain` |
| [`auth`](#auth) | Bearer-токены (`auth token`) и TOTP 2FA (`auth totp`) |
| [`integration`](integration-guide.md) | Развёртывание / проверка слоя интеграции (отдельная страница) |
| [`completion`](#completion) | Установка shell-автодополнения (bash / zsh / fish) |
| [`doctor`](#doctor) | Диагностика установки (пути, конфиг, база, vault) |
| [`update`](#update) | Проверка обновлений / обновление user-site-установки |
| [`export`](export-import.md) | Экспорт записей в JSON / SQLite-бэкап (отдельная страница) |
| [`import`](export-import.md) | Импорт записей из файла экспорта (отдельная страница) |
| [`logs`](#logs) | Просмотр трассировок пайплайна |
| [`sync`](sync.md) | Пакетная federation-синхронизация: export / import (отдельная страница) |
| [`meta-poll`](#meta-poll) | Опрос метаданных федерации: один проход поллера вручную (S2 фаза 2) |
| [`scanner`](#scanner) | Фоновый сканер секретов: `run` / `status` |

> Группа `tags` также предоставляет `tags normalize` и `tags rename` (массовое переименование префиксов с dry-run); `migrate tags` — устаревший алиас для `vesma tags rename --from gcw: --to mnemos: --no-dry-run`. Префикс `mnemos:` в неймспейсе тегов — контракт данных, ребрендингом не изменяемый (решение 6.0) — переименования проектных неймспейсов его не затрагивают.

---

## Глобальные опции

Большинство субкоманд принимают флаг `--config / -c` с путём к YAML-файлу. Порядок поиска:

1. Аргумент `--config` (если указан)
2. Переменная окружения `$VESMARO_CONFIG` (5.x канон; написание 4.x `MNEMOS_CONFIG` устарело)
3. `./config.yaml` в текущей рабочей директории
4. `~/.mnemos/config.yaml`

```bash
vesma --help
vesma add --help
```

Остальные глобальные флаги — только `--version / -V` (показать версию) и `--verbose / -v` (DEBUG-логирование для `vesma serve` и `vesma mcp-server`). Чтобы изменить уровень логирования на постоянной основе, задайте `logging.level` в конфиге или переменную окружения:

```bash
VESMARO_LOGGING__LEVEL=DEBUG vesma serve      # 5.x канон
# образы 4.x всё ещё читают написание MNEMOS_LOGGING__LEVEL (deprecated)
```

---

## Переменные окружения

Все настройки переопределяются через переменные окружения с префиксом `VESMARO_` (канон 5.x). Вложенные ключи разделяются `__`.

| Переменная (канон 5.x) | По умолчанию | Назначение |
|------------|-------------|------------ |
| `VESMARO_CONFIG` | — | Путь к `config.yaml` |
| `VESMARO_MNEMOS__DATA_DIR` | `~/.mnemos/data` | БД SQLite + векторный индекс (каноническая форма) |
| `VESMARO_MNEMOS__VAULT_PATH` | `~/.mnemos/vault` | Директория зеркала Obsidian (каноническая форма) |
| `VESMARO_MNEMOS__STRICT_TAG_CONTRACT` | `true` | Соблюдение схемы тегов M2 |
| `VESMARO_API__HOST` | `127.0.0.1` | Адрес по умолчанию для `vesma serve` |
| `VESMARO_API__PORT` | `8787` | Порт по умолчанию для `vesma serve` |
| `VESMARO_SEARCH__HYBRID_ALPHA` | `0.5` | Вес вектора в RRF-слиянии |
| `VESMARO_EMBEDDING__PROVIDER` | `nano` | `nano` (vesma-embed-v1, встроенная) / `onnx` / `ollama` / `sentence-transformers` |
| `VESMARO_LLM__PROVIDER` | `ollama` | LLM для синтеза и контекстного фильтра |
| `VESMARO_LLM__MODEL` | `qwen2.5:3b` | Имя LLM-модели |
| `VESMARO_AUTO_COLLECT` | `0` | Установите `1` для включения режима auto-collect MCP |
| `VESMARO_LOGGING__LEVEL` | `INFO` | Уровень логирования Python |

> **Deprecated: написание MNEMOS_\*.** Таблица выше перечисляет канон для 5.x — имена `VESMARO_*` (контракт двойного префикса ADR-0031; образы 5.x читают `VESMARO_*`). Те же переменные на образах 4.x отгружались как `MNEMOS_CONFIG`, `MNEMOS_API__HOST`, `MNEMOS_API__PORT`, `MNEMOS_SEARCH__HYBRID_ALPHA`, `MNEMOS_EMBEDDING__PROVIDER`, `MNEMOS_LLM__PROVIDER`, `MNEMOS_LLM__MODEL`, `MNEMOS_AUTO_COLLECT`, `MNEMOS_LOGGING__LEVEL` и остаются принятыми там до deprecation. Короткие формы `VESMARO_DATA_DIR` / `VESMARO_VAULT__VAULT_PATH` — это совместимые алиасы #139 для вложенных канонических имён; при конфликте канон env побеждает.

> **Устаревшие алиасы.** Короткие формы появились до вложенного именования и сохранены для совместимости (#139). Работают обе формы. При конфликте каноническое имя переменной — как и явное значение в конфиг-файле — имеет приоритет над алиасом; алиас лишь заполняет пробел, который иначе достался бы значению по умолчанию.

---

## `add`

Создать новую запись в памяти.

```text
vesma add [CONTENT] [OPTIONS]
```

| Опция | По умолчанию | Описание |
|-------|-------------|---------- |
| `CONTENT` (позиционный) | — | Текст для сохранения. Если не указан, читается из stdin. |
| `--title / -t` | авто | Краткий заголовок. Автогенерируется из контента, если не указан. |
| `--tags / -T` | `""` | Теги через запятую (напр. `project:test,agent:me,mnemos:learning`). |
| `--file / -f` | — | Импортировать содержимое файла. Взаимоисключающее с `CONTENT` и `--url`. |
| `--url / -u` | — | Получить и сохранить URL. Требует тегов. |
| `--source / -s` | `cli` | Источник записи: `manual`, `web`, `file`, `mcp`, `obsidian`, `cli`, `rule`, `synthesized`. |
| `--type` | `note` | Тип записи: `note`, `fact`, `snippet`, `bookmark`, `conversation`, `session_context`. |
| `--dry-run` | `false` | Проверить теги и показать статистику контекстного фильтра без сохранения. |
| `--config / -c` | — | Путь к `config.yaml`. |

> **Контракт тегов.** Каждая запись должна иметь `project:<slug>`, `agent:<slug>` и хотя бы один `vesma:<subtype>`. CLI соблюдает это в strict-режиме (по умолчанию). Полная схема — в [tag-contract.md](tag-contract.md).

### Примеры

```bash
# Встроенный контент
vesma add "Use uv, not pip" --tags project:vesma agent:tech-writer mnemos:learning

# С заголовком
vesma add "Always validate SQL with parameterized queries" \
  --title "SQL safety rule" \
  --tags "project:vesma,agent:security,mnemos:rule,severity:high"

# Из файла
vesma add --file ~/notes/architecture.md --tags project:vesma agent:tech-lead mnemos:decision

# Из URL (загружает, извлекает, сохраняет)
vesma add --url https://example.com/article --tags project:research agent:user mnemos:learning

# Из stdin
echo "Pinned CVE-2026-45829 in chromadb 1.5.9" \
  | vesma add --tags project:vesma agent:sre mnemos:bug-pattern,severity:medium
```

---

## `search`

Гибридный поиск: FTS5 + вектор + Reciprocal Rank Fusion.

```text
vesma search QUERY [OPTIONS]
```

| Опция | По умолчанию | Описание |
|-------|-------------|---------- |
| `QUERY` (позиционный) | — | Строка поиска на естественном языке. |
| `--limit / -l` | `10` | Максимум результатов. |
| `--project / -p` | — | Ограничить одним проектом. |
| `--tags / -T` | — | Теги для фильтрации, через запятую. |
| `--include-raw / --published-only` | `--include-raw` | Включать записи `raw`/`processing` (по умолчанию) или ограничиться `published`. |
| `--status` | — | Фильтр по статусу (`raw`/`processing`/`processed`/`published`/`archived`); имеет приоритет над `--include-raw`. |
| `--config / -c` | — | Путь к `config.yaml`. |

Score — это слитый RRF-скор: 0.0 = нет совпадений, 1.0 = первое место. По умолчанию ищутся и сырые записи — только что добавленная запись остаётся `raw`, пока конвейер знаний её не опубликует; используйте `--published-only`, чтобы ограничить выдачу областью векторного индекса.

### Примеры

```bash
# Простой поиск
vesma search "embedding model"

# С фильтром по проекту
vesma search "CVE" --project vesma --limit 20

# Широкий поиск
vesma search "decision" --limit 50
```

Для более широких возможностей запросов используйте HTTP API `POST /search` (см. [http-api.md#search](http-api.md#post-search--гибридный-поиск)).

---

## `recall`

Список последних записей, опционально ограниченный агентом (M3) и/или проектом.

```text
vesma recall [OPTIONS]
```

| Опция | По умолчанию | Описание |
|-------|-------------|---------- |
| `--project / -p` | — | Slug проекта для фильтрации. |
| `--agent / -a` | — | Slug агента для фильтрации. Активирует per-agent recall M3. |
| `--limit / -l` | `10` | Максимум результатов. |
| `--config / -c` | — | Путь к `config.yaml`. |

Когда `--agent` передан **без** запроса, результат — N последних записей этого агента, упорядоченных по `created_at desc`. Это те же данные, которые возвращает MCP-инструмент [`mnemos_agent_recall`](mcp-tools.md#mnemos_agent_recall).

### Примеры

```bash
# 10 последних записей для любого агента
vesma recall

# Per-agent recall (M3)
vesma recall --agent tech-writer

# Комбинированный
vesma recall --agent sre --project vesma --limit 25
```

---

## `tags validate`

Проверить контракт тегов Vesma по всей существующей директории Vesma vault. Сообщает о записях, нарушающих схему M2.

```text
vesma tags validate VAULT_PATH
```

| Аргумент | Описание |
|----------|---------- |
| `VAULT_PATH` (позиционный) | Путь к директории Vesma vault (зеркало в markdown). |

> **Статус.** Полная реализация сканирования vault ещё не подключена (`# TODO (M2): scan SQLite + vault markdown files`). Пока команда выводит заглушку. Для проверки тегов через SQLite используйте `vesma stats` и HTTP API `GET /memories?project=...`.

### Пример

```bash
vesma tags validate ~/.mnemos/vault
```

---

## `workflow`

Управление жизненным циклом записи через автомат состояний, контролируемый на стороне сервера (`open`, `in-progress`, `blocked`, `resolved`, `done`, `withdrawn`). Автомат и его guardrail живут в `MemoryManager`; CLI лишь превращает нарушения в красную строку ошибки и код выхода 1.

### `workflow get`

Показать текущий статус workflow и владельца блокировки для записи.

```text
vesma workflow get MEMORY_ID
```

### `workflow set`

Перевести запись в новый статус workflow.

```text
vesma workflow set MEMORY_ID --to STATUS --actor ACTOR [OPTIONS]
```

| Опция | По умолчанию | Описание |
|-------|-------------|---------- |
| `MEMORY_ID` (позиционный) | — | Id целевой записи. |
| `--to` | — (обязательная) | Целевой статус: `open`, `in-progress`, `blocked`, `resolved`, `done`, `withdrawn`. |
| `--actor` | — (обязательная) | Свободный идентификатор актора (Phase 1 weak identity). |
| `--reason` | `""` | Человекочитаемая причина. Обязательна вместе с `--force`. |
| `--force` | `false` | Перекрыть блокировку другого актора (требует `--reason`). |
| `--config / -c` | — | Путь к `config.yaml`. |

### `workflow history`

Показать журнал переходов workflow для записи (новые сверху).

```text
vesma workflow history MEMORY_ID [OPTIONS]
```

| Опция | По умолчанию | Описание |
|-------|-------------|---------- |
| `MEMORY_ID` (позиционный) | — | Id целевой записи. |
| `--limit` | `50` | Максимум строк (новые сверху). |
| `--config / -c` | — | Путь к `config.yaml`. |

### Пример

```bash
ID=550e8400-e29b-41d4-a716-446655440000

vesma workflow set "$ID" --to in-progress --actor tech-writer
vesma workflow get "$ID"
vesma workflow history "$ID" --limit 20
```

### Связанные ресурсы

- MCP-инструмент: [`mnemos_workflow`](mcp-tools.md#mnemos_workflow)

---

## `stats`

Показать счётчики состояния Vesma и ключевые пути.

```text
vesma stats [OPTIONS]
```

| Опция | По умолчанию | Описание |
|-------|-------------|---------- |
| `--config / -c` | — | Путь к `config.yaml`. |

### Ключи вывода

| Ключ | Значение |
|------|--------- |
| `status` | Всегда `ok` (сигнал живости) |
| `version` | Версия Vesma (сейчас `4.0.0`) |
| `data_dir` | Разрешённая директория данных |
| `vault_path` | Разрешённая директория vault |
| `total` | Общее количество записей (любой статус) |
| `by_status` | Словарь `raw` / `processing` / `processed` / `published` / `archived` |
| `vectors` | Количество векторов в локальном векторном индексе (`vectors.db`) |

### Пример

```bash
vesma stats
# status: ok
# version: 4.0.0
# data_dir: /home/you/.vesma/data
# vault_path: /home/you/.vesma/vault
# total: 142
# by_status: {'raw': 5, 'processing': 0, 'processed': 12, 'published': 120, 'archived': 5}
# vectors: 120
```

---

## `fts`

Управление FTS5-индексом. Сейчас определено одно действие: `rebuild`.

```text
vesma fts ACTION
```

| Аргумент | Описание |
|----------|---------- |
| `ACTION` (позиционный) | `rebuild` — пересобрать FTS5-индекс и сообщить число проиндексированных строк. Любое другое значение завершается ошибкой. |

### Пример

```bash
vesma fts rebuild
# ✓ FTS5 index rebuilt: 142 rows indexed
```

---

## `processor`

Управление фоновым процессором (конвейером знаний): просмотр очереди, ручной проход, запуск и остановка фонового цикла.

```text
vesma processor ACTION
```

| Аргумент | Описание |
|----------|---------- |
| `ACTION` (позиционный) | `status` — глубина очереди, время последней обработки, флаг запуска. `run` — один синхронный проход конвейера (cluster → synthesize → quality gate → publish). `start` — запустить фоновый процессор. `stop` — остановить. |

Сводка `run` сообщает счётчики `clusters`, `synthesized`, `published` и `failed_quality_gate`.

### Пример

```bash
vesma processor run
#   clusters: 3
#   synthesized: 3
#   published: 2
#   failed_quality_gate: 1
```

### Связанные ресурсы

- HTTP-эквивалент: [`POST /process`](http-api.md#post-process--запустить-end-to-end-пайплайн)

---

## `reindex`

Пересобрать векторный индекс для всех published-записей — каждая запись `published` заново векторизуется и обновляется в `vectors.db`. Используйте после включения эмбеддингов или смены модели.

```text
vesma reindex [OPTIONS]
```

| Опция | По умолчанию | Описание |
|-------|-------------|---------- |
| `--batch-size / -b` | `100` | Размер батча эмбеддингов. |
| `--config / -c` | — | Путь к `config.yaml`. |

### Пример

```bash
vesma reindex --batch-size 50
#   total: 120
#   indexed: 120
#   failed: 0
```

---

## `filter`

Запустить контекстный фильтр (M10) для записи и показать очищенный контент со статистикой сокращения. С `--all` фильтр перезапускается для всех записей с агрегированной сводкой.

```text
vesma filter [MEMORY_ID] [OPTIONS]
```

| Опция | По умолчанию | Описание |
|-------|-------------|---------- |
| `MEMORY_ID` (позиционный) | — | Запись для фильтрации. Опустите при использовании `--all`. |
| `--profile / -p` | автоопределение | `log`, `terminal`, `code`, `docs`, `web` или `default`. |
| `--budget / -b` | — | Токенный бюджет для обрезки. |
| `--all` | `false` | Перезапустить фильтр для ВСЕХ записей; существующий `clean_content` перезаписывается свежим выводом фильтра. |
| `--config / -c` | — | Путь к `config.yaml`. |

> Повторная фильтрация с другим профилем даёт другой `clean_content`. Фильтр идемпотентен только при том же профиле.

### Пример

```bash
vesma filter 550e8400-e29b-41d4-a716-446655440000 --profile terminal
# ✓ Filtered: 550e8400-e29b-41d4-a716-446655440000
#   profile: terminal
#   clean_content:
#   ...
```

### Связанные ресурсы

- [context-filter.md](context-filter.md) — профили, этапы конвейера, автофильтр
- MCP-инструмент: [`mnemos_filter`](mcp-tools.md#mnemos_filter)

---

## `serve`

Запустить HTTP API-сервер Vesma (FastAPI / Uvicorn).

```text
vesma serve [OPTIONS]
```

| Опция | По умолчанию | Описание |
|-------|-------------|---------- |
| `--host` | `settings.api.host` (127.0.0.1) | Адрес привязки. |
| `--port` | `settings.api.port` (8787) | Порт привязки. |
| `--log-file` | — | Переопределить путь к лог-файлу из конфига; передача флага включает файловое логирование. |
| `--config / -c` | — | Путь к `config.yaml`. |

Сервер использует `uvicorn[standard]` (HTTP/1.1 + WebSockets). Количество воркеров берётся из `settings.runtime.uvicorn_workers`.

> **Безопасность.** Привязка по умолчанию — `127.0.0.1`. Не открывайте этот порт в публичную сеть без обратного прокси с аутентификацией. См. [security.md](../admin/security.md).

### Mesh-сервер (нативный wiring)

При `mesh.enabled: true` в конфиге `vesma serve` дополнительно поднимает gRPC-сервер `MnemosCore` на настроенном Unix-сокете **в том же процессе**, рядом с HTTP API — бинарий `mnemos-mesh` диалит этот сокет. На старте пишется одна строка: `mesh server listening on <path>`. По `SIGINT`/`SIGTERM` uvicorn сначала дрейнит HTTP, затем gRPC-сервер дрейнит (grace 2 с) и удаляет файл сокета.

```yaml
mesh:
  enabled: true
  socket_path: /run/vesma/core.sock
  # Групповой доступ для shared-volume деплоев (Kubernetes fsGroup,
  # compose `user: <uid>:<gid>`): сокет 0660 / каталог 0770 вместо
  # owner-only 0600 / 0700 — mesh-бинарий может диалить сокет под другим
  # uid в той же gid.
  socket_group_access: true
```

При `mesh.enabled: false` (по умолчанию) команда ведёт себя ровно как раньше — только uvicorn.

#### TCP-нога mesh (опционально, W2.5 dual-mode)

При `mesh.tcp.enabled: true` (требует `mesh.enabled: true`) **тот же** gRPC-сервер дополнительно слушает TCP с mesh-CA mTLS (ADR-0019): каждый вызывающий обязан предъявить клиентский сертификат, цепляющийся к mesh CA (`RequireAndVerifyClientCert`) — анонимный TLS отбивается на handshake. На старте пишется одна строка: `mesh tcp leg listening on <bind>:<port>`. Неудавшийся bind роняет процесс (fail-fast, ADR-0019 поправка 3c — k8s-probe на 8790 намеренно нет). По умолчанию `bind: 127.0.0.1` — Phase 1 (сайдкар); Phase 2 (standalone mesh) открывает `0.0.0.0` + ingress-правило NetworkPolicy как явное действие оператора.

```yaml
mesh:
  enabled: true
  tcp:
    enabled: true          # по умолчанию false — TCP-порта нет вовсе
    port: 8790             # 0 = эфемерный (только тесты/диагностика)
    bind: 127.0.0.1
    tls:
      # Ключ для чарта (ADR-0019 поправки 3d/3e): имя k8s Secret с листом
      # идентичности mnemos-core (mnemos-core-grpc-tls, общий mesh CA).
      # Для самого процесса информационное — читаются только файлы ниже.
      existing_secret: mnemos-core-grpc-tls
      # Смонтированные PEM-пути (из этого Secret) — контракт деплоя:
      cert_file: /etc/vesma/mesh-tls/tls.crt   # лист идентичности mnemos-core
      key_file: /etc/vesma/mesh-tls/tls.key
      ca_file: /etc/vesma/mesh-tls/ca.crt      # mesh CA — trust root клиентских серт
```

Опционально пинится fingerprint клиентского серта mesh-ноды на пира (`federation.peers.<id>.mtls_cert_fingerprint`, формат `sha256:<hex>` от DER-листа) — симметрично peer-ноге mesh; валидный mesh-CA серт от другой ноды тогда отклоняется с `PERMISSION_DENIED`.

### Примеры

```bash
# Привязка по умолчанию
vesma serve

# Привязка к локальной сети (dev-машина в домашней сети)
vesma serve --host 0.0.0.0 --port 8000

# С кастомным конфигом
vesma serve --host 127.0.0.1 --port 9000 --config /etc/vesma/config.yaml

# Включить файловое логирование без правки конфига
vesma serve --log-file ~/.mnemos/logs/serve.log
```

Полная поверхность HTTP API документирована в [http-api.md](http-api.md). Swagger UI доступен по адресу `http://HOST:PORT/docs`.

---

## `meta-poll`

Один проход опроса метаданных федерации (S2 фаза 2, poll-first). Это тот же путь, который фоновый цикл выполняет на каждом тике — но один раз, в foreground, со сводкой по пирам: для ручных запусков и диагностики.

```text
vesma meta-poll [OPTIONS]
```

| Опция | По умолчанию | Описание |
|-------|--------------|----------|
| `--peer` | все цели опроса | Опросить только этот пир (должен быть целью `meta_poll`). |
| `--config / -c` | — | Путь к `config.yaml`. |

По каждому пиру команда вызывает mesh-CLI (`mnemos-mesh sync-meta --config <mesh.yaml> --peer <id> --json [--since <rev>]`), парсит JSON-страницу и импортирует записи in-process через гейтовый upsert (`upsert_index_entries` с `sender_peer_id`): действуют no-federate-тег, title-блоклист, origin-guard и LWW-разрешение конфликтов. **Только метаданные**: путь опроса трогает `federation_index` и таблицу watermark'ов поллера, но никогда `memories` и пайплайн. Успешный проход печатает строку вида:

```text
✓ peer=mnemos-B fetched=12 accepted=10 rejected_by_gate=1 stale=1 pages=1 latest_rev=47
```

Код выхода `1`, если хотя бы один опрошенный пир упал (ненулевой exit CLI, битый JSON, таймаут) — ошибка фиксируется в `federation_poll_state.last_error` и повторяется на следующем проходе; watermark (`since_rev`) двигается только при успехе.

### Фоновый цикл (`federation.meta_poll`)

Фоновый поллер работает внутри `vesma serve` как asyncio-задача и **выключен по умолчанию** — конфиг без ключа `meta_poll` парсится без изменений, поведение процесса бит-в-бит как раньше (S1 / S2 фаза 1).

```yaml
federation:
  meta_poll:
    enabled: true                      # по умолчанию false — явный opt-in
    interval_seconds: 300              # по умолчанию 300; клэмпится в [60, 86400]
    peers: all                         # "all" (все ключи federation.peers) или явный список
    mesh_config_path: /etc/vesmaro/mesh.yaml  # ОБЯЗАТЕЛЕН при enabled (передаётся CLI как --config)
    mesh_bin: mnemos-mesh              # имя бинарника (PATH) или абсолютный путь
```

Примечания:

- `enabled: true` без `mesh_config_path` — ошибка конфига (fail-fast на старте). Явный список `peers` с неизвестным id — тоже ошибка конфига: опечатка не должна превращаться в молча пропущенный пир.
- Watermark по пиру персистится после каждой успешно импортированной страницы, поэтому рестарт (или падение) посреди пира возобновляется ровно после последней потреблённой строки; следующий запрос несёт `--since <watermark>`. Страницы с `has_more: true` chained-атся сразу (до 10 страниц на пира за тик).
- Ошибка одного пира никогда не останавливает цикл: она логируется на INFO, пишется в `federation_poll_state.last_error`, и пир повторяется на следующем тике (интервал и есть backoff).
- При `runtime.uvicorn_workers > 1` каждый worker-процесс держит свой поллер — импорты идемпотентны (LWW), так что это лишние запросы, но не порча данных; `serve` печатает предупреждение.

---

## `mcp-server`

Запустить MCP-сервер Vesma через **stdio** для VS Code Copilot (или любого MCP-совместимого клиента).

```text
vesma mcp-server [OPTIONS]
```

| Опция | По умолчанию | Описание |
|-------|-------------|---------- |
| `--config / -c` | — | Путь к `config.yaml`. |

Сервер говорит на JSON-RPC 2.0 через stdin/stdout. TCP-порт отсутствует. Процесс блокируется до EOF или `Ctrl+C`.

### Примеры

```bash
# Прямой вызов (для отладки)
vesma mcp-server

# С режимом auto-collect (4.x-написание env; 5.x: VESMARO_AUTO_COLLECT)
MNEMOS_AUTO_COLLECT=1 vesma mcp-server

# Из VS Code (сниппет mcp.json)
```

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

Полный список инструментов — в [mcp-tools.md](mcp-tools.md), подключение к VS Code — в [getting-started.md#run-the-mcp-server](getting-started.md#подключите-ваш-харнес-mcp).

---

## `migrate from-ai-brain`

Однократная миграция с устаревшей установки `ai-brain` (M13).

```text
vesma migrate from-ai-brain [OPTIONS]
```

| Опция | По умолчанию | Описание |
|-------|-------------|---------- |
| `--source` | `~/.ai-brain` | Директория данных устаревшего ai-brain (должна содержать `ai_brain.db`). |
| `--vault` | `~/brain-vault` | Vault устаревшего ai-brain (зеркало Obsidian). |
| `--dry-run` | `false` | Показать что будет мигрировано, без записи. |
| `--config / -c` | — | Путь к `config.yaml`. |

Мигратор:

- Преобразует устаревшие значения `source` (напр. `telegram` → `mcp`).
- **Патчит контракт тегов** — каждая устаревшая запись получает `project:legacy`, `agent:unknown`, `mnemos:legacy`, если они отсутствуют.
- Сохраняет исходный `status` (`raw` / `processing` / `processed` / `published` / `archived`).
- Мигрирует столбцы `content_ru` / `content_en` в `metadata` (без потери данных).
- Мигрирует `parent_ids` в `metadata.parent_ids`.

### Примеры

```bash
# Сначала dry run (рекомендуется)
vesma migrate from-ai-brain --dry-run

# Реальный запуск с путями по умолчанию
vesma migrate from-ai-brain

# Из восстановления архива
vesma migrate from-ai-brain --source /tmp/restore/.ai-brain --vault /tmp/restore/brain-vault
```

Вывод — однострочная сводка:

```text
✓ Memories migrated: 1 247
✓ Vault files migrated: 1 247
```

При наличии `Errors: N` список `summary.errors` (выводится в stderr на уровне DEBUG) укажет, какие строки упали. Как правило, это строки с повреждённой схемой — их можно игнорировать или исправить вручную в SQLite.

---

## `auth`

Управление API-токенами и TOTP 2FA (ADR-0014). Две подгруппы: `auth token` (bearer-токены) и `auth totp` (второй фактор). Секреты токенов хранятся хешированными в SQLite рядом с записями памяти.

### `auth token create`

Выпустить новый bearer-токен и показать его **один раз**.

| Опция | По умолчанию | Описание |
|-------|-------------|---------- |
| `--name / -n` | — | Человекочитаемая метка. |
| `--expires / -e` | — | Срок в ISO-8601, напр. `2027-01-01`. Даты без часового пояса нормализуются к UTC. |
| `--no-totp` | `false` | Создать токен, пригодный к использованию напрямую как bearer без flow login/verify/session (устанавливает `totp_required=false`). По умолчанию токены требуют TOTP. |
| `--config / -c` | — | Путь к `config.yaml`. |

### `auth token list`

Список всех токенов — только id и метаданные, никогда секреты.

### `auth token revoke TOKEN_ID`

Безвозвратно отозвать токен (позиционный аргумент `TOKEN_ID`).

### `auth totp`

| Субкоманда | Обязательные опции | Назначение |
|------------|--------------------|----------- |
| `enroll` | `--token-id` | Сгенерировать TOTP-секрет и вывести provisioning URI + ASCII QR (если доступен). Требует `MNEMOS_API__TOTP_MASTER_KEY` для шифрования секрета. |
| `disable` | `--token-id` | Удалить TOTP-секрет у токена (отключает 2FA для него). |
| `test` | `--token-id`, `--code` | Проверить 6-значный код против сохранённого секрета (smoke-тест для оператора). |

### Пример

```bash
vesma auth token create --name "laptop" --expires 2027-01-01
# ✓ Token created:
#   token_id : 7c9e6679-7425-40de-944b-e07fc1f90ae7
#   bearer   : <открытый токен — сохраните сейчас, повторно он не показывается>
```

---

## `completion`

Установить shell-автодополнение для CLI `vesma`. Без аргументов оболочка определяется автоматически из `$SHELL`, скрипт дополнения записывается в `~/.mnemos/completion/mnemos.<shell>`, а в rc-файл добавляется одна защищённая строка `source` (`~/.bashrc` / `~/.zshrc`; fish автоматически подхватывает свою директорию дополнений). Идемпотентно — повторный запуск не дублирует строку source и мигрирует со старого формата на `eval`.

```text
vesma completion [SHELL] [OPTIONS]
```

| Аргумент / опция | По умолчанию | Описание |
|------------------|--------------|---------- |
| `SHELL` (позиционный) | авто из `$SHELL` | `bash`, `zsh` или `fish`. |
| `--show-instructions` | `false` | Показать шаги ручной установки для всех поддерживаемых оболочек; файлы не изменяются. |

### Пример

```bash
vesma completion bash
# ✓ Installed bash completion → /home/you/.mnemos/completion/vesmaro.bash
#   Source line added to /home/you/.bashrc
#   Restart your shell or run: source /home/you/.bashrc
```

---

## `doctor`

Проверки состояния Vesma: конфигурация, директория данных, vault, БД SQLite, векторное хранилище, MCP-сервер, слой интеграции, подключение агентов, контракт тегов.

```text
vesma doctor [OPTIONS]
```

| Опция | По умолчанию | Описание |
|-------|-------------|---------- |
| `--json` | `false` | Вывести результаты в JSON (для скриптов / CI) вместо таблицы. |
| `--fix` | `false` | Автоматически исправлять проверки уровня WARN (устаревшая интеграция, неподключённые агенты, отсутствие MCP-регистрации). Проверки уровня FAIL автоматически не исправляются. |
| `--dry-run` | `false` | Вместе с `--fix`: показать, что было бы исправлено, без выполнения. |
| `--paths` | `false` | Вывести все разрешённые пути (data, vault, logs, cache, completion) и выйти. |

Коды выхода: `0` — все проверки пройдены, `1` — одна или несколько провалены, `2` — только предупреждения.

> У `doctor` нет опции `--config`; конфиг читается из `$VESMARO_CONFIG` (написание 4.x: `MNEMOS_CONFIG`, устарело) или стандартного пути поиска (`./config.yaml`, `~/.mnemos/config.yaml`).

### `doctor --paths`

Показывает все пути, которые использует Vesma, разрешённые из конфига и окружения:

```bash
vesma doctor --paths
# data_dir:      /home/you/.vesma/data
# vault_path:    /home/you/.vesma/vault
# log_file:      /home/you/.mnemos/logs/mnemos.log
# cache_dir:     /home/you/.vesma/cache
# completion:    /home/you/.vesma/completion
# config_file:   /home/you/.vesma/config.yaml
```

Используйте для проверки консолидированной структуры `~/.mnemos/` после обновления или миграции.

### `doctor --fix` и `--dry-run`

С `--fix` проверки уровня WARN исправляются на месте (устаревшая интеграция → `integration update`, неподключённые агенты → `vesma integration setup`, отсутствие MCP-регистрации → MCP setup); затем затронутые проверки запускаются повторно, и сообщается новый статус. Комбинация с `--dry-run` показывает предполагаемые исправления без их выполнения. `--json --fix` добавляет списки `fixed` / `fix_skipped` в JSON-вывод.

```bash
# Только предпросмотр
vesma doctor --fix --dry-run

# Применить исправления
vesma doctor --fix

# CI: машиночитаемый вердикт, без исправлений
vesma doctor --json
```

---

## `memory status`

Отчёт только для чтения о подключении памяти по каждому харнесу (ADR-0034,
MS-0). Ничего не пишет, в сеть не ходит; MCP-конфиги читаются только для
перечисления КЛЮЧЕЙ серверов (никогда — env-значений или командных строк),
маркеры хранилища показываются как существование + время изменения.

```text
vesma memory status [OPTIONS]
```

| Опция | Умолчание | Описание |
|-------|-----------|----------|
| `--target <имя>` | все обнаруженные | Сузить до конкретных харнесов; можно повторять. |
| `--home <каталог>` | `~` | Осмотреть альтернативный домашний каталог. |

По каждому харнесу в таблице: состояние пака (штампы: attached / stale /
missing), MCP-регистрация (есть ли запись сервера `vesma`), внешние движки
памяти (другие ключи серверов из конфига харнесса), маркеры локального
хранилища (`data`, `vault`, `mnemos.db` — существование + mtime) и активный
режим приоритета (`overlay+mirror`; `replace`/`off` появятся с MS-1).

### Пример

```bash
vesma memory status
# ┌─────────┬──────────────┬─────────┬──────────────────┬─────────────┬───────────────┐
# │ Харнес  │ Пак          │ MCP     │ Внешние движки   │ Хранилище   │ Приоритет     │
# │ zcode   │ attached (25)│ vesma ✓ │ obsidian-mcp     │ db ✓ 10:01  │ overlay+mirror│
```

Коды выхода: `0`, когда отчёт построен (это статусная поверхность, а не
гейт здоровья — здоровьем заведуют `doctor` и `integration verify`).

---

## `update`

Одна команда на всё семейство обновлений: отчёт по всем поверхностям обновления на этой машине, обновление user-site-установки pip (плюс глобальный npm-пакет, best-effort), фиксация версии для отката или управление недельным таймером авто-обновления.

```text
vesma update [OPTIONS]
```

| Опция | По умолчанию | Описание |
|-------|--------------|----------|
| `--check` | `false` | Только отчёт по поверхностям, без изменений (то же, что без флагов). |
| `--yes` | `false` | Выполнить обновление: `pip install --user --upgrade`; npm-пакет обновляется best-effort. |
| `--scope` | `user` | Область обновления. Существует только `user` — prod-venv'ы, Go-бинарники и контейнеры никогда не обновляются автоматически. |
| `--to <версия>` | — | Закрепить целевую версию pip (путь отката), например `--to 5.1.1`. Требует `--yes`. |
| `--install-timer` | `false` | Установить и включить недельный systemd user-таймер обновлений (`vesma-update.timer`, `Persistent=true`). |
| `--uninstall-timer` | `false` | Выключить и удалить таймер и его service-юнит. |

### Поверхности «только отчёт»

Отчёт по умолчанию перечисляет все поверхности обновления, найденные на этой машине. Изменяется только pip user-site (и npm); prod-venv'ы и Go-бинарники — report-only by design:

- **pip-дистрибутив** — поверхность, которую обновляет `--yes` (`--break-system-packages` добавляется автоматически под PEP 668 externally-managed-интерпретаторами); каждый запуск дописывает запись в `~/.local/share/vesma/update-history.json`.
- **npm `@vesmaro/vesma`** — обновляется best-effort с `--yes`, если установлен.
- **prod-venv'ы** — `MANUAL GATE` в отчёте; обновляются вручную по upgrade-runbook.
- **Go-бинарники** (`vesmaro-agent`/`vesma-agent`, `mnemos-mesh`/`vesma-mesh`) — обновляются через goreleaser-релизы с проверкой контрольных сумм.
- **контейнерные образы** — релизные артефакты CI.

### Пример

```bash
# Отчёт по всем поверхностям обновления
vesma update

# Обновить user-site-установку
vesma update --yes --scope=user

# Откатиться на закреплённую версию
vesma update --yes --to 5.1.1

# Недельное авто-обновление user-site (переживает перезагрузку)
vesma update --install-timer
```

После успешного обновления перезапустите работающих клиентов (MCP / `serve`), чтобы подхватить новую версию.

---

## `logs`

Просмотр трассировок пайплайна (M6, слой объяснимости) — компактная таблица поверх append-only таблицы `traces`.

```text
vesma logs [OPTIONS]
```

| Опция | По умолчанию | Описание |
|-------|-------------|---------- |
| `--task / -t` | — | Фильтр по метке задачи (`cluster`, `synthesize`, `publish`, `recall`). |
| `--project / -p` | — | Фильтр по slug проекта. |
| `--limit / -l` | `50` | Максимум показанных трассировок. |
| `--since` | — | Только трассировки после этой ISO-даты (напр. `2026-06-01`). |
| `--follow / -f` | `false` | Опрашивать новые строки раз в 2 с (в стиле `tail -f`). Останов — `Ctrl+C`. |
| `--config / -c` | — | Путь к `config.yaml`. |

### Пример

```bash
vesma logs --task cluster --project vesma --limit 20

# Наблюдать конвейер вживую
vesma logs --follow
```

### Связанные ресурсы

- HTTP-эквивалент: [`GET /traces`](http-api.md#get-traces--список-трассировок-пайплайна)

---

## `scanner`

Фоновый сканер секретов — слой 2 эшелонированной защиты federation. Сканер периодически пересканирует корпус на секреты, пропущенные при записи, и автоматически ставит совпадениям тег `mnemos:no-federate`, исключая их из любого внешнего обмена. Эти субкоманды — ручной запуск и просмотр состояния.

### `scanner run`

Синхронно выполнить один проход сканера и вывести сводку.

| Опция | По умолчанию | Описание |
|-------|-------------|---------- |
| `--full` | `false` | Принудительный полный проход по корпусу (игнорировать инкрементальную границу). По умолчанию проход инкрементальный: только записи, изменённые с прошлого успешного прохода. |
| `--config / -c` | — | Путь к `config.yaml`. |

Сводка сообщает `records_scanned`, `records_tagged`, `records_skipped`, `duration_sec`, имена сработавших шаблонов со счётчиками (никогда сами значения) и метку времени.

### `scanner status`

Показать текущее состояние сканера — enabled, running, настроенные интервал и инкрементальный режим, время последнего прохода, суммарное число помеченных записей, следующий плановый запуск.

### Пример

```bash
vesma scanner run --full
# ✓ Scan complete (full)
#   records_scanned: 142
#   records_tagged:   0
#   records_skipped:  2
#   duration_sec:     1.83
#   patterns_matched: (none)
#   timestamp:        2026-09-05T12:00:00+00:00
```

### Связанные ресурсы

- [sync.md](sync.md#исключение-vesmano-federate) — что исключает `mnemos:no-federate`

---

## Коды выхода

| Код | Значение |
|-----|--------- |
| 0 | Успех |
| 1 | Ошибка пользователя (отсутствующий аргумент, неверный тег и т.п.) |
| 2 | `vesma doctor`: предупреждения, ничего не сломано |

CLI не возвращает ненулевой код при «нет результатов» — `vesma search` завершается с кодом 0 и пустой таблицей.

---

## См. также

- [getting-started.md](getting-started.md) — первое использование
- [mcp-tools.md](mcp-tools.md) — те же возможности через MCP
- [http-api.md](http-api.md) — те же возможности через HTTP
- [context-filter.md](context-filter.md) — профили фильтра, используемые `add --dry-run` и `filter`
- [tag-contract.md](tag-contract.md) — схема тегов, соблюдаемая здесь
- [runbooks/migrate.md](../admin/runbooks/migrate.md) — операционное руководство по миграции
- [обзор архитектуры](../architecture/overview.md) — структура системы

---

_Последнее обновление: 2026-10-01_
