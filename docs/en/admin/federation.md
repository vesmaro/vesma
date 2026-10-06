# Federation — Phase 1 prerequisites (per-peer ACL + trigger codes + access log)

This page documents the federation prerequisites on the **vesma**
side — per-peer ACL, the trigger-codes enum, and the federation access
log — and remains the reference for the `PeerConfig` field set and the
trigger-code contract. The mediated-pull request path itself is live:
`handle_pull` in `src/vesmaro/federation_server.py` serves
`POST /api/v1/federation/pull`, and the end-to-end verification guide is
[`federation-testing.md`](federation-testing.md). The original rollout
was staged (Phase 1: config + enums + log; Phase 2: the server), so
some sections below keep the Phase 1/Phase 2 wording.

- **Config + contract (this page):** per-peer ACL config, trigger codes
  enum, federation access log.
- **Live request path:** `src/vesmaro/federation_server.py` (B side) and
  `src/vesmaro/api/federation.py` (route adapter). The external Go peer
  binary lives in a separate repo, `vesma-mesh`.
- **References:** ArchCom contract 2026-07-17
  (`.archcom/sessions/2026-07-17-federation-contract.md` §3.2, §6, §9,
  §10), ADR-0016 (`docs/project/adr/0016-federation-threat-model.md`).

## 1. Per-peer ACL — `federation.peers`

Phase 1 extends `FederationConfig` (`src/vesmaro/config.py`) with a
`peers: dict[str, PeerConfig]` map. Each peer is keyed by its A2A id
(for example `mnemos-A`) and describes what that peer is allowed to
pull. The global `federation.shared_projects` whitelist stays as the
top-level filter; per-peer `allowed_projects` is a subset filter on
top of it.

### Fail-closed defaults

| Field | Default | Means |
|---|---|---|
| `peers` | `{}` | No peers configured — the server refuses all pull requests (fail-closed). |
| `allowed_projects` | `[]` | The peer may pull **no** projects. |
| `allowed_types` | `[]` | The peer may pull **no** record types. |
| `["*"]` (either field) | — | Explicit wildcard — all projects in `shared_projects` / all record types. Never implicit. |
| `mtls_cert_fingerprint` | `None` | mTLS pinning not enforced for this peer (operator opts in). |

An operator who wants to open a peer must say so explicitly. There is
no implicit "allow all" anywhere in the chain.

### `PeerConfig` fields

| Field | Type | Notes |
|---|---|---|
| `bearer_token_env` | `str` (required) | NAME of the env var holding the per-peer bearer token (`mnk_fed_<peer_id>_<random>` per ADR-0016). Never the value — the server reads the token from this env var at request time. |
| `allowed_projects` | `list[str]` | Subset of `shared_projects`. Empty = none. `["*"]` = all in `shared_projects`. |
| `allowed_types` | `list[str]` | One of `decision` / `learning` / `bug-pattern` / `rule` / `open-question` / `checkpoint` / `session`. Empty = none. `["*"]` = all. |
| `rate_limit_per_minute` | `int` | Per-peer pull rate limit (contract §8, DDoS mitigation). Default 30, clamped 1–600. |
| `mtls_cert_fingerprint` | `str \| None` | Optional SHA-256 of the peer's mTLS client cert. If set, the server rejects non-matching client certs. |

### Example (`config.yaml`)

All values below are RFC-reserved dummies — never real tokens.

