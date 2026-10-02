"""P1-5 — CacheAligner: prefix stabilization for provider KV cache hits.

Inspired by headroom's CacheAligner
(https://github.com/headroomlabs-ai/headroom, Apache 2.0). This is an
original implementation — no headroom code is imported.

Problem
-------
Provider prefix caches (Anthropic ``cache_control``, OpenAI prefix caching)
hit only when the *prefix* of a prompt is byte-identical across requests.
System prompts that embed dynamic values (timestamps, UUIDs, session ids,
short-lived tokens) near the top break the cache on every request — the
cache misses at the first differing byte.

Solution
--------
Extract dynamic spans from the text, relocate them to a
``--- Dynamic context ---`` block at the *end*, and leave a stable prefix
behind. The prefix up to the first dynamic span becomes cache-stable; the
dynamic values still reach the model, just from the tail.

The extractor is conservative: it only matches well-formed dynamic
literals (ISO timestamps, UUIDs, ``sess-``/``session:`` ids, bare hex/base64
tokens of sufficient entropy). Code identifiers, file paths, and prose are
not mangled. CCR markers (``[compressed: <hash> | …]``) are ATOMIC
protected regions in EVERY profile — a marker whose hash is relocated to
the trailing block is unreadable and breaks the ``mnemos_retrieve``
round-trip (vesma #282).

Determinism: same input always produces the same output (patterns are
applied in a fixed order; extracted spans are sorted by position).
"""

from __future__ import annotations

import re
from typing import Any

from vesma.ccr import CCR_MARKER_RE

# ── Dynamic-content patterns ─────────────────────────────────────────────────
# Ordered by specificity (most specific first) so e.g. an ISO timestamp is
# matched as a timestamp, not as a bare token. Each pattern is compiled once.
#
# Naming convention for extracted spans:
#   {"kind": <type>, "value": <matched text>, "start": <int>, "end": <int>}

# ISO 8601 timestamps with optional timezone: 2026-07-17T10:30:00Z,
# 2026-07-17T10:30:00.123+02:00, 2026-07-17 10:30:00.
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z|[+-]\d{2}:?\d{2})?\b"
)

# UUIDs (canonical 8-4-4-4-12 hex): 550e8400-e29b-41d4-a716-446655440000.
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# Session ids: sess-..., session:..., session-id-..., sid-...
_SESSION_RE = re.compile(
    r"\b(?:sess(?:ion)?[-:]|session-id-|sid-)[0-9a-zA-Z]{4,64}\b",
    re.IGNORECASE,
)

# Short-lived opaque tokens: bare hex/base64url of length >= 20, only when
# bounded by non-word context (whitespace, quotes, brackets). The entropy
# floor (length) avoids matching short hex words like ``0xDEADBEEF``.
_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_/+=])[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_/+=])")

# Calendar dates without time: 2026-07-17, 2026/07/17. Matched AFTER
# timestamps so a full timestamp is not split into a bare date.
_DATE_RE = re.compile(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b")

# Ordered list of (kind, regex). Order matters: more specific patterns first
# so a timestamp is not partially consumed by the date or token patterns.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("timestamp", _TIMESTAMP_RE),
    ("uuid", _UUID_RE),
    ("session_id", _SESSION_RE),
    ("date", _DATE_RE),
    ("token", _TOKEN_RE),
]

# Profile toggles: which pattern kinds each profile extracts.
#   None (default) — extract all kinds.
#   "code"         — skip tokens (would mangle long identifiers/hashes in code).
#   "docs"         — skip tokens (prose rarely contains real tokens; avoids
#                    mangling long hyphenated words).
_PROFILE_SKIP: dict[str, set[str]] = {
    "code": {"token"},
    "docs": {"token"},
}

# vesma #282 — CCR markers are ATOMIC: protected from extraction in ALL
# profiles. A marker `[compressed: <hash> | N→M chars | retrieve via
# mnemos_retrieve]` carries a 64-hex hash that _TOKEN_RE happily matches
# in token-extracting profiles (log/terminal/web/default) — the hash would
# be relocated to the trailing Dynamic-context block, destroying the marker
# line and with it the model's ability to read the hash for
# mnemos_retrieve. The span (from `vesma.ccr`, the single source of truth
# for the marker shape) is collected FIRST and any dynamic span of ANY kind
# overlapping it is dropped, so the marker stays byte-identical in place.


def _protected_marker_spans(text: str) -> list[tuple[int, int]]:
    """CCR marker spans in ``text`` — extraction-excluded regions."""
    return [(m.start(), m.end()) for m in CCR_MARKER_RE.finditer(text)]


