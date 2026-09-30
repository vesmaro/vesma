# 0014. API authentication & TOTP 2FA — threat model

*Historical artifact — English only.*

- **Status**: Accepted
- **Date**: 2026-06-17
- **Deciders**: abyss, GCW Senior Security Engineer, GCW Tech Lead

## Context

Today the Vesma HTTP API (`src/vesma/api/main.py`) is loopback-bound
(`api.host = "127.0.0.1"`, see `src/vesma/config.py::ApiConfig`) with **no
authentication**, **no CORS**, and **no rate limiting**. The implicit trust
boundary is the OS user — anyone with a shell on the host can `curl` it.

A new client, **`mnemos-eyes`** (a browser/React SPA today, Tauri desktop +
mobile later), needs to read and mutate the same API. Three deployment
scenarios are now in play:

1. **Local desktop** — operator runs both `vesma serve` and `mnemos-eyes` on
   the same host. The browser hits `http://127.0.0.1:8787`.
2. **Remote (LAN / VPN / tunnel)** — operator runs `vesma serve` on a home
   server / VPS; a browser or mobile app reaches it from another device over
   the network.
3. **Mobile** (future Tauri build) — same as remote, but from a phone that
   may roam onto untrusted networks.

Scenarios 2 and 3 break the "loopback = trusted" assumption. They put the API
on a network where credential theft, brute force, and CSRF are real. M15
closed SSRF (ADR-0009, ADR-0012); ingress auth is the next hardening front,
and it must land **before** any documented non-loopback bind. This ADR is the
gating threat model for **T-AUTH** (implementation) and **T-CORS** (browser
allow-list).

## Decision

Vesma v1.x adds an opt-in authentication layer with two trust zones and
mandatory 2FA on the higher-trust zone. **The implementer (T-AUTH) builds
exactly the contract in the "Implementation contract" subsection below.**

### Trust zones

| Zone | Bind | Auth required | TOTP required |
| --- | --- | --- | --- |
| **loopback** | `127.0.0.1` / `::1` only | optional (default off) | optional (default off) |
| **remote** | any non-loopback bind (`0.0.0.0`, LAN IP, hostname) | **mandatory** | **mandatory** |

Enforcement is **server-side at startup**, not advisory:

- If `api.host` resolves to a non-loopback address **and** (`api.auth_enabled
  is False` **or** `api.totp_enabled is False`), `vesma serve` exits non-zero
  with a clear error. No flag bypasses this. Rationale: a misconfigured
  "I'll add auth later" deploy is exactly how memory servers leak in the wild.
- Loopback may stay plain for the local-desktop scenario because the trust
  boundary is already the OS user; adding 2FA there is friction with no
  threat-model gain. Operators may still opt in (`api.auth_enabled: true`)
  for defense-in-depth.

### Token format & lifecycle

**Opaque random bearer tokens**, not JWT. Justification for a self-hosted
single-user / few-user memory server:

- No federation, no third-party verifier — there is no audience that benefits
  from JWT's "verify without calling the issuer" property.
- Revocation is **immediate** (delete the row / file entry) instead of
  needing a JWT denylist or a forced rotation of the signing key.
- No JWT footguns (`alg: none`, weak HMAC keys, leaking `kid`, library
  CVE churn).
- Operator UX matches a personal access token: short string, copy once,
  store in a password manager or `~/.config/mnemos-eyes/token`.

Generation, storage, rotation:

- **Generation**: `secrets.token_urlsafe(32)` → 256 bits of entropy. Prefix
  with `mnk_` (mnemos key) so leaked tokens are grep-able in logs and code
  scanners (gitleaks-style detection).
- **At rest**: only the **SHA-256 hash** of the token is persisted (config
  file or sqlite `auth_tokens` table). The plaintext is shown **once** on
  creation (`vesma auth token create`) and never again. Reason: a config-file
  read or a stolen DB does not yield a usable credential.
- **Transport**: `Authorization: Bearer <token>` header only. Never accept
  the token in a query string (would be logged in proxies and browser
  history).
- **Lifecycle**: tokens have an optional `expires_at`; unset = no expiry.
  Operator-driven rotation: create new token, distribute, revoke old. No
  auto-rotation in v1 (single-operator deployments do not benefit).
- **Revocation**: `vesma auth token revoke <token_id>` deletes the row;
  next request with that bearer returns 401.

### TOTP enrollment & verify flow

TOTP secret per token (or per user, equivalent in the single-user model).
Library: **`pyotp`** (RFC 6238, mature, no deps).

