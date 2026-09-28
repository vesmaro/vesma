<!-- mnemos-integration: v2.0.0 -->
# Руководство по интеграции

**🌐 Language / Язык:** [English](../../en/user/integration-guide.md) · Русский

Слой интеграции Mnemos — это набор **поведенческих триггеров**, которые
заставляют агентов реально *использовать* инструменты памяти, а не просто
иметь их доступными. Без этих триггеров агенты забывают вызвать recall в
начале сессии, пропускают checkpoint перед компакцией и не указывают
обязательные теги.

---

## Что такое слой интеграции?

Три поверхности, каждая со своей силой:

| Поверхность | Что это | Как работает | Пример |
|-------------|---------|--------------|--------|
| **Инструкции** | `*.instructions.md` с `applyTo: '**'` | Пассивные правила — загружаются в контекст каждого агента безусловно. Описывают КОГДА и КАК. | "Recall в начале сессии, перед чтением файлов" |
| **Скиллы** | файлы `SKILL.md` | Workflow-гайды — пошаговые процедуры, загружаются по требованию. | "Как эффективно искать: узко → широко" |
| **Промпт-режим** | `*.prompt.md` | Активный режим — более строгий контракт, меняющий поведение агента для работы с памятью. | Режим `mnemos-memory` с обязательным recall + checkpoint |

### Инструкции vs скиллы vs промпты

- **Инструкции** — всегда включённые правила. Они говорят *когда* действовать.
  Каждый агент с инструментами `mnemos/*` получает их.
- **Скиллы** — процедуры по требованию. Они говорят *как* действовать. Агент
  загружает их, когда нужна процедура.
- **Промпт-режим** — опциональный контракт. Он говорит *теперь ты агент с
  памятью*. Используйте для сессий, где непрерывность памяти критична.

---

## Что входит в пакет

```text
integrations/
├── instructions/
│   ├── mnemos-session-lifecycle.instructions.md   # recall / checkpoint / save
│   ├── mnemos-memory-ops.instructions.md          # search / add / agent-recall
│   ├── mnemos-tag-contract.instructions.md        # обязательный состав тегов
│   └── canon-records.instructions.md              # канон-стандарты записей (конверт + секции)
├── skills/
│   ├── mnemos-session-init.md                     # recall в начале сессии
│   ├── mnemos-checkpoint.md                       # save в середине / при компакции
│   ├── mnemos-recall.md                           # эффективный поиск (узко → широко)
│   ├── mnemos-write.md                            # написание хороших записей
│   ├── mnemos-canon-write.md                      # канон-записи (task / decision / report)
│   └── mnemos-tag-contract.md                     # справочник схемы тегов
└── prompts/
    └── mnemos-memory.prompt.md                    # активный режим памяти
```

---

## Развёртывание

### Одна команда (все цели)

```bash
mnemos integration setup
```

Развёртывает инструкции, скиллы и промпт-режим на цель по умолчанию
(`~/.copilot/` для VS Code Copilot Chat). Идемпотентно — безопасно
запускать повторно.

### По цели

```bash
mnemos integration setup --target copilot           # VS Code Copilot ~/.copilot/ (по умолчанию)
mnemos integration setup --target generic-copilot   # промпт-режим VS Code ~/.config/Code/User/prompts/
mnemos integration setup --target cursor            # Cursor ~/.cursor/
mnemos integration setup --target zcode             # ZCode (нативные скиллы + конфиг MCP)
mnemos integration setup --target agents            # стандарт ~/.agents — Claude Code, Codex, Cursor, …
mnemos integration setup --target pi                # агент Pi (бридж-расширение)
mnemos integration setup --target hermes            # Hermes Agent (нативный плагин)
mnemos integration setup --target all               # все обнаруженные цели
```

Имена целей берутся из `integrations/targets.yaml`; `--help` выводит список для
вашей установки. У Claude Code / Codex нет отдельных целей — они нативно читают
цель `agents`.

### Универсальные цели: ZCode и стандарт AGENTS.md

