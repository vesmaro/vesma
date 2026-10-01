# Plan: Vesma — standalone memory server (ai-brain fork)

> **Статус**: спецификация для будущей сессии разработки. Реализация ещё не начата.
> Создан вместе с архитектурным документом [ARCHITECTURE.md](ARCHITECTURE.md) и [README.md](README.md). При старте Vesma-сессии: прочитать README → ARCHITECTURE → PLAN, затем приступить к **Phase M1**.
>
> **Locked decisions** (из планирующей сессии):
> - Git: сохраняем full history ai-brain через `git clone` + `remote rename` (новый `origin` для Vesma, старый сохраняем как `upstream-ai-brain` read-only).
> - LLM-провайдеры: широкий набор с самого начала — Anthropic + OpenAI + Azure OpenAI + Ollama + Gemini, через provider abstraction в `mnemos/llm/`.
> - Context Filter (M10): обязательная v1-подсистема (pre-LLM фильтрация шума + дедуп + чистый контекст).
> - Cache Center (M11): отложен в v2; идемпотентность из M5 покрывает основную выгоду.

**TL;DR**: Форкаем пользовательский проект `ai-brain` (`/var/home/abyss/LABs/AI/ai-brain/`) в новый самостоятельный продукт **Vesma** (`/var/home/abyss/LABs/AI/mnemos/`, эта директория). Сохраняем всё лучшее (FastAPI + Typer CLI + MCP-сервер + ChromaDB/SQLite FTS5 + RRF + Obsidian vault + Auto-Collect), и доводим до production-зрелости: Vesma tag contract на уровне MCP-валидатора, first-class per-agent recall, knowledge pipeline (raw→processing→processed→published), automation-first/policy engine, explainability layer, улучшенный compaction-detection, авто-инжест path-scoped rules, **обязательный Context Filter перед отправкой в модель**, аудит bugs/CRs, миграционный CLI и архивирование ai-brain. Cache Center откладываем в v2.

**Локация**: `/var/home/abyss/LABs/AI/mnemos/` (рядом с архивируемым `ai-brain`).
**Лицензия**: наследуем из ai-brain.
**Связь с GCW**: standalone-сервер; в GCW сидит только тонкий плагин `mnemos-integration` (см. `GithubCopilotWorkflow/plugins/mnemos-integration/`).

---

## Phase M1 — Fork & Rebrand

1. `git clone /var/home/abyss/LABs/AI/ai-brain /var/home/abyss/LABs/AI/mnemos-tmp` → переместить содержимое в существующую папку `mnemos/` (которая уже содержит PLAN/ARCHITECTURE/README — сохранить!), либо инициализировать `mnemos/` как новый репозиторий и втянуть историю через `git remote add upstream-ai-brain ../ai-brain && git fetch upstream-ai-brain && git merge --allow-unrelated-histories`. Решение принять в начале сессии. Первый коммит после слияния: "Fork from ai-brain @ <sha>". Сохранить upstream remote read-only для cherry-picks.
2. Переименования (массовая текстовая замена + проверка):
   - Python package `ai_brain` → `mnemos` (через `git mv` + `sed -i`)
   - CLI binary `brain` → `mnemos` (entry-points в `pyproject.toml`)
   - MCP tools: `brain_search` → `mnemos_search`, `brain_add` → `mnemos_add`, `brain_save_context` → `mnemos_save_context`, `brain_recall_context` → `mnemos_recall_context`, `brain_list_recent` → `mnemos_list_recent`, `brain_list_tags` → `mnemos_list_tags`, `brain_ingest_url` → `mnemos_ingest_url`, `brain_watch_start/stop/status` → `mnemos_watch_*`, `brain_auto_collect_status` → `mnemos_auto_collect_status`, `brain_stats` → `mnemos_stats`
   - Env vars: `AI_BRAIN_*` → `MNEMOS_*` (особенно `AI_BRAIN_AUTO_COLLECT`, `AI_BRAIN_DATA_DIR`, `AI_BRAIN_VAULT`)
   - Defaults: vault path `~/brain-vault/` → `~/mnemos-vault/`; data dir `~/.ai-brain/` → `~/.mnemos/`
   - Container artefacts: `Containerfile`, `compose.yaml`, quadlet файлы, systemd units — переименование сервиса `ai-brain` → `mnemos`
   - Docs: README, architecture.md, mcp-integration.md — обновить названия + URL примеры
