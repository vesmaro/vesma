# CI/CD Runbook

**🌐 Language / Язык:** [English](../../../en/admin/runbooks/ci-cd.md) · Русский

> **Область**: Работа, отладка и расширение pipeline GitHub Actions CI для
> Vesma. Источник истины: [`.github/workflows/ci.yml`](../../../../.github/workflows/ci.yml).

---

## Обзор pipeline

CI workflow (`.github/workflows/ci.yml`) запускается при каждом push в `main`,
каждом pull request с целью `main` и еженедельно для drift check (понедельник,
06:00 UTC). Содержит два job'а:

| Job | Runner | Назначение |
|---|---|---|
| `verify` | `ubuntu-latest`, матрица Python 3.11 / 3.12 / 3.13 | Lint + format + mypy + bandit + pip-audit + pytest + coverage |
| `build-container` | `ubuntu-latest` (rootless buildah) | Smoke-тест сборки `Containerfile` и работы CLI внутри образа (сегодня — легаси-хук `mnemos --help`) |

Job `verify` является **обязательной status check** для `main` (см.
[Защита веток](#защита-веток)).

---

## Локальная валидация

Запускайте те же проверки локально перед push, чтобы не тратить минуты CI:

```bash
cd /path/to/vesma   # корень репозитория
source .venv/bin/activate

ruff check src/ tests/                                # lint
ruff format --check src/ tests/                       # format
mypy --strict src/vesma/                             # типы
bandit -r src/ -f json -o bandit-report.json          # безопасность (статическая)
pip-audit --ignore-vuln CVE-2026-45829 --ignore-vuln PYSEC-2026-4146   # зависимости (два игнора, см. dependency-updates.md)
pytest tests/ -q --tb=short                           # тесты
pytest --cov=src/vesmaro --cov-fail-under=80 --cov-report=term-missing tests/ -q   # gate по покрытию
```

Эквивалент одной командой:

```bash
make verify
```

Если локальный gate зелёный, CI gate тоже будет зелёным. Если CI красный, а
локально зелёный — разница почти всегда в **окружении**: патч-версия Python,
библиотеки ОС (например, sqlite) или поведение pip resolver.

---

## Воспроизведение CI локально

**Основной локальный путь — `scripts/local-ci.sh`** (цели `make local-ci` /
`make local-ci-build`): байт-в-байт репликация `verify`-job'а ci.yml — lint,
format, mypy, bandit, pip-audit (с теми же ignore-флагами), pytest, coverage
gate и doctor; `--build` добавляет сборку wheel/sdist как в release.yml.
GitHub Actions заблокирован по billing (#117), поэтому merge- и release-гейты
замыкаются этим скриптом; при возобновлении Actions он останется быстрым
pre-push-санити.

Локальная сборка образа с smoke-ом (то, что делает job `build-container`, —
легаси-CI хук `mnemos --help` внутри образа):

```bash
buildah bud -t mnemos:test .
buildah from --name vesma-test mnemos:test
buildah run vesma-test -- mnemos --help
```

[`act`](https://github.com/nektos/act) — альтернатива для прогонов workflow
файла в Docker, когда Actions снова заработают; пока биллинг заблокирован,
прогоны идут только через `local-ci.sh`.

---

## Защита веток

> ⚠️ Это **не** применяется workflow — нужно настроить в настройках репозитория
> GitHub (Settings → Branches → Branch protection rules → `main`).

Рекомендуемые настройки для `main`:

| Настройка | Значение |
|---|---|
| Require a pull request before merging | ✅ |
| Required approving reviews | **1** |
| Dismiss stale pull request approvals when new commits are pushed | ✅ |
| Require review from Code Owners | ❌ (нет `CODEOWNERS` пока) |
| Require status checks to pass before merging | ✅ |
| Require branches to be up to date before merging | ✅ |
| Required status checks | `Lint + Test + Type + Security (Python 3.12)` |
| Require conversation resolution before merging | ✅ |
| Require signed commits | ❌ (слишком много трения сейчас) |
| Require linear history | ✅ (squash-merge) |
| Include administrators | ✅ |

Обязательная status check — **средняя** запись матрицы (`Python 3.12`):
это версия, которую мы используем для разработки, и та, с которой Codecov
загружает данные. Все остальные записи матрицы и контейнерный job
информационны — они краснеют на PR, но не блокируют merge самостоятельно.

Настройка через UI GitHub: Settings → Branches → Add rule → pattern ветки
`main` → включить указанные пункты. Эквивалент на Terraform — в platform repo
(вне области этого slice).

### Почему не применяем все три версии Python

Применение всех трёх версий матрицы как обязательных checks блокировало бы
merge, когда одна из них падает по причине, не затрагивающей production (3.12
— базовая версия `Containerfile` и та, которую мы поставляем). Остальные записи
матрицы отображаются как ❌ на PR — мы считаем регрессию в 3.11 или 3.13
release blocker и исправляем до следующего релиза, но не блокируем ежедневную
работу.

---

## Покрытие

- Порог: **80%** (`--cov-fail-under=80`).
- Загружается в Codecov только с Python 3.12, чтобы избежать трёх дублирующих
  загрузок на один прогон.
- Codecov опционален — action завершается корректно, если `CODECOV_TOKEN`
  не задан (`fail_ci_if_error: false`).

### Почему gate на 80%, а не на 100%

Оставшийся разрыв сосредоточен в:

1. `src/vesma/llm/*.py` — адаптеры провайдеров с тонким pass-through к
   vendor SDK (anthropic / openai / gemini / ollama). Высокая связанность с
   форматами HTTP-ошибок vendor делает полноценный e2e-тест дорогим.
2. `src/vesma/watchers/` — обработчики событий файловой системы; покрыты
   юнит-тестами, но не в-процессными end-to-end потоками.
3. `src/vesma/auto_collect.py` — путь auto-collect cron запускается вручную,
   не в CI.

Для каждого есть follow-up issue. До их закрытия gate 80% — намеренный пол.

---

## Dependabot

Конфигурация: [`.github/dependabot.yml`](../../../../.github/dependabot.yml).

| Экосистема | Расписание | Лимит PR | Метки |
|---|---|---|---|
| `pip` | Еженедельно, понедельник 06:00 UTC | 5 | `dependencies`, `security` |
| `github-actions` | Еженедельно, понедельник | 5 | `ci`, `dependencies` |

Patch и minor обновления группируются в один PR на прогон для снижения нагрузки
на ревьюера. Major-версии намеренно исключены для `aiohttp` и `starlette` —
эти pin'ы закрывают транзитивные CVE
([ADR-0008](../../../project/adr/0008-sql-injection-via-fstring.md)) и требуют
процедуры из [dependency-updates.md](dependency-updates.md) для безопасного
обновления.

Если Dependabot открывает PR, нарушающий политику закреплённых версий в
`pyproject.toml` (например, пытается поднять `aiohttp` выше `4.0`), закройте
его и пересмотрите вручную по runbook'у dependency-updates.

---

## Job сборки контейнера

Job `build-container` использует `buildah` (rootless, без daemon'а) вместо
Docker, чтобы избежать привилегированного контейнера на GitHub-hosted runners.
Шаги:

1. `apt-get install buildah`
2. `buildah bud -t mnemos:test .` — сборка `Containerfile`
3. `buildah from --name mnemos-test mnemos:test` — запуск контейнера
4. `buildah run mnemos-test -- mnemos --help` — smoke-тест (плюс вывод версии Python)

> Smoke-шаг запускает CLI внутри собранного образа, поэтому проверяет
> CLI-точку входа, а не только базовый образ. Имя `mnemos` — легаси-хук
> точки входа, который в образе остаётся (двойной период до 6.0); canonical
> CLI — `vesma`.

При падении контейнерного job'а проверьте лог на:

- **Инвалидация layer-кэша при `pip install`** — обычно временная проблема PyPI.
  Перезапустите job.
- **Ошибки прав доступа `buildah bud`** — иногда возникает на runner-образе
  `ubuntu-22.04`; pin `ubuntu-latest` избегает этого в 99% случаев. Если
  воспроизводится, переключите runner явно на `ubuntu-24.04`.

---

## Отладка упавших прогонов

1. Откройте упавший прогон в GitHub Actions.
2. Найдите упавший шаг. Лог каждого шага сворачивается — разверните его.
3. Наиболее полезные шаги при нестабильности:
   - `Security (pip-audit)` — `pip-audit` чувствителен к свежести advisory DB.
     Если единственный сбой — НОВОЕ CVE, проверьте pin'ы в `pyproject.toml` и
     runbook dependency-updates.
   - `Test (pytest)` — прокрутите вверх; assertion обычно на несколько сотен
     строк выше сводки.
   - `Coverage check` — если единственный сбой — порог, смотрите `term-missing`
     отчёт в том же шаге. Он перечисляет непокрытые строки.

### Перезапуск job'а

Используйте кнопку **"Re-run jobs"** в UI GitHub. Если сбой был flake (сеть,
временная проблема), это правильная кнопка. Если сбой реальный — сначала
исправьте код; никогда не перезапускайте вместо исправления.

### Скачивание артефактов

Артефакт `bandit-report-pyX.Y` загружается **только при сбое**. Скачать со
страницы сводки прогона → секция Artifacts. Срок хранения — 7 дней.

---

## Добавление нового шага в job `verify`

Откройте `.github/workflows/ci.yml`. Новый шаг помещается после существующего
блока lint/format/type/security и перед тестовым шагом. Соглашения:

1. Использовать `source .venv/bin/activate &&`, чтобы шаг выполнялся в проектном
   venv (зависимости, установленные uv, живут там; venv создаётся шагом
   `uv venv` — только install-флоу, никаких ручных venv).
2. Кэширование pip уже включено на шаге `setup-python` (`cache: pip`). Workflow
   пинит ruff (`ruff>=0.15,<0.16`) под версию локального `make verify`,
   поэтому новый инструмент = правка extras `[project.optional-dependencies].dev`
   + при необходимости синхронный пин в шаге CI.
3. Если шаг производит отчёт (например, `bandit-report.json`), загружайте его
   как артефакт с guard `if: failure()`, чтобы артефакт появлялся только при
   сбое.

После редактирования — проверьте локально:

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
```

Затем push в feature-ветку и убедитесь в зелёной check на draft PR перед мержем.

---

## Вне области (сейчас)

- **CD / deploy** — release-pipeline живёт в
  [`.github/workflows/release.yml`](../../../../.github/workflows/release.yml):
  тег `v*.*.*` должен собирать wheel/sdist, GitHub Release образ
  `ghcr.io/vesmaro/vesma:$VERSION` + `:latest` (реестр исправлен с
  доребрендингового имени `korrnals`), job `release-complete` считает релиз
  незавершённым без любой из составляющей. Workflow заблокирован по billing
  (#117) и не срабатывает — рабочий поезд релиза локальный:
  `scripts/pypi-publish.sh --publish` с обязательной image-фазой
  (`scripts/image-publish.sh`; см. [`pypi-publish.md`](pypi-publish.md),
  «Container image»), а `scripts/local-release.sh` — устаревший fallback.
  Использование контейнеров — в
  [`container-deployment.md`](container-deployment.md).
- **Self-hosted runner** — не нужен в текущем масштабе. GitHub-hosted
  `ubuntu-latest` достаточно быстр, а concurrency group держит затраты под
  контролем.
- **Матрица по ОС** — только Debian/Ubuntu. Проект не поддерживает Windows или
  macOS, поэтому матрица `runs-on:` не нужна.
- **Блокировка через Codecov dashboard** — gate 80% применяется
  `pytest --cov-fail-under`, а не status check Codecov. Это позволяет gate
  работать даже без `CODECOV_TOKEN`.
