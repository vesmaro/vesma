<!-- mnemos-integration: v2.0.0 -->
# Context Filter Guide

**🌐 Language / Язык:** English · [Русский](../../ru/user/context-filter.md)

The Context Filter is a five-stage pipeline that strips noise from raw
content **before** it reaches a model. It runs automatically on every
`mnemos_add` (when `auto_filter: true`) and can be re-run explicitly on
existing memories via the `mnemos_filter` MCP tool or the `vesma filter`
CLI command.

---

## What it does

Raw agent input — terminal output, build logs, scraped web pages — is
often 5–10× larger than the signal a model needs. The filter sits between
ingest and storage, producing a `clean_content` field that search and
recall return in place of the raw blob.

| Stage | Name | What it removes | Applies to |
|-------|------|-----------------|------------|
| 1 | **dedup** | Exact and near-duplicate lines (normalised comparison) | all profiles |
| 2 | **noise** | ANSI codes, progress bars, timestamps, separator lines | log, terminal, default |
| 3 | **extract** | Signal-rich lines (errors, warnings, exit codes) + context window; sampling for long logs | log, terminal |
| 4 | **compress** | Repetitive blocks (stack frames, repeated patterns) collapsed to `... (N similar lines) ...` | all profiles |
| 5 | **tokens** | Token estimation + optional budget truncation | all profiles |

The original content is never lost: `raw_content` is preserved alongside
`clean_content`, and `filter_stats` records per-stage metrics.

---

## Profiles

The filter picks a profile automatically from content heuristics. You can
override it explicitly via `--profile` (CLI) or the `profile` argument
(MCP tool).

| Profile | When it's selected | What it optimises for |
|---------|--------------------|-----------------------|
| `log` | ISO timestamps or `[DEBUG\|INFO\|WARN\|ERROR]` markers in the first 20 lines | Log lines — strip timestamps, extract errors + warnings |
| `terminal` | ANSI escape codes or spinner glyphs (`▶◀◐◑`) present | Interactive terminal output — strip ANSI, progress bars |
| `code` | `def` / `class` / `import` / `function` / `const` patterns (≥3 hits) | Source code — dedup and compress, no timestamp stripping |
| `web` | `<html`, `<!doctype`, or `<div` tags present | HTML content — dedup and compress |
| `docs` | Markdown with many `#` headings or `---` rules | Documentation — dedup and compress |
| `default` | None of the above matched | General — dedup, noise, compress, no profile-specific extraction |

### Profile detection order

1. Explicit `hint` / `--profile` argument (if valid) → used as-is.
2. `log` — timestamps or log-level markers.
3. `terminal` — ANSI codes or spinner glyphs.
4. `code` — code keyword patterns.
5. `web` — HTML tags.
6. `docs` — markdown structure.
7. `default` — fallback.

---

## Auto-filter on ingest

When `auto_filter: true` (the default for new installs), every
`mnemos_add` call runs the filter pipeline before storing the memory.

```yaml
# config.yaml
vesma:
  auto_filter: true
```

What happens on ingest:

1. The memory is saved with `raw_content` = the original input.
2. The filter runs on `raw_content`, producing `clean_content`.
3. `filter_profile`, `filter_stats`, and `filter_version` are stored.
4. If the filter fails, the memory is **still saved** with `raw_content`
   only — filter failures are non-fatal.

`mnemos_search` and `mnemos_recall_context` return `clean_content` when
available, falling back to `raw_content` (or `content`) if the memory was
stored before filtering was enabled.

---

## Manual filter

### CLI — single memory

```bash
vesma filter <memory-id>
```

Re-runs the filter on an existing memory. Auto-detects the profile unless
`--profile` is given. Prints the detected profile, per-stage stats, and
the resulting `clean_content`.

```bash
vesma filter abc123 --profile terminal
vesma filter abc123 --budget 2000
```

| Flag | Description |
|------|-------------|
| `--profile`, `-p` | `log \| terminal \| code \| docs \| web \| default` (auto-detected if omitted) |
| `--budget`, `-b` | Token budget for truncation stage |
| `--all` | Re-filter every memory (reports aggregate stats) |
| `--config` | Path to config file (standard option) |

### CLI — all memories

```bash
vesma filter --all
```

