# Паритет поверхностей API — CLI / REST / MCP

**Ратифицировано Архитектурным комитетом 2026-10-06 (консенсус; Analytics
Lead и Product Architect подписали каждую диспозицию). Эта запись существует,
чтобы вопрос не переоткрывался:** будущие предложения поверхностей
оцениваются по ней, а не переобсуждаются. Parity-вердикт той же сессии,
что приняла [ADR-0038](../../project/adr/0038-pg1-graph-walk.md).

## Принцип

**Паритет — это названный пользователь поверхности, а не подсчёт глаголов.**
Гэп — это реальный пользователь без своей поверхности, а не отсутствующая
галочка в тройном диффе.

| Поверхность | Для кого |
|---|---|
| CLI (`vesma ...`) | Люди и shell-скрипты |
| REST (`/api/v1/...`) | Не-Python интеграции |
| MCP (тулы `mnemos_*` — канон; бренд-примари `vesma_*` при `VESMA_MCP_BRAND`) | Агенты |

Возможность может легитимно жить только на одной поверхности. Твин
добавляется, когда его названный пользователь существует — и тогда только
по канону версионирования ниже (новые REST-твины идут в `/api/v1`).

## Матрица (сверена с main `408836f`)

Пере-энумерирована по коду, а не по старым таблицам: 10 top-level CLI-команд
плюс 21 CLI-группа (`src/vesmaro/cli/`), 66 REST-роутов (`src/vesmaro/api/` —
main, auth, federation, A2A-сессии), 40 MCP-тулов (`src/vesmaro/mcp_server.py`).
`✓` — есть, `—` — нет, `— *(wontfix)*` — ратифицированное non-goal (см.
диспозиции ниже).

| Операция | CLI | REST | MCP |
|---|---|---|---|
| Добавить / искать / пере-фильтровать память | ✓ `add` `search` `filter` | ✓ `POST /memories` · `/search` · `/filter/{id}` | ✓ `add` `search` `filter` |
| Список недавних памятей | — | ✓ `GET /memories` | ✓ `list_recent` |
| Agent recall (именованный агент) | ✓ `recall agent` | ✓ `GET /recall/agent/{name}` | ✓ `agent_recall` |
| Recall контекста сессии (чекпоинты) | — | ✓ `POST /context/recall` | ✓ `recall_context` |
| Context save / assemble / rewrite | — | ✓ `/context/save` · `/context/assemble` · `/context/rewrite` | ✓ `save_context` · `assemble_context` · `context_rewrite` |
| Сжать → достать оригинал (CCR) | — | ✓ `POST /compress` + `POST /retrieve` | ✓ `compress` + `retrieve` |
| Теги (список / add / remove / rename / validate) | ✓ `tags validate·normalize·rename·audit` | ✓ `GET /tags` · `POST /tags/rename` · `POST /api/v1/tags/add` · `/remove` | ✓ `tags` · `tags_rename` · `list_tags` |
| Pipeline process | ✓ `processor run·start·stop·status` | ✓ `POST /process` | ✓ `reprocess` |
| Pipeline-операции (synthesize / publish / DLQ / quarantine release) | — | ✓ `/synthesize` · `/publish/{id}` · `/dlq*` · `/memories/{id}/quarantine/release` | — |
| Трейсы пайплайна | ✓ `logs` | ✓ `GET /traces` | — |
| Lifecycle-хуки | — | ✓ `POST /hooks/{action}` | ✓ `hooks` |
| Ingest path-scoped-правил | — | ✓ `POST /rules/ingest` · `DELETE /rules/ingest` | — |
| Reindex | ✓ `reindex` | ✓ `POST /reindex` | — *(wontfix)* |
| Ingest URL | ✓ `add --url` | ✓ `POST /ingest-url` | ✓ `ingest_url` |
| Ingest документов (quarantine-пайплайн) | — | ✓ `POST /ingest-document` | ✓ `ingest_document` |
| Watch / auto-collect | — | ✓ `/watch/start·stop·status` · `GET /auto-collect` | ✓ `watch_start·stop·status` · `auto_collect_status` |
| Статистика / метрики | ✓ `stats` | ✓ `GET /api/v1/stats` (+ `/timeseries`) · `GET /api/v1/metrics` (Prometheus); `GET /metrics` — легаси-JSON | ✓ `stats` |
| Export / import | ✓ `export` · `import` | ✓ `POST /api/v1/export` · `POST /api/v1/import` | ✓ `export` · `import` |
| Workflow-жизненный цикл | ✓ `workflow get·set·history` | ✓ `GET·POST·DELETE /memories/{id}/workflow` | ✓ `workflow` |
| Graph-чтение (index / status / search / trace / outline / snippet / coverage / schema / список / delete) | — | ✓ `/graph/*` | ✓ `index_project` … `delete_graph_project` |
| Graph register | ✓ `graph register` | ✓ `POST /api/v1/graph/register` | ✓ `register_project` |
| Graph repoint | ✓ `graph repoint` | ✓ `POST /api/v1/graph/repoint` | — *(wontfix)* |
| Sync-пейлоад (офлайн export/import-файл) | ✓ `sync export·import` | — *(wontfix)* | — |
| Federation pull | ✓ `fetch` | ✓ `POST /api/v1/federation/pull` | — |
| Сканер (quarantine-дашборд) | ✓ `scanner run·status` | — *(wontfix)* | — |
| Выравнивание кэша / awareness / usage report | — | — *(wontfix)* | ✓ `align_prefix` · `awareness` · `usage_report` |
| Ops (auth, сессии, service, update, doctor, migrate, fts, edge-stats, integration, agent-token, serve) | ✓ | ✓ только `/auth/*`; A2A-сессии на `/v1` — отдельная версионированная поверхность | — |

