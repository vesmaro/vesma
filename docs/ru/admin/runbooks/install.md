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

Скриптовый вариант (venv в `~/.mnemos/venv` + лаунчер в `~/.local/bin` +
опциональная проводка VS Code):

```bash
curl -fsSL https://raw.githubusercontent.com/vesmaro/vesmaro/main/scripts/install.sh | bash
```

> ⚠️ **Имена.** Пакет на PyPI — `vesma` (голый слот, наш — основной канал;
> `pip install vesma` ставит этот проект). Доребрендинговый
> `mnemos-memory-server` живёт до deprecation (заморожен на 5.2.0),
> `vesma-memory-server` — наше живое зеркало. Скриптовый установщик выше
> ставит `vesma` и опрашивает тот же канал для последней версии — зеркало
> используется только как fallback с предупреждением. Таблица каналов:
> [ранбук публикации в PyPI](pypi-publish.md).

## Конфигурация

Конфиг по умолчанию — `~/.mnemos/config.yaml` (опционально — значений по умолчанию
достаточно). Минимальный вариант:

```yaml
mnemos:
  data_dir: ~/.mnemos/data
  vault_path: ~/.mnemos/vault
  strict_tag_contract: true
embedding:
  provider: nano  # vesma-embed-v1 — встроенная локальная модель, работает офлайн; или onnx, ollama
```

Хранилище: `~/.mnemos/data/mnemos.db` (SQLite, WAL). Зеркало vault:
`~/.mnemos/vault/` (Obsidian-совместимый markdown).

## Запуск MCP-сервера

Добавьте в VS Code **User** или **Workspace** `mcp.json`:

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

Пресеты по харнесам (Claude Code, Cursor, OpenCode, Codex, Windsurf, ZCode, pi,
Hermes): [`integrations/mcp-presets.md`](../../../../integrations/mcp-presets.md).
Поведенческий пакет (инструкции / скиллы / промпты): `vesma integration setup`.

## Запуск HTTP API

```bash
vesma serve  # uvicorn на 127.0.0.1:8787
```

## Контейнер

Полное контейнерное развёртывание (compose, Kubernetes, systemd quadlet) — см.
[ранбук container-deployment.md](container-deployment.md).

Быстрый запуск одиночного контейнера из выпущенного образа:

```bash
podman run -d -v vesma-data:/data -v vesma-vault:/vault -p 8787:8787 \
  --env MNEMOS_API__TOTP_MASTER_KEY=<your-key> ghcr.io/vesmaro/vesma:4.3.0
```

Или через compose из корня репозитория:

```bash
podman-compose up -d
```

## Обновление

```bash
pip install --upgrade vesma
```

Схема хранилища мигрирует автоматически при первом запуске новой версии.
Делайте бэкап `~/.mnemos/data/` перед мажорными обновлениями — см.
[backup-restore.md](backup-restore.md).

## Проверка

```bash
vesma add "Hello Vesma" --tags "project:test,agent:manual,mnemos:learning"
vesma search "Hello"
vesma recall agent manual --project test
```
