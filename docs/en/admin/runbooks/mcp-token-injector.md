# Runbook: MCP Token Injector (seamless harness access)

**🌐 Language / Язык:** English · [Русский](../../../ru/admin/runbooks/mcp-token-injector.md)

Architecture: [ADR-0052](../../../project/adr/0052-seamless-harness-mcp-token-injector.md) ·
Doctor probe: **DR-26** (`vesma doctor`) · Generator: `vesma service injector-unit`

## What this is

A loopback, MCP-blind, stdlib reverse proxy (`vesma.mcp_injector`) sits between
a harness and the core's `/mcp` plane. The harness authenticates with a static
listener secret; the proxy re-reads the agent token file on EVERY request and
injects exactly one fresh Bearer toward the core. Sessions of any age survive
every token rotation.

- **Canonical source:** `src/vesma/mcp_injector.py` (repo artifact, stdlib only).
- **Live deployment (host-local):** `~/.local/bin/vesma-mcp-injector` + the
  systemd --user unit `vesma-mcp-injector.service` — a deployed copy; it keeps
  serving until its next update absorbs the repo artifact.

Port canon (ADR-0052 addendum): 8787 core REST/MCP · **8789 injector zcode** ·
8790 mesh.tcp (in-core) · 8791 mesh component · 8792 mcp-front · further
harnesses from 8793. ONE injector per harness identity: port + token-file +
secret-file ARE the identity.

## The rotation invariant (never revert)

**Rotation period must stay strictly below the token TTL.** The 2x/day cadence
(04:17 + 16:17, timer `Persistent=true`) against the 24h TTL gives ≥12h of
overlap — the TIME layer. The proxy's one 401-retry, taken ONLY when the token
file changed byte-wise, closes the millisecond boundary race around the atomic
`mv` — the CODE layer. Both are mandatory:

- reverting the cadence to daily silently reintroduces a daily race window
  with no external symptoms — DR-26 warns when the token file mtime ages past
  25h (rotation automation stopped);
- the rotation script never rewrites the harness config (the config is
  write-once: URL literal + listener secret).

## Deploy a new harness injector (user plane)

```sh
# 0. Mint the identity's token file and the listener secret, both 0600.
# 1. Render the unit from the identity pins (read-only print):
vesma service injector-unit \
  --token-file ~/.secrets/vesma_zcode_agent_token \
  --secret-file ~/.secrets/vesma_proxy_listener_zcode.secret \
  --port 8789 --upstream-port 8787 > ~/.config/systemd/user/vesma-mcp-injector.service
# 2. Activate:
systemctl --user daemon-reload
systemctl --user enable --now vesma-mcp-injector.service
# 3. Point the harness config at http://127.0.0.1:8789/mcp/ with
#    Authorization: Bearer <listener secret> — then restart the harness once.
# 4. Verify: vesma doctor (DR-26) + the healthz below.
```

## Flip window order (migration to the proxy — acceptance item)

1. Deploy the rotation script WITHOUT config-rewrite FIRST (until the config
   flips, it mints and atomically replaces the token exactly as before).
2. Flip the harness config (atomic write, 0600): URL literal
   `http://127.0.0.1:8789/mcp/` + the listener secret as the Bearer.
3. One-time restart of the running sessions (the client reads its config once
   at start — the honest physical limit; afterwards session age is irrelevant).

## Health and diagnostics

```sh
curl -s http://127.0.0.1:8789/healthz
# -> {"ok": true, "token_file_readable": true, "upstream_tcp_ok": true}
#    (no auth; no paths/versions/pids in the body — contract shape)
journalctl --user -u vesma-mcp-injector -f
# one structured line per request: peer=... METHOD path -> status dur_ms retried token_changed
# WARN on every auth mismatch (listener auth mismatch) and every non-loopback peer
vesma doctor   # DR-26: unit active? healthz? credential files 0600 + fresh?
```

429 from the proxy is load-shedding, NOT an auth failure — raise `--rate-max`
for a bursty harness. 503 `auth source unavailable` is fail-closed: fix the
credential file (0600), the proxy recovers by itself (Restart=always).

## Rollback (minutes; drill BEFORE flipping live sessions)

1. Restore the harness config to the direct core URL with a Bearer from the
   live token file (atomic, 0600).
2. Restore the ORIGINAL rotation script WITH its config-rewrite step (the
   `.orig` backup) and the v1 timer — WITHOUT this step the next rotation
   run leaves sessions on the stale config and the defect returns.
3. `systemctl --user disable --now vesma-mcp-injector.service`.
4. One-time restart of the running sessions.

## Reference

- ADR-0052 (`docs/project/adr/0052-seamless-harness-mcp-token-injector.md`)
- Contract + protocol: `~/.gcw/architectural-committee/2026-11-06-mcp-token-seamless-injector{,-contract}.md`
- pytest packaging of the smoke checklist: `tests/test_mcp_injector.py`
