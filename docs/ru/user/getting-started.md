# Начало работы

**🌐 Language / Язык:** [English](../../en/user/getting-started.md) · Русский

> Полное руководство первого запуска Vesma — от установки одной командой до
> первой записи, первого поиска и подключённого агентского харнеса.

Vesma опубликован на PyPI — без клонирования, сборки и знания venv. Эта страница
проводит вас через весь первый запуск. Каждая команда выполнима на чистой Linux /
macOS / WSL2-машине.

Для общего контекста см. [обзор архитектуры](../architecture/overview.md). Справочник
по всем подкомандам CLI — [cli-reference.md](cli-reference.md). По каждому
MCP-инструменту — [mcp-tools.md](mcp-tools.md). По каждому HTTP-эндпоинту —
[http-api.md](http-api.md).

---

## Установка

Vesma опубликован на PyPI пакетом **`vesma`** (голый слот — наш, со времён ребрендинга). Выберите строку под ваш сценарий:

| Вы хотите… | Команда | Что получите |
|-----------|---------|--------------|
| **Всё сразу** — обычный случай: сервер плюс MCP-поверхность, с которой разговаривает харнес | `pip install vesma` | сервер + CLI `vesma` + REST API + MCP-сервер |
| Команда `vesma` в `PATH`, проектные окружения не тронуты | `uv tool install vesma` — или `pipx install vesma` | то же самое, изолированно |
| Плюс внешнее LLM-дообогащение | `pip install "vesma[ollama]"` — также `openai`, `anthropic`, `gemini` | + SDK выбранного провайдера |

> **Один пакет, без экстры.** Начиная с 4.1.0 MCP SDK — основная зависимость (ADR-0023):
> базовая установка обслуживает агентские харнесы из коробки, а легасная экстра `[mcp]`
> осталась пустым no-op-алиасом, чтобы старые команды и сниппеты продолжали работать.
> Модель эмбеддингов `vesma-embed-v1` (~30 МБ) встроена в wheel: поиск работает полностью
> офлайн, на CPU, без загрузок и без API-ключей.

> ⚠️ **Имена.** Продукт и CLI — `vesma` (`pip install vesma`). Доребрендинговый пакет
> `mnemos-memory-server` живёт до deprecation и ставит тот же сервер
> (`pip install "mnemos-memory-server[ollama]"` работает весь двойной период). Голый
> `pip install mnemos` — посторонний сторонний проект, не используйте его.

### Скриптовый вариант (без решений)

Установщик создаёт изолированный venv в `~/.mnemos/venv`, кладёт лаунчер `vesma`
в `~/.local/bin` и в том же запуске предлагает настроить VS Code MCP и развернуть
integration-пак:

```bash
curl -fsSL https://raw.githubusercontent.com/vesmaro/vesmaro/main/scripts/install.sh | bash
```

### Фиксация версии и другие каналы

| Метод | Команда |
|-------|---------|
| Зафиксировать версию | `pip install vesma==4.3.0` (*пин до ребрендинга: `mnemos-memory-server==4.1.0` ставится до deprecation*) |
| Контейнер одной командой | `… install.sh \| bash -s -- --container` — см. [container-deployment.md](../admin/runbooks/container-deployment.md) |
| Из исходников (контрибьюторам) | `git clone https://github.com/vesmaro/vesma && cd vesma && uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"` — см. [CONTRIBUTING.ru.md](../../../CONTRIBUTING.ru.md) |

<details>
<summary><strong>Готовый wheel и готовый контейнерный образ</strong> — каналы с фиксированной версией</summary>

**Готовый wheel** (зафиксировать конкретную версию):

<!-- version:pip -->
```bash
pip install https://github.com/vesmaro/vesma/releases/download/v4.3.0/mnemos_memory_server-4.3.0-py3-none-any.whl
```
<!-- /version:pip -->

