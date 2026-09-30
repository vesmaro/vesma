# Финальный код-ревью Vesma — 2026-06

*Historical artifact — English only.*

**Ревьюер:** GCW: Code Reviewer (multi-pass)
**Объём:** весь `src/vesma/` (39 модулей, ~7 300 LOC) + тесты + документация
**Базовая ревизия:** `main` @ `208a686` (tag `v0.2.0`)
**Ветка с исправлениями:** `fix/code-review-resource-leaks`
**Режим:** `standard` (security + architecture + quality + performance + tests)

---

## Executive summary

Кодовая база в хорошем состоянии: все автоматические гейты (`ruff`, `ruff
format`, `mypy --strict`, `bandit`, `pip-audit`, 326 тестов, покрытие 83 %)
были зелёными ещё до ревью. SSRF-guard, SQL-whitelist и FTS5-escaping
реализованы грамотно и задокументированы в ADR.

Тем не менее найдено и **немедленно исправлено 3 реальных дефекта**, один из
которых — security-уязвимость уровня High:

| # | Severity | Область | Статус |
|---|----------|---------|--------|
| 1 | **High** | Security (SSRF) | ✅ Исправлено |
| 2 | **Medium** | Reliability (утечка ресурсов) | ✅ Исправлено |
| 3 | **Low** | Release hygiene (версия) | ✅ Исправлено |

После исправлений: `make verify` — зелёный, 327 тестов (добавлен 1
регрессионный), покрытие ≥ 83 %.

---

## Findings by severity

### [High-1] SSRF через HTTP-редиректы в `ingest_url`

