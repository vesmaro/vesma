# Документация Vesma (Русский)

**🌐 Language / Язык:** [English](../en/index.md) · Русский

> Vesma — автономный сервер памяти и знаний для AI-агентов. Даёт каждому агенту
> настоящую долгосрочную память — структурированную, доступную для поиска,
> управляемую строгим контрактом тегов, — которая переживает сессии, рестарты
> и сжатие контекста. Один локальный сервер, три поверхности управления
> (MCP / CLI / HTTP), и любой харнесс с поддержкой MCP подключается одной строкой.

Кодовая база тоже становится памятью: **[граф проектов](user/project-graph.md)**
(ADR-0032, включён по умолчанию) превращает зарегистрированный проект в
searchable-карту символов — схемы файлов, ранжированный поиск по символам,
трассировку вызовов, сканированные на секреты сниппеты строк — без единого
байта исходника на сервере.

---

## Установка (одна команда)

Vesma опубликован на **PyPI** (голый слот — наш, со времён ребрендинга):

```bash
pip install vesma
```

Модель эмбеддингов по умолчанию (`vesma-embed-v1`, ~30 МБ) встроена в wheel —
поиск работает полностью офлайн, на CPU, без API-ключей и без скачиваний.
Изолированный вариант: `uv tool install vesma` или `pipx install vesma`.

> ⚠️ **Имена.** Пакет на PyPI — **`vesma`** (голый слот наш со времён ребрендинга —
> основной канал). Доребрендинговый пакет `mnemos-memory-server` остаётся живым
> до deprecation (`pip install mnemos-memory-server` ставит тот же сервер).

npm (расширение pi): `pi-vesma` · `vesma-pi` · `@korrlabs/vesmapi` ·
`@korrlabs/vesma-pi`. Контейнер: `ghcr.io/vesmaro/vesma`.

---

## Подключите ваш харнес

**MCP — основная поверхность интеграции.** Любой харнесс с поддержкой MCP
говорит с Vesma по одному и тому же stdio-проводу. **Ручная регистрация MCP
отменена — единственный путь: утилита.** Копипаст-fallback для нестандартных
харнесов собран на одной странице:
**[Подключите Vesma к любому харнесу](../../integrations/mcp-presets.md)**.

