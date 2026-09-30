# ADR-0016: Federation threat model

*Formal record — English only.*

- **Status**: Accepted — extends ADR-0014 (operator API auth) to cover the federation peer surface. Does NOT supersede ADR-0014.
- **Date**: 2026-07-21
- **Deciders**: abyss, GCW Senior Security Engineer, GCW Tech Lead
- **Related**: ADR-0014 (`0014-api-auth-threat-model.md`), ADR-0017 (`mnemos-mesh/docs/adr/0017-mnemos-mesh-architecture.md`), ArchCom contract 2026-07-17 (`.archcom/sessions/2026-07-17-federation-contract.md`), ArchCom 2026-07-20 (`.archcom/sessions/2026-07-20-automated-channel.md`)

## Context

Federation Phase 0 (batch sync) shipped 2026-07-19: moderation pipeline,
compact exchange format, `sync-peers.sh`, audit log, and the three-layer
defence-in-depth (`mnemos:no-federate` tag on add, background scanner,
moderation on export). The owner then raised four gaps that batch sync
does not cover — not fully automated, not a constant channel, freshness
lag, no query specificity — prompting the Architectural Committee to
plan Phase 1 (networked prerequisites) and Phase 2 (live pull).

ADR-0014 is the threat model for the **operator** HTTP API: opaque bearer
tokens (`mnk_` prefix), SHA-256-at-rest via PBKDF2-HMAC-SHA256, TOTP 2FA
mandatory on remote binds, rate limiting, fail-closed middleware. ADR-0014
explicitly **excludes federation** from its scope — the operator surface is
per-user, loopback-or-remote, browser-or-CLI. Federation is a different
surface:

- **Per-peer, not per-user.** A federation token authenticates a mesh node
  to another mesh node, not a human operator to the API.
- **Server-to-server, mTLS first.** The operator surface can hide behind a
  reverse proxy; the federation surface is peer-to-peer gRPC over mTLS.
- **Long-lived, low-rotation.** Operator tokens rotate on demand; peer
  credentials rotate on a cadence (default 90 days) tied to cert lifecycle.
- **Asymmetric trust.** An operator is typically one person with full
  access; a peer is another mnemos instance with a narrow per-peer ACL.

ADR-0014's trust-zone model (loopback optional-auth / remote mandatory-TOTP)
does not map cleanly to federation: a peer is never "loopback". ADR-0016
extends the model with a third trust zone — **federated peer** — with its
own credential format, transport, ACL, and audit. ADR-0014 remains
authoritative for the operator surface; ADR-0016 does not change it.

The Phase 3 contract lives in `mnemos/federation/proto/federation.proto`
(`package mnemos.federation.v1`; RPCs `Pull`, `SyncMetadata`, `Subscribe`;
enum `TriggerCodes`). ADR-0017 (`mnemos-mesh/docs/adr/0017-mnemos-mesh-architecture.md`)
records the mesh architecture; this ADR records the **threat model** ADR-0017
forward-references as "to be authored by `@GCW: Senior Security Engineer`".

### Scope — what ADR-0016 covers vs ADR-0014

| Surface | ADR-0014 (operator) | ADR-0016 (federation peer) |
| --- | --- | --- |
| Caller | Human operator (browser, CLI, Tauri) | Another mnemos instance via `mnemos-mesh` |
| Token format | `mnk_<random>` (256-bit, `secrets.token_urlsafe`) | `mnk_fed_<peer_id>_<random>` (per-peer, same 256-bit entropy) |
| Token binding | Per user / per token id | Bound to a specific peer identity (cert CN) |
| 2FA | TOTP mandatory on remote bind | N/A — machine-to-machine, mTLS replaces 2FA |
| Transport | HTTP behind a TLS-terminating proxy | mTLS gRPC, private CA, pinned fingerprints |
| Access control | Loopback bypass or session-gated | Per-peer ACL GATE (projects / types / tags) |
| Audit | `auth_tokens` table, access log | `federation_access_log` (SHA-256 topic hash, not plaintext) |
| Rate limiting | `slowapi` per IP + per token | Exhaustive-response contract + trigger codes (no query budget) |

### Acronyms used in this ADR