- **Файл:** [src/vesma/manager.py](../../src/vesma/manager.py#L487)
- **Класс:** OWASP A10:2021 — Server-Side Request Forgery (CWE-918)
- **Суть:** `_validate_url()` валидирует только **исходный** хост. HTTP-клиент
  создавался с `httpx.Client(follow_redirects=True, max_redirects=5)`, поэтому
  атакующий мог подать публичный URL, прошедший проверку, который отвечает
  `302 Location: http://169.254.169.254/latest/meta-data/` — и клиент слепо
  шёл на внутренний/метадата-эндпоинт в обход guard'а.
- **Усугубляющий фактор:** документация [docs/security.md](../en/admin/security.md) §2 явно
  заявляла «**Followed redirects? No (in v1)**», а ADR-0009 ошибочно называл
  `follow_redirects=True` *митигацией* DNS-rebinding. Код противоречил
  собственной модели угроз.
- **Исправление:** `httpx.Client(follow_redirects=False)`. Это приводит код в
  соответствие с задокументированной политикой v1 и закрывает redirect-вектор.
  Обновлён ADR-0009 (убрана ошибочная формулировка). Добавлен регрессионный
  тест `test_ingest_url_does_not_follow_redirects`.

### [Medium-2] Утечка SQLite-соединений в `VectorStore`

- **Файл:** [src/vesma/storage/vector_store.py](../../src/vesma/storage/vector_store.py#L45)
- **Класс:** Reliability / resource leak
- **Суть:** `VectorStore` кэшировал соединение в `threading.local`, но —
  в отличие от `SQLiteStore` и `SessionStore` — **не имел метода `close()`**.
  `MemoryManager.close()` закрывал только `self.sqlite`. Соединения
  освобождались лишь сборщиком мусора → `ResourceWarning: unclosed database`
  (доминирующее предупреждение в тестах, ~150 шт.) и утечка файловых
  дескрипторов в долгоживущих процессах (API-сервер, watcher).
- **Исправление:** добавлен `VectorStore.close()` (зеркало `SQLiteStore.close()`),
  вызывается из `MemoryManager.close()`. Фикстура `test_vector_store.py::vs`
  теперь закрывает store. `ResourceWarning` устранён (проверено
  `pytest -W error::ResourceWarning`).

### [Low-3] Рассинхрон версии пакета с релизным тегом

- **Файлы:** [pyproject.toml](../../pyproject.toml#L7),
  [src/vesma/__init__.py](../../src/vesma/__init__.py#L6),
  [src/vesma/api/main.py](../../src/vesma/api/main.py#L59)
- **Класс:** Release hygiene
- **Суть:** CHANGELOG и git-тег объявляли релиз `0.2.0`, но `pyproject.toml`,
  `__version__` и FastAPI-app сообщали `0.1.0`. Версия пакета (и `/docs` Swagger)
  не совпадала с релизом.
- **Исправление:** bump до `0.2.0` во всех трёх местах; FastAPI-app теперь
  читает версию из `vesma.__version__` (единый источник истины, устраняет
  будущий дрейф). Добавлена секция `[Unreleased]` в CHANGELOG для этих фиксов.

---

## Проверено и признано корректным (без изменений)

- **SSRF-валидатор `_validate_url`** — покрывает schemes, IPv4/IPv6 литералы,
  RFC1918, link-local, метадата, DNS-резолвинг с проверкой resolved-IP
  (ADR-0012). Прочная реализация.
- **SQL-инъекции** — динамический SQL в `update_fields` (whitelist
  `_FIELD_UPDATERS`) и `list_all` (статические литералы колонок, значения
  биндятся) — параметризован корректно. `# nosec B608` легитимны.
- **FTS5-инъекции** — `_FTS5_SPECIAL_CHARS` strip + фразовое экранирование по
  рекомендации SQLite. Корректно.
- **Broad `except`** в `traces.py`, `mcp_server.py`, `cli/migrate.py` —
  все логируют и не глушат молча (non-fatal recovery). Приемлемо.
- **`llm/base.py::create_provider`** — `NotImplementedError` + `TODO (M4)`:
  не мёртвый код, а явная заглушка незакрытой вехи. Покрытие 0 % ожидаемо.
- **`.history/`** — мусор VS Code Local History, уже в `.gitignore`, не
  трекается. Действий не требуется.
- **`nosec`/`noqa`-аннотации** — все задокументированы ссылками на ADR.

---

## Deferred items (вне объёма ревью, не дефекты)

- **Покрытие `mcp_server.py` (0 %)** и `llm/base.py` (0 %) — интеграционные
  поверхности, тяжело юнит-тестируемые. Рекомендация: smoke-тест MCP-tool
  dispatch в отдельной вехе. Не блокер.
- **Покрытие `embeddings/__init__.py` (60 %)**, `manager.py` (77 %) — провайдеры
  требуют сети/моделей. Рекомендация: моки провайдеров.
- **Branch protection на `main`** — заблокировано лимитом GitHub (нужен Pro /
  публичный репо). Требует действий администратора, не кода.

---

## Verification gate (после исправлений)

| Проверка | Результат |
|----------|-----------|
| `ruff check` | ✅ All checks passed |
| `ruff format --check` | ✅ 54 files formatted |
| `mypy --strict` | ✅ no issues (39 files) |
| `bandit` | ✅ 0 findings (нет новых skip) |
| `pip-audit` | ✅ clean (1 ignored CVE, документирован) |
| `pytest` | ✅ 327 passed |
| coverage | ✅ ≥ 83 % (`--cov-fail-under=80`) |
| `ResourceWarning` | ✅ устранён |

---

## Изменённые файлы

- `src/vesma/manager.py` — SSRF fix + vectors.close()
- `src/vesma/storage/vector_store.py` — добавлен `close()`
- `src/vesma/__init__.py`, `pyproject.toml`, `src/vesma/api/main.py` — версия 0.2.0
- `tests/test_security.py` — регрессионный тест на redirects
- `tests/test_vector_store.py` — закрытие фикстуры
- `docs/adr/0009-ssrf-guard-in-ingest-url.md` — корректная формулировка митигации
- `CHANGELOG.md` — секция `[Unreleased]`