| Харнесс | Самый быстрый путь |
|---------|--------------------|
| VS Code Copilot | `vesma integration setup --target copilot` |
| Claude Code | `vesma integration setup --target claude-code` |
| Cursor | `vesma integration setup --target cursor` |
| Codex | `vesma integration setup --target codex` |
| Windsurf | `vesma integration setup --target windsurf` |
| ZCode / pi | `vesma integration setup --target zcode` / `--target pi` |
| Hermes Agent | нативный in-process плагин — `vesma integration setup --target hermes` |
| OpenCode | один блок в `~/.config/opencode/opencode.json` (нативной цели нет — [пресет](../../integrations/mcp-presets.md#opencode)) |
| Всё остальное | [adapter-template.md](../../integrations/adapter-template.md) — Connect / Expose / Configure |

`vesma integration setup` без флагов разворачивает всё сразу — и заодно
разворачивает **поведенческий слой** (инструкции памяти, 14+ скиллов,
режим промпта, wiring агентов), чтобы агенты *знали когда и как* пользоваться памятью:

```bash
vesma integration setup
```

Таргеты, флаги и полная карта развёртывания: [integration-guide.md](user/integration-guide.md).

### Свойства MCP-сервера

| Свойство | Значение |
|----------|---------|
| Протокол | MCP поверх **stdio JSON-RPC 2.0** |
| Имя сервера | `vesma` (ключ реестра в конфигах харнесов: `vesma` — контракт двойного префикса) |
| Транспорт | stdio — без TCP-порта |
| Префикс инструментов | `vesma_` |
| Запуск | `vesma mcp-server` |

### Режим автосбора

Установите `VESMA_AUTO_COLLECT=1`
в блоке `env` сервера, чтобы Vesma предлагал
агенту вызывать `vesma_save_context` каждые ~6 вызовов инструментов
(проактивные напоминания о чекпоинтах). О компромиссах:
[mcp-tools.md#auto-collect-mode](user/mcp-tools.md#режим-auto-collect).

### 38 MCP-инструментов (префикс `vesma_`)

| Инструмент | Назначение |
|-----------|-----------|
| `vesma_search` | Гибридный поиск FTS5 + вектор со слиянием ранжирования RRF (по умолчанию — опубликованные записи) |
| `vesma_add` | Создать запись — **соблюдает контракт тегов Vesma** |
| `vesma_filter` | Прогнать или обновить контекстный фильтр на существующей записи (например, с другим профилем) |
| `vesma_agent_recall` | Recall по агенту (M3) — последние записи одного агента |
| `vesma_save_context` | Сохранить чекпоинт сессии |
| `vesma_recall_context` | Восстановить последний чекпоинт проекта |
| `vesma_list_recent` | Список последних записей |
| `vesma_list_tags` | Список всех тегов с количеством |
| `vesma_tags_rename` | Массово переименовать префикс тега по существующим записям (по умолчанию dry-run) |
| `vesma_tags` | Массовые операции с тегами: переименовать префикс, удалить или добавить теги |
| `vesma_ingest_url` | Скачать веб-страницу и сохранить как запись |
| `vesma_ingest_document` | Ингест документа чанками с born-quarantine (ADR-0027 Ф3) |
| `vesma_watch_start` | Регистрация проекта в watch-опросе графа проектов (ADR-0032 §3.2; графовые флаги включены по умолчанию с 2026-09-28) |
| `vesma_watch_stop` | Остановить одну или все регистрации watch |
| `vesma_watch_status` | Активные регистрации watch и итог последнего опроса |
| `vesma_index_project` | Индексация зарегистрированного корня проекта в граф проектов (ADR-0032, on by default) |
| `vesma_project_graph_status` | Объёмы, свежесть, ошибки разбора и poisoned-файлы графа проекта |
| `vesma_search_graph` | Ранжированный поиск по графу с токен-контрактом |
| `vesma_trace_path` | BFS по рёбрам графа от одного символа (глубина ≤ 2) |
| `vesma_get_file_outline` | Схема символов одного проиндексированного файла (формы, никогда тела) |
| `vesma_get_code_snippet` | Секрет-сканированное чтение диапазона строк с диска (PG4) |
| `vesma_check_graph_coverage` | Вердикты покрытия по путям: indexed / stale / parse-error / unindexed / poisoned |
| `vesma_get_graph_schema` | Карта контракта графа для агентов |
| `vesma_list_graph_projects` | Зарегистрированные проекты вместе со статусом индекса |
| `vesma_delete_graph_project` | Удалить индекс графа (только sidecar); очищает poisoned-набор |
| `vesma_auto_collect_status` | Вектор сигналов уплотнения контекста (M7) |
| `vesma_stats` | Счётчики здоровья и ключевые пути |
| `vesma_reprocess` | Вручную запустить конвейер знаний по очереди записей |
| `vesma_compress` | CCR: сжать большой контент без потери данных — оригинал в кэше, возвращается маркер |
| `vesma_retrieve` | Получить оригинал контента по хешу CCR-маркера |
| `vesma_align_prefix` | CacheAligner (P1-5): перенос динамического контента в хвост под попадания KV-кэша |
| `vesma_assemble_context` | Собрать контекстный блок для модели: поиск → сжатие → фильтр → скан секретов → выравнивание кэша → бюджет токенов |
| `vesma_context_rewrite` | `on_context_rewrite` (ADR-0018): сохранить оригинал без потерь при перезаписи истории харнессом |
| `vesma_hooks` | Хуки жизненного цикла: действия `pre_llm_call` / `on_session_start` / `post_tool_call` |
| `vesma_export` | Экспорт записей в файл на диске |
| `vesma_import` | Импорт записей из файла экспорта |
| `vesma_workflow` | Жизненный цикл workflow записи (open → in-progress → done, blocked / …) |

Полный каталог со схемами ввода, примерами и HTTP-эквивалентами:
**[user/mcp-tools.md](user/mcp-tools.md)**

---

## С чего начать

| Если вы… | Читайте |
|----------|---------|
| Устанавливаете Vesma впервые | [user/getting-started.md](user/getting-started.md) |
| Подключаете конкретный харнес | [Подключите Vesma к любому харнесу](../../integrations/mcp-presets.md) |
| Ищете конкретную команду / флаг | [user/cli-reference.md](user/cli-reference.md) |
| Ищете конкретный MCP-инструмент | [user/mcp-tools.md](user/mcp-tools.md) |
| Делаете кодовую базу навигируемой для агентов | [user/project-graph.md](user/project-graph.md) |
| Разрабатываете HTTP-клиент | [user/http-api.md](user/http-api.md) |
| Разбираетесь в устройстве системы | [architecture/overview.md](architecture/overview.md) |
| Диагностируете проблему | [user/getting-started.md#устранение-неполадок](user/getting-started.md#устранение-неполадок) |

---

## Документация для пользователей

- [Карта функционала](features.md) — что работает из коробки, что частично, что в плане (v4.0.0).
- [Начало работы](user/getting-started.md) — установка → первая запись → первый поиск → подключение харнеса.
- [Руководство по интеграции](user/integration-guide.md) — поведенческий слой, таргеты развёртывания, wiring MCP-инструментов к агентам, плагин Hermes.
- [Справочник MCP-инструментов](user/mcp-tools.md) — все инструменты `vesma_*`.
- [Граф проектов](user/project-graph.md) — кодовая база как память: индексация, десять инструментов, ограждения безопасности, конфигурация.
- [Справочник HTTP API](user/http-api.md) — все эндпоинты, форматы запросов и ответов, коды ошибок.
- [Справочник CLI](user/cli-reference.md) — все подкоманды `vesma` с флагами, значениями по умолчанию и примерами.
- [Контракт тегов](user/tag-contract.md) — схема M2, обязательная для каждой записи (`project:`, `agent:`, `vesma:`).
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
