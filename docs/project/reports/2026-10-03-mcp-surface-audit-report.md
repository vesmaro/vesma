# Аудит MCP-поверхности: usage-рейтинг, reindex-parity, вес схем, 6.0-пакет — 2026-10-03

> **Карточка `vesma-mcp-audit` W-D.** Аудит поверхности 39 MCP-тулов БЕЗ её
> изменения: телеметрический рейтинг использования, решение по паритету
> reindex, замер per-session токен-веса манифеста и документация 6.0-пакета
> регруппировки. База аудита: `9cd961d` (5.4.0), `src/vesmaro/mcp_server.py`.
> Изменения этого слайса — только новые `.md`; поверхность, докстринги и код
> не тронуты.

## 1. USAGE-RANKING (телеметрия `awareness_events`)

**Метод.** Стор `~/.mnemos/data/metrics.sqlite` (vitals-sidecar, ADR-0035
wave-0 shadow — телеметрия `tool_call` пишется на каждый dispatch, retention
90 дней; в прод-стор — только read-only подключения `mode=ro`, ни одной
записи). Запрос:

```sql
SELECT json_extract(meta_json,'$.tool') AS tool, COUNT(*) AS n
FROM awareness_events WHERE kind='tool_call'
GROUP BY 1 ORDER BY n DESC;
```

**Окно данных: 2026-10-02 20:33 → 2026-10-03 23:40 (≈ 27.1 ч).** Всего
322 события: `tool_call` 143, `peer_write` 77, `heartbeat_delivery` 62,
`delta_available` 40. Тулов вызвано: **16 из 39**.

### 1.1 Рейтинг вызванных (16)

| # | Тул | Вызовов | | # | Тул | Вызовов |
|---|-----|--------:|---|---|-----|--------:|
| 1 | `mnemos_save_context` | 52 | | 9 | `mnemos_project_graph_status` | 4 |
| 2 | `mnemos_add` | 25 | | 10 | `mnemos_list_graph_projects` | 3 |
| 3 | `mnemos_search` | 14 | | 11 | `mnemos_delete_graph_project` | 2 |
| 4 | `mnemos_search_graph` | 14 | | 12 | `mnemos_index_project` | 2 |
| 5 | `mnemos_recall_context` | 7 | | 13 | `mnemos_list_recent` | 2 |
| 6 | `mnemos_workflow` | 6 | | 14 | `mnemos_auto_collect_status` | 1 |
| 7 | `mnemos_trace_path` | 5 | | 15 | `mnemos_stats` | 1 |
| 8 | `mnemos_awareness` | 4 | | 16 | `mnemos_watch_status` | 1 |

### 1.2 Dead-кандидаты (23 тула, 0 вызовов — доказательство: GROUP BY выше
вернул ровно 16 имён из 39; строк по каждому из перечисленных — 0)

`mnemos_agent_recall`, `mnemos_align_prefix`, `mnemos_assemble_context`,
`mnemos_check_graph_coverage`, `mnemos_compress`, `mnemos_context_rewrite`,
`mnemos_export`, `mnemos_filter`, `mnemos_get_code_snippet`,
`mnemos_get_file_outline`, `mnemos_get_graph_schema`, `mnemos_hooks`,
`mnemos_import`, `mnemos_ingest_document`, `mnemos_ingest_url`,
`mnemos_list_tags`, `mnemos_register_project`, `mnemos_reprocess`,
`mnemos_retrieve`, `mnemos_tags`, `mnemos_tags_rename`,
`mnemos_watch_start`, `mnemos_watch_stop`.

### 1.3 Интерпретация (ограничение данных — обязательно)

Окно ≈ 27 ч shadow-телеметрии одной инсталляции — **недостаточно для вывода
«тул мёртв»**. Классы ложной смерти:

- **compatibility-алиасы**: `mnemos_tags_rename` — уже non-breaking алиас
  группового `mnemos_tags action="rename"` (`mcp_server.py:2713-2725`); его
  «смертность» — аргумент ЗА дроп из манифеста в 6.0, не за удаление кода.
- **integration-driven**: `mnemos_align_prefix`, `mnemos_assemble_context`,
  `mnemos_context_rewrite`, `mnemos_hooks` зовутся harness-слоем по
  контракту кэш-дисциплины, а не моделью по собственному решению — частота
  отражает сценарий, а не полезность.
- **ops/редкие lifecycle**: `export`/`import`/`register_project`/
  `ingest_*`/`compress`/`reprocess` — событийные, часы окна их не ловят.

Реальный сигнал рейтинга: **рабочий корень** —
`save_context`/`add`/`search`/`recall_context` (85 % вызовов) плюс заметный
граф-контур (search_graph/trace_path/index_project). Это согласуется с
G1–G4-каноном: пишущие и читающие контуры живут, сервисные — спят.

## 2. REINDEX-PARITY: решение

**Факты (сверены по коду):**

- **MCP**: тула reindex среди 39 нет (манифест `_canonical_tools()`,
  `mcp_server.py:469-2126`; список имён сверен grep-харвестом
  `name="mnemos_…"` — 39 уникальных).
