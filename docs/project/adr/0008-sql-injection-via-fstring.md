# 0008. SQL injection via f-string is fixed via whitelisted dispatch (M15)

*Historical artifact — English only.*

- **Status**: Accepted
- **Date**: 2026-06-15
- **Deciders**: abyss, GCW Senior Security Engineer, GCW Senior DBA

## Context

Bandit (run with `skips = ["B104", "B608", "B615"]` in the legacy `pyproject.toml`)
identified three B608 findings in v0.1.0:

- `src/vesma/storage/sqlite_store.py:419` — `update_fields` builds `setters` via
  `f"{k}=?"` for `k` in `updates.keys()`.
- `src/vesma/storage/sqlite_store.py:523` — `fts_search` builds the SQL query via
  a multi-line f-string with five interpolated conditions.
- `src/vesma/storage/vector_store.py:150` — same pattern, smaller scale.

The current code is **safe in practice** because `updates` is filtered through a
static `allowed` set before the f-string is built. Bandit, however, does not
understand allowlist filters — it flags the f-string itself.

The deeper problem: this is a maintenance hazard. Any future contributor who adds
a new f-string interpolation without re-reading the allowlist breaks safety
silently. The pre-M15 `skips = ["B608"]` in `pyproject.toml` was a **silent
suppression** of a real category — explicitly forbidden by
`lint-and-validate.instructions.md`.

## Decision

We fix this in two complementary ways:

1. **`update_fields` — whitelisted dispatch.** A static module-level dict
   `_FIELD_UPDATERS: dict[str, str]` maps known field names to the
   `column=?` SQL fragment. `setters` is built by joining fragments from this
   dict — no f-string, no user input flows into SQL identifiers. The single
   remaining `f"UPDATE ... SET {setters} ..."` is annotated `# nosec B608`
   with an inline comment that names the whitelisted source.
2. **`fts_search` — static SQL template + parameter substitution.** The query
   body becomes a static string. User input flows only through `?`
   placeholders. FTS5 user queries are passed through `_escape_fts_query()`
   which strips FTS5 operators (`* " ( ) :`) and wraps the result in
   double quotes to disable FTS5 syntax.
3. **`vector_store.py:150` — same pattern as `fts_search`.**
4. **`pyproject.toml` — remove the `skips = ["B104", "B608", "B615"]` block.**
   The codebase is now clean under default Bandit config.

The B104 (`0.0.0.0` binding in uvicorn for container port-mapping) is a confirmed
false positive — annotated with `# nosec B104` and a one-line justification. This
is the only inline suppression added; it is documented in `docs/security.md`.

## Consequences

**Positive**

- `bandit -r src/` reports zero findings under default config (no skips).
- The static-dict pattern makes the safety property **mechanically obvious** to
  future contributors: SQL identifier construction cannot be added without
  editing `_FIELD_UPDATERS`, which is reviewed on every PR.
- FTS5 escaping protects against a real attack vector: a malicious search query
  with `"` or `*` characters could previously have caused a malformed MATCH
  expression.

**Negative**

- A new field that should be updatable via `update_fields` now requires adding
  the field to `_FIELD_UPDATERS`. This is intentional — it forces a code-review
  touchpoint for any schema change.
- The FTS5 escape (`_escape_fts_query`) is conservative. A search for `*` or `"`
  returns empty results — these characters are stripped before the MATCH. This
  is acceptable for v1; we can switch to FTS5's `query()` syntax for richer
  queries in v2.

**Neutral**

- Performance is unchanged. The whitelisted dispatch compiles to the same SQL
  the f-string did. FTS5 escaping is a single regex pass per query.

## Alternatives considered

- **Keep `skips = ["B608"]` and document the reason.** Rejected: this is
  suppression without a confirmed false positive. The current code happens to
  be safe, but the pattern is not.
- **Move to SQLAlchemy Core with bound parameters.** Rejected: adds a heavy
  dependency for a single SQLite store; the whitelisted-dispatch pattern is
  smaller and clearer.
- **Use `sqlite3` named parameters only.** Rejected: named parameters
  (`":name"`) bind values, not identifiers. They do not solve the
  f-string-in-identifier problem.

## References

- `tasks/senior-security-engineer/M15.2-bandit-cleanup.md` — full task
- `tasks/senior-dba/M15.3-sql-injection-refactor.md` — coordinated refactor
- `docs/security.md` — threat model + B104 justification
- `src/vesma/storage/sqlite_store.py` — `_FIELD_UPDATERS`, `_escape_fts_query`
- `src/vesma/storage/vector_store.py` — same pattern
