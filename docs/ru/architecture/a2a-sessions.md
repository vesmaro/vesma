# A2A Sessions API (M16)

**🌐 Language / Язык:** [English](../../en/architecture/a2a-sessions.md) · Русский

> **Статус**: Реализовано в Vesma M16
> **Аудитория**: AI-агенты / MCP orchestrator
> **Base URL**: `http://localhost:8787/v1/` (loopback по умолчанию)
> **Исходная спецификация**: [ADR-0007 — A2A Sessions API v1](../../project/adr/0007-a2a-sessions-api-v1.md)

Vesma предоставляет 5 HTTP-эндпоинтов для уровня A2A-маршрутизации. Они
дают агентам персистентный backend для сессий переписки и истории отдельных шагов,
так что многошаговые цепочки агентов выживают после перезапуска, а
кросс-сессионный контекст доступен для поиска.

Если Vesma недоступен, MCP-слой откатывается на файловый лог
(`~/.gcw/a2a-messages.jsonl`) — Vesma **не является** единственной точкой
отказа для агентов.

---

## Эндпоинты — обзор

| # | Метод | Путь | Назначение |
|---|-------|------|-----------|
| 1 | POST  | `/v1/sessions` | Создать новую сессию |
| 2 | GET   | `/v1/sessions/{session_id}` | Получить метаданные сессии + количество шагов |
| 3 | POST  | `/v1/sessions/{session_id}/turns` | Добавить шаг (идемпотентно по `message_id`) |
| 4 | GET   | `/v1/sessions/{session_id}/turns/{turn_id}` | Ленивая загрузка одного шага (summary или full) |
| 5 | POST  | `/v1/sessions/{session_id}/turns/range` | Массовая загрузка непрерывного диапазона шагов |

OpenAPI-схема генерируется автоматически и доступна по адресам:

- `GET /openapi.json` — машиночитаемая спецификация
- `GET /docs`         — Swagger UI
- `GET /redoc`        — ReDoc UI

---

## 1. `POST /v1/sessions`

Создать новую сессию переписки. Сервер генерирует `session_id` в формате
`conv-YYYY-MM-DD-<short-uuid>`.

### Запрос

```http
POST /v1/sessions
Content-Type: application/json
```

```json
{
  "user_id": "abyss",
  "metadata": {
    "started_by": "vscode",
    "workspace": "/var/home/abyss/LABs/Projects/Reserching/GithubCopilotWorkflow"
  },
  "ttl_expires_at": null
}
```

| Поле | Тип | Обязательное | Примечание |
|------|-----|--------------|------------|
| `user_id` | string | нет | Пустая строка допустима. Обрезается пробел. |
| `metadata` | object | нет | Свободный JSON. |
| `ttl_expires_at` | string | нет | ISO-8601. Опционально. |

### Ответ — 201

```json
{
  "session_id": "conv-2026-06-15-eca03ad4",
  "user_id": "abyss",
  "created_at": "2026-06-15T20:28:33.279446Z",
  "updated_at": "2026-06-15T20:28:33.279446Z",
  "turns_count": 0,
  "metadata": { "started_by": "vscode" },
  "ttl_expires_at": null
}
```

---

## 2. `GET /v1/sessions/{session_id}`

Получить метаданные сессии и текущее значение `turns_count`.

### Запрос

```http
GET /v1/sessions/conv-2026-06-15-eca03ad4
```

### Ответ — 200

```json
{
  "session_id": "conv-2026-06-15-eca03ad4",
  "user_id": "abyss",
  "created_at": "2026-06-15T20:28:33.279446Z",
  "updated_at": "2026-06-15T20:30:11.123Z",
  "turns_count": 7,
  "metadata": { "started_by": "vscode" },
  "ttl_expires_at": null
}
```

### Ошибки

- `404` — сессия не найдена.

---

## 3. `POST /v1/sessions/{session_id}/turns`

Добавить шаг к сессии. **Идемпотентно** по `message_id`: повторный POST с
тем же id возвращает существующий шаг вместо создания дубля.

### Запрос

```http
POST /v1/sessions/conv-2026-06-15-eca03ad4/turns
Content-Type: application/json
```

```json
{
  "role": "a2a_message",
  "from": "gcw-senior-system-engineer",
  "to":   "gcw-senior-dba",
  "message_id": "msg-x7y8z9",
  "content": "Need migration for orders.archived_at column. DECISION: add column, not table. - [x] write migration plan",
  "outcome": "delivered",
  "tags": ["migration", "schema", "orders"]
}
```

| Поле | Тип | Обязательное | Примечание |
|------|-----|--------------|------------|
| `role` | enum | да | Одно из `user`, `agent`, `a2a_message`, `system` |
| `content` | string | да | Максимум 1 000 000 символов |
| `from` | string | нет | Агент / пользователь, создавший шаг |
| `to` | string | нет | Агент-получатель |
| `message_id` | string | нет (рекомендуется) | Ключ идемпотентности. Усекается до 256 символов. |
| `outcome` | enum | да (для `a2a_message`) | Одно из `delivered`, `rejected`, `budget-exhausted`, `loop-detected`. По умолчанию `delivered` для роли `a2a_message`. |
| `tags` | string[] | нет | Свободный формат. Максимум 32 уникальных элемента. |