3. Verification: `uv pip install -e .`; `vesma --help`; `pytest`; запуск MCP-сервера; smoke-тест `mnemos_stats` через MCP-клиент.

**Зависимости**: M1 блокирует все остальные фазы.

---

## Phase M2 — Vesma Tag Contract enforcement at MCP layer

1. Добавить модель `TagContract` в `mnemos/models.py`: schema с обязательными `project:<...>` + `agent:<...>` + ≥1 `mnemos:*`, плюс whitelist префиксов (`severity:`, `stack:`, `applyTo:`, `source:`).
2. Конфиг-флаг `strict_tag_contract: bool` (default `true` для новых установок, `false` для legacy миграций) в `mnemos/config.py`.
3. Валидатор в `mnemos_add` MCP-инструменте: при `strict_tag_contract=true` отказываем с понятным сообщением «missing required tag: project:* / agent:* / mnemos:*». При `false` — warning в логах + автодобавление `mnemos:legacy` + `agent:unknown`.
4. CLI команда `vesma tags validate <vault>` — проверка существующего vault на соответствие контракту, отчёт по non-conformant записям.
5. Документация: новая `docs/tag-contract.md` со схемой + примерами + миграционным гайдом.
6. Тесты: `tests/test_tag_contract.py` — happy-path, missing tags, invalid prefix, strict/lax modes.

**Зависимости**: M1.

---

## Phase M3 — First-class per-agent recall

1. Новый MCP tool `mnemos_agent_recall(agent: str, project: str | None, query: str | None, limit: int = 20)` — фильтрует по тегу `agent:<name>` опционально с проектным scope, опционально с FTS/vector query. Если query пустой — возвращает свежие N записей агента.
2. API endpoint `GET /recall/agent/{name}?project=&q=&limit=` в FastAPI.
3. CLI: `vesma recall --agent cr-security-reviewer --project gcw --limit 10`.
4. Индекс: убедиться что SQLite индекс на тег-таблицу покрывает `(tag_value, project_value)` для быстрого фильтра.
5. Тесты: `tests/test_agent_recall.py` — multi-agent vault, фильтр по агенту, фильтр + project, hybrid search в scope агента.

**Зависимости**: M1. Параллельно с M2.

---

## Phase M4 — Knowledge Pipeline (raw → processing → processed → published)

Источник дизайна: `ai-brain/docs/knowledge-pipeline-concept.md` (v0.4 roadmap).

1. Расширить модель `Memory`: добавить поля `status: Literal["raw","processing","processed","published"]`, `quality_score: float | None`, `confidence: float | None`, `source_coverage: int | None`, `cluster_id: str | None`, `derived_from: list[str]` (ids).
2. **Vector indexing gate**: ChromaDB индекс перестраивается только для `status="published"`. Raw/processing/processed остаются в SQLite, не выходят в vector recall. Это резко повышает signal-to-noise при поиске.
3. **Stages**:
   - `raw`: всё что пришло через `mnemos_add` без явного status — сырая заметка
   - `processing`: помечено воркером-кластеризатором; ассоциировано с `cluster_id`
   - `processed`: прошло LLM-синтез в draft-статью (через `vesma process --cluster <id>`)
   - `published`: прошло quality gates → попало в vector index
4. **Clustering worker**: `vesma cluster` — группирует свежие raw по схожести (embedding similarity threshold, configurable). Записывает `cluster_id`.
5. **Draft synthesis**: `vesma synthesize --cluster <id>` — берёт raw+processing записи кластера, отправляет в LLM (модель из конфига) для генерации article, ставит `status=processed`.
6. **Quality gates**: проверки на `quality_score >= threshold`, `confidence >= threshold`, `source_coverage >= min_sources`. Конфигурируются в `mnemos/config.py`.
7. **Publish**: `vesma publish --id <id>` (или авто, см. M5) — `status=processed→published`, добавляет в vector index.
8. **API**: новые эндпоинты `POST /process`, `POST /synthesize`, `POST /publish`, `GET /memories?status=`.
9. Тесты: state machine transitions, quality gate enforcement, vector index reflects only published, rollback on failure.

