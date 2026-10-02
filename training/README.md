# training/ — инфраструктура обучения нано-модели (NM-1a/b)

Каталог training-инфраструктуры трека нано-модели (ADR-0021,
`docs/project/nano-model-plan.md` §3). Этап **NM-1a** (этот): датасет-преп,
скрипт дистилляции, экспорт int8 ONNX, eval-джига, one-command пайплайн.
Этап **NM-1b** (прогон владельцем): фактическая дистилляция 45–60M-студента
поверх учителя, калибровка int8, артефакт ≤60 МБ.

Анти-скоуп ADR-0021: **обучение отделено от рантайма**. Ничего из
`training/` не попадает в wheel/sdist (исключение в `pyproject.toml`), не
импортируется из `src/vesma/`, не запускается в CI (CI проверяет только
хеш артефакта). Зависимости обучения объявлены в
`training/requirements.txt`, НЕ в `pyproject.toml`.

Язык: русский, по стилю `docs/project/dev-plan.md`. Отчёты eval — в
`benchmarks/reports/` (gitignored).

## Карта файлов

| Путь | Назначение |
| --- | --- |
| `dataset/prepare_dataset.py` | Сбор memory-shaped RU+EN корпуса (фикстуры репо + синтетика + опц. локальный стор: `--from-mnemos-dir` / `--from-mnemos-db` SQLite read-only с project-фильтром), дедуп, лимит 256 токенов, RU-квота ≥40 %, train/val 95/5 (seed=42) |
| `dataset/synthetic_templates.py` | Программные RU+EN шаблоны (заметки/чат-выдержки/код-заголовки), 50+ шаблонов × вариации |
| `distill.py` | Дистилляция студента (KD-loss: MSE на cos-similarity к учителю, temperature-скейл; round 3: Qwen3-учитель с instruct-префиксом и last-token pooling, MRL-головы `--mrl-dims`), чекпоинты per-epoch, детерминизм |
| `export_onnx.py` | Экспорт студента в ONNX (opset пин, static shapes 1×256), int8 static PTQ, `manifest.json` с fingerprints + `mrl_dims` |
| `eval_distilled.py` | Eval-джига: cos-sim студент/учитель на val; retrieval-proxy recall@5 против BM25-эталона на judged-корпусе; сводка JSON+markdown |
| `run_pilot.sh` | One-command пайплайн NM-1b: prepare → distill → export → eval |
| `requirements.txt` | Зависимости обучения (torch, matplotlib для отчётов и пр.) — вне рантайма |
| `logs/` | Логи прогонов (gitignored) |

Канонические отчёты бенчмарков (PNG + анализ) —
`benchmarks/reports/generate_report.py` → `benchmarks/reports/canonical/`
(см. `benchmarks/reports/README.md`, retention-политика).

## Требования (Silverblue-специфика)

ОС хоста — Fedora Silverblue (неизменяемый корень): **всё выполняется в
контейнере**, ничего не ставим на хост. Контейнер toolbox/distrobox с
Python 3.12 и torch+cpu:

```bash
# одноразово: контейнер с training-окружением
toolbox create mnemos-training -c fedora:40   # или distrobox create
toolbox enter mnemos-training
cd /path/to/mnemos
python3 -m pip install -r training/requirements.txt
```

Модель-учитель скачивается при прогоне NM-1b у владельца (см. ниже) —
**никаких сетевых загрузок моделей в CI или сессии подготовки**.

## Запуск

Все команды — из корня репо внутри контейнера.

### 1. Датасет (NM-1a; без сети, без torch)

```bash
# полный корпус до 100k пар (по умолчанию)
python3 training/dataset/prepare_dataset.py \
  --out-dir training/data --seed 42

# с приватной выгрузкой локального стора владельца (данные не выходят с машины)
python3 training/dataset/prepare_dataset.py --from-mnemos-dir ~/.mnemos/data

# прямо из живой SQLite-базы мнемоса (read-only, только поле content;
# опциональный фильтр по project:-тегам)
python3 training/dataset/prepare_dataset.py \
  --from-mnemos-db ~/.mnemos/data/mnemos.db \
  --mnemos-db-projects "project-mnemos,project-atlas" \
  --mnemos-db-limit 20000

# кап по числу пар (смоук)
python3 training/dataset/prepare_dataset.py --max-pairs 500 --out-dir /tmp/nm1a-smoke
```

`--from-mnemos-db` открывает базу в режиме `file:…?mode=ro` (сервер может
продолжать работать): в обучающий пул попадает ТОЛЬКО `content` (нарезка
на абзацы ≥40 символов, как у dir-коллектора); `tags`/`project` читаются
исключительно для фильтра `project:*` и в корпус не попадают. Приватность —
та же, что у `--from-mnemos-dir`: локально, без сети, данные не покидают
машину.

Выход: `train.jsonl` / `val.jsonl` (95/5, seed=42 детерминизм), строки
`{"text", "lang", "source"}`, fingerprint печатается в stdout.

