---
name: vesma-watch
description: Watch directories and auto-index changes into memory — keep the vault in sync with living code and docs
---

# Vesma Watch

Start a background watcher over project directories so file changes are
auto-indexed into memory. The vault stays current without manual
`vesma_add` for every doc change.

## WHEN

- **Long-running multi-session work** — docs/rules written between sessions
  should be searchable without re-ingestion.
- **A shared knowledge dir** (ADRs, runbooks) that several agents read.
- **After restoring or migrating a vault** — one scan pass re-indexes
  everything.

## STEPS

1. **Start watching** (initial scan included by default):

   ```text
   vesma_watch_start(paths=["/project", "/project/docs"], scan=true)
   ```

2. **Check health periodically** — especially after long idle periods:

   ```text
   vesma_watch_status()
   ```

3. **Stop cleanly** when the workstream ends:

   ```text
   vesma_watch_stop()
   ```

## DISCIPLINE

- Watch **knowledge directories**, not whole repos — `src/` churn would
  flood the pipeline with noise.
- `.github/instructions/*.instructions.md` is picked up automatically when
  present — no need to add it twice.
- The watcher is per-session background state: if the session died, restart
  it rather than assuming it survived.

## See also

- Skill `vesma-ingest` — one-shot URL ingestion vs. directory watching
- `vesma doctor` — reports watcher health among other checks

<!-- vesma-harness-pack: 1.0.0 -->
