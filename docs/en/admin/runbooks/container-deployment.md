# Container Deployment

**🌐 Language / Язык:** English · [Русский](../../../ru/admin/runbooks/container-deployment.md)

> Admin-tier runbook for running Vesma in a container. **The published image
> is the primary path — pulling it is all you need; no local build required.**
> Building from source is a fallback for development, custom patches, or
> air-gapped environments. For real Kubernetes/K3s clusters use the Helm
> chart — [kubernetes-deployment.md](../kubernetes-deployment.md).

---

## Overview

One published image — `ghcr.io/vesmaro/vesmaro` — covers every path. Pick by target:

| Path | Tool | When to use |
|------|------|-------------|
| Pull & run | podman / docker | Fastest start — one command, no repo needed |
| docker/podman-compose | Compose | Single-host production — [`deploy/docker/`](../../../../deploy/docker/) |
| Helm chart | Helm 3 + K8s/K3s | Real clusters — [kubernetes-deployment.md](../kubernetes-deployment.md) |
| `podman kube play` | podman | Kubernetes-style pod on a single host |
| systemd quadlet | podman + systemd | Long-running user service with automatic restart |
| Build from source | podman / buildah | Fallback: development, patches, air-gapped |

The container exposes **port 8787** and uses two named volumes — `vesma-data` (SQLite + vector index)
and `vesma-vault` (Obsidian markdown mirror); the compose path names them `vesmaro-data`/`vesmaro-vault`.

---

## Prerequisites

- **podman** ≥ 4.0 (rootless fully supported) or **docker**
- **podman-compose** or **docker compose** — only for the compose path
- Python and `git` are **not** required on the host — everything runs inside the container
- Pulls are anonymous — the published package is **public**; `podman/docker
  login ghcr.io` is only needed if you hit a GHCR anonymous rate limit

---

## Run — pre-built image (fastest)

Pull the released image and start it directly — nothing to build:

```bash
podman pull ghcr.io/vesmaro/vesmaro:4.3.0      # :latest tracks the newest release
podman run -d --name vesma \
  -v vesma-data:/data -v vesma-vault:/vault \
  -p 8787:8787 \
  --env MNEMOS_API__TOTP_MASTER_KEY=<your-key> \
  ghcr.io/vesmaro/vesmaro:4.3.0
```

`docker` works identically — swap `podman` for `docker`. The image includes
`config.container.yaml` baked in as `/app/config.yaml` — no config mount is
required unless you want to override settings. The TOTP master key is
mandatory (the baked config binds to `0.0.0.0`); 4.x images read the
`MNEMOS_API__*` spelling, 5.x+ read `VESMARO_API__*` — setting both is always
safe.

Verify:

```bash
curl -fsS http://localhost:8787/health    # → {"status":"ok"}
```

---

## Run — compose (single-host production)

The ready-made compose file lives in [`deploy/docker/`](../../../../deploy/docker/) and uses
the published image — no build step:

```bash
cd deploy/docker
cp .env.example .env          # then edit: TOTP_MASTER_KEY=$(openssl rand -hex 32)
docker compose up -d          # or: podman-compose up -d
podman-compose logs -f vesmaro
podman-compose down
```

Details (env file, image tag pinning, Ollama sidecar): [deploy/docker/README.md](../../../../deploy/docker/README.md).

Optional local-embeddings sidecar:

```bash
docker compose --profile ollama up -d
docker exec vesmaro-ollama ollama pull nomic-embed-text
```