1. **Enrollment** — `vesma auth totp enroll [--token-id <id>]`:
   - Server generates a 160-bit secret (`pyotp.random_base32(length=32)`).
   - Returns the `otpauth://` URI + an ASCII QR (use `qrcode[pil]` only at CLI
     time; do not pull it into the server runtime).
   - Operator scans into Aegis / 1Password / Authy.
   - Server stores the secret **encrypted at rest** with a key derived from
     a separately-configured `api.totp_master_key` (env-only, never on disk).
     If the master key is absent, the server refuses to enable TOTP. Reason:
     a stolen `~/.mnemos/auth.db` must not yield usable TOTP secrets.
2. **Login** — `POST /auth/login` with `{ "token": "mnk_..." }`:
   - Server hashes the supplied token and looks it up. On miss → 401 with a
     constant-time response.
   - On hit, if TOTP is enabled for that token, returns
     `{ "challenge_id": "...", "ttl_sec": 120 }`. The `challenge_id` is a
     short-lived (≤2 min), single-use random ID kept server-side. **No
     session is issued yet.**
   - If TOTP is disabled (loopback-only mode), returns the session token
     directly (see step 3).
3. **Verify** — `POST /auth/verify` with `{ "challenge_id": "...", "code":
   "123456" }`:
   - Server validates the 6-digit code with `pyotp.TOTP(secret).verify(code,
     valid_window=1)` (±30 s clock skew).
   - On success, issues a **session token** (separate from the bearer
     token): `Set-Cookie: mnemos_session=...; HttpOnly; Secure; SameSite=Strict`
     **and** returns the same value in the JSON body so non-browser clients
     (mnemos-eyes Tauri, mobile, curl) can use the `Authorization:
     Bearer <session>` form.
   - Session token TTL: `api.session_ttl_sec` (default 8 h). Sliding window
     refresh on each authenticated request, hard cap at 24 h since issue.
   - On failure: 401, increment per-token failure counter, see rate-limit.
4. **Logout** — `POST /auth/logout` clears the session row + cookie.

### Rate limiting & lockout

Use `slowapi` (a lightweight Starlette/FastAPI port of `flask-limiter`,
already in the Python ecosystem and mypy-friendly) with an in-process
in-memory backend in v1 (sqlite-backed in v2 if multi-worker is needed).

| Endpoint | Limit | Lockout |
| --- | --- | --- |
| `POST /auth/login` | 5 req / min per source IP **and** per token hash | After 10 failures in 10 min on the **same token hash**: token disabled for 15 min; logged at WARN |
| `POST /auth/verify` | 5 req / min per challenge_id; challenge invalidated after 5 failed codes | After 3 consecutive failed TOTP for a token: token disabled for 15 min; logged at WARN |
| All `/auth/*` | 30 req / min per source IP (umbrella) | n/a |
| Everything else (memory API, `/v1/sessions/*`) | 120 req / min per session token | n/a |

Source-IP limits use `X-Forwarded-For` only when `api.trusted_proxies` is
explicitly set (default: empty → use `request.client.host`). Defends against
the "rate-limit bypass via forged header" class.

### Transport

- **Loopback**: plain HTTP is acceptable. The kernel boundary is the trust
  boundary.
- **Remote**: TLS is **mandatory**. Vesma itself does **not** terminate TLS
  in v1 — it expects a reverse proxy (Caddy, Nginx, Traefik) in front. The
  server refuses to bind non-loopback if `api.behind_tls_proxy` is not set
  to `true` in config; this is an explicit operator acknowledgement, not a
  network probe.
- Session cookies set `Secure` only when the request was received over a
  TLS-terminating proxy (i.e. `X-Forwarded-Proto: https` from a trusted
  proxy, or `api.behind_tls_proxy: true` as a static promise).
- HSTS is the proxy's responsibility, documented in the runbook, not set
  by the app.

### STRIDE-lite threats & mitigations

