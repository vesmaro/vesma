# Docker / Docker Compose deployment

Single-host deployment of the pre-built image via Docker Compose (or
podman-compose — the file is compatible with both).

**Full container docs:
[docs/en/admin/runbooks/container-deployment.md](../../docs/en/admin/runbooks/container-deployment.md) ·
[docs/ru/admin/runbooks/container-deployment.md](../../docs/ru/admin/runbooks/container-deployment.md)**
Umbrella over all deployment paths: [../README.md](../README.md)

## Start

```bash
cd deploy/docker
cp .env.example .env          # then edit: TOTP_MASTER_KEY=$(openssl rand -hex 32)
docker compose up -d          # or: podman-compose up -d
curl -fsS http://localhost:8787/health    # → {"status":"ok"}
```

## Notes

- The published image is **public** — no login required for pulls.
- Data lives in named volumes `vesma-data` (SQLite + vector index) and
  `vesma-vault` (markdown mirror).
- Optional local-embeddings sidecar: `docker compose --profile ollama up -d`.
- Building from source is a development fallback — `podman build -f Containerfile .`
  (see the runbook's *Build from source* section); every compose file in the
  repo uses the published image.
