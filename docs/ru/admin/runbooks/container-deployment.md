# Контейнерное развёртывание

**🌐 Language / Язык:** [English](../../../en/admin/runbooks/container-deployment.md) · Русский

> Runbook уровня администратора для запуска Vesma в контейнере. **Опубликованный
> образ — основной путь: достаточно его скачать, локальная сборка не нужна.**
> Сборка из исходников — фолбэк для разработки, собственных патчей или
> air-gapped-сред. Для настоящих кластеров K8s/K3s используйте helm-чарт —
> [kubernetes-deployment.md](../kubernetes-deployment.md).

---

## Обзор

Один опубликованный образ — `ghcr.io/vesmaro/vesma` — покрывает все пути. Выбирайте по среде:

| Путь | Инструмент | Когда использовать |
|------|-----------|-------------------|
| Скачать и запустить | podman / docker | Самый быстрый старт — одна команда, репо не нужно |
| docker/podman-compose | Compose | Production на одном хосте — [`deploy/docker/`](../../../../deploy/docker/) |
| Helm-чарт | Helm 3 + K8s/K3s | Настоящие кластеры — [kubernetes-deployment.md](../kubernetes-deployment.md) |
| `podman kube play` | podman | Kubernetes-подобный pod на одном хосте |
| systemd quadlet | podman + systemd | Постоянный user-сервис с автоматическим перезапуском |
| Сборка из исходников | podman / buildah | Фолбэк: разработка, патчи, air-gapped |

Контейнер открывает **порт 8787** и использует два named volume — `vesma-data` (SQLite + векторный
индекс) и `vesma-vault` (Obsidian markdown mirror); путь compose называет их `vesma-data`/`vesma-vault`.

---

## Предварительные требования

- **podman** ≥ 4.0 (rootless-режим полностью поддерживается) или **docker**
- **podman-compose** или **docker compose** — только для пути через compose
- Python и `git` на хосте **не требуются** — всё запускается внутри контейнера
- Pull анонимный — опубликованный пакет **публичный**; `podman/docker
  login ghcr.io` нужен только при упоре в rate-limit

---

## Запуск — готовый образ (быстрее всего)

Скачайте опубликованный образ и запустите сразу — собирать ничего не нужно:

```bash
podman pull ghcr.io/vesmaro/vesma:4.3.0      # :latest указывает на свежий релиз
podman run -d --name vesma \
  -v vesma-data:/data -v vesma-vault:/vault \
  -p 8787:8787 \
  --env MNEMOS_API__TOTP_MASTER_KEY=<your-key> \
  ghcr.io/vesmaro/vesma:4.3.0
```

`docker` работает идентично — замените `podman` на `docker`. В образ встроен
`config.container.yaml` как `/app/config.yaml` — монтировать конфиг не требуется,
если только вы не хотите переопределить настройки. TOTP-мастер-ключ обязателен
(вшитый конфиг биндится на `0.0.0.0`); образы 4.x читают написание
`MNEMOS_API__*`, 5.0–5.2 — `VESMARO_API__*`, а 5.3+ — каноническое
`VESMA_API__*`; задать все сразу всегда безопасно (устаревшие написания
принимаются до 6.0, ADR-0031).

Проверка:

```bash
curl -fsS http://localhost:8787/health    # → {"status":"ok"}
```

---

## Запуск — compose (production на одном хосте)

Готовый compose-файл лежит в [`deploy/docker/`](../../../../deploy/docker/) и использует
опубликованный образ — без сборки:

```bash
cd deploy/docker
cp .env.example .env          # затем впишите: TOTP_MASTER_KEY=$(openssl rand -hex 32)
docker compose up -d          # или: podman-compose up -d
podman-compose logs -f vesma
podman-compose down
```

Детали (env-файл, фиксация тега образа, Ollama sidecar): [deploy/docker/README.md](../../../../deploy/docker/README.md).

Ollama sidecar (опциональные embeddings):

```bash
docker compose --profile ollama up -d
docker exec vesma-ollama ollama pull nomic-embed-text
```

