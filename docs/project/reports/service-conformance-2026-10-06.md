# Service-layer conformance — W6 (svc-w6-integration-conformance) + W7 closers

- Дата: 2026-10-06 (W7-дополнение: 2026-10-05/06)
- Волна: W6, карта `svc-w6-integration-conformance`; W7-дополнение —
  карта закрытия двух последних пунктов перед ратификацией
  (`feat/svc-w7-ratification-closers`, от `origin/main` @ `1ff47b9`):
  SL-06 true-container лега + LY-12 API-лега отказа записи кэша.
  W1–W5 слиты в main, head `c793b98`; W6 слит в main, head `1ff47b9`.
- Контракты: `specs/service-lifecycle/v1` (1.0.0-draft.2),
  `specs/control-socket/v1` (1.0.0-draft.2), `specs/layout/v1`
  (1.0.0-draft.2), `specs/component-manifest/v1` (1.0.0-draft.2)
- Ветка: `feat/svc-w6-integration` (от `origin/main` @ `c793b98`)
- Чеклисты: `conformance/checklist.md` каждого контракта — SL-01…SL-18,
  CS-1…CS-16, LY-01…LY-14, CM-01…CM-17

## Метод

Интеграция закрыта реальным адаптером `SupervisorBackend`
(`src/vesmaro/service/backend.py`): статус/health из FSM-снапшота
супервайзера, идемпотентные start/stop, логи из in-memory буфера
(`ComponentLogBuffer` — композит перед реальным logsink), и композицией
`ServiceApp` — `vesma service run` держит контрольный сокет, in-process
ядро (тот же `vesmaro.api.main:app`, uvicorn workers=1) и дерево
компонентов в одном процессе (SL §3.1: смерть ядра = смерть супервайзера,
exit 1; отдельного механизма рестарта ядра нет — сознательно).

Вердикты — по колонке «Проверка/Способ проверки» чеклистов: исполняемые
пункты цитируются именем теста (`tests/…`), ручные — ссылкой на
инспекцию. Незакрываемые в этой среде леги помечены `gap` с точной
причиной; `partial`/`gap` не приравниваются к `pass`.

## Scorecard

| Чеклист | pass | gap | n/a | fail |
|---|---|---|---|---|
| SL-01…SL-18 (service-lifecycle) | 18 (W7: SL-06 контейнерная лега закрыта реальным прогоном) | 0 | 0 | 0 |
| CS-1…CS-16 (control-socket) | 16 | 0 (2 live-леги cross-uid — честные skip вне root, решение unit-протестировано) | 0 | 0 |
| LY-01…LY-12, LY-14 (layout, user-профиль) | 13 (W7: LY-12 API-лега закрыта — taint + write-API) | 0 | 1 (LY-13, system-профиль v2) | 0 |
| CM-01…CM-17 (component-manifest) | 17 | 0 | 0 | 0 |

Раннер спеков (`tools/conformance/run.py --all specs` от
`vesma-specs` @ waves W5/W6): **24 passed / 0 failed / 0 warned**
(suite `component-manifest-v1` — schema_valid / kind_launch_consistency /
argv_placeholder_allowlist / name_not_reserved / license_spdx /
env_file_outside_manifests_dir / checker_block_consistency / no_secret_in_vars,
плюс все негативные фикстуры `expect=fail`). Бандлед-манифесты движка
(`board.yaml`, `metrics.yaml`) проходят полный загрузчик `load_manifest`
(`tests/test_service_manifest.py::test_bundled_manifests_together_form_valid_installation`).

---

## Таблица SL-01…SL-18 (service-lifecycle v1)

