"""M10 — Context Filter pipeline.

5-stage filtering that sits between raw input and model-facing flows:
  1. dedup    — exact + near-duplicate line suppression
  2. noise    — ANSI/progress/timestamps/separators cleanup
  3. extract  — errors/warnings/exit-status + informative sampling
  4. compress — semantic compression of repetitive blocks
  5. tokens   — pre-tokenization estimation and budget accounting

Profiles: log | terminal | code | docs | web | default
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# ── Stage 1: dedup ───────────────────────────────────────────────────────────


def _stage_dedup(text: str, threshold: float = 0.9) -> tuple[str, dict[str, Any]]:
    """Remove exact and near-duplicate lines.

    JSON content is skipped — dedup would break JSON array structure by
    removing repeated keys like `"value": 0`, making the array unparseable
    for the compress stage's JSON sampling (P0-3 fix).

    Returns (deduped_text, stats).
    """
    # Skip dedup for JSON content — preserve structure for compress stage
    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            import json as _json

            _json.loads(text)
            lines = text.splitlines()
            return text, {
                "lines_in": len(lines),
                "lines_out": len(lines),
                "exact_dups": 0,
                "near_dups": 0,
                "json_skipped": True,
            }
        except (ValueError, TypeError):
            pass  # Not valid JSON, continue with normal dedup

    lines = text.splitlines()
    if not lines:
        return text, {"lines_in": 0, "lines_out": 0, "exact_dups": 0, "near_dups": 0}

    seen: set[str] = set()
    out_lines: list[str] = []
    exact_dups = 0
    near_dups = 0

    for line in lines:
        stripped = line.rstrip()
        if not stripped:
            out_lines.append(line)
            continue

        # Exact duplicate
        if stripped in seen:
            exact_dups += 1
            continue

        # Near-duplicate: same normalized form (alphanumeric only)
        normalized = re.sub(r"\W+", "", stripped).lower()
        if normalized and normalized in seen:
            near_dups += 1
            continue

        seen.add(stripped)
        if normalized:
            seen.add(normalized)
        out_lines.append(line)

    stats = {
        "lines_in": len(lines),
        "lines_out": len(out_lines),
        "exact_dups": exact_dups,
        "near_dups": near_dups,
    }
    return "\n".join(out_lines), stats


# ── Stage 2: noise ───────────────────────────────────────────────────────────

# ANSI escape codes
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Progress bars / spinners
_PROGRESS_RE = re.compile(
    r"[#\-=▶◀◐◑◒◓\|\\/]+\s*\d+%?"
    r"|\[\s*#+\s*\]\s*\d*%?"
    r"|\[\s*\d+/\d*\s*\]"
)
# Timestamps (ISO, common log formats)
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\s*"
)
# Separator lines
_SEPARATOR_RE = re.compile(r"^[-=]{3,}$|^\s*[*•]\s*$", re.MULTILINE)


def _stage_noise(text: str, profile: str) -> tuple[str, dict[str, Any]]:
    """Clean up noise patterns based on profile.

    Returns (cleaned_text, stats).
    """
    original_len = len(text)
    removed_ansi = 0
    removed_progress = 0
    removed_timestamps = 0
    removed_separators = 0

    # ANSI codes (all profiles)
    text, count = _ANSI_RE.subn("", text)
    removed_ansi = count

    if profile in ("log", "terminal"):
        # Timestamps first (before progress bars that might corrupt dates)
        text, count = _TIMESTAMP_RE.subn("", text)
        removed_timestamps = count

        # Progress bars
        text, count = _PROGRESS_RE.subn("", text)
        removed_progress = count

    if profile in ("log", "terminal", "default"):
        # Separator lines
        lines = text.splitlines()
        out_lines = []
        for line in lines:
            if _SEPARATOR_RE.match(line.strip()):
                removed_separators += 1
                continue
            out_lines.append(line)
        text = "\n".join(out_lines)

    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    stats = {
        "original_chars": original_len,
        "removed_ansi": removed_ansi,
        "removed_progress": removed_progress,
        "removed_timestamps": removed_timestamps,
        "removed_separators": removed_separators,
    }
    return text, stats


# ── Stage 3: extract ─────────────────────────────────────────────────────────

# Error/warning patterns
_ERROR_RE = re.compile(
    r"(?:error|exception|traceback|failed|failure|fatal|panic|assertionerror|"
    r"runtimeerror|typeerror|valueerror|keyerror|indexerror)\s*:?\s*",
    re.IGNORECASE,
)
_WARNING_RE = re.compile(
    r"(?:warning|warn|deprecated|obsolete|todo|fixme|hack|xxx)\s*:?\s*",
    re.IGNORECASE,
)
_EXIT_RE = re.compile(
    r"(?:exit\s*code|return\s*code|status\s*code|rc=|exited\s*with(?:\s*code)?)"
    r"\s*:?\s*\d+",
    re.IGNORECASE,
)

# Verbose success patterns — only dropped in log/terminal profiles
_VERBOSE_RE = re.compile(
    r"(?:^\s*\[\s*(?:debug|info|trace)\s*\]|"
    r"^\s*(?:debug|info|trace)\s*:?\s|"
    r"(?:started|completed|finished|succeeded|ok|done|ready)\s*\.?\s*$)",
    re.IGNORECASE,
)

# Import patterns (Python, JS/TS, Go, Rust, Java, C) — for code compression
_IMPORT_RE = re.compile(
    r"^\s*(?:import\s|from\s+\S+\s+import\s|#include\s|use\s|"
    r"require\s|const\s+\{.*\}\s*=\s*require)",
    re.IGNORECASE,
)


def _stage_extract(text: str, profile: str) -> tuple[str, dict[str, Any]]:
    """Extract signal-rich lines and sample context.

    Profile-aware (P0-3 fix):
      - log/terminal: aggressive — keep only signal lines + context, drop
        verbose success lines ("INFO", "DEBUG", "started", "completed").
      - code: moderate — keep all lines (compression handled in stage 4).
      - docs/web: light — return original, preserve content.
      - JSON arrays: skip extraction — let stage 4 (compress) handle JSON
        array sampling. Extracting signal lines from JSON would destroy
        the array structure before compress can sample it.

    Returns (extracted_text, stats).
    """
    lines = text.splitlines()
    if not lines:
        return text, {"signal_lines": 0, "sampled": 0}

    # Light touch for prose-heavy profiles — no extraction
    if profile in ("docs", "web", "default"):
        return text, {"signal_lines": 0, "sampled": 0}

    # JSON content — skip extraction, let compress stage handle it
    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            import json as _json

            _json.loads(text)
            return text, {"signal_lines": 0, "sampled": 0, "json_skipped": True}
        except (ValueError, TypeError):
            pass  # Not valid JSON, continue with normal extraction

    signal_lines: list[tuple[int, str]] = []  # (idx, line)
    verbose_dropped = 0

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # Signal detection
        is_signal = bool(
            _ERROR_RE.search(stripped) or _WARNING_RE.search(stripped) or _EXIT_RE.search(stripped)
        )

        if is_signal:
            # Include context: 2 lines before and after
            start = max(0, idx - 2)
            end = min(len(lines), idx + 3)
            for i in range(start, end):
                if (i, lines[i]) not in signal_lines:
                    signal_lines.append((i, lines[i]))
        elif profile in ("log", "terminal") and _VERBOSE_RE.search(stripped):
            # Drop verbose success lines in aggressive profiles
            verbose_dropped += 1

    if signal_lines:
        # Sort by original index and reconstruct
        signal_lines.sort(key=lambda x: x[0])
        out = "\n".join(line for _, line in signal_lines)
        stats = {
            "signal_lines": len(signal_lines),
            "sampled": len(signal_lines),
            "verbose_dropped": verbose_dropped,
        }
        return out, stats

    # No signals found — return original with sampling for long content
    if len(lines) > 50 and profile in ("log", "terminal"):
        # Keep first 10, last 10, and every 10th in between
        keep = set(range(10))
        keep.update(range(len(lines) - 10, len(lines)))
        for i in range(10, len(lines) - 10, 10):
            keep.add(i)
        out_lines = [lines[i] for i in sorted(keep)]
        sampled = len(lines) - len(out_lines)
        stats = {"signal_lines": 0, "sampled": sampled, "verbose_dropped": verbose_dropped}
        return "\n".join(out_lines), stats

    return text, {"signal_lines": 0, "sampled": 0}


# ── Stage 4: compress ────────────────────────────────────────────────────────


def _stage_compress(text: str, profile: str) -> tuple[str, dict[str, Any]]:
    """Compress repetitive blocks (stack traces, repeated patterns).

    Content-type-aware (P0-3 fix):
      - JSON arrays: statistical sampling — keep schema (first items),
        recency (last items), anomalies (errors/non-zero), drop middle.
        Target 60%+ reduction on large JSON.
      - Code blocks: drop repeated boilerplate (imports, blank lines),
        keep signatures + errors.
      - Logs: collapse repeated stack-trace frames (original behavior).
      - Prose/docs: light touch — only collapse 3+ identical lines.

    Returns (compressed_text, stats).
    """
    # JSON array sampling — detect JSON arrays and apply SmartCrusher-like
    # statistical sampling (head + tail + anomalies, drop middle).
    if profile in ("log", "terminal", "default", "web"):
        json_result = _compress_json_arrays(text)
        if json_result is not None:
            compressed, j_stats = json_result
            return compressed, {
                "original_lines": text.count("\n") + 1,
                "compressed_lines": compressed.count("\n") + 1,
                "compressed_blocks": j_stats["blocks_collapsed"],
                "json_arrays_sampled": j_stats["arrays_sampled"],
                "json_items_in": j_stats["items_in"],
                "json_items_out": j_stats["items_out"],
                # C7: out-of-band accounting consumed by the issuance
                # envelope (ccr.compress ``dropped_items``).
                "json_items_dropped": j_stats["items_dropped"],
            }

    # Code boilerplate stripping
    if profile == "code":
        compressed, c_stats = _compress_code(text)
        return compressed, {
            "original_lines": text.count("\n") + 1,
            "compressed_lines": compressed.count("\n") + 1,
            "compressed_blocks": c_stats["boilerplate_blocks"],
            "imports_dropped": c_stats["imports_dropped"],
            "blank_lines_dropped": c_stats["blank_lines_dropped"],
        }

    # Log/terminal: collapse repeated stack-trace frames (original behavior)
    lines = text.splitlines()
    if len(lines) < 10:
        return text, {"original_lines": len(lines), "compressed_blocks": 0}

    out_lines: list[str] = []
    compressed_blocks = 0
    i = 0

    while i < len(lines):
        line = lines[i]
        # Look for repeated patterns (same first two words)
        words = line.strip().split()
        if len(words) >= 2 and i + 2 < len(lines):
            prefix = f"{words[0]} {words[1]}"
            run = [line]
            j = i + 1
            while j < len(lines):
                j_words = lines[j].strip().split()
                if len(j_words) >= 2 and f"{j_words[0]} {j_words[1]}" == prefix:
                    run.append(lines[j])
                    j += 1
                else:
                    break

            if len(run) >= 3:
                out_lines.append(f"  ... ({len(run)} similar lines) ...")
                compressed_blocks += 1
                i = j
                continue

        out_lines.append(line)
        i += 1

    result = "\n".join(out_lines)
    stats = {
        "original_lines": len(lines),
        "compressed_lines": len(out_lines),
        "compressed_blocks": compressed_blocks,
    }
    return result, stats


# ── JSON array compression (SmartCrusher-inspired) ───────────────────────────


def _compress_json_arrays(text: str) -> tuple[str, dict[str, Any]] | None:
    """Compress JSON arrays via statistical sampling.

    Strategy (headroom SmartCrusher-inspired):
      - Keep first N items (schema/structure representative)
      - Keep last N items (recency)
      - Keep anomaly items (errors, non-zero values, non-null)
      - Drop middle items, insert a marker

    Only triggers on arrays with ≥20 items (small arrays not worth it).
    Returns (compressed_text, stats) or None if no JSON arrays found.
    """
    import json as _json

    # Find JSON array boundaries — look for lines that are pure JSON arrays
    # or fenced JSON blocks. We scan for the largest JSON array in the text.
    try:
        # Try parsing the entire text as JSON first
        data = _json.loads(text)
        if isinstance(data, list) and len(data) >= 20:
            compressed, arr_stats = _sample_json_array(data)
            stats = {
                "blocks_collapsed": 1,
                "arrays_sampled": 1,
                "items_in": arr_stats["items_in"],
                "items_out": arr_stats["items_out"],
                # C7: authoritative out-of-band drop count (the in-band
                # ``_compressed_marker`` object is human-readable legacy).
                "items_dropped": arr_stats["dropped"],
            }
            return _json.dumps(compressed, indent=2, ensure_ascii=False), stats
    except (ValueError, TypeError):
        pass

    # Scan for embedded JSON arrays in fenced blocks or standalone
    # Look for ```json ... ``` blocks or lines starting with [
    array_pattern = re.compile(r"(\[[\s\S]*?\])", re.MULTILINE)
    modified = False
    stats_total: dict[str, Any] = {
        "blocks_collapsed": 0,
        "arrays_sampled": 0,
        "items_in": 0,
        "items_out": 0,
        "items_dropped": 0,
    }

    def _replace_array(match: re.Match[str]) -> str:
        nonlocal modified
        raw = match.group(1)
        try:
            arr = _json.loads(raw)
        except (ValueError, TypeError):
            return raw
        if not isinstance(arr, list) or len(arr) < 20:
            return raw
        compressed, arr_stats = _sample_json_array(arr)
        modified = True
        stats_total["blocks_collapsed"] += 1
        stats_total["arrays_sampled"] += 1
        stats_total["items_in"] += arr_stats["items_in"]
        stats_total["items_out"] += arr_stats["items_out"]
        stats_total["items_dropped"] += arr_stats["dropped"]
        return _json.dumps(compressed, indent=2, ensure_ascii=False)

    result = array_pattern.sub(_replace_array, text)
    if not modified:
        return None
    return result, stats_total


def _sample_json_array(arr: list[Any]) -> tuple[list[Any], dict[str, Any]]:
    """Statistical sampling of a JSON array.

    Keep: first 5 (schema), last 5 (recency), anomalies (errors/non-zero).
    Drop: middle items, replaced with a count marker.
    """
    n = len(arr)
    head = arr[:5]
    tail = arr[-5:]
    middle = arr[5:-5]

    # Anomaly detection in middle: keep items with errors, non-zero, non-null
    anomalies: list[Any] = []
    for item in middle:
        if _is_json_anomaly(item):
            anomalies.append(item)

    # Cap anomalies to avoid keeping too many
    if len(anomalies) > 10:
        anomalies = anomalies[:10]

    dropped_count = len(middle) - len(anomalies)
    # C7 (ArchCom 2026-08-27): the in-band marker object below is
    # HUMAN-READABLE LEGACY ONLY — it lives inside issued content the
    # caller can rewrite, so it is spoofable and must NEVER be parsed
    # back for decisions or accounting. The authoritative drop count
    # travels OUT-of-band: the per-array ``dropped`` figure returned in
    # stats feeds the issuance envelope (``ccr.compress`` →
    # ``dropped_items``), mirroring the P1-b per-item ``redactions``
    # pattern. Source parser inventory at landing: none (grep
    # ``_compressed_marker`` — producer-only in src).
    result: list[Any] = []
    result.extend(head)
    if anomalies:
        result.append(
            {
                "_compressed_marker": True,
                "dropped": dropped_count,
                "anomalies_kept": len(anomalies),
            }
        )
        result.extend(anomalies)
    else:
        result.append({"_compressed_marker": True, "dropped": dropped_count})
    result.extend(tail)

    return result, {
        "items_in": n,
        "items_out": len(result),
        "dropped": dropped_count,
    }


def _is_json_anomaly(item: Any) -> bool:
    """Detect anomalous JSON items worth keeping (errors, non-zero, non-null).

    Conservative detection — only flags items that genuinely look like
    errors/anomalies, not every item with a non-zero numeric field.
    """
    if item is None:
        return False
    if isinstance(item, bool):
        return item  # True is interesting (errors often flagged)
    if isinstance(item, str):
        lower = item.lower()
        return any(
            kw in lower
            for kw in ("error", "fail", "warn", "exception", "critical", "fatal", "timeout")
        )
    if isinstance(item, dict):
        # Check for error/status keys with error-like values
        for k, v in item.items():
            if not isinstance(k, str):
                continue
            kl = k.lower()
            # Keys that indicate error/anomaly fields
            if kl in ("error", "error_code", "error_message", "exception", "failure"):
                return True
            if kl in ("status", "level", "severity", "state"):
                # Check if the value indicates an error
                if isinstance(v, str):
                    vl = v.lower()
                    if any(kw in vl for kw in ("error", "fail", "fatal", "critical", "warn")):
                        return True
                if isinstance(v, bool) and v:
                    return True
        return False
    if isinstance(item, list):
        return any(_is_json_anomaly(v) for v in item)
    # Numbers and other types — not anomalies by themselves
    return False


# ── Code compression ─────────────────────────────────────────────────────────


def _compress_code(text: str) -> tuple[str, dict[str, Any]]:
    """Compress code by dropping boilerplate (imports, blank lines).

    Keeps: function/class signatures, logic, errors, comments with TODO/FIXME.
    Drops: repeated import blocks, consecutive blank lines.
    """
    lines = text.splitlines()
    out_lines: list[str] = []
    total_imports_dropped = 0
    blank_lines_dropped = 0
    boilerplate_blocks = 0

    in_import_block = False
    import_block_start = -1
    block_imports_dropped = 0

    for line in lines:
        stripped = line.strip()

        # Collapse consecutive blank lines (keep at most 1)
        if not stripped:
            if out_lines and out_lines[-1].strip() == "":
                blank_lines_dropped += 1
                continue
            out_lines.append(line)
            continue

        # Import handling: keep first import, collapse rest into a marker
        if _IMPORT_RE.match(stripped):
            if not in_import_block:
                in_import_block = True
                import_block_start = len(out_lines)
                block_imports_dropped = 0
                out_lines.append(line)
            else:
                block_imports_dropped += 1
                total_imports_dropped += 1
                continue
        else:
            if in_import_block:
                # End of import block — add marker if we dropped any
                if block_imports_dropped > 0:
                    marker = f"  # ... {block_imports_dropped} more imports ..."
                    out_lines.insert(import_block_start + 1, marker)
                    boilerplate_blocks += 1
                in_import_block = False
            out_lines.append(line)

    # Handle trailing import block
    if in_import_block and block_imports_dropped > 0:
        marker = f"  # ... {block_imports_dropped} more imports ..."
        out_lines.append(marker)
        boilerplate_blocks += 1

    result = "\n".join(out_lines)
    return result, {
        "boilerplate_blocks": boilerplate_blocks,
        "imports_dropped": total_imports_dropped,
        "blank_lines_dropped": blank_lines_dropped,
    }


# ── Stage 5: tokens ────────────────────────────────────────────────────────────


def _estimate_tokens(text: str) -> int:
    """Rough token estimation: ~4 chars per token for English/code."""
    if not text:
        return 0
    # Count words and punctuation
    words = len(text.split())
    chars = len(text)
    # Heuristic: average token is ~4 chars for code/English
    return max(words, chars // 4)


#: Public alias for :func:`_estimate_tokens` — single source of the token
#: estimation heuristic, shared with the ``assemble_context`` budget stage
#: (ADR-0017 D1, vesma #125). Import this name, not the underscore one.
estimate_tokens = _estimate_tokens


def _stage_tokens(text: str, budget: int | None = None) -> tuple[str, dict[str, Any]]:
    """Estimate tokens and optionally truncate to budget.

    Returns (text, stats).
    """
    estimated = _estimate_tokens(text)
    stats = {
        "estimated_tokens": estimated,
        "budget": budget,
        "truncated": False,
    }

    if budget and estimated > budget and text:
        # Truncate by characters (rough approximation)
        target_chars = budget * 4
        truncated_text = text[:target_chars]
        # Try to end at a line boundary
        last_nl = truncated_text.rfind("\n")
        if last_nl > target_chars * 0.8:
            truncated_text = truncated_text[:last_nl]
        truncated_text += "\n\n[...truncated...]"
        stats["truncated"] = True
        stats["estimated_tokens_after"] = _estimate_tokens(truncated_text)
        return truncated_text, stats

    return text, stats


# ── Profile detection ─────────────────────────────────────────────────────────


def detect_profile(text: str, hint: str | None = None) -> str:
    """Heuristic profile detection from content.

    Returns one of: log, terminal, code, docs, web, default
    """
    if hint:
        hint_lower = hint.lower()
        if hint_lower in ("log", "terminal", "code", "docs", "web", "default"):
            return hint_lower

    text_lower = text.lower()
    lines = text.splitlines()

    # Log detection
    log_indicators = 0
    for line in lines[:20]:
        if re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", line):
            log_indicators += 1
        if re.search(r"\[(?:debug|info|warn|error|fatal)\]", line, re.IGNORECASE):
            log_indicators += 1
    if log_indicators >= 3:
        return "log"

    # Terminal detection
    if "\x1b[" in text or any(c in text for c in "▶◀◐◑◒◓"):
        return "terminal"

    # Code detection
    code_patterns = [
        r"^\s*def\s+",
        r"^\s*class\s+",
        r"^\s*import\s+",
        r"^\s*#\s*include",
        r"^\s*function\s+",
        r"^\s*const\s+",
        r"^\s*let\s+",
        r"^\s*var\s+",
    ]
    code_hits = sum(1 for pat in code_patterns for line in lines if re.search(pat, line))
    if code_hits >= 3:
        return "code"

    # Web detection
    if "<html" in text_lower or "<!doctype" in text_lower or "<div" in text_lower:
        return "web"

    # Docs detection
    if text.count("#") > 3 or text.count("---") > 1:
        return "docs"

    return "default"


# ── Main pipeline ────────────────────────────────────────────────────────────


def apply_filter(
    text: str,
    *,
    profile: str | None = None,
    budget: int | None = None,
    version: str = "v1",
) -> dict[str, Any]:
    """Run the full 5-stage context filter pipeline.

    Args:
        text: Raw content to filter.
        profile: One of log | terminal | code | docs | web | default.
                 If None, auto-detected from content.
        budget: Optional token budget for truncation.
        version: Pipeline version string for tracking.

    Returns:
        Dict with:
          - clean_content: str
          - profile: str
          - stats: dict (per-stage metrics)
          - version: str
    """
    detected = profile or detect_profile(text, hint=profile)
    logger.debug("apply_filter: profile=%s budget=%s", detected, budget)

    stats: dict[str, Any] = {"profile": detected}

    # Stage 1: dedup
    text, dedup_stats = _stage_dedup(text)
    stats["dedup"] = dedup_stats

    # Stage 2: noise
    text, noise_stats = _stage_noise(text, detected)
    stats["noise"] = noise_stats

    # Stage 3: extract
    text, extract_stats = _stage_extract(text, detected)
    stats["extract"] = extract_stats

    # Stage 4: compress
    text, compress_stats = _stage_compress(text, detected)
    stats["compress"] = compress_stats

    # Stage 5: tokens
    text, token_stats = _stage_tokens(text, budget)
    stats["tokens"] = token_stats

    # Overall reduction
    stats["reduction"] = {
        "original_chars": noise_stats.get("original_chars", len(text)),
        "final_chars": len(text),
        "char_ratio": len(text) / max(noise_stats.get("original_chars", 1), 1),
    }

    return {
        "clean_content": text,
        "profile": detected,
        "stats": stats,
        "version": version,
    }
