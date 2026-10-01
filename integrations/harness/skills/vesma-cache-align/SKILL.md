---
name: vesma-cache-align
description: Stabilize prompt prefixes for provider KV caches — move dynamic tokens to the end before caching matters
---

# Vesma Cache Align

Relocate dynamic content (timestamps, UUIDs, session ids, tokens) to the
END of a text so its prefix stays byte-identical across requests. That
makes provider KV caches (Anthropic cache_control, OpenAI prefix caching)
actually hit.

## WHEN

- **Repeated calls with a large shared prefix** — system prompts, tool
  definitions, style guides — with only a volatile tail.
- **Cache hit-rate is poor** despite stable-looking prompts — hidden
  timestamps/ids at the top are usually the cause.
- **Building a prompt template** that will fire many times.

## STEPS

1. **Align the text**:

   ```text
   vesma_align_prefix(text=<prompt>, profile="code")
   ```

   Profiles: `code` (keeps bare identifiers in place — avoids mangling
   long symbol names), `docs`, `default`.

2. **Use the aligned text** as the stable prefix; append the truly
   per-request values AFTER it.

3. **Verify by diffing** two aligned outputs for the same logical content —
   the prefix must be byte-identical.

## DISCIPLINE

- Align ONCE at template build time, not per request.
- Don't align user-facing prose where order carries meaning — this is for
  machine-consumed prompts.
- `code` profile skips bare tokens deliberately; the default profile is
  more aggressive.

## See also

- Skill `vesma-compress` — shrinking big outputs that changed anyway

<!-- vesma-harness-pack: 1.0.0 -->
