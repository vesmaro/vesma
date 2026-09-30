# 0007. A2A Sessions API: 5 minimal endpoints with summary-mode default

*Historical artifact — English only.*

- **Status**: Accepted
- **Date**: 2026-06-15
- **Deciders**: abyss, GCW Agent Architect (requesting team), Vesma Tech Lead

## Context

The GCW A2A routing protocol (v0.6.0+) needs a persistent backend to store
agent-to-agent message turns. The existing in-memory MCP state works for a single
session, but multi-step chains, crash recovery, and cross-session search all require
persistence.

GCW's `mnemos-requirements.md` defines a minimal contract: 5 HTTP endpoints, SQLite +
FTS5, atomic write, `mode=summary` default, idempotency via `message_id`. Three
"bonus" endpoints (search, summarize, stream) are explicitly deferred to v0.7+.

## Decision

Vesma implements the 5 required endpoints as a new **isolated sub-module**
`src/vesma/sessions/`, mounted under `/v1/sessions/*` in FastAPI. The existing
memory API (`/memories`, `/recall/*`, `/search`) is **not** modified.

Key design choices:

1. **Idempotency via `message_id` UNIQUE constraint** — repeat POST returns the
   existing turn, no duplicate. GCW retries are safe.
2. **`mode=summary` is the default** for `GET /turns/{turn_id}`. Summary is
   extractive (first 200 chars + key_decisions regex) — no LLM in v1.
3. **Atomic write** — single SQLite transaction with `BEGIN IMMEDIATE`, explicit
   `commit` after `INSERT`, `rollback` on any error. WAL mode is set at session
   table creation.
4. **Failure mode: NOT single point of failure** — GCW has a file-based fallback
   (`~/.gcw/a2a-messages.jsonl`). Vesma is best-effort, not a hard dependency.
5. **Schema isolation** — `sessions` and `turns` tables are separate from
   `memories`. They share the SQLite connection but not the schema contract.

## Consequences

**Positive**

- GCW v0.6.0 ships with persistent A2A backend. Multi-step chains survive crashes.
- The memory API and the A2A API have independent evolution paths.
- Summary-mode-by-default saves bandwidth — full content is opt-in.

**Negative**

- Two HTTP surfaces (memory + A2A) double the operator's mental model.
- A future Postgres migration must cover both schemas; can't migrate one without
  the other.
- Idempotency check adds one extra `SELECT` per turn write. Negligible at typical
  workload (<1ms).

**Neutral**

- The bonus endpoints (`/v1/search`, `/v1/sessions/{id}/summarize`, WebSocket) are
  not in v1; they are tracked in PLAN.md M-series but not scheduled.

## Alternatives considered

- **Extend the memory API with session_id on each memory.** Rejected: it conflates
  two distinct concepts (human/agent knowledge vs. conversation transcript) and
  pollutes `mnemos_search` results with conversation noise.
- **Implement a separate service on a different port.** Rejected: deploys two
  processes when one is enough; operational overhead not justified at v1 scale.
- **Defer the A2A API entirely.** Rejected: GCW v0.6.0 ships without a
  persistent backend — that is a regression for the team.

## References

- `tasks/senior-system-engineer/M16-a2a-sessions-api.md` — implementation spec
- `/var/home/abyss/LABs/Projects/Reserching/GithubCopilotWorkflow/docs/a2a/mnemos-requirements.md` — GCW contract
- `src/vesma/sessions/` — implementation
- `docs/a2a-sessions.md` — user-facing API reference
- `tests/test_a2a_sessions.py` — 26 tests