<!-- deprecated-note: релизы 5.x идут wheel'ом `vesma` (голый слот PyPI); имя артефакта
mnemos_memory_server-*.whl покрывает линейку 4.x до deprecation. -->

**Готовый образ** (публикуется в `ghcr.io/vesmaro/vesmaro`; работает и `docker` — замените `podman` на `docker`):

```bash
export VESMARO_API__TOTP_MASTER_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
# образы 4.x дополнительно принимают легаси-написание MNEMOS_API__TOTP_MASTER_KEY (deprecated)
podman run -d --name vesma \
  -p 8787:8787 \
  -v vesma-data:/data \
  -v vesma-vault:/vault \
  -e VESMARO_API__TOTP_MASTER_KEY="${VESMARO_API__TOTP_MASTER_KEY}" \
<!-- version:image -->
  ghcr.io/vesmaro/vesmaro:4.3.0
<!-- /version:image -->

curl -s http://localhost:8787/health | jq
```

<!-- version:tags -->
Теги: `:4.3.0` (фиксированная) · `:latest` (rolling).
<!-- /version:tags -->

Полное руководство: [container-deployment.md](../admin/runbooks/container-deployment.md).

</details>

<details>
<summary><strong>Опциональные экстры</strong> — внешние LLM-провайдеры, только если нужны</summary>

Vesma вызывает внешние LLM для синтеза в конвейере (M4) и дообработки — никогда
для хранения или поиска. Устанавливайте только нужное:

```bash
uv pip install "vesma[ollama]"      # локальный Ollama (провайдер по умолчанию)
uv pip install "vesma[openai]"      # OpenAI / Azure OpenAI
uv pip install "vesma[anthropic]"   # Anthropic Claude
uv pip install "vesma[gemini]"      # Google Gemini
```

Провайдер по умолчанию — `ollama`, указывающий на `http://localhost:11434`.
Полную матрицу провайдеров см. в [config.example.yaml](../../../config.example.yaml).

</details>

### Предварительные требования

| Инструмент | Версия | Зачем |
|------------|--------|-------|
| Python | ≥ 3.11 | Минимальная среда выполнения (решается через `pip` — ручной venv не нужен) |
| `uv` или `pipx` | последняя | Опционально, для изолированной tool-установки |

> **Замечание об ОС.** Vesma разрабатывается на Linux (Arch, Fedora, Ubuntu 22.04+)
> и регулярно проходит smoke-тест на macOS. Windows работает через WSL2. Юнит systemd
> в `contrib/systemd/` — только для Linux.

> **Железо.** Встроенная `vesma-embed-v1` комфортно работает на одном ядре CPU.
> GPU не требуется. VM с 2 vCPU / 2 ГБ ОЗУ достаточно для личного использования.

---

## Первая запись (CLI)

```bash
vesma add "Hello world" --tags project:test agent:getting-started mnemos:learning
```

Ожидаемый вывод:

```text
✓ Saved: Hello world (550e8400-e29b-41d4-a716-446655440000)
```

Vesma автоматически:

1. **Записал запись в SQLite** по пути `~/.mnemos/data/mnemos.db` (создаётся при первом запуске).
2. **Отразил её в Obsidian-vault** `~/.mnemos/vault/` как markdown-файл с YAML-фронтматтером.
3. **Проверил контракт тегов** — `project:test` + `agent:getting-started` + `mnemos:learning` —
   корректная тройка. Пропустите один из тегов, и вместо подтверждения получите
   `❌ Tag contract violation: ...`.

Контракт тегов описан в [tag-contract.md](tag-contract.md). Коротко: каждая запись требует
**ровно одного** `project:<slug>`, **ровно одного** `agent:<slug>` и **хотя бы одного**
`vesma:<subtype>` (например, `mnemos:learning`, `mnemos:bug-pattern`, `mnemos:decision`).

