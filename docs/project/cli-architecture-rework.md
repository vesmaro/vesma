# CLI-реструктуризация: дизайн-док остатка после W-C

**Статус:** дизайн-док (Senior System Engineer по карточке `vesma-cli-architecture-rework`, п.1 приёмки; 2026-10-03). Одобрение владельца — отдельный шаг, в этот док не входит. Живой статус — `docs/project/dev-plan.md`.
**Основание:** докстринг-контракт владельца (2026-10-02); прецедент W-C — `vesma update` в 5.4.0.
**Рамка:** только CLI-поверхность (`src/vesmaro/cli/`); API, MCP-инструменты и схема хранения не затрагиваются.

## 1. Контракт именования (владелец, 2026-10-02)

- **Подкоманда = функция (ЧТО); флаг = конфиг (КАК).**
- **Режимные `--флаги`, переключающие функцию, → подкоманды.**
- **Рестракт без ломки:** старая форма живёт как hidden-алиас до следующего мажора; удаления — только в MAJOR (6.0).
- **Перед каждым шагом — проверка scripted-зависимостей** (systemd `ExecStart`, runbooks, CI): форма, на которой стоит чужой скрипт, не переименовывается без алиаса.

Прецедент W-C (5.4.0): `vesma update check|apply|timer|components` — реальные саб-аппы; старые флаговые формы (`--yes`, `--to`, `--scope`, `--install-timer`, `--uninstall-timer`) — hidden-алиасы с однострочным stderr-хинтом (`src/vesmaro/cli/update_cmd.py:967-975`, `hidden=True` на :999-1048). Поставляемый юнит продолжает работать на старой форме: `ExecStart=%h/.local/bin/vesma update --yes --scope=user` (`src/vesmaro/cli/update_cmd.py:79-80`, `contrib/vesma-update.service:31`).

## 2. Остаток — ранжированные нарушители

Ключевое наблюдение по коду: для трио `fts` / `processor` / `edge-stats` позиционная форма **текстуально совпадает** с целевой сабкомандной (`vesma processor run` → саб-апп `processor` с командой `run` — та же строка вызова). Реструктуризация в настоящие typer-саб-аппы не ломает ни одного существующего вызова; меняются только help, discoverability и путь ошибки «unknown action» (см. §2, столбец «дельта»).

| # | Нарушитель | Поверхность сейчас | Целевая форма | Дельта для существующих вызовов | Оценка |
|---|---|---|---|---|---|
| 1 | `processor` — позиционный action | `src/vesmaro/cli/main.py:877-905` (`status\|run\|start\|stop`, else→exit 1) | саб-апп `vesma processor status|run|start|stop` | нет (текст та же); bad-action: exit 1 → exit 2 typer-usage | M |
| 2 | `edge-stats` — позиционный action | `main.py:965-1040` (`stats` — default, `purge`) | саб-апп `vesma edge-stats stats|purge` | одна: голый `vesma edge-stats` (default=stats) начнёт показывать help вместо stats — в репо голая форма не используется, но нужна строка в CHANGELOG | S |
| 3 | `fts` — позиционный action | `main.py:861-874` (`rebuild`) | саб-апп `vesma fts rebuild` | нет (1 глагол; bad-action exit 1 → 2) | S |
| 4 | `recall --agent` — режимный флаг | `main.py:286-307` (ветвление `AgentRecallQuery` vs `recall_context`) | `vesma recall agent <slug>` (recall → группа с default-командой через `invoke_without_command`, голый `vesma recall` работает как сейчас); `--agent` → hidden-алиас + хинт | форма меняется → полный alias-цикл (§3) | M |
| 5 | `add --url/--file` — режимные флаги | `main.py:107-216` (`--url/-u`, `--file/-f`) | `vesma ingest url <url>` / `vesma ingest file <path>` (soft); `vesma add <content>` остаётся каноническим quick-capture | формы меняются → полный alias-цикл, soft-режим (§3) | M |
| 6 | `reindex`, `backfill-embedding-ids` | `main.py:911-926`, `main.py:932-959` — одиночные verb-команды | не нарушители контракта; кандидат на консолидацию `vesma index rebuild|backfill-embedding-ids` в окне 6.0 | при консолидации — alias-цикл; до тех пор — нет | S |

Докстринг самого кода уже признаёт legacy-статус: `src/vesmaro/docs_ingest.py:65` называет `vesma add --url` «legacy CLI name».

## 3. Политика deprecation / hidden-алиасов

Единый цикл, выверенный по прецеденту W-C и ADR-0031 (dual-period, ретракт легаси «no earlier than 6.0»):

1. **Минор N:** новая каноническая форма входит в хелп и доки; старая форма остаётся working-алиасом — для текстуально несовпадающих форм: `hidden=True` + однострочный stderr-хинт `[deprecated] '<old>' is deprecated — use: <new>` (паттерн `_deprecated_flag_hint`, `update_cmd.py:967-975`).
2. **Миноры N..MAJOR-1:** алиас живёт без деградации; телеметрия legacy-вызовов (по образцу ADR-0031) собирает данные для решения об удалении.
3. **MAJOR (6.0):** удаления — одним мажором, по данным телеметрии, единым списком (вместе с уже запланированными 6.0-ретрактами: `VESMARO_*` env, legacy MCP-префикс `mnemos_*`, `vesma update --yes/--to/--scope/--install-timer/--uninstall-timer`).
4. **Отказ от ломки без мажора абсолютен** для форм, на которых стоят scripted-зависимости (§4): такой форме алиас обязателен до её 6.0-судьбы.
5. Для текстуально совместимых реструктуризаций (трио §2.1–2.3) alias-цикл не нужен — требуется только CHANGELOG-заметка (включая голую форму `edge-stats`) и обновление `docs/en|ru/user/cli-reference.md`.