- **REST**: `POST /reindex` есть — `api/main.py:733-741`,
  `mgr.rebuild_vector_index(batch_size)`: «Re-embeds every published memory
  and upserts into the vector store. Use after enabling embeddings or
  switching embedding models».
- **CLI**: `vesma reindex` есть — `cli/main.py:911-927`, тот же manager-вызов.
- Ближайшие MCP-аналоги: `mnemos_index_project` (граф-индексация проекта),
  `mnemos_watch_start` (adaptive-полл, «reindexes on actual changes»,
  `mcp_server.py:1036-1064`), PG-0.5 autoindex (`codegraph/autoindex.py`).

**Решение (DR-1): reindex остаётся ops-only — в MCP НЕ добавляется,
фиксируется как задокументированное исключение MCP-поверхности.**

**Rationale.** Полная пере-эмбедировка всех published-памятей —
операторский maintenance-шаг жизненного цикла модели эмбеддингов (дорого,
долго, не имеет агентского сценария: агенту нужна свежесть контекста, а её
закрывают `mnemos_index_project` + watch-полл + autoindex). Добавление в
манифест стоило бы ~400–500 токенов веса (п. 3) при нулевом
телеметрическом спросе. REST+CLI уже дают автоматизации и оператору полный
доступ. Пересмотр — только если появится реальный agent-facing сценарий
«пересобери индекс» (отдельная карточка).

## 3. ВЕС СХЕМ (per-session token-вес манифеста)

**Метод.** Живой манифест `_canonical_tools()` импортирован из репо
(`.venv`, без запуска сервера), каждый тул сериализован как
`{"name","description","inputSchema"}` (json.dumps, как его видит клиент в
`tools/list`); оценка токенов — консервативно chars/4. Замер в обоих режимах
auto-collect.

| Режим | Тулов | Символов | Оценка токенов (chars/4) |
|-------|------:|---------:|-------------------------:|
| auto_collect=off | 39 | 43 726 | **≈ 10 932** |
| auto_collect=on | 39 | 43 594 | ≈ 10 898 |

Бренд-режим вес НЕ удваивает: при `VESMA_MCP_BRAND` манифест отдаёт только
`<brand>_*`-имена (`mcp_server.py:424-441`), схема одна на тул.

### 3.1 Топ-12 по весу (auto_collect=off)

| Тул | Токены | Симв. | из них desc / schema |
|-----|-------:|------:|----------------------|
| `mnemos_hooks` | 847 | 3 389 | 1 090 / 2 239 |
| `mnemos_assemble_context` | 625 | 2 500 | 919 / 1 510 |
| `mnemos_awareness` | 612 | 2 449 | 1 605 / 780 |
| `mnemos_save_context` | 524 | 2 096 | 462 / 1 567 |
| `mnemos_export` | 479 | 1 917 | 545 / 1 311 |
| `mnemos_context_rewrite` | 478 | 1 911 | 909 / 932 |
| `mnemos_retrieve` | 466 | 1 865 | 699 / 1 103 |
| `mnemos_search` | 460 | 1 841 | 186 / 1 594 |
| `mnemos_workflow` | 440 | 1 762 | 655 / 1 044 |
| `mnemos_tags` | 435 | 1 740 | 322 / 1 359 |
| `mnemos_search_graph` | 413 | 1 652 | 591 / 994 |
| `mnemos_import` | 356 | 1 423 | 509 / 853 |

### 3.2 Групповая разбивка

| Группа | Тулов | Симв. | ~Токены | Доля |
|--------|------:|------:|--------:|-----:|
| context/gates (save/recall/agent_recall/list_recent/align/assemble/rewrite/hooks/awareness) | 9 | 15 713 | ~3 928 | 36 % |
| graph (10 тулов ADR-0032) | 10 | 8 832 | ~2 208 | 20 % |
| ingest/export (ingest×2, export, import, workflow, register) | 6 | 7 594 | ~1 898 | 17 % |
| core-memory (add/search/filter/retrieve/compress/reprocess/stats) | 7 | 7 210 | ~1 802 | 17 % |
| tags (tags, tags_rename, list_tags) | 3 | 2 947 | ~736 | 7 % |
| watch (start/stop/status) | 3 | 1 211 | ~302 | 3 % |
| прочее (auto_collect_status) | 1 | 219 | ~54 | <1 % |

**Главный источник веса — распределённый, не одна туша:** (а) длинные
описания-инструкции в докстринг-стиле (`mnemos_awareness` — 1 605 симв.
описания; gates-канон и так живёт в instruction-файлах харнеса); (б)
JSON-Schema boilerplate, продублированный по тула́м — у `mnemos_search`
схема 1 594 симв. при описании 186; у граф-тулов пропсы
`project_id/agent/session` повторяются в каждом из 10 (в коде шарятся,
при сериализации дублируются). Топ-12 тулов держат ~55 % веса.

