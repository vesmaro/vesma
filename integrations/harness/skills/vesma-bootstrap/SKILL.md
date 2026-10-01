---
name: vesma-bootstrap
description: One-shot bootstrap to set up the file-mode memory store on first use of any vesma-* skill.

user-invocable: false
---

# Vesma bootstrap (STUB mode)

Run once per workspace before any other `vesma-*` skill uses the file store.

## Steps

1. Check whether MCP tools `vesma_recall_context`/`vesma_save_context`/`vesma_add`/`vesma_search` /
   `vesma_agent_recall` are available in the session.

  - If **available** → set mode = `mcp`. Stop. No file work needed.
  - If **not available** → set mode = `file` and continue.

> **Additional MCP tools:** when MCP mode is active, the
> following tools are also available: `vesma_compress` / `vesma_retrieve`
> (CCR reversible compression), `vesma_ingest_url` (URL ingest with
> credential stripping), `vesma_watch_start` / `vesma_watch_stop` /
> `vesma_watch_status` (file watcher), `vesma_auto_collect_status`
> (compaction signal vector), `vesma_reprocess` (manual pipeline trigger),
> `vesma_filter` (re-filter existing memory). See `vesma-memory-ops`
> instruction §4 for details.

1. Ensure `.copilot/memory/` directory exists at workspace root.

1. Ensure `.copilot/memory/MEMORY.md` exists. If missing, create with header:

```markdown
# Workspace memory (file-mode stub)

> Managed by vesma-* skills. Will migrate to Vesma MCP when installed
> (see https://github.com/vesmaro/vesma).

## Sessions

<!-- session entries appended here -->

## Bug patterns

<!-- mnemos:bug-pattern entries -->

## Learnings

<!-- mnemos:learning entries -->

## Decisions

<!-- mnemos:decision entries -->

```

1. Print a one-line notice on first use only:

   `mnemos: file-mode (MCP not installed); see plugins/vesma-integration/README.md`.

## Idempotency

All steps must be safe to re-run. Existing files/sections must be left alone.

## Migration note

When Vesma MCP is later installed, the contents of `.copilot/memory/MEMORY.md`should be importable via`vesma_add`
calls — preserve the tag conventions from skill `vesma-tag-contract` so migration is mechanical.

<!-- vesma-harness-pack: 1.0.0 -->
