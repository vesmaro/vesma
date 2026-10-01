---
name: vesma-recall
description: Effective memory search — start narrow, broaden if no hits; avoids re-learning what was already learned
---

# Vesma Recall

Query the memory store for relevant prior entries. Use before architectural
decisions, before web searches, and when resuming work on a topic.

## WHEN

- **Before an architectural decision** — choosing a pattern, library, or
  approach. Check if a prior decision exists.
- **Before a web search** — the answer may already be in memory.
- **When resuming a topic** — recall what was learned last time.
- **When debugging** — check if this bug-pattern was seen before.

## STEPS

1. **Start narrow** — tag-filtered, project-scoped:

   ```text
   vesma_search(
     query=<natural language query>,
     project=<current-project>,
     tags=["mnemos:decision"],     # or mnemos:bug-pattern, mnemos:learning
     limit=10
   )
   ```

2. **Broaden if no hits** — drop the tag filter, keep the project scope:

   ```text
   vesma_search(
     query=<query>,
     project=<current-project>,
     limit=10
   )
   ```

3. **Broaden further if still no hits** — drop project scope:

   ```text
   vesma_search(
     query=<query>,
     limit=10
   )
   ```

4. **For agent-scoped recall** — when you need your own prior context:

   ```text
   vesma_agent_recall(
     agent=<your-slug>,
     project=<current-project>,   # optional
     query=<optional focus>,
     limit=20
   )
   ```

5. **Return a compact list** — do not paste full bodies unless the caller
   asks:

   ```text
   - <title> (<tags>) [project=<...>]
   ```

6. **If 0 results, say so explicitly.** Do not fabricate prior context.

## DISCIPLINE

- **Narrow → broaden.** Starting broad returns too much noise; starting
  narrow returns signal or confirms absence.
- **Default to recency, not relevance, when ranking ties.** The most recent
  entry is usually the most applicable.
- **Do not paste full bodies.** Keep the recall list scannable. Recall the
  full entry only if the caller needs it.
- **Never fabricate.** If search returns nothing, say "no prior context
  found for <query>". Do not infer what "probably" was in memory.
- **Search before web.** A web search that re-discovers what memory already
  has is wasted tokens and time.

## See also

- Skill `vesma-write` — capture what you learned
- Instruction `vesma-memory-ops.instructions.md`

<!-- vesma-harness-pack: 1.0.0 -->
