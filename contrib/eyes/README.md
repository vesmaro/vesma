# vesma-eyes as a platform component (operator template)

Wave R of the component platform (ArchCom 2026-10-29, verdict `1c2b8d64`):
«глаза — компонент с манифестом» — the platform owns the eyes process
lifecycle (supervisor spawn, health probe, restart policy, stop
discipline). This directory is the OPERATOR-documented template for that
component; it is deliberately NOT part of the bundled pack (CM §3.1).

Files:

| File | Role |
|---|---|
| `eyes.yaml` | the component manifest template (copy → components.d, edit `OPERATOR-EDIT` values) |
| `eyes.env.example` | the secrets slot template (copy → env.d, chmod 600, fill) |

## Why python-child, not a launcher binary

vesma-eyes is a Python/FastAPI app (no PyPI package, no binary artifact).
The platform's installer dispatch is exclusive: a manifest whose argv does
not reference `{venv_bin}` is a BINARY child → the binary_artifact slot
(~/.vesma/bin/<name>, sha-pinned) — and that leg materializes NO component
venv, so eyes' dependencies (fastapi, uvicorn, …) would have no slot at
all: either a runtime-created venv (forbidden — venvs are created only by
the install flow, layout DR-12) or an unmanaged interpreter (deps outside
the slot table — the exact «вне платформы» class this wave closes).

The python-child form keeps every artifact in its ratified slot:

| Artifact | Slot (user / system) |
|---|---|
| manifest | `~/.vesma/components.d/eyes.yaml` / `/etc/vesma/components.d/eyes.yaml` |
| dependencies (venv) | `~/.vesma/venvs/eyes` / `/usr/local/lib/vesma/venvs/eyes` — installer-materialized |
| secrets | `~/.vesma/env.d/eyes.env` / `/etc/vesma/env.d/eyes.env` (0600, fail-closed) |
| data (SQLite board.db) | `~/.vesma/components/eyes` / `/var/lib/vesma/eyes` (`VESMARO_DATA`) |
| lifecycle (spawn/health/restart/stop) | the supervisor (`vesma.service` / `vesma@*.service`) — never a hand unit |

## Install (user plane)

```sh
# 0. Prerequisites: vesma-eyes checkout on this machine (see the
#    invariant note below), engine running (default API :8787).
# 1. Manifest into the slot; edit cwd/--app-dir + VESMARO_DATA first.
cp contrib/eyes/eyes.yaml ~/.vesma/components.d/eyes.yaml
# 2. Secrets into the env.d slot; fill real values.
cp contrib/eyes/eyes.env.example ~/.vesma/env.d/eyes.env
chmod 600 ~/.vesma/env.d/eyes.env
# 3. Install: validates fail-closed, materializes the data dir.
vesma service components install eyes
# 4. Materialize the component venv (install-flow only — never pip by hand).
vesma service install
# 5. Run under the supervisor; health = /api/health on 127.0.0.1:8140.
vesma service start eyes
```

System plane: the same steps with `/etc/vesma/...` slots (root-owned) and
`vesma system install` owning the venv; the `VESMARO_DATA` literal
becomes `/var/lib/vesma/eyes`.

## Operational notes

- **Port 8140, loopback-only** — the v1 public-bind posture (the bundled
  board manifest is the precedent). The dev podman/distrobox instance
  (0.0.0.0:8140) and this component share the port CONVENTION but must
  not run simultaneously on one machine; the dev instance remains the
  dev circuit and is not touched by the platform form.
- **Pins** — `launch.python.requirements` uses the LY §3.8 exact-pins
  grammar: bare names only (`uvicorn[standard]==…` is rejected), so the
  runtime-useful members of the standard extra (`uvloop`, `httptools`)
  are pinned explicitly. Re-pin from the pinned eyes release:
  `uv pip freeze` over its `server/requirements.txt`.
- **Upgrading eyes** — bump `metadata.version`, re-pin requirements,
  `vesma service install` rebuilds the venv from scratch (a venv that no
  longer matches its lock is never silently kept).

## Invariant residual (honest note)

The eyes CHECKOUT itself is an operator-provisioned code source: eyes
ships no PyPI package, so its `server/`+`web/` code sits outside the
«артефакт × профиль» slot table (no `operator_code` role exists yet).
Everything the platform installs and runs — manifest, venv, env, data,
lifecycle — is in-table; the checkout is documented as a prerequisite and
is a candidate for either a table role or the future eyes publishing
train (ArchCom follow-up, not this wave).

## Uninstall

`vesma service uninstall eyes` (user plane; `vesma system uninstall` owns
the system profile as a whole) removes what the install flow owns — the
component venv above all. The operator-authored manifest file and the
env.d secret file stay operator-owned: remove them by hand if desired.
Component data dirs are operator data and are preserved.