To activate Ollama as the embedding provider, set `embedding.provider: ollama`
in the container config (see [Configuration](#configuration)).

> The repo-root [`compose.yaml`](../../../../compose.yaml) also uses the published image —
> it keeps the historic `vesma-*` resource names for existing podman-compose users.
> The build-from-source flow is described in
> [Build from source](#build-from-source-fallback).

---

## Run — Kubernetes / K3s (cluster)

Use the Helm chart — it deploys the published image with an ingress, TLS and
persistent storage:

```bash
helm install vesmaro deploy/helm/vesmaro \
  --namespace vesmaro --create-namespace \
  --set auth.totpMasterKey="$(openssl rand -hex 32)" \
  --set ingress.className=traefik \
  --set 'ingress.hosts[0].host=mnemos.example.com'
```

Full guide with values, TLS and troubleshooting:
**[kubernetes-deployment.md](../kubernetes-deployment.md)**.

---

## Run — Kubernetes-style pod (podman kube play)

Vesma ships a Kubernetes-style pod manifest (`deploy/podman/kube/mnemos-pod.yaml`) compatible
with `podman kube play`. The manifest pulls the published image, injects the TOTP key from a
podman secret, and defines health probes.

### Start

```bash
printf 'MNEMOS_API__TOTP_MASTER_KEY=<your-key>\nVESMARO_API__TOTP_MASTER_KEY=<your-key>\n' \
  | podman secret create vesmaro-totp -
podman volume create vesma-data
podman volume create vesma-vault
podman kube play deploy/podman/kube/mnemos-pod.yaml
```

Shortcut (creates volumes automatically before playing the manifest):

```bash
./scripts/deploy.sh kube-up
```

### Stop

```bash
podman kube down deploy/podman/kube/mnemos-pod.yaml
```

Shortcut:

```bash
./scripts/deploy.sh kube-down
```

---

## Run — systemd (quadlet)

The quadlet path installs a systemd **user** unit and manages the container as a persistent
service. The unit references the published `ghcr.io/vesmaro/vesmaro:4.3.0`, pulled
automatically; to run a local build instead, build the image first (see
[Build from source](#build-from-source-fallback)) and set `Image=localhost/mnemos:latest` in the unit.

### Set the TOTP key

The unit reads the key from `~/.vesmaro.env` (`EnvironmentFile`), so no unit
editing is needed. Both env spellings must carry the same value — 4.x images
read `MNEMOS_API__*`, 5.x images read `VESMARO_API__*` (ADR-0031):

```bash
KEY=$(openssl rand -hex 32)
printf 'MNEMOS_API__TOTP_MASTER_KEY=%s\nVESMARO_API__TOTP_MASTER_KEY=%s\n' "$KEY" "$KEY" > ~/.vesmaro.env
```

### Install the unit

```bash
./scripts/deploy.sh quadlet
```

This copies `deploy/podman/quadlet/mnemos.container` to `~/.config/containers/systemd/` and runs
`systemctl --user daemon-reload`.

### Start and enable

```bash
systemctl --user start vesma
systemctl --user enable vesma   # autostart on login
```

### Check status

```bash
systemctl --user status vesma
```

---

## Build from source (fallback)

> Only needed for development, custom patches, or air-gapped environments.
> The published image is kept in sync with every release — end users never
> need this section.

```bash
podman build -t localhost/vesma:4.3.0 -f Containerfile .
```

The `Containerfile` uses `python:3.12-slim` as the base, installs the package (the MCP SDK rides in core),
copies `config.container.yaml` as `/app/config.yaml`, and sets the serve command on port 8787.

Makefile shortcut (builds `localhost/mnemos:latest`):

```bash
make build-image
```

The deploy helper does the same:

```bash
./scripts/deploy.sh build
```

**Pushing to ghcr.io (maintainers):** the release pipeline (`scripts/local-release.sh`)
pushes the versioned tag and `:latest` on every release — GitHub Actions are disabled, and
this script is the canonical path (see [ci-cd.md](ci-cd.md)). The pipeline currently targets
the legacy `ghcr.io/korrnals/vesma` name (the flip is part of the 5.0.0 phase-g, GWS card
#331); new releases are backfilled to the org namespace `ghcr.io/vesmaro/vesmaro` manually.
Manual push, if ever needed (PAT with `write:packages`):

```bash
podman login ghcr.io
podman tag localhost/vesma:4.3.0 ghcr.io/vesmaro/vesmaro:4.3.0
podman push ghcr.io/vesmaro/vesmaro:4.3.0
podman push ghcr.io/vesmaro/vesmaro:latest
```

---

## Configuration

Vesma uses `config.container.yaml` as the container config. It is:

- Embedded in the image at build time as `/app/config.yaml`
- Overridden by mounting your own config at the same path (read-only)

Key settings:

| Setting | Default | Notes |
|---------|---------|-------|
| `mnemos.data_dir` | `/data` | Mapped to the `vesma-data` named volume |
| `mnemos.vault_path` | `/vault` | Mapped to the `vesma-vault` named volume |
| `api.host` | `0.0.0.0` | Binds to all interfaces — **requires auth** |
| `api.port` | `8787` | Container-internal port; host mapping set in compose/run |
| `api.auth_enabled` | `true` | Must stay `true` when `host` is `0.0.0.0` |
| `api.totp_enabled` | `true` | Requires TOTP 2FA; key via `MNEMOS_API__TOTP_MASTER_KEY` (+ `VESMARO_API__*` from 5.x — ADR-0031) |
| `api.behind_tls_proxy` | `true` | TLS terminates upstream (Caddy, nginx, ingress, etc.) |
| `embedding.provider` | `nano` | mnema-embed-v1: bundled local model, works offline; no GPU required |

### Security requirements

Binding to `0.0.0.0` **requires** both `auth_enabled: true` and `totp_enabled: true`.
The TOTP master key must be supplied via the env spelling — it must never appear
in the config file or in any committed file.

Place Vesma behind a TLS-terminating reverse proxy (Caddy, nginx, ingress, etc.).
Set `trusted_proxies` to the CIDR of your proxy so that `X-Forwarded-For` headers are trusted.

For the full threat model and auth configuration details, see [../security.md](../security.md).

### Embedding provider

- **Default**: `nano` — the bundled `mnema-embed-v1` local ONNX model (no torch, no GPU, works offline; external providers like `onnx`/`sentence-transformers` remain available)
- **Ollama sidecar**: set `embedding.provider: ollama` and `embedding.ollama_url: http://ollama:11434`
  (see the compose section above)

---

## Health & ops

### Container healthcheck

Healthchecks probe the unauthenticated `/health` HTTP endpoint (compose and
quadlet alike) — no CLI dependency. Check the current health state:

```bash
podman inspect --format '{{.State.Health.Status}}' vesma
```

### Status overview

```bash
./scripts/deploy.sh status
```

Prints running containers (name, status, ports) and named volumes.

### Shell access

```bash
./scripts/deploy.sh shell
# equivalent to: podman exec -it vesma /bin/bash
```

### Run CLI inside the container

```bash
./scripts/deploy.sh cli search "hello"
# equivalent to: podman exec vesma vesma search "hello"
```

---

## See also

- [kubernetes-deployment.md](../kubernetes-deployment.md) — Helm chart for real K8s/K3s clusters
- [`deploy/README.md`](../../../../deploy/README.md) — all deployment paths at a glance
- [install.md](install.md) — bare-metal / virtualenv install
- [../security.md](../security.md) — threat model, auth model, SSRF guard
- [../../user/getting-started.md](../../user/getting-started.md) — first run guide

---

_Source files: `Containerfile`, `compose.yaml`, `config.container.yaml`, `scripts/deploy.sh`,
`deploy/podman/quadlet/mnemos.container`, `deploy/podman/kube/mnemos-pod.yaml`,
`deploy/docker/`, `deploy/helm/vesmaro/`_