### 2. Дистилляция (NM-1b; torch; CPU/iGPU через IPEX, иначе CPU-потоки)

Учитель по умолчанию — `Qwen/Qwen3-Embedding-0.6B` (round 3): Apache-2.0,
596M параметров, нативная размерность 1024 с MRL-поддержкой 32-1024. Ключевые
механики (автоматически, см. `--teacher-pooling auto`):

- **last-token pooling + left padding** — официальная геометрия Qwen3-Embedding
  (causal LM); BERT-класс учителей остаётся на mean-pooling;
- **instruction-префикс на query-стороне** — Qwen3-Embedding требует
  `Instruct: <task>\nQuery: <text>` для запросов; при дистилляции корпусные
  тексты проходят через query-сторону учителя:

```bash
python3 training/distill.py \
  --teacher Qwen/Qwen3-Embedding-0.6B \
  --teacher-instruct-template "Given a memory note, retrieve similar notes" \
  --student-init cointegrated/rubert-tiny2 \
  --pairs training/data/train.jsonl --val training/data/val.jsonl \
  --epochs 3 --batch-size 32 --threads 4 --out-dir training/runs/nm1b-r3
```

`--teacher-instruct-template` принимает либо голое описание задачи
(оборачивается в канонический `Instruct: …\nQuery: …`), либо свободный
шаблон с плейсхолдером `{text}`. Без флага текст уходит учителю как есть
(поведение MiniLM-учителей round 1-2).

**MRL-головы (Matryoshka, тренд-фича round 3)** — студент учится сразу на
несколько размерностей, loss = взвешенная сумма KD по каждой (срез первых
`d` компонент у студента и учителя, L2-ренормализация среза):

```bash
python3 training/distill.py --mrl-dims "64,128,256,384" \
  --mrl-weights "4,2,1,1"   # опционально; default — равные веса
```

- одна модель — четыре размерности на инференсе (срез + L2-ренорм);
- экспорт — ОДНОЙ моделью на полную размерность (`--embed-dim`, default
  384), обученные срезы фиксируются в `manifest.json` как `mrl_dims`
  (читаются из `mrl_dims.json` в чекпоинте);
- KD-цель по умолчанию — срез учителя 1024→384 + ренормализация: это
  валидная целевая геометрия ТОЛЬКО потому, что Qwen3-Embedding сам
  MRL-обучен; для не-MRL учителей (MiniLM) держите `--mrl-dims 384`
  (default = обычный режим, численно идентичен round 2).

Прочее без изменений: чекпоинты каждую эпоху (`epoch<N>/`, +
`mrl_dims.json`), метрики per-epoch в `metrics.jsonl` (при MRL — cos-sim
по каждой размерности), детерминизм seed, `--max-pairs 100 --epochs 1` —
смоук.

### 3. Экспорт ONNX + int8 PTQ (после дистилляции)

```bash
python3 training/export_onnx.py \
  --run-dir training/runs/nm1b --out-dir training/runs/nm1b/onnx \
  --calib-samples 200
```

Выход: `model.onnx` (static shapes 1×256, opset пин в константе), +
`tokenizer.json`, + `manifest.json` (base_teacher, student_params,
dataset_fingerprint sha256 jsonl, weights_sha256 пост-экспорт, opset,
created, license).

### 4. Eval-джига

```bash
python3 training/eval_distilled.py \
  --run-dir training/runs/nm1b --onnx-dir training/runs/nm1b/onnx \
  --report-dir benchmarks/reports
```

- (а) cos-sim студента к учителю на val: среднее/медиана/распределение;
- (б) retrieval-proxy recall@5 на judged-корпусе (`benchmarks/corpus`):
  студент против BM25-лексического эталона; сравнительная таблица
  студент/учитель/chromadb-MiniLM.
- Выход: JSON + markdown-сводка в `benchmarks/reports/`.

### 5. Пайплайн пилота (one command)

```bash
THREADS=4 MAX_PAIRS=100000 EPOCHS=3 bash training/run_pilot.sh
```

env-ручки: `THREADS` (потоки torch, default: половина ядер),
`MAX_PAIRS` (кап датасета, default 100000), `EPOCHS` (default 3).
Логи — `training/logs/pilot-<ts>.log`; таймауты в шапке скрипта.

### Тесты

```bash
/usr/bin/python3.12 -m pytest tests/test_training_dataset.py tests/test_training_round3.py -q -p no:cacheprovider
```

Smoke-тесты датасет-препа и manifest-схемы без torch: детерминизм seed,
RU-квота, дедуп, лимит 256 токенов. Round-3 юниты
(`test_training_round3.py`): MRL-парсинг/агрегация (numpy-фекта вместо
torch), instruct-template, last-token pooling, `--from-mnemos-db` (мок
SQLite), детект `mrl_dims` при экспорте. Тяжёлые импорты
(torch/transformers) мокаются; если torch в окружении нет — тесты
скипаются с причиной.

