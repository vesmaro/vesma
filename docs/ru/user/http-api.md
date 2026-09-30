# Справочник HTTP API

**🌐 Language / Язык:** [English](../../en/user/http-api.md) · Русский

> Полная справка по HTTP API Vesma — CRUD записей, поиск, пайплайн, DLQ, контекстный фильтр, трассировки, path-scoped rules, граф проектов (ADR-0032) и A2A Sessions API (M16).

HTTP-сервер — FastAPI-приложение, обслуживаемое Uvicorn. Запуск:

```bash
vesma serve --host 127.0.0.1 --port 8000
```

| Ресурс | URL |
|--------|-----|
| Swagger UI | `http://HOST:PORT/docs` |
| ReDoc | `http://HOST:PORT/redoc` |
| Схема OpenAPI 3.1 | `http://HOST:PORT/openapi.json` |

> **Привязка по умолчанию — `127.0.0.1`.** Не открывайте этот порт в публичную сеть без обратного прокси с аутентификацией. Модель угроз — в [security.md](../admin/security.md).

> **Аутентификация** — когда `api.auth_enabled=true`, все маршруты кроме `/health`, `/auth/login`, `/auth/verify`, `/docs`, `/redoc` и `/openapi.json` требуют валидного сессионного токена (заголовок `Authorization: Bearer <session>` или куки `mnemos_session`; заголовок имеет приоритет). Для получения сессии используйте `POST /auth/login`. См. раздел [Аутентификация](#аутентификация-authentication) ниже.

> **CORS** — отключён по умолчанию. При `api.cors_enabled=true` CORS middleware регистрируется крайним слоем, чтобы OPTIONS preflight-запросы отвечались до проверки аутентификации. Укажите явный список разрешённых origins в `cors_allow_origins`; комбинация `["*"]` с `cors_allow_credentials=true` отклоняется при старте.

Те же возможности через другие транспорты — в [mcp-tools.md](mcp-tools.md) (MCP) и [cli-reference.md](cli-reference.md) (CLI). Для общего контекста — [обзор архитектуры](../architecture/overview.md). Контракт A2A также описан в [a2a-sessions.md](../architecture/a2a-sessions.md) (ссылка на обоснование дизайна).

---

## Соглашения

- Все тела запросов — JSON, если не указано иное.
- Все временны́е метки — ISO 8601 в UTC (`2026-06-15T10:42:00+00:00`).
- ID — UUID, если не указано иное (A2A-сессии используют `conv-YYYY-MM-DD-<short>`).
- Ошибки — JSON формата `{"detail": "..."}`.
- Только стандартные HTTP-статус-коды — нет кастомных кодов ошибок.
- `200 OK` и `201 Created` несут JSON-тело. `204 No Content` используется для удалений без тела ответа.

---

## Статус-коды

| Код | Значение в этом API |
|-----|-------------------- |
| `200` | OK (успех по умолчанию) |
| `201` | Created (POST, вставляющий строку) |
| `400` | Bad request (напр. попытка опубликовать не-`processed` запись; отказ бюджета графа проектов) |
| `404` | Not found (memory_id, cluster_id, dlq_id, session_id, turn_id) |
| `401` | Unauthorised (отсутствует или недействителен токен; только при `api.auth_enabled=true`) |
| `403` | Forbidden — отказ по политике: конфайнмент графа проектов (путь вне зарегистрированного корня или незарегистрированный проект, PG2) на `/graph/*` и `/watch/start` |
| `413` | Payload too large — превышение лимита индексации графа проектов (fail-closed, PG7) на `/graph/index` |
| `422` | Unprocessable entity (сбой валидации Pydantic в теле запроса) |
| `500` | Internal server error (см. логи сервера) |
| `503` | Service unavailable — auth не инициализирован (fail-closed AuthMiddleware) либо мастер-флаг графа проектов `code_graph.enabled` выключен (операторский гейт на `/graph/*` и `/watch/start`) |

---

## Аутентификация {#authentication}

> **Управляется `api.auth_enabled`.** Все четыре эндпоинта смонтированы на `/auth`. При `api.auth_enabled=false` (по умолчанию) эти маршруты существуют, но middleware не применяет проверку на других маршрутах.

Модель аутентификации использует **непрозрачные bearer-токены** (префикс `mnk_`) с опциональным TOTP 2FA. Токены хранятся как PBKDF2-HMAC-SHA256 дайджесты; plaintext показывается один раз при `vesma auth token create` и больше никогда. Сессии выдаются после успешного логина (+ TOTP verify при `api.totp_enabled=true`) и имеют тот же формат `Authorization: Bearer <session>`.

### `POST /auth/login` — начать сессию

**Тело запроса**

| Поле | Тип | Обязательное | Описание |
|------|-----|--------------|---------- |
| `token` | string | **да** | Непрозрачный bearer-токен (`mnk_...`). |

**Ответ 200 — TOTP отключён**

```json
{
  "session": "mnk_session_...",
  "expires_at": "2026-06-18T02:00:00+00:00"
}
```

**Ответ 200 — TOTP включён**

```json
{
  "challenge_id": "chal_a1b2c3d4",
  "ttl_sec": 120
}
```

При включённом TOTP сессия **не** выдаётся здесь; вызовите `POST /auth/verify` с `challenge_id` и 6-значным кодом TOTP.

**Ошибки**

| Код | Причина |
|-----|-------- |
| `401` | Неизвестный или отключённый токен. |
| `429` | Превышен лимит запросов (5 req/мин на IP или хэш токена). |

### `POST /auth/verify` — завершить TOTP-challenge

**Тело запроса**

| Поле | Тип | Обязательное | Описание |
|------|-----|--------------|---------- |
| `challenge_id` | string | **да** | Из ответа `POST /auth/login`. |
| `code` | string | **да** | 6-значный код TOTP. |

**Ответ 200**

```json
{
  "session": "mnk_session_...",
  "expires_at": "2026-06-18T02:00:00+00:00"
}
```

Также устанавливает `Set-Cookie: mnemos_session=...; HttpOnly; Secure; SameSite=Strict`.

**Ошибки**

| Код | Причина |
|-----|-------- |
| `401` | Недействительный или истёкший challenge, неверный TOTP-код. |
| `429` | Превышен лимит запросов (5 req/мин на challenge). |

### `POST /auth/logout` — инвалидировать сессию

Требует валидной сессии (заголовок или куки). Удаляет строку сессии на сервере и сбрасывает куки.

**Ответ 200**

```json
{"ok": true}
```

### `GET /auth/me` — информация о текущей сессии

Требует валидной сессии.

**Ответ 200**

```json
{
  "token_id": "tok_...",
  "totp": false,
  "expires_at": "2026-06-18T02:00:00+00:00"
}
```

---

## Здоровье и метрики

### `GET /health`

Liveness probe.

**Ответ 200**

```json
{"status": "ok"}
```

### `GET /metrics`

Метрики в стиле Prometheus (наблюдаемость M5). Сейчас возвращает ту же структуру, что и агрегированная статистика `GET /memories`:

```json
{
  "status": "ok",
  "version": "4.0.0",
  "data_dir": "/home/you/.vesma/data",
  "vault_path": "/home/you/.vesma/vault",
  "total": 142,
  "by_status": {"raw": 5, "processing": 0, "processed": 12, "published": 120, "archived": 5},
  "vectors": 120
}
```

---

## Теги

### `GET /tags` — список тегов с количеством

Возвращает все уникальные теги в хранилище памяти с количеством использования.

**Ответ 200** — массив объектов тегов

| Поле | Тип | Описание |
|------|-----|---------- |
| `tag` | string | Полная строка тега (напр. `project:vesma`). |
| `count` | int | Количество записей с этим тегом. |

Отсортировано по `count` убывающе; при равенстве — по `tag` возрастающе (алфавитно).

**Пример**

```bash
curl -s http://127.0.0.1:8000/tags
```

```json
[
  {"tag": "project:vesma", "count": 142},
  {"tag": "agent:tech-writer", "count": 58},
  {"tag": "mnemos:learning", "count": 41}
]
```

### `POST /tags/rename` — массовое переименование префикса тегов

Переименовывает все теги вида `from_prefix:<subtype>` → `to_prefix:<subtype>` (кейс миграции GCW → mnemos). Зеркалирует MCP-инструмент `mnemos_tags_rename` и CLI-команду `vesma tags rename`. Безопасно по построению: обычный `UPDATE` (FTS5-индекс external content остаётся консистентным), и `dry_run` по умолчанию `true` — ничего не пишется, пока вызывающий явно не передаст `dry_run: false`.

**Тело запроса**

| Поле | Тип | По умолчанию | Описание |
|------|-----|--------------|----------|
| `from_prefix` | string | — | Исходный префикс (напр. `gcw`). |
| `to_prefix` | string | — | Целевой префикс (напр. `vesma`). |
| `subtypes` | string[] \| null | `null` | Опциональный whitelist подтипов для переименования. |
| `dry_run` | bool | `true` | Предпросмотр без записи. |
| `project` / `agent` | string \| null | `null` | Ограничить переименование одним проектом / агентом. |
| `invalid_subtypes_to_legacy` | bool | `false` | Переименовывать невалидные подтипы в `<to_prefix>legacy` вместо пропуска. |

**Ответ 200** — отчёт о переименовании (предпросмотр или применённое), включая счётчик `changed`.

---

## CRUD записей

### `POST /memories` — создать запись {#create-memory}

Контракт тегов M2 соблюдается на стороне сервера. Эндпоинт извлекает `project` и `agent` из тегов и сохраняет их как денормализованные столбцы для быстрой фильтрации.

**Тело запроса** — см. [MemoryCreate](../architecture/overview.md#структура-данных)

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `content` | string | **да** | — | Основной текст. |
| `title` | string | нет | авто | Краткий заголовок. |
| `tags` | string[] | **да** | — | Должны включать `project:<slug>`, `agent:<slug>` и хотя бы один `vesma:<subtype>`. |
| `source` | string | нет | `manual` | Одно из `manual`, `web`, `file`, `mcp`, `obsidian`, `cli`, `rule`, `synthesized`. |
| `source_url` | string | нет | — | URL происхождения. |
| `memory_type` | string | нет | `note` | Одно из `note`, `fact`, `snippet`, `bookmark`, `conversation`, `session_context`. |
| `status` | string | нет | `raw` | Одно из `raw`, `processing`, `processed`, `published`, `archived`. |
| `filter_profile` | string | нет | — | Одно из `log`, `terminal`, `code`, `docs`, `web`, `default`. |
| `metadata` | object | нет | `{}` | Произвольное хранилище ключей/значений. Одна зарезервированная конвенция (ADR-0027 Фаза 0): doc-группировочная тройка `{doc_id, chunk_idx, heading_path}` — **всё-или-ничего**: словарь metadata с любым из трёх ключей обязан содержать все три в корректной форме, иначе запрос отклоняется 422 (валидация на записи; неполная тройка разорвала бы документ при группировке по `doc_id` в docs-as-memory-ридере Фазы 3). |
| `category` | string | нет | — | Произвольная метка категории. |

**Ответ 201** — полный объект [`Memory`](#схема-memory-memory-schema).

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/memories \
  -H "Content-Type: application/json" \
  -d '{
    "content": "Use uv, not pip — it resolves transitive CVE closure correctly.",
    "tags": ["project:vesma", "agent:tech-writer", "mnemos:learning"]
  }'
```

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "content": "Use uv, not pip — it resolves transitive CVE closure correctly.",
  "title": "Use uv, not pip",
  "tags": ["project:vesma", "agent:tech-writer", "mnemos:learning"],
  "source": "manual",
  "memory_type": "note",
  "status": "raw",
  "project": "vesma",
  "agent": "tech-writer",
  "created_at": "2026-06-15T10:42:00+00:00",
  "updated_at": "2026-06-15T10:42:00+00:00",
  "raw_content": null,
  "metadata": {}
}
```

**Ошибки**

| Код | Причина |
|-----|-------- |
| `422` | Отсутствует обязательный тег (`project:`, `agent:` или `vesma:`) |
| `500` | Сбой записи SQLite / vault |

### `GET /memories/{memory_id}` — получить одну запись

**Параметры пути**

| Имя | Тип | Описание |
|-----|-----|---------- |
| `memory_id` | UUID | Идентификатор записи. |

**Query-параметры**

| Имя | Тип | По умолчанию | Описание |
|-----|-----|-------------|---------- |
| `include_raw` | bool | `false` | При true включает `raw_content`. |

**Ответ 200** — полный объект [`Memory`](#схема-memory-memory-schema).

**Ответ 404** — `{"detail": "Memory <id> not found"}`.

**Пример**

```bash
curl -s http://127.0.0.1:8000/memories/550e8400-e29b-41d4-a716-446655440000
```

### `GET /memories` — список последних

**Query-параметры**

| Имя | Тип | По умолчанию | Описание |
|-----|-----|-------------|---------- |
| `status` | string | — | Фильтр по значению enum `MemoryStatus`. |
| `project` | string | — | Ограничить проектом. |
| `task` | string | — | ADR-0027 Phase 2 (epic #308): опциональная область задачи — «голый» slug (`[a-z0-9_-]{1,64}`, без префикса `task:`). Байт-в-байт эквивалентен добавлению `task:<slug>` к `tags`; с `tags` комбинируется пересечением. Неисправимые slug → `400`. |
| `limit` | int | `20` | Максимум строк. Жёсткий cap `500`. |

**Ответ 200** — массив объектов [`Memory`](#схема-memory-memory-schema) (без `raw_content`).

**Пример**

```bash
curl -s "http://127.0.0.1:8000/memories?project=vesma&limit=10"
```

---

## Жизненный цикл workflow

Три маршрута управляют state-машиной workflow записи (`open → in-progress → blocked / resolved / done / withdrawn`; `blocked → done` запрещён, терминальные состояния финальны). Каждый переход попадает в audit-лог.

### `GET /memories/{memory_id}/workflow` — текущий статус

Возвращает текущий статус workflow и владельца блокировки. `workflow_status` нормализуется в `open`, если workflow записи никогда не задавали. `404`, если записи не существует.

### `POST /memories/{memory_id}/workflow` — переход статуса

**Тело запроса**

| Поле | Тип | По умолчанию | Описание |
|------|-----|--------------|----------|
| `to` | string | — | Целевой статус (`open`, `in-progress`, `blocked`, `resolved`, `done`, `withdrawn`). |
| `actor` | string | — | Идентификатор вызывающего (обязателен; попадает в audit-лог). |
| `reason` | string | `""` | Свободная причина (обязательна при `force=true`). |
| `force` | bool | `false` | Перекрыть блокировку другого актора. |

Нарушения state-машины / guardrail'ов возвращают `409`. Менеджер — единственный источник истины, валидация на маршруте не дублируется.

### `DELETE /memories/{memory_id}/workflow` — отзыв (отмена)

`DELETE` — это **отмена**: workflow завершается в `withdrawn` — терминальном, необратимом состоянии; блокировка снимается как побочный эффект, но запись не возвращается в возобновляемое состояние.

| Query-параметр | Тип | По умолчанию | Описание |
|----------------|-----|--------------|----------|
| `actor` | string | — | Обязателен (`422` при отсутствии). |
| `reason` | string | `"DELETE withdraw"` | Свободная причина. |
| `force` | bool | `false` | Перекрыть чужую блокировку. |

---

## Поиск {#search}

### `POST /search` — гибридный поиск

RRF-слияние FTS5 и векторной ветки. По умолчанию ищет только среди `published`-записей.

**Семантика запроса:** FTS5-ветка трактует ВСЮ строку `query` как одну цитированную фразу (токены подряд, в порядке следования — `_build_fts_query` заключает весь ввод в кавычки). Запрос из набора ключевых слов вида `postgres migration` найдёт только точную фразу; чтобы найти отдельные ключевые слова, делайте отдельные запросы по одному термину.

**Тело запроса** — см. `SearchQuery` в `src/vesmaro/models.py`

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `query` | string | **да** | — | Строка поиска на естественном языке. Матчится FTS5-веткой как ОДНА целая фраза — см. «Семантика запроса» выше. |
| `tags` | string[] | нет | — | Фильтр: все эти теги должны присутствовать. |
| `project` | string | нет | — | Ограничить проектом. |
| `task` | string | нет | — | ADR-0027 Phase 2 (epic #308): опциональная область задачи — «голый» slug (`[a-z0-9_-]{1,64}`, без префикса `task:`). Байт-в-байт эквивалентен `tags=["task:<slug>"]` (поверхность arm-C из F1); с `tags` комбинируется пересечением. Неисправимые slug → `400` (сначала нормализуются: `My Task` → `my-task`). |
| `limit` | int | нет | `20` | Максимум результатов. |
| `include_raw` | bool | нет | `false` | При true возвращает `raw_content` вместо очищенного контента. |

**Ответ 200** — массив результатов

| Поле | Тип | Описание |
|------|-----|---------- |
| `id` | UUID | ID записи. |
| `title` | string | Авто/явный заголовок. |
| `content` | string | Очищенный контент или raw при `include_raw=true`. |
| `tags` | string[] | Список тегов. |
| `score` | float | RRF-скор, чем выше — тем лучше. |
| `search_type` | string | Всегда `hybrid` здесь. |

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "embedding model", "project": "vesma", "limit": 5}'
```

```json
[
  {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "title": "Use uv, not pip",
    "content": "Use uv, not pip — it resolves transitive CVE closure correctly.",
    "tags": ["project:vesma", "agent:tech-writer", "mnemos:learning"],
    "score": 0.812,
    "search_type": "hybrid"
  }
]
```

---

## Per-agent recall (M3) {#agent-recall}

### `GET /recall/agent/{name}` — отзыв агента

Возвращает последние записи одного агента, опционально фильтруя по проекту и/или подзапросу.

**Параметры пути**

| Имя | Тип | Описание |
|-----|-----|---------- |
| `name` | string | Slug агента. |

**Query-параметры**

| Имя | Тип | По умолчанию | Описание |
|-----|-----|-------------|---------- |
| `project` | string | — | Ограничить проектом. |
| `q` | string | — | Опциональный FTS/векторный подзапрос. |
| `task` | string | — | ADR-0027 Phase 2 (epic #308): опциональная область задачи — «голый» slug (`[a-z0-9_-]{1,64}`, без префикса `task:`). Байт-в-байт эквивалентен фильтру по тегу `task:<slug>`; сужает записи агента до одной области задачи на обеих ветках. Неисправимые slug → `400`. |
| `limit` | int | `20` | Максимум строк. Жёсткий cap `100`. |

**Ответ 200** — массив объектов:

| Поле | Тип |
|------|-----|
| `id` | UUID |
| `title` | string |
| `content` | string |
| `tags` | string[] |
| `created_at` | ISO 8601 string |

**Пример**

```bash
curl -s "http://127.0.0.1:8000/recall/agent/cr-security-reviewer?project=vesma&limit=5"
```

---

## Контекст сессии (save / recall)

Эти эндпоинты зеркалируют плагин-инструменты `mnemos_save_context` и
`mnemos_recall_context`. Они сохраняют и извлекают записи типа
`session_context` с тегом `mnemos:checkpoint`, позволяя агенту восстановить
рабочее состояние между сессиями или после компакции контекста.

### `POST /context/save` — сохранить чекпойнт сессии

Формирует структурированный Markdown из переданных полей и сохраняет его как
запись `SESSION_CONTEXT` с тегом `mnemos:checkpoint`. Зеркалирует плагин-инструмент
`mnemos_save_context`.

**Тело запроса**

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `project` | string | **да** | — | Slug проекта для чекпойнта. |
| `goals` | string | нет | — | Активная цель одним предложением. |
| `completed` | string | нет | — | Список завершённой работы. |
| `in_progress` | string | нет | — | Ближайшее следующее действие. |
| `decisions` | string | нет | — | Решения, которые стоит сохранить. |
| `context` | string | нет | — | Пути к файлам, заметки по архитектуре, подводные камни. |
| `agent` | string | нет | `"user"` | Идентичность агента — валидируемый канал идентичности (при указании — непустая строка, из одних пробелов отклоняется). Должен совпадать с серверной привязкой session→agent, если передан `session`. |
| `session` | string | нет | — | Идентификатор сессии, привязывающий чекпойнт к разговору; первое предъявление фиксирует привязку session→agent на сервере (первый писавший выигрывает). |
| `task` | string | нет | — | ADR-0027 Phase 2 (epic #308): опциональная область задачи — «голый» slug (`[a-z0-9_-]{1,64}`, без префикса `task:`). Штампует тег `task:<slug>` на чекпойнте на границе сохранения (максимум одна задача на запись); отзывается через `task=` в `POST /context/recall` / `POST /search` / `GET /memories`. Дедуп-попадание возвращает первую запись с ЕЁ областью задачи. Неисправимые slug → `400`. |

**Ответ 201**

```json
{
  "status": "saved",
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "title": "Session checkpoint — 2026-07-07T12:00:00+00:00",
  "duplicate": false
}
```

Флаг `duplicate` равен `true`, когда идентичный чекпойнт того же `(project, agent)` уже существует: возвращается существующий id, ничего нового не сохраняется (форма ответа аддитивна — старые клиенты могут игнорировать флаг).

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/context/save \
  -H "Content-Type: application/json" \
  -d '{
    "project": "vesma",
    "goals": "Закончить HTTP API доки для всех 15 инструментов",
    "completed": "- Обновлён plugin.yaml\n- Обновлён prompt mode",
    "in_progress": "Документировать /context/save и /context/recall",
    "decisions": "- CCR-эндпоинты идут перед секцией Knowledge pipeline",
    "context": "См. docs/en/user/http-api.md"
  }'
```

**Ошибки**

| Код | Причина |
|-----|-------- |
| `400` | Не прошла валидация идентичности (пустые `agent`/`session` или из одних пробелов), неисправимый slug `task`, либо тривиальный отказ: все пять полей полезной нагрузки пусты — ничего не сохраняется |
| `409` | `session` уже привязана к другому агенту (серверная привязка session→agent, первый писавший выигрывает) |
| `422` | Отсутствует обязательное поле `project` (валидация Pydantic) |
| `500` | Сбой записи SQLite / vault |

### `POST /context/recall` — отозвать контекст сессии

Возвращает последние чекпойнт-записи для проекта, опционально отфильтрованные
подзапросом. Зеркалирует плагин-инструмент `mnemos_recall_context`.

**Тело запроса**

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `project` | string | **да** | — | Slug проекта для отзыва. |
| `query` | string | нет | — | Опциональный FTS/векторный подзапрос для фокуса. |
| `task` | string | нет | — | ADR-0027 Phase 2 (epic #308): опциональная область задачи — «голый» slug (`[a-z0-9_-]{1,64}`, без префикса `task:`). Байт-в-байт эквивалентен фильтру тега чекпойнта плюс `task:<slug>` (поверхность arm-C из F1): возвращает только чекпойнты, сохранённые в этой задаче. Неисправимые slug → `400`. |
| `limit` | int | нет | `5` | Максимум чекпойнтов. |

**Ответ 200 — с контекстом**

```json
{
  "project": "vesma",
  "checkpoints": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "title": "Session checkpoint — 2026-07-07T12:00:00+00:00",
      "content": "# Session checkpoint — 2026-07-07T12:00:00+00:00\n\n## Goals\nЗакончить HTTP API доки для всех 15 инструментов\n",
      "tags": ["project:vesma", "agent:user", "mnemos:checkpoint"],
      "created_at": "2026-07-07T12:00:00+00:00"
    }
  ]
}
```

**Ответ 200 — нет контекста**

```json
{
  "project": "vesma",
  "checkpoints": [],
  "message": "No context found. Start by saving context with POST /context/save."
}
```

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/context/recall \
  -H "Content-Type: application/json" \
  -d '{"project": "vesma", "limit": 3}'
```

---

## Контракт провайдера (ADR-0017 D1)

### `POST /context/assemble` — сборка контекстного блока перед вызовом LLM

Зеркало MCP-инструмента `mnemos_assemble_context` через тот же путь менеджера
(mnemos #125, волна 1). Фиксированный конвейер: гибридный RRF recall
(статусный гейт инварианта входа — наружу выходят только `published`/
`processed`) → опциональный разворот CCR-маркеров → контекстный фильтр →
**обязательный** скан секретов (редакции считаются по блокам; refuse-режим
выбрасывает блок) → CacheAligner → токен-бюджет. Каждый внедряемый блок
несёт строку провенанса
`[mnemos:<id> project=<slug> status=<status> origin=<source> pipeline=<phase> v=<n> retrieved=<iso>]`
(сегмент `pipeline=` опускается для легаси-строк с NULL pipeline_state).
`retrieved=` привязан к сессии (#282): штампуется при первой сборке
сессии и не меняется при всех последующих сборках той же сессии, поэтому
префикс блока байт-стабилен для KV-кэширования на стороне харнесса.

**Тело запроса**

| Поле | Тип | Обяз. | По умолч. | Описание |
|------|-----|-------|-----------|----------|
| `session` | string | **да** | — | Идентификатор сессии вызывающего (эхом в результате). |
| `project` | string | **да** | — | Слаг проекта: ограничивает recall и погашение CCR. |
| `agent` | string | нет | `null` | Слаг агента вызывающего — образует с `session` контекст эмитента: с ним стадия разворота CCR-маркеров идёт под строгой валидацией; без него строгое окружение пропускает разворот маркеров со штампом эмитента (легаси-строки с NULL-эмитентом разворачиваются). |
| `file` | string | нет | `null` | Путь к файлу: термины recall-запроса + пиннинг applyTo-правил. |
| `budget` | integer | нет | `2048` | Токен-бюджет (`1..1_000_000`). |
| `mode` | string | нет | `sync` | `sync` / `async` (сохранение + handle) / `code` / `prose` (фильтр contentType). |
| `expand_ccr` | boolean | нет | `false` | Включить опциональную стадию разворота CCR. |
| `async_handle` | string | нет | `null` | Забрать (однократно) результат, сохранённый через `mode="async"`. Привязан к сессии: выкупить может только собравшая сессия; несовпадение → 422, handle не изымается. |

**Ответы**

- `200` — ContextBlock: `text` (блоки с провенансом), `blocks[]`
  (по-блочный провенанс, счётчики редакций и токенов), `tokens`
  (`budget`, `estimated`), `stats` (телеметрия по стадиям, включая дословный
  порядок стадий; `stats.recall.query_source` равен `explicit`, если recall
  вёл явный запрос/`context_hint`, иначе `derived`; `stats.ccr.skipped_refused`
  считает маркеры, чей разворот отклонён строгой валидацией). При
  `mode="async"` — только конверт с handle.
- `422` — ошибка валидации на границе (некорректные `session`/`project`/
  `mode`/`budget`, неизвестный `async_handle` или handle, принадлежащий
  другой сессии).

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/context/assemble \
  -H "Content-Type: application/json" \
  -d '{"session": "sess-42", "project": "vesma", "file": "src/manager.py", "budget": 1024}'
```

Полная документация по полям: [`mcp-tools.md` → `mnemos_assemble_context`](mcp-tools.md#mnemos_assemble_context).

### `POST /context/rewrite` — сообщить о перезаписи контекста (ADR-0018)

Зеркало MCP-инструмента `mnemos_context_rewrite` через тот же путь менеджера
(mnemos #125, волна 2). Харнесс сообщает, что перезаписал блок своего
рабочего контекста; оригинал без поток попадает в LTM через обычный
knowledge-конвейер (входит как `raw`, в контексте достижим только после
продвижения конвейером), идемпотентен по content-addressed ключу события,
без версий (линия замены — опциональное ребро `supersedes`).

**Тело запроса**

| Поле | Тип | Обяз. | По умолч. | Описание |
|------|-----|-------|-----------|----------|
| `content` | string | **да** | — | Оригинальный текст заменённого блока — источник истины, сохраняется без изменений. |
| `project` | string | **да** | — | Слаг проекта (тег `project:<slug>`). |
| `agent` | string | **да** | — | Слаг агента (тег `agent:<slug>`). |
| `session` | string | нет | `null` | Идентификатор сессии — провенанс, часть ключа идемпотентности. |
| `supersedes` | string | нет | `null` | id заменяемой записи — создаёт ребро `supersedes` новый → старый (должна существовать). |
| `diff` | string | нет | `null` | Адвизорный diff was→becomes — хранится в метаданных, никогда не load-bearing. |
| `include_marker` | boolean | нет | `false` | Также вернуть CCR compress-маркер оригинала. |

**Ответы**

- `200` — квитанция события: `status` (`stored` / `deduplicated` — событие
  идемпотентно, повторная доставка это 200, а не 201), `memory_id`,
  `memory_status` (`raw` при первой доставке), `event_key`, эхо
  `project`/`agent`/`session`, `supersedes`
  (`{"to_memory_id", "edge_created"}` или `null`) и `ccr_marker` при
  `include_marker=true`. Версионных полей и полей порядка нет — by design.
- `429` — превышена квота записи: более
  `mnemos.context_rewrite_rate_limit_per_minute` (по умолчанию 30)
  СОХРАНЁННЫХ событий на `(project, session)` за последнюю минуту.
  Дедуплицированные повторные доставки квоту не расходуют.
- `422` — ошибка валидации на границе (пустые обязательные поля, пустые
  опциональные строки, нарушение размерных лимитов — `content` > 1 МиБ /
  `diff` > 256 КиБ по умолчанию, нарушение контракта тегов в
  strict-режиме или цель `supersedes`, не найденная в этом проекте, —
  сообщение не различает «не найдено» и «запись другого проекта»).

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/context/rewrite \
  -H "Content-Type: application/json" \
  -d '{"content": "<текст оригинального блока>", "project": "vesma", "agent": "zcode",
       "session": "sess-42", "supersedes": "3f2a…", "diff": "was: v1 → became: v2",
       "include_marker": true}'
```

Полная документация по полям: [`mcp-tools.md` → `mnemos_context_rewrite`](mcp-tools.md#mnemos_context_rewrite).

---

## Хуки жизненного цикла (ADR-0017 D1 / ADR-0018, #125 Wave 3)

Один параметрический маршрут на три хука жизненного цикла — REST-близнец
группового MCP-инструмента `mnemos_hooks` (оба идут через один и тот же
роутер `dispatch_hook`). Идентичность (`session`, `project`, `agent`)
обязательна на каждом вызове; для `post_tool_call` это мандат реестра A2 N2 —
вызов compress всегда протаскивает `(agent, session)` вызывающего в строку
кэша, чтобы строгая валидация маркеров могла позже доказать провенанс.

### `POST /hooks/{action}` — выполнить один хук

`action` ∈ `pre_llm_call` | `on_session_start` | `post_tool_call` (неизвестное →
404). Поля конкретного действия валидируются границей хуков (`ValueError` →
422): `context_hint`/`file`/`budget`/`task` для `pre_llm_call`, `limit` для
`on_session_start`, `tool_name`/`output_text`/`auto_compress`/`profile` для
`post_tool_call`. `output_text` ограничен капом `hooks.max_output_chars`
(по умолчанию 1 048 576 символов, конвенция капов context-rewrite; `0`
отключает) — превышение отклоняется с 422 ДО любой записи.

**Тело запроса**

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|----------|
| `session` | string | **да** | — | Идентификатор сессии вызывающего. |
| `project` | string | **да** | — | Slug проекта. |
| `agent` | string | **да** | — | Slug агента вызывающего (идентичность эмитента). |
| `context_hint` | string | нет | — | `pre_llm_call`: явный recall-запрос (о чём вызов модели). Семантика FTS5 — вся подсказка матчится как ОДНА цитированная фраза (токены подряд, в порядке), а не набор ключевых слов. |
| `file` | string | нет | — | `pre_llm_call`: опциональный путь к файлу. |
| `task` | string | нет | — | `pre_llm_call` (ADR-0027 Фаза 0): опциональная task-область — «голый» slug задачи (`[a-z0-9_-]{1,64}`, без префикса `task:`). Сужает recall до записей с тегом `task:<slug>` (доктрина пересечения; только хвост — закреплённые префиксы и формат провенанса не тронуты). |
| `budget` | integer | нет | `2048` | `pre_llm_call`: бюджет токенов. |
| `limit` | integer | нет | `5` | `on_session_start`: количество чекпоинтов. |
| `tool_name` | string | `post_tool_call` | — | Инструмент, породивший вывод. |
| `output_text` | string | `post_tool_call` | — | Сырой вывод инструмента для сжатия. |
| `auto_compress` | boolean | нет | ручка | `post_tool_call`: точечное переопределение `hooks.auto_compress` (по умолчанию `false`). |
| `include_awareness` | boolean | нет | `false` | `pre_llm_call`/`on_session_start` (mnemos #254): скомпоновать секцию awareness-дельты И «операционную картину» swarm v0a/v0b (наблюдаемые счётчики/ids/времена плюс заявленная каждым соседом задача — самоподанное утверждение с меткой `[unverified]`) — добавляются ПОСЛЕДНИМИ, никогда не закрепляются; курсор awareness двигается только в `pre_llm_call`. Выключено (по умолчанию) = байт-идентичный вывод. Кап частоты по `(project, agent)` (`vesmaro.awareness_picture_rate_limit_per_minute`, по умолчанию 30) — при превышении деградация в rate-limit-строку, никогда 5xx. |
| `profile` | string | нет | авто | `post_tool_call`: подсказка профиля фильтра. |

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/hooks/post_tool_call \
  -H "Content-Type: application/json" \
  -d '{"session": "sess-42", "project": "vesma", "agent": "zcode",
       "tool_name": "bash", "output_text": "<60+ строк build-лога>",
       "auto_compress": true}'
```

Ответы по действиям: `pre_llm_call` → собранный ContextBlock (та же форма,
что `POST /context/assemble`, доставка строго синхронная); `on_session_start`
→ `{checkpoints: [{id, content, created_at, redactions, …}], redactions}`
(контент сканируется на выдаче в самом канале); `post_tool_call` → конверт CCR
с `compressed_text`/`marker` для подстановки (или выключенный конверт
`{auto_compress: false, compressed: false}`). С `include_awareness=true`
`pre_llm_call` дополнительно возвращает ключ `awareness`, а отрендеренные
секции awareness и операционной картины едут в хвосте `text`
(см. [`mcp-tools.md` → `mnemos_awareness`](mcp-tools.md#mnemos_awareness)).
Полная документация по полям:
[`mcp-tools.md` → `mnemos_hooks`](mcp-tools.md#mnemos_hooks).

---

## Обратимое сжатие (CCR)

Эти эндпоинты реализуют **Compress-Cache-Retrieve (CCR)** — сжатие без потери
данных для большого контента (логи, JSON, вывод команд). Оригинал кэшируется
по SHA-256 хэшу; в сжатый текст встраивается короткий парсируемый маркер,
чтобы полный оригинал можно было извлечь по требованию. Типичное сжатие —
70–90% на логах.

### `POST /compress` — сжать контент

Сжимает `text` через CCR, кэширует оригинал и возвращает сжатый текст плюс
маркер с хэшем. Контент короче ~500 символов возвращается как есть (не
кэшируется). Зеркалирует плагин-инструмент `mnemos_compress`.

**Тело запроса**

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|----------|
| `text` | string | **да** | — | Контент для сжатия. ≥500 символов для кэширования. |
| `profile` | string | нет | авто | Профиль фильтра: `log`, `terminal`, `code`, `docs`, `web`, `default`. Авто-определение, если опущен. |
| `project` | string | нет | `""` | Slug проекта для области кэша. |
| `agent` | string | нет | — | **Реестр эмитентов A2:** slug агента вызывающего — записывается в строку кэша как эмитент маркера (для строгой проверки происхождения). |
| `session` | string | нет | — | **Реестр эмитентов A2:** id сессии вызывающего, сохраняется вместе с `agent`. |

**Ответ 200**

```json
{
  "compressed_text": "[compressed: a1b2c3... 8.2KB → 0.9KB (89%)]\n## Top 5 lines by frequency\nERROR Connection refused (×42)\n...",
  "hash": "a1b2c3d4e5f6...",
  "original_size": 8200,
  "compressed_size": 900,
  "reduction_pct": 89.0,
  "marker": "[compressed: a1b2c3... 8.2KB → 0.9KB (89%)]",
  "cached": true,
  "profile": "log"
}
```

При `cached: false` (контент < ~500 символов) поле `hash` пустое, а
`compressed_text` равен входному `text`.

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/compress \
  -H "Content-Type: application/json" \
  -d '{"text": "<8KB логов>", "profile": "log", "project": "vesma"}'
```

### `POST /retrieve` — извлечь оригинал из CCR-кэша

Извлекает оригинальный несжатый контент по хэшу маркера CCR. Если `query`
опущен — возвращает полный оригинал. Если `query` задан — возвращает
FTS5-ранжированные сниппеты из оригинала (полезно, когда оригинал большой,
а релевантны несколько строк). Зеркалирует плагин-инструмент
`mnemos_retrieve`.

**Тело запроса**

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|----------|
| `hash` | string | **да** | — | SHA-256 хэш из маркера `[compressed: ...]`. |
| `query` | string | нет | — | Поисковый запрос для извлечения сниппетов. |
| `snippet_count` | int | нет | `5` | Количество сниппетов при заданном `query`. |
| `project` | string | нет | — | Slug проекта: ограничивает поиск записями проекта — хеш из другого проекта возвращается как не найденный. |
| `validate_marker` | boolean | нет | `ccr.validate_markers` | **Строгий режим A2:** валидировать маркер (существование + целостность `original_chars` + происхождение эмитента) до выдачи контента. |
| `original_chars` | int | нет | — | `N` из маркера `[compressed: <hash> | N→M chars]` — включает проверку целостности. |
| `agent` | string | нет | — | Slug агента вызывающего — доверенный контекст эмитента для проверки происхождения. |
| `session` | string | нет | — | Id сессии вызывающего — в паре с `agent` как доверенный контекст эмитента. |

**Строгая валидация маркера A2.** Запрос имеет *форму маркера*, если несёт любое из полей `original_chars` / `agent` / `session`. В строгом режиме запрос формы маркера обязан пройти: (1) **существование** по `(project, hash)` — область `project` в строгом режиме ОБЯЗАТЕЛЬНА: `validate_marker=true` (или включённый переключатель) без `project` отклоняется с `reason="marker validation failed: existence: project scope required for marker validation"` и без контента; (2) **целостность** — `N` маркера равен длине сохранённого оригинала в символах; (3) **происхождение** — реестр эмитентов строки кэша (записан при сжатии) совпадает с парой `(agent, session)` вызывающего (`null`-сессия соответствует только `null`-сессии эмитента). Непройденная проверка возвращает:

```json
{
  "hash": "a1b2c3d4e5f6...",
  "found": true,
  "refused": true,
  "reason": "marker validation failed: provenance: issuer mismatch: ...",
  "redactions": 0,
  "redacted_patterns": {}
}
```

— без `original`/`snippets` (fail-closed), `retrieval_count` не инкрементируется. Причины — фиксированные строки без oracle-утечек: длины и пары эмитентов никогда не возвращаются. **Закрытие hash-only (раунд ревью F2):** в строгом режиме запрос только с хешем по строке со штампом эмитента отклоняется с `reason="marker validation required"` (без контента); легаси-строки с NULL-эмитентом остаются доступными по hash-only с WARNING. Обычные hash-only запросы при выключенном переключателе не затрагиваются. `POST /context/assemble` с `expand_ccr=true` принимает `agent` (в паре с `session`), чтобы расширение шло в контексте эмитента вызывающего; без полной идентичности строгое развёртывание пропускает расширение штампованных маркеров (маркер остаётся; статистика CCR-стадии несёт `skipped_refused`). Остаточный риск: посев внутри проекта доверенным вызывающим compress принят (реестр остаточных рисков ADR-0018, single-operator).

**Ответ 200 — полное извлечение (без `query`)**

```json
{
  "hash": "a1b2c3d4e5f6...",
  "found": true,
  "original": "<полный оригинальный текст>",
  "size_bytes": 8200,
  "retrieval_count": 1
}
```

**Ответ 200 — извлечение сниппетов (с `query`)**

```json
{
  "hash": "a1b2c3d4e5f6...",
  "found": true,
  "query": "Connection refused",
  "snippets": [
    "ERROR Connection refused (×42)",
    "WARN Retrying after Connection refused on host:3306"
  ],
  "retrieval_count": 1
}
```

**Ответ 200 — не найдено**

```json
{
  "hash": "000000000000...",
  "found": false,
  "retrieval_count": 0
}
```

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/retrieve \
  -H "Content-Type: application/json" \
  -d '{"hash": "a1b2c3d4e5f6...", "query": "Connection refused", "snippet_count": 3}'
```

---

## Сигнал авто-сбора

### `GET /auto-collect` — вектор сигналов компакции

Возвращает счётчик вызовов в процессе / сигналы прошедшего времени плюс
клиентские эвристические слоты. Поле `recommendation` равно
`"save_checkpoint"`, когда счётчик вызовов или прошедшее время превышают
порог, иначе `"ok"`. Зеркалирует плагин-инструмент
`mnemos_auto_collect_status`.

**Ответ 200**

```json
{
  "auto_collect_enabled": true,
  "signals": {
    "call_counter": {
      "calls_since_save": 12,
      "threshold": 25,
      "triggered": false
    },
    "elapsed_secs": {
      "value": 3600,
      "threshold": 1800,
      "triggered": true
    },
    "context_size_heuristic": {"value": null, "note": "populated by client"},
    "summary_marker_detected": {"value": null, "note": "populated by client"},
    "reference_drop_heuristic": {"value": null, "note": "populated by client"}
  },
  "recommendation": "save_checkpoint",
  "next_reminder_in_calls": 13
}
```

**Пример**

```bash
curl -s http://127.0.0.1:8000/auto-collect
```

---

## URL-ингест

### `POST /ingest-url` — получить и сохранить веб-страницу

Загружает веб-страницу, извлекает основной текст (через trafilatura) и
сохраняет как `RAW`-запись. Учётные данные, встроенные в URL, удаляются
перед сохранением (OWASP A02). Теги валидируются через теговый контракт
проекта. Зеркалирует плагин-инструмент `mnemos_ingest_url`.

**Тело запроса**

| Поле | Тип | Обязательное | Описание |
|------|-----|--------------|---------- |
| `url` | string | **да** | HTTP/HTTPS URL для загрузки. |
| `tags` | string[] | **да** | Должны включать `project:<slug>`, `agent:<slug>` и хотя бы один `vesma:<subtype>`. |

**Ответ 201**

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "title": "FastAPI dependency injection guide",
  "url": "https://fastapi.tiangolo.com/tutorial/dependencies/"
}
```

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/ingest-url \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://fastapi.tiangolo.com/tutorial/dependencies/",
    "tags": ["project:vesma", "agent:tech-lead", "mnemos:learning"]
  }'
```

**Ошибки**

| Код | Причина |
|-----|-------- |
| `422` | Отсутствует обязательный тег (`project:`, `agent:` или `vesma:`) или отсутствует `url` |
| `500` | Сбой загрузки, сбой извлечения или сбой записи SQLite / vault |

---

## Ингест документов (ADR-0027, фаза 3)

### `POST /ingest-document` — ингест документа чанками с born-quarantine

Docs-as-memory: текст документа разбивается с сохранением структуры на
чанки по заголовкам (каждая строка несёт метаданные-конвенцию
`{doc_id, chunk_idx, heading_path}`) и каждый чанк попадает в память
**сразу в карантине** — ингестируемые документы недоверены (инвариант 8
ADR-0027), невидимы для выдачи, пока danger-sweep в момент завершения
ингеста их не очистит.

**Жизненный цикл** (дисциплина ADR-0019 §5): born-quarantine → Phase A
danger-детектор прогоняется по всем чанкам (чистые освобождаются в
обычные строки `published`; помеченные остаются в карантине с кодами
классов детектора в операторской причине; ошибки сканера — fail-closed) →
чанково-атомарная семантика освобождения (чистый чанк освобождается, даже
если сосед помечен) → выдача остаётся последней линией (освобождённый
чанк, contaminated после освобождения, всё равно отбрасывается/замазывается
повторным секрет-сканом на каждом канале — инвариант 7 ADR-0027).

**Повторный ингест** (инвариант 4 ADR-0027): повторный ингест того же
`doc_id` заменяет чанки документа и поднимает версию doc-chunk `ccr_cache`
**в той же SQLite-транзакции**. Честная рамка: ключ — **consumer-facing
счётчик инвалидации** (экспонирован в `GET /stats` как
`doc_chunk_cache_version`, та же позиция, что у `graph_epoch`); любой
кеширующий потребитель обязан его читать. В-repo потребителя пока нет.

Зеркалирует плагин-инструмент `mnemos_ingest_document`. `POST /ingest-url`
сохраняет свои однострочные семантики до фазы 3 — граница между
инструментами намеренная.

**Тело запроса**

| Поле | Тип | Обязателен | Описание |
|-------|-----|-----------|----------|
| `text` | string | **да** | Полный текст документа. |
| `doc_id` | string | **да** | Логическая идентичность документа; стабильна при повторном ингесте. |
| `tags` | string[] | **да** | Обязательны `project:<slug>`, `agent:<slug>` и хотя бы один `vesma:<subtype>`. |
| `title` | string | нет | Необязательный заголовок. |
| `source_url` | string | нет | Необязательный URL-источник. |

**Ответ 201**

```json
{
  "doc_id": "dep-guide",
  "chunks_total": 3,
  "released": 2,
  "quarantined": 1,
  "chunk_ids": ["550e8400…", "550e8401…", "550e8402…"],
  "reingest": false,
  "cache_version": 0,
  "truncated": false
}
```

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/ingest-document \
  -H "Content-Type: application/json" \
  -d '{
    "text": "# Деплой\n\nЗапусти раскатку.\n\n# Откат\n\nВерни предыдущий релиз.",
    "doc_id": "dep-guide",
    "title": "Гайд по деплою",
    "tags": ["project:vesma", "agent:tech-lead", "mnemos:learning"]
  }'
```

**Ошибки**

| Код | Причина |
|------|---------|
| `422` | Нет обязательного тега, нет `text`/`doc_id`, или пустой `doc_id` |
| `500` | Ошибка записи SQLite / vault |

---

## Граф проектов (ADR-0032)

Namespace `/graph/` зеркалит десять MCP-инструментов [`vesma_*graph*`](mcp-tools.md#инструменты-графа-проектов-adr-0032) над графом кода зарегистрированного проекта (символы, схемы файлов, сниппеты — [ADR-0032](../../project/adr/0032-project-graph.md)).

> **Включено по умолчанию (решение владельца 2026-09-28).** Явный `code_graph.enabled: false` — и каждый эндпоинт отвечает `503`. REST-поверхность остаётся полностью ручной — индексация запускается только `POST /graph/index`; нативная авто-индексация волны PG-0.5 (авто-регистрация + фоновая индексация при первом контакте через MCP/хуки) — путь MCP/хуков и через HTTP никогда не срабатывает.

**Коды ошибок для всего namespace** — `503` disabled (операторский гейт), `403` конфайнмент (незарегистрированный проект или путь вне зарегистрированного корня, PG2), `413` превышение лимита индексации (fail-closed, PG7), `400` bad request (бюджет не вмещает ни одной строки, некорректные аргументы). Каждый вызов — чтение или запись — аудируется по агенту (PG7); поэтому поле `agent` обязательно везде.

### `POST /graph/index` — индексация зарегистрированного проекта

Полная или инкрементальная индексация зарегистрированного корня проекта. Сериализуется по проекту; лимиты PG7 — fail-closed.

**Тело запроса**

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `project_id` | string | **да** | — | Идентификатор или уникальное имя зарегистрированного проекта. |
| `agent` | string | **да** | — | Идентичность вызывающего (PG7). |
| `session` | string | нет | — | Id сессии для аудита. |
| `incremental` | bool | нет | `true` | Пропустить работу, если ничего не изменилось. |
| `reason` | string | нет | — | Причина для аудита. |

**Ответ 200**

```json
{
  "status": "indexed",
  "nodes": 2143,
  "edges": 5107,
  "files_indexed": 312,
  "files_skipped": 88,
  "poisoned": [],
  "parse_errors": {},
  "duration_sec": 4.212,
  "incremental": true,
  "staleness": { "total_files": 400, "fresh_percent": 100.0, "changed_files": [], "last_indexed_at": "2026-09-28T12:00:04+00:00" }
}
```

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/graph/index \
  -H "Content-Type: application/json" \
  -d '{"project_id": "vesma", "agent": "ci-runner"}'
```

### `GET /graph/status/{project_id}` — статус графа проекта

Объёмы, свежесть, ошибки разбора и число poisoned-файлов. `agent` — обязательный query-параметр; `session` — необязательный.

**Ответ 200**

```json
{
  "project": "vesma",
  "nodes": 2143,
  "edges": 5107,
  "files": 400,
  "parse_errors": {},
  "parse_error_count": 0,
  "poisoned_count": 0,
  "staleness": { "total_files": 400, "fresh_percent": 97.5, "changed_files": ["src/vesmaro/manager.py"], "last_indexed_at": "2026-09-28T12:00:04+00:00" }
}
```

**Пример**

```bash
curl -s "http://127.0.0.1:8000/graph/status/vesma?agent=ci-runner"
```

### `POST /graph/search` — поиск по графу проектов

Ранжированный поиск по имени/квалифицированному имени/пути (точный > префиксный > подстрочный), окно токен-контракта.

**Тело запроса**

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `project_id` | string | **да** | — | Идентификатор или уникальное имя зарегистрированного проекта. |
| `query` | string | **да** | — | Подстрока имени / квалифицированного имени / пути. |
| `agent` | string | **да** | — | Идентичность вызывающего (PG7). |
| `session` | string | нет | — | Id сессии для аудита. |
| `kind` | string | нет | — | Фильтр по виду узла (`Project`, `File`, `Module`, `Class`, `Function`, `Method`, `Type`). |
| `limit` | int | нет | `50` | Максимум ранжированных строк на страницу. |
| `cursor` | int | нет | `0` | Курсор страницы из предыдущего вызова. |
| `max_output_tokens` | int | нет | `3200` | Бюджет вывода (128–1 млн). |
| `include_signature` | bool | нет | `false` | Включить формы сигнатур (opt-in). |

**Ответ 200**

```json
{
  "project": "vesma",
  "query_kind": null,
  "results": [
    { "score": 3, "id": "mnemos#src/vesmaro/codegraph/service.py#window_rows#158", "project": "vesma", "kind": "Function", "name": "window_rows", "qname": "vesmaro.codegraph.service.window_rows", "path": "src/vesmaro/codegraph/service.py", "start_line": 158, "end_line": 190, "lang": "python", "signature": "def window_rows(rows, max_output_tokens, cursor)" }
  ],
  "total_matches": 1,
  "cursor": 0,
  "has_more": false,
  "last_indexed_at": "2026-09-28T12:00:04+00:00"
}
```

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/graph/search \
  -H "Content-Type: application/json" \
  -d '{"project_id": "vesma", "query": "window_rows", "agent": "ci-runner"}'
```

### `POST /graph/trace` — обход пути от символа

BFS по рёбрам графа от одного символа (разрешение по квалифицированному имени; неоднозначные отказы называют поиск). Глубина ≤ 2, лимиты fanout и суммарной работы.

**Тело запроса**

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `project_id` | string | **да** | — | Идентификатор или уникальное имя зарегистрированного проекта. |
| `qname` | string | **да** | — | Квалифицированное имя символа (точное или уникальный хвост). |
| `agent` | string | **да** | — | Идентичность вызывающего (PG7). |
| `session` | string | нет | — | Id сессии для аудита. |
| `depth` | int | нет | `2` | Глубина BFS, 1–2. |
| `max_output_tokens` | int | нет | `3200` | Бюджет вывода (128–1 млн). |

**Ответ 200**

```json
{
  "project": "vesma",
  "start": "vesmaro.codegraph.service.window_rows",
  "depth": 2,
  "nodes": [ { "id": "mnemos#…#window_rows#158", "qname": "vesmaro.codegraph.service.window_rows", "kind": "Function", "path": "src/vesmaro/codegraph/service.py", "start_line": 158, "end_line": 190, "depth": 0 } ],
  "edges": [ { "from": "mnemos#…#window_rows#158", "to": "mnemos#…#resolve_token_budget#135", "kind": "CALLS", "provenance": "tree-sitter" } ],
  "truncated": false,
  "cursor": 0,
  "has_more": false,
  "last_indexed_at": "2026-09-28T12:00:04+00:00"
}
```

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/graph/trace \
  -H "Content-Type: application/json" \
  -d '{"project_id": "vesma", "qname": "vesmaro.codegraph.service.window_rows", "agent": "ci-runner"}'
```

### `POST /graph/outline` — схема символов одного файла

Схема символов одного проиндексированного файла — формы, никогда тела (PG1). Путь относительно корня репозитория, в границах зарегистрированного корня (PG2).

**Тело запроса**

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `project_id` | string | **да** | — | Идентификатор или уникальное имя зарегистрированного проекта. |
| `path` | string | **да** | — | Путь к файлу относительно корня репозитория. |
| `agent` | string | **да** | — | Идентичность вызывающего (PG7). |
| `session` | string | нет | — | Id сессии для аудита. |
| `cursor` | int | нет | `0` | Курсор страницы. |
| `max_output_tokens` | int | нет | `3200` | Бюджет вывода (128–1 млн). |

**Ответ 200**

```json
{
  "project": "vesma",
  "path": "src/vesmaro/codegraph/service.py",
  "lang": "python",
  "outline": [ { "kind": "Function", "name": "window_rows", "qname": "vesmaro.codegraph.service.window_rows", "start_line": 158, "end_line": 190, "signature": "def window_rows(rows, max_output_tokens, cursor)" } ],
  "parse_error": null,
  "cursor": 0,
  "has_more": false,
  "last_indexed_at": "2026-09-28T12:00:04+00:00"
}
```

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/graph/outline \
  -H "Content-Type: application/json" \
  -d '{"project_id": "vesma", "path": "src/vesmaro/codegraph/service.py", "agent": "ci-runner"}'
```

### `POST /graph/snippet` — секрет-сканированный диапазон строк с диска

Чтение диапазона строк С ДИСКА (PG4): отказ poisoned (навсегда) → конфайнмент → проверка индексации → свежесть по mtime+size+sha256 → секрет-скан выдачи (любое попадание отклоняет fail-closed) → целострочное токен-окно. Кэша сниппетов нет.

**Тело запроса**

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `project_id` | string | **да** | — | Идентификатор или уникальное имя зарегистрированного проекта. |
| `path` | string | **да** | — | Путь к файлу относительно корня репозитория. |
| `start_line` | int | **да** | — | Первая строка (с 1). |
| `end_line` | int | **да** | — | Последняя строка (включительно). |
| `agent` | string | **да** | — | Идентичность вызывающего (PG7). |
| `session` | string | нет | — | Id сессии для аудита. |
| `max_output_tokens` | int | нет | `3200` | Бюджет вывода (128–1 млн); целострочные дропы. |

**Ответ 200**

```json
{
  "project": "vesma",
  "path": "src/vesmaro/codegraph/service.py",
  "start_line": 158,
  "end_line": 172,
  "content": "def window_rows(\n    rows: list[dict[str, Any]],\n    ...",
  "total_file_lines": 1081,
  "has_more": true,
  "next_start_line": 173,
  "scanned": true,
  "stale": false
}
```

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/graph/snippet \
  -H "Content-Type: application/json" \
  -d '{"project_id": "vesma", "path": "src/vesmaro/codegraph/service.py", "start_line": 158, "end_line": 172, "agent": "ci-runner"}'
```

### `POST /graph/coverage` — пакетная проверка покрытия

Вердикт по каждому пути: `indexed` / `stale` / `parse-error` / `unindexed` / `poisoned`. Честность покрытия — содержимое проверяйте через `POST /graph/snippet`.

**Тело запроса**

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `project_id` | string | **да** | — | Идентификатор или уникальное имя зарегистрированного проекта. |
| `paths` | string[] | **да** | — | Пути относительно корня репозитория. |
| `agent` | string | **да** | — | Идентичность вызывающего (PG7). |
| `session` | string | нет | — | Id сессии для аудита. |

**Ответ 200**

```json
{
  "project": "vesma",
  "coverage": [
    { "path": "src/vesmaro/manager.py", "verdict": "stale" },
    { "path": "src/vesmaro/codegraph/service.py", "verdict": "indexed" }
  ]
}
```

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/graph/coverage \
  -H "Content-Type: application/json" \
  -d '{"project_id": "vesma", "paths": ["src/vesmaro/manager.py"], "agent": "ci-runner"}'
```

### `GET /graph/schema` — карта контракта графа

Виды узлов и рёбер, токен-контракт, лимиты, версия схемы; необязательный query-параметр `project_id` добавляет объёмы этого проекта.

**Ответ 200**

```json
{
  "schema_version": 1,
  "node_kinds": ["Project", "File", "Module", "Class", "Function", "Method", "Type"],
  "edge_kinds": ["CONTAINS_FILE", "DEFINES", "IMPORTS", "CALLS", "INHERITS", "TESTS", "USES"],
  "token_contract": { "max_output_tokens_default": 3200, "max_output_tokens_min": 128, "max_output_tokens_max": 1000000, "bytes_per_token": 4 },
  "limits": { "index_max_files": 20000, "index_max_source_mb": 500 },
  "trace": { "max_depth": 2, "fanout_cap": 32, "total_work_cap": 512 }
}
```

**Пример**

```bash
curl -s "http://127.0.0.1:8000/graph/schema?agent=ci-runner&project_id=vesma"
```

### `GET /graph/projects` — список проектов графа

Зарегистрированные проекты вместе со статусом их индекса; зарегистрированные, но ещё не индексированные остаются видимыми.

**Ответ 200**

```json
{
  "projects": [
    { "project": "vesma", "registered": true, "has_root": true, "nodes": 2143, "edges": 5107, "files": 400, "poisoned": 0, "last_indexed_at": "2026-09-28T12:00:04+00:00" }
  ],
  "has_more": false,
  "cursor": 0
}
```

**Пример**

```bash
curl -s "http://127.0.0.1:8000/graph/projects?agent=ci-runner"
```

### `DELETE /graph/projects/{project_id}` — удаление индекса графа

Удалить ИНДЕКС графа проекта (только sidecar-данные — никогда сущность проекта). Единственная операция, очищающая poisoned-набор (PG3). `agent` — обязательный query-параметр; тело запроса несёт необязательные `session` и `reason`.

**Ответ 200**

```json
{ "project": "vesma", "deleted_nodes": 2143, "status": "deleted" }
```

**Пример**

```bash
curl -s -X DELETE "http://127.0.0.1:8000/graph/projects/vesma?agent=operator" \
  -H "Content-Type: application/json" \
  -d '{"reason": "reindex from scratch"}'
```

---

## Watch-опрос (граф проектов, ADR-0032 §3.2)

Эти эндпоинты управляют внутрипроцессным watch-опросом: один кооперативный поток проверяет проиндексированные файлы проекта по mtime+size на адаптивном интервале и переиндексирует при фактических изменениях. Они зеркалируют MCP-инструменты `mnemos_watch_start`, `mnemos_watch_stop` и `mnemos_watch_status`.

> **Изменение поведения.** Прежняя форма наблюдателя директорий (`paths=` / `scan=` / `include_rules=`) была нереализованной заглушкой и удалена — эти поля больше не принимаются. `POST /watch/start` требует графовых флагов (включены по умолчанию с 2026-09-28) и существующего индекса; явный `code_graph.enabled: false` даёт `503`, незарегистрированный проект — `403`.

### `POST /watch/start` — регистрация watch-опроса графа проектов

**Тело запроса**

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `project_id` | string | **да** | — | Идентификатор зарегистрированного проекта. |
| `agent` | string | **да** | — | Идентичность вызывающего (PG7). |
| `session` | string | нет | — | Id сессии для аудита. |

**Ответ 200**

```json
{
  "status": "registered",
  "project": "vesma",
  "project_id": "vesma",
  "root": "/home/you/projects/vesma",
  "agent": "ci-runner",
  "session": null,
  "registered_at": "2026-09-28T12:00:00+00:00",
  "interval_sec": 5.0,
  "runs": 0,
  "reindexes": 0,
  "last_run_at": null,
  "last_result": null,
  "last_error": null
}
```

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/watch/start \
  -H "Content-Type: application/json" \
  -d '{"project_id": "vesma", "agent": "ci-runner"}'
```

### `POST /watch/stop` — остановить регистрации watch

Идемпотентно. Необязательный query-параметр `project_id` останавливает одну регистрацию; без него останавливаются ВСЕ.

**Ответ 200**

```json
{"status": "stopped", "stopped": 1}
```

**Пример**

```bash
curl -s -X POST "http://127.0.0.1:8000/watch/stop?project_id=vesma"
```

### `GET /watch/status` — статус watch-опроса

Активные регистрации и итог последнего опроса по каждому проекту.

**Ответ 200**

```json
{
  "running": true,
  "watch_enabled": true,
  "cap": 8,
  "registrations": [
    { "project": "vesma", "project_id": "vesma", "root": "/home/you/projects/vesma", "agent": "ci-runner", "session": null, "registered_at": "2026-09-28T12:00:00+00:00", "interval_sec": 5.0, "runs": 3, "reindexes": 1, "last_run_at": "2026-09-28T12:00:15+00:00", "last_result": "fresh", "last_error": null }
  ]
}
```

**Пример**

```bash
curl -s http://127.0.0.1:8000/watch/status
```

---

## Пайплайн знаний (M4)

### `POST /process` — запустить end-to-end пайплайн

Кластеризация → синтез → качественный шлюз → публикация. Тяжёлая операция; может занять секунды или минуты при большом бэклоге.

**Query-параметры**

| Имя | Тип | По умолчанию | Описание |
|-----|-----|-------------|---------- |
| `project` | string | — | Ограничить одним проектом. |
| `agent` | string | — | Ограничить одним агентом. |
| `limit` | int | `100` | Максимум raw-записей для обработки. Жёсткий cap `500`. |

**Ответ 200**

```json
{
  "status": "ok",
  "clustered": 8,
  "synthesized": 7,
  "published": 6,
  "quality_rejected": 1,
  "errors": 0
}
```

**Пример**

```bash
curl -s -X POST "http://127.0.0.1:8000/process?project=vesma&limit=200"
```

### `POST /synthesize` — синтезировать один кластер

**Query-параметры**

| Имя | Тип | Описание |
|-----|-----|---------- |
| `cluster_id` | string | Идентификатор кластера. |

**Ответ 200**

```json
{
  "status": "ok",
  "draft_id": "dr-...",
  "cluster_id": "cl-...",
  "source_coverage": 0.92,
  "model_used": "qwen2.5:3b"
}
```

**Ответ 404** — `{"detail": "Cluster <id> not found or empty"}`.

### `POST /publish/{memory_id}` — опубликовать обработанную запись

Переводит одну `processed`-запись в `published` и индексирует её в векторном хранилище.

**Параметры пути**

| Имя | Тип | Описание |
|-----|-----|---------- |
| `memory_id` | UUID | Идентификатор записи. |

**Ответ 200**

```json
{
  "status": "published",
  "memory_id": "550e8400-e29b-41d4-a716-446655440000",
  "vector_indexed": true
}
```

**Ответ 400** — `{"detail": "Publish failed for <id> (status=raw)"}` (публиковать можно только `processed`-записи).

---

## Dead-Letter Queue (M5)

DLQ хранит задачи, которые автоматизация не смогла завершить (таймаут LLM, временный сбой эмбеддера и т.д.). Три эндпоинта управляют очередью.

### `GET /dlq` — список записей DLQ

**Query-параметры**

| Имя | Тип | По умолчанию | Описание |
|-----|-----|-------------|---------- |
| `task_label` | string | — | Фильтр по метке задачи. |
| `ready_only` | bool | `false` | При true — только записи, у которых наступило время следующего retry. |
| `limit` | int | `50` | Максимум строк. Жёсткий cap `500`. |

**Ответ 200** — массив словарей строк DLQ (точная структура — в `SQLiteStore.dlq_list`).

### `POST /dlq/{dlq_id}/retry` — запланировать повтор

**Параметры пути**

| Имя | Тип |
|-----|-----|
| `dlq_id` | string |

**Ответ 200** — `{"status": "retry_scheduled", "entry": { ... }}`

### `DELETE /dlq/{dlq_id}` — удалить запись DLQ

**Ответ 200** — `{"status": "discarded", "dlq_id": "..."}`

**Ответ 404** — `{"detail": "DLQ entry <id> not found"}`.

---

## Контекстный фильтр (M10)

### `POST /filter/{memory_id}` — применить 5-этапный контекстный фильтр

Гейт на выдаче (финальное ревью M1): в контекст фильтруемы только `published`/
`processed` записи (`raw`/`processing`/`archived` → 422); опциональный `project`
вызывающего при несовпадении отказывает fail-closed (→ 403); возвращаемый
`clean_content` просканирован на секреты (редактированная копия + счётчики
`redactions`; refuse-режим → 403 без контента).

**Параметры пути**

| Имя | Тип | Описание |
|-----|-----|---------- |
| `memory_id` | UUID | Целевая запись. |

**Тело запроса** — см. `FilterRequest` в `src/vesmaro/models.py`

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `profile` | string | нет | авто | Одно из `log`, `terminal`, `code`, `docs`, `web`, `default`. |
| `budget` | int | нет | — | Бюджет токенов / символов. |
| `project` | string | нет | — | Слаг проекта вызывающего — запись обязана принадлежать ему (несовпадение → 403). Опустите для явной operator-семантики. |

**Ответ 200** — `FilterResult` (см. `manager.issue_context_filter`; добавляются `redactions` и, при ненулевом счётчике, `redacted_patterns`).

**Ответ 404** — `{"detail": "..."}` (запись не найдена). **422** — статусный гейт. **403** — несовпадение проекта или отказ выдачи.

---

## Трассировки (M6)

### `GET /traces` — список трассировок пайплайна

Слой трассировок — хук объяснимости. Каждый шаг пайплайна записывает строку трассировки.

**Query-параметры**

| Имя | Тип | По умолчанию | Описание |
|-----|-----|-------------|---------- |
| `task_label` | string | — | Фильтр по метке задачи (напр. `pipeline`, `synthesize`, `publish`). |
| `limit` | int | `50` | Максимум строк. Жёсткий cap `500`. |

**Ответ 200** — массив словарей строк трассировки.

---

## Загрузка path-scoped rules (M8)

### `POST /rules/ingest` — загрузить файлы `.instructions.md`

**Тело запроса** — см. `RuleIngestRequest`

| Поле | Тип | Обязательное | Описание |
|------|-----|--------------|---------- |
| `rules_dir` | string | **да** | Директория для рекурсивного сканирования. |
| `project` | string | нет | Slug проекта для тегирования. |
| `agent` | string | нет | Slug агента для тегирования. |
| `pattern` | string | нет | Glob (по умолчанию `*.instructions.md`). |

**Ответ 200**

```json
{
  "status": "ok",
  "processed": 7,
  "results": [
    {"file_path": ".github/instructions/communication-language.instructions.md", "memory_id": "..."}
  ]
}
```

**Пример**

```bash
curl -s -X POST http://127.0.0.1:8000/rules/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "rules_dir": "/home/you/vesma/.github/instructions",
    "project": "vesma",
    "agent": "tech-writer"
  }'
```

### `DELETE /rules/ingest` — удалить запись rule

**Тело запроса** — см. `RuleRemoveRequest`

| Поле | Тип | Обязательное | Описание |
|------|-----|--------------|---------- |
| `file_path` | string | **да** | Абсолютный или vault-относительный путь к rule. |

**Ответ 200** — `{"status": "removed", "removed": true, "memory_id": "..."}`

**Ответ 404** — `{"detail": "Rule for <path> not found"}`.

---

## A2A Sessions API (M16)

> Смонтировано под `/v1`. Контракт подробно описан в [a2a-sessions.md](../architecture/a2a-sessions.md); этот раздел — HTTP-поверхность.

Все пять эндпоинтов используют одну семантику ошибок:

- `422` — сбой валидации Pydantic.
- `404` — `SessionNotFoundError` или `TurnNotFoundError`.
- `500` — любой другой сбой на стороне сервера (см. логи).

### `POST /v1/sessions` — создать сессию

**Тело запроса** — `SessionCreate`

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `user_id` | string | нет | `""` | До 256 символов, пробельные символы обрезаются. |
| `metadata` | object | нет | `{}` | Произвольное хранилище ключей/значений. |
| `ttl_expires_at` | string (ISO 8601) | нет | — | Опциональная клиентская подсказка для TTL. Сервер может скорректировать. |

**Ответ 201** — `SessionRead`

```json
{
  "session_id": "conv-2026-06-15-a1b2c3d4",
  "user_id": "user-42",
  "created_at": "2026-06-15T10:42:00+00:00",
  "updated_at": "2026-06-15T10:42:00+00:00",
  "turns_count": 0,
  "metadata": {},
  "ttl_expires_at": null
}
```

Формат `session_id`: `conv-YYYY-MM-DD-<8 hex>` (дата UTC).

### `GET /v1/sessions/{session_id}` — получить сессию

**Ответ 200** — `SessionRead`.

**Ответ 404** — `{"detail": "..."}`

### `POST /v1/sessions/{session_id}/turns` — добавить ход

Идемпотентен по `message_id`: повторный POST с тем же `message_id` возвращает существующий ход (статус-код по-прежнему `201`) вместо дублирования.

**Тело запроса** — `TurnCreate`

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `role` | string | **да** | — | Одно из `user`, `agent`, `a2a_message`, `system`. |
| `content` | string | **да** | — | От 1 до 1 000 000 символов. |
| `from` | string | нет | — | Идентификатор отправителя. До 256 символов. |
| `to` | string | нет | — | Идентификатор получателя. |
| `message_id` | string | нет | — | Усечено до 256 символов. Ключ идемпотентности. |
| `outcome` | string | условное | авто | Одно из `delivered`, `rejected`, `budget-exhausted`, `loop-detected`. Обязательно при `role=a2a_message`; иначе отклоняется. |
| `tags` | string[] | нет | `[]` | Дедуплицируется, максимум 32 записи. |

**Ответ 201** — `TurnRead`

```json
{
  "turn_id": "tr-...",
  "session_id": "conv-2026-06-15-a1b2c3d4",
  "step_number": 1,
  "role": "user",
  "from": "user-42",
  "to": null,
  "summary": null,
  "key_decisions": [],
  "content": "Hello, Vesma.",
  "outcome": null,
  "tags": [],
  "context_pointer": "ctx-...",
  "message_id": "msg-001",
  "created_at": "2026-06-15T10:42:00+00:00"
}
```

### `GET /v1/sessions/{session_id}/turns/{turn_id}` — получить один ход

**Query-параметры**

| Имя | Тип | По умолчанию | Описание |
|-----|-----|-------------|---------- |
| `mode` | string | `summary` | `summary` — дешёвый путь (без `content`); `full` — полный raw-контент. |

**Ответ 200** — `TurnRead`.

### `POST /v1/sessions/{session_id}/turns/range` — диапазон ходов

**Тело запроса** — `TurnRangeRequest`

| Поле | Тип | Обязательное | По умолчанию | Описание |
|------|-----|--------------|-------------|---------- |
| `from_step` | int | **да** | — | Включительно нижняя граница. |
| `to_step` | int | **да** | — | Включительно верхняя граница. Должна быть ≥ `from_step`. |
| `mode` | string | нет | `summary` | `summary` или `full`. |

**Ответ 200** — `TurnRangeResponse`

```json
{
  "turns": [ { "...TurnRead..." } ],
  "total": 12,
  "mode": "summary"
}
```

Результат отсортирован по `step_number` возрастающе. `total` — количество фактически возвращённых ходов (не всей сессии).

---

## Схема Memory {#memory-schema}

Pydantic-модель `Memory` (определена в `src/vesmaro/models.py`) возвращается из `POST /memories`, `GET /memories/{id}` и `GET /memories`.

| Поле | Тип | Примечания |
|------|-----|----------- |
| `id` | UUID | Назначается сервером. |
| `content` | string | Очищенный / эффективный контент. |
| `raw_content` | string \| null | Исходный контент до фильтрации. Не включается без `include_raw=true`. |
| `title` | string \| null | Автогенерируется, если не указан. |
| `tags` | string[] | Должны удовлетворять контракту M2. |
| `source` | string | Одно из `manual`, `web`, `file`, `mcp`, `obsidian`, `cli`, `rule`, `synthesized`. |
| `source_url` | string \| null | URL происхождения. |
| `memory_type` | string | Одно из `note`, `fact`, `snippet`, `bookmark`, `conversation`, `session_context`. |
| `status` | string | Одно из `raw`, `processing`, `processed`, `published`, `archived`. |
| `project` | string | Денормализовано из тега `project:`. |
| `agent` | string | Денормализовано из тега `agent:`. |
| `category` | string \| null | Произвольное поле. |
| `quality_score` | float \| null | Устанавливается качественным шлюзом M4. |
| `confidence` | float \| null | Устанавливается качественным шлюзом M4. |
| `cluster_id` | string \| null | Указатель кластера M4. |
| `derived_from` | string[] | ID родительских записей (линия синтеза). |
| `file_path` | string \| null | Vault-относительный путь. |
| `metadata` | object | Произвольное поле. |
| `filter_profile` | string \| null | Применённый профиль фильтра M10. |
| `created_at` | ISO 8601 | UTC. |
| `updated_at` | ISO 8601 | UTC. |

---

## OpenAPI / Swagger

Полная машиночитаемая схема доступна на `/openapi.json` (3.1.0) и рендерится как UI на `/docs` (Swagger) и `/redoc` (ReDoc). Они генерируются FastAPI из декораторов маршрутов в `src/vesmaro/api/main.py` и `src/vesmaro/sessions/api.py`, поэтому схема никогда не расходится с работающим кодом.

Для генерации статического клиента — скачайте схему и выполните [`openapi-generator`](https://openapi-generator.tech/):

```bash
curl -s http://127.0.0.1:8000/openapi.json -o mnemos-openapi.json
npx @openapitools/openapi-generator-cli generate \
  -i mnemos-openapi.json -g typescript-fetch -o ./mnemos-client
```

---

## См. также

- [mcp-tools.md](mcp-tools.md) — те же возможности через MCP
- [cli-reference.md](cli-reference.md) — те же возможности через CLI
- [обзор архитектуры](../architecture/overview.md) — структура системы и модель данных
- [a2a-sessions.md](../architecture/a2a-sessions.md) — контракт A2A и обоснование дизайна
- [tag-contract.md](tag-contract.md) — схема M2, соблюдаемая `POST /memories`
- [security.md](../admin/security.md) — защита от SSRF, безопасность секретов, модель аутентификации, правила на границе запросов

---

_Последнее обновление: 2026-07-07_