- **STRIDE** — Spoofing, Tampering, Repudiation, Information disclosure, Denial of service, Elevation of privilege (Microsoft threat-modelling framework)
- **mTLS** — mutual TLS (both sides present and verify certificates)
- **ACL** — Access Control List
- **KMS** — Key Management Service (envelope encryption key store)
- **CA** — Certificate Authority
- **SPKI** — Subject Public Key Info (the hashable part of a cert used for pinning)
- **CWE** — Common Weakness Enumeration (MITRE catalog of weakness types)
- **PII** — Personally Identifiable Information
- **TTL** — Time To Live
- **RPC** — Remote Procedure Call

## Decision

Adopt a **per-peer credential stack** for the federation surface:
per-peer bearer `mnk_fed_<peer_id>_<random>` bound to a pinned mTLS
client cert, gated by a per-peer ACL, audited by `federation_access_log`,
capped by the exhaustive-response contract and trigger codes. The
ephemeral classification is a **policy layer, not a technical guarantee**.
Moderation remains a three-layer defence-in-depth; 100% detection is not
claimed.

### 1. Per-peer bearer token — `mnk_fed_<peer_id>_<random>`

Distinct from operator tokens (`mnk_`). Per-peer, not per-user. The token
authenticates a mesh node to another mesh node and is bound to a specific
peer identity.

- **Generation**: `secrets.token_urlsafe(32)` → 256 bits of entropy, same
  primitive as ADR-0014. Prefix `mnk_fed_` followed by the peer id and an
  underscore, e.g. `mnk_fed_mnemos-B_<43-char-base64url>`. The `fed_` infix
  is grep-able in logs and code scanners (gitleaks-style) so a leaked
  federation token is distinguishable from an operator token.
- **At rest**: SHA-256 hash via PBKDF2-HMAC-SHA256 at 600 000 iterations
  with the fixed salt `mnemos.api.auth.fernet.v1`, identical to ADR-0014.
  A stolen peer-credential DB does not yield a usable bearer.
- **Transport**: `Authorization: Bearer <token>` on the gRPC metadata
  channel **and** mTLS client cert presented on the same connection. The
  token alone is insufficient — both must validate. This is stricter than
  ADR-0014, where a bearer + TOTP session is enough.
