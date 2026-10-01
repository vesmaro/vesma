# Podman deployments

Two Podman paths for single-host setups. For Kubernetes/K3s clusters use the
[Helm chart](../helm/vesma/) instead; umbrella over all paths:
[../README.md](../README.md).

## Option A — systemd quadlet (recommended for long-running hosts)

Installs a systemd **user** unit; the container restarts on failure and on login.

```bash
mkdir -p ~/.config/containers/systemd
cp deploy/podman/quadlet/mnemos.container ~/.config/containers/systemd/
KEY=$(openssl rand -hex 32)
printf 'MNEMOS_API__TOTP_MASTER_KEY=%s\nVESMARO_API__TOTP_MASTER_KEY=%s\n' "$KEY" "$KEY" > ~/.vesma.env
podman pull ghcr.io/vesmaro/vesma:5.1.2   # or: podman build -t localhost/vesma:latest -f Containerfile . + edit the unit Image=
systemctl --user daemon-reload && systemctl --user start vesma
curl -fsS http://localhost:8787/health
```

> The two env spellings in `~/.vesma.env` must hold the SAME key value —
> 4.x images read `MNEMOS_API__*`, 5.x images read `VESMARO_API__*`.

## Option B — `podman kube play` (Kubernetes-style pod on a single host)

```bash
printf 'MNEMOS_API__TOTP_MASTER_KEY=<your-key>\nVESMARO_API__TOTP_MASTER_KEY=<your-key>\n' \
  | podman secret create vesma-totp -
podman volume create vesma-data && podman volume create vesma-vault
podman kube play deploy/podman/kube/mnemos-pod.yaml    # pulls ghcr.io/vesmaro/vesma:5.1.2
curl -fsS http://localhost:8787/health
```

Stop: `podman kube down deploy/podman/kube/mnemos-pod.yaml`.

## Notes

- `podman secret create` / `envFrom` in kube play need podman ≥ 4.8.
- The TOTP key is REQUIRED: the baked container config binds to `0.0.0.0`,
  and an empty master key is rejected at startup.
- Full runbook with the compose path, Ollama sidecar and ops commands:
  [container-deployment.md](../../docs/en/admin/runbooks/container-deployment.md)
  ([RU](../../docs/ru/admin/runbooks/container-deployment.md)).
