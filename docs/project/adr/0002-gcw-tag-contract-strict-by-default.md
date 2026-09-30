# 0002. Vesma tag contract is strict by default at the MCP layer

*Historical artifact — English only.*

- **Status**: Accepted
- **Date**: 2026-05-15
- **Deciders**: abyss, GCW Agent Architect

## Context

GCW agents (Tech Lead, SRE, DBA, etc.) write to Vesma via MCP. Without a contract, every
agent invents its own tag schema, search becomes unreliable, and per-agent recall
(`mnemos_agent_recall`) cannot filter by `agent:` reliably.

A pre-existing personal `ai-brain` had a lax tag system — any tag was allowed. The user
wants this cleaned up in Vesma so that:

- `agent:gcw-tech-lead` is always present
- `project:gcw` (or another project slug) is always present
- at least one `vesma:*` namespace tag is always present
  (`mnemos:session`, `mnemos:bug-pattern`, `mnemos:learning`, `mnemos:decision`, `mnemos:rule`,
  `mnemos:open-question`, `mnemos:checkpoint`, `mnemos:legacy`)

## Decision

We will enforce the Vesma tag contract at the **MCP `mnemos_add` layer**, not the storage
layer, because:

1. MCP is the only public surface that produces records — the CLI is for human use and
   the HTTP API is for the A2A sessions (a separate schema).
2. Storage-level enforcement would break the existing `ai-brain` migration path
   (M13), which deliberately imports legacy records in lax mode.
3. Whitelisted prefix namespace (`severity:`, `stack:`, `applyTo:`, `source:`) lets
   GCW agents add domain-specific tags without breaking search.

A `strict_tag_contract: bool` setting (default `true` for new installs, `false` during
M13 migration) controls enforcement. When `false`, Vesma auto-tags incoming records
with `mnemos:legacy` and `agent:unknown` and logs a warning.

## Consequences

**Positive**

- Per-agent recall is reliable: `mnemos_agent_recall(agent="gcw-tech-lead")` is exact.
- Cross-agent search is normalised: GCW namespace tags are universal across the family.
- The contract is **machine-checkable** in CI (`tests/test_tag_contract.py` covers
  happy-path, missing tags, invalid prefix, strict/lax modes — 31 tests).

**Negative**

- Developers writing ad-hoc CLI scripts must add the required tags or set
  `strict_tag_contract=false`. Friction is intentional.
- The `mnemos:legacy` tag pollutes the namespace slightly. Acceptable; it lets operators
  filter "old migrated records" out of recall results.

**Neutral**

- The contract is a runtime check, not a compile-time type. Pydantic could enforce it
  statically but at the cost of migration complexity.

## Alternatives considered

- **Enforce at the SQLite CHECK constraint level.** Rejected: M13 migration would fail
  on every legacy row; would need a separate "legacy" table.
- **Enforce only in the CLI, not MCP.** Rejected: most writes come from agents via MCP,
  not from humans via CLI.
- **Make all tags optional, run cleanup as a background job.** Rejected: data quality
  drifts immediately; recall is unreliable until cleanup runs.

## References

- `PLAN.md` §"Phase M2 — Vesma Tag Contract"
- `ARCHITECTURE.md` §2 (TagContract section)
- `docs/tag-contract.md` — operator-facing schema
- `tests/test_tag_contract.py` — 31 tests