### Ответ — 201

```json
{
  "turn_id": "turn-2",
  "session_id": "conv-2026-06-15-eca03ad4",
  "step_number": 2,
  "role": "a2a_message",
  "from": "gcw-senior-system-engineer",
  "to": "gcw-senior-dba",
  "summary": "Need migration for orders.archived_at column. DECISION: add column, not table. - [x] write migration plan",
  "key_decisions": [
    "DECISION: add column, not table",
    "- [x] write migration plan"
  ],
  "content": "Need migration for orders.archived_at column. ...",
  "outcome": "delivered",
  "tags": ["migration", "schema", "orders"],
  "context_pointer": "memory://conv-2026-06-15-eca03ad4#step-2",
  "message_id": "msg-x7y8z9",
  "created_at": "2026-06-15T20:30:11.123Z"
}
```

### Контракт идемпотентности

- Одинаковый `message_id` → возвращает **существующий** шаг (тот же `turn_id`,
  `step_number`, `context_pointer`).
- Разный `message_id` → создаёт новый шаг, увеличивает `step_number`.
- Без `message_id` → не идемпотентно; каждый вызов добавляет новый шаг.

### Ошибки

- `404` — сессия не найдена.
- `422` — неверный `role`, отсутствует `content`, outcome для не-A2A роли и т.д.

---

## 4. `GET /v1/sessions/{session_id}/turns/{turn_id}`

Ленивая загрузка одного шага в режиме `summary` (по умолчанию) или `full`.

### Запрос

```http
GET /v1/sessions/conv-2026-06-15-eca03ad4/turns/turn-2?mode=summary
```

| Параметр | По умолчанию | Примечание |
|---------|-------------|------------|
| `mode` | `summary` | `summary` возвращает до 200 символов + key_decisions, **без** `content`. `full` возвращает всё, включая `content`. |

### Ответ — 200 (`mode=summary`)

```json
{
  "turn_id": "turn-2",
  "session_id": "conv-2026-06-15-eca03ad4",
  "step_number": 2,
  "role": "a2a_message",
  "from": "gcw-senior-system-engineer",
  "to": "gcw-senior-dba",
  "summary": "Need migration for orders.archived_at column. DECISION: add column, not table. - [x] write migration plan",
  "key_decisions": [
    "DECISION: add column, not table",
    "- [x] write migration plan"
  ],
  "outcome": "delivered",
  "tags": ["migration", "schema", "orders"],
  "context_pointer": "memory://conv-2026-06-15-eca03ad4#step-2",
  "message_id": "msg-x7y8z9",
  "created_at": "2026-06-15T20:30:11.123Z"
}
```

`mode=full` добавляет поле `content` с полным оригинальным сообщением.

### Ошибки

- `404` — сессия или шаг не найдены.
- `422` — неверное значение `mode`.

---

## 5. `POST /v1/sessions/{session_id}/turns/range`

Массовая загрузка непрерывного диапазона шагов, упорядоченных по `step_number`
по возрастанию.

### Запрос

```http
POST /v1/sessions/conv-2026-06-15-eca03ad4/turns/range
Content-Type: application/json
```

```json
{
  "from_step": 1,
  "to_step":   5,
  "mode":      "summary"
}
```

| Поле | Тип | Диапазон | Примечание |
|------|-----|---------|------------|
| `from_step` | int | 1..10 000 000 | Нижняя граница включительно. |
| `to_step` | int | 1..10 000 000 | Верхняя граница включительно. Должна быть ≥ `from_step`. |
| `mode` | enum | `summary` / `full` | Та же семантика, что у GET одного шага. |

Окно ограничено 1000 шагами на вызов (защита от больших ответов).

### Ответ — 200

```json
{
  "turns": [
    { "turn_id": "turn-1", "step_number": 1, "summary": "..." },
    { "turn_id": "turn-2", "step_number": 2, "summary": "..." },
    { "turn_id": "turn-3", "step_number": 3, "summary": "..." }
  ],
  "total": 3,
  "mode": "summary"
}
```

`total` — количество шагов, фактически возвращённых в диапазоне (не размер
всей сессии). Запрос за пределами диапазона возвращает
`{"turns": [], "total": 0, "mode": "summary"}` со статусом 200 — это не ошибка.

### Ошибки

- `404` — сессия не найдена.
- `422` — `to_step < from_step` или диапазон > 1000 шагов.

---

## Модель хранения

Vesma использует тот же файл SQLite, что и остальная часть проекта. Включён
WAL mode, поэтому хранилище A2A и основное хранилище памяти могут читать
одновременно, пока одно из них пишет. Две новые таблицы существуют рядом с
существующими:

```sql
CREATE TABLE sessions ( ... );     -- одна строка на сессию
CREATE TABLE turns    ( ... );     -- одна строка на шаг; UNIQUE(message_id)
```

(FTS5-тень `turns_fts` и её триггеры `turns_ai`/`turns_ad`/`turns_au`
УДАЛЕНЫ — C8, Архкомитет 2026-08-27: мёртвый индекс без читателей, вторая
plaintext-копия каждого шага на диске и write-амплификация на горячем пути.
Поиск по шагам — не фича; DDL восстановим из истории VCS по спросу.)

Таблицы A2A входят в ту же WAL-группу, что и таблицы memories, поэтому одна
прагма `journal_mode=WAL` покрывает обе.

### Гарантии атомарности

- `POST /turns` выполняется в одной транзакции: SELECT для идемпотентности,
  INSERT, UPDATE `sessions.updated_at`, COMMIT.
- При любой `sqlite3.Error` транзакция откатывается — сбой в середине записи
  не может оставить частично вставленный шаг.
- Дефолтная изоляция SQLite для write-транзакций — SERIALIZABLE, поэтому
  паттерн «проверить перед вставкой» безопасен при конкурентных писателях.

### Извлечение summary (без LLM в v1)

Поле `summary` вычисляется при записи из содержимого `content` шага:

1. Берётся первый абзац (текст до первой пустой строки).
2. Если он превышает 200 символов — усекается по ближайшей границе слова
   с добавлением `...`.

Поле `key_decisions` извлекается регулярным выражением, распознающим два
паттерна:

- `DECISION: <text>` (регистронезависимо).
- `- [x] <text>` (выполненная задача в формате GitHub-flavoured markdown).

Распознаются как формы с начала строки, так и сентенциально-граничные
(например, `… statement. DECISION: x`), что позволяет работать как с
однострочными, так и с многострочными A2A payload'ами. Возвращается до
5 решений в порядке их появления.

---

## Режимы отказа (с точки зрения агента)

| Поведение Vesma | Что должен делать агент |
|------------------|-----------------------|
| Ответ `5xx` | Повторить до 3 раз с экспоненциальным backoff, затем откатиться на файловый лог. |
| Отказ в соединении / timeout > 2 с | Пропустить персистентность — продолжить обработку без `context_pointer`. |
| `4xx` (валидация) | НЕ повторять. Залогировать ошибку и продолжить. |
| Успешный `2xx` | Использовать возвращённый `context_pointer` для адресации шага в последующих шагах. |

Vesma сам не реализует эти повторы — это задача MCP-слоя. Полный контракт
см. в [ADR-0007 — A2A Sessions API v1](../../project/adr/0007-a2a-sessions-api-v1.md).

---

## Примеры

### Создание сессии и запись одного шага (curl)

```bash
# 1. Создать
SESSION=$(curl -s -X POST http://localhost:8787/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"abyss","metadata":{"started_by":"vscode"}}' \
  | jq -r '.session_id')

# 2. Записать шаг
curl -s -X POST "http://localhost:8787/v1/sessions/$SESSION/turns" \
  -H 'Content-Type: application/json' \
  -d '{
    "role": "a2a_message",
    "from": "gcw-senior-system-engineer",
    "to":   "gcw-senior-dba",
    "message_id": "msg-001",
    "content": "Need migration for orders.archived_at column. DECISION: add column, not table.",
    "outcome": "delivered",
    "tags": ["migration", "orders"]
  }'

# 3. Прочитать шаг (summary)
curl -s "http://localhost:8787/v1/sessions/$SESSION/turns/turn-1" | jq

# 4. Массовая загрузка первых 5 шагов
curl -s -X POST "http://localhost:8787/v1/sessions/$SESSION/turns/range" \
  -H 'Content-Type: application/json' \
  -d '{"from_step": 1, "to_step": 5, "mode": "summary"}' | jq
```

### Идемпотентность (Python)

```python
payload = {
    "role": "a2a_message",
    "content": "hello",
    "message_id": "msg-stable-1",
}
r1 = client.post(f"/v1/sessions/{sid}/turns", json=payload)
r2 = client.post(f"/v1/sessions/{sid}/turns", json=payload)
assert r1.json()["turn_id"] == r2.json()["turn_id"]   # возвращается тот же шаг
```

---

## Примечания по миграции

При обновлении с версии Vesma без таблиц A2A ручных шагов миграции не
требуется: схема использует `CREATE TABLE IF NOT EXISTS …` и применяется
автоматически при первом открытии основного `SQLiteStore` (при запуске FastAPI).

Виртуальная таблица `turns_fts` раньше создавалась заранее, хотя ни один
эндпоинт её не запрашивал; она удалена (C8, Архкомитет 2026-08-27 — мёртвый
индекс, вторая plaintext-копия на диске, write-амплификация). У legacy-баз
таблица и триггеры удаляются идемпотентно при первом открытии `SQLiteStore`
после обновления.
