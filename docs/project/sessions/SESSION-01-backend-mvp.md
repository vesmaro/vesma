# SESSION: mnemos — Backend MVP for mnemos-eyes (Auth + CORS + hardening)

*Historical artifact — English only.*

> **How to start this session:** tell the agent
> _"проинициализируй сессию из `docs/sessions/SESSION-01-backend-mvp.md`"_.
> The agent (acting as `@GCW: Tech Lead`) reads this file, restores context,
> and dispatches the tasks below to the named specialists.

- **Project:** `vesma` (`git@github.com:Korrnals/mnemos.git`)
- **Repo path:** `/var/home/abyss/LABs/AI/vesma`
- **Session owner / orchestrator:** `@GCW: Tech Lead`
- **Specialists:** `@GCW: Senior Security Engineer`, `@GCW: Senior System Engineer`, `@GCW: Senior QA Engineer`
- **Status:** � **Complete** — all tasks delivered to `main` (HEAD `d2f2025`), CI green. See §10 Outcome.
- **Companion session:** `mnemos-eyes` L1 viewer — see `../mnemos-eyes/docs/sessions/SESSION-01-l1-viewer.md`

---

## 1. Why this session exists

`mnemos-eyes` (the GUI) needs the mnemos backend to be reachable from a browser
and protected. Today mnemos is loopback-only with **no CORS and no auth**. This
session delivers those, plus two approved hardening items from the M19 code
review that were green-lit but not yet implemented.

This is the **gating dependency** for `mnemos-eyes` task T6 (real data + login).

## 2. Goal

1. **AuthN/AuthZ** for the mnemos HTTP API (token-based; 2FA/TOTP for remote).
2. **CORS** support (configurable allow-list).
3. **`GET /tags`** endpoint (convenience for the GUI; client-side aggregation
   works for L1 but a real endpoint is cleaner).
4. **MCP-tool dispatch smoke-test** (`mcp_server.py` is at 0% coverage).
5. **v2 SSRF guard:** per-hop redirect re-validation in `ingest_url`.

## 3. 🔴 Prerequisite — reconcile `feat/m15-production-hardening` → `main`

**Investigated 2026-06-17. The released `main` (tag `v0.2.0`) is missing the
M15 hardening AND the M19 code-review fixes.** Specifically:

- PR #5 **was merged**, but its base was **`feat/m15-production-hardening`**,
  not `main`. That branch never reached `main`.
- Therefore `origin/main` **still has the High-severity SSRF**:
  `src/vesma/manager.py` uses `follow_redirects=True` (line ~490). It also
  lacks `VectorStore.close()`.
- Divergence is clean: `origin/feat/m15-production-hardening` is **exactly 2
  commits ahead of `origin/main`**, and `main` has **0** commits the branch
  lacks. The fix (`follow_redirects=False`) is confirmed present on the branch.

**First action of this session (T0):** review and merge **PR #16**
(`feat/m15-production-hardening → main`, already open). It is a clean merge that
lands the live SSRF remedy + `VectorStore.close()`. This is **security-urgent**
— the released v0.2.0 is exploitable until it merges. Only after T0 does the **v2
SSRF** task (T5) make sense, since it extends the v1 `follow_redirects=False`
posture. After merge: tag `v0.2.1`, and retarget the 9 dependabot PRs to `main`.

## 4. Context to restore (read first)

| File | Why |
| --- | --- |
| `src/vesma/api/main.py` | The FastAPI app — where CORS + auth middleware + `/tags` go |
| `src/vesma/manager.py` | `ingest_url` + `_validate_url` (SSRF v2 target) |
| `src/vesma/mcp_server.py` | 0% coverage — needs a dispatch smoke-test |
| `docs/security.md` | Existing security posture (SSRF §2, loopback binding) |
| `docs/adr/0009-ssrf-guard-in-ingest-url.md` | SSRF v1 rationale |
| `docs/code-review-2026-06.md` | M19 review (origin of T4/T5) |

## 5. Task breakdown & assignment

