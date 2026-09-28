---
name: mnemos-checkpoint
description: Save a compaction-resilient checkpoint mid-session — survives context compression and session restart
---

# Mnemos Checkpoint

Write a snapshot every time the session reaches a meaningful milestone or
signals compaction. The checkpoint is what the agent reads back after
compaction to recover state.

## WHEN

- **Just finished a multi-step phase** — a batch of files saved, a test suite
  fixed, a refactor completed.
- **About to invoke an expensive subagent chain** — before delegating to a
  worker that may consume significant context.
- **User confirmed a non-trivial decision** — before moving on.
- **Compaction signal detected** — summary banner, long conversation
  (≳30 turns since last checkpoint), `mnemos_auto_collect_status` recommends
  checkpoint, or sudden loss of earlier-turn references.
- **Before a long step** — when the next action will consume many turns.

## STEPS

1. Compose the checkpoint content:

   | Field | Content |
   |-------|---------|
   | `goals` | The active goal in one sentence. |
   | `completed` | Recent completed items (≤7 bullets). |
   | `in_progress` | The immediate next action (one bullet). |
   | `decisions` | Decisions worth surviving compaction (bullets). |
   | `context` | File paths, architecture notes, gotchas (free text). |

2. Save the checkpoint:

   ```text
   mnemos_save_context(
     project=<current-project>,
     goals=<one sentence>,
     completed=<bullets>,
     in_progress=<one bullet>,
     decisions=<bullets>,
     context=<file paths, notes>
   )
   ```

3. Confirm with a one-line notice:

   ```text
   mnemos: checkpoint saved (project=<name>)
   ```

## DISCIPLINE

- **Idempotent within a session.** Re-saving shortly after a previous
  checkpoint should update the latest checkpoint, not create duplicates.
- **Keep the body short.** The checkpoint is for waking up after compaction,
  not for archival. ≤7 bullets per field.
- **Never block on checkpoint failure.** If `mnemos_save_context` errors,
  log a one-line notice and continue. Memory is an enhancement, not a
  dependency.
- **Do not checkpoint on a timer.** Write on signals (phase done, compaction
  detected), not every N turns. Timer-based checkpoints create noise.
- **Include the next action.** A checkpoint without `in_progress` leaves the
  post-compaction agent without a starting point.

## See also

- Skill `mnemos-session-init` — recall at session start
- Skill `mnemos-canon-write` — canon-compliant task / decision / report records
- Instruction `canon-records.instructions.md` — canon record standards
- Instruction `mnemos-session-lifecycle.instructions.md`