| # | Threat | Vector | Mitigation |
| --- | --- | --- | --- |
| T1 | **Token theft from disk** | Reading `~/.mnemos/auth.db` or config | Only hashes stored; TOTP secrets encrypted with env-only master key |
| T2 | **Token theft from logs / URLs** | Tokens leaked into access logs | Bearer header only; never query string; access log scrubber documented |
| T3 | **CSRF (browser SPA)** | mnemos-eyes runs in a browser; another tab POSTs to the API | Session cookie is `SameSite=Strict`; **state-changing endpoints additionally require the `Authorization: Bearer <session>` header**, which a cross-origin attacker cannot read or set on a cookie-only request. Cookie alone is insufficient for mutations |
| T4 | **Brute force on TOTP** | Attacker has stolen the bearer, guesses 6 digits | Rate limit + 3-failure auto-disable of the token |
| T5 | **Brute force on bearer tokens** | Attacker enumerates 256-bit space | Computationally infeasible; rate limit is belt-and-braces |
| T6 | **Replay of TOTP code** | Attacker captures a verify request | `challenge_id` is single-use server-side; replay of a code within its validity window is additionally blocked by the per-token `totp_last_step` column (a candidate code's time-step must strictly exceed the last accepted step). See "Implementation notes — as built". |
| T7 | **Replay of session token** | Attacker captures an authenticated request | TLS on remote prevents capture; session bound to creation IP (optional, configurable `api.session_pin_ip`) |
| T8 | **SSRF (recap)** | `mnemos_ingest_url` | Already mitigated, ADR-0009 + ADR-0012 |
| T9 | **CORS bypass** | Malicious site fetches user data | Allow-list (T-CORS) is **defense in depth**, not a security boundary. T3 mitigation is the real defense |
| T10 | **Privilege escalation across tokens** | One token modifying another's TOTP | v1 has no scopes; all tokens are admin. Documented limitation. v2 ticket for scoped tokens |
| T11 | **Master-key loss** | Operator loses `MNEMOS_API__TOTP_MASTER_KEY` | TOTP secrets become unreadable → operator must re-enroll all TOTP. Documented as a known recovery path |
| T12 | **Timing attack on token lookup** | Attacker measures login latency | Token lookup uses constant-time hash compare; login response time is normalised |

### CORS interaction (separate from auth)

CORS allow-list (`api.cors_origins`, delivered by **T-CORS**, not this ADR)
is an instruction to compliant browsers about which origins may read
responses. It is **not** a security control against:

- Non-browser clients (curl, Tauri shell, mobile native HTTP).
- Browsers older than the allow-list rules.
- Bugs in CORS implementations.

Therefore the auth layer (this ADR) **never relies on CORS for security**.
Every state-changing endpoint independently requires `Authorization: Bearer
<session>`. CORS becomes useful only to reduce noise and to make accidental
cross-origin reads fail loudly in development.

## Implementation notes — as built (`integration/backend-mvp`)

This subsection records the specific implementation choices made during
T-AUTH delivery. Where the ADR said "PBKDF2" or "constant-time" without
specifying parameters, these are the settled values.

### Token hashing

Tokens are hashed with **PBKDF2-HMAC-SHA256 at 600 000 iterations** using
the fixed salt string `mnemos.api.auth.fernet.v1` (UTF-8 encoded). The
iteration count follows the OWASP 2024 recommendation for PBKDF2-HMAC-SHA256.
The hash is stored as a hex digest in `auth_tokens.token_sha256`.

### Master-key validation at startup

An empty `api.totp_master_key` when `api.totp_enabled=true` raises a
`ValueError` at process startup, preventing silent TOTP secret exposure.
The check is part of `ApiConfig` model validation (Pydantic `@model_validator`
or equivalent) so the server never reaches the listen loop in an insecure
state.

### TOTP replay prevention

A `totp_last_step` integer column was added to `auth_tokens`. It stores the
TOTP time-step index (Unix timestamp // 30) of the last accepted code. A
new code is accepted only when `current_step > totp_last_step`; equality
(same window) is rejected. The column is `NULL` until the first successful
TOTP verification. This is stricter than `pyotp.verify(valid_window=1)`
alone, which would accept the same code twice within its window.

### Fail-closed middleware

`AuthMiddleware` accesses `request.state.api_config` (injected at
application startup) to determine whether auth is active. If the attribute
is absent — which can happen only due to a startup ordering bug — the
middleware returns **HTTP 503** `{"detail": "Auth not initialised"}` for
every protected route rather than silently allowing the request through.
This is a **fail-closed** posture consistent with ADR-0013's production
hardening principles.

### X-Forwarded-For gating

`X-Forwarded-For` is consumed **only** when the direct peer's IP (from
`request.client.host`) is covered by a CIDR in `api.trusted_proxies`. If
`trusted_proxies` is empty (the default), XFF is always ignored and the
raw client IP is used for rate-limit keying and session-IP pinning. This
closes the "rate-limit bypass via forged XFF" class described in the STRIDE
table (T7).

### CLI environment propagation

`vesma serve` sets `MNEMOS_API__HOST` and `MNEMOS_API__PORT` in the OS
environment before `uvicorn.run(...)` (or the equivalent subprocess exec).
The worker's `@app.on_event("startup")` guard reads these values and calls
`sys.exit(1)` with an explanatory message if the bind address is non-loopback
and `api.auth_enabled` is `False`. This mirrors the design intent described
in the Decision section but adds the concrete environment-variable mechanism
rather than relying solely on in-process config validation.

## Implementation contract for T-AUTH

The implementer of T-AUTH **must** ship exactly the surface below. Anything
not on this list is out of scope for T-AUTH and requires a follow-up ADR.

### Endpoints (new router: `src/vesma/api/auth.py`, mounted at `/auth`)

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `POST` | `/auth/login` | `{"token": "mnk_..."}` | `{"challenge_id": "...", "ttl_sec": 120}` (TOTP on) **or** `{"session": "...", "expires_at": "..."}` (TOTP off) |
| `POST` | `/auth/verify` | `{"challenge_id": "...", "code": "123456"}` | `{"session": "...", "expires_at": "..."}` + `Set-Cookie` |
| `POST` | `/auth/logout` | (session header) | `{"ok": true}` |
| `GET`  | `/auth/me` | (session header) | `{"token_id": "...", "totp": true/false, "expires_at": "..."}` |

### Middleware

- `AuthMiddleware` runs after CORS, before routes.
- Allows `/health`, `/auth/login`, `/auth/verify`, `/docs`, `/redoc`,
  `/openapi.json` without auth.
- All other routes require a valid session (Bearer header **or** the cookie;
  Bearer takes precedence).
- Loopback bypass: if `api.auth_enabled is False` and the request's
  `request.client.host` is in `{"127.0.0.1", "::1"}`, allow through.
  **Reject** with 401 if `auth_enabled is False` but client is not loopback
  (defense-in-depth against future misconfig).

### CLI (extend `src/vesma/cli.py`)

```text
mnemos auth token create [--name <label>] [--expires <iso8601>]
mnemos auth token list
mnemos auth token revoke <token_id>
mnemos auth totp enroll  --token-id <id>
mnemos auth totp disable --token-id <id>
mnemos auth totp test    --token-id <id> --code <123456>
```

### Config additions (`src/vesma/config.py::ApiConfig`)

```python
class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787
    # T-AUTH additions ────────────────────────────────────────────────
    auth_enabled: bool = False               # default off on loopback
    totp_enabled: bool = False               # default off on loopback
    totp_master_key: SecretStr = SecretStr("")  # env-only via MNEMOS_API__TOTP_MASTER_KEY
    session_ttl_sec: int = Field(default=8 * 3600, ge=300, le=24 * 3600)
    session_pin_ip: bool = False             # bind session to creation IP
    behind_tls_proxy: bool = False           # operator-asserted; required for non-loopback bind
    trusted_proxies: list[str] = []          # CIDRs allowed to set X-Forwarded-*
    # T-CORS adds cors_origins here separately
```

Persistence: a new sqlite table in the existing `mnemos.db` file:

```sql
CREATE TABLE IF NOT EXISTS auth_tokens (
    token_id      TEXT PRIMARY KEY,           -- short random id, surfaced in CLI
    token_sha256  TEXT NOT NULL UNIQUE,       -- hex digest of the bearer
    name          TEXT,
    totp_secret_encrypted BLOB,               -- NULL if TOTP disabled
    created_at    TEXT NOT NULL,
    expires_at    TEXT,
    disabled_at   TEXT,                       -- non-NULL ⇒ locked out / revoked
    failure_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    session_sha256 TEXT PRIMARY KEY,
    token_id       TEXT NOT NULL REFERENCES auth_tokens(token_id) ON DELETE CASCADE,
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    client_ip      TEXT                       -- populated iff session_pin_ip
);
CREATE TABLE IF NOT EXISTS auth_challenges (
    challenge_id   TEXT PRIMARY KEY,
    token_id       TEXT NOT NULL REFERENCES auth_tokens(token_id) ON DELETE CASCADE,
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 0
);
```

### Dependencies (add to `pyproject.toml`)

| Package | Why |
| --- | --- |
| `pyotp` ~= 2.9 | RFC 6238 TOTP (small, audited, no transitive deps) |
| `slowapi` ~= 0.1.9 | FastAPI-compatible rate limiter |
| `cryptography` (already transitive via httpx[http2]; pin explicit) | Fernet for TOTP-secret-at-rest using `api.totp_master_key` |

No new runtime services (no Redis, no separate process).

### Tests (mandatory before T-AUTH merges)

- `tests/test_auth.py` — token creation, hash storage, login happy path,
  TOTP enroll + verify, session lifecycle, logout.
- `tests/test_auth_security.py` — covers each STRIDE row T1–T12 above as a
  named test. Specific must-haves:
  - `test_non_loopback_bind_refuses_without_auth_and_totp`
  - `test_state_changing_endpoint_rejects_cookie_only_request` (CSRF / T3)
  - `test_totp_brute_force_locks_token`
  - `test_token_replay_after_revoke_returns_401`
  - `test_totp_secret_unreadable_without_master_key`

### Out of scope for T-AUTH (deferred)

- Scoped tokens / RBAC (single admin role in v1).
- WebAuthn / passkeys (future ADR; TOTP first).
- OIDC / SSO (not for a self-hosted single-operator product).
- Multi-worker session sharing (current uvicorn config is single-worker;
  document the constraint).

## Consequences

**Positive**

- Vesma can be safely exposed on the LAN / over a tunnel without becoming
  the next "open Elasticsearch on the internet" story.
- The browser SPA (`mnemos-eyes`) has a clear contract: bearer for headless,
  cookie + bearer for browser, CSRF-resistant by construction.
- TOTP recovery is honest (lose the master key → re-enroll). No "magic
  email reset" the operator must trust.
- Opaque tokens keep the surface boring: no JWT CVE churn to chase.

**Negative**

- Operators on loopback get a no-cost default (auth off), but switching to
  remote is an explicit multi-step setup: token, TOTP enroll, TLS proxy,
  master key. Documented, but real friction.
- The TOTP master key is a new operator secret class. Loss = re-enroll.
- `slowapi` in-process is single-worker only; multi-worker uvicorn deploys
  must wait for v2 (or use a reverse-proxy-level limiter).
- The 2026-06 audit shipped without auth. Anyone running v0.2.0 on a
  non-loopback bind is exposed; release notes and SECURITY.md must call
  this out alongside the v1.x upgrade path.

**Neutral**

- The `auth_tokens` / `auth_sessions` / `auth_challenges` tables live in
  the existing `mnemos.db`. A future Postgres migration (ADR-pending) must
  cover them alongside the memory and sessions schemas.
- The CSRF strategy (require Bearer header for mutations) means
  `mnemos-eyes` keeps the session token in JS-readable storage to send it
  as a header. That is the intended design — it is the price of CSRF
  immunity without a separate CSRF-token endpoint. Documented for the
  frontend implementer.

## Alternatives considered

- **JWT (HS256 or RS256)**. Rejected for a single-user/few-user
  self-hosted server: no audience for "stateless verify", revocation is
  worse, and the historical CVE surface (alg confusion, `kid` injection,
  weak HMAC keys) is larger than opaque tokens. Reconsider only if Vesma
  ever federates with another service.
- **Basic auth + TOTP**. Rejected: forces the password into every request,
  doubles the credential-rotation pain, no clean revocation.
- **OAuth2 / OIDC against an external IdP**. Rejected for v1 — adds a
  hard third-party dependency for a product meant to run on a Raspberry
  Pi. Revisit in v2 if multi-user deployment becomes a goal.
- **CORS as the primary browser defense**. Rejected: CORS protects browser
  reads, not server state. Mutations need an auth control that is not
  attached to a cookie (T3).
- **Auth off by default everywhere with a big warning**. Rejected: warnings
  are ignored. Server-side refusal to bind non-loopback without auth is
  the only reliable control.
- **mTLS only, no app-layer auth**. Rejected: works for service-to-service,
  not for a phone browser. Could be a future "fleet" mode.

## References

- ADR-0009 — SSRF guard at `mnemos_ingest_url` (the previous boundary
  hardening that this ADR continues).
- ADR-0012 — IPv6 SSRF gap fix (defense-in-depth precedent).
- ADR-0013 — Production hardening gate (sets the standard this work
  upholds).
- `docs/security.md` — operator-facing summary; update under T-AUTH.
- `src/vesma/api/main.py` — current loopback-only API.
- `src/vesma/config.py::ApiConfig` — config surface to extend.
- RFC 6238 — TOTP.
- OWASP ASVS 4.0 — V2 (Authentication), V3 (Session Management), V4
  (Access Control), V11 (BOLA / brute force). Findings tagged accordingly.
- OWASP Top 10 2021 — A01 (Broken Access Control), A02 (Cryptographic
  Failures), A07 (ID & Auth Failures).
- CWE-307 (Improper Restriction of Excessive Auth Attempts), CWE-352
  (CSRF), CWE-798 (Hard-coded credentials — avoided by master-key
  env-only).