## Политика параллельной работы (ноутбук)

- Прогон — **в фоне** (`nohup`/`tmux`/`toolbox run -t … &`), не в
  интерактивной сессии агента: дистилляция занимает часы.
- Троттлинг потоков: `--threads` ≤ половина физических ядер; в пайплайне —
  env `THREADS`. Меньше потоков = холоднее ноутбук = стабильнее.
- **Питание/крышка**: ноутбук подключить к питанию, крышку НЕ закрывать
  (или `loginctl inhibit-sleep` / настройка suspend-при-закрытии-крышки
  для сессии прогона). Уход в suspend убивает прогон — чекпоинты
  per-epoch смягчают потерю, но эпоха перезапустится заново.

## Политика приватности и лицензий

- `--from-mnemos-dir` / `--from-mnemos-db` читают только **локальный** стор
  владельца; данные не покидают машину (ни сети, ни выгрузок — прогон
  NM-1b автономен).
- Учитель — только Apache-2.0/MIT-лицензии. Кандидаты (размеры проверены по
  HF API, safetensors total, 2026-09-03):

| Модель (HF) | Лицензия | Параметры | dim | Комментарий |
| --- | --- | --- | --- | --- |
| `Qwen/Qwen3-Embedding-0.6B` | Apache-2.0 | 595.8M | 1024 (MRL 32-1024) | **дефолт round 3**: MTEB-MM 64.33; last-token pooling + instruct-префикс |
| `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | Apache-2.0 | 117.7M | 384 | дефолт round 1-2; остаётся через `--teacher` |
| `sentence-transformers/LaBSE` | Apache-2.0 | ~471M | 768 | сильнее на переводных парах, тяжелее |

Выбор — параметр `--teacher` (у `distill.py` и `export_onnx.py` через
manifest `base_teacher`).

### Кандидаты на init студента 45-60M (round 3, проверено по HF API)

Цель NM-1b — студент 45-60M. Проверенные pre-trained мультиязычные
кандидаты (HTTP 200 + safetensors total, 2026-09-03):

| Модель (HF) | Лицензия | Параметры | Вердикт для init |
| --- | --- | --- | --- |
| `cointegrated/rubert-tiny2` | MIT | 29.4M | **остаётся дефолтом** — единственный <100M, RU-сильный |
| `intfloat/multilingual-e5-small` | MIT | 117.7M | ближайший мультиязычный, но 2× сверх бюджета 60M |
| `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | Apache-2.0 | 117.7M | 118M — сверх бюджета |
| `intfloat/multilingual-e5-base` | MIT | 278.0M | слишком большой для init |
| `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` | Apache-2.0 | 278.0M | слишком большой для init |
| `cointegrated/LaBSE-en-ru` | Apache-2.0 | 129.0M | 129M — сверх бюджета |

**Рекомендация round 3**: в окне 45-60M pre-trained мультиязычных
чекспоинтов НЕТ (все проверенные >100M). Остаёмся на `rubert-tiny2`
(29.4M, MIT) как init и растим качество за счёт корпуса
(`--from-mnemos-db` — реальные данные) и более сильного учителя
(Qwen3-Embedding). Флаг `--student-init` — для экспериментов: если
качество встанет, следующий кандидат — `intfloat/multilingual-e5-small`
(117.7M), с осознанным превышением бюджета размера (компенсируется
int8-квантизацией при экспорте).

## Статус NM-1a / NM-1b

- NM-1a (скелет): этот каталог; детерминированность скриптов,
  smoke-тесты, prepare без сети — готово.
- NM-1b (прогон владельцем): скачивание учителя, полный корпус 100k,
  фактическая дистилляция, int8-калибровка, eval-отчёт с порогом
  provisional (cos-sim ≥0.95 к учителю; retrieval-proxy не ниже
  учитель − 2 %) — отложено на прогон у владельца (GPU-хост по §4 плана).


## Бинарник `mnemos-train`

После `pip install -e ".[training]"` (editable — репозиторий должен быть на диске) доступна команда `mnemos-train` — та же самая точка входа, что `python training/train.py`:

```bash
mnemos-train status
mnemos-train prepare --max-pairs 100000
mnemos-train train --epochs 3
mnemos-train snapshot good-checkpoint
mnemos-train status && mnemos-train export && mnemos-train eval
mnemos-train stop   # мягкая остановка на границе эпохи
mnemos-train doctor
```

Важно: `training/` исключён из wheel по ADR-0021 (обучение вне рантайма), поэтому `mnemos-train` работает только там, где есть репозиторий (editable-установка, хост, toolbox/distrobox-контейнер с примонтированным репо). Entry point — обёртка `mnemos.train_entry`, которая при чистой wheel-установке сервера завершается с внятным fail-loud сообщением (код 3), а не ModuleNotFoundError.
