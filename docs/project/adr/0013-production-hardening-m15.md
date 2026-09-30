# 0013. M15 production hardening is the gate to declaring v1 "done"

*Historical artifact — English only.*

- **Status**: Verified (2026-06-16, M19 closure — see CHANGELOG 0.2.0)
- **Date**: 2026-06-15
- **Deciders**: abyss, GCW Tech Lead

## Context

Vesma v0.1.0 (CHANGELOG) claims "M1–M15 complete" and README claims
"production-ready". The reality (audit 2026-06-15):

- 209 tests pass ✅
- `ruff` clean ✅
- `mypy --strict` ❌ 45 errors
- `bandit` ❌ 3 HIGH + 7 MEDIUM + 1 LOW (with skips in `pyproject.toml` masking
  the worst categories)
- 7 modified + 7 untracked files in the working tree (no clean commit history)
- README/CHANGELOG docs claim M15 is done; it is not

The gap is the difference between "the feature works on the happy path" and
"the project survives a security audit, a coverage report, and a CI run".

## Decision

M15 production hardening is the **gating milestone** for v1. v1 is "done" when
**all** of the following are true:

1. `mypy --strict` reports 0 errors (M15.1).
2. `bandit -r src/` reports 0 issues, with `pyproject.toml` `[tool.bandit].skips`
   removed (M15.2).
3. `pytest tests/ -q` reports ≥ 209 passed (no regression).
4. `pytest --cov=src/vesma --cov-fail-under=80` passes (M18, coverage gate).
5. README and CHANGELOG reflect the actual M1-M14 + M15-in-progress state (M15.4).
6. Working tree is clean — pending changes are committed via `feat/m15-...` PR
   (M15.5).
7. CI (GitHub Actions, M17) is green on Python 3.11, 3.12, 3.13.

The README claim "production-ready" is **removed** from `main` until all seven
gates are green. v1 ships with explicit "M15 complete" + "M1-M14 verified"
status.

## Consequences

**Positive**

- v1 ships as a real, auditable product. The README claim matches the code.
- Coverage is enforced, not aspirational.
- The CI gate prevents future regressions in mypy / bandit / coverage.
- The M15.5 PR is a **single, large, reviewable change** — easy to revert if
  a problem surfaces.

**Negative**

- The M15 milestone is large. M15.1, M15.2, M15.3, M15.4, M15.5 are six
  separate commits, each requiring review. The M19 final review is a hard
  gate before merge.
- Documentation hygiene is a tax. M15.4 exists only because v0.1.0 shipped
  with docs that did not match the code.
- The M15.1 mypy fix changes types but not runtime behaviour. Any missed
  regression is a real bug. Mitigation: full `pytest` run after every batch.

**Neutral**

- M15 supersedes the v0.1.0 "production-ready" claim. Anyone who read the v0.1.0
  README should re-validate against the post-M15 main.

## Alternatives considered

- **Skip M15, declare v1 done with the v0.1.0 status quo.** Rejected: the
  audit's 45 mypy errors and 3 HIGH bandit findings are real blockers, not
  paperwork.
- **M15.5 (commit) first, then M15.1-M15.4 as separate PRs.** Rejected: each
  individual PR would not pass CI (mypy/bandit red). The work must land
  together.
- **Cut v0.2.0 instead of v1.0.0, push M15 work to v0.3.0.** Rejected: the
  feature set is at v1 scope; the only missing piece is hardening.

## References

- `tasks/AUDIT.md` — full state of v0.1.0
- `tasks/tech-lead/TL-001-coordination.md` — sequencing
- `tasks/senior-system-engineer/M15.{1,5}.md` — implementation
- `tasks/senior-security-engineer/M15.2-bandit-cleanup.md` — security
- `tasks/senior-dba/M15.3-sql-injection-refactor.md` — performance + type polish
- `tasks/tech-writer/M15.4-docs-reconcile.md` — docs
- `tasks/sre-devops/M17-ci-pipeline.md` — CI
- `tasks/senior-qa-engineer/M18-e2e-smoke.md` — coverage
- `tasks/code-reviewer/M19-final-review.md` — final gate