**Зависимости**: M1. Параллельно с M2/M3, но M5 зависит от M4.

---

## Phase M5 — Automation-first / Policy engine

1. **Scheduler layer**: периодические задачи (cron-style) — `cluster every 1h`, `synthesize every 6h`, `publish-eligible every 30m`. Реализация: APScheduler или аналог.
2. **Event-trigger layer**: подписка на события vault watcher (debounce + batching). Например: 5 raw записей одного кластера за 10 минут → trigger immediate cluster→synthesize.
3. **Policy engine**: декларативные правила (YAML, `~/.mnemos/policies.yaml`) — например:
   - `if quality_score >= 0.85 and confidence >= 0.8 and source_coverage >= 2 then auto_publish`
   - `if cluster.size < 3 then defer_synthesis`
   - `if record.age > 90d and status=raw then archive`
4. **Reliability layer**:
   - Retry с экспоненциальным backoff
   - Dead letter queue (DLQ) для failed синтезов; CLI `vesma dlq list/retry/discard`
   - Идемпотентность: ключ = `hash(cluster_id, prompt_version, model_version)` — повторный синтез того же кластера тем же промптом возвращает кэшированный результат (это же — v1-замена отложенного Cache Center)
5. **Observability**: метрики Prometheus-style (`mnemos_pipeline_processed_total`, `_failed_total`, `_latency_seconds`); endpoint `/metrics`.
6. **KPI цели** (документируем, мониторим): ≥80% raw→draft автоматически, ≥60% draft→published автоматически.
7. Тесты: policy engine evaluation, scheduler triggers, DLQ behaviour, idempotency.

**Зависимости**: M4.

---

## Phase M6 — Explainability layer

1. Трейс каждого pipeline-шага в SQLite таблицу `traces`:
   - `task_label` (cluster/synthesize/publish/recall)
   - `current_project`, `current_step`, `current_item_id`
   - `llm_called: bool`, `llm_done: bool`, `cache_hit: bool`, `fallback_used: bool`
   - `latency_ms`, `tokens_in`, `tokens_out`, `tokens_per_sec`
   - `rationale_summary: str` (≤200 символов, краткое объяснение решения — НЕ chain-of-thought)
2. API: `GET /traces?item_id=&task=&since=` для просмотра истории.
3. Web UI badges (если есть UI в ai-brain — расширяем; если нет — только API): на каждой записи бейдж «auto-published by policy X», «manually reviewed», «retried 2x», и т.п.
4. **БЕЗОПАСНОСТЬ**: не экспонируем raw LLM reasoning / chain-of-thought. Только краткие rationale-summary, генерируемые отдельным финальным шагом.
5. Тесты: trace recording, API filters, rationale summary truncation.

**Зависимости**: M4 (нужны pipeline-шаги для трейса). Параллельно с M5.

---

## Phase M7 — Better compaction-detection

Текущий Auto-Collect в ai-brain: счётчик 6 вызовов / 480 сек.

1. Дополнительные сигналы:
   - **Context-size heuristic**: если плагин-клиент шлёт estimated context tokens — триггер при >80% от модельного лимита
   - **Summary-marker detection**: парсинг последних сообщений на маркеры VS Code Copilot compaction (`<conversation-summary>`, `<compacted>`, и т.п.)
   - **Missing prior-turn references heuristic**: если в последних N tool-calls агент перестал ссылаться на ранее обсуждённые идентификаторы — флаг
2. Конфиг: `~/.mnemos/auto_collect.yaml` с порогами по каждому сигналу + weights.
3. MCP tool `mnemos_auto_collect_status` расширяется: возвращает не только «next reminder in N calls», но и текущие значения всех сигналов.
4. Тесты: симуляция сценариев compaction-в-реальном-времени.