Ячейки описывают наличие возможности, а не равенство аргументов флаг-в-флаг —
у каждой поверхности свой справочник
([cli-reference.md](cli-reference.md),
[http-api.md](http-api.md),
[mcp-tools.md](mcp-tools.md)).

## Ратифицированные диспозиции

Каждый гэп ниже разобран комитетом и закрыт с основанием.
**Все четыре гэпа — `wontfix`; вопрос по умолчанию не переоткрывается.**

| Гэп | Диспозиция | Почему |
|---|---|---|
| Generic `GET /recall` | wontfix | Сценарий «достать релевантное» уже закрыт `POST /search` + `POST /context/recall`; третий глагол размывает поверхность — а кэшируемый идемпотентный GET с тяжёлым семантическим запросом ломает REST-семантику. |
| `/fetch` | wontfix — гэпа нет | Твин существует: `POST /retrieve` зеркалит MCP `retrieve`; CLI `fetch` — про federation S2, это и было источником путаницы. |
| `/sync` | wontfix | Файл-пейлоад с шифрованием — это человек за CLI (export/import уже живут под `/api/v1` — `/sync` стал бы третьим глаголом); сервисный bulk закрыт `/api/v1/federation/pull`. |
| `/scanner` | wontfix (пересмотр, если станет операторским дашбордом) | Инициатор скана — оператор (CLI/service); REST-потребители — объекты скана, и у них уже есть quarantine/release. |

Ретро-строки (тот же вердикт): `align_prefix` (выравнивание KV-кэша —
работа агента) / `awareness` (интроспекция агента; диагностика живёт в
`/api/v1/stats`) / reindex без MCP / graph repoint без MCP (операторское
обслуживание) — **wontfix**. Reprocess — гэпа нет (`POST /process`).

## Канон версионирования

Ратифицирован АрхКомом 2026-10-04 и действует:

- **Канонические руты живут только под `/api/v1`.** Новые маршруты никогда
  не появляются на голом корне.
- **Корневые пути — легаси-алиасы.** Пока Vesma 6.0 физически не перевезёт
  их, каждый ответ через алиас украшается deprecation-мидлварём
  (`Deprecation: true` + заголовок `Link` с каноническим шаблоном `/api/v1`).
- **`Sunset` (RFC 8594) появится с 6.0.** Заголовок требует абсолютную
  HTTP-дату; его добавят, как только дата 6.0 зафиксирована, — выдумывать
  её раньше значит вводить клиентов в заблуждение.
- **Breaking-унификация запаркована за 6.0** — снятие алиасов, физический
  переезд маршрутов и любая унификация форм запросов/ответов садятся в
  один мажор, а не по кусочкам.
- **`/metrics` и `/api/v1/metrics` — задокументированный около-дубликат:**
  корневой путь отдаёт легаси-тело со stats-JSON, канонический —
  Prometheus-экспозицию в текстовом формате.

Детали и таблицы по маршрутам: [http-api.md](http-api.md) («Версионирование
маршрутов» в Соглашениях).

---

_Протокол комитета 2026-10-06 (team-local:
`~/.gcw/architectural-committee/2026-10-06-vesma-pg1-walk-and-rest-parity.md`),
[ADR-0038](../../project/adr/0038-pg1-graph-walk.md). Последнее обновление: 2026-10-06._
