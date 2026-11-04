# mcp-front as a platform component (operator template)

ADR-0050 wave W2 (Ф2, ratified 2026-10-31): the MCP front becomes a
PLATFORM COMPONENT — a stateless streamable-HTTP proxy that gives
harnesses ONE entry point to the core `/mcp` plane. The platform owns
the process lifecycle (supervisor spawn, health probe, restart policy,
stop discipline). This directory is the OPERATOR-documented template for
that component; it is deliberately NOT part of the bundled pack (CM §3.1).

Files:

| File | Role |
|---|---|
| `mcp-front.yaml` | the component manifest template (copy → components.d, edit `OPERATOR-EDIT` values) |
| `mcp-front.env.example` | the env.d config channel template (copy → env.d, chmod 600 — NO secrets inside, see below) |

## Зачем фронт (why the front exists)

- **Единая точка подключения харнесов.** Every harness points its MCP
  client at ONE supervised URL (`http://127.0.0.1:8790/...`) instead of
  spawning its own `vesma mcp-server` per session — no version drift,
  every write flows through the core's writer invariants (ADR-0049
  writer-lock, mint-protection), W3 namespaces and quotas apply to every
  request by construction.
- **Слот для W4-спула — занят.** ADR-0051 (W4) landed IN this component
  (see "Degraded write mode" below): the front is the ONE process
  between harnesses and the core, so it is the spool carrier — writes
  queue locally while the core is down and replay through the core's
  own write plane on recovery. Nothing else in the topology changed
  when that landed.

Contract (C2, ADR-0050 §3): **the front is an identity carrier, not an
authorizer.** Harness `Authorization` headers pass through UNREAD; the
core's W1 gate stays the only authority for HARNESS identity. The front
holds exactly ONE credential of its own — the replay service token
(`VESMA_FRONT_SERVICE_TOKEN`, ADR-0051 W4; see the env template) — and
authenticates no harness with it. Everything else in the slot stays
non-secret.

## Why python-child, not a launcher binary

Same rationale as the eyes template (`contrib/eyes/`): the installer's
leg dispatch is exclusive — an argv without `{venv_bin}` is a BINARY
child (sha-pinned, no component venv), which would leave the front's
Python dependencies with no platform slot. The python-child form keeps
every artifact in its ratified slot:

| Artifact | Slot (user / system) |
|---|---|
| manifest | `~/.vesma/components.d/mcp-front.yaml` / `/etc/vesma/components.d/mcp-front.yaml` |
| dependencies (venv) | `~/.vesma/venvs/mcp-front` / `/usr/local/lib/vesma/venvs/mcp-front` — installer-materialized (`vesma==`, `uvicorn==`, `httpx==` exact pins) |
| config channel | `~/.vesma/env.d/mcp-front.env` / `/etc/vesma/env.d/mcp-front.env` (0600, fail-closed — carries NO secrets, ever) |
| lifecycle | the supervisor (`vesma.service` / `vesma@*.service`) — never a hand unit |
| state | none — the front is stateless; the `component_data` dir the install creates stays empty (reserved) |

## Install (user plane)

```sh
# 0. Prerequisites: the core running (`vesma serve`, MCP plane on
#    127.0.0.1:8787 — `mcp.endpoint_enabled`, default on).
# 1. Manifest into the slot; edit the --port / health.url pair if 8790
#    is taken (they must move together).
cp contrib/mcp-front/mcp-front.yaml ~/.vesma/components.d/mcp-front.yaml
# 2. Config channel into the env.d slot (0600). NO secrets here — the
#    front carries harness tokens THROUGH, it never stores them; the
#    file today holds only the non-secret target.
cp contrib/mcp-front/mcp-front.env.example ~/.vesma/env.d/mcp-front.env
chmod 600 ~/.vesma/env.d/mcp-front.env
# 3. Install: validates fail-closed, materializes the (empty, reserved)
#    component data dir.
vesma service components install mcp-front
# 4. Materialize the component venv (install-flow only — never pip by hand).
vesma service install
# 5. Run under the supervisor; health = /healthz on 127.0.0.1:8790.
vesma service start mcp-front
```

System plane: the same steps with `/etc/vesma/...` slots (root-owned)
and `vesma system install` owning the venv.

Harness side: point the MCP client (streamable HTTP transport) at
`http://127.0.0.1:8790/mcp` and send the SAME agent token it would send
to the core (`vesma agent-token issue --agent <harness> --node
<instance-id>`). The path is not mapped — any path on the front lands on
the configured core plane URL; `/healthz` is the one local exception.

## Degraded write mode (ADR-0051, W4)

When the core's `/mcp` plane is unreachable, the front — with degraded
mode enabled — keeps harness writes alive instead of failing them:

- **Machine**: 3 failed probes (request outcomes double as probes; the
  poller adds the quiet-period cadence) flip `HEALTHY → DEGRADED`;
  a probe success flips to `REPLAY` and, after 100% verified delivery,
  back to `HEALTHY`; the TTL expiry flips to `BLOCKED_WRITES` (writes
  get the typed `DegradedTTLExceeded` refusal, reads stay available,
  the spool freezes FOREVER — nothing is auto-deleted).
