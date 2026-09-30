# 0010. GCW A2A is a "best-effort, not a hard dependency" backend

*Historical artifact — English only.*

- **Status**: Accepted
- **Date**: 2026-06-15
- **Deciders**: abyss, GCW Agent Architect, Vesma Tech Lead

## Context

`mnemos-requirements.md` (from the GCW team) explicitly states:

> Vesma НЕ является single point of failure для GCW. Если он упал — система работает,
> просто теряет cross-session persistence.

The contract is: GCW must continue working when Vesma is down, slow, or returns
errors. The implication: every Vesma call site in GCW must have a defined
fallback. Vesma's job is to be **strictly better** than the fallback, not
**strictly required**.

## Decision

Vesma commits to the following contract with GCW:

1. **Vesma can be down**: GCW writes to file-based fallback
   (`~/.gcw/a2a-messages.jsonl`) and continues.
2. **Vesma can be slow**: GCW times out at 2 seconds, logs a warning, and writes
   the turn later (async).
3. **Vesma returns 5xx**: GCW retries 3× with exponential backoff, then falls
   back to file.
4. **Vesma returns 4xx (validation)**: GCW logs the error, **does not** write
   the turn, and continues.

Vesma's role is to be a **strict improvement** over the file-based fallback, not
a hard dependency. A GCW operator who runs the system without Vesma still has a
working A2A pipeline.

## Consequences

**Positive**

- GCW v0.6.0 ships without a hard dep on Vesma. Onboarding to GCW does not
  require running Vesma.
- Vesma outages do not cascade into GCW outages. The two systems are loosely
  coupled.
- File-based fallback is a clean **lower bound** — Vesma must beat it on at
  least one axis (search, dedup, multi-session aggregation) to justify its
  existence.

**Negative**

- A user who runs Vesma but never sets up the file-based fallback can lose
  data when Vesma is down. Documented in the GCW runbooks.
- The 4xx "do not write" path means a malformed A2A message that Vesma
  rejects is **silently dropped** in GCW. Mitigated by GCW logging the rejection
  and providing operator visibility.

**Neutral**

- The two-tier system (Vesma + file fallback) is a known anti-pattern in
  microservice literature ("dual writes"). It is acceptable here because the
  fallback is a **last resort**, not the primary store.

## Alternatives considered

- **Strong-consistency sync between Vesma and GCW state.** Rejected: the
  requirement explicitly forbids Vesma as a hard dependency. Strong consistency
  would force Vesma into the critical path.
- **Make Vesma a write-through cache (with no fallback).** Rejected: violates
  the GCW team's stated constraint.
- **Make the file-based fallback write-through Vesma, not the other way around.**
  Rejected: Vesma is a server, not a sidecar. It cannot be guaranteed to be
  running.

## References

- `/var/home/abyss/LABs/Projects/Reserching/GithubCopilotWorkflow/docs/a2a/mnemos-requirements.md`
  §"Failure modes (как GCW обрабатывает)"
- `docs/a2a-sessions.md` § Failure modes
- ADR-0007 (A2A Sessions API design)