Чтобы активировать Ollama как провайдер эмбеддингов, задайте `embedding.provider: ollama`
в конфиге контейнера (см. [Конфигурация](#конфигурация)).

> Корневой [`compose.yaml`](../../../../compose.yaml) тоже использует опубликованный образ —
> он сохраняет исторические имена ресурсов `vesma-*` для существующих пользователей
> podman-compose. Про сборку из исходников см.
> [Сборка из исходников](#сборка-из-исходников-фолбэк).

---

## Запуск — Kubernetes / K3s (кластер)

Используйте helm-чарт — он разворачивает опубликованный образ с ингрессом, TLS
и постоянным хранилищем:

```bash
helm install vesma deploy/helm/vesma \
  --namespace vesma --create-namespace \
  --set auth.totpMasterKey="$(openssl rand -hex 32)" \
  --set ingress.className=traefik \
  --set 'ingress.hosts[0].host=vesma.example.com'
```

Полное руководство по values, TLS и разбору неполадок:
**[kubernetes-deployment.md](../kubernetes-deployment.md)**.

---

## Запуск — Kubernetes-подобный pod (podman kube play)

Vesma поставляется с Kubernetes-подобным манифестом pod'а (`deploy/podman/kube/mnemos-pod.yaml`)
для `podman kube play`. Манифест скачивает опубликованный образ, инжектит TOTP-ключ из
podman-секрета и определяет пробы здоровья.

### Запуск

```bash
printf 'MNEMOS_API__TOTP_MASTER_KEY=<your-key>\nVESMARO_API__TOTP_MASTER_KEY=<your-key>\nVESMA_API__TOTP_MASTER_KEY=<your-key>\n' \
  | podman secret create vesma-totp -
podman volume create vesma-data
podman volume create vesma-vault
podman kube play deploy/podman/kube/mnemos-pod.yaml
```

Shortcut (создаёт volumes автоматически перед запуском манифеста):

```bash
./scripts/deploy.sh kube-up
```

### Остановка

```bash
podman kube down deploy/podman/kube/mnemos-pod.yaml
```

Shortcut:

```bash
./scripts/deploy.sh kube-down
```

---

## Запуск — systemd (quadlet)

Путь через quadlet устанавливает systemd **user**-юнит и управляет контейнером как постоянным
сервисом. Юнит ссылается на опубликованный `ghcr.io/vesmaro/vesma:4.3.0`, образ скачивается
автоматически; для локальной сборки соберите образ заранее (см.
[Сборка из исходников](#сборка-из-исходников-фолбэк)) и укажите
`Image=localhost/mnemos:latest` в юните.

### Задать TOTP-ключ

Юнит читает ключ из `~/.vesmaro.env` (`EnvironmentFile`), править юнит не нужно.
Все имена переменной должны нести одно значение — образы 4.x читают `MNEMOS_API__*`,
5.0–5.2 читают `VESMARO_API__*`, 5.3+ читают каноническое `VESMA_API__*` (ADR-0031):

```bash
KEY=$(openssl rand -hex 32)
printf 'MNEMOS_API__TOTP_MASTER_KEY=%s\nVESMARO_API__TOTP_MASTER_KEY=%s\nVESMA_API__TOTP_MASTER_KEY=%s\n' "$KEY" "$KEY" "$KEY" > ~/.vesmaro.env
```

### Установка юнита

```bash
./scripts/deploy.sh quadlet
```

Копирует `deploy/podman/quadlet/mnemos.container` в `~/.config/containers/systemd/` и выполняет
`systemctl --user daemon-reload`.

### Запуск и автозапуск

```bash
systemctl --user start vesma
systemctl --user enable vesma   # автозапуск при входе в систему
```

### Проверка статуса

```bash
systemctl --user status vesma
```

---

## Сборка из исходников (фолбэк)

> Нужна только для разработки, собственных патчей или air-gapped-сред.
> Опубликованный образ синхронизируется с каждым релизом — конечным
> пользователям этот раздел не нужен.

```bash
podman build -t localhost/vesma:4.3.0 -f Containerfile .
```

`Containerfile` использует `python:3.12-slim` в качестве базового образа, устанавливает пакет (MCP SDK едет в core),
копирует `config.container.yaml` как `/app/config.yaml` и задаёт serve-команду на
порту 8787.

Shortcut через Makefile (собирает `localhost/mnemos:latest`):

```bash
make build-image
```

Хелпер делает то же самое:

```bash
./scripts/deploy.sh build
```

**Залитие в ghcr.io (мейнтейнеры):** релизный конвейер (`scripts/local-release.sh`)
при каждом релизе пушит версионный тег и `:latest` — GitHub Actions отключены, и этот
скрипт является каноническим путём (см. [ci-cd.md](ci-cd.md)). Конвейер таргетит
публичное имя `ghcr.io/vesmaro/vesma` напрямую.
Ручное залитие, если когда-нибудь понадобится (PAT с правом `write:packages`):

```bash
podman login ghcr.io
podman tag localhost/vesma:4.3.0 ghcr.io/vesmaro/vesma:4.3.0
podman push ghcr.io/vesmaro/vesma:4.3.0
podman push ghcr.io/vesmaro/vesma:latest
```

---

## Конфигурация

Vesma использует `config.container.yaml` в качестве конфига контейнера. Файл:

- Встроен в образ при сборке как `/app/config.yaml`
- Перекрывается монтированием своего конфига по тому же пути (read-only)

Ключевые настройки:

| Параметр | Значение | Примечания |
|---------|---------|-----------|
| `mnemos.data_dir` | `/data` | Маппится на named volume `vesma-data` |
| `mnemos.vault_path` | `/vault` | Маппится на named volume `vesma-vault` |
| `api.host` | `0.0.0.0` | Привязка ко всем интерфейсам — **требует auth** |
| `api.port` | `8787` | Внутренний порт контейнера; маппинг задаётся в compose/run |
| `api.auth_enabled` | `true` | Обязательно `true` при `host: 0.0.0.0` |
| `api.totp_enabled` | `true` | Требует TOTP 2FA; ключ через `MNEMOS_API__TOTP_MASTER_KEY` (+ `VESMARO_API__*` на 5.0–5.2, канон `VESMA_API__*` с 5.3 — ADR-0031) |
| `api.behind_tls_proxy` | `true` | TLS завершается выше по стеку (Caddy, nginx, ingress и т.п.) |
| `embedding.provider` | `nano` | vesma-embed-v1: встроенная локальная модель, работает офлайн; GPU не требуется |

### Требования безопасности

Привязка к `0.0.0.0` **требует** одновременно `auth_enabled: true` и `totp_enabled: true`.
TOTP-мастер-ключ должен передаваться через env-переменную — он никогда не должен
присутствовать в файле конфигурации или в любом коммитируемом файле.

Размещайте Vesma за TLS-терминирующим реверс-прокси (Caddy, nginx, ingress и т.п.).
Задайте `trusted_proxies` с CIDR-диапазоном вашего прокси, чтобы заголовки `X-Forwarded-For`
доверялись корректно.

Полную модель угроз и детали конфигурации аутентификации см. в [../security.md](../security.md).

### Провайдер эмбеддингов

- **По умолчанию**: `nano` — встроенная локальная ONNX-модель `vesma-embed-v1` (без torch, без GPU, работает офлайн; внешние провайдеры вроде `onnx`/`sentence-transformers` остаются доступны)
- **Ollama sidecar**: задайте `embedding.provider: ollama` и `embedding.ollama_url: http://ollama:11434`
  (см. раздел compose выше)

---

## Здоровье и операции

### Healthcheck контейнера

Healthcheck'и опрашивают неаутентифицированный HTTP-эндпоинт `/health` (и compose,
и quadlet) — без зависимости от CLI. Проверить текущее состояние:

```bash
podman inspect --format '{{.State.Health.Status}}' vesma
```

### Обзор статуса

```bash
./scripts/deploy.sh status
```

Выводит запущенные контейнеры (имя, статус, порты) и named volumes.

### Доступ через shell

```bash
./scripts/deploy.sh shell
# эквивалентно: podman exec -it vesma /bin/bash
```

### Запуск CLI внутри контейнера

```bash
./scripts/deploy.sh cli search "hello"
# эквивалентно: podman exec vesma vesma search "hello"
```

---

## См. также

- [kubernetes-deployment.md](../kubernetes-deployment.md) — helm-чарт для настоящих кластеров K8s/K3s
- [`deploy/README.md`](../../../../deploy/README.md) — все пути развёртывания одним взглядом
- [install.md](install.md) — установка на bare-metal / в virtualenv
- [../security.md](../security.md) — модель угроз, аутентификация, SSRF-защита
- [../../user/getting-started.md](../../user/getting-started.md) — руководство по первому запуску

---

_Исходные файлы: `Containerfile`, `compose.yaml`, `config.container.yaml`, `scripts/deploy.sh`,
`deploy/podman/quadlet/mnemos.container`, `deploy/podman/kube/mnemos-pod.yaml`,
`deploy/docker/`, `deploy/helm/vesma/`_