```yaml
federation:
  shared_projects:
    - vesma
    - project-umbra
  peers:
    mnemos-A:
      bearer_token_env: VESMA_FED_PEER_A_TOKEN
      allowed_projects:
        - vesma
      allowed_types:
        - decision
        - learning
        - bug-pattern
      rate_limit_per_minute: 30
      # Optional — pin the peer's mTLS client cert:
      mtls_cert_fingerprint: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

The token value lives in the named env var (here
`VESMA_FED_PEER_A_TOKEN`), set in the operator's environment or
secret manager — never committed to the config file.

## 2. Trigger codes — `src/vesmaro/trigger_codes.py`

Contract §9 replaces a per-session query budget with an
**exhaustive response** plus a trigger code. The B side (Phase 2
federation server) returns one of five codes in the `share-finding`
A2A payload; the A side (Phase 2 federation client) dispatches on the
code.

| Code | When B returns it | What A does |
|---|---|---|
| `EXHAUSTIVE` | B gave the full sanitized answer | Use it; do not repeat the request for the same topic. |
| `ALREADY_EXHAUSTED` | B already answered `EXHAUSTIVE` on this topic (checked via the access log) | Reuse the prior answer; do not re-query. |
| `PARTIAL` | Answer is partial (records missing or moderation redacted a portion) | Refine the query (different topic/angle); do not repeat verbatim. |
| `REFUSED` | B refused — content cannot be shared even after redaction | Do not repeat; fall back to local `mnemos_search` (КП-2). |
| `OFFLINE_LITE` | B online in reduced mode (e.g. moderation partially offline) | Use the partial result; supplement with local `mnemos_search`. |

Two helpers:

- `is_terminal(code)` — `True` for `EXHAUSTIVE`, `ALREADY_EXHAUSTED`,
  `REFUSED` (A should not re-query the same topic).
- `should_fallback_to_local(code)` — `True` for `REFUSED`,
  `OFFLINE_LITE` (A falls back to local `mnemos_search`).

Phase 1 defines the enum and the two helpers. Phase 2 wires the codes
into the server (returned in the payload) and the client (dispatched on
receive).

## 3. Federation access log — `src/vesmaro/federation_access_log.py`

Contract §10. A B-side append-only JSONL audit log at
`~/.mnemos/logs/federation-access.jsonl` that records who queried
what, when, with what trigger code, and which records were returned.
The log powers **anti-correlation tracking**: B sees A already got
`EXHAUSTIVE` on topic X → the next request on the same topic returns
`ALREADY_EXHAUSTED` (a short code, not a re-ship of sanitized content).

### Privacy — no plaintext query (КП-5)

The log stores only a **SHA-256 hash** of the query topic, never the
plaintext topic. The same topic hashes to the same digest, so B can
match a repeat request to a prior `EXHAUSTIVE` answer without ever
learning the query intent. If the log file leaks, the topic intent
does not.

### `AccessLogEntry` fields

| Field | Type | Notes |
|---|---|---|
| `peer_id` | `str` | A2A id of the requesting agent (who). |
| `topic_hash` | `str` (64 hex chars) | `SHA-256(query_topic)` — never plaintext. |
| `timestamp` | `datetime` (UTC ISO-8601) | When the request was served. |
| `project_scope` | `str` | Project slug that was requested. |
| `trigger_code` | `TriggerCode` | Code returned to the peer (§9). |
| `record_ids_accessed` | `list[str]` | Record ids returned (forensic audit). |

### `FederationAccessLog` API

| Method | Purpose |
|---|---|
| `append(entry)` | Append one JSON line, `flush` + `os.fsync` (audit integrity), process-local lock for thread safety. |
| `query(peer_id, topic_hash)` | Most recent entry for the (peer, topic) pair — used by the server to decide `ALREADY_EXHAUSTED`. |
| `query_recent(peer_id, since=...)` | All entries for a peer since a UTC timestamp — audit reports. |
| `count_by_trigger_code(peer_id, since=...)` | Zero-filled counts per trigger code — metrics/audit. |

Module helper: `hash_topic(topic: str) -> str` — `SHA-256(topic)` hex.

### Not replicated — B-side only

The access log lives **only on B**. It is never exported, never synced
to peers, never included in `vesma export`. Like the moderation
mapping table, it is a leak surface — replicating it would let a peer
reconstruct another peer's query history.

## 4. Status — Phase 2 is in production

The federation server (B-side) and client (A-side) live in the codebase:

1. **Server** — `handle_pull` in `src/vesmaro/federation_server.py` reads
   `federation.peers`, validates the per-peer bearer token from the
   named env var, optionally pins the mTLS client cert, applies the
   per-peer ACL on top of `shared_projects`, runs the moderation
   pipeline, checks the access log for `ALREADY_EXHAUSTED`, and returns
   the sanitized response with a `TriggerCode`. HTTP route:
   `POST /api/v1/federation/pull` (adapter `src/vesmaro/api/federation.py`).
2. **Client** — `src/vesmaro/federation_client.py` sends a pull request,
   receives the `TriggerCode`, and dispatches — `is_terminal` /
   `should_fallback_to_local` decide whether to use the answer, refine
   it, or fall back to local search. End-to-end verification:
   [`federation-testing.md`](federation-testing.md).

The Go binary that carries the gRPC transport lives in a separate
repo (`vesma-mesh`) and is out of scope for this page.

## 5. See also

- [ADR-0016 — Federation threat model](../../project/adr/0016-federation-threat-model.md)
- [Security — Federation defence-in-depth](security.md#11-federation-defence-in-depth)
- [Federation — Batch Sync (Phase 0)](../user/sync.md)
- ArchCom contract 2026-07-17 (`.archcom/sessions/2026-07-17-federation-contract.md`)