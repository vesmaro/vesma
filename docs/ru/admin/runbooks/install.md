# Runbook: Установка Vesma

**🌐 Language / Язык:** [English](../../../en/admin/runbooks/install.md) · Русский

## Предварительные требования

- Python 3.11+ (wheel — чистый Python плюс встроенная ONNX-модель, этап сборки не нужен)
- `pip` (или `uv` / `pipx` для изолированных установок)
- Опционально: `ollama` для внешнего LLM-обогащения (для хранения и поиска не нужен никогда)

## Быстрая установка (PyPI)

```bash
pip install vesma
```

- MCP-сервер входит в базовый пакет — `vesma mcp-server` работает из коробки (ADR-0023).
- Модель эмбеддингов (`vesma-embed-v1`) встроена: без скачиваний, работает офлайн.

Изолированный вариант (кладёт CLI `vesma` в `PATH`, проектные окружения не затрагиваются):

```bash
uv tool install vesma
# или
pipx install vesma
```

Другие каналы: npm (`npm install -g @vesmaro/vesma`) и ghcr-контейнер — см. блок
«Контейнер» ниже. Обновление уже установленного Vesma — силами самой утилиты:
`vesma update apply` (раздел «Обновление» ниже).

> ⚠️ **Имена.** Пакет на PyPI — `vesma` (голый слот, наш — основной канал;
> `pip install vesma` ставит этот проект). Доребрендинговый
> `mnemos-memory-server` живёт до deprecation (заморожен на 5.2.0),
> `vesma-memory-server` — наше живое зеркало-алиас. Таблица каналов:
> [ранбук публикации в PyPI](pypi-publish.md).

> **venv — только install-флоу.** Ручное создание venv не входит ни в один рабочий
> сценарий: сервисный слой ожидает venv, созданный `vesma service install`
> (`~/.local/share/vesma/venv/` и `venvs/<name>/`). Старые ручные venv — легаси,
> их вычистка описана в [getting-started.md](../../user/getting-started.md#вычистка-старых-установок).

## Конфигурация

Конфиг по умолчанию — `~/.mnemos/config.yaml` (опционально — значений по умолчанию
достаточно). Минимальный вариант:

```yaml
vesma:
  data_dir: ~/.mnemos/data
  vault_path: ~/.mnemos/vault
  strict_tag_contract: true
embedding:
  provider: nano  # vesma-embed-v1 — встроенная локальная модель, работает офлайн; или onnx, ollama
```

Хранилище: `~/.mnemos/data/mnemos.db` (SQLite, WAL). Зеркало vault:
`~/.mnemos/vault/` (Obsidian-совместимый markdown).

## Сервис (рекомендуется для постоянной работы)

```bash
vesma service install                         # манифесты, data-каталоги, venv, юнит systemd user
systemctl --user enable --now vesma.service   # автозапуск
vesma service status && vesma service health  # живое состояние и вердикт здоровья
```

Без systemd — `vesma service run` (супервайзер на переднем плане). Полный цикл
глаголов (start/stop/restart/logs/uninstall) и диагностика установки
(`vesma doctor service`) — в [getting-started.md](../../user/getting-started.md#сервис-vesma-service).

## Подключение MCP

**Ручная настройка MCP отменена** — не добавляйте блоки в `mcp.json` и родные
конфиги харнесов руками. Единственный путь — утилита:

```bash
vesma integration setup              # все обнаруженные харнесы + wiring агентов, идемпотентно
vesma integration setup -t copilot   # только один харнес
```

После обновления пакета освежите развёрнутое: `vesma integration update`.
Копипаст-блоки для нестандартных харнесов (fallback, не основной путь):
[`integrations/mcp-presets.md`](../../../../integrations/mcp-presets.md).

## Запуск HTTP API

```bash
vesma serve  # uvicorn на 127.0.0.1:8787
```

Когда работает сервис-супервайзер, ядро HTTP API уже встроено в него — отдельный
`vesma serve` не нужен.

## Контейнер

Полное контейнерное развёртывание (compose, Kubernetes, systemd quadlet) — см.
[ранбук container-deployment.md](container-deployment.md).

Быстрый запуск одиночного контейнера из выпущенного образа:

```bash
podman run -d -v vesma-data:/data -v vesma-vault:/vault -p 8787:8787 \
  --env VESMA_API__TOTP_MASTER_KEY=<your-key> ghcr.io/vesmaro/vesma:6.0.0  # 6.0.0: единственное читаемое написание (старые префиксы выведены)
```

Или через compose из корня репозитория:

```bash
podman-compose up -d
```

## Обновление

```bash
vesma update check    # отчёт по всем поверхностям, ничего не меняет
vesma update apply    # pip user-site (+ npm best-effort), без промптов
```

`apply` трогает только pip user-site (и npm, если установлен); прод-венвы,
Go-бинарники и контейнеры никогда не обновляются автоматически. Недельная
автоматизация: `vesma update timer install` (systemd user-таймер). Полная карта
подкоманд — в [getting-started.md](../../user/getting-started.md#подкоманды-vesma-update).

Схема хранилища мигрирует автоматически при первом запуске новой версии.
Делайте бэкап `~/.mnemos/data/` перед мажорными обновлениями — см.
[backup-restore.md](backup-restore.md).

## Проверка

```bash
vesma add "Hello Vesma" --tags "project:test,agent:manual,vesma:learning"
vesma search "Hello"
vesma recall agent manual --project test
vesma doctor          # база здоровья: конфиг, хранилище, MCP-транспорт, регистрации
vesma doctor service  # сервис-установка по контракту layout v1 (DR-01…DR-13)
```