## 4. Scripted-зависимости (grep по репо, 2026-10-03, ветка cli-arch-doc)

**CI / Makefile / scripts/ — 0 зависимостей** (`.github/workflows/{ci,release,publish-npm}.yml`, `Makefile`, `scripts/` — ни одного вызова старых форм).

**systemd-юниты — 1 живая зависимость, уже удержана W-C:** `contrib/vesma-update.service:31` — `ExecStart=%h/.local/bin/vesma update --yes --scope=user` (форма `update --yes --scope` — hidden-алиас `update_cmd.py:79-80`). Остаток из §2 в юнитах (`contrib/systemd/vesma-{server,watcher,sync}.service`) не упоминается. Дистрибутивные формы с `distrobox-enter … vesma update --yes --scope=user` (`contrib/vesma-update.service:21-22`) покрыты тем же алиасом.

**Runbooks — 4 файла × en/ru (8 файлов, ~10 строк вызовов):**
- `docs/en|ru/admin/runbooks/edge-stats-maintenance.md:35,38,41,70` — `vesma edge-stats stats|purge …` (текстуально совместимы с W1);
- `docs/en|ru/admin/runbooks/migrate.md:98|99` — `vesma processor start` (совместим);
- `docs/en|ru/admin/runbooks/backup-restore.md:62` — `vesma add --file` (меняется при W3 → доки обновляются в волне введения `ingest`);
- `docs/en|ru/admin/runbooks/install.md:109|115` — `vesma recall --agent` (меняется при W2).

**Корневые и пользовательские доки (обновляются в своих волнах, не блокируют код):** `README.md:176`, `ARCHITECTURE.md:203`, `PLAN.md:54`, `docs/en|ru/user/cli-reference.md` (разделы `add`:101, `recall`:184, `fts`:364, `processor`:385, `reindex`:415), `docs/en|ru/architecture/overview.md:262-270`, `docs/en|ru/admin/security.md:397|390`, `docs/en|ru/user/mcp-tools.md` (кросс-ссылки :256, :647, :1573).

**In-code зависимости от строковых форм (ловятся тестами — обновлять в той же волне, что и форму):**
- `src/vesmaro/cli/doctor.py:617,649` — hint-строки `vesma processor start`;
- `tests/test_doctor_pending_refine.py:79,127` — ассерты этих hint-строк;
- `src/vesmaro/cli/main.py:1003-1005` — примеры в docstring `edge-stats`;
- `src/vesmaro/docs_ingest.py:65` — docstring-упоминание `vesma add --url`.

Политика §3 сохраняет всё перечисленное: ни одна форма не исчезает до 6.0, scripted-формы (`update --yes --scope`) имеют алиас уже сейчас, доки обновляются в волнах введения новых форм, а не отложенно.

## 5. Порядок волн

| Волна | Окно | Содержание | Оценка | Гейт |
|---|---|---|---|---|
| **W1** | 5.5.0 | Трио `fts` + `edge-stats` + `processor` → настоящие саб-аппы (текстуально совместимо, без alias-цикла); CHANGELOG (голая форма `edge-stats`, exit-коды bad-action); doctor-хинты и тесты остаются валидными | S+S+M одной волной, один паттерн | полный suite + grep-контроль scripted-форм |
| **W2** | 5.6.0 | `recall agent <slug>`; `recall` → группа с `invoke_without_command` (голая форма не меняется); `--agent` → hidden-алиас + хинт; обновление install-runbook, PLAN, ARCHITECTURE, cli-reference | M | suite + тесты на алиас |
| **W3** | 5.6.0–5.7.0 | `vesma ingest url|file`; `add --url/--file` → hidden-алиасы (soft, live до 6.0+); обновление backup-restore, security, overview, mcp-tools, cli-reference | M | suite + тесты на алиасы |
| **W4** | 6.0 (окно) | Удаления алиасов одним мажором (update-флаги W-C, `recall --agent`, `add --url/--file`); опционально консолидация `vesma index` (§2.6); решение — по телеметрии legacy-вызовов | M (в уже запланированном окне 6.0) | отдельное решение 6.0, не в этой карточке |

Прецедент W-C отмечен как образец: волна выполнена в 5.4.0 без единого слома scripted-зависимости (`contrib/vesma-update.service` продолжает работать на старой форме).

## 6. Что нужно от владельца (после одобрения дока)

- Подтвердить целевую форму `vesma recall agent <slug>` (vs альтернатива «оставить `--agent` как легитимный фильтр» — отклонена как нарушающая контракт: флаг переключает функцию извлечения, а не конфигурирует её).
- Подтвердить soft-режим для `add --url/--file` (алиасы без даты удаления внутри 5.x; судьба — в 6.0 по телеметрии).
- Подтвердить окно W1 = 5.5.0.
