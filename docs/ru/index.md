# Документация Mnemos (Русский)

**🌐 Language / Язык:** [English](../en/index.md) · Русский

> Mnemos — автономный сервер памяти и знаний для AI-агентов. Даёт каждому агенту
> настоящую долгосрочную память — структурированную, доступную для поиска,
> управляемую строгим контрактом тегов, — которая переживает сессии, рестарты
> и сжатие контекста. Один локальный сервер, три поверхности управления
> (MCP / CLI / HTTP), и любой харнесс с поддержкой MCP подключается одной строкой.

---

## Установка (одна команда)

Mnemos опубликован на **PyPI**:

```bash
pip install mnemos-memory-server
```

Модель эмбеддингов по умолчанию (`mnema-embed-v1`, ~30 МБ) встроена в wheel —
поиск работает полностью офлайн, на CPU, без API-ключей и без скачиваний.
Изолированный вариант: `uv tool install mnemos-memory-server` или
`pipx install mnemos-memory-server`.

> ⚠️ Имя пакета на PyPI — **`mnemos-memory-server`**: `pip install mnemos`
> устанавливает не связанный проект.

npm (расширение pi): `pi-mnemos` · `mnemos-pi` · `@korrlabs/mnemospi` ·
`@korrlabs/mnemos-pi`. Контейнер: `ghcr.io/vesmaro/vesmaro`.

---

## Подключите ваш харнес

**MCP — основная поверхность интеграции.** Любой харнесс с поддержкой MCP
говорит с Mnemos по одному и тому же stdio-проводу. Полные инструкции для
копирования для каждого харнесса собраны на одной странице:
**[Подключите Mnemos к любому харнесу](../../integrations/mcp-presets.md)**.

| Харнесс | Самый быстрый путь |
|---------|--------------------|
| VS Code Copilot | `curl -fsSL …/scripts/mcp-setup.sh \| bash` — или блок `mcp.json` в одно действие со страницы пресетов |
| Claude Code | `claude mcp add --scope user mnemos -- mnemos mcp-server` |
| Cursor | одна строка в `~/.cursor/mcp.json` |
| OpenCode | один блок в `~/.config/opencode/opencode.json` |
| Codex | один TOML-блок в `~/.codex/config.toml` |
| Windsurf | та же JSON-строка, что для Cursor, в `~/.codeium/windsurf/mcp_config.json` |
| ZCode / pi | `mnemos integration setup --target zcode` / `--target pi` |
| Hermes Agent | нативный in-process плагин — `mnemos integration setup --target hermes` |
| Всё остальное | [adapter-template.md](../../integrations/adapter-template.md) — Connect / Expose / Configure |

Чтобы заодно развернуть **поведенческий слой** (инструкции памяти, 14+ скиллов,
режим промпта, wiring агентов), чтобы агенты *знали когда и как* пользоваться памятью:

```bash
mnemos integration setup
```

Таргеты, флаги и полная карта развёртывания: [integration-guide.md](user/integration-guide.md).

### Свойства MCP-сервера

| Свойство | Значение |
|----------|---------|
| Протокол | MCP поверх **stdio JSON-RPC 2.0** |
| Имя сервера | `mnemos` |
| Транспорт | stdio — без TCP-порта |
| Префикс инструментов | `mnemos_` |
| Запуск | `mnemos mcp-server` |

### Режим автосбора