**Зависимости**: M1. Параллельно с M2-M6.

---

## Phase M8 — Path-scoped rules ingest

1. File-watcher mode для путей `.github/instructions/*.instructions.md` в проектных репах (опционально включается через конфиг или CLI flag `vesma watch --include-rules`).
2. При обнаружении файла — парсим frontmatter (`applyTo:` glob), парсим body как markdown, создаём knowledge unit с:
   - `status=published` (rules — это уже отшлифованные знания)
   - tags: `mnemos:rule`, `project:<repo>`, `applyTo:<glob>`, `source:path-scoped-rule`
   - content: markdown body
3. `mnemos_recall_context` и `mnemos_search` при наличии параметра `current_file_path` бустят rules с matching `applyTo:` glob в топ результатов.
4. При изменении/удалении файла — обновляем/удаляем соответствующий knowledge unit.
5. Тесты: ingest простого файла, multi-glob, удаление, recall с file context.

**Зависимости**: M1, M2 (теги), M3 (для агент-агностик recall).

---

## Phase M9 — ai-brain bugs/CRs audit & fix

1. **Подход**: ручной аудит (наш code-reviewer плагин ещё не существует — chicken-and-egg). Применяем чек-лист GCW code-review (security/architecture/quality/performance/tests).
2. **Известные проблемные места** (без полного аудита — гипотезы для проверки):
   - Возможные SQL-injection в FTS5 запросах (если строки конкатенируются)
   - Гонки в watcher при бурных изменениях
   - Утечки соединений ChromaDB/SQLite
   - Отсутствие rate-limiting на MCP endpoints
   - Логи могут утекать секреты (URL с токенами при ingest)
3. **Процесс**:
   - Прогнать `bandit`, `ruff`, `mypy --strict`, `pip-audit` — фиксировать всё HIGH/CRITICAL
   - Pytest coverage report — добить до ≥80%
   - Manual review каждого MCP-инструмента: input validation, error handling, idempotency
4. **Документировать каждое исправление** в `CHANGELOG.md` с reference на ai-brain код.

**Зависимости**: M1. Может идти параллельно со всеми остальными.

---

## Phase M10 — Context Filter (mandatory v1)

> **Мотивация**: в модель часто уходит сырой шум (необработанные логи, длинные stdout, дубли, служебная разметка). Это раздувает токены и ухудшает качество ответа. Нужен фильтр ДО отправки запроса в модель.

**Ключевой инвариант**: фильтрация не разрушает данные. Храним обе версии: `raw_content` (оригинал) и `clean_content` (проекция для модели). Drill-down до raw всегда доступен.

1. Расширить модель `Memory`:
   - `raw_content: text`
   - `clean_content: text`
   - `filter_profile: str` (`log|terminal|code|docs|web|default`)
   - `filter_stats: json` (`tokens_raw`, `tokens_clean`, `reduction_ratio`, `dedup_blocks`, `stripped_patterns`)
   - `filter_version: str`
2. Добавить модуль `mnemos/filter/`:
   - `dedup.py` (exact + near-duplicate)
   - `noise.py` (ANSI/progress/timestamps/separators/whitespace cleanup)
   - `extract.py` (ошибки/warnings/exit-status + информативное семплирование длинных выводов)
   - `compress.py` (семантическая компрессия однотипных блоков)
   - `tokens.py` (pre-tokenization estimation)
3. Профили фильтрации в `~/.mnemos/filter_profiles.yaml`; project overrides через `~/.mnemos/policies.yaml`.
4. Интеграция:
   - `mnemos_add` принимает `filter_profile` (если не указан — эвристика/`source:` tag)
   - `mnemos_search` / `mnemos_recall_context` / `mnemos_agent_recall` возвращают `clean_content` по умолчанию; `include_raw=true` — для drill-down
   - watchers и ingest path используют профиль по типу источника (`docs`, `web`, `terminal`, ...)