### 3.3 Proposal по похудению (НЕ в этом слайсе — только фиксация)

1. **Группировка (non-breaking 5.x подготовка, дроп в 6.0):** `watch_*` →
   один тул с `action`; `mnemos_tags_rename` исчезает из манифеста (алиас
   уже диспетчится). Экономия ~0.4k tok.
2. **Description-diet:** срезать инструкции-повторы (мандатные формулы
   AUTO-COLLECT, gate-напоминания) на 30–40 % — канон живёт в
   instruction-паке, манифесту достаточно one-line + ссылки. Экономия
   ~1.5–2k tok.
3. **Schema-diet:** общие `$defs`-фрагменты (graph-пропсы), короче
   описания пропсов, схлопывание необязательных полей. Экономия ~1–1.5k tok.
4. **Opt-in компактный манифест** для стеснённых клиентов (env-gated,
   non-breaking): name + one-line, полная схема по запросу — требует
   клиентской поддержки, проработать после MCP-решения по lazy tools.

Суммарно реалистично: **~10.9k → ~6.5–7.5k tok/сессия** без потери
функциональности.

## 4. 6.0-POCKET: регруппировка (документация, всё non-breaking в 5.x)

1. **watch-группировка.** Сейчас 3 тула (`watch_start`/`watch_stop`/
   `watch_status`, ~302 tok): REST-пара `/watch/start|stop|status`
   (`api/main.py:1558-1600`) зеркалится тремя MCP-тулами. В 6.0 — один
   `watch(action)`; описание `watch_start` уже фиксирует, что прежняя
   directory-watcher-форма была стаб-ом и удалена (`mcp_server.py:1036-1064`).
2. **Слияние tags/tags_rename.** Фактически УЖЕ слито в диспетче:
   `mnemos_tags_rename` — non-breaking алиас, форсит `action="rename"` в
   групповой `mnemos_tags` (`mcp_server.py:2713-2725`). 6.0 дропает алиас из
   манифеста; код-путь един.
3. **Выход `mnemos_*` легаси-алиасов при бренде.** Бренд-маппинг:
   `_brand_alias`/`_canonicalize_tool_name` (`mcp_server.py:105-130`).
   С 5.x манифест brand-primary (только `<brand>_*`, `mcp_server.py:424-441`),
   легаси-`mnemos_*` принимаются на call-path — «dual-period until 6.0»,
   «retires no earlier than 6.0» (`mcp_server.py:73-80, 429, 2201`).
4. **Унификация env-семейства VESMARO_* → VESMA_*.** Пары dual-period:
   `VESMARO_AUTO_COLLECT` → `VESMA_AUTO_COLLECT` (`mcp_server.py:63-71`),
   `VESMARO_MCP_BRAND` → `VESMA_MCP_BRAND` (`mcp_server.py:82-92`),
   `VESMARO_EXPORT_PASSPHRASE` → `VESMA_EXPORT_PASSPHRASE`
   (`mcp_server.py:2349-2361`), плюс `VESMARO_`-префикс settings-модели
   (`config.py:1313`). Все помечены «accepted until 6.0».
   **Поправка к карточке:** символов `_VESMARO_COMPLETE`/`_VESMA_COMPLETE`
   в репо нет (grep по коду и докам — 0 совпадений); носитель этого пункта —
   перечисленное env-семейство, в 6.0 снимаются deprecated-спеллинги.

## 5. Decision records (TL-черновики — до ратификации АрхКомом)

**DR-1 (reindex-parity).** Reindex — ops-only (REST `POST /reindex` + CLI
`vesma reindex`); в MCP не добавляется, исключение документируется.
Rationale: операторский lifecycle-шаг пере-эмбедировки; агентскую свежесть
закрывают `mnemos_index_project` + watch + autoindex; цена манифеста
~400–500 tok без спроса.

**DR-2 (бюджет веса манифеста).** Базовый per-session вес зафиксирован:
≈ 10.9k tok (39 тулов, chars/4 по сериализованному манифесту). Цель 6.x —
≤ 7k tok теми же четырьмя рычагами п. 3.3; замер воспроизводим скриптом
аудита и пригоден для CI-отчёта.

**DR-3 (состав 6.0-пакета).** watch-группировка; дроп `mnemos_tags_rename`
из манифеста; выход `mnemos_*` call-path-алиасов при бренде; снятие
`VESMARO_*` deprecated-env. Всё — один несущий релиз 6.0, в 5.x только
документирование (этот отчёт).

## 6. Ограничения аудита

- Телеметрия: ≈ 27 ч shadow-окна (wave-0 ADR-0035), одна инсталляция —
  dead-выводы валидны только как кандидаты, не вердикты (п. 1.3).
- Оценка токенов chars/4 — консервативная нижняя граница; реальный
  клиентский счёт зависит от токенизатора и конверта `tools/list`.
- Аудит читал прод-стор только в `mode=ro`; запись не производилась,
  секреты не печати (в телеметрии их и не ожидается — fail-closed
  meta-allowlist ADR-0035).