> **Замечание.** Только что добавленные записи получают статус `raw`. Фоновый процессор
> (работает в режимах MCP и HTTP API) автоматически кластеризует, синтезирует, проверяет
> качество и публикует их. Индекс векторного поиска включает только записи в статусе
> `published`. Перестроить его вручную: `vesma reindex` (CLI) или `POST /reindex` (HTTP API).

---

## Первый поиск

Гибридный поиск объединяет полнотекстовый FTS5 SQLite с векторным сходством
и сливает ранжирования через Reciprocal Rank Fusion (RRF):

```bash
vesma search "hello"
```

Полезные флаги:

| Флаг | Действие |
|------|---------|
| `--limit N` / `-l N` | Максимум результатов (по умолчанию 10) |
| `--project P` / `-p P` | Ограничить slug'ом проекта |

Для программного доступа с расширенными опциями (вес вектора, сырой контент, фильтр
по тегам) используйте HTTP API — см. [http-api.md#search](http-api.md#post-search--гибридный-поиск).

---

## Ваша кодовая база может стать памятью (граф проектов)

Кроме сессий, Vesma умеет индексировать **структуру кода** проекта: схемы
файлов, поиск по символам, трассировку вызовов, сниппеты со сканом на
секреты — без единого байта исходника в хранилище. Это
[граф проектов](project-graph.md), включённый по умолчанию, — и с волны
PG-0.5 **ваш проект индексируется сам**: первый MCP-вызов (или хук
`pre_llm_call`), который агент делает внутри каталога с packaging-манифестом
(`pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`, `setup.py`),
авторегистрирует и индексирует его в фоне. Ни явного вызова, ни инструкций,
ни скиллов.

Что происходит, по шагам:

1. Работайте в проекте как обычно — агент вызывает там любой MCP-инструмент.
2. Первый индекс идёт в фоне (`auto-first`); дальше строка-маячок в выводе
   `assemble_context` сообщает свежесть графа.
3. Проверьте: `mnemos_project_graph_status` — объёмы, свежесть,
   poisoned-файлы. (Ему нужен `project_id` проекта — см.
   `mnemos_list_graph_projects`.)

Предпочитаете явный путь? Зарегистрируйте корень вручную
(`mgr.sqlite.save_project(Project(name="myproj", paths=["/abs/path/to/myproj"]))`)
и вызовите `mnemos_index_project` с `project_id` и `agent` — ручной поток
доступен всегда, а успешный ручной индекс к тому же снимает приостановку
авто-пути.

Не хотите этого? Два выключателя в `config.yaml`: `code_graph.auto_index:
false` останавливает только фоновый авто-путь (ручные инструменты
работают); `code_graph.enabled: false` выключает всю поверхность — каждый
вызов графа отвечает `code: "disabled"`. Полный гид:
[project-graph.md](project-graph.md).

---

## Подключите ваш харнес (MCP)

MCP-сервер — основная поверхность интеграции: ваш агентский харнес порождает
`vesma mcp-server` по stdio и получает полный набор инструментов `vesma_*`.
Выберите свой харнес:

| Харнесс | Самый быстрый путь |
|---------|--------------------|
| VS Code Copilot | `curl -fsSL …/scripts/mcp-setup.sh \| bash`, затем перезагрузить окно |
| Claude Code | `claude mcp add --scope user vesma -- vesma mcp-server` |
| Cursor | вставить одну строку в `~/.cursor/mcp.json` |
| OpenCode | вставить один блок в `~/.config/opencode/opencode.json` |
| Codex / Windsurf | по одному TOML / JSON блоку |
| ZCode, pi, Hermes Agent | `vesma integration setup --target zcode` / `--target pi` / `--target hermes` |
| Всё остальное | [adapter-template.md](../../../integrations/adapter-template.md) |

**Полные инструкции для копирования для каждого харнесса собраны на одной странице:
[Подключите Vesma к любому харнесу](../../../integrations/mcp-presets.md).** Поведенческий
слой — инструкции, скиллы и режим промпта, из-за которых агенты реально *пользуются*
памятью, — отдельный шаг в один проход:

```bash
vesma integration setup
```

Таргеты и флаги — в [руководстве по интеграции](integration-guide.md).

Ручная справка для VS Code — `mcp.json` уровня user или workspace:

```jsonc
{
  "servers": {
    "vesma": {
      "type": "stdio",
      "command": "vesma",
      "args": ["mcp-server"]
    }
  }
}
```

> **Примечание о ключе реестра.** Ключ `"vesma"` в MCP-конфигах — это *регистрационное имя*
> сервера, которое integration-слой читает и ведёт (`servers["vesma"]`); ребрендингом оно не
> тронуто. *Команда* — `vesma mcp-server`.

> **Подсказка — режим автосбора.** Установите `VESMARO_AUTO_COLLECT=1` (легаси-написание:
> `MNEMOS_AUTO_COLLECT`, устарело) в блоке `env`
> сервера, чтобы Vesma предлагал агенту вызывать `mnemos_save_context` каждые ~6
> вызовов инструментов. О компромиссах см. [mcp-tools.md#auto-collect-mode](mcp-tools.md#режим-auto-collect).

---

## Запуск HTTP API (опционально)

Для не-MCP клиентов, дашбордов и A2A-трафика:

```bash
vesma serve --host 127.0.0.1 --port 8787
```

| Эндпоинт | Назначение |
|----------|-----------|
| `http://127.0.0.1:8787/health` | Проверка живости |
| `http://127.0.0.1:8787/metrics` | Статистика (в стиле Prometheus) |
| `http://127.0.0.1:8787/docs` | Swagger UI |
| `http://127.0.0.1:8787/v1/sessions` | A2A sessions API (M16) |

> **Безопасность.** Значение по умолчанию — привязка к `127.0.0.1`. Не выставляйте
> порт наружу без обратного прокси с аутентификацией — см. [security.md](../admin/security.md).

Быстрая проверка:

```bash
curl -s http://127.0.0.1:8787/health | jq
# {"status":"ok"}
```

---

## Проверьте установку

```bash
vesma doctor
```

прогоняет проверки здоровья по хранилищу, конфигу, MCP-транспорту и известным
регистрациям харнесов — и печатает по строке PASS/WARN/FAIL на каждую проверку.
`vesma doctor --fix` автоматически устраняет типовые предупреждения (устаревшие
файлы интеграции, неподключённые агенты, отсутствующая регистрация MCP).

Полный девелоперский гейт (только для контрибьюторов): клонируйте репозиторий,
`uv pip install -e ".[dev,mcp]"`, затем `make verify` — ruff + mypy `--strict` +
bandit + pip-audit + набор тестов. Если `pip-audit` жалуется на закреплённую CVE,
см. [ранбук по обновлению зависимостей](../admin/runbooks/dependency-updates.md).

---

## Обновления

Vesma сообщает о новой версии и обновляется одной командой (issue #445).

**Проверка обновлений — включена по умолчанию, тихая.** Vesma спрашивает у PyPI
«есть ли версия новее?» одним GET версионного манифеста (таймаут 3с, никакой
телеметрии, ничего не отправляется), кэширует ответ на 24 часа в
`<data_dir>/update-check.json` и показывает его в трёх местах:

| Где | Что вы видите |
|-----|---------------|
| `mnemos_stats` (MCP) / `vesma stats` | объект `update_available`: `{installed, latest, dist, update_available, checked_at}` (или `null`) |
| `vesma --version` | строка в stderr: `update available: 5.2.0 (run 'vesma update --check')` |
| Старт сервера (`vesma serve`, `vesma mcp-server`) | одна INFO-строка в логе |

Офлайн-машины не страдают: неудачная проверка отдаёт кэшированный ответ
(с пометкой «stale») и никогда ничего не ломает. Выключить проверку:

```bash
VESMARO_UPDATES_CHECK=off vesma serve      # жёсткий env-выключатель
```

или в `config.yaml` (env-эквивалент: `VESMARO_UPDATES__CHECK_ENABLED=false`):

```yaml
updates:
  check_enabled: false
```

**`vesma update` — одна команда на машину.** Без флагов показывает все поверхности
обновлений, найденные на этой машине: pip-дистрибутив, который она бы обновила
(установленная версия vs последняя), глобальный npm-пакет `@vesmaro/vesma`,
прод-венвы хоста и Go-бинарники:

```bash
vesma update            # или: vesma update --check
vesma update --yes --scope=user    # pip install --user --upgrade <dist>, npm -g best-effort
vesma update --to 5.1.1 --yes      # откат / закрепление на конкретной версии
```

`--yes` трогает только pip user-site (и npm, если установлен) — прод-венвы,
Go-бинарники и контейнеры молча не обновляются никогда, они только в отчёте.
Каждый запуск дописывает запись в
`~/.local/share/vesma/update-history.json`. После обновления перезапустите
агент-харнес / `vesma serve`, чтобы подхватить новую версию.

**Полностью автоматически (опционально).** Еженедельный systemd user-таймер
делает то же самое:

```bash
vesma update --install-timer      # пишет ~/.config/systemd/user/vesma-update.{service,timer}, включает weekly + Persistent
vesma update --uninstall-timer    # снять
```

Шаблоны юнитов лежат в
[`contrib/vesma-update.service`](../../../contrib/vesma-update.service) /
[`.timer`](../../../contrib/vesma-update.timer) — в шапке файлов объяснена
адаптация под distrobox (по одному `distrobox-enter` ExecStart на бокс, тот же
паттерн, что у прод-юнитов) и что никогда не обновляется автоматически.

---

## Миграция с legacy ai-brain

Если у вас есть старая установка `ai-brain` (`~/.ai-brain/ai_brain.db` +
`~/brain-vault/`), Vesma импортирует её одной командой. Сначала dry-run:

```bash
vesma migrate from-ai-brain --dry-run
```

Прочитайте сводку, затем запускайте по-настоящему:

```bash
vesma migrate from-ai-brain
```

Мигратор переводит легаси-типы источников, исправляет контракт тегов
(`project:legacy`, `agent:unknown`, `mnemos:legacy`), сохраняет статусы записей
и переносит колонки `content_ru` / `content_en` в `metadata` (без потери данных).
Для нестандартных расположений используйте `--source PATH` и `--vault PATH`.

---

## Конфигурация

Vesma читает `config.yaml` из текущего каталога или `~/.mnemos/config.yaml`.
Полная схема — в [config.example.yaml](../../../config.example.yaml). Самые полезные ручки:

| Параметр | По умолчанию | Назначение |
|----------|--------------|-----------|
| `mnemos.data_dir` | `~/.mnemos/data` | Хранилище SQLite + векторный индекс |
| `mnemos.vault_path` | `~/.mnemos/vault` | Зеркало Obsidian |
| `mnemos.strict_tag_contract` | `true` | Принуждать контракт тегов (`false` — только для легаси-импортов) |
| `embedding.provider` | `nano` | `nano` (vesma-embed-v1, встроенная) / `onnx` / `ollama` / `sentence-transformers` |
| `search.hybrid_alpha` | `0.5` | Вес векторной ноги в RRF (0.0 = чистый FTS, 1.0 = чистый вектор). Дефолт перенастроен 0.7 → 0.5: баланс ног не даёт доминированию векторной ноги топить FTS-совпадения ранга 1 (issue #300) |
| `api.host` / `api.port` | `127.0.0.1` / `8787` | Значения по умолчанию для `vesma serve` |
| `llm.provider` / `llm.model` | `ollama` / `qwen2.5:3b` | Синтез конвейера и контекстный фильтр |

Любой из них переопределяется переменными окружения (`VESMARO_*`, `__` — разделитель вложенности; написание 4.x `MNEMOS_*` устарело):

```bash
VESMARO_SEARCH__HYBRID_ALPHA=0.7 vesma search "deployment"
```

### Логирование

Vesma пишет логи в `~/.mnemos/logs/mnemos.log` по умолчанию (ротация, 10 МБ × 3 файла):

```yaml
logging:
  level: INFO                    # DEBUG | INFO | WARNING | ERROR
  log_file: ~/.mnemos/logs/mnemos.log
  max_file_size_mb: 10
  backup_count: 3
```

CLI: `vesma --verbose serve` для уровня DEBUG, `vesma serve --log-file /path/to/log`
для переопределения пути.

---

## Устранение неполадок

### Команда `vesma` не найдена

Если ставили обычным `pip` в venv — venv должен быть активирован. Предпочитайте
изолированную установку (`uv tool` / `pipx` / `install.sh`) — она кладёт `vesma`
в `PATH` в каждом шелле (`~/.local/bin`; добавьте каталог в `PATH`, если ваш
дистрибутив этого не делает).

### `vesma mcp-server` падает с ошибкой импорта `mcp`

Установка сломана либо поверх основного SDK лёг чужой `mcp` 1.x:
`pip install --force-reinstall mnemos-memory-server` (SDK — основная зависимость с 4.1.0 —
ADR-0023; после переустановки транспорт подтверждает `vesma doctor`).

### Поиск возвращает только «raw» записи

Векторный индекс включает только записи в статусе `published`; новые записи
стартуют как `raw` и публикуются фоновым процессором. Чтобы опубликовать сразу,
задайте `status: "published"` при создании через HTTP API — или дайте конвейеру
отработать.

### `sqlite3.OperationalError: database is locked`

Другой процесс `vesma` (CLI, MCP или HTTP) держит блокировку записи. SQLite
использует WAL-режим, но писатель в каждый момент один. Закройте другой процесс
или дождитесь коммита его транзакции (таймаут по умолчанию — 5 с). Для
мульти-харнесных установок выдайте каждому харнесу свой data dir — см. замечание
«один владелец на хранилище» в [руководстве по интеграции](integration-guide.md).

### MCP-сервер работает, но инструменты не появляются в харнесе

1. Проверьте, что конфиг харнеса парсится (валидный JSONC / TOML, без висячих запятых).
2. Перезапустите харнес после правки конфига.
3. Проверьте провод напрямую: `printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"probe","version":"0.0.0"}}}\n' | vesma mcp-server` — JSON-RPC-ответ с `"serverInfo":{"name":"vesma"...}` означает, что серверная сторона в порядке. (Имя в serverInfo остаётся `vesma` весь двойной период — часть контракта MCP-регистрации.)
4. Запустите `vesma doctor` — проверки MCP-транспорта и регистраций укажут на сломанное звено.

---

## Куда идти дальше

| Если хотите… | Читайте |
|--------------|---------|
| Подключить конкретный харнес (VS Code, Claude Code, Cursor, OpenCode, Codex, Windsurf, pi, Hermes…) | [Подключите Vesma к любому харнесу](../../../integrations/mcp-presets.md) |
| Развернуть поведенческий пакет (инструкции / скиллы / промпты / wiring агентов) | [integration-guide.md](integration-guide.md) |
| Посмотреть все подкоманды CLI | [cli-reference.md](cli-reference.md) |
| Посмотреть все MCP-инструменты | [mcp-tools.md](mcp-tools.md) |
| Посмотреть все HTTP-эндпоинты | [http-api.md](http-api.md) |
| Понять устройство системы | [обзор архитектуры](../architecture/overview.md) |
| Прочитать схему тегов | [tag-contract.md](tag-contract.md) |
| Выполнить операционную задачу | [admin/runbooks/install.md](../admin/runbooks/install.md) |
| Пересмотреть границы безопасности | [security.md](../admin/security.md) |
| Узнать, почему принято то или иное решение | [project/adr/](../../project/adr/) |

---

_Последнее обновление: 2026-10-01_