- **Binding**: the `peer_id` encoded in the token prefix must match the
  client cert CN presented on the mTLS connection. Mismatch → 401 before
  any RPC dispatch. This prevents a stolen token from being replayed by a
  different peer (the attacker would also need the peer's private key).
- **Lifecycle**: tokens have an optional `expires_at`; unset = no expiry.
  Operator-driven rotation on a cadence (default 90 days, same as cert).
- **Revocation**: `vesma fed token revoke <peer_id>` deletes the row; next
  request with that bearer returns 401. Immediate, no denylist.

### 2. mTLS client cert pinned per peer

Mutual TLS between mesh nodes, private CA, pinned fingerprints. Operator
trust is explicit: the operator ships their own root and pins per-peer
leaf fingerprints. No public CA, no certificate transparency exposure.

- **CA**: private, operated out-of-band by the operator. Never a public
  CA. Rationale: federation is a closed trust set — public CA trust adds
  exposure without value.
- **Cert**: one leaf cert per node, CN = `node_id`. The mesh extracts
  `peer_id` from the verified client cert CN and uses it for (a) per-peer
  ACL lookup and (b) the `peer_id` field in every RPC request.
- **Pinning**: SPKI pinning or full-cert pinning, per operator choice.
  Configured in `~/.mnemos/mesh.yaml` under `mtls.peer_fingerprints`. A
  peer cannot impersonate another node: cert verification fails before
  any RPC is dispatched, and the pinned fingerprint provides a second
  check on top of CA validation, defending against a compromised CA.
- **Rotation**: `mtls.rotation_days` (default 90); mesh reloads on SIGHUP
  without dropping live streams. The CA rotates less frequently and is
  distributed out-of-band.
- **Why mTLS over the bearer alone**: the bearer authenticates "which
  token"; mTLS authenticates "which cryptographic identity holds the
  matching private key". Requiring both raises the bar from "steal the
  token" to "steal the token AND the peer's private key" — the latter is
  a host compromise, not a credential-in-transit theft.

### 3. Per-peer ACL — projects / types / tags whitelist

A whitelist of projects, memory types, and tags is configured per peer
and enforced as a **GATE** before any record leaves the node. This is
the federation analogue of ADR-0014's auth-required-on-remote-bind rule:
a misconfigured peer must not silently leak content the operator did not
authorise.

- **Where enforced**: mnemos, not the mesh. The mesh carries the
  ACL-relevant fields (`peer_id`, `project_scope`, request filters) in
  the RPC; mnemos checks them against its per-peer config before
  returning any record. Because the mesh does not decrypt, it cannot
  inspect content to enforce a content ACL.
- **Three-layer gate** (mirrors ADR-0014's fail-closed posture):
  1. mTLS identity check (mesh) — reject unknown peers before dispatch.
  2. ACL gate (mnemos) — filter projects / types / tags against the
     per-peer whitelist.
  3. Moderation (mnemos) — secrets + PII + neutral-value, on the current
     version, no verdict cache (КП-3 cancelled, session 3).
- **Config schema**: see `mnemos-mesh/docs/architecture.md` §8. A peer
  with an empty `projects` list receives nothing — fail-closed, not
  fail-open.

### 4. Trigger codes — exhaustive-response contract

Replaces the earlier "query budget" model (КП-3, cancelled session 3).
B initially gives an **exhaustive answer** (`EXHAUSTIVE`); on repeat
queries B softly cuts off A with `ALREADY_EXHAUSTED`. The codes are enum
values (not prose) so agents can act on them deterministically and they
cost minimal tokens.

- **`EXHAUSTIVE` (0)** — B gave an exhaustive answer. A does not repeat.
- **`ALREADY_EXHAUSTED` (1)** — repeat query on an exhausted topic. B
  checked `federation_access_log` and found it already returned
  `EXHAUSTIVE` for the same topic hash. A does not repeat.
- **`PARTIAL` (2)** — partial answer. A may refine the query, not
  repeat verbatim.
- **`REFUSED` (3)** — B refused (moderation could not sanitise). A falls
  back to local `mnemos_search`.
- **`OFFLINE_LITE` (4)** — B answered in degraded mode. A receives a
  partial result and may supplement with local search.

The enum is defined in `federation.proto` as `TriggerCodes`. Exactly five
values — new codes require a contract amendment, not an ad-hoc addition.

### 5. `federation_access_log` — anti-correlation tracking

B-side audit field, not a separate store. Records who queried what, when,
under which project scope — with a SHA-256 topic hash, not a plaintext
query (КП-5 accepted session 3, §0.п.8).

- **Fields**: `(peer_id, timestamp, topic_hash, project_scope, trigger_code, access_log_entry_id)`.
  The `topic_hash` is `SHA-256(query_topic)` — one native operation,
  nanoseconds, no external dependency.
- **No plaintext query** — privacy constraint (КП-5). The log is forensic,
  not a search index; storing plaintext queries would create a new leak
  surface (the log itself).
- **Used by** `ALREADY_EXHAUSTED`: B checks the log on repeat queries;
  if it already returned `EXHAUSTIVE` for the same topic hash, it answers
  `ALREADY_EXHAUSTED` without re-running the full moderation pipeline.
- **Cross-peer forensics**: A may surface `access_log_entry_id` in its
  own audit trail so an operator can correlate a peer's pull history
  across nodes.

### 6. Ephemeral enforcement — policy layer, not technical guarantee

The `ttl_class=ephemeral` marker on A2A `share-finding` messages is a
policy contract, not a cryptographic guarantee. An LLM agent may still
call `mnemos_add` on received ephemeral content. This is accepted as a
residual risk (Q3 confirmed session 3, §0.п.13).

- **Policy contract**: A2A message `ttl_class=ephemeral` + agent body
  rule "no `mnemos_add` for content received with `ttl_class=ephemeral`".
- **Detection post-hoc**: a background scanner on A looks for sanitized
  patterns (RFC 5737 IPs, `user@example.com`, `example.invalid`
  hostnames) re-appearing among published records. Hits → flag + audit
  log + operator review.
- **Forensic, not preventive**: the contract provides an audit trail,
  not a block. Prevention is policy + agent body, not a technical gate.

### 7. Moderation pipeline — three-layer defence-in-depth

The same pipeline built once in Phase 0 (`vesma/src/vesma/moderation.py`)
and reused for both batch export and live pull. No verdict cache (КП-3
cancelled) — B checks the current version on every request.

| Layer | Where | When | What it catches |
| --- | --- | --- | --- |
| 1. Write-path scanner | `mnemos_add` | every write | Most secrets / PII at ingest time |
| 2. Background scanner | MCP server, background job (default 6 h) | scheduled | False negatives the write-path scanner missed |
| 3. Moderation on export / pull | Export (Phase 0) / Pull (Phase 2) | every export / pull | Records without the `mnemos:no-federate` tag but with sensitive content |

100% detection is not claimed — the contract accepts moderation false
negatives as a residual risk, mitigated by the three layers, not
eliminated.

## Threat model (STRIDE)

Twelve threats identified against the federation peer surface. Each row
gives the threat, the STRIDE category, the mitigation, and the residual
risk accepted. The mitigations map to the seven decisions above and to
ADR-0014 where applicable.

| # | Threat | STRIDE | Mitigation | Residual risk accepted |
| --- | --- | --- | --- | --- |
| F1 | **Peer impersonation** — attacker presents a valid cert for a peer they are not | Spoofing | mTLS with private CA + pinned fingerprints (decision 2); `peer_id` from cert CN must match token prefix (decision 1 binding) | Compromised CA + stolen pinned fingerprint → impersonation. Accepted: private CA is operator-controlled, rotation 90 days |
| F2 | **Stolen federation token replay** — attacker captures a `mnk_fed_` token and replays it | Spoofing / Information disclosure | Token bound to peer cert CN (decision 1 binding); mTLS required on the same connection; token alone is insufficient | Attacker also needs the peer's private key → host compromise, not in-transit theft. Accepted at the "host is trusted" trust boundary |
| F3 | **Token theft from disk** — reading the peer-credential DB or config | Information disclosure | PBKDF2-HMAC-SHA256 hash at rest, same as ADR-0014 (600 000 iterations, fixed salt) | None material — stolen DB yields hashes, not usable tokens |
| F4 | **Token theft from logs / metadata** — leaked into gRPC access logs | Information disclosure | Bearer in gRPC metadata, never query string; access log scrubber (per `sensitive-data.instructions.md`); `mnk_fed_` prefix grep-able for scanners | Misconfigured logging that captures metadata verbatim. Accepted: documented in runbook, scanner-detectable |
| F5 | **ACL bypass via crafted `project_scope`** — peer requests a project not in its whitelist | Elevation of privilege | Per-peer ACL GATE evaluated by mnemos against `peer_id + project_scope` (decision 3); empty `projects` list = nothing returned (fail-closed) | Operator misconfigures the whitelist. Accepted: config is operator-owned, fail-closed default prevents accidental over-sharing |
| F6 | **Multi-query inference** — A correlates multiple exhaustive answers from B to reconstruct a record B did not authorise | Information disclosure | Exhaustive-response + trigger codes (decision 4); `federation_access_log` detects repeats (decision 5); `ALREADY_EXHAUSTED` caps repeats | Per-query anonymization ≠ per-session protection. Accepted residual risk (ArchCom contract §8) — mitigated, not eliminated |
| F7 | **Ephemeral non-enforcement** — A's agent persists ephemeral content via `mnemos_add` | Repudiation / Information disclosure | Policy contract + agent body rule + post-hoc pattern scanner (decision 6); forensic audit trail | LLM may violate the agent body rule. Accepted: ephemeral is a policy layer, not a technical guarantee (Q3, session 3) |
| F8 | **Moderation false negative** — sensitive content passes all three layers and is exported | Information disclosure | Three-layer defence-in-depth (decision 7); no verdict cache (КП-3 cancelled) — current version checked on every request | 100% detection impossible. Accepted residual risk — mitigated by three layers, not eliminated |
| F9 | **Transport capture / MITM** — attacker on the network path captures or alters federation traffic | Tampering / Information disclosure | mTLS (decision 2) provides integrity + confidentiality on the wire; envelope encryption (ADR-0017 §4) protects at-rest-in-transit | None material — captured packets are ciphertext; mesh compromise leaks ciphertext, not plaintext (criterion 1) |
| F10 | **Replay of a `Pull` / `Subscribe` request** — attacker replays a captured request | Spoofing / Repudiation | mTLS handshake provides forward secrecy; `message_id` idempotency (A2A R-checks); `federation_access_log` records request history | Replay within the mTLS session window. Accepted: session-bound, short-lived |
| F11 | **Denial of service — peer floods B with pulls** | Denial of service | Exhaustive-response contract (decision 4) caps repeat queries; `ALREADY_EXHAUSTED` short-circuits; operator can rate-limit per peer in mesh config | A peer determined to enumerate topic hashes can force many moderation passes. Accepted: bounded by exhaustive-response + operator rate-limit |
| F12 | **Cross-peer audit gap** — operator cannot trace which peer pulled which topic | Repudiation | `federation_access_log` (decision 5) records `(peer_id, timestamp, topic_hash, project_scope, trigger_code)`; `access_log_entry_id` surfaces in A's audit trail | Topic hash is SHA-256, not plaintext (КП-5). Accepted: privacy-preserving by design, operator can correlate hashes to known queries |

## Consequences

### Positive

- **Per-peer credential isolation.** A token for `mnemos-B` is useless
  against `mnemos-C` — the cert CN binding (decision 1) prevents it. One
  peer's compromise does not cascade.
- **Defence-in-depth on content.** Three moderation layers + per-peer
  ACL + mTLS mean a single failed layer does not leak content. The mesh
  never decrypts (ADR-0017 criterion 1), so a mesh compromise leaks
  ciphertext only.
- **Auditable by design.** `federation_access_log` gives every pull a
  forensic trail; trigger codes make the response contract machine-parseable
  and token-cheap.
- **Reuses ADR-0014 primitives.** The 256-bit entropy, PBKDF2 hashing,
  fail-closed posture, and "no query string" transport rule all carry
  over — no new crypto to audit.
- **No query budget to misconfigure.** The exhaustive-response contract
  replaces a numeric limit with a semantic contract — agents reason
  about `EXHAUSTIVE` / `ALREADY_EXHAUSTED`, not "am I over budget?".

### Negative

- **Two credential types to operate.** Operator tokens (`mnk_`) and
  federation tokens (`mnk_fed_`) are distinct; operators must not mix
  them. The `fed_` infix and per-peer binding reduce confusion but do
  not eliminate it.
- **Private CA operational burden.** Cert generation, rotation, root
  distribution, fingerprint pinning — all operator-owned. Mitigated by
  the runbook (to be authored alongside `mnemos-mesh/PLAN.md`).
- **mTLS + bearer is two checks, not one.** Every pull authenticates
  twice (cert + token). Correct, but adds latency and a second failure
  mode (cert expired while token valid).
- **Ephemeral is not enforced.** The policy layer (decision 6) is the
  best available without breaking the agent's ability to use received
  context. Operators who need a hard guarantee must not enable
  federation for that content.

### Neutral

- The threat model applies to Phase 1 (networked prerequisites) and
  Phase 2 (live pull). Phase 0 (batch sync) is offline and carries only
  the SSH-key risk documented in ArchCom 2026-07-20 — this ADR does not
  change Phase 0's threat surface.
- `federation_access_log` is B-side only. A-side audit is the operator's
  existing mnemos logs + the `access_log_entry_id` pointer. No new
  A-side store is introduced.

## Accepted residual risks

Three residual risks are accepted explicitly. They are mitigated, not
eliminated; the mitigation reduces likelihood or blast radius, not to
zero. Each is recorded so a future re-evaluation can check whether the
mitigation still holds.

1. **Multi-query inference** (F6). A can correlate multiple exhaustive
   answers from B to reconstruct a record B did not authorise. Mitigated
   by exhaustive-response + trigger codes + `federation_access_log`;
   not eliminated. Per-query anonymization ≠ per-session protection.
   Re-evaluate if a peer is observed correlating topic hashes beyond
   reasonable use.

2. **Ephemeral non-enforcement** (F7). A's agent may persist ephemeral
   content via `mnemos_add` despite the policy contract. Ephemeral is a
   policy layer, not a technical guarantee. Detection is post-hoc
   (pattern scanner), not preventive. Re-evaluate if the post-hoc scanner
   reports frequent violations from a specific agent.

3. **Moderation false negatives** (F8). Sensitive content may pass all
   three defence-in-depth layers and be exported. 100% detection is
   impossible with deterministic, non-LLM moderation. Re-evaluate if a
   false negative is observed in the wild — add a pattern or tighten
   the existing rules.

## Alternatives considered

Four alternatives were raised during the ArchCom challenge phase
(`2026-07-17-federation-contract.md` §0 and §4; `2026-07-20-automated-channel.md`).
Each is recorded with the reason it was rejected.

| Alternative | Rejected because |
| --- | --- |
| **Query budget** (numeric limit per session) | Replaced by exhaustive-response + trigger codes (session 3, §0.п.3). A numeric budget is misconfigurable, game-able by topic hashing, and opaque to agents. The semantic contract (`EXHAUSTIVE` / `ALREADY_EXHAUSTED`) is machine-parseable and token-cheap |
| **Verdict caching on B** (КП-3) | Cancelled session 3 (§0.п.6). B checks the current version on every request, no verdict cache. A cache would serve stale redaction after a record is updated; it also overlaps with КП-1 (both are caches the owner declined) |
| **In-memory response cache on A** (КП-1) | Rejected session 3 (§0.п.4). Burdens every MCP to hold a cache → double storage → disk pressure. Replaced by reusing the existing `raw_content` / `clean_content` pipeline + the new `federation_access_log` field on B |
| **Two-tier anonymization** (КП-4) | Deferred session 3 (§0.п.7). Basic trust is axiomatic (connecting requires setup). Extended integration tiers (`patterns` / `trusted`) are a distant prospect. Recorded as a handshake stub for future expansion, not in the current roadmap |

## Decision log — transport-level replay protection (2026-07-31)

Amendment recorded after ADR acceptance (2026-07-21). Origin: GitHub issue
#88 point #4, reopened during the #88 closure review — does federation need a
separate transport-level replay-protection layer (nonce + timestamp + HMAC)
ON TOP of mTLS, as defense-in-depth?

- **Date**: 2026-07-31
- **Status**: Rejected — mTLS-only retained. No in-band HMAC replay layer is
  added.
- **Deciders**: GCW Tech Lead (chair), GCW Senior Security Engineer
  (recommend against), GCW Senior System Engineer (recommend against).
  Consensus 2:0 against the additional layer; chair accepted.
- **Body**: Architectural Committee (ArchCom) — a design adjudication on an
  existing accepted threat model, not an implementation request.

### Decision

The federation transport keeps its existing replay / transport protection
and rejects a separate in-band HMAC layer:

- mTLS (TLS 1.3 AEAD + anti-replay window + per-connection keys);
- pinned SPKI fingerprints (decision 2 — mitigation of F1);
- per-peer ACL GATE (decision 3);
- `federation_access_log` audit (decision 5).

No nonce / timestamp / HMAC layer is added on top of mTLS.

### Rationale

1. **Correlated failure, not independent depth.** An HMAC key held in the
   same operator infra as the mTLS cert material is not an independent
   layer — one host or CA compromise breaks both. Defense-in-depth requires
   independent failure domains, which a co-located key does not provide.
2. **CA-compromise already mitigated by pinning.** The `pinVerifier` checks
   the leaf cert SHA-256 against a per-peer pin set (decision 2 / F1). A
   cert minted via a compromised CA has a new SPKI not in the pin set → the
   handshake fails. An HMAC layer would not strengthen this path.
3. **mTLS replay protection is sufficient for the workload.** TLS 1.3 1-RTT
   has inherent replay protection, and the application layer is already
   replay-tolerant: `Pull` / `SyncMetadata` are idempotent keyed on record
   id, and `Subscribe` is cursor-based with id-dedup on import. A replay
   yields at worst duplicates, not corruption.
4. **The layer adds its own attack surface.** A separate HMAC layer needs a
   new stateful nonce store (in-memory per mesh criterion 6/11 → lost on
   restart → leaky within the TTL window), an NTP / clock-skew dependency,
   and a duplicate key-distribution problem — the HMAC key is either
   out-of-band like certs (duplicating cert management) or derived from the
   mTLS session (adding nothing). It pulls crypto into a mesh that is
   deliberately crypto-free.

### Residual risk accepted

Session-bound, short-lived replay within the TLS session — **unchanged**.
This is F10 in the threat table above. The decision confirms F10's residual
risk as accepted; no new residual risk is introduced.

### CA-side hardening track (optional)

If the threat context changes, the cheaper and higher-leverage move is to
strengthen the CA side rather than add a fourth crypto layer:

- HSM-backed or offline root CA;
- shorter cert TTL (e.g. 30 days vs the current 90-day default);
- CT-style issuance logging;
- alerting on pin-set changes.

This is **recommendation (c)** from the committee — optional, not adopted
by this decision.

### Triggers to revisit

Re-open this decision if any of the following becomes true:

1. Transition to multi-tenant / hosted federation.
2. Entry into a regulated industry (compliance mandates a distinct layer).
3. Cross-domain federation with third-party (untrusted) peers.
4. A concrete CVE in Go `crypto/x509` or gRPC TLS that pinning cannot
   mitigate.

### Cross-references

- **vesma decision `82a3608b`** — full committee rationale (mnemos
  `mnemos:decision`, `project:vesma`, `committee`). The open question
  `c3fd3fbc` is superseded by this decision (`committee:queue` removed).
- **GitHub issue #88** point #4 — the design task this decision closes.
- **F10** (threat table above) — the residual risk this decision confirms.
- **ADR-0017** (`mnemos-mesh/docs/adr/0017-mnemos-mesh-architecture.md`) —
  the mesh architecture this threat model sits on; mTLS, pinning, and the
  per-peer ACL are defined there.

## References

- **ADR-0014** — `vesma/docs/project/adr/0014-api-auth-threat-model.md` — operator API auth (bearer + TOTP 2FA), federation explicitly excluded. This ADR extends it.
- **ADR-0017** — `mnemos-mesh/docs/adr/0017-mnemos-mesh-architecture.md` — mnemos-mesh architecture (trust boundaries §3, key management §4, mTLS §7, per-peer ACL §8). ADR-0017 references this ADR as the formal residual risk record.
- **ArchCom contract 2026-07-17** — `.archcom/sessions/2026-07-17-federation-contract.md` — §3.2 mediated pull contract, §7.1 safe channel (Q1), §8 risks, §9 trigger codes, §10 `federation_access_log`.
- **ArchCom 2026-07-20** — `.archcom/sessions/2026-07-20-automated-channel.md` — ADR-0016 scope explicitly defined (§3.2): per-peer bearer `mnk_fed_<peer_id>_` + mTLS client cert pinned per peer.
- **Peer API proto** — `mnemos/federation/proto/federation.proto` (`package mnemos.federation.v1`; RPCs `Pull`, `SyncMetadata`, `Subscribe`; enum `TriggerCodes`).
- **mnemos-mesh architecture** — `mnemos-mesh/docs/architecture.md` — trust boundaries §3, key management §4, mTLS §7, per-peer ACL §8.
- **Compact format source of truth** — `vesma/src/vesma/compact.py` (`CompactRecord` Pydantic model; proto mirrors it field-by-field).
- **Moderation pipeline** — `vesma/src/vesma/moderation.py` (secrets detector + PII scrubber + neutral-value replacement).
- **GitHub issue #105** — <https://github.com/Korrnals/vesma/issues/105> (Phase 3 contract design).
- **OWASP ASVS** — baseline for the secrets / PII / transport controls cited in F3, F4, F9.
- **CWE** — `CWE-522` (insufficiently protected credentials) for the at-rest hashing requirement; `CWE-295` (improper certificate validation) for the mTLS pinning requirement.

<!-- end of ADR-0016 -->