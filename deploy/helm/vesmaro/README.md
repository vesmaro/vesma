# vesmaro Helm chart

Deploys the full Vesmaro (Mnemos) memory server — HTTP API + bundled vesma-embed-v1
embedder — into any Kubernetes 1.25+ cluster (vanilla K8s, K3s, kind, k0s) behind
an ingress.

**Full walkthrough (values, TLS, K3s specifics, troubleshooting):
[docs/en/admin/kubernetes-deployment.md](../../../docs/en/admin/kubernetes-deployment.md) ·
[docs/ru/admin/kubernetes-deployment.md](../../../docs/ru/admin/kubernetes-deployment.md)**

## Quick start

```bash
helm install vesmaro deploy/helm/vesmaro \
  --namespace vesmaro --create-namespace \
  --set auth.totpMasterKey="$(openssl rand -hex 32)" \
  --set ingress.className=nginx \
  --set ingress.hosts[0].host=mnemos.example.com
```

Then `kubectl -n vesmaro port-forward svc/vesmaro 8787:8787` or use the ingress
address. `helm test vesmaro` runs an in-cluster health check.

## What it creates

| Resource | Purpose |
|----------|---------|
| Deployment (1 replica, `Recreate`) | The server; SQLite is single-writer, do not scale up |
| Service | `http` port 8787 → `/health` probes are unauthenticated |
| Ingress (optional, on by default) | `/` → service; TLS via `ingress.tls` |
| ConfigMap | Renders `/app/config.yaml` from `api.*`, `embedding.*`, `search.*`, `mcp.*` values |
| Secret (optional) | TOTP master key; `auth.existingSecret` skips rendering |
| PVC ×2 | `mnemos-data` (SQLite + vector index) and `mnemos-vault` (markdown mirror) |

## Image registry status

The default image is `ghcr.io/vesmaro/vesmaro` (org namespace, backfilled in
the 4.3.0 wave) and it is **public** — plain pulls work with no credentials.
`image.pullSecrets` stays available for private-registry setups or rate
limits, but is not needed here. The legacy `ghcr.io/korrnals/mnemos` package
holds pre-rebrand releases and will be archived.

## Values

See [values.yaml](values.yaml) — every key is documented inline. Key groups:
`image`, `ingress`, `auth` (TOTP master key / existingSecret), `api` (auth, CORS,
trusted proxies), `persistence` (two PVCs), `probes`, `resources`.