- **Writes** (the op classifier in `vesma.resilience`: `vesma_add`,
  `vesma_save_context`, tag/import/ingest writes, …) land in the spool
  at `<state_home>/vesma/spool/<instance>/<actor>/` — 0600/0700,
  append-only JSONL with a per-row SHA-256 hash chain, fsync, a
  secret-scan at write time (flagged rows carry pattern NAMES only).
  The harness gets a normal MCP tool result whose TEXT is the receipt
  (op_id, TTL remainder, do-not-retry) plus the `X-Vesma-Degraded`
  header on every front response while not HEALTHY; `/healthz` carries
  the degraded section (mode/since/ttl_left/spool counters). DR-24 in
  `vesma doctor` audits the spool (age, %TTL, mode/owner, chain).
- **Reads** stay transparent: while degraded they answer the honest
  fixed 503 (the front has no local planes to read from).
- **Replay** goes through the core's OWN `tools/call` plane under the
  FRONT SERVICE TOKEN — the spool never becomes a second engine. Rows
  carry `vesma_degraded_op_id` in their arguments; the front removes a
  spool atomically only after every row applied; any failed row keeps
  the whole spool loud and on disk.
- **Config** (the env.d slot, fail-closed): WITHOUT
  `VESMA_FRONT_DEGRADED_MAX_DURATION` the front runs refuse-writes (the
  W2 fixed-503 behavior, loudly logged at boot). The same knobs exist
  canonically in `config.yaml` under `resilience.degraded.*` (required
  `enabled` + `max_duration`, `extra=forbid`).

## Operational notes

- **Port 8790, loopback-only** — the port the prod `mcp` config section
  reserves for the MCP plane (`mcp.front.port`). NOTE: `mesh.tcp.port`
  (the default-off Phase-2 k8s mesh leg) defaults to the SAME number —
  do not enable both on one box without moving one. Widening the bind is
  a deliberate, documented operator change (public-bind edge); argv and
  `health.url` must move together.
- **Config keys** — the deployment defaults live at `mcp.front.*`
  (`port` / `target` / `timeout` — `McpFrontConfig`, `extra=forbid`).
  The CM grammar has NO port placeholder, so the manifest pins the
  operative literals (the eyes-template precedent); `VESMA_FRONT_TARGET`
  in the env.d slot carries the non-secret core URL. `mcp.front.target`
  refuses non-loopback values at config time (C2: the target is where
  harness tokens GO).
- **Pins** — `launch.python.requirements` uses the LY §3.8 exact-pins
  grammar; the engine pin `vesma==` tracks the release train BY HAND
  (an operator template is not release-train-tracked — bump it on every
  engine upgrade, re-pinning `uvicorn`/`httpx` from that release's
  `uv.lock`). `vesma service install` rebuilds the venv from scratch
  (a venv that no longer matches its lock is never silently kept).
- **Timeout** — `--timeout 60` (seconds) in argv; MCP tool calls can run
  long against a big store, keep generous. Core unreachable → a FIXED
  503 JSON (cause only in the log). Oversized bodies (> 4 MiB, the SDK
  ceiling) → 413 at the front, before any hop.

## Honest residuals (documented, not hidden)

- **UDS transport is NOT implemented** (ADR-0050 §2 prefers a Unix
  socket for Ф2 — it closes the local-impostor-on-TCP-loopback class);
  this template ships the documented TCP-loopback form with the
  mandatory token auth carried by the core. The UDS leg is a deliberate
  follow-up, not a silent gap.
- **Degraded-mode residuals (ADR-0051 W4, honest)**: (a) per-row CLIENT
  re-authz at replay is approximated, not implemented — the front never
  stored harness tokens (C2), so a client revoked during a degradation
  is not re-checked per row; the service token refusal aborts the run
  with nothing applied, and the `actor_policy` seam exists for a future
  core status API. (a2, the F2 residual) an IDENTITY-TAGGED row (a
  client `agent:<slug>` tag) replayed under the front SERVICE token is
  either delivered into the FRONT-SERVICE namespace (untagged rows) or
  REFUSED by the core's namespace enforcement and pocketed
  **rejected-permanent** — counted, never retried, visible in the
  replay log, the persisted replay summary and DR-24; DELIVERY INTO THE
  CLIENT'S NAMESPACE is a future core mechanism (an op-level identity
  delegation), not a silent re-own. The receipt text deliberately does
  NOT promise delivery — it points at DR-24. (b) the `superseded` rule
  (a store row newer than the spooled op wins) stays 0 in W4 — it needs
  a core-side conditional write, a later line. (c) the per-row commit
  watermark bounds the crash window to ONE re-application: a crash
  between "row applied" and "watermark persisted" can re-apply exactly
  that row on the next pass (the `vesma_degraded_op_id` envelope is in
  place for a future core-side dedup).
- The front proxies to a LOOPBACK core by construction; a remote-core
  deployment (front and core on different boxes) needs an explicit
  future opt-out in `validate_target` — today it is refused fail-closed
  (token-exfiltration guard).

## Uninstall

`vesma service uninstall mcp-front` (user plane) removes what the
install flow owns — the component venv above all. The operator-authored
manifest file and the env.d config file stay operator-owned: remove them
by hand if desired.