`zcode` и `agents` используют **вложенную раскладку скиллов** — каждый скилл
ложится как `<каталог-скиллов>/<имя>/SKILL.md`, формат, который ZCode и
кросс-инструментный стандарт `~/.agents` читают нативно:

| Цель | Скиллы → | Регистрация MCP |
|------|----------|-----------------|
| `zcode` | `~/.zcode/skills/<имя>/SKILL.md` | `~/.zcode/cli/config.json` → `mcp.servers` (JSON-слияние, остальные серверы сохраняются) |
| `agents` | `~/.agents/skills/<имя>/SKILL.md` | `~/.agents/mcp.json` → ключ `mcpServers` верхнего уровня |

Цель `agents` работает в **любом харнессе**, читающем стандартные
расположения AGENTS.md (ZCode, Claude Code, Codex, Cursor, …) — одна
установка на все инструменты. Слияние MCP аддитивное: существующие серверы,
плагины и пользовательски настроенный `env` у записи `mnemos` никогда не
перезаписываются.

### Агент Pi

Цель `pi` — для [агента Pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
(npm `@earendil-works/pi-coding-agent`). У Pi нет встроенного MCP-клиента —
инструменты приходят через TypeScript-расширения, поэтому регистрация MCP —
это файл: поставляемый бридж `integrations/extensions/mnemos-mcp.ts`
развёртывается (со штампом версии) в `~/.pi/agent/extensions/`, откуда Pi
загружает его автоматически. При старте сессии бридж поднимает
`mnemos mcp-server` по stdio и нативно регистрирует все инструменты
`mnemos_*`; `/reload` перезагружает расширение, `/mnemos` переподключает
бридж.

```bash
mnemos integration setup --target pi
```

Скиллы развёртываются во вложенной раскладке, которую Pi читает нативно
(`~/.pi/agent/skills/<имя>/SKILL.md`). Поскольку Pi также читает
`~/.agents/skills/`, предпочитайте `--target pi` вместо развёртывания обеих
целей — чтобы не дублировать скиллы. `mnemos integration uninstall
--target pi` удаляет только штампованные бридж и скиллы — пользовательские
расширения не затрагиваются.

### Установка в другое окружение (--home)

Развёртывание в home другого окружения (контейнер, checkout дотфайлов) без
правки targets.yaml:

```bash
mnemos integration setup --target zcode \
  --home /var/home/you/.distrobox/other-box/home \
  --mnemos-bin /path/to/mnemos-wrapper \
  --no-wire-agents
```

`~` в targets.yaml резолвится относительно `--home`. Передавайте
`--mnemos-bin`, если целевое окружение запускает mnemos через враппер или по
другому пути.

### Куда что развёртывается

| Цель | Инструкции → | Скиллы → | Промпты → |
|------|--------------|----------|-----------|
| `copilot` | `~/.copilot/instructions/` | `~/.copilot/skills/` | — |
| `generic-copilot` | — | — | `~/.config/Code/User/prompts/` |
| `cursor` | `~/.cursor/rules/` | — | — |
| `hermes` | `~/.hermes/skills/` | `~/.hermes/skills/` (+ плагин в `~/.hermes/plugins/mnemos/`) | — |
| `zcode` | — | `~/.zcode/skills/<имя>/SKILL.md` | MCP в `~/.zcode/cli/config.json` |
| `agents` | — | `~/.agents/skills/<имя>/SKILL.md` | MCP в `~/.agents/mcp.json` |
| `pi` | — | `~/.pi/agent/skills/<имя>/SKILL.md` | бридж: `~/.pi/agent/extensions/mnemos-mcp.ts` |

---

## Проверка

После развёртывания проверьте, что все файлы на месте:

```bash
mnemos integration verify
```

Проверяет:

- Все файлы инструкций присутствуют с валидным frontmatter (`applyTo: '**'`).
- Все файлы скиллов присутствуют с `name:` и `description:`.
- Файл промпт-режима присутствует с `mode:` и `tools:`.
- Версионный штамп `<!-- mnemos-integration: v2.0.0 -->` в каждом файле.
- Нет ссылок на `ai-brain` (кроме комментария "adapted from" в промпте).

Код выхода `0` = все проверки пройдены. Ненулевой = файлы отсутствуют или
невалидны.

---

## Обновление

Когда новая версия Mnemos поставляет обновлённый контент интеграции:

```bash
mnemos integration update
```

Обновляет только изменённые файлы. Сохраняет локальные настройки (файлы, не
управляемые Mnemos, не трогаются). После обновления запустите
`mnemos integration verify`.

---

## Удаление

Чтобы удалить все файлы интеграции Mnemos:

```bash
mnemos integration uninstall
```

Удаляет только файлы, развёрнутые `mnemos integration setup`. Локальные настройки
сохраняются. **Это деструктивная операция** — она удаляет файлы. Подтвердите
при запросе.

---

## Подключение MCP-инструментов к агентам

Развёртывание инструкций и скиллов говорит агентам *когда* вызывать
инструменты памяти. **Подключение MCP-инструментов к агентам** (agent MCP
wiring) идёт дальше: добавляет `mnemos/*` во фронтматтер `tools:` файлов
Copilot-агентов (`~/.copilot/agents/*.agent.md`), чтобы инструменты реально
выдавались агенту при запросе.

Без wiring у агента могут быть поведенческие инструкции, но не быть
инструментов `mnemos_*` во фронтматтере — харнес не передаст их модели.
Wiring закрывает этот разрыв.

### Что он делает

- Сканирует `~/.copilot/agents/` на наличие файлов `*.agent.md`.
- Разбирает YAML-фронтматтер и добавляет `mnemos/*` (wildcard) или
  индивидуальные ссылки `mnemos/mnemos_*` в массив `tools:`.
- **Меняется только `tools:`** — `model:`, `model_tier:`, `agents:` и
  другие ключи никогда не затрагиваются.
- Идемпотентно — повторный запуск не дублирует записи `mnemos/*`.

### Что пропускается

| Условие | Причина |
|---------|---------|
| У агента уже есть `mnemos/*` или `mnemos/mnemos_*` в `tools:` | Уже подключён — изменений не требуется. |
| Агент использует `tool_profile:` вместо `tools:` | Разрешается Copilot-инсталлером (`make install-all`); изменение будет перезаписано при следующей установке. |
| У агента нет разбираемого фронтматтера | Нельзя безопасно редактировать — помечается как пропущенный. |

### Использование

`mnemos integration setup` подключает агентов в том же проходе, что и
развёртывание файлов и регистрацию MCP. Флаги wiring управляют поведением:

```bash
# Подключить все неподключённые агенты (без промпта)
mnemos integration setup --wire-agents --all

# Подключить конкретных агентов по имени или стеблю файла
mnemos integration setup --wire-agents --select tech-lead,code-reviewer

# Пропустить wiring агентов полностью (без промпта)
mnemos integration setup --no-wire-agents

# Предпросмотр изменений без модификации файлов
mnemos integration setup --wire-agents --dry-run
```

Если не передан ни `--wire-agents`, ни `--no-wire-agents`, команда
спрашивает интерактивно (тот же паттерн, что у промпта регистрации MCP).
В неинтерактивном терминале (CI / pipe) по умолчанию подключаются все
неподключённые агенты.

| Флаг | Описание |
|------|----------|
| `--wire-agents` | Включить wiring агентов (интерактивный промпт по умолчанию) |
| `--wire-agents --all` | Подключить все неподключённые агенты без промпта |
| `--wire-agents --select name1,name2` | Подключить только указанных агентов (совпадение по `name`, стеблю или имени файла) |
| `--no-wire-agents` | Пропустить wiring агентов полностью (явный opt-out) |
| `--precise` | Использовать индивидуальные имена `mnemos/mnemos_*` вместо wildcard `mnemos/*` |
| `--dry-run` | Показать изменения без модификации файлов |

### Wildcard vs precise mode

- **Wildcard** (по умолчанию): добавляет одну запись `mnemos/*`, выдающую
  все mnemos-инструменты. Компактный фронтматтер, выдаёт всё.
- **Precise** (`--precise`): добавляет индивидуальные записи
  `mnemos/mnemos_*` (add, search, recall_context, agent_recall,
  save_context, list_recent, list_tags, ingest_url, stats,
  auto_collect_status). Явный список выдачи — админ-инструменты
  `watch_*` намеренно исключены.

Используйте precise mode, когда нужен детальный контроль над тем, какие
инструменты получает каждый агент. Используйте wildcard mode для удобства,
когда все агенты должны иметь полный набор инструментов mnemos.

### Проверка wiring

После wiring проверьте состояние:

```bash
mnemos integration verify
```

Секция агентов в отчёте verify показывает:

- **Wired** — агенты с `mnemos/*` или `mnemos/mnemos_*` в `tools:`.
- **Unwired** — агенты без mnemos-инструментов (кандидаты на wiring).
- **Skipped** — агенты с `tool_profile:` (управляются Copilot-инсталлером).

`mnemos doctor` также включает проверку wiring агентов (9-я проверка),
которая выводит ту же сводку и предупреждает, если обнаружены
неподключённые агенты.

---

## Контекстный фильтр

Контекстный фильтр — пятиступенчатый конвейер (dedup, noise, extract,
compress, tokens), который очищает сырой контент от шума до того, как он
попадёт к модели. Запускается автоматически при каждом `mnemos_add`, когда
`auto_filter: true` (по умолчанию для новых установок).

Ключевые поверхности:

- **Автофильтр при приёме** — `mnemos_add` сохраняет `raw_content` +
  `clean_content` + `filter_stats`. Поиск и recall возвращают
  `clean_content`, если он есть.
- **MCP-инструмент `mnemos_filter`** — явная перефильтрация существующей
  записи (переопределение профиля, задание бюджета токенов).
- **CLI `mnemos filter`** — `mnemos filter <id>` для одной записи,
  `mnemos filter --all` для бэкфилла нефильтрованных записей.
- **Метрики фильтра в `mnemos stats`** — счётчики filtered/unfiltered,
  среднее сокращение, разбивка по профилям.
- **Профили** — `log | terminal | code | docs | web | default`,
  автоопределяются по эвристикам содержимого.

Полное руководство с деталями стадий, таблицей профилей, примерами и
конфигурацией — в [context-filter.md](context-filter.md).

---

## Хуки и SDK для автоматизации

В mnemos есть две выделенные поверхности для интеграции харнессов и
автоматизации (ADR-0017 D1 / ADR-0018, mnemos #125 Wave 3):

- **Хуки жизненного цикла** — групповой MCP-инструмент `mnemos_hooks` и
  REST-близнец `POST /hooks/{action}` с тремя действиями: `pre_llm_call`
  (собрать контекстный блок для инъекции перед вызовом модели — передайте
  `context_hint` = о чём вызов, и опционально `task` = «голый» slug задачи,
  чтобы сузить сборку до записей одной задачи, ADR-0027 Фаза 0),
  `on_session_start` (вспомнить недавние
  чекпоинты) и `post_tool_call` (автосжатие: при `hooks.auto_compress: true`
  в конфиге — или точечном `auto_compress: true` — вывод инструмента сжимается
  через CCR и возвращается `compressed_text` с маркером в голове для
  подстановки в ваше окно). Идентичность (`session`/`project`/`agent`)
  обязательна на каждом вызове хука. Полный справочник:
  [mcp-tools.md → `mnemos_hooks`](mcp-tools.md#mnemos_hooks)
  / [http-api.md → Хуки жизненного цикла](http-api.md).
- **`MnemosSDK`** (`from mnemos.sdk import MnemosSDK`) — тонкая типизированная
  Python-обёртка над `MemoryManager` для in-process адаптеров:
  `remember` / `recall` / `forget` / `stats` / `assemble_context` /
  `rewrite`. Доменная логика живёт в путях менеджера (те же сканы, гейты и
  идемпотентность, что у поверхностей MCP/REST); две обязанности границы
  канала — собственные у фасада, как у всякого другого канала выдачи:
  `recall` сканирует каждый эхо-элемент на выдаче (контент + заголовок,
  по-элементные редакции, отбрасывание в refuse-режиме), а `remember`
  валидирует теги вызывающего по контракту тегов до любой записи.
  Local-first: `MnemosSDK(settings)` строит свой менеджер,
  `MnemosSDK(manager=…)` переиспользует ваш.

Полная документация адаптеров для интеграторов харнессов — раздел
[Hermes Agent ниже](#hermes-agent): эталонная миграция на контракт.

---

## `mnemos integration setup` — поток по умолчанию

По умолчанию `mnemos integration setup` теперь **запрашивает подключение
агентов** в том же проходе, что и развёртывание файлов и регистрацию
MCP. Это закрывает пробел, когда инструкции развёрнуты, но у агентов
нет `mnemos/*` в фронтматтере `tools:`.

```bash
mnemos integration setup
# → Развёртывает инструкции + скиллы + промпты
# → Регистрирует MCP-сервер
# → Запрашивает: "Wire mnemos/* into Copilot agents? [Y/n]"
```

| Флаг | Поведение |
|------|-----------|
| (нет, интерактивно) | Запрашивает подключение агентов (по умолчанию) |
| `--wire-agents --all` | Подключить всех неподключённых агентов без запроса |
| `--wire-agents --select name1,name2` | Подключить только указанных агентов |
| `--no-wire-agents` | Пропустить подключение агентов |
| `--precise` | Использовать индивидуальные имена `mnemos/mnemos_*` вместо wildcard |
| `--dry-run` | Предпросмотр без изменения файлов |

В неинтерактивном терминале (CI / pipe) команда по умолчанию подключает
всех неподключённых агентов. Полный справочник флагов — в разделе
[Подключение MCP-инструментов к агентам](#подключение-mcp-инструментов-к-агентам)
выше.

---

## `mnemos add --dry-run` — предпросмотр фильтра

Предпросмотр того, как контекстный фильтр преобразует контент **перед
сохранением**. Валидирует контракт тегов, запускает пятиступенчатый
фильтр-пайплайн и выводит статистику — без записи в хранилище.

```bash
mnemos add "long log output..." --tags "project:mnemos,agent:tech-lead,mnemos:trace" --dry-run
```

Вывод:

```text
[dry-run] Filter preview (no memory saved):
  Input:     320 tokens
  Output:    180 tokens (43.8% reduction)
  Profile:   log (auto-detected)
  Dedup:     2 exact, 0 near-duplicates removed
  Noise:     14 lines cleaned
  Budget:    not set (no truncation)
[dry-run] Memory would be saved with these filter stats.
```

| Поле | Значение |
|------|----------|
| Input / Output | Оценка токенов до и после фильтрации |
| Profile | Автоопределённый профиль контента (`log`, `terminal`, `code`, `docs`, `web`, `default`) |
| Dedup | Точные и почти-дубликаты строк удалены |
| Noise | ANSI-коды, прогресс-бары, временные метки, разделители удалены |
| Budget | Токен-бюджет если задан (усечение); `not set` — без усечения |

> `--dry-run` не поддерживается с `--url` (контент загружается при
> ингесте). Используйте с позиционным контентом или `--file`.

---

## `mnemos doctor --fix` — автоисправление предупреждений

`mnemos doctor` запускает проверки здоровья и сообщает статус. С `--fix`
он **автоматически исправляет WARN-уровневые проверки** — ручное
вмешательство не нужно для типовых случаев.

```bash
mnemos doctor          # только отчёт
mnemos doctor --fix    # исправить предупреждения, затем перепроверить
mnemos doctor --fix --dry-run   # предпросмотр исправлений
```

| Предупреждение | Действие автоисправления |
|----------------|--------------------------|
| Integration stale | `mnemos integration update` — обновить устаревшие файлы до текущей версии |
| Agent wiring — неподключённые агенты | `mnemos integration setup --wire-agents --all` |
| MCP server не зарегистрирован | Регистрация MCP через `mcp-setup.sh` |

**FAIL-уровневые проверки не автоисправимы** — они требуют ручной
диагностики (отсутствует конфиг, сломана SQLite-БД, отсутствует vault).
После исправлений `doctor` перепроверяет затронутые проверки и сообщает
новый статус.

Коды выхода: `0` = все прошли, `1` = одна или более провалены, `2` =
только предупреждения.

---

## `mnemos logs` — трассы пайплайна

Просмотр журнала трасс пайплайна (таблица `traces`) прямо из CLI.
Показывает шаги cluster, synthesize, publish и recall с задержкой,
LLM-флагами, кэшем и fallback.

```bash
mnemos logs                       # последние 50 трасс
mnemos logs --task cluster        # только cluster-трассы
mnemos logs --project mnemos      # фильтр по проекту
mnemos logs --limit 100           # больше строк
mnemos logs --since 2026-06-01    # только трассы после этой даты
mnemos logs --follow              # опрос новых трасс (tail -f)
```

| Флаг | Описание |
|------|----------|
| `--task`, `-t` | Фильтр по метке задачи (`cluster`, `synthesize`, `publish`, `recall`) |
| `--project`, `-p` | Фильтр по проекту |
| `--limit`, `-l` | Максимум трасс (по умолчанию 50) |
| `--since` | Только трассы после этой ISO-даты |
| `--follow`, `-f` | Опрос новых трасс (в стиле tail -f) |
| `--config`, `-c` | Путь к config.yaml |

Колонки таблицы: Timestamp, Task, Project, Step, Item, Latency, LLM
(вызван?), Cache (попадание?), Fallback (использован?). Трассы — журнал
аудита конвейера знаний — см. [контекстный фильтр](context-filter.md) и
[обзор архитектуры](../architecture/overview.md) для стадий пайплайна.

---

## Как агенты обнаруживают инструменты

Слой интеграции предполагает, что MCP-сервер Mnemos уже подключён. Инструменты
(`mnemos_*`) появляются в списке инструментов агента после регистрации
MCP-сервера в конфигурации клиента. Подключение MCP-инструментов к агентам
(выше) гарантирует, что фронтматтер `tools:` реально выдаёт эти инструменты
каждому агенту.

Для VS Code Copilot Chat см. [getting-started.md](getting-started.md#подключите-ваш-харнес-mcp)
по настройке MCP-сервера. После подключения инструкции и скиллы из этого пакета
говорят агенту *когда* и *как* вызывать эти инструменты.

---

## Однострочные MCP-пресеты

Для харнессов без нативной цели развёртывания Mnemos поставляет готовые
однострочные MCP-пресеты — всё подключение это одна строка (или один блок)
на харнесс, всегда один и тот же stdio-провод (ADR-0017 D1):
`command "mnemos", args ["mcp-server"]`.

| Харнесс | Конфиг | Пресет |
|---------|--------|--------|
| Cursor | `~/.cursor/mcp.json` | `"mnemos": { "type": "stdio", "command": "mnemos", "args": ["mcp-server"] }` |
| Claude Code | `claude mcp add` | `claude mcp add --scope user mnemos -- mnemos mcp-server` |
| Codex | `~/.codex/config.toml` | TOML-блок `[mcp_servers.mnemos]` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | та же JSON-строка, что для Cursor |
| OpenCode | `~/.config/opencode/opencode.json` | `"mnemos": { "type": "local", "command": ["mnemos", "mcp-server"] }` внутри `mcp` |
| VS Code Copilot | `mcp.json` уровня user/workspace | `mcp-setup.sh` или JSON-блок `servers` |
| ZCode / инструменты `~/.agents` | `mnemos integration setup --target zcode` / `--target agents` | скриптово, аддитивное слияние |

Для OpenCode файл можно создать целиком одной shell-строкой (перезапишет существующий
конфиг; иначе вставьте строку из таблицы в объект `mcp` — обратите внимание на тип
`"local"` и команду-**массив**, это диалект самого OpenCode):

```bash
mkdir -p ~/.config/opencode && echo '{"$schema":"https://opencode.ai/config.json","mcp":{"mnemos":{"type":"local","command":["mnemos","mcp-server"]}}}' > ~/.config/opencode/opencode.json
```

Полные строки для копирования (плюс shell-однострочники для чистой установки
и настройку env): [`integrations/mcp-presets.md`](../../../integrations/mcp-presets.md).
Переменные окружения не нужны — сервер по умолчанию использует
`~/.mnemos/{data,vault}` и создаёт обе директории при первом запуске.

## Шаблон адаптера

Для любого харнесса, не покрытого нативной целью или пресетом, скопируйте
опубликованный шаблон адаптера —
[`integrations/adapter-template.md`](../../../integrations/adapter-template.md):
три секции (**Connect** — MCP-провод → **Expose** — инструменты `mnemos_*` →
**Configure** — слаги project/agent и контракт тегов) плюс чеклист приёмки,
который сам шаблон проходит. Если ваш харнесс говорит по MCP stdio — шаблон
и есть вся интеграция.

---

## Контракт тегов

Каждый вызов `mnemos_add` и `mnemos_ingest_url` должен содержать:

- **ровно один** `project:<slug>`
- **ровно один** `agent:<slug>` (или `agent:user`)
- **минимум один** `mnemos:<subtype>`

Полная схема — в [tag-contract.md](tag-contract.md). Слой интеграции
подкрепляет это в трёх местах: инструкция `mnemos-tag-contract`, скилл
`mnemos-tag-contract` и промпт-режим `mnemos-memory`.

---

## Hermes Agent

Mnemos предоставляет нативный плагин `MemoryProvider` для [Hermes Agent](https://hermes-agent.nousresearch.com/) от Nous Research. После миграции на контракт провайдера ADR-0017 D1 (#125 W5) плагин работает **in-process на контракте**: каждая операция с памятью идёт через `mnemos.adapters.hermes.HermesMemoryAdapter` — фасад `MnemosSDK` плюс хуки жизненного цикла (`pre_llm_call` / `on_session_start` / `post_tool_call`) — вниз к одному `MemoryManager`. Легаси-путь с самодельным HTTP (urllib-клиент, TOTP-логин, circuit breaker, обходной auto-publish) удалён.

### Установка

1. Сделайте пакет `mnemos` импортируемым в Python-окружении Hermes:
   ```bash
   pip install mnemos-memory-server
   ```
   Отдельный процесс `mnemos serve` больше не нужен.

2. Разверните интеграцию:
   ```bash
   mnemos integration setup --target hermes
   ```
   Это копирует плагин в `~/.hermes/plugins/mnemos/` и развёртывает скиллы/инструкции в `~/.hermes/skills/`.

3. Активируйте через мастер:
   ```bash
   hermes memory setup
   ```
   Выберите "mnemos" из списка провайдеров и настройте slug'и project/agent и пути к хранилищу.

4. Перезапустите сессию Hermes (`/restart` в гейтвее или перезапуск CLI).

> **Один владелец на хранилище:** плагин встраивает сервер памяти — указывайте `data_dir`/`vault_path`, на которые больше никто не пишет (SQLite single-writer). Чтобы разделить память с `mnemos serve` или другими харнессами, выделяйте каждому свой data dir.

### Инструменты

Плагин экспонирует инструменты `mnemos_*` как нативные инструменты Hermes — теперь поверх контрактных глаголов (`MnemosSDK.remember` / `recall`, хуки) вместо сырого HTTP. `mnemos_align_prefix` (P1-5 CacheAligner) остаётся **MCP-only** — выравнивание применяется внутри пайплайна сборки, отдельного глагола менеджера нет.

| Инструмент | Поверхность контракта |
|------------|----------------------|
| `mnemos_search` | `MnemosSDK.recall` (скан выдачи) |
| `mnemos_add` | `MnemosSDK.remember` (контракт тегов на канале) |
| `mnemos_recall_context` | recall чекпоинтов + скан канала |
| `mnemos_save_context` | `MnemosSDK.remember` (`mnemos:checkpoint`) |
| `mnemos_agent_recall` | агентский recall + скан канала |
| `mnemos_list_recent` | `MemoryManager.list_recent` (скан только заголовков) |
| `mnemos_list_tags` | `MemoryManager.list_tags` |
| `mnemos_stats` | `MnemosSDK.stats` (срез проекта) |
| `mnemos_auto_collect_status` | in-process счётчик вызовов (та же форма) |
| `mnemos_ingest_url` | `MemoryManager.ingest_url` |
| `mnemos_compress` | хук `post_tool_call` (идентичность N2) |
| `mnemos_retrieve` | `MemoryManager.retrieve_content` (agent+session) |
| `mnemos_watch_start` | `MemoryManager.watch_start` |
| `mnemos_watch_stop` | `MemoryManager.watch_stop` |
| `mnemos_watch_status` | `MemoryManager.watch_status` |

### Конфигурация

Конфиг хранится в `~/.hermes/config.yaml` в секции `memory.mnemos`:

| Ключ | По умолчанию | Описание |
|------|--------------|----------|
| `data_dir` | (пусто) | Каталог данных Mnemos (пусто = значение по умолчанию) |
| `vault_path` | (пусто) | Путь к vault Obsidian (пусто = по умолчанию) |
| `project` | `hermes` | Slug проекта по умолчанию для контракта тегов |
| `agent` | `hermes-default` | Slug агента по умолчанию для контракта тегов |
| `auto_sync` | `true` | Зеркалировать встроенные записи памяти и синхронизировать значимые ходы |
| `publish_on_write` | `true` | Сразу публиковать записи (постура без LLM; `false` — когда работает пайплайн знаний) |
| `sync_interval` | `10` | Синхронизация каждые N ходов |
| `sync_min_user_chars` | `50` | Порог значимости: символов в сообщении пользователя |

**Breaking относительно легаси-плагина:** ключи `base_url` / `api_key` / `totp_secret` удалены — плагин встраивает сервер в процесс (loopback по построению, ADR-0017 D6; аутентификационного хопа больше нет).

### Архитектура

Плагин реализует ABC `MemoryProvider` Hermes как тонкий шим над `HermesMemoryAdapter`:

- **prefetch()** — хук `pre_llm_call` → `assemble_context` (recall → фильтр → скан секретов → align → бюджет, провенанс на каждом блоке), вне цикла хода
- **sync_turn()** — `MnemosSDK.remember` (`mnemos:session`) для значимых ходов (пользователь > 50 символов или каждый N-й)
- **on_memory_write()** — зеркало записей MEMORY.md/USER.md через `MnemosSDK.remember` (`mnemos:learning` / `mnemos:rule`)
- **on_session_end()** — один итог `mnemos:session` на сессию через `remember`
- **on_pre_compress()** — мост ADR-0018: отбрасываемый блок репортится через `MnemosSDK.rewrite` (`on_context_rewrite`), оригинал попадает в LTM без потерь
- **Идентичность** — `project`+`agent` фиксируются при construction (с валидацией контракта тегов заранее), `session` привязывается на сессию Hermes и прошивается в каждый глагол (включая A2-гейт CCR-эмитента и мандат N2 на сжатие)

Приёмка адаптера закреплена in-process тестом `tests/test_hermes_adapter.py` (гейт фазы 1 ADR-0017 — «Hermes e2e на контракте»).

---

## Версионирование

Каждый файл в слое интеграции несёт версионный штамп:

```html
<!-- mnemos-integration: v2.0.0 -->
```

Это позволяет `mnemos integration verify` обнаруживать устаревшие файлы после
обновления. Если штамп не совпадает с установленной версией Mnemos, файл
помечается к обновлению.