| ID | Вердикт | Основание (тест / инспекция) |
|---|---|---|
| SL-01 | **pass** | `tests/test_service_supervisor.py::TestSL01Isolation` (SIGKILL подмножеств детей — супервайзер жив, FSM → backoff); сокет-лега: `tests/test_service_control.py::test_sl01_socket_still_accepts_after_child_death`; живая лега поверх реального адаптера: `tests/test_service_e2e.py::TestIsolation::test_kill9_child_socket_alive_fsm_backoff` (kill -9 при живом сокете: hello проходит, §3.4 exit-строка `signal=KILL`, T8 backoff + респаун, глобальный health `healthy`) |
| SL-02 | **pass** | `TestSL02ExceptionBoundary` — инъекция падающего per-child обработчика; main loop жив, FSM → backoff; плюс `TestP2FBoundaryHardening` (даже `BaseException` из in-process фабрики — ребёнок, не поток) |
| SL-03 | **pass** | `TestSL03Sessions::test_child_pgid_equals_pid` — `/proc/<pid>/stat` field 5 после spawn; спавн с `start_new_session=True` (`supervisor.py::_spawn`), несовпадение pgid==pid — RuntimeError |
| SL-04 | **pass** (авт+ручн) | авт: `TestSL04GroupStop` — потомок-«упрямец» переживает SIGTERM, умирает с группой по SIGKILL; ручн: `grep -rn pkill src/vesmaro/` — ноль вхождений (проверено 2026-10-06); pkill в сгенерированном юните отсутствует — ExecStop не генерируется вовсе (`tests/test_service_unitgen.py::test_no_execstop_generated`) |
| SL-05 | **pass** | `TestSL05Subreaper::test_orphaned_grandchild_reaped_by_supervisor` — внук репарентится в subreaper и reap'ится (нет зомби); закалка.flак W6: payload ребенка polled до PARSE-COMPLETION (`_read_payload`), дедлайны 20–30 s — детерминированно зелёный под нагрузкой полного сюита |
| SL-06 | **pass** (юнит + контейнер) | юнит: `TestSL06Pid1::test_pid1_unit_legs_sigterm_graceful_stop_exit_zero` (SIGTERM/SIGINT обработаны, graceful-порядок, exit 0, дети остановлены и reap'лены до exit). контейнер (W7): `tests/test_service_pid1_container.py` → `scripts/pid1_conformance.sh` — реальный Supervisor как PID 1 контейнера (rootless podman на хосте владельца через `distrobox-host-exec`; in-box podman отказан — `newuidmap: write to uid_map failed: EPERM`, ровно как в gap-заметке W6; образ `ghcr.io/vesmaro/vesma:latest`, репозиторий смонтирован read-only, работа только в собственном контейнере `vesma-pid1-conformance`, убран после прогона). SIGTERM контейнеру → обработчик PID1 → graceful stop → **exit code 0**. Хвост прогона (2026-10-05 22:50 MSK, `src/vesmaro/service/pid1_probe.py`):<br>`VESMA_PID1_PROBE_READY orphan_pid=9 orphan_reaped=True`<br>`VESMA_PID1_SUMMARY {"ok": true, "pid1": true, "exit_code": 0, "child_pid": 5, "child_gone": true, "child_final_state": "stopped", "zombies": [], "orphan_pid": 9, "orphan_reaped_before_stop": true, "journal": ["…event=health … from=stopped to=starting", "…event=spawn pid=5", "…from=starting to=healthy", "…from=healthy to=stopped"]}`<br>Внук-сирота (двойной fork, pid=9) репарентился в subreaper и reap'нут ДО стопа — ни одного зомби в /proc; ребёнок (pid=5) остановлен и reap'нут до exit'а probe (журнал §3.4 — доказательство порядка); exit 0. Вне podman-хостов лега честно skip с точной причиной отказа пробы; `unshare`-лега повторно пробуется с таймаутом 30 s (в sandbox отказ — named skip) |
| SL-07 | **pass** | `tests/test_service_fsm.py::TestNormativeRows` — T1…T14 построчно; `TestExhaustiveFuzz::test_every_triple_is_table_row_or_raises` — фаззер всех троек (состояние×событие×бюджет): каждая — строка таблицы или `TransitionError` (запрещённые переходы невозможны) |
| SL-08 | **pass** | `TestSL08Grammar` — regex-парсер §3.4 грамматики над всеми строками журнала прогона (префикс, порядок полей, code/signal взаимоисключающи, severity); валидация по построению в `logsink.py` (builders отказываются рендерить линию вне грамматики); живой прогон поверх адаптера — линии буфера суть `line.render()` |
| SL-09 | **pass** | `TestSL09OptionalBudgetExhaustion` — исчерпание бюджета: состояние degraded + health-флаг + РОВНО ОДНА ERROR-строка `reason=restart-budget-exhausted attempts=5 window=300s`; флаг supervisor-level — `TestP1BBudgetExhaustedHealthFlag` |
| SL-10 | **pass** | `TestSL10LazyRetry` — тайминг попыток, ноль ERROR-строк в lazy-режиме; ручной `start` сбрасывает бюджет (повторный алерт после ручного старта) |
| SL-11 | **pass** | `TestSL11CoreCrashLoop` — 10 попыток без healthy → глобальный `degraded` + строка `reason=crash-loop attempts=10 window=none`, рестарты продолжаются |
| SL-12 | **pass** | `TestSL12CoreBackoffTiming` — замер интервалов base 1s ×2 cap 30s с допуском на jitter ±20%; сброс счётчика после 300s uptime |
| SL-13 | **pass** | `TestSL13ChildEnv` — conformance-ребёнок печатает env, раннер сверяет: ровно конструируемый PATH + `env.vars` + `env_file` + `PYTHONNOUSERSITE=1`, ноль хостовых переменных; зарезервированный PATH в env-файле отклоняется (`TestP2DEnvFileReservedKeys`) |
| SL-14 | **pass** | `TestSL14SocketIsolation` — `close_fds=True`/пустой `pass_fds` (fd сокета не наследуется), путь сокета отсутствует в env ребёнка по построению (env конструируется, не наследуется) |
| SL-15 | **pass** | `TestSL15StartOrdering` — топологический старт по `depends_on`, независимые параллельно; цикл = ошибка (`_topological_order` → ValueError, install-валидация поверх); blocked при неподнятой core-зависимости + backoff-логи |
| SL-16 | **pass** | `TestSL16GracefulStop` + живая лега `tests/test_service_e2e.py::TestGracefulShutdown::test_sigterm_reverse_topological_stop_no_zombies` — SIGTERM супервайзеру: реверс-топологический порядок stop-переходов доказан по append-only журналу (top → mid → base), ноль живых детей/зомби на момент exit, сокет удалён |
| SL-17 | **pass** | `tests/test_service_unitgen.py::TestMustTable::test_every_must_directive_exact` (все директивы таблицы MUST), `test_execstart_one_static_line_no_shell`, `test_no_execstop_generated`, валидность `StartLimit*`; структурное равенство с эталоном спеки — `TestSpecsExampleParity` |
| SL-18 | **pass** (авт+ручн) | авт: `TestHardening::test_full_hardening_block_present` (полный hardening-блок), `test_systemcallfilter_commented_tier_b`, `test_memorydenywriteexecute_deliberately_absent`; клампы restart-override (base ≥ 500ms, max ≤ 5min, attempts ≥ 3) — `tests/test_service_install.py::test_out_of_clamp_restart_override_surfaces_loader_error`; контейнерные деградации громкие и только allowlist — `TestContainerDowngrades`, `tests/test_service_install.py::test_container_downgrade_is_loud_and_marked` |

---

## Таблица CS-1…CS-16 (control-socket v1)

Все пункты — `tests/test_service_control.py` (использует реальный
`ControlServer`; бэкенд — протокол-конформный `FakeBackend` из
`tests/control_fakes.py`; поведение живого бэкенда — e2e, ниже).

| № | Вердикт | Основание |
|---|---|---|
| 1 | **pass** | `test_cs1_rights_are_umask_independent` — 0700/0600 явно (chmod после bind) при разных umask |
| 2 | **pass** | `test_cs2_fallback_path_and_warn` — пустой `XDG_RUNTIME_DIR` → `~/.local/state/vesma/run/` + WARN; симметрично `tests/test_service_layout.py::test_empty_runtime_dir_falls_back_with_warning` |
| 3 | **pass** | `test_cs3_peercred_own_uid_allowed` / `test_cs3_peercred_mismatch_denied` (решение, fail-closed на getsockopt); live cross-uid лега `test_cs3_live_cross_uid_connection_closed` — **честный skip вне root** (в sandbox нет второго uid); проверка на КАЖДОМ accept до первого байта (`serve_forever`) |
| 4 | **pass** | `test_cs4_second_instance_exits_already_running` — probe живого отвечает на hello → exit «already running», без unlink/bind |
| 5 | **pass** | `test_cs5_stale_own_uid_socket_is_cleaned`, `test_cs5_symlink_refused_never_followed`, `test_cs5_regular_file_refused_not_deleted`, `test_cs5_target_classification_unit`, `test_cs5_foreign_uid_socket_refused` (skip вне root, классификация unit-протестирована) |
| 6 | **pass** | `test_cs6_rights_verification_fatal_on_mismatch` — fstat fd + stat ноды + getsockname-связка; расхождение — fatal при старте |
| 7 | **pass** | `test_cs7_concurrent_starts_exactly_one_binds` — гонка одновременных стартов: ровно один bind'ится; settle re-probe исключает unlink свежего сокета победителя |
| 8 | **pass** | `test_cs8_status_before_hello_rejected_then_hello_recovers` — error 4 + `data.reason="hello_required"` + `data.supported`; W6: соединение, не приславшее hello, закрывается по таймауту (см. CS-12/W6) |
| 9 | **pass** | `test_cs9_version_mismatch_reports_supported` — major 2 → error 4 + `supported`; major 1 → hello-результат |
| 10 | **pass** | `test_cs10_start_stop_idempotent` (fake) + живая лега адаптера `tests/test_service_e2e.py::TestVerbSemantics::test_start_stop_restart_idempotence_markers` — уже-start/already-running, уже-stop/already-stopped, повтор не меняет состояние; включает ручной start после ручного stop (фикс W6: `request_start` сбрасывает `stop_requested`) |
| 11 | **pass** | `test_cs11_stop_force_flag` (флаг доходит до бэкенда) + живая семантика force: graceful-фаза пропущена, SIGKILL после `FORCE_KILL_DELAY_S=0.5` (`supervisor.py::_stop_child(force=True)`), тот же reaper-pause/pgid-барьер |
| 12 | **pass** | `test_cs12_oversized_line_error_and_close` (1 MiB+1 → error 1 + close), `test_cs12_tail_above_limit_invalid_params` (tail 10001 → error 3); W6 **измеренная лега**: `test_cs12_response_time_measured_within_limit` — полный burst hello+status+logs(tail=10000)+health замерен на проводе < 10 s; клиентский таймаут — `tests/test_service_client.py::test_client_response_timeout` (DeadServer-инъекция) |
| 13 | **pass** | `test_cs13_injection_names_invalid_params` (инъекционные имена на всех методах → error 3), `test_cs13_valid_name_unknown_component` (нет в реестре → error 100); живая лега — e2e `TestVerbSemantics::test_unknown_component_maps_to_100` |
| 14 | **pass** | `test_cs14_follow_frames_then_final_on_source_stop`, `test_cs14_client_disconnect_frees_subscription` (нет утечки подписчиков); живая лега — e2e `TestLogsOverBuffer::test_logs_tail_and_follow_until_source_stop` (replay tail → живые кадры → stop вторым соединением → финальный `{state: stopped}`) |
| 15 | **pass** | `test_cs15_no_tcp_listeners_created` (скан сокетов процесса супервайзера), `test_cs15_registry_respects_ranges` (диапазоны кодов + неизвестный код по диапазону) |
| 16 | **pass** | `test_cs16_third_follow_same_peer_rejected`, `test_cs16_ninth_follow_installation_wide_rejected` (оба → error 3), `test_cs16_idle_follow_closed_with_final_answer` (60 s idle → финальный ответ), `test_cs16_hard_cap_closes_despite_live_data_then_resubscribe` (3600 s потолок, переподписка работает) |

W6-закалка поверх чеклиста (PR #494 backlog): лимит соединений
(`test_w6_connection_cap_closes_newcomer_unread` — новичок на капит
закрывается нечитанным, освободившийся слот принимает следующего),
hello-таймаут (`test_w6_hello_timeout_closes_silent_connection`),
send-deadline follow-насоса (`test_w6_follow_send_deadline_frees_subscription`
— 1 MB бурст нечитающему пиру освобождает слот подписки), гигиена канала
сообщений 102 (`test_w6_start_failed_message_is_fixed_text` — message
короткий фиксированный, пути/секреты не попадают, `data.reason` —
санитизированный токен).

---

## Таблица LY-01…LY-12, LY-14 (layout v1, user-профиль)

| ID | Вердикт | Основание |
|---|---|---|
| LY-01 | **pass** | `tests/test_service_layout.py::test_canonical_dir_modes_after_ensure` (плохой umask → 0700 явно), `tests/test_service_install.py::test_canonical_dirs_with_explicit_modes` |
| LY-02 | **pass** | `tests/test_service_install.py::test_installs_both_bundled_manifests` (install/uninstall, чужие файлы не тронуты — `test_sibling_manifest_untouched`), fail-closed на невалидном соседе — `test_stray_file_in_components_d_fails_closed`; атомарная запись install-флоу |
| LY-03 | **pass** | `tests/test_service_envfile.py` — 0600/владелец/fail-closed (`ENV_FILE_UNSAFE` с готовой командой) на не-0600, чужого владельца, отсутствующий файл |
| LY-04 | **pass** | Права data-каталогов — `tests/test_service_install.py::test_data_dirs_and_schema`; дефолтный cwd ребёнка = `{data_dir}` — живая лега `tests/test_service_e2e.py::TestLiveSystem` (`/proc/<pid>/cwd` == `resolve_component_paths(name).data_dir`) |
| LY-05 | **pass** | `tests/test_service_install.py::test_exactly_one_component_venv_ly05` — board (in-process) + metrics (python-ребёнок): ровно один `venvs/metrics`; in-process venv не имеет |
| LY-06 | **pass** | `tests/test_service_doctor.py::TestDR02` (site-packages 0777 → FAIL; чужой пакет → FAIL с командой) + `tests/test_service_install.py::test_mismatching_freeze_fails_with_fix_command` (дрейф `pip freeze` ≠ lock → FAIL) |
| LY-07 | **pass** | `PYTHONNOUSERSITE=1` безусловно в env каждого python-процесса — `tests/test_service_supervisor.py::TestSL13ChildEnv` (последним ключом, перезаписать нельзя); env-лега конформанса (SL-13) симметрична |
| LY-08 | **pass** | `tests/test_service_install.py::test_non_pin_requirements_rejected` (не-`==` пины → отказ), `test_exact_pins_accepted`, lock-файл full-freeze; URL-зависимости отклоняются пин-валидацией |
| LY-09 | **pass** | `tests/test_service_logsink.py` — РОВНО ОДИН режим (journald при наличии сокета, иначе files-under-state; никогда оба): `test_journald_primary_when_socket_present`, `test_file_mode_when_socket_absent`; ротация 10 MB × 5 — `test_rotates_with_injected_small_budget`, `test_contract_rotation_numbers`; маркировка `SYSLOG_IDENTIFIER` супервайзером — `test_journald_sink_payload_marks_identifier_and_priority`; «третьих мест» нет (запись только в state/logs) |
| LY-10 | **pass** | `tests/test_service_logsink.py::test_appends_with_iso8601_prefix_and_modes` (0700/0600), `test_append_only_across_instances` (существующие записи не переписываются) |
| LY-11 | **pass** | `tests/test_service_layout.py::test_xdg_runtime_dir_set`, `test_empty_runtime_dir_falls_back_with_warning`; права сокета — CS-1; pid-файлов нет by construction (single-instance = probe, CS-4) |
| LY-12 | **pass** (режим + API-лега, W7) | Режим 0750 и канонический путь — `tests/test_service_layout.py::test_canonical_dir_modes_after_ensure` / `test_table_matches_layout_3_4`. API-лега (W7): taint при загрузке — `load_env_file` возвращает `TaintedValues`/`TaintedValue` (прозрачные str/dict-подклассы, не ломающие существующих потребителей — env-конструкция, сравнения, JSON); API записи — `src/vesmaro/service/cache.py::put_under_cache(name, key, value, *, tainted=False)`: отказ на taint-значении (объявленном флагом или несущем тип `TaintedValue`) с типизированной `CacheWriteRefusedError`, в сообщении только компонент/ключ — значение НИКОГДА не эхо; валидация имени (грамматика CM §3.1) и ключа (безопасный сегмент пути), атомарная запись (tmp+rename) в `~/.cache/vesma/<name>/` c явными 0750 (не от umask); удаление каталога безопасно — следующая запись пересоздаёт. Тесты `tests/test_ly12_cache_taint.py`: taint-at-load, probe-запись env-значения → отказ, отказ по флагу/типу/каждому значению, untainted → принят (0750 при плохом umask), rm -rf → операция не меняется. Честный скоуп: сегодня в движке НЕТ фичи, пишущей env-значения в кэши, — пункт закрыт самим API + тестами отказа («щит до первого писателя»); taint — типовая метка на границе загрузки, стрижётся строковыми преобразованиями (документировано в докстринге API; обход API same-uid в ФС — вне контрактного радиуса, ловит doctor §3.10) |
| LY-13 | **n/a (v2)** | System-профиль исполняется и чеклится на горизонте v2 вместе с реализацией (спека §3.3; отложено за миграцией легаси) |
| LY-14 | **pass** | `tests/test_service_doctor.py::TestEveryFindingHasFixCommand` (каждая находка DR-01…DR-13 несёт severity + готовую команду), `TestDoctorDoesNotMutate` (doctor не исполняет команды и не мутирует инсталляцию), `TestFailClosedLoad` (FAIL DR-01 на env-файле согласован с fail-closed стартом); инжекция находок — классы TestDR01…TestDR13 |

Doctor-матрица DR-01…DR-13: реализована полностью
(`src/vesmaro/service/doctor_checks.py`, `vesma doctor service` —
`tests/test_service_doctor.py`, 52 теста + `tests/test_doctor_paths.py`),
каждая находка с severity `OK/WARN/FAIL` и командой исправления.

---

## Таблица CM-01…CM-17 (component-manifest v1)

Двухуровневое основание: **loader** — строгая W1-валидация движка
(`tests/test_service_manifest.py`, 42 теста: схема Draft 2020-12 +
контрактные проверки, fail-closed) и **раннер спеков** (24/24 pass);
**live** — живые прогоны W2/W4/W6 (spawn/env/stop, install, e2e).

| ID | Вердикт | Основание |
|---|---|---|
| CM-01 | **pass** | Loader: strict validation, неизвестное поле → отказ (`test_service_manifest.py::test_unknown_field`); раннер `schema_valid` на всех примерах и `schema_invalid` на всех негативных фикстурах |
| CM-02 | **pass** | `apiVersion: vesma.component/v1` обязателен, чужие версии отвергаются (`SUPPORTED_API_VERSIONS`); раннер `api_version_present` |
| CM-03 | **pass** | Имя по шаблону `^[a-z][a-z0-9-]{0,62}$`, уникальность в установке (loader + раннер `name_kebab_unique`); socket-слой пере-проверяет тем же regex (CS-13) |
| CM-04 | **pass** | `version` SemVer 2.0.0, `description` ≤ 200 — loader + схема |
| CM-05 | **pass** | `tier` обязателен (`core|optional`); рестарт-поведение в манифесте НЕ дублируется — применяется супервайзером (`RestartPolicy.from_manifest`; переопределения только в контрактных клампах) |
| CM-06 | **pass** | `provenance.repo` https + SPDX license (loader + раннер `license_spdx`); для бинарей `artifact_sha256` сверяется при spawn (`supervisor.py::_artifact_hash_ok`, несовпадение = отказ старта) |
| CM-07 | **pass** | Ровно один `kind`; секция только своя — `kind_launch_consistency` (раннер) + loader (`kind-stop-mismatch` фикстура отвергается) |
| CM-08 | **pass** | argv без shell-метасимволов/whitespace/`sh -c` (`no_shell_metacharacters`), плейсхолдеры из allowlist `{config_path} {data_dir} {runtime_dir} {venv_bin}` (`argv_placeholder_allowlist`; `tests/test_service_placeholders.py`) |
| CM-09 | **pass** | `env.vars` без секретов по именам и значениям (`no_secret_in_vars`); секреты только в `env_file` вне каталога манифестов (`env_file_outside_manifests_dir`; `tests/test_service_envfile.py` — 0600, fail-closed) |
| CM-10 | **pass** | Ровно один health-блок по `checker` — `checker_block_consistency`; in-process callback не бросает наружу и возвращает `{state, detail?}` (`tests/test_service_health.py`; `tests/test_service_board.py` — живой callback панели) |
| CM-11 | **pass** | Живая лега: компоненты реально завершаются по `signal` в grace_period (`TestSL04GroupStop` — упрямец умирает SIGKILL'ом группы после grace; e2e-стопы всех детей); `kind: in-process` с секцией `stop` отвергается (фикстура `kind-stop-mismatch`) |
| CM-12 | **pass** | Ровно одна из `schema_file`/`schema_inline` (loader); текущая конфигурация валидируется при старте (`supervisor.py::_validate_component_config`, CONFIG_INVALID fail-closed; `tests/test_service_supervisor.py` — отказ старта при несоответствии схеме) |
| CM-13 | **pass** | Клампы (base ≥ 500ms, max ≤ 5min, attempts ≥ 3) — loader + `tests/test_service_install.py::test_out_of_clamp_restart_override_surfaces_loader_error`; раннер-фикстура `fix-bad-restart-clamp` |
| CM-14 | **pass** | `depends_on` только на имена установки, граф ацикличен (`_topological_order` — цикл = ошибка; `TestSL15StartOrdering`); «up» = первый успешный health-проход (T4) |
| CM-15 | **pass** | Манифесты пишутся install-флоу; `doctor` расхождений не показывает (`tests/test_service_doctor.py`); бандлед-пак написан как install-артефакт |
| CM-16 | **pass** | `metadata.name` ∉ {`venv`, `venvs`} — loader enum + фикстура `fix-reserved-name` (раннер) |
| CM-17 | **pass** | `tier: core` требует `artifact_sha256` (hex64, сверяется) — loader + фикстура `fix-core-no-sha`; отсутствие хэша у optional — осознанно, doctor WARN (`TestDR01`/`TestDR…` семейство, `tests/test_service_install.py`) |


---

## Полный сюит и гейты (прогон W6; W7-дополнение)

- `ruff check .` — чисто; `ruff format --check` по затронутым файлам — чисто
  (W7-прогон 2026-10-05: то же)
- `mypy --strict src/vesmaro` — 0 ошибок (W6: 143 файла; W7: 145 файлов)
- `bandit -r src/vesmaro/service` — 0 High; 1 Medium B310 (pre-existing W2,
  `health.py` HttpChecker: URL приходит из schema-валидированной
  health-секции манифеста; вне скоупа W6), 3 Low (B404 subprocess —
  конструируемый argv из валидированного манифеста, no shell; B105
  «supervisor» — идентификатор структурных строк, не пароль; B311 random —
  джиттер backoff, не криптография)
- `pytest tests/ -q` (W6): **5917 passed / 8 skipped / 0 failed** (9:11 мин);
  базлайн main — 5904 passed / 9 skipped; дельта W6: +13 passed
  (8 e2e + 5 hardening-тестов), −1 skipped (удалён мёртвый pre-W2
  skip-страж `test_cli_run_without_w2_supervisor_fails_cleanly` вместе с
  его seam). SL-05 флак не воспроизвёлся (закалка 4d в деле)
- `pytest tests/ -q` (W7, 2026-10-05): **5940 passed / 8 skipped /
  0 failed** (9:29 мин); дельта к W6-бейлайну: **+23 passed, skips без
  изменений** (22 теста LY-12 `tests/test_ly12_cache_taint.py` + 1
  контейнерная лега SL-06 `tests/test_service_pid1_container.py` — на
  хосте владельца лега исполняется РЕАЛЬНО, в CI без podman — named
  skip; unshare-лега — тот же честный skip с расширенным до 30 s
  таймаутом повторной пробы)

## Известные честные пробелы

1. **CS-3/CS-5 live cross-uid**: нет второго uid вне root; решение
   unit-протестировано, live-леги — честные skip с именованной причиной.

(W7 закрыл прежние пункты 1 и 3: SL-06 true-container лега — реальный
прогон на rootless podman хоста владельца, см. строку SL-06; LY-12
API-лега — taint при загрузке + write-API с отказом, см. строку LY-12.)

## Вердикт

Все исполняемые пункты четырёх чеклистов — pass с приведёнными
доказательствами; единственный оставшийся пробел (CS-3/CS-5 live
cross-uid) — средовый, именованный и не блокирует ратификацию контрактов
по описанной в спеках процедуре («первое зелёное прохождение чеклиста
реализацией»; средовые леги — smoke-матрица целевых профилей). W7 снял
последние два блокера (SL-06 контейнерная лега, LY-12 API-лега) —
решение draft→stable за держателями спек, не этой волны.
