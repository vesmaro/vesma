# Deploy

Deployment assets for the Vesmaro (Mnemos) memory server, grouped by target:

| Directory | Target | Entry point |
|-----------|--------|-------------|
| [`helm/vesmaro/`](helm/vesmaro/) | **Kubernetes / K3s** (any 1.25+ cluster) | `helm install vesmaro deploy/helm/vesmaro --set auth.totpMasterKey=$(openssl rand -hex 32)` — Deployment + Service + **Ingress** + 2×PVC + Secret |
| [`docker/`](docker/) | **Docker / Docker Compose** (pre-built image) | `cp .env.example .env && docker compose up -d` (podman-compose compatible) |
| [`podman/quadlet/`](podman/quadlet/) | **Podman** as a systemd user unit | copy unit + `systemctl --user start mnemos` |
| [`podman/kube/`](podman/kube/) | **Podman** `kube play` (single-host pod) | `podman kube play deploy/podman/kube/mnemos-pod.yaml` |
| [`../../compose.yaml`](../compose.yaml) | Compose from the repo root (published image, historic `mnemos-*` names) | `TOTP_MASTER_KEY=$(openssl rand -hex 32) docker compose up -d` |

Full documentation:

- EN: [docs/en/admin/kubernetes-deployment.md](../docs/en/admin/kubernetes-deployment.md) (Helm/K8s/K3s) ·
  [docs/en/admin/runbooks/container-deployment.md](../docs/en/admin/runbooks/container-deployment.md) (Docker/Podman/compose)
- RU: [docs/ru/admin/kubernetes-deployment.md](../docs/ru/admin/kubernetes-deployment.md) ·
  [docs/ru/admin/runbooks/container-deployment.md](../docs/ru/admin/runbooks/container-deployment.md)

## Requirements common to every path

- **TOTP master key** — required in any non-loopback deployment. Generate:
  `openssl rand -hex 32`. An empty key is rejected at startup.
- **Image**: published at `ghcr.io/vesmaro/vesma` (tags `5.1.2`, `latest`;
  **public** — anonymous pulls, backfilled from the legacy
  `ghcr.io/korrnals/mnemos` user namespace). The release pipeline targets
  the legacy name until 5.0.0 phase-g (GWS card #331); new releases are
  backfilled manually in the meantime.
- **Data**: two volumes — `/data` (SQLite + vector index) and `/vault`
  (Obsidian markdown mirror). Health surface: unauthenticated `GET /health`.

---

## RU — кратко

Каталог деплоя Vesmaro (Mnemos): **K8s/K3s** — Helm-чарт с ингрессом
(`helm/vesmaro/`), **Docker** — docker-compose с готовым образом
(`docker/`), **Podman** — quadlet-юнит и kube-play манифест (`podman/`).
Полные руководства — в docs (EN/RU, ссылки выше). Обязательное для любого
не-loopback деплоя: TOTP-ключ (`openssl rand -hex 32`); образ публичный —
`ghcr.io/vesmaro/vesma`, тянется без логина.
