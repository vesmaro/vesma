<!-- mnemos-integration: v2.0.0 -->
# Метрики дашборда

**🌐 Language / Язык:** [English](../../en/user/metrics.md) · Русский

> Три HTTP-эндпоинта питают дашборд [`vesma-eyes`](https://github.com/vesmaro/vesma-eyes) и внешние стеки
> наблюдаемости: структурированный JSON-статус, временные ряды и
> Prometheus text exposition. Плюс расширенные фильтры `GET /memories`
> для списков.

---

## Обзор

Vesma предоставляет три метрических эндпоинта для фронтенда
`vesma-eyes` и внешних инструментов наблюдаемости (Grafana, Prometheus,
дашборды):

| Эндпоинт | Формат | Назначение |
|----------|--------|------------|
| `GET /api/v1/stats` | JSON | Структурированные данные дашборда (volume, filter, pipeline, search, vectors, sessions) |
| `GET /api/v1/stats/timeseries` | JSON | Временные данные для графиков (памяти по дням) |
| `GET /api/v1/metrics` | Prometheus text | Exposition-формат для скрейпинга Grafana / Prometheus |
| `GET /metrics` | JSON | Legacy-алиас — возвращает `stats()` JSON (обратная совместимость) |

Эндпоинт `GET /memories` также расширен фильтрами для списков дашборда
(status, project, agent, tags, since, until, limit, offset).

---

## `GET /api/v1/stats`

Возвращает структурированный JSON-объект, агрегирующий текущее состояние
хранилища памятей. Это основной эндпоинт для дашборда `vesma-eyes`.

**Ответ** (`200 OK`):

```json
{
  "version": "4.0.0",
  "timestamp": "2026-06-20T14:30:00+00:00",
  "volume": {
    "memories_total": 1248,
    "by_status": {"published": 980, "processed": 210, "raw": 58},
    "by_project": {"vesma": 540, "umbra": 708},
    "by_agent": {"tech-lead": 620, "code-reviewer": 628},
    "by_type": {"note": 900, "decision": 200, "trace": 148}
  },
  "filter": {
    "auto_filter": true,
    "filtered_total": 1100,
    "unfiltered_total": 148,
    "avg_reduction_pct": 42.5,
    "by_profile": {"log": 30, "code": 20, "docs": 15, "default": 35}
  },
  "pipeline": {
    "processed_total": 1190,
    "failed_total": 3,
    "dlq_depth": 3,
    "last_run": null
  },
  "search": {
    "requests_total": 87,
    "cross_project_requests_total": 3,
    "avg_latency_ms": 12.4,
    "avg_results": 5.2
  },
  "vectors": {
    "indexed_total": 980
  },
  "sessions": {
    "active": 4,
    "total": 12
  }
}
```

### Секции

| Секция | Поля | Источник |
|--------|------|----------|
| `volume` | `memories_total`, `by_status`, `by_project`, `by_agent`, `by_type` | SQLite-агрегаты |
| `filter` | `auto_filter`, `filtered_total`, `unfiltered_total`, `avg_reduction_pct`, `by_profile` | Статистика контекстного фильтра |
| `pipeline` | `processed_total`, `failed_total`, `dlq_depth`, `last_run` | Счётчики статусов + DLQ |
| `search` | `requests_total`, `cross_project_requests_total`, `avg_latency_ms`, `avg_results` | In-memory инструментирование |
| `vectors` | `indexed_total` | Счётчик векторного хранилища |
| `sessions` | `active` (обновлённые за 24ч), `total` | Таблица сессий |

> **Инструментирование поиска — in-memory.** Счётчики `requests_total`,
> `cross_project_requests_total`, `avg_latency_ms` и `avg_results`
> сбрасываются при каждом перезапуске сервера. Это принятый компромисс
> для дашборда — для постоянных метрик скрейпьте `GET /api/v1/metrics`
> через Prometheus.
>
> **`cross_project_requests_total`** (ArchCom 2026-08-27, A9): поиски,
> выполненные в явном глобальном режиме (`project` не передан / пустой) —
> по определению кросс-проектные. Каждый проектно-ограниченный поиск в
> этот счётчик не попадает.

---

## `GET /api/v1/stats/timeseries`

Возвращает временные данные для графиков дашборда. Сейчас поддерживается
`memories_added` (дневные счётчики из SQLite).

**Параметры запроса**:

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|--------------|----------|
| `metric` | string | `memories_added` | Метрика для графика (только `memories_added`) |
| `range` | string | `30d` | Диапазон, формат `<N>d` (напр. `7d`, `30d`, `90d`) |
| `granularity` | string | `day` | Размер бакета (только `day`; принят для прямой совместимости) |

**Пример**:

```http
GET /api/v1/stats/timeseries?metric=memories_added&range=30d&granularity=day
```

**Ответ** (`200 OK`):

```json
{
  "granularity": "day",
  "range": "30d",
  "series": [
    {
      "metric": "memories_added",
      "points": [
        {"timestamp": "2026-05-22", "value": 12},
        {"timestamp": "2026-05-23", "value": 8},
        {"timestamp": "2026-05-24", "value": 0}
      ]
    }
  ]
}
```

**Ошибки**:

| Статус | Причина |
|--------|---------|
| `422` | Неверный формат `range` (ожидается `<положительное-целое>d`) |

Неподдерживаемые значения `metric` возвращают пустой массив `points` с
примечанием — они не вызывают ошибку, поэтому дашборд может плавно
деградировать.

---

## `GET /api/v1/metrics`

Возвращает Prometheus text exposition для скрейпинга Grafana / Prometheus.
Content-Type: `text/plain; version=0.0.4; charset=utf-8`.

**Доступные метрики**:

| Метрика | Тип | Метки | Описание |
|---------|-----|-------|----------|
| `mnemos_memories_total` | gauge | — | Всего памятей в хранилище |
| `mnemos_memories_by_status` | gauge | `status` | Памяти по статусу |
| `mnemos_memories_by_project` | gauge | `project` | Памяти по проекту |
| `mnemos_memories_by_agent` | gauge | `agent` | Памяти по агенту |
| `mnemos_memories_by_type` | gauge | `type` | Памяти по memory_type |
| `mnemos_filter_avg_reduction_pct` | gauge | — | Средний процент сокращения фильтром |
| `mnemos_filter_filtered_total` | gauge | — | Памяти с заполненным `clean_content` |
| `mnemos_pipeline_processed_total` | counter | — | Всего обработанных памятей |
| `mnemos_pipeline_dlq_depth` | gauge | — | Текущая глубина DLQ |
| `mnemos_search_requests_total` | counter | — | Запросов поиска с перезапуска |
| `mnemos_search_avg_latency_ms` | gauge | — | Средняя задержка поиска в мс |
| `mnemos_vectors_indexed_total` | gauge | — | Индексированных векторов |
| `mnemos_sessions_active` | gauge | — | Активных сессий (обновлены за 24ч) |
| `mnemos_sessions_total` | gauge | — | Всего сессий |

**Пример вывода** (фрагмент):

```prometheus
# HELP mnemos_memories_total Total number of memories in storage
# TYPE mnemos_memories_total gauge
mnemos_memories_total 1248
# HELP mnemos_memories_by_status Memories by status
# TYPE mnemos_memories_by_status gauge
mnemos_memories_by_status{status="published"} 980
mnemos_memories_by_status{status="processed"} 210
```

### Конфиг скрейпинга Prometheus

```yaml
scrape_configs:
  - job_name: vesma
    static_configs:
      - targets: ["localhost:8787"]
    metrics_path: /api/v1/metrics
```

**Авторизация (#249):** эндпоинты метрик обходят авторизацию **только на
loopback-биндах** (локальному Prometheus-агенту, скрейпящему `localhost`,
учётные данные не нужны). На не-loopback биндах (`api.host` — не loopback
адрес) оба эндпоинта `/api/v1/metrics` и `/metrics` требуют
аутентифицированную сессию: экспозиция отдаёт гейджи
`mnemos_memories_by_project` / `mnemos_memories_by_agent`, поэтому
неаутентифицированный удалённый доступ — разведывательная поверхность
(CWE-200). Удалённый скрейпер аутентифицируется bearer-заголовком:

```yaml
scrape_configs:
  - job_name: vesma
    static_configs:
      - targets: ["mnemos.example.internal:8787"]
    metrics_path: /api/v1/metrics
    authorization:
      type: Bearer
      credentials: <mnk_-токен, созданный с --no-totp>
```

### Legacy `GET /metrics`

Старый эндпоинт `GET /metrics` возвращает `stats()` JSON (не Prometheus
text). Он сохранён для обратной совместимости. Для скрейпинга
Prometheus используйте `GET /api/v1/metrics`.

---

## `GET /memories` — расширенные фильтры

Эндпоинт списка памятей поддерживает фильтры для списков дашборда и
постраничного просмотра.

**Параметры запроса**:

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|--------------|----------|
| `status` | string | (все) | `raw`, `processing`, `processed`, `published`, `archived` |
| `project` | string | (все) | Slug проекта |
| `agent` | string | (все) | Slug агента |
| `tags` | string | (все) | Теги через запятую (логика И) |
| `since` | string | (все) | Нижняя граница ISO datetime |
| `until` | string | (все) | Верхняя граница ISO datetime |
| `limit` | int | `20` | Максимум результатов (≤ 500) |
| `offset` | int | `0` | Смещение пагинации (≥ 0) |

**Пример**:

```http
GET /memories?project=vesma&status=published&tags=mnemos:decision&limit=10&offset=20
```

**Ошибки**:

| Статус | Причина |
|--------|---------|
| `422` | Неверное значение `status` (допустимо: `raw`, `processing`, `processed`, `published`, `archived`) |

---

## Как vesma-eyes использует эти эндпоинты

Фронтенд `vesma-eyes` (L1 read-only viewer) использует метрические
эндпоинты следующим образом:

| Виджет дашборда | Эндпоинт | Секция / параметр |
|-----------------|----------|-------------------|
| Карточки объёма (всего, по статусу) | `GET /api/v1/stats` | `volume.memories_total`, `volume.by_status` |
| Разбивка по проекту / агенту | `GET /api/v1/stats` | `volume.by_project`, `volume.by_agent` |
| Эффективность фильтра | `GET /api/v1/stats` | `filter.avg_reduction_pct`, `filter.by_profile` |
| Здоровье пайплайна (DLQ) | `GET /api/v1/stats` | `pipeline.dlq_depth`, `pipeline.processed_total` |
| Производительность поиска | `GET /api/v1/stats` | `search.avg_latency_ms`, `search.requests_total` |
| График памятей по времени | `GET /api/v1/stats/timeseries` | `metric=memories_added&range=30d` |
| Список памятей с фильтрами | `GET /memories` | `?project=…&status=…&tags=…&limit=…&offset=…` |
| Внешний Grafana-дашборд | `GET /api/v1/metrics` | Скрейпинг Prometheus |

---

## См. также

- [Справочник HTTP API](http-api.md) — все эндпоинты.
- [Справочник CLI](cli-reference.md) — `vesma stats`, `vesma logs`.
- [Контекстный фильтр](context-filter.md) — секция `filter` в статистике.