def _extract_spans(
    text: str,
    skip: set[str] | None,
    protected: list[tuple[int, int]] | None = None,
) -> list[dict[str, Any]]:
    """Find all dynamic spans in ``text``.

    Returns a list of ``{"kind","value","start","end"}`` dicts sorted by
    ``start``. Overlapping matches are resolved by earliest start then
    longest match — a timestamp wins over a bare date overlapping its tail.

    ``protected`` (vesma #282) lists regions (``(start, end)``) that no
    dynamic span may touch — CCR marker spans in every profile. Any span
    of any kind overlapping a protected region is dropped, so a marker
    stays byte-identical at its original position.
    """
    raw: list[dict[str, Any]] = []
    for kind, rx in _PATTERNS:
        if skip and kind in skip:
            continue
        for m in rx.finditer(text):
            start, end = m.start(), m.end()
            if protected and any(start < p_end and end > p_start for p_start, p_end in protected):
                continue
            raw.append(
                {
                    "kind": kind,
                    "value": m.group(0),
                    "start": start,
                    "end": end,
                }
            )
    if not raw:
        return []

    # Sort by start, then by longest match (end - start) descending so that
    # a timestamp (longer) is preferred over a date (shorter) overlapping
    # its tail. This produces a deterministic, non-overlapping selection.
    raw.sort(key=lambda s: (s["start"], -(s["end"] - s["start"])))

    # Greedy non-overlapping selection: walk the sorted list, drop any span
    # whose start is inside a previously-accepted span.
    accepted: list[dict[str, Any]] = []
    last_end = -1
    for span in raw:
        if span["start"] >= last_end:
            accepted.append(span)
            last_end = span["end"]
    return accepted


def align(
    text: str,
    *,
    profile: str | None = None,
    skip_kinds: set[str] | None = None,
) -> dict[str, Any]:
    """Relocate dynamic spans to the end of ``text`` for prefix stability.

    Args:
        text: Input text (system-prompt-like).
        profile: Optional filter profile (``"code"``, ``"docs"``) that
            toggles which pattern kinds are extracted. ``None`` or the
            string ``"default"`` extracts all kinds (the two are
            equivalent — "default" is the canonical no-op profile name).
        skip_kinds: Optional set of pattern kinds to skip regardless of
            profile. Merged (union) with the profile's skip set so a
            config-level toggle can widen what a profile already skips.
            Kind names match the ``_PATTERNS`` keys: ``"timestamp"``,
            ``"uuid"``, ``"session_id"``, ``"date"``, ``"token"``.

    Returns:
        ``{"aligned_text": str, "extracted": list[dict], "prefix_stabilized": bool,
        "moved_chars": int}``.

        - ``aligned_text`` — the input with dynamic spans removed and a
          ``--- Dynamic context ---`` block appended at the end listing
          each extracted value.
        - ``extracted`` — the list of extracted spans (kind, value, start,
          end in the *original* text).
        - ``prefix_stabilized`` — ``True`` when at least one span was
          extracted from the prefix region (i.e. the aligned prefix is
          longer than the original prefix up to the first dynamic span).
        - ``moved_chars`` — total characters relocated (sum of span
          lengths).

        CCR markers (``[compressed: …]``) are never extracted — see
        ``_extract_spans`` (vesma #282): they stay byte-identical at
        their original position in every profile.
    """
    if not text:
        return {
            "aligned_text": "",
            "extracted": [],
            "prefix_stabilized": False,
            "moved_chars": 0,
        }

    # Normalize profile: "default" string is equivalent to None (no-op
    # profile). This keeps the public surface friendly to callers that
    # pass the canonical profile name from config/UI without needing to
    # know that internally None means "no profile".
    if profile == "default":
        profile = None

    # Build the effective skip set: union of the profile's skip set and
    # any caller-supplied skip_kinds (e.g. from CacheAlignerConfig
    # per-kind toggles). Either source may be None/empty.
    profile_skip = _PROFILE_SKIP.get(profile or "") if profile else None
    skip = profile_skip | skip_kinds if profile_skip and skip_kinds else profile_skip or skip_kinds
    # vesma #282 — CCR marker spans are protected BEFORE any dynamic
    # pattern runs (all profiles, no profile can opt out): a relocated
    # marker hash breaks the mnemos_retrieve round-trip.
    protected = _protected_marker_spans(text)
    spans = _extract_spans(text, skip, protected=protected)
    if not spans:
        return {
            "aligned_text": text,
            "extracted": [],
            "prefix_stabilized": False,
            "moved_chars": 0,
        }

    # Remove spans from the text. Walk right-to-left so earlier offsets
    # stay valid as we delete. Replace each span with a single space to
    # avoid gluing adjacent tokens together (e.g. "...at 2026-07-17T...
    # done..." → "...at  done..."), then collapse runs of spaces later.
    chars: list[str] = list(text)
    for span in sorted(spans, key=lambda s: s["start"], reverse=True):
        for i in range(span["start"], span["end"]):
            chars[i] = "" if i != span["start"] else " "
    aligned_body = "".join(chars)
    # Collapse runs of spaces left by removal (but preserve newlines).
    aligned_body = re.sub(r"[ \t]{2,}", " ", aligned_body)
    # Trim trailing spaces on each line (removal may leave them).
    aligned_body = re.sub(r"[ \t]+\n", "\n", aligned_body)
    aligned_body = aligned_body.rstrip()

    # Build the dynamic-context block. Each extracted value is listed on
    # its own line with its kind, so the model still sees every value but
    # at the tail where it cannot break the prefix cache.
    lines = ["", "--- Dynamic context ---"]
    for span in spans:
        lines.append(f"- {span['kind']}: {span['value']}")
    dynamic_block = "\n".join(lines)

    aligned_text = aligned_body + "\n" + dynamic_block + "\n"

    moved_chars = sum(s["end"] - s["start"] for s in spans)
    # Prefix is stabilized iff the first dynamic span was not at offset 0
    # (i.e. there is a non-empty stable prefix before the first span).
    first_start = spans[0]["start"]
    prefix_stabilized = first_start > 0

    return {
        "aligned_text": aligned_text,
        "extracted": spans,
        "prefix_stabilized": prefix_stabilized,
        "moved_chars": moved_chars,
    }