5. CLI/API:
   - `vesma filter preview --profile <name> --input <file>`
   - `vesma filter stats --since <date>`
   - `vesma filter reprocess --id <id> --profile <name>`
   - `GET /memories/<id>?include_raw=true`
6. Observability:
   - `mnemos_filter_tokens_saved_total{profile}`
   - `mnemos_filter_reduction_ratio{profile}`
   - `mnemos_filter_drill_downs_total`
7. Safety:
   - при ошибке фильтра `clean_content = raw_content` + trace warning
   - raw-данные никогда не удаляются фильтром
8. Тесты: `tests/test_filter.py` (unit/integration/regression/property).

**KPI цели**:
- ≥50% медианная экономия токенов на terminal/log входах
- ≤1% drill-down rate (иначе фильтр слишком агрессивен)
- 0% data loss

**Зависимости**: M1, M2. Параллельно с M4-M9.

---

## Phase M11 — Cache Center (DEFERRED to v2)

**Решение** (рекомендация): откладываем в v2. Cache center — operator-concern (производительность LLM-вызовов на больших vault). В v1 достаточно идемпотентности из M5 (которая сама даёт основное преимущество кэша синтеза). Подтвердить с пользователем перед стартом импла.

---

## Phase M12 — Docs/API portal & Web UI улучшения

1. Если у ai-brain уже есть Web UI — расширяем: страницы «Pipeline status», «Policies», «Traces», «Tags directory».
2. Если нет — минимальное FastAPI Swagger UI + статичный docs site (mkdocs или docusaurus) генерируемый из `docs/`.
3. Operator runbooks: `docs/runbooks/` — install, backup/restore, troubleshooting, migration from ai-brain.

**Зависимости**: M1-M6 (контент для документации).

---

## Phase M13 — Migration tool

1. CLI команда `vesma migrate-from-ai-brain --source ~/.ai-brain --vault ~/brain-vault` — импортирует существующий ai-brain SQLite + vault в Vesma формат.
2. Применяет tag contract в **lax mode** (`strict_tag_contract=false`) к импортируемым записям, чтобы старые записи без `agent:` тега не отвергались. Помечает их `mnemos:legacy` + `agent:unknown`.
3. Создаёт backup перед миграцией.
4. Dry-run режим: показывает что будет импортировано без записи.
5. Тесты: миграция test-fixture vault'а ai-brain.

**Зависимости**: M1, M2.

---

## Phase M14 — ai-brain archival

1. В `ai-brain/README.md` (upstream) — добавить шапку:
   > **DEPRECATED**: This project has been superseded by [Vesma](../mnemos/). All new development continues there. ai-brain remains for historical reference only.
2. Создать tag `final-v0.2.x` в ai-brain repo для последнего рабочего состояния.
3. Перенести open issues (если есть) в Vesma repo с label `migrated-from-ai-brain`.
4. Заморозить main branch (или сделать protection rule: no commits).

**Зависимости**: M13 готов (чтобы пользователи могли мигрировать).

---

## Phase M15 — Verification

1. Полный pytest suite (расширенный из ai-brain): ≥80% coverage, все новые модули покрыты.
2. **Новые наборы тестов**:
   - `tests/test_tag_contract.py` (M2)
   - `tests/test_agent_recall.py` (M3)
   - `tests/test_pipeline.py` (M4) — state machine + quality gates
   - `tests/test_policy_engine.py` (M5)
   - `tests/test_traces.py` (M6)
   - `tests/test_compaction_detection.py` (M7)
   - `tests/test_path_scoped_rules.py` (M8)
   - `tests/test_migration.py` (M13)
3. **Integration tests**: end-to-end через MCP клиент (stdio): add → cluster → synthesize → publish → search → agent-recall.
4. **Smoke**: запустить Vesma в контейнере (`podman compose up`), подключить из VS Code через mcp.json, выполнить полный цикл из реального Copilot-сессии.
5. **Benchmark**: гибридный поиск на vault с 10k записей — латентность < 200ms p95.

**Зависимости**: все предыдущие фазы.

---

## Relevant files (high-level, в форке)

