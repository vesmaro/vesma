# 0011. `mypy --strict` is the production-readiness gate, not a check-the-box lint

*Historical artifact — English only.*

- **Status**: Accepted
- **Date**: 2026-06-15
- **Deciders**: abyss, GCW Senior System Engineer, GCW Tech Lead

## Context

The Makefile `make verify` runs `mypy --strict src/vesma/`, but `pyproject.toml`
had `warn_return_any = false`. The result: the `make verify` step passed while
`src/vesma/` had **45 unfixed type errors**. The two configurations contradicted
each other.

The deeper problem: `mypy --strict` is treated as a check-the-box lint in v0.1.0
(it is run, the report is read, but the errors are tolerated). For v1, we want
`mypy --strict` to be a **gate** — failing `make verify` is a production blocker.

## Decision

1. `pyproject.toml` `warn_return_any = true` (default, matches `--strict`).
2. `make verify` fails on **any** mypy error, not just `error:` severity.
3. 45 pre-existing errors in v0.1.0 are fixed in M15.1 (`fix(m15): resolve 45
   mypy --strict errors`) before M15.5 commits to main.
4. CI (M17) runs `mypy --strict` on Python 3.11, 3.12, 3.13. Any new errors
   block the PR.

The 45 errors break into three severity classes:

- **High** (potential runtime crash): `Memory | None → Memory` narrowing
  (`manager.py:223,230`); `object` types in `policy/scheduler.py:90-99` (NoneType
  crash on `value["status"]`); `attr-defined` for `ClusterResult/PublishResult/
  QualityResult/SynthesisResult` (`manager.py:33-36`) which fall through to
  runtime import errors.
- **Medium** (type quality): `no-any-return` for SQLite row conversions, missing
  type arguments for `list`/`dict` generics.
- **Low** (cosmetic): `untyped-decorator` for MCP SDK runtime decorators
  (`mcp_server.py:120,391`); requires `# type: ignore[untyped-decorator]`
  with justification comment.

## Consequences

**Positive**

- `make verify` becomes a meaningful gate. The README claim "production-ready"
  matches reality.
- Type narrowing catches real bugs: the `Memory | None` issue in `manager.py:223`
  would have crashed a caller when the memory was deleted between `get()` and
  the next use. The fix is a 4-line None-check.
- The codebase is forward-compatible: future Python 3.13+ typing features
  (`Self`, `TypeVar` defaults) work without rewrites.

**Negative**

- New contributors will hit `mypy --strict` on their first PR. We document the
  expected workflow in `docs/runbooks/contributing.md` (planned).
- The `untyped-decorator` suppressions for MCP SDK are not strictly type-safe.
  They are pragmatic — the SDK uses runtime decorators that mypy cannot model.
  This is the **only** `# type: ignore` block allowed in v1; everything else
  must be a real fix.

**Neutral**

- The 45-error fix is not a "type-purify" exercise. The fixes preserve runtime
  behaviour exactly; only the types change. Tests must continue to pass.

## Alternatives considered

- **Drop `--strict` from the Makefile.** Rejected: the whole point of
  `make verify` is that it is strict.
- **Use `--warn-unused-ignores` only (drop the rest).** Rejected: half-measure.
  `--strict` is the right default.
- **Add a separate `make typecheck` target, keep `make verify` lax.** Rejected:
  invites two-tier behaviour. The whole codebase should be strict.

## References

- `tasks/senior-system-engineer/M15.1-mypy-strict.md` — full task
- `tasks/AUDIT.md` §3.2 — full error breakdown
- `pyproject.toml` — `[tool.mypy]` section
- `Makefile` — `make verify` target
