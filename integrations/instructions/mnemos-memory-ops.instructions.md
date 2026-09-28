---
applyTo: '**'
description: Mnemos memory operations — search before deciding, add when learning, agent-scoped recall
---

# Mnemos Memory Operations

## Applies to / Применяется ко всем агентам

This instruction governs the day-to-day memory operations: searching before
acting, writing when learning, and recalling agent-scoped context. It
complements `mnemos-session-lifecycle.instructions.md` (which governs
start/checkpoint/end).

---

## WHEN

| Trigger | Action | Tool |
|---------|--------|------|
|| **Before an architectural decision** — choosing a pattern, library, approach | Search memory for prior decisions on this topic | `mnemos_search` |
|| **When learning something non-obvious** — a gotcha, a hidden constraint, a surprising behaviour | Capture it as a learning or bug-pattern | `mnemos_add` |
|| **Before a web search** — querying the internet for a solution | Search memory first; the answer may already be stored | `mnemos_search` |
|| **When resuming work as a specific agent** — e.g. `cr-security-reviewer` re-entering | Recall this agent's recent entries | `mnemos_agent_recall` |
|| **When a decision is made** — design, process, or tradeoff chosen | Capture the decision and rationale | `mnemos_add` |
|| **When a large tool output threatens to fill context** — long logs, big JSON dumps, verbose command output | Compress it before pasting into context; retrieve the original later if needed | `mnemos_compress` |
|| **When retrieving the full original behind a CCR marker** — or searching within a cached original for specific lines | Retrieve by hash (full) or by hash + query (FTS5 snippets) | `mnemos_retrieve` |
|| **When web research yields a useful page** — a docs page, a blog post, a Stack Overflow answer | Save the page directly to memory so future agents find it | `mnemos_ingest_url` |

---

## HOW

### Search before deciding

```text
mnemos_search(
  query=<natural language query>,
  project=<current-project>,   # optional, scope to project
  tags=["mnemos:decision"],        # optional, narrow by tag
  limit=10
)
```

- Start narrow (tag-filtered, project-scoped), then broaden if no hits.
- If 0 results, say so explicitly — do not fabricate prior context.
- Default to recency when ranking ties.

### Agent-scoped recall

```text
mnemos_agent_recall(
  agent=<your-agent-slug>,
  project=<current-project>,    # optional
  query=<optional focus>,
  limit=20
)
```

- Use when you need **your own** prior context, not the project's.
- Example: `cr-security-reviewer` resuming a review should recall its own
  past findings before re-reviewing.

### Capture when learning

```text
mnemos_add(
  content=<markdown: what you learned, with context>,
  tags=[
    "project:<slug>",
    "agent:<slug>",
    "mnemos:learning"              # or mnemos:bug-pattern, mnemos:decision, mnemos:rule
  ],
  title=<short title>           # optional, auto-generated if omitted
)
```

- **Tag contract is mandatory.** See `mnemos-tag-contract.instructions.md`.
- Write what you would want to read back in 30 days. Trivia is noise.
- One idea per entry. Split complex learnings into multiple entries.

### Capture a decision

```text
mnemos_add(
  content=<markdown: decision + rationale + alternatives considered>,
  tags=[
    "project:<slug>",
    "agent:<slug>",
    "mnemos:decision"
  ]
)
```

- Include the **why**, not just the **what**. A decision without rationale
  is a rule without justification — future agents cannot tell if it still
  applies.

### Compress large tool outputs

```text
mnemos_compress(
  text=<large content: logs, JSON, command output>,
  profile=<log|terminal|code|docs|web|default>,  # optional, auto-detected
  project=<current-project>                       # optional
)
```

- Use when a tool output is large enough to crowd the context window
  (typically ≥500 chars). The original is cached keyed by SHA-256 hash and a
  short marker is returned in its place.
- Keep the returned marker in context — pass its `hash` to
  `mnemos_retrieve` to fetch the full original (or FTS5 snippets from within
  it) on demand.
- Achieves 70–90% token reduction on typical logs with zero data loss.

### Retrieve a compressed original

```text
mnemos_retrieve(
  hash=<SHA-256 hash from a [compressed: …] marker>,
  query=<optional search query for snippet retrieval>,
  snippet_count=<int, default 5>   # optional, only when query is set
)
```

- Without `query`: returns the full cached original.
- With `query`: returns FTS5-ranked snippets from within the original — useful
  when only a few lines are relevant and the original is large.

### Save web research to memory

```text
mnemos_ingest_url(
  url=<HTTP/HTTPS URL of the page to save>,
  tags=[
    "project:<slug>",
    "agent:<slug>",
    "mnemos:<subtype>"          # e.g. mnemos:learning, mnemos:decision, mnemos:rule
  ]
)
```

- Fetches the page, extracts its main content (via trafilatura), and stores
  it as a memory. Credentials embedded in the URL are stripped before
  storage (OWASP A02).
- Same M2 tag contract as `mnemos_add`. Returns `{id, title, url}`.
- Use after web research so the finding is available to future agents via
  `mnemos_search` without re-fetching.

---

## Discipline

- **Search first, always.** Before a web search, before an architectural
  decision — check memory. Re-learning what was already learned is waste.
- **Write sparingly.** Memory is not a log of every action. Write when you
  learned something **non-obvious** that a future agent would benefit from.
- **Update by key, don't duplicate.** If you are re-capturing something,
  search for it first and update rather than appending a duplicate.
- **Never block on memory failure.** If `mnemos_*` errors, log a notice
  and continue with the task. Memory is an enhancement, not a dependency.

---

## See also

- `mnemos-session-lifecycle.instructions.md` — recall at start, checkpoint on compaction
- `mnemos-tag-contract.instructions.md` — required tag composition
- `canon-records.instructions.md` — canon record standards (envelope + body sections)
- Skill `mnemos-recall` — effective search (narrow → broaden)
- Skill `mnemos-write` — writing good entries
- Skill `mnemos-canon-write` — canon-compliant task / decision / report records