| # | Task | Owner | Depends on |
| --- | --- | --- | --- |
| **T0** | **Security-urgent.** Review + merge **PR #16** (`feat/m15-production-hardening → main`, open; 2 commits ahead, clean) — lands the live SSRF fix + `VectorStore.close()`. Then tag `v0.2.1` + retarget 9 dependabot PRs to `main`. | `@GCW: Tech Lead` + `@GCW: Code Reviewer` | — |
| **T-THREAT** | Threat model for the GUI access: local-desktop vs remote/mobile; decide where 2FA is mandatory; token format, storage, rotation; rate-limiting. Output: short ADR. | `@GCW: Senior Security Engineer` | — |
| **T-AUTH** | Implement auth: token-based API auth + **TOTP 2FA for remote/mobile**; login/verify endpoints; protect all read endpoints. Tests. | `@GCW: Senior System Engineer` | T-THREAT |
| **T-CORS** | Add configurable CORS allow-list (env/config driven; default = none/strict). Tests. | `@GCW: Senior System Engineer` | — |
| **T-TAGS** | `GET /tags` endpoint (list tags + counts). Tests. | `@GCW: Senior System Engineer` | — |
| **T4-MCP** | Smoke-test for MCP-tool dispatch in `mcp_server.py` (raise coverage off 0%; assert each registered tool dispatches). | `@GCW: Senior QA Engineer` | — |
| **T5-SSRF** | v2 SSRF: re-validate host **on every redirect hop** (not just initial). Keep `follow_redirects=False` default; add an explicit, bounded, re-validated manual redirect-follow path if needed. Regression tests for redirect→metadata-IP pivot. | `@GCW: Senior Security Engineer` + `@GCW: Senior System Engineer` | **T0** |

## 6. Sequencing

1. **T0** (merge PR #5) — unblocks T5.
2. Parallel: **T-THREAT**, **T-CORS**, **T-TAGS**, **T4-MCP** (independent).
3. **T-AUTH** after T-THREAT.
4. **T5-SSRF** after T0.

## 7. Workflow rules (per repo policy)

- One feature branch + PR per task group (e.g. `feat/api-auth`, `feat/api-cors`,
  `feat/tags-endpoint`, `test/mcp-smoke`, `fix/ssrf-redirect-revalidation`).
- Conventional commits in **English**; respond to user in **Russian**.
- `make verify` must be **green by fix, not suppression** before each PR
  (ruff + ruff format + mypy --strict + bandit + pip-audit + pytest, coverage ≥ 80%).
- Subagents report back to `@GCW: Tech Lead`; the Tech Lead commits.
- No secrets in code/commits; tokens via env/secret store; redact in logs.
- `bandit-report.json` regenerates on `make verify` → `git restore` it if noisy.

## 8. Definition of done

- [x] PR #5 prerequisite reconciled into `origin/main` (the SSRF v1 fix +
      `VectorStore.close()` were already present on `main` via release `1.0.0`;
      T0 verified, no separate merge needed).
- [x] Auth enforced on all read endpoints; 2FA/TOTP path for remote; tests green.
- [x] CORS allow-list configurable; default strict; tests green.
- [x] `GET /tags` returns tags+counts; tests green.
- [x] `mcp_server.py` covered by a dispatch smoke-test (off 0%) **and** routing
      asserts the correct manager method (mcp-3).
- [x] SSRF re-validated per redirect hop; metadata-IP pivot regression test passes.
- [x] `make verify` green; CHANGELOG `[Unreleased]` updated; docs synced.

## 9. Handoff to the GUI session

When **T-AUTH + T-CORS** are merged, notify the `mnemos-eyes` session: its
task **T6** (wire `HttpAdapter` to the real API + login flow) is unblocked.

**T-AUTH and T-CORS are merged → `mnemos-eyes` T6 is UNBLOCKED.**

## 10. Outcome (closed 2026-06-17)

All session goals delivered to `main` (HEAD `d2f2025`, CI green). Final gate on
main: ruff / ruff format / mypy --strict / bandit clean, **449 tests passing**,
coverage **86.52%**.

| Task | Delivered via | Notes |
| --- | --- | --- |
| T0 (SSRF v1 + `close()`) | release `1.0.0` (pre-session) | Verified already on `main`; no separate merge needed |
| T-THREAT | ADR-0014 | Threat model + trust zones; startup guard requires auth+TOTP+TLS off-loopback |
| T-AUTH | PR #19 → #24 | Opaque `mnk_` bearer (SHA-256), TOTP 2FA; review fixes auth-1..8 |
| T-CORS | PR #20 → #24 | Configurable allow-list, strict default; registered outermost (after auth) |
| T-TAGS | PR #21 → #24 | `GET /tags` list + counts, explicit sort |
| T4-MCP | PR #22 → #24 | `mcp_server.py` 0% → 86%; routing asserts correct method (mcp-3, PR #26) |
| T5-SSRF | PR #23 → #24 | Per-hop `_validate_url`, max 5 hops, `follow_redirects=False` kept |
| Hardening | PR #25 | auth-9 (revoked column split), auth-12 (absolute session cap) |

Delivery: 5 feature branches → integration PR #24 (merged) → 2 post-merge
hardening PRs (#25 auth-9/12, #26 mcp-3). All 5 source PRs auto-closed as merged.
Docs synced: CHANGELOG `[Unreleased]`, `config.example.yaml`, `api-reference.md`,
`security.md`, ADR-0014 as-built notes.
