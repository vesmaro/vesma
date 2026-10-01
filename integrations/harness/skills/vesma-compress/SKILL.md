---
name: vesma-compress
description: Zero-loss compression of huge tool outputs — keep context small, fetch the original back on demand
---

# Vesma Compress (CCR)

Compress oversized tool output (logs, JSON, build logs) with ZERO data loss
before pasting it into context. The original is cached server-side; a marker
hash lets you retrieve it later.

## WHEN

- **A tool returned >500 lines / huge JSON** that you only partly need now.
- **Long command output** where the gist matters but details may matter
  later.
- **Before writing a memory entry** that quotes a big blob — store the
  compressed marker, not the blob.

## STEPS

1. **Compress and keep the marker**:

   ```text
   vesma_compress(text=<huge output>, profile="log")
   # → [compressed: <sha256> | 87% saved | retrieve via vesma_retrieve]
   ```

   Profiles: `log`, `terminal`, `code`, `docs`, `web`, `default`.

2. **Carry the marker in context** instead of the original text.

3. **Retrieve when details are needed** — full original or ranked snippets:

   ```text
   vesma_retrieve(hash=<sha256>)                      # full original
   vesma_retrieve(hash=<sha256>, query="timeout")     # FTS-ranked snippets
   ```

## DISCIPLINE

- Compression is **zero-loss** — never summarize by hand "because it's
  compressed anyway"; retrieve instead.
- Pick the matching profile: `code` preserves identifiers, `log` keeps
  timestamps aligned.
- Don't compress short outputs (<500 chars) — the marker costs more than
  the text.

## See also

- Skill `vesma-cache-align` — stabilizing prompts for provider KV caches
- Skill `vesma-write` — persisting the marker into a memory entry

<!-- vesma-harness-pack: 1.0.0 -->
