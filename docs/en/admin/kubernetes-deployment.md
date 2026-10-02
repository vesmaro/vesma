# Kubernetes Deployment (Helm)

**🌐 Language / Язык:** English · [Русский](../../ru/admin/kubernetes-deployment.md)

> Admin-tier guide for deploying the full Vesma (Vesma) server into any
> Kubernetes 1.25+ cluster — vanilla K8s, K3s, kind, k0s — with the bundled
> Helm chart (`deploy/helm/vesma/`): Deployment, Service, **Ingress**,
> two PersistentVolumeClaims and the TOTP secret.

---

## Overview

The chart deploys a complete single-node installation:

| Resource | Purpose |
|----------|---------|
| Deployment (1 replica, `Recreate` strategy) | HTTP API server on port 8787; SQLite is single-writer — do not scale replicas |
| Service (`ClusterIP`) | `http` port → pods |
| Ingress (enabled by default) | `/` → service; ingress class and TLS are values |
| ConfigMap | Renders `/app/config.yaml` from values (`api.*`, `embedding.*`, `search.*`, `mcp.*`) |
| Secret (optional) | TOTP master key; skipped when `auth.existingSecret` is set |
| PVC ×2 | `-data` (SQLite + vector index), `-vault` (Obsidian markdown mirror) |

Health surface: unauthenticated `GET /health` (used by probes and `helm test`).

## Prerequisites

- Kubernetes 1.25+ with a working StorageClass (K3s ships `local-path`)
- Helm 3.8+
- An ingress controller for the ingress (K3s ships Traefik preinstalled)
- The container image reachable from the cluster — see
  [Image registry status](#image-registry-status)

## Quick start

```bash
helm install vesma deploy/helm/vesma \
  --namespace vesma --create-namespace \
  --set auth.totpMasterKey="$(openssl rand -hex 32)" \
  --set ingress.className=nginx \
  --set 'ingress.hosts[0].host=vesma.example.com'
```

K3s (Traefik + local-path are the defaults, so nothing extra is needed):

```bash
helm install vesma deploy/helm/vesma \
  --namespace vesma --create-namespace \
  --set auth.totpMasterKey="$(openssl rand -hex 32)" \
  --set ingress.className=traefik \
  --set 'ingress.hosts[0].host=vesma.home.lan'
```

Verify:

```bash
kubectl -n vesma rollout status deploy/vesma
helm -n vesma test vesma            # in-cluster wget against /health
kubectl -n vesma port-forward svc/vesma 8787:8787
curl -fsS http://localhost:8787/health  # → {"status":"ok"}
```

## TOTP master key

Any non-loopback bind **requires** auth + TOTP; an empty master key is
rejected at startup, so pods crash-loop until the key is provided. Three
supported ways, in order of preference:

1. **Pre-created secret (production):**

   ```bash
   kubectl -n vesma create secret generic vesma-totp \
     --from-literal=totp-master-key="$(openssl rand -hex 32)"
   helm install vesma deploy/helm/vesma -n vesma \
     --set auth.existingSecret=vesma-totp
   ```

2. **`--set` at install time** (kept in Helm release history — acceptable for
   homelabs): `--set auth.totpMasterKey="$(openssl rand -hex 32)"`.

3. **Values file** — never commit the real value; keep it out of git.

The key is injected under **both env spellings** —
`MNEMOS_API__TOTP_MASTER_KEY` (read by 4.x images) and
`VESMA_API__TOTP_MASTER_KEY` (canonical since 5.3; 5.0–5.2 images read the
deprecated `VESMARO_API__TOTP_MASTER_KEY`, accepted until 6.0) — from the
single secret key. This is the ADR-0031 dual-prefix contract: one value,
several names, so the chart works across the 4.3.0 → 5.0.0 → 5.3.0 rebrand
boundaries unchanged.

## Ingress & TLS

```yaml
ingress:
  enabled: true
  className: nginx            # or traefik (K3s default), haproxy, ...
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"   # long agent calls
    cert-manager.io/cluster-issuer: letsencrypt-prod         # if cert-manager installed
  hosts:
    - host: vesma.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: vesma-tls
      hosts:
        - vesma.example.com
```

The server runs with `behind_tls_proxy: true` by default and trusts
`X-Forwarded-For` from `api.trustedProxies` (default: private ranges). If your
ingress controller pods run in a different CIDR, add it:

```bash
helm upgrade vesma deploy/helm/vesma -n vesma --reuse-values \
  --set 'api.trustedProxies={10.0.0.0/8,172.16.0.0/12,10.42.0.0/16}'
```

## Storage

Two PVCs are created by default (`persistence.data.size: 5Gi`,
`persistence.vault.size: 1Gi`, cluster-default StorageClass). Pin a class
explicitly when needed:

```bash
--set persistence.data.storageClass=local-path --set persistence.vault.storageClass=local-path
```

Backups: the store is a single SQLite file — snapshot the `-data` volume
(see [runbooks/backup-restore.md](runbooks/backup-restore.md) for the
consistent procedure).

## Image registry status

Published images live at **`ghcr.io/vesmaro/vesmaro`** (org namespace,
backfilled from the legacy user namespace in the 4.3.0 wave) and are
**public** — plain pulls work with no credentials. `image.pullSecrets`
remains available for private-registry setups or rate limits, but is not
needed for this image.

The release pipeline still targets the legacy `ghcr.io/korrnals/vesma` name
until the 5.0.0 registry migration (ADR-0031 / GWS card #331, phase g); new
releases are backfilled to the org namespace manually in the meantime.

## Upgrades & uninstall

```bash
helm upgrade vesma deploy/helm/vesma -n vesma --reuse-values \
  --set image.tag=4.4.0              # data volumes survive upgrades
helm uninstall vesma -n vesma    # PVCs are kept; delete them explicitly if needed
```

The `Recreate` strategy is deliberate: the old pod must release the
ReadWriteOnce volume before the new one mounts it.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Pods stuck in `CreateContainerConfigError` (or `CrashLoopBackOff` with a `ValueError` about the master key) | No TOTP key set — see [TOTP master key](#totp-master-key) |
| `ImagePullBackOff` | Registry auth or rate limit — verify the image ref; `image.pullSecrets` for private setups or rate limits |
| PVC `Pending` | No default StorageClass — set `persistence.*.storageClass` |
| Ingress returns 404 | Wrong `ingress.className`, or the controller watches other namespaces only |
| 401 on `/api/*` | Expected — all API endpoints except `/health` require the TOTP login flow ([security.md](security.md)) |

## See also

- [runbooks/container-deployment.md](runbooks/container-deployment.md) — Docker / docker-compose / Podman paths
- [security.md](security.md) — threat model, auth model, TOTP enrollment
- [http-api.md](../user/http-api.md) — REST endpoints
- [`deploy/README.md`](../../../deploy/README.md) — all deployment paths at a glance