Установите `MNEMOS_AUTO_COLLECT=1` в блоке `env` сервера, чтобы Mnemos предлагал
агенту вызывать `mnemos_save_context` каждые ~6 вызовов инструментов
(проактивные напоминания о чекпоинтах). О компромиссах:
[mcp-tools.md#auto-collect-mode](user/mcp-tools.md#режим-auto-collect).

### 38 MCP-инструментов (префикс `mnemos_`)

| Инструмент | Назначение |
|-----------|-----------|
| `mnemos_search` | Гибридный поиск FTS5 + вектор со слиянием ранжирования RRF (по умолчанию — опубликованные записи) |
| `mnemos_add` | Создать запись — **соблюдает контракт тегов Mnemos** |
| `mnemos_filter` | Прогнать или обновить контекстный фильтр на существующей записи (например, с другим профилем) |
| `mnemos_agent_recall` | Recall по агенту (M3) — последние записи одного агента |
| `mnemos_save_context` | Сохранить чекпоинт сессии |
| `mnemos_recall_context` | Восстановить последний чекпоинт проекта |
| `mnemos_list_recent` | Список последних записей |
| `mnemos_list_tags` | Список всех тегов с количеством |
| `mnemos_tags_rename` | Массово переименовать префикс тега по существующим записям (по умолчанию dry-run) |
| `mnemos_tags` | Массовые операции с тегами: переименовать префикс, удалить или добавить теги |
| `mnemos_ingest_url` | Скачать веб-страницу и сохранить как запись |
| `mnemos_ingest_document` | Ингест документа чанками с born-quarantine (ADR-0027 Ф3) |
| `mnemos_watch_start` | Регистрация проекта в watch-опросе графа проектов (ADR-0032 §3.2; графовые флаги включены по умолчанию с 2026-09-28) |
| `mnemos_watch_stop` | Остановить одну или все регистрации watch |
| `mnemos_watch_status` | Активные регистрации watch и итог последнего опроса |
| `mnemos_index_project` | Индексация зарегистрированного корня проекта в граф проектов (ADR-0032, on by default) |
| `mnemos_project_graph_status` | Объёмы, свежесть, ошибки разбора и poisoned-файлы графа проекта |
| `mnemos_search_graph` | Ранжированный поиск по графу с токен-контрактом |
| `mnemos_trace_path` | BFS по рёбрам графа от одного символа (глубина ≤ 2) |
| `mnemos_get_file_outline` | Схема символов одного проиндексированного файла (формы, никогда тела) |
| `mnemos_get_code_snippet` | Секрет-сканированное чтение диапазона строк с диска (PG4) |
| `mnemos_check_graph_coverage` | Вердикты покрытия по путям: indexed / stale / parse-error / unindexed / poisoned |
| `mnemos_get_graph_schema` | Карта контракта графа для агентов |
| `mnemos_list_graph_projects` | Зарегистрированные проекты вместе со статусом индекса |
| `mnemos_delete_graph_project` | Удалить индекс графа (только sidecar); очищает poisoned-набор |
| `mnemos_auto_collect_status` | Вектор сигналов уплотнения контекста (M7) |
| `mnemos_stats` | Счётчики здоровья и ключевые пути |
| `mnemos_reprocess` | Вручную запустить конвейер знаний по очереди записей |
| `mnemos_compress` | CCR: сжать большой контент без потери данных — оригинал в кэше, возвращается маркер |
| `mnemos_retrieve` | Получить оригинал контента по хешу CCR-маркера |
| `mnemos_align_prefix` | CacheAligner (P1-5): перенос динамического контента в хвост под попадания KV-кэша |
| `mnemos_assemble_context` | Собрать контекстный блок для модели: поиск → сжатие → фильтр → скан секретов → выравнивание кэша → бюджет токенов |
| `mnemos_context_rewrite` | `on_context_rewrite` (ADR-0018): сохранить оригинал без потерь при перезаписи истории харнессом |
| `mnemos_hooks` | Хуки жизненного цикла: действия `pre_llm_call` / `on_session_start` / `post_tool_call` |
| `mnemos_export` | Экспорт записей в файл на диске |
| `mnemos_import` | Импорт записей из файла экспорта |
| `mnemos_workflow` | Жизненный цикл workflow записи (open → in-progress → done, blocked / …) |

Полный каталог со схемами ввода, примерами и HTTP-эквивалентами:
**[user/mcp-tools.md](user/mcp-tools.md)**

---

## С чего начать

| Если вы… | Читайте |
|----------|---------|
| Устанавливаете Mnemos впервые | [user/getting-started.md](user/getting-started.md) |
| Подключаете конкретный харнес | [Подключите Mnemos к любому харнесу](../../integrations/mcp-presets.md) |
| Ищете конкретную команду / флаг | [user/cli-reference.md](user/cli-reference.md) |
| Ищете конкретный MCP-инструмент | [user/mcp-tools.md](user/mcp-tools.md) |
| Разрабатываете HTTP-клиент | [user/http-api.md](user/http-api.md) |
| Разбираетесь в устройстве системы | [architecture/overview.md](architecture/overview.md) |
| Диагностируете проблему | [user/getting-started.md#устранение-неполадок](user/getting-started.md#устранение-неполадок) |

---

## Документация для пользователей

- [Карта функционала](features.md) — что работает из коробки, что частично, что в плане (v4.0.0).
- [Начало работы](user/getting-started.md) — установка → первая запись → первый поиск → подключение харнеса.
- [Руководство по интеграции](user/integration-guide.md) — поведенческий слой, таргеты развёртывания, wiring MCP-инструментов к агентам, плагин Hermes.
- [Справочник MCP-инструментов](user/mcp-tools.md) — все инструменты `mnemos_*`.
- [Справочник HTTP API](user/http-api.md) — все эндпоинты, форматы запросов и ответов, коды ошибок.
- [Справочник CLI](user/cli-reference.md) — все подкоманды `mnemos` с флагами, значениями по умолчанию и примерами.
- [Контракт тегов](user/tag-contract.md) — схема M2, обязательная для каждой записи (`project:`, `agent:`, `mnemos:`).
- [Контекстный фильтр](user/context-filter.md) — пятиступенчатый очиститель шума (dedup, noise, extract, compress, tokens) с профилями и автофильтром.

---

## Администрирование / Эксплуатация

- [Ранбук: Установка](admin/runbooks/install.md) — операционный чеклист первого запуска.
- [Ранбук: Контейнерное развёртывание](admin/runbooks/container-deployment.md) — сборка, push, compose, podman, Kubernetes, quadlet.
- [Ранбук: Миграция](admin/runbooks/migrate.md) — импорт из legacy `ai-brain`.
- [Ранбук: Резервное копирование и восстановление](admin/runbooks/backup-restore.md) — бэкап, восстановление на момент времени.
- [Ранбук: Обновление зависимостей](admin/runbooks/dependency-updates.md) — разбор CVE + еженедельный обзор.
- [Ранбук: CI/CD](admin/runbooks/ci-cd.md) — эксплуатация пайплайна GitHub Actions.
- [Ранбук: Публикация в PyPI](admin/runbooks/pypi-publish.md) — доступность имени, wheel-конвейер, версионные гейты, процедура первой публикации.
- [Модель безопасности](admin/security.md) — модель угроз, SSRF-защита, гигиена секретов, модель аутентификации.

---

## Архитектура

- [Обзор системы](architecture/overview.md) — слоистый дизайн, модель данных, автоматы состояний, границы безопасности, эксплуатационные аспекты.
- [Конвейер знаний](user/http-api.md#пайплайн-знаний-m4) — как запись проходит `raw` → `processing` → `processed` → `published` (M4).
- [A2A Sessions](architecture/a2a-sessions.md) — контракт разговоров агент-агент (M16).

---

## Проект (историческое, только EN)

- [Architecture Decision Records](../project/adr/README.md) — 22 ADR, покрывающие эволюцию M1 → M16 и фундамент v4.0.0.
- [Milestones](../project/milestones.md) — журнал вех с легендой статусов.
- [Отчёты о завершённых этапах](../project/reports/) — итоговые отчёты по каждой завершённой фазе дорожной карты (Фазы 0–1: PR #135–#157).
- [Code Review 2026-06](../project/code-review-2026-06.md) — итоговые находки и исправления финального код-ревью.
- [Сессии](../project/sessions/) — документы оркестрационных сессий.

---

## Корень репозитория

- [README](../../README.md) — главная страница проекта.
- [CHANGELOG](../../CHANGELOG.md) — release notes.
- [PLAN](../../PLAN.md) — поэтапный план реализации.
- [ARCHITECTURE](../../ARCHITECTURE.md) — краткое резюме архитектуры (one-pager; полная версия — [architecture/overview.md](architecture/overview.md)).

---

_Последнее обновление: 2026-09-05_
