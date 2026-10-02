"""Extractive summary helpers for the A2A Sessions API (M16, v1).

Per the spec, v1 uses **no LLM** for turn summaries — this is by design:

  * Latency: a 200-char extract is a few microseconds, an LLM call is
    seconds and brittle under load.
  * Determinism: tests and idempotency checks can rely on the summary
    being a pure function of the content.
  * Cost: the entire point of the A2A summary endpoint is to avoid
    shoving full A2A messages across the wire to other agents.

The trade-off: summaries may be abrupt for very long content.  The
extractor prefers the first paragraph and falls back to a word-boundary
truncate of the first N chars.

The ``key_decisions`` extractor looks for two well-known patterns:

  * ``DECISION: ...`` — a free-form convention used in A2A messages.
  * ``- [x] ...``    — GitHub-flavoured markdown task done.

Both are extracted as-is (no rewriting) and capped at 5 entries to keep
the payload small.
"""

from __future__ import annotations

import re

# Match GitHub-flavoured markdown completed task: ``- [x] foo`` (case-insensitive
# on the x, per the spec) and a tiny subset of the ``DECISION:`` family.
#
# Two flavours are supported:
#   1. Line-start: ``\nDECISION: ...`` or ``\n- [x] ...`` (multi-line A2A msgs)
#   2. Sentence-boundary: ``. DECISION: ...`` or ``. - [x] ...`` (single-line
#      payloads where the whole turn is one paragraph)
#
# Both alternatives are wrapped in a non-capturing group, and the body
# up to the next sentence boundary (``. ``) or end-of-line / end-of-string
# is captured as group 1.
_DECISION_LINE_RE = re.compile(
    r"(?:^|[\n.]\s+)(?:- \[x\]|DECISION:)\s+([^\n.]+?)(?=\.\s|\n|$)",
    re.IGNORECASE | re.MULTILINE,
)


def extract_summary(content: str, max_chars: int = 200) -> str:
    """Return a short extractive summary of ``content``.

    Args:
        content: Full turn content.  Must be a string (empty allowed).
        max_chars: Hard upper bound on the returned length, in characters.
            Defaults to 200 — keeps the wire payload under 1 KB after
            JSON encoding for typical Unicode content.

    Returns:
        The first paragraph of ``content`` (whitespace-trimmed), truncated
        to ``max_chars`` at the nearest word boundary.  Empty content
        returns an empty string.
    """
    if not content:
        return ""
    text = content.strip()
    if not text:
        return ""

    first_para = text.split("\n\n", 1)[0].strip()
    if not first_para:
        return ""

    if len(first_para) <= max_chars:
        return first_para

    truncated = first_para[:max_chars]
    # Snap to the last whitespace so we don't cut a word in half.
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    return truncated.rstrip(",;:.- ") + "..."


def extract_key_decisions(content: str, max_items: int = 5) -> list[str]:
    """Return up to ``max_items`` decision-shaped lines from ``content``.

    The match is intentionally narrow:

      * Lines starting with ``DECISION:`` (case-insensitive).
      * Lines starting with ``- [x]`` (a completed GitHub-flavoured task).

    Both line-start and sentence-boundary (``. DECISION: ...``) prefixes
    are recognised, so a single-line A2A payload works just as well as a
    multi-line one.  Each match is returned as the full prefix-plus-body
    string (``DECISION: add column, not table``) so the caller can see
    the decision verbatim.

    Other markdown patterns (``* [x]``, ``1. [x]``, etc.) are not matched
    in v1 — the GCW A2A protocol spec only commits to the two above.
    """
    if not content:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _DECISION_LINE_RE.finditer(content):
        # Reconstruct the full line, including its ``DECISION:`` / ``- [x]``
        # prefix, so the caller sees a self-contained decision statement
        # rather than just the body.
        body = (m.group(1) or "").strip()
        # Pick the prefix that actually matched (either ``DECISION:`` or
        # ``- [x]``) by inspecting the matched span.
        full = m.group(0)
        if full.upper().lstrip(". ").upper().startswith("DECISION:"):
            candidate = f"DECISION: {body}".rstrip(" .,")
        else:
            candidate = f"- [x] {body}".rstrip(" .,")
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
        if len(out) >= max_items:
            break
    return out
