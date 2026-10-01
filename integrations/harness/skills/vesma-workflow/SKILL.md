---
name: vesma-workflow
description: Memory workflow lifecycle — track open questions and tasks from open to done without losing them
---

# Vesma Workflow

Drive the lifecycle of a memory entry: `open → in-progress → blocked →
resolved → done` (or `withdrawn`). Use it so open questions and tasks
survive context resets and multiple agents can pick them up.

## WHEN

- **An open question needs follow-up** — anyone should be able to see its
  state and who holds it.
- **Starting work on a tracked item** — move it to `in-progress` first.
- **Blocked on someone/something** — record WHY in the transition.
- **Closing work** — `resolved` when answered, `done` when delivered,
  `withdrawn` when abandoned as no longer relevant.

## STEPS

1. **Check current state** (id from a search result or `mnemos:open-question`
   entry):

   ```text
   vesma_workflow(action="get", memory_id=<id>)
   ```

2. **Transition with an explicit actor** (your agent slug) and a reason:

   ```text
   vesma_workflow(action="set", memory_id=<id>, to="in-progress",
                   actor=<your-slug>, reason="investigating repro on #123")
   ```

3. **Review the audit trail** before overriding someone else's state:

   ```text
   vesma_workflow(action="history", memory_id=<id>)
   ```

## RULES

- `blocked → done` is **forbidden** — resolve a blocker first; the state
  machine enforces this.
- Stale locks auto-release after 24h; if you force-unlock, give a reason.
- Same-status transitions are no-ops — don't churn the audit log.

## See also

- Skill `vesma-write` — creating the `mnemos:open-question` entry first
- Skill `vesma-agent-recall` — finding who owns an open item

<!-- vesma-harness-pack: 1.0.0 -->