- `mnemos/` (Python package, переименованный из `ai_brain/`)
- `mnemos/models.py` — добавить `TagContract`, расширить `Memory` (M2, M4)
- `mnemos/mcp_server.py` — переименовать tools, добавить `mnemos_agent_recall`, валидатор тегов (M2, M3)
- `mnemos/pipeline/` (новый модуль) — clustering, synthesis, quality gates, publish (M4)
- `mnemos/policy/` (новый модуль) — scheduler, event triggers, policy engine, DLQ (M5)
- `mnemos/traces.py` (новый) — explainability layer (M6)
- `mnemos/auto_collect.py` — расширить compaction detection (M7)
- `mnemos/watchers/path_scoped.py` (новый) — path-scoped rules ingest (M8)
- `mnemos/cli/migrate.py` (новый) — migration tool (M13)
- `mnemos/llm/` (новый модуль) — provider abstraction (Anthropic + OpenAI + Azure + Ollama + Gemini)
- `docs/tag-contract.md` (новый), `docs/pipeline.md` (новый), `docs/policies.md` (новый), `docs/runbooks/*` (M12)
- `tests/test_*.py` — все новые наборы (M15)
- `pyproject.toml`, `Containerfile`, `compose.yaml`, quadlet units — переименование (M1)

См. также детальную раскладку модулей в [ARCHITECTURE.md §11](ARCHITECTURE.md).

---

## Phase ordering (что параллельно vs последовательно)

```
M1 (fork & rebrand) ─┬─> M2 (tag contract) ──┬──> M4 (pipeline) ──> M5 (policy) ──> M15 (verify)
                     ├─> M3 (agent recall) ──┤                    └─> M6 (traces) ──┤
                     ├─> M7 (compaction) ────┤                                       │
                     ├─> M8 (path rules) ────┤                                       │
                     ├─> M10 (context filter)┤                                       │
                     ├─> M9 (audit/fix) ─────┤                                       │
                     └─> M12 (docs portal) ──┘                                       │
                                                                                     │
                     M2 + M4 ──> M13 (migrate) ──> M14 (archive ai-brain) ──────────┘
```

---

## Decisions / scope boundaries

- **Источник правды**: форк, не обёртка. Vesma владеет данными, схемой, MCP-интерфейсом. Upstream ai-brain — historical reference.
- **Совместимость**: миграционный CLI (M13) — единственная гарантия. Никакой runtime-совместимости со старыми `brain_*` инструментами (clean break).
- **Vesma tag contract — встроенный**, не опциональный (но настраиваемый через `strict_tag_contract` для миграции).
- **Knowledge Pipeline — обязательная фича v1** (M4). Это главная архитектурная доработка vs ai-brain.
- **Context Filter — обязательная v1 фича** (M10).
- **Cache Center — v2** (M11 deferred).
- **Никакого Web UI с нуля в v1** — если нет в ai-brain, ограничиваемся Swagger + mkdocs.
- **Безопасность**: запуск в rootless podman (как ai-brain); не экспонируем MCP наружу контейнера.

---

## Further considerations / questions для следующей сессии

1. **Lazy embeddings**: оставляем ONNX/MiniLM как в ai-brain или мигрируем на серверный embedding API? Рекомендация: оставляем локальный ONNX (privacy + offline). Confirm.
2. **LLM провайдеры для synthesis (M4)**: широкий набор сразу (Anthropic + OpenAI + Azure OpenAI + Ollama + Gemini) — *locked*. Подтвердить порядок реализации провайдеров (рекомендация: Anthropic → Ollama → OpenAI → Azure → Gemini).
3. **Naming в MCP клиенте**: оставлять «mnemos» как имя сервера или давать пользователю переименовывать через mcp.json? Рекомендация: сервер сам себя называет `vesma`; user-facing alias настраиваемый в mcp.json.
4. **Git стратегия**: чистый форк (без upstream history) или сохраняем full git history ai-brain? — *locked: сохраняем*. Подтвердить конкретный механизм (`git clone` + remote rename vs `git remote add upstream-ai-brain` + merge `--allow-unrelated-histories`).
