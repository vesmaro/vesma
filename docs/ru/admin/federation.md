# Федерация — предпосылки Phase 1 (per-peer ACL + триггер-коды + журнал доступа)

**🌐 Language / Язык:** [English](../../en/admin/federation.md) · Русский

Эта страница описывает предпосылки федерации на стороне **vesma** —
per-peer ACL, enum триггер-кодов и журнал доступа федерации — и остаётся
справочником по набору полей `PeerConfig` и контракту триггер-кодов.
Сам запросный путь mediated pull уже живой: `handle_pull` в
`src/vesma/federation_server.py` обслуживает
`POST /api/v1/federation/pull`, а руководство по сквозной проверке —
[`federation-testing.md`](federation-testing.md). Изначально внедрение
было поэтапным (Phase 1: конфиг + enum'ы + журнал; Phase 2: сервер),
поэтому часть разделов ниже сохранена формулировка Phase 1/Phase 2.

- **Конфиг и контракт (эта страница):** конфигурация per-peer ACL,
  enum триггер-кодов, журнал доступа федерации.
- **Живой запросный путь:** `src/vesma/federation_server.py` (сторона B)
  и `src/vesma/api/federation.py` (route-адаптер). Внешний Go-бинарник
  пира живёт в отдельном репозитории, `vesma-mesh`.
- **Ссылки:** контракт ArchCom 2026-07-17
  (`.archcom/sessions/2026-07-17-federation-contract.md` §3.2, §6, §9,
  §10), ADR-0016 (`docs/project/adr/0016-federation-threat-model.md`).

## 1. Per-peer ACL — `federation.peers`

Phase 1 расширяет `FederationConfig` (`src/vesma/config.py`) картой
`peers: dict[str, PeerConfig]`. Ключ каждого peer'а — его A2A id
(например, `vesma-A`), а значение описывает, что этому peer'у разрешено
вытягивать. Глобальный whitelist `federation.shared_projects` остаётся
фильтром верхнего уровня; per-peer `allowed_projects` — фильтр-подмножество
поверх него.

### Fail-closed-значения по умолчанию

| Поле | По умолчанию | Что означает |
|---|---|---|
| `peers` | `{}` | Peer'ы не сконфигурированы — сервер Phase 2 отклонит все pull-запросы. |
| `allowed_projects` | `[]` | Peer не может вытянуть **ни один** проект. |
| `allowed_types` | `[]` | Peer не может вытянуть **ни один** тип записей. |
| `["*"]` (любое из полей) | — | Явный wildcard — все проекты из `shared_projects` / все типы записей. Никогда не подразумевается неявно. |
| `mtls_cert_fingerprint` | `None` | mTLS-пиннинг для этого peer'а не применяется (оператор включает явно). |

Оператор, желающий открыть доступ peer'у, должен сказать об этом явно.
Нигде в цепочке нет неявного «allow all».

### Поля `PeerConfig`

| Поле | Тип | Примечания |
|---|---|---|
| `bearer_token_env` | `str` (обязательное) | ИМЯ переменной окружения, хранящей per-peer bearer-токен (`mnk_fed_<peer_id>_<random>` по ADR-0016). Никогда само значение — сервер читает токен из этой переменной в момент запроса. |
| `allowed_projects` | `list[str]` | Подмножество `shared_projects`. Пусто = ничего. `["*"]` = всё из `shared_projects`. |
| `allowed_types` | `list[str]` | Один из `decision` / `learning` / `bug-pattern` / `rule` / `open-question` / `checkpoint` / `session`. Пусто = ничего. `["*"]` = все. |
| `rate_limit_per_minute` | `int` | Per-peer rate limit на pull (контракт §8, защита от DDoS). По умолчанию 30, диапазон 1–600. |
| `mtls_cert_fingerprint` | `str \| None` | Опциональный SHA-256 mTLS-сертификата клиента peer'а. Если задан, сервер отклоняет несовпадающие клиентские сертификаты. |

### Пример (`config.yaml`)

Все значения ниже — RFC-зарезервированные заглушки, никогда реальные токены.

```yaml
federation:
  shared_projects:
    - vesma
    - project-umbra
  peers:
    vesma-A:
      bearer_token_env: VESMA_FED_PEER_A_TOKEN
      allowed_projects:
        - vesma
      allowed_types:
        - decision
        - learning
        - bug-pattern
      rate_limit_per_minute: 30
      # Optional — pin the peer's mTLS client cert:
      mtls_cert_fingerprint: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

Значение токена живёт в именованной переменной окружения (здесь
`VESMA_FED_PEER_A_TOKEN`), заданной в окружении оператора или в
секрет-менеджере — в конфиг-файл оно никогда не коммитится.

## 2. Триггер-коды — `src/vesma/trigger_codes.py`

Контракт §9 заменяет per-session бюджет запросов на **исчерпывающий
ответ** плюс триггер-код. Сторона B (сервер федерации Phase 2) возвращает
один из пяти кодов в A2A-payload `share-finding`; сторона A (клиент
федерации Phase 2) диспетчеризует по коду.

| Код | Когда B его возвращает | Что делает A |
|---|---|---|
| `EXHAUSTIVE` | B дал полный sanitized-ответ | Использовать; не повторять запрос по той же теме. |
| `ALREADY_EXHAUSTED` | B уже отвечал `EXHAUSTIVE` по этой теме (проверяется по журналу доступа) | Переиспользовать прежний ответ; не перезапрашивать. |
| `PARTIAL` | Ответ частичный (записи отсутствуют или moderation отредактировал часть) | Уточнить запрос (другая тема/ракурс); не повторять дословно. |
| `REFUSED` | B отказал — контент нельзя расшарить даже после редекции | Не повторять; уйти в локальный `vesma_search` (КП-2). |
| `OFFLINE_LITE` | B в сети в урезанном режиме (например, moderation частично офлайн) | Использовать частичный результат; дополнить локальным `vesma_search`. |

Два хелпера:

- `is_terminal(code)` — возвращает `True` для `EXHAUSTIVE`,
  `ALREADY_EXHAUSTED`, `REFUSED` (A не должен перезапрашивать ту же тему).
- `should_fallback_to_local(code)` — возвращает `True` для `REFUSED`,
  `OFFLINE_LITE` (A уходит в локальный `vesma_search`).

Phase 1 определяет enum и оба хелпера. Phase 2 подключает коды к серверу
(возврат в payload) и клиенту (диспетчеризация при получении).

## 3. Журнал доступа федерации — `src/vesma/federation_access_log.py`

Контракт §10. B-side append-only JSONL audit-лог в
`~/.mnemos/logs/federation-access.jsonl`, фиксирующий, кто, когда и с
каким триггер-кодом что запрашивал и какие записи были возвращены.
Журнал обеспечивает **anti-correlation tracking**: B видит, что A уже
получал `EXHAUSTIVE` по теме X → следующий запрос по той же теме
возвращает `ALREADY_EXHAUSTED` (короткий код вместо повторной отгрузки
sanitized-контента).

### Приватность — без открытого текста запроса (КП-5)

В журнале хранится только **SHA-256-хэш** темы запроса, никогда открытый
текст. Одна и та же тема даёт один и тот же дайджест, поэтому B может
сопоставить повторный запрос с прежним ответом `EXHAUSTIVE`, так и не
узнав намерения запроса. Если файл журнала утекает, намерение запроса — нет.

### Поля `AccessLogEntry`

| Поле | Тип | Примечания |
|---|---|---|
| `peer_id` | `str` | A2A id запрашивающего агента (кто). |
| `topic_hash` | `str` (64 hex-символа) | `SHA-256(query_topic)` — никогда открытый текст. |
| `timestamp` | `datetime` (UTC ISO-8601) | Когда запрос был обслужен. |
| `project_scope` | `str` | Slug проекта, который запрашивали. |
| `trigger_code` | `TriggerCode` | Код, возвращённый peer'у (§9). |
| `record_ids_accessed` | `list[str]` | Id возвращённых записей (форензический аудит). |

### API `FederationAccessLog`

| Метод | Назначение |
|---|---|
| `append(entry)` | Дописывает одну JSON-строку, `flush` + `os.fsync` (целостность аудита), process-local лок для потокобезопасности. |
| `query(peer_id, topic_hash)` | Самая свежая запись для пары (peer, topic) — сервер использует её, чтобы решить `ALREADY_EXHAUSTED`. |
| `query_recent(peer_id, since=...)` | Все записи peer'а начиная с UTC-метки — отчёты аудита. |
| `count_by_trigger_code(peer_id, since=...)` | Счётчики по каждому триггер-коду с нулевым заполнением — метрики/аудит. |

Хелпер модуля: `hash_topic(topic: str) -> str` — `SHA-256(topic)` в hex.

### Не реплицируется — только сторона B

Журнал доступа живёт **только на B**. Он никогда не экспортируется, не
синхронизируется на peer'ов и не включается в `vesma export`. Как и
moderation mapping-таблица, это поверхность утечки — репликация позволила
бы peer'у восстановить историю запросов другого peer'а.

## 4. Статус — Phase 2 в продакшене

Сервер федерации (сторона B) и клиент (сторона A) живут в кодовой базе:

1. **Сервер** — `handle_pull` в `src/vesma/federation_server.py` читает
   `federation.peers`, валидирует per-peer bearer-токен из именованной
   переменной окружения, опционально пиннит mTLS-сертификат клиента,
   применяет per-peer ACL поверх `shared_projects`, прогоняет moderation
   pipeline, проверяет журнал доступа на `ALREADY_EXHAUSTED` и возвращает
   sanitized-ответ с `TriggerCode`. HTTP-маршрут:
   `POST /api/v1/federation/pull` (адаптер `src/vesma/api/federation.py`).
2. **Клиент** — `src/vesma/federation_client.py` отправляет pull-запрос,
   получает `TriggerCode` и диспетчеризует — `is_terminal` /
   `should_fallback_to_local` решают, использовать ответ, уточнить его
   или уйти в локальный поиск. Сквозная проверка —
   [`federation-testing.md`](federation-testing.md).

Go-бинарник, несущий gRPC-транспорт, живёт в отдельном репозитории
(`vesma-mesh`) и вне области этой страницы.

## 5. См. также

- [ADR-0016 — модель угроз федерации](../../project/adr/0016-federation-threat-model.md)
- [Security — Federation defence-in-depth](security.md#11-federation-defence-in-depth)
- [Федерация — пакетная синхронизация (Phase 0)](../user/sync.md)
- Контракт ArchCom 2026-07-17 (`.archcom/sessions/2026-07-17-federation-contract.md`)

---

_Последнее обновление: 2026-09-05_
