# Справочник MCP-инструментов

**🌐 Language / Язык:** [English](../../en/user/mcp-tools.md) · Русский

> Полная справка по инструментам `mnemos_*`, экспортируемым MCP-сервером Mnemos (`mnemos mcp-server`).

Mnemos говорит на [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) поверх **stdio JSON-RPC 2.0**. VS Code Copilot и любой MCP-совместимый клиент могут вызывать инструменты, перечисленные здесь.

Сервер определён в `src/mnemos/mcp_server.py`. Каждый инструмент регистрируется с помощью декоратора `@server.list_tools()` и диспетчеризируется функцией `call_tool()`.

Быстрое подключение к VS Code — в [getting-started.md#run-the-mcp-server](getting-started.md#подключите-ваш-харнес-mcp). Те же возможности доступны через HTTP — см. [http-api.md](http-api.md). Схема тегов, соблюдаемая большинством инструментов, — в [tag-contract.md](tag-contract.md).

---

## Транспорт

| Свойство | Значение |
|----------|--------- |
| Протокол | MCP (JSON-RPC 2.0 поверх stdio) |
| Имя сервера | `mnemos` |
| Транспорт по умолчанию | stdio (без TCP) |
| Префикс инструментов | `mnemos_` |
| Кодировка | UTF-8, JSON |

Сервер не занимает никакой порт. Остановить через `Ctrl+C` или отправкой EOF на stdin.

---

## Каталог инструментов (сводка)

| Инструмент | Назначение | Требует теги |
|------------|------------ |--------------|
| [`mnemos_add`](#mnemos_add) | Создать новую запись | да |
| [`mnemos_search`](#mnemos_search) | Гибридный поиск FTS + вектор | нет |
| [`mnemos_agent_recall`](#mnemos_agent_recall) | Per-agent recall (M3) | нет |
| [`mnemos_recall_context`](#mnemos_recall_context) | Восстановить контекст сессии для проекта | нет |
| [`mnemos_save_context`](#mnemos_save_context) | Сохранить контрольную точку сессии | нет (авто) |
| [`mnemos_list_recent`](#mnemos_list_recent) | Список последних записей | нет |
| [`mnemos_list_tags`](#mnemos_list_tags) | Список всех тегов с количеством | нет |
| [`mnemos_tags`](#mnemos_tags) *(пилот #97)* | Сгруппированные операции над тегами: rename / remove / add (`action: enum`) | нет |
| [`mnemos_tags_rename`](#mnemos_tags_rename) | Массовое переименование префиксов тегов (напр. `gcw:` → `mnemos:`); по умолчанию dry-run | нет |
| [`mnemos_workflow`](#mnemos_workflow) *(#96)* | Жизненный цикл workflow: set / get / history (`action: enum`) | нет |
| [`mnemos_ingest_url`](#mnemos_ingest_url) | Загрузить и сохранить веб-страницу | да |
| [`mnemos_watch_start`](#mnemos_watch_start) | Запустить фоновый file watcher | нет |
| [`mnemos_watch_stop`](#mnemos_watch_stop) | Остановить file watcher | нет |
| [`mnemos_watch_status`](#mnemos_watch_status) | Статус watcher'а | нет |
| [`mnemos_auto_collect_status`](#mnemos_auto_collect_status) | Вектор сигналов сжатия контекста (M7) | нет |
| [`mnemos_compress`](#mnemos_compress) | Обратимое сжатие (CCR) — кэш оригинала, маркер в вывод | нет |
| [`mnemos_retrieve`](#mnemos_retrieve) | Извлечение оригинала из кэша CCR или FTS5-сниппеты | нет |
| [`mnemos_align_prefix`](#mnemos_align_prefix) | CacheAligner — перенос динамического контента для стабильности prefix cache | нет |
| [`mnemos_filter`](#mnemos_filter) | Запуск / обновление контекстного фильтра для существующей записи (секреты сканируются) | нет |
| [`mnemos_assemble_context`](#mnemos_assemble_context) *(#125)* | ADR-0017 D1 — сборка контекстного блока перед вызовом LLM (recall → CCR → filter → scan → align → budget) | нет |
| [`mnemos_context_rewrite`](#mnemos_context_rewrite) *(#125)* | ADR-0018 — событие жизненного цикла `on_context_rewrite`: сообщить о перезаписи контекста, оригинал уходит в LTM (идемпотентно, без версий) | нет |
| [`mnemos_hooks`](#mnemos_hooks) *(#125)* | Хуки жизненного цикла ADR-0017 D1 / ADR-0018 — групповой инструмент `action:enum`: `pre_llm_call` / `on_session_start` / `post_tool_call` (автосжатие, opt-in) | нет |
| [`mnemos_awareness`](#mnemos_awareness) *(#254)* | Awareness-пре-флайт — наблюдаемая сервером активность соседей, дельта, подсказки о конфликтах и «операционная картина» swarm v0a (соседи в том же проекте: только счётчики/ids/времена) | нет |
| [`mnemos_export`](#mnemos_export) | Экспорт записей в файл (JSON или SQLite-снимок) | нет |
| [`mnemos_import`](#mnemos_import) | Импорт записей из файла экспорта (merge или restore) | нет |
| [`mnemos_reprocess`](#mnemos_reprocess) | Ручной запуск конвейера знаний для записей в очереди raw/processing | нет |
| [`mnemos_stats`](#mnemos_stats) | Счётчики состояния и ключевые пути | нет |

---

## `mnemos_add`

Создать новую запись в памяти. MCP-слой применяет контракт тегов Mnemos ([M2](tag-contract.md)) перед записью.

### Входные параметры

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `content` | string | **да** | — | Текст для запоминания. |
| `title` | string | нет | авто | Краткий заголовок. |
| `tags` | string[] | **да** | — | Должны включать `project:<slug>`, `agent:<slug>` и хотя бы один `mnemos:<subtype>`. |
| `memory_type` | string | нет | `note` | Одно из `note`, `fact`, `snippet`, `bookmark`, `conversation`. |
| `filter_profile` | string | нет | авто | Одно из `log`, `terminal`, `code`, `docs`, `web`, `default`. Управляет контекстным фильтром M10. |
| `verbosity` | string | нет | из конфига | Одно из `default`, `terse`, `minimal`. Вставляет подсказку по стилю вывода во framing результата. См. [Сокращение токенов вывода (P1-7)](#сокращение-токенов-вывода-p1-7). |
| `effort` | string | нет | из конфига | Одно из `low`, `medium`, `high`. Вставляет подсказку по уровню размышлений во framing результата. См. [Сокращение токенов вывода (P1-7)](#сокращение-токенов-вывода-p1-7). |

### Вывод

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "title": "Use uv, not pip",
  "status": "raw"
}
```

### Пример вызова (JSON-RPC)

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "mnemos_add",
    "arguments": {
      "content": "Use uv, not pip",
      "tags": ["project:mnemos", "agent:tech-writer", "mnemos:learning"]
    }
  }
}
```

### Ошибки

| Ошибка | Причина |
|--------|-------- |
| `❌ Tag contract violation: ...` | Отсутствует тег `project:`, `agent:` или `mnemos:`. |
| `❌ Error: ...` | Сбой записи SQLite, vault или ошибка эмбеддинга (последняя не критична — см. [обзор архитектуры](../architecture/overview.md#1-хранилище-storage-layer)). |

### Связанные ресурсы

- Схема тегов: [tag-contract.md](tag-contract.md)
- HTTP-эквивалент: [`POST /memories`](http-api.md#post-memories--создать-запись-create-memory)
- CLI-эквивалент: [`mnemos add`](cli-reference.md#add)

---

## `mnemos_search`

Гибридный поиск: FTS5 (полнотекстовый) + вектор + Reciprocal Rank Fusion. По умолчанию ищет только среди `published`-записей.

**Семантика запроса:** FTS5-ветка трактует ВСЮ строку `query` как одну цитированную фразу (токены подряд, в порядке следования — `_build_fts_query` заключает весь ввод в кавычки). Запрос из набора ключевых слов вида `postgres migration` найдёт только точную фразу; чтобы найти отдельные ключевые слова, делайте отдельные запросы по одному термину.

### Входные параметры

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `query` | string | **да** | — | Строка поиска на естественном языке. Матчится FTS5-веткой как ОДНА целая фраза — см. «Семантика запроса» выше. |
| `tags` | string[] | нет | — | Фильтр: все эти теги должны присутствовать. |
| `project` | string | нет | — | Ограничить проектом. |
| `limit` | integer | нет | `10` | Максимум результатов. |
| `include_raw` | boolean | нет | `false` | Если true, возвращает `raw_content` вместо очищенного `content`. |
| `verbosity` | string | нет | из конфига | Одно из `default`, `terse`, `minimal`. Вставляет подсказку по стилю вывода во framing результата. См. [Сокращение токенов вывода (P1-7)](#сокращение-токенов-вывода-p1-7). |
| `effort` | string | нет | из конфига | Одно из `low`, `medium`, `high`. Вставляет подсказку по уровню размышлений во framing результата. См. [Сокращение токенов вывода (P1-7)](#сокращение-токенов-вывода-p1-7). |

### Вывод

```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "title": "Use uv, not pip",
    "content": "Use uv, not pip — it's faster and resolves transitive CVE closure correctly.",
    "tags": ["project:mnemos", "agent:tech-writer", "mnemos:learning"],
    "score": 0.812,
    "search_type": "hybrid",
    "status": "published"
  }
]
```

### Пример вызова

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "mnemos_search",
    "arguments": {
      "query": "how to manage Python dependencies",
      "limit": 5,
      "project": "mnemos"
    }
  }
}
```

### Ошибки

- `❌ Error: ...` — сбой парсинга запроса (редко; обычно завершается с пустым результатом).

### Связанные ресурсы

- HTTP-эквивалент: [`POST /search`](http-api.md#post-search--гибридный-поиск)
- CLI-эквивалент: [`mnemos search`](cli-reference.md#search)

---

## `mnemos_agent_recall`

Per-agent recall (M3). Возвращает последние записи одного агента, опционально фильтруя по проекту и/или подзапросу.

### Входные параметры

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `agent` | string | **да** | — | Slug агента, напр. `cr-security-reviewer`. |
| `project` | string | нет | — | Ограничить проектом. |
| `query` | string | нет | — | Опциональный FTS/векторный запрос в рамках агента. |
| `limit` | integer | нет | `20` | Максимум записей. |

При отсутствии `query` инструмент возвращает последние записи (по убыванию времени). При наличии `query` выполняет гибридный поиск в рамках тегов агента.

### Вывод

```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "title": "Bandit B608 hardcoded SQL — flag for triage",
    "content": "Found hardcoded SQL in src/legacy/loader.py:42 ...",
    "tags": ["project:mnemos", "agent:cr-security-reviewer", "mnemos:bug-pattern"],
    "created_at": "2026-06-15T10:42:00+00:00",
    "status": "published"
  }
]
```

### Пример вызова

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "mnemos_agent_recall",
    "arguments": {
      "agent": "cr-security-reviewer",
      "project": "mnemos",
      "query": "bandit SQL injection",
      "limit": 10
    }
  }
}
```

### Ошибки

- Нет типичных. Возвращает пустой массив при отсутствии совпадений.

### Связанные ресурсы

- HTTP-эквивалент: [`GET /recall/agent/{name}`](http-api.md#get-recallagentname--отзыв-агента)
- CLI-эквивалент: [`mnemos recall --agent <slug>`](cli-reference.md#recall)

---

## `mnemos_recall_context`

Восстановить последнюю контрольную точку сессии для проекта. **Первое**, что агент должен вызвать при старте сессии, особенно после сжатия контекста.

### Входные параметры

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `project` | string | нет | авто (cwd) | Имя проекта. Автоопределяется из текущей директории, если не указано. |
| `query` | string | нет | — | Опциональный фокусный аспект. |
| `verbosity` | string | нет | из конфига | Одно из `default`, `terse`, `minimal`. Вставляет подсказку по стилю вывода во framing результата. См. [Сокращение токенов вывода (P1-7)](#сокращение-токенов-вывода-p1-7). |
| `effort` | string | нет | из конфига | Одно из `low`, `medium`, `high`. Вставляет подсказку по уровню размышлений во framing результата. См. [Сокращение токенов вывода (P1-7)](#сокращение-токенов-вывода-p1-7). |

### Вывод

Блок простого текста в формате Markdown:

```text
# Context for project 'mnemos'

---
# Session checkpoint — 2026-06-15T10:42:00+00:00

## Goals
Ship M15 production hardening.
## Completed
bandit clean, mypy --strict green
## In Progress
pip-audit CVE-2026-45829 ignore
## Decisions
Pin chromadb 1.5.9 with audit
## Context
Active files: src/mnemos/manager.py, src/mnemos/api/main.py
```

Если контрольная точка не найдена:

```text
No context found for project 'mnemos'. Start by saving context with mnemos_save_context.
```

В **режиме auto-collect** (`MNEMOS_AUTO_COLLECT=1`) к выводу добавляется блок `## 🔄 Auto-Collect Mode Active` с обязательными правилами сессии.

### Пример вызова

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "tools/call",
  "params": {
    "name": "mnemos_recall_context",
    "arguments": { "project": "mnemos" }
  }
}
```

### Связанные ресурсы

- `mnemos_save_context` — парный инструмент записи
- [architecture.md](../architecture/overview.md)
- HTTP-эквивалент: [`POST /context/recall`](http-api.md#post-contextrecall--отозвать-контекст-сессии)

---

## `mnemos_save_context`

Сохранить контрольную точку сессии. Агенты должны вызывать это **превентивно**: после значимой работы, перед переключением задач или при большом размере контекста.

### Входные параметры

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `project` | string | нет | авто (cwd) | Имя проекта. |
| `goals` | string | нет | — | Текущие цели сессии. |
| `completed` | string | нет | — | Что завершено. |
| `in_progress` | string | нет | — | Что в процессе. |
| `decisions` | string | нет | — | Ключевые технические решения + обоснование. |
| `context` | string | нет | — | Прочий контекст (пути к файлам, архитектура, особенности). |
| `agent` | string | нет | `"user"` | Идентичность агента для чекпойнта — валидируемый канал идентичности (при указании — непустая строка, из одних пробелов отклоняется). Должен совпадать с серверной привязкой session→agent, если передан `session`. |
| `session` | string | нет | — | Идентификатор сессии, привязывающий чекпойнт к разговору. Первое предъявление фиксирует привязку session→agent на сервере; последующие вызовы с той же сессией, но другим агентом отклоняются. |

Mnemos синтезирует части в единую запись Markdown с тегами `project:<slug>`, `agent:<валидированный агент>` (`agent:user`, если не указан) и `mnemos:checkpoint`. Валидированная идентичность дополнительно штампуется в серверные метаданные (`checkpoint_agent`, `checkpoint_session`) — именно они являются источником истины для атрибуции по агентам; теги носят демонстрационный характер.

Чекпойнт с пятью пустыми полями тривиально отклоняется до любого сохранения (zero-loss: вызывающий получает отказ, ничего не отбрасывается молча). Повторная отправка идентичной полезной нагрузки того же `(project, agent)` идемпотентна: возвращается id существующей записи с `duplicate=true`, ничего нового не создаётся.

### Вывод

```text
✅ Context saved (id=550e8400-...).
✅ Duplicate checkpoint (id=550e8400-..., duplicate=true) — identical checkpoint already stored, nothing new created.
```

### Пример вызова

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "tools/call",
  "params": {
    "name": "mnemos_save_context",
    "arguments": {
      "project": "mnemos",
      "goals": "Finish M15.1 mypy --strict",
      "completed": "Added None checks in 12 functions",
      "in_progress": "tests/test_api.py:241 type narrowing",
      "decisions": "Use cast() sparingly, prefer TypeGuard"
    }
  }
}
```

### Связанные ресурсы

- `mnemos_recall_context` — парный инструмент чтения
- Режим auto-collect: [mcp-tools.md#режим-auto-collect](#режим-auto-collect)
- HTTP-эквивалент: [`POST /context/save`](http-api.md#post-contextsave--сохранить-чекпойнт-сессии)

---

## `mnemos_list_recent`

Список последних записей в памяти, старые — последними.

### Входные параметры

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `limit` | integer | нет | `10` | Максимум записей. |
| `tags` | string[] | нет | — | Фильтр: хотя бы один из этих тегов должен присутствовать. |
| `project` | string | нет | — | Ограничить проектом. |

### Вывод

```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "title": "Use uv, not pip",
    "tags": ["project:mnemos", "agent:tech-writer", "mnemos:learning"],
    "status": "raw",
    "created_at": "2026-06-15T10:42:00+00:00"
  }
]
```

### Пример вызова

```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "method": "tools/call",
  "params": {
    "name": "mnemos_list_recent",
    "arguments": { "limit": 20, "project": "mnemos" }
  }
}
```

### Связанные ресурсы

- HTTP-эквивалент: [`GET /memories`](http-api.md#get-memories--список-последних)
- CLI-эквивалент: [`mnemos recall`](cli-reference.md#recall)

---

## `mnemos_list_tags`

Список всех тегов в памяти с количеством вхождений.

### Входные параметры

Отсутствуют.

### Вывод

```json
{
  "project:mnemos": 142,
  "agent:tech-writer": 23,
  "agent:sre": 41,
  "mnemos:learning": 67,
  "mnemos:bug-pattern": 12,
  "mnemos:decision": 8,
  "mnemos:checkpoint": 14
}
```

### Пример вызова

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "tools/call",
  "params": { "name": "mnemos_list_tags", "arguments": {} }
}
```

### Связанные ресурсы

- HTTP-эквивалент: [`GET /tags`](http-api.md#get-tags--список-тегов-с-количеством)

---

## `mnemos_tags`

Сгруппированные массовые операции над тегами: переименование префикса, удаление или добавление тегов. Диспетчеризация по действию (`action: enum`) — сгруппированный пилотный инструмент (#97); каждое действие идёт через один и тот же безопасный путь записи (обычный `UPDATE`, FTS5-индекс остаётся консистентным), по умолчанию работает как предпросмотр (`dry_run: true`) и идемпотентно.

### Входные параметры

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|--------------|---------- |
| `action` | string | **да** | — | `rename`, `remove` или `add`. |
| `from_prefix` | string | для `rename` | — | Исходный префикс, напр. `gcw:`. Должен заканчиваться на `:`. |
| `to_prefix` | string | для `rename` | — | Целевой префикс, напр. `mnemos:`. Должен заканчиваться на `:`. |
| `tags` | string[] | для `remove` / `add` | — | Теги для удаления или добавления. Обязательны для этих двух действий. |
| `subtypes` | string[] | нет | — | Опциональный белый список подтипов для переименования (только `rename`). |
| `wildcard` | boolean | нет | `false` | Только `remove`: считать каждый элемент `tags` префиксом и снимать все совпадающие теги `prefix*` вместо точного совпадения. `rename` построен на префиксах по своей природе. |
| `dry_run` | boolean | нет | `true` | Предпросмотр без записи. |
| `project` | string | нет | — | Ограничить сканирование slug проекта. |
| `agent` | string | нет | — | Ограничить сканирование slug агента. |
| `invalid_subtypes_to_legacy` | boolean | нет | `false` | Только `rename`: переименовывать невалидные подтипы в `<to_prefix>legacy` вместо пропуска. |

> **Безопасность контракта.** Итоговый набор тегов повторно валидируется в strict-режиме для каждой записи: снятие последнего тега `project:` / `agent:` / `mnemos:` (или иное нарушение контракта) отклоняется по конкретной записи с записью в `errors`, а не портит хранилище.

### Вывод

Словарь-отчёт. `changed` считает записи, чей набор тегов реально изменился; `renamed` сохранён для обратной совместимости с вызывающими `mnemos_tags_rename` и зеркалит `changed`:

```json
{
  "action": "remove",
  "scanned": 142,
  "changed": 9,
  "removed_tags": ["severity:high"],
  "wildcard": false,
  "errors": [],
  "dry_run": true
}
```

`rename` возвращает `{"from_prefix", "to_prefix", "scanned", "renamed", "changed", "skipped_invalid", "errors", "dry_run"}`; `add` возвращает `{"action", "scanned", "changed", "added_tags", "errors", "dry_run"}`.

### Пример вызова

```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "method": "tools/call",
  "params": {
    "name": "mnemos_tags",
    "arguments": {
      "action": "rename",
      "from_prefix": "gcw:",
      "to_prefix": "mnemos:",
      "dry_run": false
    }
  }
}
```

### Ошибки

| Ошибка | Причина |
|--------|-------- |
| `unknown action '<x>'...` | `action` не равен `rename` / `remove` / `add`. |
| `action='rename' requires 'from_prefix' and 'to_prefix' ...` | Для rename не переданы префиксы. |
| `tags must be a non-empty list` | `remove` / `add` вызван с пустым списком `tags`. |

### Связанные ресурсы

- Группированный родственник: [`mnemos_tags_rename`](#mnemos_tags_rename) — legacy-алиас для `action: "rename"`

---

## `mnemos_tags_rename`

Массовое переименование тегов `from_prefix:<subtype>` → `to_prefix:<subtype>` по всем существующим записям. Сохранён как **неразрывающий алиас**: вызовы диспетчеризируются в тот же путь rename, что и у [`mnemos_tags`](#mnemos_tags) с `action: "rename"` (случайный ключ `action` в аргументах игнорируется). Безопасно — переименование идёт через обычный `UPDATE`, поэтому FTS5 external-content индекс остаётся консистентным, — и идемпотентно: повторный запуск с теми же аргументами переименовывает 0 записей.

### Входные параметры

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|--------------|---------- |
| `from_prefix` | string | **да** | — | Исходный префикс, напр. `gcw:`. Должен заканчиваться на `:`. |
| `to_prefix` | string | **да** | — | Целевой префикс, напр. `mnemos:`. Должен заканчиваться на `:`. |
| `subtypes` | string[] | нет | — | Опциональный белый список подтипов для переименования. |
| `dry_run` | boolean | нет | `true` | Предпросмотр без записи. |
| `project` | string | нет | — | Ограничить slug проекта. |
| `agent` | string | нет | — | Ограничить slug агента. |
| `invalid_subtypes_to_legacy` | boolean | нет | `false` | Переименовывать невалидные подтипы в `<to_prefix>legacy` вместо пропуска. |

### Вывод

```json
{
  "from_prefix": "gcw:",
  "to_prefix": "mnemos:",
  "scanned": 142,
  "renamed": 0,
  "changed": 0,
  "skipped_invalid": 3,
  "errors": [],
  "dry_run": true
}
```

### Пример вызова

```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "method": "tools/call",
  "params": {
    "name": "mnemos_tags_rename",
    "arguments": {
      "from_prefix": "gcw:",
      "to_prefix": "mnemos:",
      "invalid_subtypes_to_legacy": true
    }
  }
}
```

### Связанные ресурсы

- Группированный инструмент: [`mnemos_tags`](#mnemos_tags) — `action: "rename"` — тот же путь кода
- HTTP-эквивалент: `POST /tags/rename` (реализован в API; пока не описан в [http-api.md](http-api.md))

---

## `mnemos_ingest_url`

Загрузить веб-страницу, извлечь основной контент (через `trafilatura`) и сохранить как запись.

### Входные параметры

| Поле | Тип | Обязательное | Описание |
|------|-----|--------------|---------- |
| `url` | string | **да** | HTTP / HTTPS URL для загрузки. |
| `tags` | string[] | **да** | Тот же контракт M2, что и в `mnemos_add`. |

> **Защита от SSRF.** MCP-слой удаляет `user:password@` из authority URL перед загрузкой (глубокая защита совместно с внутрипроцессной защитой). Не обходите это, собирая URL из строки.

### Вывод

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "title": "How to manage Python dependencies",
  "url": "https://example.com/article"
}
```

### Пример вызова

```json
{
  "jsonrpc": "2.0",
  "id": 8,
  "method": "tools/call",
  "params": {
    "name": "mnemos_ingest_url",
    "arguments": {
      "url": "https://example.com/article",
      "tags": ["project:research", "agent:user", "mnemos:learning"]
    }
  }
}
```

### Ошибки

| Ошибка | Причина |
|--------|-------- |
| `❌ Error: ...` | Сетевой сбой, заблокированный URL (защита от SSRF) или ошибка извлечения `trafilatura`. |

### Связанные ресурсы

- CLI-эквивалент: [`mnemos add --url <URL>`](cli-reference.md#add)
- HTTP-эквивалент: [`POST /memories` с ручным контентом](http-api.md#post-memories--создать-запись-create-memory)
- HTTP-эквивалент: [`POST /ingest-url`](http-api.md#post-ingest-url--получить-и-сохранить-веб-страницу)
- Безопасность: [security.md](../admin/security.md#2-защита-от-ssrf-memorymanager_validate_url)

---

## `mnemos_watch_start`

Запустить фоновый file watcher. Новые и изменённые файлы по отслеживаемым путям автоматически индексируются в Mnemos.

### Входные параметры

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `paths` | string[] | нет | `[cwd]` | Директории для наблюдения. |
| `scan` | boolean | нет | `true` | Выполнить начальное сканирование для обработки существующих файлов. |
| `include_rules` | boolean | нет | `false` | Также отслеживать `.github/instructions/*.instructions.md` (правила M8). |

### Вывод

```text
✅ Watcher started on ['/home/you/project']
# или, с include_rules:
✅ Watcher started on ['/home/you/project'] (including .instructions.md rules)
```

### Пример вызова

```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "method": "tools/call",
  "params": {
    "name": "mnemos_watch_start",
    "arguments": {
      "paths": ["/home/you/mnemos", "/home/you/notes"],
      "include_rules": true
    }
  }
}
```

### Примечания

- Лимит размера файла — `watcher.max_file_size_kb` (по умолчанию 512 КБ); файлы больше пропускаются.
- Игнорируемые директории по умолчанию: `.git`, `node_modules`, `__pycache__`, `.venv`, `dist`, `build`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`.
- Отслеживаемые расширения по умолчанию: `.md`, `.py`, `.js`, `.ts`, `.yaml`, `.yml`, `.toml`, `.json`, `.txt`, `.rst`, `.sh`, `.css`, `.html`, `.sql`.

### Связанные ресурсы

- HTTP-эквивалент: [`POST /watch/start`](http-api.md#post-watchstart--запустить-файловый-наблюдатель)

---

## `mnemos_watch_stop`

Остановить фоновый file watcher.

### Входные параметры

Отсутствуют.

### Вывод

```text
✅ Watcher stopped.
```

### Связанные ресурсы

- HTTP-эквивалент: [`POST /watch/stop`](http-api.md#post-watchstop--остановить-файловый-наблюдатель)

---

## `mnemos_watch_status`

Сообщить текущее состояние фонового watcher'а.

### Входные параметры

Отсутствуют.

### Вывод

```json
{
  "running": true,
  "paths": ["/home/you/mnemos"],
  "files_queued": 3,
  "files_indexed": 142,
  "include_rules": false
}
```

### Связанные ресурсы

- HTTP-эквивалент: [`GET /watch/status`](http-api.md#get-watchstatus--статус-наблюдателя)

---

## `mnemos_auto_collect_status`

Вернуть текущий вектор сигналов обнаружения сжатия контекста (M7). Агент читает это для принятия превентивного решения о вызове `mnemos_save_context`.

### Входные параметры

Отсутствуют.

### Вывод

```json
{
  "auto_collect_enabled": false,
  "signals": {
    "call_counter": {
      "calls_since_save": 7,
      "threshold": 12,
      "triggered": false
    },
    "elapsed_secs": {
      "value": 312,
      "threshold": 900,
      "triggered": false
    },
    "context_size_heuristic": {
      "value": null,
      "note": "populated by client (M7)"
    },
    "summary_marker_detected": {
      "value": null,
      "note": "populated by client (M7)"
    },
    "reference_drop_heuristic": {
      "value": null,
      "note": "populated by client (M7)"
    }
  },
  "recommendation": "ok",
  "next_reminder_in_calls": 5
}
```

Поле `recommendation` принимает одно из значений:

| Значение | Значение |
|----------|--------- |
| `ok` | Контрольная точка пока не нужна. |
| `save_checkpoint` | Сохранить сейчас — достигнут или превышен порог. |

### Режим auto-collect

Установите `MNEMOS_AUTO_COLLECT=1` в окружении сервера. Пороги напоминаний ужесточаются:

| Настройка | Обычный | Auto-collect |
|-----------|---------|--------------|
| Вызовов с момента сохранения | 12 | 6 |
| Прошедших секунд | 900 (15 мин) | 480 (8 мин) |

Описания инструментов также меняются (с префиксами `🔄 [AUTO-COLLECT] MANDATORY:`), чтобы агенты серьёзнее воспринимали подсказки. **Рекомендуется для продакшн-агентов**, не для одноразовых скриптов.

### Связанные ресурсы

- HTTP-эквивалент: [`GET /auto-collect`](http-api.md#get-auto-collect--вектор-сигналов-компакции)

---

## `mnemos_stats`

Вернуть счётчики состояния Mnemos.

### Входные параметры

Отсутствуют.

### Вывод

Та же структура, что и у команды CLI `mnemos stats` — см. [cli-reference.md#stats](cli-reference.md#stats).

```json
{
  "status": "ok",
  "version": "4.0.0",
  "data_dir": "/home/you/.mnemos/data",
  "vault_path": "/home/you/.mnemos/vault",
  "total": 142,
  "by_status": {"raw": 5, "processing": 0, "processed": 12, "published": 120, "archived": 5},
  "vectors": 120
}
```

### Связанные ресурсы

- HTTP-эквивалент: [`GET /metrics`](http-api.md#get-metrics)
- CLI-эквивалент: [`mnemos stats`](cli-reference.md#stats)

---

## `mnemos_reprocess`

Ручной запуск конвейера знаний для обработки очереди записей `raw` / `processing` в `published` знание: cluster → synthesize → quality gate → publish. Используйте, когда `mnemos_stats` показывает большую `queue_depth`, или после массового импорта.

### Входные параметры

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|--------------|---------- |
| `project` | string | нет | — | Ограничить проход slug проекта. |
| `agent` | string | нет | — | Ограничить проход slug агента. |
| `limit` | integer | нет | `100` | Максимум рассматриваемых записей. |

### Вывод

Словарь-сводка конвейера:

```json
{
  "clusters": 3,
  "synthesized": 3,
  "published": 5,
  "failed_quality_gate": 1,
  "single_promoted": 2,
  "stuck_rescued": 0,
  "published_ids": ["550e8400-e29b-41d4-a716-446655440000"],
  "refined": 4,
  "refined_noop": 1,
  "refine_failed": 0,
  "quarantined": 0
}
```

Записи, не образовавшие кластер, продвигаются поодиночке (`single_promoted`), поэтому очередь разбирается даже при уникальности большинства записей.

### Пример вызова

```json
{
  "jsonrpc": "2.0",
  "id": 11,
  "method": "tools/call",
  "params": {
    "name": "mnemos_reprocess",
    "arguments": { "project": "mnemos", "limit": 200 }
  }
}
```

### Связанные ресурсы

- HTTP-эквивалент: [`POST /process`](http-api.md#post-process--запустить-end-to-end-пайплайн)
- CLI-эквивалент: [`mnemos processor run`](cli-reference.md#processor)

---

## `mnemos_compress`

Сжатие большого контента (вывод инструментов, логи, JSON) **без потери данных**. Оригинал кэшируется в таблице `ccr_cache` SQLite по SHA-256 хешу; сжатый вывод содержит короткий парсимый маркер, по которому LLM может вызвать `mnemos_retrieve` и получить полный оригинал по требованию. Даёт 70–90% сокращения токенов на типичных логах и JSON.

Контент короче `min_size_chars` (по умолчанию 500) возвращается как есть — не кэшируется и не сжимается (мелкий контент не даёт экономии токенов).

### Входные параметры

| Поле | Тип | Обяз. | По умолч. | Описание |
|------|-----|-------|-----------|----------|
| `text` | string | **да** | — | Контент для сжатия. ≥500 символов для кэширования. |
| `profile` | string | нет | auto | Один из `log`, `terminal`, `code`, `docs`, `web`, `default`. Автоопределение, если опущен. |
| `project` | string | нет | `""` | Slug проекта для привязки записи кэша. |
| `agent` | string | нет | — | **Реестр эмитентов A2:** ваш slug агента — записывается в записи кэша как эмитент, чтобы строгая валидация маркера могла позже доказать, что маркер выпущен в вашем контексте. |
| `session` | string | нет | — | **Реестр эмитентов A2:** ваш id сессии — сохраняется вместе с `agent` как пара эмитента. |

### Вывод

```json
{
  "compressed_text": "[compressed: a1b2... | 30000→900 chars | retrieve via mnemos_retrieve]\n...отфильтрованный контент...",
  "hash": "a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef12345678",
  "original_size": 30000,
  "compressed_size": 900,
  "reduction_pct": 97.0,
  "marker": "[compressed: a1b2... | 30000→900 chars | retrieve via mnemos_retrieve]",
  "cached": true,
  "profile": "log"
}
```

### Формат маркера

```text
[compressed: <sha-256-хеш> | <N>→<M> символов | retrieve via mnemos_retrieve]
```

Маркер — единственный оверхед поверх отфильтрованного контента. Короткий, парсимый, удобный для LLM. Хеш адресован по содержимому, поэтому повторное сжатие того же текста — no-op (запись кэша переиспользуется). Пара эмитента (`agent`/`session`) принадлежит ПЕРВОМУ писателю строки `(project, hash)` — поздняя сессия, повторно сжимающая идентичный контент, получает маркер, который строгая валидация привязывает к первому эмитенту (fail-closed; безвредно — повторный сжимающий уже располагает контентом).

### Пример

Сжать лог сборки на 30K строк → ~900 символов в контекстном окне. Когда LLM нужен полный traceback, он вызывает `mnemos_retrieve` с хешем из маркера.

### Связанные ресурсы

- HTTP-эквивалент: [`POST /compress`](http-api.md#post-compress--сжать-контент)

---

## `mnemos_retrieve`

Извлечение оригинального несжатого контента по хешу маркера CCR. Если `query` опущен — возвращается полный оригинал. Если `query` задан — возвращаются FTS5-ранжированные сниппеты из кэшированного оригинала (полезно, когда оригинал большой, а релевантны несколько строк).

### Входные параметры

| Поле | Тип | Обяз. | По умолч. | Описание |
|------|-----|-------|-----------|----------|
| `hash` | string | **да** | — | SHA-256 хеш из маркера `[compressed: ...]`. |
| `query` | string | нет | — | Поисковый запрос для извлечения сниппетов. |
| `snippet_count` | integer | нет | `5` | Количество сниппетов при заданном `query`. |
| `project` | string | нет | — | Slug проекта: ограничивает поиск записями проекта — хеш, закэшированный в другом проекте, возвращается как не найденный. |
| `validate_marker` | boolean | нет | `ccr.validate_markers` | **Строгий режим A2:** валидировать маркер до выдачи контента. |
| `original_chars` | integer | нет | — | `N` из маркера `[compressed: <hash> | N→M chars]` — включает проверку целостности. |
| `agent` | string | нет | — | Ваш slug агента — доверенный контекст эмитента для проверки происхождения. |
| `session` | string | нет | — | Ваш id сессии — в паре с `agent` как доверенный контекст эмитента. |

### Строгая валидация маркера A2

Запрос имеет **форму маркера**, если несёт любое из полей `original_chars` / `agent` / `session` — метаданные, которые харнесс извлекает из маркера, плюс собственную идентичность. В строгом режиме (`validate_marker=true` либо конфиг-переключатель `ccr.validate_markers`) запрос формы маркера обязан пройти три проверки ДО выдачи контента (АрхКом 2026-08-27, решение `archcom-2026-08-27-deferrals-triage`):

1. **существование** — запись найдена по `(project, hash)`; строгая валидация ТРЕБУЕТ область `project`: `validate_marker=true` (или включённый переключатель) без `project` отклоняется с `reason="marker validation failed: existence: project scope required for marker validation"` и без контента (поиск без области выкупил бы маркер по первой сохранённой копии любого проекта);
2. **целостность** — `original_chars` маркера равен длине сохранённого оригинала в символах;
3. **происхождение** — реестр эмитентов строки (записан при сжатии через `agent`/`session`) совпадает с вашей парой `(agent, session)`: `null`-сессия соответствует только `null`-сессии эмитента, никогда wildcard.

Любая непройденная проверка возвращает refused-форму с `reason="marker validation failed: <проверка>: <деталь>"` и **без контента** (fail-closed). Причины — ФИКСИРОВАННЫЕ строки без oracle-утечек: они никогда не содержат длину сохранённого оригинала или пару эмитента (утёкшая причина — двухвызовный oracle, ломающий provenance). Строки, сохранённые без идентичности эмитента (легаси-миграции, сжатие без идентичности), проваливают полную валидацию с отдельной причиной `unverifiable legacy marker`. **Закрытие hash-only (раунд ревью F2):** в строгом режиме запрос только с хешем по строке со штампом эмитента отклоняется с `reason="marker validation required"` — срезание опциональных аргументов не обходит гейт; легаси-строки с NULL-эмитентом остаются доступными по hash-only с WARNING (неверифицируемы по построению; отказ окирпичил бы все pre-A2 кэши). Обычные hash-only запросы при выключенном переключателе не затрагиваются. Отказ валидации не инкрементирует `retrieval_count`.

Для `mnemos_assemble_context` с `expand_ccr=true`: передавайте `agent` вместе с `session`, чтобы расширение шло в вашем контексте эмитента; без полной идентичности `(agent, session)` строгое развёртывание ПРОПУСКАЕТ расширение штампованных маркеров (маркер остаётся — модель сохраняет handle по требованию); легаси-строки с NULL-эмитентом расширяются. Статистика CCR-стадии несёт `skipped_refused` для этих случаев.

Остаточный риск (принят, реестр остаточных рисков ADR-0018): доверенный харнесс с доступом к compress может засеять контент внутри своего проекта и выкупить маркер той же идентичностью — single-operator; пересмотр по первому multi-principal-триггеру.

### Вывод (полное извлечение)

```json
{
  "hash": "a1b2...",
  "found": true,
  "original": "...полный оригинальный текст...",
  "size_bytes": 30000,
  "retrieval_count": 2
}
```

### Вывод (извлечение сниппетов)

```json
{
  "hash": "a1b2...",
  "found": true,
  "query": "Traceback",
  "snippets": [
    {"text": "Traceback (most recent call last):", "rank": 1.0},
    {"text": "  File \"app.py\", line 42, in handler", "rank": 0.8}
  ],
  "retrieval_count": 3
}
```

Если хеша нет в кэше (например, вытеснен по TTL или LRU), `found` равно `false` с полем `reason`.

### Связанные ресурсы

- HTTP-эквивалент: [`POST /retrieve`](http-api.md#post-retrieve--извлечь-оригинал-из-ccr-кэша)

---

## `mnemos_align_prefix`

**CacheAligner (P1-5)** — переносит динамический контент (ISO-таймстампы, UUID, session id, короткоживущие токены, календарные даты) из system-prompt-подобного текста в блок `--- Dynamic context ---` в конце, чтобы prefix оставался побайтово идентичным между запросами и KV-кэши провайдеров (Anthropic `cache_control`, OpenAI prefix caching) попадали. Инспирировано headroom CacheAligner (https://github.com/headroomlabs-ai/headroom, Apache 2.0). Оригинальная реализация — код headroom не импортируется.

Когда CacheAligner отключён в конфиге, текст возвращается без изменений с пустым списком `extracted`.

### Входные параметры

| Поле | Тип | Обяз. | По умолч. | Описание |
|------|-----|-------|-----------|----------|
| `text` | string | **да** | — | System-prompt-подобный текст для стабилизации. |
| `profile` | string | нет | `default` | Одно из `code`, `docs`, `default`. Переключает, какие виды динамического контента извлекаются. `code` и `docs` пропускают «голые» токены (избегают искажения длинных идентификаторов или дефисных слов); `default` извлекает все виды. |

### Вывод

```json
{
  "aligned_text": "You are a senior engineer.\n\n--- Dynamic context ---\n- timestamp: 2026-07-17T10:30:00Z\n- session_id: sess-abc123def456\n",
  "extracted": [
    {"kind": "timestamp", "value": "2026-07-17T10:30:00Z", "start": 24, "end": 44},
    {"kind": "session_id", "value": "sess-abc123def456", "start": 60, "end": 78}
  ],
  "prefix_stabilized": true,
  "moved_chars": 38
}
```

- `aligned_text` — входной текст с удалёнными динамическими спанами и добавленным в конец блоком `--- Dynamic context ---`, где каждое извлечённое значение указано со своим `kind`.
- `extracted` — список извлечённых спанов (`kind`, `value`, `start`, `end` в *оригинальном* тексте).
- `prefix_stabilized` — `true`, если хотя бы один спан извлечён из prefix-области (т.е. выровненный prefix длиннее оригинального prefix вплоть до первого динамического спана).
- `moved_chars` — суммарно перенесено символов (сумма длин спанов).

### Пример

Вход:
```text
You are a senior engineer. Today is 2026-07-17T10:30:00Z. Session: sess-abc123def456.
[стабильные правила далее...]
```

Выровненный вывод (prefix вплоть до первого динамического спана теперь побайтово стабилен между запросами):
```text
You are a senior engineer. Today is . Session: .
[стабильные правила далее...]

--- Dynamic context ---
- timestamp: 2026-07-17T10:30:00Z
- session_id: sess-abc123def456
```

### Поведение профиля

| Профиль | Пропускает | Почему |
|---------|------------|--------|
| `default` (или опущен) | ничего | извлекает все виды |
| `code` | `token` | «голые» 20+ символьные токены исказили бы длинные идентификаторы / хеши в коде |
| `docs` | `token` | в прозе редко бывают реальные токены; избегаем искажения длинных дефисных слов |

Skip-множество профиля объединяется (union) с поключевыми тогглами из `CacheAlignerConfig` — отключение вида в конфиге расширяет то, что профиль уже пропускает.

### Конфигурация

```yaml
cache_aligner:
  enabled: true               # главный переключатель
  extract_timestamps: true   # ISO 8601 таймстампы
  extract_uuids: true        # канонические 8-4-4-4-12 UUID
  extract_session_ids: true  # sess-*, session:*, sid-*
  extract_dates: true        # календарные даты 2026-07-17 / 2026/07/17
  extract_tokens: true       # «голые» 20+ символьные непрозрачные токены
```

Вид с тогглом `false` добавляется в skip-множество и остаётся на месте (не переносится).

### Связанные ресурсы

- Архитектура: [overview.md#cachealigner-p1-5](../architecture/overview.md#cachealigner-p1-5)
- Референс конфига: [config.example.yaml](../../../config.example.yaml)

---

## `mnemos_filter`

Запустить или обновить контекстный фильтр (M10) для существующей записи и вернуть её `clean_content`. Полезно, когда автофильтр был выключен при приёме или нужно перефильтровать с другим профилем.

Инструмент — **issue-ограниченный** двойник обслуживающего примитива: в контекст фильтруются только записи `published` / `processed` (`raw` / `processing` / `archived` отказывают по fail-closed), опциональный slug проекта вызывающего при несовпадении тоже отказывает, а возвращаемый `clean_content` сканируется на секреты — refuse-режим полностью отбрасывает контент, redact-режим возвращает отредактированную копию со счётчиками.

### Входные параметры

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|--------------|---------- |
| `memory_id` | string | **да** | — | Id записи для фильтрации. |
| `profile` | string | нет | автоопределение | Профиль контекстного фильтра. См. [context-filter.md#профили](context-filter.md#профили). |
| `budget` | integer | нет | — | Токенный бюджет для обрезки. |
| `project` | string | нет | — | Slug проекта вызывающего — запись должна принадлежать ему (несовпадение отказывает). Опустите для семантики оператора. |

### Вывод

```json
{
  "memory_id": "550e8400-e29b-41d4-a716-446655440000",
  "profile": "terminal",
  "clean_content": "...filtered text...",
  "stats": { "...": "статистика конвейера фильтра" },
  "redactions": 0
}
```

При `redactions` > 0 ответ также содержит `redacted_patterns` (только имена шаблонов — совпавшие значения никогда не возвращаются).

### Пример вызова

```json
{
  "jsonrpc": "2.0",
  "id": 12,
  "method": "tools/call",
  "params": {
    "name": "mnemos_filter",
    "arguments": {
      "memory_id": "550e8400-e29b-41d4-a716-446655440000",
      "profile": "terminal"
    }
  }
}
```

### Ошибки

Полезная нагрузка ошибки содержит поле `reason`:

| `reason` | Причина |
|----------|---------|
| `not_found` | Записи с таким id нет. |
| `status_gate` | Статус записи не `published` / `processed` (или она в карантине). |
| `project_scope` | Запись не принадлежит проекту `project` вызывающего. |
| `no_content` | У записи нет контента для фильтрации. |
| `refused` | Сканер секретов отказал в выдаче контента (контент не возвращается). |

### Связанные ресурсы

- [context-filter.md](context-filter.md) — профили, этапы конвейера, поведение автофильтра (список профилей живёт там — здесь не дублируется)
- HTTP-эквивалент: [`POST /filter/{memory_id}`](http-api.md#post-filtermemory_id--применить-5-этапный-контекстный-фильтр)
- CLI-эквивалент: [`mnemos filter`](cli-reference.md#filter)

---

## Напоминание о контрольной точке (автовставка)

Каждый вызов не-save инструмента возвращает нормальный результат **плюс** опциональную строку-напоминание при достижении одного из порогов auto-collect:

```text
... normal result ...

⚠️ [mnemos] 12 tool calls since last checkpoint (970s ago). Consider calling mnemos_save_context to preserve your current progress.
```

Это информационное сообщение; ничто в Mnemos не блокирует вызов. Отключить, установив `MNEMOS_AUTO_COLLECT=0` (по умолчанию).

---

## Напоминание о контракте тегов

Инструменты `mnemos_add` и `mnemos_ingest_url` отклоняют вызовы, нарушающие контракт M2. Три обязательных семейства тегов:

| Тег | Формат | Кардинальность | Назначение |
|-----|--------|----------------|------------ |
| `project:<slug>` | `[a-z0-9][a-z0-9\-_]{0,63}` | ровно 1 | Привязывает к кодовой базе / инициативе |
| `agent:<slug>` | `[a-z0-9][a-z0-9\-_]{0,63}` | ровно 1 | Агент-автор |
| `mnemos:<subtype>` | `[a-z][a-z0-9\-]*` | не менее 1 | Когнитивная категория |

Допустимые подтипы `mnemos:`: `session`, `bug-pattern`, `learning`, `decision`, `rule`, `open-question`, `checkpoint`, `legacy`.

Опциональный скоуп-тег (ADR-0027 Фаза 0): `task:<slug>` (`[a-z0-9][a-z0-9\-_]{0,63}`, не более 1) сужает запись до одной task-области — см. [tag-contract.md](tag-contract.md#task--task-область-многоконтекстная-память-adr-0027-фаза-0).

Полная справка: [tag-contract.md](tag-contract.md).

---

## Сокращение токенов вывода (P1-7)

`mnemos_add`, `mnemos_search` и `mnemos_recall_context` принимают два опциональных параметра, которые управляют стилем вывода вызывающей стороны, не меняя того, что Mnemos хранит или возвращает:

| Параметр | Значения | Что делает |
|----------|----------|------------|
| `verbosity` | `default`, `terse`, `minimal` | Вставляет во framing результата подсказку по стилю вывода. `terse` просит краткий вывод без преамбул; `minimal` просит только факты. |
| `effort` | `low`, `medium`, `high` | Вставляет подсказку по уровню размышлений. `low` помечает рутинный шаг (минимум размышлений); `high` просит вдумчивых размышлений и проверки. |

Это **подсказки, передаваемые вызывающей стороне**, а не изменения конфигурации модели. Инспирировано работой headroom по сокращению токенов вывода. Оригинальная реализация.

### Обратная совместимость

- Оба параметра опциональны. Пропуск использует значения из конфига по умолчанию (`default_verbosity=default`, `default_effort=medium`).
- Значения по умолчанию (`default` / `medium`) дают пустую подсказку — результат инструмента побайтово идентичен выводу до P1-7.
- Невалидные значения (например, `"verbose"`, `"turbo"`) валидируются по разрешённым frozenset'ам, логируются на уровне `WARNING` и откатываются к значению из конфига — мягкая деградация, никогда не выбрасывает исключение.

### Конфигурация

```yaml
output_style:
  enabled: true              # главный переключатель; при false steering — no-op
  default_verbosity: default # значение по умолчанию, если вызывающая сторона опустила verbosity
  default_effort: medium     # значение по умолчанию, если вызывающая сторона опустила effort
```

Когда `output_style.enabled` равно `false`, оба resolver'а возвращают no-op-значения по умолчанию независимо от ввода вызывающей стороны.

### Пример

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "tools/call",
  "params": {
    "name": "mnemos_search",
    "arguments": {
      "query": "cache aligner prefix stability",
      "verbosity": "terse",
      "effort": "low"
    }
  }
}
```

Результат инструмента содержит обычный payload **плюс** короткую подсказку:

```text
... обычные результаты поиска ...

---
*Output style: terse. Be brief. No preambles, no restated context, no ceremony. Lead with the result. Omit explanations the caller already has.*
*Effort: low — routine step, minimal reasoning.*
```

---

## `mnemos_assemble_context`

**Контракт провайдера ADR-0017 D1 (mnemos #125, волна 1)** — один вызов собирает модельно-ориентированный контекстный блок для инъекции перед вызовом LLM. Любой MCP-совместимый харнесс получает стандартизованную сборку контекста вместо приватного recall-кода адаптера.

Фиксированный конвейер, по порядку (дословно записывается в `stats.stages`):

1. **recall** — гибридный RRF (FTS5 + вектор) через стандартный путь поиска; статусный гейт инварианта входа пропускает только записи `published` / `processed` (`raw` и DLQ недостижимы). Параметр `file` задаёт поисковый запрос и поднимает applyTo-правила в начало списка.
2. **ccr** *(опционально, `expand_ccr=true`)* — встроенные маркеры `[compressed: <hash> | …]` в найденном контенте разворачиваются через project-scoped извлечение, с учётом бюджета: оригинал, не влезающий в бюджет, остаётся сжатым (маркер на месте — модель может вызвать `mnemos_retrieve` сама).
3. **filter** — 5-стадийный контекстный фильтр по каждому блоку (профиль автоопределяется).
4. **scan** *(обязательно)* — каждый блок проходит issuance-скан секретов; редacted-спаны (`<REDACTED:<pattern>>`) считаются по блокам; refuse-режим (`ccr.retrieve_refuse_on_secret`) выбрасывает блок (fail-closed). Ничто не попадает в собранный вывод без скана.
5. **align** — CacheAligner переносит динамический контент в хвост каждого блока (выполняется ДО обёртки провенансом, чтобы строка провенанса оставалась парсабельной).
6. **budget** — целые блоки с провенансом включаются жадно в порядке релевантности в рамках токен-бюджета; не влезающие блоки пропускаются целиком (без обрезания на середине).

Каждый внедряемый блок несёт строку провенанса, точный формат:

```text
[mnemos:<memory-id> project=<slug> status=<status> origin=<source> pipeline=<phase> v=<n> retrieved=<iso8601>]
```

Сегмент `pipeline=` опускается, если `pipeline_state` строки NULL (легаси-строки).
`retrieved=` привязан к сессии (#282): штампуется при первой сборке
сессии и не меняется при всех последующих сборках той же сессии, поэтому
префикс блока байт-стабилен для KV-кэширования на стороне харнесса.

### Вход

| Поле | Тип | Обяз. | По умолч. | Описание |
|------|-----|-------|-----------|----------|
| `session` | string | **да** | — | Идентификатор сессии вызывающего (эхом возвращается в результате; идентифицирует сборку, не записи). |
| `project` | string | **да** | — | Слаг проекта, ограничивает recall и погашение CCR-маркеров. |
| `agent` | string | нет | — | Слаг агента вызывающего — образует с `session` контекст эмитента: с ним стадия разворота CCR-маркеров идёт под строгой валидацией; без него строгое окружение пропускает разворот маркеров со штампом эмитента (маркер остаётся; легаси-строки с NULL-эмитентом разворачиваются). |
| `file` | string | нет | — | Путь к файлу: задаёт recall-запрос и поднимает applyTo-правила в начало. |
| `budget` | integer | нет | `2048` | Токен-бюджет собранного блока. |
| `mode` | string | нет | `sync` | `sync` (по умолчанию) / `async` (сохранить результат, вернуть handle) / `code` / `prose` (синхронная доставка + фильтр кандидатов по типу контента, зафиксированному при ingest). |
| `expand_ccr` | boolean | нет | `false` | Включить опциональную стадию разворота CCR-маркеров. |
| `async_handle` | string | нет | — | Забрать (и изъять) результат, сохранённый вызовом с `mode="async"`. Привязан к сессии: выкупить может только собравшая сессия; несовпадение → ошибка, handle не изымается. |

### Выход

```json
{
  "session": "sess-42",
  "project": "my-project",
  "file": null,
  "mode": "sync",
  "content_type": null,
  "text": "[mnemos:3f2a… project=my-project status=published retrieved=2026-08-27T10:00:00+00:00]\nDeployment guide…",
  "blocks": [
    {
      "memory_id": "3f2a…",
      "project": "my-project",
      "status": "published",
      "score": 0.0114,
      "search_type": "hybrid",
      "content_type": "prose",
      "provenance": "[mnemos:3f2a… project=my-project status=published retrieved=2026-08-27T10:00:00+00:00]",
      "content": "Deployment guide…",
      "tokens": 96,
      "redactions": 1,
      "redacted_patterns": {"aws-key": 1},
      "ccr_expanded": false,
      "ccr_hashes": []
    }
  ],
  "tokens": {"budget": 2048, "estimated": 96},
  "stats": {
    "stages": ["recall", "ccr", "filter", "scan", "align", "budget"],
    "recall": {"query": "my-project", "query_source": "derived",
                "candidates": 3, "admissible": 3,
                "content_type_filtered": 0,
                "content_type_fallbacks": 1, "applyto_pinned": 0},
    "ccr": {"enabled": false, "markers_found": 0, "expanded": 0,
             "skipped_missing": 0, "skipped_budget": 0, "skipped_refused": 0},
    "filter": {"profiles": {"default": 1, "code": 1}},
    "scan": {"blocks_scanned": 2, "blocks_refused": 0},
    "align": {"blocks_aligned": 1, "moved_chars": 24},
    "budget": {"budget": 2048, "estimated_tokens": 96,
                "blocks_included": 2, "blocks_skipped": 0}
  }
}
```

При `mode="async"` вызов возвращает только конверт с handle (`{"mode": "async", "handle": "<hex>", "status": "ready", "note": …}`); передайте `async_handle` в следующем вызове, чтобы забрать сохранённый результат (однократно: handle можно выкупить один раз, и только сессией, которая его собрала — попытка из другой сессии отклоняется, не изымая handle).

### Замечания

- **Валидация на границе** — некорректные `session` / `project` / `mode` / `budget`, нестроковый `file`, неизвестный `async_handle` или handle, принадлежащий другой сессии, возвращают `{"error": …}` (REST-близнец отвечает 422).
- **Разбиение contentType** — `mode=code` оставляет кандидатов, у которых ingest-время `detect_profile` дало `code`; `mode=prose` — остальных (бинарное разбиение). Легаси-строки без сохранённых метаданных классифицируются на лету и считаются в `recall.content_type_fallbacks`.
- **Разбиение бюджета (аддендум 2)** — в этой волне бюджет монолитный; резервирование active-state линии до аллокации recall ждёт коридора по baseline D5.
- **Async-реестр** — в памяти, на менеджере, с лимитом (вытеснение старейших); записи привязаны к сессии (CWE-863: handle — bearer-токен, выкупить может только собравшая сессия); рестарт сервера теряет незабранные handle.
- **`ccr_hashes`** — по-блочная наблюдаемость: content-addressed хэши CCR-маркеров, развёрнутых в этот блок (пусто, если их нет). Формат обёртки провенанса не меняется — она называет внешнюю запись.

### Связанное

- REST-близнец: `POST /context/assemble` (тот же путь менеджера) — [http-api.md](http-api.md)
- Обоснование конвейера: ADR-0017 (D1), ADR-0018 (инвариант входа: скан + провенанс + статусный гейт на каждом пути LTM → контекст)
- CCR: [`mnemos_compress`](#mnemos_compress) / [`mnemos_retrieve`](#mnemos_retrieve)

---

## `mnemos_context_rewrite`

**Событие жизненного цикла `on_context_rewrite` (ADR-0018, mnemos #125, волна 2)** — харнесс сообщает, что он *перезаписал* блок своего рабочего контекста. Оригинал заменённого блока — источник истины: он без потерь попадает в долговременную память через **обычный knowledge-конвейер** и становится доступен для rehydrate через **существующие** просканированные/гейтованные каналы. Компакция харнесса становится lossless, когда оригиналы ложатся в провайдера.

Семантика (ADR-0018, дословно):

- **Идемпотентность** — повторная доставка того же события не выполняет повторных записей. Ключ идемпотентности content-addressed: SHA-256 по length-prefixed каноническому кортежу `project/agent/session/supersedes/content`, сохраняется как `metadata["rewrite_event_key"]` и проверяется *до* любой записи. Адвизорный `diff` в ключ сознательно не входит — он не load-bearing, поэтому повторная доставка с другим diff — то же событие. Два одинаковых блока, перезаписанные в двух разных сессиях, — два события (`session` участвует в ключе).
- **Без версий** — никаких обещаний порядка и цепочек версий. Линия замены — ребро `supersedes` (минимальная поверхность `memory_edges` фазы 1); обход/расширение — фаза 2 (ADR-0017 D2).
- **Вход в конвейер** — оригинал входит как `raw` через `MemoryManager.add`; в контексте он достижим только после продвижения конвейером в `processed`/`published` (гейт `CONTEXT_ADMISSIBLE_STATUSES`). Layer-1 скан на записи выполняется по `content` (при находке автоматом ставится `mnemos:no-federate`; zero-loss — оригинал сохраняется без изменений). Адвизорный diff получает собственный Layer-1-вердикт (`rewrite_diff_scan_verdict`: clean/hit/unknown), и находка тоже помечает запись `mnemos:no-federate` — иначе адвизорная полезная нагрузка федерировалась бы без флага через канал, сканирующий только `content`.
- **Rehydrate = существующие каналы** — сохранённые перезаписью оригиналы всплывают через `mnemos_retrieve` / `mnemos_assemble_context` (скан на выдаче, провенанс, статусный гейт). Нового пути извлечения сознательно нет.
- **Маркер** — CCR-маркер остаётся в окне харнесса (на стороне вызывающего). Установите `include_marker=true`, чтобы также получить compress-маркер оригинала; rehydrate этого маркера идёт через `mnemos_retrieve` (скоуп по проекту, скан на выдаче).

### Вход

| Поле | Тип | Обязательное | По умолчанию | Описание |
|-------|------|----------|---------|-------------|
| `content` | string | **да** | — | Оригинальный текст заменённого блока контекста — источник истины, сохраняется без изменений. |
| `project` | string | **да** | — | Слаг проекта (тег `project:<slug>`). |
| `agent` | string | **да** | — | Слаг агента (тег `agent:<slug>`). |
| `session` | string | нет | — | Идентификатор сессии — метаданные провенанса и часть ключа идемпотентности. |
| `supersedes` | string | нет | — | id записи, которую заменили, — создаёт ребро `supersedes` новый → старый (должна существовать; тоже часть ключа события). |
| `diff` | string | нет | — | Адвизорный diff was→becomes — хранится в метаданных, никогда не load-bearing, не возвращается. |
| `include_marker` | boolean | нет | `false` | Также вернуть CCR compress-маркер оригинала. |

### Выход

```json
{
  "status": "stored",
  "memory_id": "3f2a…",
  "memory_status": "raw",
  "event_key": "9c1d…",
  "project": "my-project",
  "agent": "my-agent",
  "session": "sess-42",
  "supersedes": {"to_memory_id": "a17b…", "edge_created": true}
}
```

`status` — `stored` (первая доставка; `memory_status` = `raw` — конвейер ещё не отработал) или `deduplicated` (повторная доставка: тот же `memory_id`, новых записей нет; идемпотентная вставка ребра вернёт `edge_created: false`). `ccr_marker` (полный результат `mnemos_compress`) появляется только при `include_marker=true`. Квитанция **не содержит версионных полей и полей порядка** — by design (событие без версий).

### Замечания

- **Валидация на границе** — пустые `content`/`project`/`agent`, пустые опциональные строки, нарушение контракта тегов (strict-режим), нарушение размерных лимитов (`content` > `mnemos.context_rewrite_max_content_chars`, по умолчанию 1 МиБ; `diff` > `mnemos.context_rewrite_max_diff_chars`, по умолчанию 256 КиБ) или цель `supersedes`, **не найденная в проекте вызывающего**, возвращают `{"error": …}` (REST-близнец отвечает 422). Сообщение о supersedes сознательно не различает «нет такой записи» и «запись другого проекта» — глобального оракула существования нет.
- **Rate limit на поверхность записи** — `mnemos.context_rewrite_rate_limit_per_minute` (по умолчанию 30, 0 отключает) считает СОХРАНЁННЫЕ события на `(project, session)` в скользящую минуту; при превышении возвращается `{"error": …, "rate_limited": true}` (REST 429). Дедуплицированные повторные доставки не пишут и не расходуют квоту — штормы ретраев безвредны.
- **Сохраняемые теги** — `project:<slug>`, `agent:<slug>`, `mnemos:session` (ближайший существующий подтип для живого сессионного материала; отдельный подтип `mnemos:context-rewrite` — изменение словаря контракта тегов, отложено на комитет), плюс `mnemos:no-federate` при любой находке секрета.
- **Метаданные провенанса** — `metadata["source"] = "context-rewrite"`, `rewrite_session`, `rewrite_event_key` и (при наличии) `rewrite_diff` + `rewrite_diff_scan_verdict`.
- **Модель доверия single-tenant** — харнесс является доверенным ПО; провайдер гарантирует хранение, сканирование, гейтирование и провенанс, но не политику замены (pinned-зоны, бюджеты и эмиссия событий замены остаются на стороне харнесса).

### Связанное

- REST-близнец: `POST /context/rewrite` (тот же путь менеджера) — [http-api.md](http-api.md)
- Обоснование: ADR-0018 (§"on_context_rewrite": событие жизненного цикла, а не версионируемый примитив)
- Каналы rehydrate: [`mnemos_retrieve`](#mnemos_retrieve) / [`mnemos_assemble_context`](#mnemos_assemble_context); маркер через [`mnemos_compress`](#mnemos_compress)

---

## `mnemos_hooks`

**Хуки жизненного цикла (ADR-0017 D1 / ADR-0018, mnemos #125 Wave 3)** — точки интеграции для автоматизации, сгруппированные за `action:enum` (групповой паттерн mnemos #97). Три действия, один инструмент:

- **`pre_llm_call`** — собрать контекстный блок для **инъекции перед вызовом модели** (тонкая обёртка над `mnemos_assemble_context`, доставка синхронная). `context_hint` (о чём предстоящий вызов) используется как явный recall-запрос вместо производного термина проект/файл. `task` (ADR-0027 Фаза 0, эпик #308) — идентификатор задачи от харнесса, «голый» slug: сужает recall до записей с тегом `task:<slug>` (доктрина пересечения — task-условие только сужает, никогда не расширяет) и компонует только хвост сборки на этот вызов; закреплённые префиксы и формат провенанса не тронуты. Инвариант входа ADR-0018 — скан секретов, провенанс, гейт статуса — выполняется внутри конвейера сборки; хук ничего к нему не добавляет. С `include_awareness=true` (mnemos #254, по умолчанию `false` — выключено означает байт-идентичный вывод) секция awareness-дельты И «операционная картина» swarm v0a (соседи того же проекта: только счётчики/ids/времена) добавляются ПОСЛЕДНИМИ, никогда не закрепляются, курсор awareness двигается; картина рендерится ниже секции дельты (см. [`mnemos_awareness`](#mnemos_awareness)).
- **`on_session_start`** — вспомнить недавние чекпоинты для бутстрапа сессии (тонкая обёртка над recall-путём; эхо-контент сканируется на выдаче в самом этом канале, как в `mnemos_recall_context`).
- **`post_tool_call`** — **точка входа автосжатия** (ADR-0018): когда `auto_compress` разрешается в true (аргумент вызова, иначе ручка `hooks.auto_compress`, по умолчанию `false`), вывод инструмента сжимается через CCR и возвращается `compressed_text` с маркером в голове — вызывающий **подставляет** его вместо сырого вывода в своём окне. По умолчанию выключено: конверт сообщает об этом и ничего не пишет.

**Мандат идентичности (реестр A2, N2, громко):** `session` + `project` + `agent` обязательны на КАЖДОМ вызове. Для `post_tool_call` это требование безопасности, а не эргономика — вызов compress всегда протаскивает `(agent, session)` вызывающего в строку кэша (реестр эмитентов A2), чтобы строгая валидация маркеров (`ccr.validate_markers`) могла позже доказать, что маркер отчеканен в собственном контексте погашающего. Сжатие без идентичности чеканит NULL-issuer строки, которые строгая валидация отказывается погашать, — у хука нет режима без идентичности.

### Входные параметры

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|----------|
| `action` | string | **да** | — | `pre_llm_call` / `on_session_start` / `post_tool_call`. |
| `session` | string | **да** | — | Идентификатор сессии вызывающего. |
| `project` | string | **да** | — | Slug проекта. |
| `agent` | string | **да** | — | Slug агента вызывающего (идентичность эмитента). |
| `context_hint` | string | нет | — | `pre_llm_call`: о чём предстоящий вызов модели — явный recall-запрос. Семантика FTS5 — вся подсказка матчится как ОДНА цитированная фраза (токены подряд, в порядке), а не набор ключевых слов. |
| `file` | string | нет | — | `pre_llm_call`: опциональный путь к файлу (термины recall + закрепление applyTo-правил). |
| `task` | string | нет | — | `pre_llm_call` (ADR-0027 Фаза 0): опциональная task-область — «голый» slug задачи (`[a-z0-9_-]{1,64}`, без префикса `task:`). Сужает recall до записей с тегом `task:<slug>`; только хвост. |
| `budget` | integer | нет | `2048` | `pre_llm_call`: бюджет токенов. |
| `limit` | integer | нет | `5` | `on_session_start`: количество чекпоинтов. |
| `tool_name` | string | `post_tool_call` | — | Инструмент, породивший вывод. |
| `output_text` | string | `post_tool_call` | — | Сырой вывод инструмента для сжатия. |
| `auto_compress` | boolean | нет | ручка | `post_tool_call`: точечное переопределение `hooks.auto_compress`. |
| `profile` | string | нет | авто | `post_tool_call`: подсказка профиля фильтра для сжатия. |

### Возвращаемое значение

`pre_llm_call` возвращает полный результат `mnemos_assemble_context` плюс ключи `hook`/`injection` (инъецируйте `text` перед вызовом модели). `on_session_start` возвращает `{hook, session, project, agent, checkpoints: [{id, content, created_at, redactions, redacted_patterns?}], redactions}` — контент чекпоинтов сканируется на выдаче; refuse-режим отбрасывает чекпоинт. `post_tool_call` с включённым автосжатием возвращает конверт CCR (`ccr`, `compressed_text`, `marker`, `compressed`, `action: "substitute …"`); при выключенном — `{auto_compress: false, compressed: false, note}` без записи.

### Замечания

- **Конфигурация** — две ручки: `hooks.auto_compress` (по умолчанию `false`) и `hooks.max_output_chars` (по умолчанию 1 048 576 символов — `post_tool_call` отклоняет превышающий кап `output_text` на границе ДО любой записи, по конвенции капов context-rewrite; `0` отключает). Read-only хуки не требуют включения: они не открывают никаких возможностей, которых не дают поверхности сервера.
- **Только sync (эта волна)** — ADR-0017 D1 называет режимы sync/async; асинхронная доставка ждёт потребителя, которому она нужна. Харнессы, которым нужны режимы `async`/`code`/`prose`, вызывают `mnemos_assemble_context` напрямую.
- **Сохранение в память — явно** — `post_tool_call` молча не складывает выводы инструментов в память; используйте `MnemosSDK.remember` (или `mnemos_add`/REST), когда результат стоит сохранения.
- **Ошибки** — нарушения границы возвращают `{"error": …}` (REST-близнец отвечает 422; неизвестное действие там — 404). Превышение капа `output_text` — нарушение границы: `{"error": "output_text exceeds hooks.max_output_chars (N > M)"}`, ничего не записано.

### Связанное

- REST-близнец: `POST /hooks/{action}` — [http-api.md](http-api.md)
- Программная поверхность: `MnemosSDK` ([integration-guide.md](integration-guide.md))
- Обоснование: ADR-0017 D1 (интеграция жизненного цикла), ADR-0018 (автосжатие post_tool_call, реестр остаточных рисков N2)

---

## `mnemos_awareness`

**Awareness-пре-флайт (mnemos #254, R3; swarm v0a — АрхКом 2026-09-27)** — поверхность, которую параллельная сессия вызывает ПЕРЕД рискованной операцией (контракт PR #224: релиз, закрытый невидимой параллельной сессией). Два действия, один инструмент:

- **`pre_flight`** (только чтение) — наблюдаемая сервером активность соседей: присутствие (кто активен), дельта (что изменилось с вашего курсора — одна строка на соседа), лексические подсказки о конфликтах с вашей последней целью чекпоинта и **операционная картина** (swarm v0a): явный блок соседей того же проекта, где каждая строка несёт id агента, последнюю наблюдаемую активность, число записей в 900-секундном окне присутствия и факт наличия чекпоинта — **только счётчики, id агентов и времена**. Ни заголовок, ни тело, ни тег записи соседа никогда не эхируются; клиентский текст в картину не попадает. Строго в рамках одного проекта (`project=None` — отказ; межпроектной видимости не существует — ни параметра, ни флага). Курсор awareness двигается ТОЛЬКО через `mnemos_hooks` `pre_llm_call` с `include_awareness=true` — пре-флайт никогда не помечает записи соседей потреблёнными.
- **`record_abstention`** — зарегистрировать воздержание-по-присутствию как ДЕЙСТВИЕ с восстановимой цепочкой провенанса (воздержание → дельта-блок → id-чекпоинта → сессия-писателя); передайте `basis_checkpoint_id` из ответа пре-флайта.

**Присутствие — поведенческие метаданные** (id агентов, времена активности, счётчики записей) — гейты поиска покрывают КОНТЕНТ записей и к присутствию не применяются. Картина только дескриптивна (кто / сколько / когда), никогда предиктивна, и это данные, а не управление: блоки картины не несут `memory_id`, никогда не закрепляются, семантики `applyTo:`/`severity:` в них нет. Ноль записей, порождённых картиной, не хранится (курсоры — в meta, действия — в traces); любая будущая awareness-derived запись рождается `mnemos:no-federate`.

**Кап частоты (C9):** queries к картине/awareness ограничены по `(project, agent)` ручкой `vesmaro.awareness_picture_rate_limit_per_minute` (по умолчанию 30, `0` выключает). При превышении — ДЕГРАДАЦИЯ в однострочную секцию «rate-limited, retry later»; форма ответа сохраняется, ошибки нет.

### Вход

| Поле | Тип | Обязательное | Описание |
|------|-----|-------------|----------|
| `action` | string | **да** | `pre_flight` / `record_abstention`. |
| `session` | string | **да** | Id сессии вызывающего. |
| `project` | string | **да** | Slug проекта (fail-closed; строго тот же проект). |
| `agent` | string | **да** | Slug агента вызывающего. |
| `basis_checkpoint_id` | string | `record_abstention` | Id чекпоинта соседа — основание воздержания. |
| `note` | string | нет | `record_abstention`: произвольная заметка (с ограничением длины, сканируется на выдаче). |

### Выход

`pre_flight` возвращает `{action, project, presence, delta, picture, conflict_hints, text, disclaimer, cursor_advanced: false}` — `picture.agents` несёт `{agent, last_seen, entries, checkpoint}` на каждого соседа по проекту (кап 8, свежие первыми; `agents_capped_from` делает обрезание наблюдаемым). `record_abstention` возвращает id трейса и полную цепочку провенанса.

### Заметки

- **Ошибки** — нарушения границы возвращают `{"error": …}`; REST-близнец отвечает 422.
- Неподвижный дисклеймер R3 едет дословно в каждой отрендеренной секции: утверждения о присутствии не должны останавливать работу без координации с оператором.

### Связанное

- Композиция: `mnemos_hooks` `pre_llm_call` / `on_session_start` с `include_awareness=true` (картина рендерится последней, ниже секции дельты)
- REST-близнец: `POST /hooks/{action}` с `include_awareness` — [http-api.md](http-api.md)

---

## `mnemos_export`

Экспорт записей в файл на диске. Тонкая обёртка над логикой CLI `mnemos export`. Возвращает только метаданные — содержимое экспорта **никогда** не возвращается в теле ответа (stdio-транспорт не может передать бинарный SQLite-tarball или большой JSON-блок через канал JSON-RPC поверх stdout).

Защита federation defense-in-depth (#86) наследуется автоматически, так как инструмент вызывает ту же функцию `run_export`, что и CLI/HTTP: записи с тегом `mnemos:no-federate` исключаются из экспорта, а обнаруженные секреты в проходящих записях заменяются на `<REDACTED:<pattern_name>>`.

### Входные параметры

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|----------|
| `output_path` | string | **да** | — | Абсолютный путь, куда записывается файл экспорта. |
| `format` | enum `json` \| `sqlite` | нет | `json` | `json` = экспорт только метаданных (фильтры применяются); `sqlite` = полный `tar.gz`-снимок (фильтры игнорируются). |
| `compress` | enum `none` \| `gzip` | нет | `none` | Режим сжатия. (`zstd` — только для CLI.) |
| `project` | string | нет | — | Фильтр по slug проекта (только json). |
| `agent` | string | нет | — | Фильтр по slug агента (только json). |
| `status` | enum `raw` \| `processing` \| `processed` \| `published` \| `archived` | нет | — | Фильтр по статусу записи (только json). |
| `tags` | array of string | нет | — | Фильтр по тегам (только json). |
| `since` | string (ISO-8601) | нет | — | Только записи, созданные не ранее этой даты (только json). |
| `until` | string (ISO-8601) | нет | — | Только записи, созданные до этой даты (только json). |
| `encrypt` | boolean | нет | `false` | Если `true`, шифрует результат. Парольная фраза читается из переменной окружения `MNEMOS_EXPORT_PASSPHRASE`. |

### Возвращаемое значение

```json
{
  "path": "/abs/path/to/backup.json",
  "memory_count": 42,
  "format": "json",
  "compress": "none",
  "encrypted": false,
  "bytes": 18234,
  "warnings": []
}
```

### Замечание по безопасности

- **Парольная фраза через окружение, никогда в аргументах.** При `encrypt=true` сервер читает парольную фразу из переменной окружения `MNEMOS_EXPORT_PASSPHRASE`. Передача значения в `output_path` или любой другой аргумент приведёт к утечке в логи MCP — никогда так не делайте.
- **Без встроенного контента.** Инструмент пишет в `output_path` и возвращает только метаданные. Прочитайте файл с диска, чтобы осмотреть экспорт.
- **Наследование #86.** Записи `mnemos:no-federate` исключаются; секреты в проходящих записях редактируются. Дополнительная настройка не нужна.

### Пример

```json
{
  "jsonrpc": "2.0",
  "id": 8,
  "method": "tools/call",
  "params": {
    "name": "mnemos_export",
    "arguments": {
      "output_path": "/tmp/mnemos-backup.json",
      "format": "json",
      "project": "mnemos",
      "compress": "gzip"
    }
  }
}
```

Зашифрованный полный снимок:

```json
{
  "name": "mnemos_export",
  "arguments": {
    "output_path": "/tmp/mnemos-snapshot.tar.gz",
    "format": "sqlite",
    "encrypt": true
  }
}
```

(При установленной в окружении сервера `MNEMOS_EXPORT_PASSPHRASE`.)

---

## `mnemos_import`

Импорт записей из файла экспорта. Тонкая обёртка над логикой CLI `mnemos import`. Два режима: **merge** (вставка новых, пропуск или перезапись существующих) и **restore** (полная очистка и импорт — деструктивный, требует `confirm=true`).

Валидация импорта (#86) наследуется автоматически: дрейф схемы, слишком большой контент, невалидные теги и prompt-injection-паттерны обрабатываются той же функцией `run_import`, что и в CLI/HTTP.

### Входные параметры

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|----------|
| `source_path` | string | **да** | — | Абсолютный путь к файлу экспорта для импорта. |
| `mode` | enum `merge` \| `restore` | нет | `merge` | `merge` = вставка новых / пропуск-или-перезапись существующих; `restore` = полная очистка и импорт (требует `confirm=true`). |
| `overwrite` | boolean | нет | `false` | Перезапись существующих записей (только режим merge). |
| `confirm` | boolean | нет | `false` | **Обязательно `true` для режима `restore`** (жёсткий гейт — restore стирает все данные). |
| `dry_run` | boolean | нет | `false` | Валидация без записи; возвращает отчёт валидации. |
| `passphrase_env` | string | нет | — | Имя переменной окружения с парольной фразой для расшифровки (НЕ само значение). |

### Возвращаемое значение

```json
{
  "mode": "merge",
  "dry_run": false,
  "imported": 12,
  "skipped": 3,
  "updated": 0,
  "errors": [],
  "warnings": [],
  "format_version": "1.0",
  "mnemos_version": "4.0.0"
}
```

### Замечание по безопасности

- **Парольная фраза через имя переменной окружения, не значение.** `passphrase_env` принимает *имя* переменной окружения (например, `"MY_IMPORT_PASS"`), и сервер читает `os.environ["MY_IMPORT_PASS"]`. Передача значения в аргументе приведёт к утечке в логи MCP.
- **Restore требует `confirm=true`.** Без него инструмент возвращает ошибку и не трогает живые данные. Restore стирает все записи, векторы и проекты.
- **Наследование #86.** Дрейф схемы отклоняется; слишком большой контент (>1 МиБ) отклоняется; невалидные теги вызывают ошибку контракта тегов; prompt-injection-паттерны логируются на WARNING (не блокируются — контент может правомерно обсуждать инъекцию).

### Пример

```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "method": "tools/call",
  "params": {
    "name": "mnemos_import",
    "arguments": {
      "source_path": "/tmp/mnemos-backup.json",
      "mode": "merge",
      "overwrite": false
    }
  }
}
```

Restore (деструктивный) с подтверждением:

```json
{
  "name": "mnemos_import",
  "arguments": {
    "source_path": "/tmp/mnemos-snapshot.tar.gz",
    "mode": "restore",
    "confirm": true
  }
}
```

Зашифрованный импорт (при установленной в окружении сервера `MNEMOS_IMPORT_PASS`):

```json
{
  "name": "mnemos_import",
  "arguments": {
    "source_path": "/tmp/encrypted.bin",
    "mode": "merge",
    "passphrase_env": "MNEMOS_IMPORT_PASS"
  }
}
```

---

## `mnemos_workflow`

Управление жизненным циклом workflow для памяти (mnemos #96). Отделяет изменяемое **состояние workflow** (open → in-progress → done, blocked/resolved, терминальные состояния) от добавляемого только в конец **тегового классификатора** (`project:X`, `mnemos:decision`). Теговый слой остаётся append-only; этот слой — изменяемый жизненный цикл работы.

Диспетчеризация на основе `action` — тот же паттерн `action: enum`, что и у `mnemos_tags`. Конечный автомат и пять guardrail применяются **на стороне сервера** в `MemoryManager.workflow_set`; этот инструмент (и REST `POST /memories/{id}/workflow`) — тонкие обёртки, которые не могут его обойти.

### Состояния и переходы

```mermaid
stateDiagram-v2
    [*] --> open
    open --> in_progress
    open --> withdrawn
    in_progress --> blocked
    in_progress --> done
    in_progress --> withdrawn
    blocked --> resolved
    blocked --> withdrawn
    resolved --> in_progress
    resolved --> done
    resolved --> withdrawn
    done --> [*]
    withdrawn --> [*]
```

- **`blocked → done` запрещён** — застрявшая зависимость должна сначала пройти через `resolved` (blocked → resolved → done). Это ключевой запрещённый переход; агент не может тихо перепрыгнуть блокер, прыгнув сразу в терминальное состояние.
- **`done` и `withdrawn` терминальны** — из них невозможны дальнейшие переходы.
- Память, у которой workflow ни разу не устанавливался (устаревшая строка или свежесозданная), трактуется как `open` для первого перехода.

### Входные данные

| Поле | Тип | Обязательно | По умолчанию | Описание |
|-------|------|----------|---------|-------------|
| `action` | enum `set` \| `get` \| `history` | **да** | — | `set` выполняет переход статуса; `get` возвращает текущий статус + владельца блокировки; `history` возвращает аудит-след. |
| `memory_id` | string | **да** | — | Целевой id памяти. |
| `to` | enum `open` \| `in-progress` \| `blocked` \| `resolved` \| `done` \| `withdrawn` | `set`: **да** | — | Целевой статус. `blocked → done` запрещён. |
| `actor` | string | `set`: **да** | — | Свободный id актора. **Фаза 1 — слабая идентичность, НЕТ authn/authz.** |
| `reason` | string | `set` + `force=true`: **да** | `""` | Читаемая причина. Обязательна при `force=true`. |
| `force` | boolean | нет | `false` | Переопределить блокировку другого актора (guardrail 4 — требует `reason`). |
| `limit` | integer | нет | `50` | Максимум строк истории (только `history`). |

### Guardrails (применяются на стороне сервера)

| # | Guardrail | Поведение |
|---|-----------|-----------|
| G1 | **Аудит-лог** | Каждый записанный переход пишет строку в `memory_workflow_history` (`from`, `to`, `actor`, `reason`, `force_used`, `created_at`). **Отклонённые переходы** (запрещённое ребро, конфликт блокировок, rate-limit, force-без-причины) **не** пишут аудиторскую строку — лог фиксирует изменения состояния, а не попытки. |
| G2 | **Авто-релиз устаревшей блокировки** | Блокировка старше `workflow_stale_lock_threshold_hours` (по умолчанию `24`) авто-освобождаема другим актором — без `force`. Логируется на уровне WARNING. |
| G3 | **Идемпотентные переходы** | Установка `to=X`, когда память уже `X` — **no-op** (без записи, без аудиторской строки). Возвращает `idempotent: true`, `recorded: false`. |
| G4 | **Force-unlock** | `force=true` переопределяет чужую блокировку; `force_used=1` фиксируется в аудит-логе. **`reason` обязательна** — пустая причина отклоняется. |
| G5 | **Rate limit** | Более `workflow_rate_limit_per_minute` переходов (по умолчанию `30`) по одной памяти в минуту отклоняется. Лимит **на память, не на актора** — churn по одной памяти регулируется независимо от того, какой актор двигает переходы. |

### Выходные данные

**`action: set`** (результат перехода):

```json
{
  "memory_id": "01HXYZ...",
  "from_status": "open",
  "to_status": "in-progress",
  "actor": "agent-dba",
  "previous_locked_by": null,
  "locked_by": "agent-dba",
  "locked_at": "2026-07-31T12:00:00+00:00",
  "stale_lock_released": false,
  "force_used": false,
  "idempotent": false,
  "recorded": true,
  "reason": "",
  "terminal": false
}
```

**`action: get`** (текущая проекция — `workflow_status` нормализует незаданное → `open`):

```json
{
  "memory_id": "01HXYZ...",
  "workflow_status": "in-progress",
  "locked_by": "agent-dba",
  "locked_at": "2026-07-31T12:00:00+00:00"
}
```

**`action: history`** (аудит-след, новые сверху):

```json
{
  "memory_id": "01HXYZ...",
  "history": [
    {
      "id": "uuid...",
      "memory_id": "01HXYZ...",
      "from_status": "open",
      "to_status": "in-progress",
      "actor": "agent-dba",
      "reason": "",
      "force_used": 0,
      "created_at": "2026-07-31T12:00:00+00:00"
    }
  ]
}
```

### Ошибки

- **Отсутствует `memory_id`** → `{"error": "memory_id is required ..."}`.
- **`action: set` без `to` или `actor`** → `{"error": "action='set' requires 'to' ..."}` / `"... requires 'actor' ..."}`.
- **Неизвестный `action`** → `{"error": "unknown action 'X'. Valid actions: 'set', 'get', 'history'"}`.
- **Запрещённый переход / нарушение guardrail** (например `blocked → done`, блокировка другого актора без `force`, force без `reason`, rate limit) → `{"error": "<дословное сообщение manager>"}`. Через REST маппится в HTTP `409`; через MCP-инструмент возвращается в поле `error`.
- **Память не найдена** (`get`) → `{"error": "memory 'X' not found"}`.

### Семантика блокировок

| Целевой статус | Эффект на блокировку |
|---------------|-------------|
| `in-progress` | Захватывает блокировку (владелец = `actor`, timestamp обновлён). |
| `blocked` / `resolved` | Удерживает блокировку; при перехвате (`force` / stale-release) владельцем становится `actor` и таймер устаревания перезапускается. |
| `open` / `done` / `withdrawn` | Освобождает блокировку (`locked_by` и `locked_at` очищаются). |

### Пример

Начать работу над памятью:

```json
{
  "name": "mnemos_workflow",
  "arguments": {
    "action": "set",
    "memory_id": "01HXYZ...",
    "to": "in-progress",
    "actor": "agent-dba"
  }
}
```

Попали в блокер, затем разрешили и завершили:

```json
{"name": "mnemos_workflow", "arguments": {"action": "set", "memory_id": "01HXYZ...", "to": "blocked", "actor": "agent-dba", "reason": "waiting on upstream spec tag"}}
{"name": "mnemos_workflow", "arguments": {"action": "set", "memory_id": "01HXYZ...", "to": "resolved", "actor": "agent-dba"}}
{"name": "mnemos_workflow", "arguments": {"action": "set", "memory_id": "01HXYZ...", "to": "done", "actor": "agent-dba"}}
```

Принудительно перехватить устаревшую блокировку другого актора:

```json
{
  "name": "mnemos_workflow",
  "arguments": {
    "action": "set",
    "memory_id": "01HXYZ...",
    "to": "in-progress",
    "actor": "agent-dba",
    "force": true,
    "reason": "previous actor unreachable for >24h"
  }
}
```

### Фаза 1 — слабая идентичность

`actor` — **свободная строка без authn/authz** в Фазе 1. Любой вызывающий может заявить любой id актора; guardrails (stale-lock, force, rate limit) — единственная защита. Будущая фаза привяжет `actor` к аутентифицированному принципалу; до тех пор трактуйте слой workflow как рекомендательную координацию, а не границу безопасности.

### REST-эквивалент

Тот же жизненный цикл доступен по HTTP, вложенно под памятью (а не на верхнем уровне `/status`):

| Метод | Путь | Маппится на |
|--------|------|---------|
| `GET` | `/memories/{memory_id}/workflow` | `workflow_get` (404 если память отсутствует) |
| `POST` | `/memories/{memory_id}/workflow` | `workflow_set` (тело: `to`, `actor`, `reason`, `force`; `409` при нарушении guardrail) |
| `DELETE` | `/memories/{memory_id}/workflow` | `workflow_set(... to="withdrawn")` — **отмена / withdraw** (терминально, необратимо). Завершает workflow в `withdrawn`; блокировка очищается как побочный эффект достижения терминального состояния. query-параметр `actor` обязателен; `force` переопределяет чужую блокировку. |

`DELETE` — это **отмена / withdraw** — завершает workflow в терминальном состоянии `withdrawn` (дальнейшие переходы невозможны). Это **не** «релиз блокировки в возобновляемое состояние»: у конечного автомата нет ребра обратно в `open`, поэтому память не возвращается в возобновляемое состояние. Чтобы **завершить** работу штатно, используйте `POST` с `to=done`.

### Смотрите также

- [http-api.md](http-api.md) — вложенные REST-эндпоинты `/memories/{id}/workflow`
- [tag-contract.md](tag-contract.md) — слой append-only классификации (отличается от этого изменяемого слоя жизненного цикла)
- ArchCom 2026-07-18 сессия 2 — решение по паттерну `action: enum` + вложенному REST-неймингу

---

## См. также

- [getting-started.md](getting-started.md) — подключение `mcp.json` и первый вызов
- [http-api.md](http-api.md) — те же возможности через HTTP
- [cli-reference.md](cli-reference.md) — те же возможности через CLI
- [tag-contract.md](tag-contract.md) — схема M2, соблюдаемая `mnemos_add`
- [security.md](../admin/security.md) — защита от SSRF, безопасность секретов
- [обзор архитектуры](../architecture/overview.md#mcp-сервер) — жизненный цикл сервера

---

_Последнее обновление: 2026-09-05_
