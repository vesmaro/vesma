"""Secrets detection scanner for the Vesma federation defence-in-depth.

This module is the **single source of truth** for secret-pattern detection
across vesmaro. It is consumed by three defence-in-depth layers
(ArchCom 2026-07-17 federation contract §2.2.1):

* **Layer 1 — write-path scanner** (this issue, #86): wired into
  ``MemoryManager.add`` / ``mnemos_add`` / HTTP ``POST /memories`` /
  ``ingest_url`` / ``ingest_path_scoped_rules``. On detect → auto-add the
  ``mnemos:no-federate`` tag so the record is excluded from external
  exchange (batch sync + mediated pull).
* **Layer 2 — background scanner** (issue #89, future): a periodic job
  that re-scans the corpus for false negatives missed at write time.
  Re-uses ``detect_secrets`` unchanged (DRY).
* **Layer 3 — moderation pipeline** (Phase 0, issue #85, future): runs
  at export / pull as a final defence. Re-uses ``detect_secrets`` and
  ``redact_content`` unchanged.

Public API (stable — must NOT break Layer 2 / Layer 3 consumers):

* :class:`SecretFinding`  — dataclass describing one match.
* :func:`detect_secrets` — scan ``content`` and return findings.
* :func:`redact_content` — replace each match with ``<REDACTED:<name>>``.

Design constraints
------------------
* **Never log raw matched values.** ``SecretFinding.matched_value`` exists
  for programmatic use (redaction, masking) — logging code MUST use
  ``pattern_name`` + counts only.
* **Compiled patterns are module-level** so the regex compilation cost is
  paid once per process, not once per call.
* **High-entropy detection uses Shannon entropy** on base64-like spans.
  The threshold is tuned to limit false positives on normal English prose
  (see ``_HIGH_ENTROPY_THRESHOLD`` docstring).
* **Overlapping patterns** are resolved by first-match precedence: the
  scanner sorts matches by start position and, for ties, by the order in
  which patterns are declared. Earlier-declared patterns win. A partially
  overlapping later finding (e.g. a high-entropy run whose prefix matched
  a discrete pattern) extends the accepted span to its end so no tail is
  left unredacted (m3, ADR-0018 P1).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "SecretFinding",
    "detect_secrets",
    "redact_content",
]

# ── Pattern catalogue ─────────────────────────────────────────────────────────
#
# Each entry: (pattern_name, compiled regex, description).
# Order matters for first-match precedence when two patterns match the
# same start offset (e.g. a JWT that begins with ``eyJ`` could overlap with
# an OpenAI ``sk-`` token only if the literal differs; precedence is still
# well-defined because the start offsets differ). The list is deliberately
# ordered: specific high-signal patterns first, generic high-entropy last.
#
# All test fixtures MUST use obviously fake values (see
# ``sensitive-data.instructions.md``). Real credentials never appear in
# this file or in tests.

_PatternDef = tuple[str, re.Pattern[str], str]

_PATTERNS: Final[list[_PatternDef]] = [
    # AWS access key IDs — 20 chars starting with AKIA + 16 uppercase alnum.
    (
        "aws-key",
        re.compile(r"AKIA[0-9A-Z]{16}"),
        "AWS access key ID",
    ),
    # GitHub tokens — prefixes: personal (ghp_), OAuth (gho_), user (ghu_),
    # server-to-server (ghs_), refresh (ghr_). Each followed by 36 alnum.
    (
        "github-token",
        re.compile(r"gh[opusrs]_[A-Za-z0-9]{36}"),
        "GitHub personal / OAuth / server token",
    ),
    # Slack tokens — xoxa-/xoxb-/xoxp-/xoxr-/xoxs- + alnum + dashes.
    (
        "slack-token",
        re.compile(r"xox[abprs]-[A-Za-z0-9-]+"),
        "Slack token",
    ),
    # OpenAI API keys — sk- prefix + 20+ alnum. (Also catches Anthropic
    # sk-ant-... keys since they share the sk- prefix.)
    (
        "openai-key",
        re.compile(r"sk-[A-Za-z0-9]{20,}"),
        "OpenAI / Anthropic API key (sk- prefix)",
    ),
    # JWT — three base64url segments joined by dots, starting with eyJ
    # (base64url-encoded ``{"``).
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
        "JSON Web Token",
    ),
    # PEM private key headers — ``-----BEGIN <KIND> PRIVATE KEY-----``.
    # We match only the header line; the body is base64 and is not
    # separately flagged (the header is the strong signal).
    (
        "pem-private-key",
        re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
        "PEM-encoded private key header",
    ),
    # Connection strings with embedded credentials —
    # ``<scheme>://<user>:<pass>@``. Schemes: postgres, postgresql, mysql,
    # mongodb (with optional +srv). The user/password portion is captured
    # up to the ``@`` but the host is NOT captured (avoid leaking hosts).
    (
        "connection-string",
        re.compile(r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://[^:/\s@]+:[^@\s]+@"),
        "Database connection string with embedded credentials",
    ),
]


# ── High-entropy detection ────────────────────────────────────────────────────
#
# Shannon entropy on base64-like spans. A span is "base64-like" if it is a
# long run of ``[A-Za-z0-9+/=_-]``. We compute Shannon entropy on the span
# and flag it when the entropy exceeds ``_HIGH_ENTROPY_THRESHOLD``.
#
# Threshold choice (documented):
#   - English prose has Shannon entropy ~3.5-4.5 bits/char when measured
#     over the full ASCII alphabet, but base64 spans are constrained to
#     64 symbols; within those spans normal text rarely exceeds ~4.2.
#   - Random base64 (keys, tokens) sits at ~5.5-6.0 bits/char.
#   - We set the threshold to **4.8 bits/char** with a minimum span length
#     of **32 chars** to suppress short matches (common identifiers like
#     ``someVariableName`` have high per-char entropy but short length).
#   - Tuned against the test suite (true positive: 40-char base64 key;
#     false positive: a 200-word English paragraph must NOT match).

_HIGH_ENTROPY_MIN_LEN: Final[int] = 32
_HIGH_ENTROPY_THRESHOLD: Final[float] = 4.8

# Span of base64-like characters (URL-safe + standard base64 alphabet).
_BASE64_SPAN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9+/=_-]{32,}")


def _shannon_entropy(text: str) -> float:
    """Shannon entropy in bits per character for ``text``.

    Returns 0.0 for empty input.
    """
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(text)
    ent = 0.0
    for c in counts.values():
        p = c / n
        ent -= p * math.log2(p)
    return ent


# ── Finding dataclass ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SecretFinding:
    """One detected secret occurrence.

    ``matched_value`` is the **raw** matched substring. It MUST NOT be
    logged. Use ``pattern_name`` and counts in log messages.
    Programmatic consumers (redaction, masking) may read ``matched_value``
    to perform the replacement, but it must never enter log records, chat
    output, Vesma memory content, or commits.
    """

    pattern_name: str
    matched_value: str
    start: int
    end: int

    def redacted_display(self) -> str:
        """Return a log-safe representation (``<REDACTED:<name>>``)."""
        return f"<REDACTED:{self.pattern_name}>"


# ── Public API ─────────────────────────────────────────────────────────────────


def detect_secrets(content: str) -> list[SecretFinding]:
    """Scan ``content`` for known secret patterns and high-entropy spans.

    Args:
        content: The text to scan. ``None`` or empty → empty list.

    Returns:
        List of :class:`SecretFinding` sorted by ``start`` ascending.
        Overlapping matches are resolved by first-match precedence:
        the earliest-starting match wins; ties broken by pattern
        declaration order (specific patterns first, entropy last).
        A partially overlapping later finding extends the accepted
        span to its own end (the tail of the longer run must not
        survive unredacted); fully contained findings are dropped.

    Notes:
        * The scanner is **read-only** — it never mutates ``content``.
        * Reusable: Layer 2 (background scanner) and Layer 3 (moderation
          pipeline) import this function verbatim. Do not add side
          effects, logging of raw values, or stateful caches.
    """
    if not content:
        return []

    raw_findings: list[SecretFinding] = []

    # ── Discrete patterns ──────────────────────────────────────────────
    for name, regex, _desc in _PATTERNS:
        for m in regex.finditer(content):
            raw_findings.append(
                SecretFinding(
                    pattern_name=name,
                    matched_value=m.group(0),
                    start=m.start(),
                    end=m.end(),
                )
            )

    # ── High-entropy spans ─────────────────────────────────────────────
    for m in _BASE64_SPAN_RE.finditer(content):
        span = m.group(0)
        if len(span) < _HIGH_ENTROPY_MIN_LEN:
            continue
        if _shannon_entropy(span) < _HIGH_ENTROPY_THRESHOLD:
            continue
        raw_findings.append(
            SecretFinding(
                pattern_name="high-entropy",
                matched_value=span,
                start=m.start(),
                end=m.end(),
            )
        )

    if not raw_findings:
        return []

    # ── Sort by start, then by pattern declaration order ───────────────
    # Pattern declaration order is encoded by the index in the combined
    # list. We attach the declaration index to each finding so the sort
    # is deterministic and the caller can rely on stable precedence.
    pattern_order: dict[str, int] = {name: i for i, (name, _rx, _d) in enumerate(_PATTERNS)}
    pattern_order["high-entropy"] = len(_PATTERNS)  # entropy is last

    raw_findings.sort(key=lambda f: (f.start, pattern_order.get(f.pattern_name, 999)))

    # ── Resolve overlaps ───────────────────────────────────────────────
    # Drop any finding whose span is fully contained within an
    # already-accepted finding. This handles the JWT-containing-AWS-like
    # case: the JWT starts earlier and is accepted; any AWS-key match
    # that falls *inside* the JWT span is dropped. Discrete patterns
    # that start earlier always win because of the sort above.
    #
    # Partial overlap (m3 fix, ADR-0018 P1): when a later finding starts
    # inside the accepted span but runs past it — e.g. a discrete pattern
    # matching the prefix of a longer high-entropy run — the accepted
    # span is EXTENDED to the later finding's end. Dropping the partial
    # overlap entirely would leave the tail of the longer run unredacted;
    # extending keeps the earlier finding's name (first-match precedence
    # for labelling) while guaranteeing no tail leaks into the clear.
    accepted: list[SecretFinding] = []
    last_end = -1
    for f in raw_findings:
        if f.start >= last_end:
            accepted.append(f)
            last_end = f.end
        elif f.end > last_end:
            prev = accepted[-1]
            accepted[-1] = SecretFinding(
                pattern_name=prev.pattern_name,
                matched_value=content[prev.start : f.end],
                start=prev.start,
                end=f.end,
            )
            last_end = f.end
        # else: fully contained → dropped

    return accepted


def redact_content(content: str, findings: list[SecretFinding]) -> str:
    """Replace each finding's span with ``<REDACTED:<pattern_name>>``.

    Args:
        content: The original text.
        findings: Findings returned by :func:`detect_secrets`. They MUST be
            sorted by ``start`` ascending (as :func:`detect_secrets`
            returns). Overlapping findings must already be resolved.

    Returns:
        A copy of ``content`` with every finding replaced. The original
        matched values are gone. If ``findings`` is empty, ``content`` is
        returned unchanged.

    Notes:
        * Works on a single forward pass using a list of string pieces,
          which is O(n) in content length.
        * Findings must not overlap — :func:`detect_secrets` already
          resolves overlaps, but a caller passing hand-built findings
          must de-overlap first. Overlapping findings raise
          ``ValueError``.
    """
    if not findings:
        return content

    # Defensive: verify non-overlapping (caller contract).
    last_end = -1
    for f in findings:
        if f.start < last_end:
            raise ValueError(
                "redact_content received overlapping findings; "
                "de-overlap before calling (use detect_secrets' output directly)."
            )
        last_end = f.end

    pieces: list[str] = []
    cursor = 0
    for f in findings:
        pieces.append(content[cursor : f.start])
        pieces.append(f"<REDACTED:{f.pattern_name}>")
        cursor = f.end
    pieces.append(content[cursor:])
    return "".join(pieces)


# ── Helper for logging-safe summaries ─────────────────────────────────────────


def findings_by_pattern(findings: list[SecretFinding]) -> dict[str, int]:
    """Return a ``{pattern_name: count}`` mapping suitable for logging.

    This helper exists so logging code never has to touch
    ``matched_value``. Use it like::

        counts = findings_by_pattern(findings)
        logger.info("auto-tagged record %s (patterns: %s)", memory_id, counts)
    """
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.pattern_name] = counts.get(f.pattern_name, 0) + 1
    return counts