Iterates every memory in batches and re-applies the filter. Useful after
enabling `auto_filter` on a vault that already has unfiltered entries, or
after a pipeline upgrade. Reports `filtered`, `total`, `failed`, and
`skipped` counts. Individual failures are non-fatal.

### MCP tool — `mnemos_filter`

Agents can call `mnemos_filter` explicitly to re-filter a memory, for
example when the auto-detected profile was wrong or when a new profile
should be applied.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `memory_id` | string | **yes** | — | ID of the memory to filter |
| `profile` | string | no | auto-detected | `log \| terminal \| code \| docs \| web \| default` |
| `budget` | integer | no | none | Token budget for truncation |

Returns:

```json
{
  "memory_id": "abc123",
  "profile": "terminal",
  "clean_content": "...filtered text...",
  "stats": {
    "profile": "terminal",
    "dedup": { "lines_in": 120, "lines_out": 98, "exact_dups": 18, "near_dups": 4 },
    "noise": { "original_chars": 8400, "removed_ansi": 32, "removed_progress": 12, "removed_timestamps": 20, "removed_separators": 3 },
    "extract": { "signal_lines": 15, "sampled": 0 },
    "compress": { "original_lines": 98, "compressed_lines": 90, "compressed_blocks": 2 },
    "tokens": { "estimated_tokens": 410, "budget": null, "truncated": false }
  }
}
```

---

## When to use explicit filter

- **Wrong auto-profile** — a log was detected as `default`; re-run with
  `--profile log` to get timestamp stripping and signal extraction.
- **Old unfiltered records** — memories added before `auto_filter` was
  enabled have no `clean_content`. Run `vesma filter --all` to backfill.
- **New profile** — after a pipeline upgrade adds a new profile, re-filter
  to take advantage of improved heuristics.
- **Budget change** — re-run with `--budget` to enforce a tighter token
  limit for a specific recall context.

---

## Filter stats in `vesma stats`

`vesma stats` includes a filter section showing vault-wide health:

```text
auto_filter: True
filtered_count: 142
unfiltered_count: 8
avg_reduction_pct: 63.4
by_profile: {'log': 48, 'terminal': 52, 'code': 22, 'default': 20}
```

| Metric | Meaning |
|--------|---------|
| `auto_filter` | Whether auto-filter is enabled in config |
| `filtered_count` | Memories with `clean_content` populated |
| `unfiltered_count` | Memories without `clean_content` (pre-filter or failed) |
| `avg_reduction_pct` | Average size reduction across filtered memories |
| `by_profile` | Count of memories per detected profile |

A high `unfiltered_count` relative to `filtered_count` suggests running
`vesma filter --all` to backfill.

---

## Examples

### Terminal output — before and after

**Raw (340 lines, 12 000 chars):**

```text
[38;5;10m✓[0m Building...
[38;5;10m✓[0m Building...
[██████████] 100%
2026-06-20T10:14:32Z INFO  Starting build
2026-06-20T10:14:33Z INFO  Compiling module A
2026-06-20T10:14:34Z ERROR FileNotFoundError: config.yaml
2026-06-20T10:14:34Z INFO  Compiling module B
...
```

**Filtered (`terminal` profile, 42 lines, 1 800 chars):**

```text
✓ Building...
ERROR FileNotFoundError: config.yaml
INFO  Compiling module B
... (8 similar lines) ...
```

ANSI codes, progress bars, and timestamps stripped; duplicate build lines
collapsed; error line preserved with context.

### Code — before and after

**Raw (repeated import blocks across files):**

```python
import os
import sys
import logging
import os
import sys
import logging
```

**Filtered (`code` profile):**

```python
import os
import sys
import logging
```

Exact duplicates removed; the near-duplicate detector normalises lines
(alphanumeric-only comparison) so whitespace-only variants collapse too.

---

## Configuration

```yaml
vesma:
  auto_filter: true   # run filter on every mnemos_add (default: true)
```

To disable auto-filter (store raw content only, filter manually later):

```yaml
vesma:
  auto_filter: false
```

Profile selection is automatic — there is no global "default profile"
config. Override per-call with `--profile` (CLI) or the `profile`
argument (MCP `mnemos_filter`).

---

## See also

- [Integration Guide](integration-guide.md) — behavioural instructions that tell agents *when* to filter.
- [MCP Tools Reference](mcp-tools.md) — full `vesma_*` tool catalogue.
- [CLI Reference](cli-reference.md) — every `vesma` subcommand.
