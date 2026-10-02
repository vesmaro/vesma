"""Moderation pipeline — federation defence-in-depth Layer 3.

This module is the **shared component** built once for Phase 0 (batch
sync, issue #85) and reused unchanged by Phase 2 (mediated pull, future).
It runs at export / pull time as the final defence: even if the
write-path scanner (Layer 1, #86) and the future background scanner
(Layer 2, #89) miss a sensitive record, the moderation pipeline
sanitizes the content before it leaves the node.

Pipeline (ArchCom 2026-07-17 federation contract §2.2)::

    raw content
      → secrets detector   (REUSE secrets_detector.detect_secrets)
      → PII scrubber        (NEW — regex-based, deterministic)
      → neutral-value replacement  (NEW — RFC-reserved ranges)
      → mapping table       (NEW — in-memory, TTL 24h, NOT replicated)
      → verdict            (allow / redact / refuse)
      → sanitized content  (counters logged, no raw values)

Design constraints (contract §2.2):

* **No verdict caching** (КП-3 cancelled) — each export/pull re-runs the
  pipeline on the current content. The mapping table is per-run,
  in-memory only; it is NEVER persisted to the DB or written to logs
  (the mapping itself is a leak surface).
* **`patterns` mode only** (КП-4 deferred) — full anonymization to
  RFC-reserved values. ``trusted`` mode (less anonymization for trusted
  peers) is a future stub; not implemented here.
* **Deterministic + auditable** — regex-based PII scrubbing, NOT LLM.
  The same input always produces the same sanitized output (modulo the
  mapping table, which is per-run but deterministic within a run).
* **Counters, not values** — logging uses pattern names + counts only.
  Raw matched values never enter log records (per
  ``sensitive-data.instructions.md``).

Public API (stable — reused by Phase 2 pull unchanged):

* :class:`ModerationVerdict` — enum: ``allow`` / ``redact`` / ``refuse``.
* :class:`ModerationResult` — dataclass returned by :func:`moderate`.
* :func:`moderate` — the main entry point.
* :func:`anonymize_pii` — PII scrubber + neutral-value replacement
  (exposed for unit testing; callers should use :func:`moderate`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from vesma.models import NO_FEDERATE_TAG
from vesma.secrets_detector import (
    detect_secrets,
    findings_by_pattern,
    redact_content,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PII_TYPES",
    "MappingTable",
    "ModerationResult",
    "ModerationVerdict",
    "anonymize_pii",
    "moderate",
    "neutral_value_for",
]

# ── PII pattern catalogue ──────────────────────────────────────────────────────
#
# Regex-based, deterministic, auditable. NOT LLM. Each entry:
# (pii_type, compiled regex, description). Order matters for first-match
# precedence when two patterns could match the same start offset.
#
# All test fixtures MUST use RFC-reserved values (see
# ``sensitive-data.instructions.md``): 192.0.2.0/24 (RFC 5737),
# user@example.com (RFC 5322), example.invalid (RFC 6761).

_PIIType = tuple[str, re.Pattern[str], str]

_PII_PATTERNS: Final[list[_PIIType]] = [
    # Email — RFC 5322 simplified. Match before hostnames/IPs so
    # ``user@example.invalid`` is treated as an email, not a hostname.
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        "Email address",
    ),
    # IPv4 — four dot-separated octets. We accept 1-3 digit octets and
    # rely on neutral replacement rather than strict 0-255 validation:
    # the goal is anonymization, not validation. A value like 999.0.0.1
    # is not a valid IPv4 address but is unlikely in prose; if it
    # appears, anonymizing it is harmless.
    (
        "ipv4",
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        "IPv4 address",
    ),
    # IPv6 — pragmatic simplified pattern. Full RFC 4291 IPv6 regex is
    # complex (mixed hex/dec, ``::`` compression, zone IDs); we match
    # the common cases: full 8-group hex, and ``::``-compressed forms.
    # False positives on hex-like words are acceptable because the
    # replacement is a neutral documentation value.
    (
        "ipv6",
        re.compile(
            r"\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}\b"
            r"|\b(?:[0-9A-Fa-f]{1,4}:)+:[0-9A-Fa-f]{1,4}\b"
            r"|\b[0-9A-Fa-f]{1,4}::(?:[0-9A-Fa-f]{1,4}:)*[0-9A-Fa-f]{1,4}\b"
        ),
        "IPv6 address",
    ),
    # Hostname — FQDN with at least one dot and a TLD of 2+ alpha chars.
    # Exclude the RFC-reserved ``example.invalid`` / ``example.com`` /
    # ``example.net`` / ``example.org`` / ``example.edu`` so we don't
    # re-anonymize our own neutral replacements. Negative lookahead for
    # the ``example.`` prefix (case-insensitive).
    (
        "hostname",
        re.compile(
            r"\b(?!example\.(?:invalid|com|net|org|edu)\b)"
            r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"
            r"[A-Za-z]{2,}\b"
        ),
        "Hostname / FQDN",
    ),
    # Unix file path — absolute path with at least one directory segment.
    # Leading ``/`` followed by path chars. Catches ``/etc/passwd``,
    # ``/home/user/...``, ``/var/log/...``. Does NOT catch relative
    # paths (``./foo``) — too ambiguous in prose.
    (
        "unix-path",
        re.compile(r"\b/(?:[A-Za-z0-9._\-]+/)+[A-Za-z0-9._\-]+\b"),
        "Unix absolute file path",
    ),
    # Windows drive-letter path — e.g. ``C:\Users\admin\secret.txt``.
    # Drive letter (A-Z), colon, backslash, then ≥1 directory segments
    # and a final name component. Does NOT catch relative Windows
    # paths (``foo\bar``) — too ambiguous in prose and risks matching
    # escaped chars in JSON/regex literals.
    (
        "win-path",
        re.compile(r"[A-Z]:\\(?:[A-Za-z0-9._\-]+\\)+[A-Za-z0-9._\-]+"),
        "Windows drive-letter file path",
    ),
    # UNC path — e.g. ``\\server\share\file``. Two leading backslashes,
    # server name, then ≥1 share/path segments. Catches DFS/SMB shares
    # common in enterprise Windows environments. The final segment
    # excludes ``.`` to avoid capturing trailing sentence punctuation.
    (
        "unc-path",
        re.compile(r"\\\\[A-Za-z0-9._\-]+\\(?:[A-Za-z0-9_\-]+\\)*[A-Za-z0-9_\-]+"),
        "UNC (server share) file path",
    ),
]

#: List of PII type names (for introspection / docs).
PII_TYPES: Final[tuple[str, ...]] = tuple(name for name, _rx, _d in _PII_PATTERNS)

# ── Neutral-value replacements (RFC-reserved) ─────────────────────────────────
#
# Each PII type maps to a fixed RFC-reserved replacement. The replacement
# value is fixed per type so the sanitized output is deterministic given
# the input. We do NOT rotate replacements across runs — the same email
# always becomes ``user@example.com``. This makes the sanitized output
# reproducible and avoids a stateful mapping store that would itself be a
# leak surface.
#
# RFC references:
#   - RFC 5737 — IPv4 documentation blocks: 192.0.2.0/24,
#     198.51.100.0/24, 203.0.113.0/24.
#   - RFC 5322 — ``user@example.com`` is the canonical example mailbox.
#   - RFC 6761 — ``.invalid`` TLD is reserved for documentation; it
#     will never resolve.
#   - RFC 3849 — IPv6 documentation prefix 2001:db8::/32.

_NEUTRAL_VALUES: Final[dict[str, str]] = {
    "email": "user@example.com",
    "ipv4": "192.0.2.1",
    "ipv6": "2001:db8::1",
    "hostname": "example.invalid",
    "unix-path": "/example/path/to/file",
    "win-path": r"C:\example\path\to\file",
    "unc-path": r"\\example.invalid\share\file",
}

#: RFC 5737 documentation IPv4 block (used in tests + docs).
DOC_IPV4_BLOCK: Final[str] = "192.0.2.0/24"


def neutral_value_for(pii_type: str) -> str:
    """Return the RFC-reserved neutral replacement for ``pii_type``.

    Raises ``KeyError`` for an unknown type. Callers should use the
    values from :data:`PII_TYPES`.
    """
    return _NEUTRAL_VALUES[pii_type]


# ── Mapping table (per-run, in-memory, TTL) ───────────────────────────────────


@dataclass
class _MappingEntry:
    """One entry in the per-run mapping table.

    The mapping is in-memory only. It is NEVER persisted to the DB or
    written to logs (the mapping itself is a leak surface — contract
    §2.2). The TTL is config-driven (default 24h); after expiry the
    entry is dropped and a fresh replacement is issued on the next
    moderation run.
    """

    replacement: str
    expires_at: datetime


class MappingTable:
    """Per-run mapping of original PII values → neutral replacements.

    The mapping is **deterministic within a run**: the same original
    value always maps to the same neutral replacement *within one
    ``MappingTable`` instance*. Across runs (or after TTL expiry), a
    fresh table is used. The table is **never replicated** — it lives
    only in the moderating process's memory.

    The table is keyed by ``(pii_type, original_value)``. The replacement
    is the RFC-reserved neutral value for that type (see
    :data:`_NEUTRAL_VALUES`). Because the replacement is fixed per type,
    the mapping is actually a *set* of seen values — but we keep the
    dict shape so the API can evolve (e.g. to per-value rotation in a
    future ``trusted`` mode without breaking callers).
    """

    def __init__(self, *, ttl_hours: int = 24, now: datetime | None = None) -> None:
        self._ttl = timedelta(hours=ttl_hours)
        self._now = now or datetime.now(UTC)
        self._entries: dict[tuple[str, str], _MappingEntry] = {}

    def get_or_create(self, pii_type: str, original: str) -> str:
        """Return the replacement for ``original``, creating it if new.

        Expired entries are evicted on access (lazy TTL). The replacement
        is the RFC-reserved neutral value for ``pii_type``.
        """
        key = (pii_type, original)
        entry = self._entries.get(key)
        if entry is not None and entry.expires_at > self._now:
            return entry.replacement
        # New or expired → (re)create. Replacement is fixed per type.
        replacement = _NEUTRAL_VALUES[pii_type]
        self._entries[key] = _MappingEntry(
            replacement=replacement,
            expires_at=self._now + self._ttl,
        )
        return replacement

    def __len__(self) -> int:
        return len(self._entries)

    def evict_expired(self) -> int:
        """Drop expired entries. Returns the count evicted (for tests)."""
        before = len(self._entries)
        self._entries = {k: v for k, v in self._entries.items() if v.expires_at > self._now}
        return before - len(self._entries)


# ── Verdict + result ──────────────────────────────────────────────────────────


class ModerationVerdict(StrEnum):
    """Outcome of moderating one record.

    * ``allow`` — content is clean; pass through unchanged.
    * ``redact`` — content had secrets and/or PII; pass the sanitized
      version (secrets redacted, PII anonymized to RFC-reserved values).
    * ``refuse`` — content cannot be shared even after redaction (e.g.
      the record carries ``mnemos:no-federate``, or the content is
      entirely secret/PII with no useful remainder).
    """

    ALLOW = "allow"
    REDACT = "redact"
    REFUSE = "refuse"


@dataclass(frozen=True, slots=True)
class ModerationResult:
    """Outcome of :func:`moderate` for one record.

    ``stats`` carries counters only — no raw values, no mapping. The
    per-run mapping table (orig → replacement) is **NOT** in the result;
    it is a leak surface and is discarded after the call returns.

    Stats keys:
        - ``secrets_redacted`` (int) — number of secret findings redacted.
        - ``pii_anonymized`` (int) — number of PII entities anonymized.
        - ``verdict_code`` (int) — numeric verdict (0=allow, 1=redact,
          2=refuse) for log/metric pipelines that prefer ints.
    """

    verdict: ModerationVerdict
    sanitized_content: str
    stats: dict[str, int] = field(default_factory=dict)


# Verdict code mapping for stats (int for log/metric pipelines).
_VERDICT_CODE: Final[dict[ModerationVerdict, int]] = {
    ModerationVerdict.ALLOW: 0,
    ModerationVerdict.REDACT: 1,
    ModerationVerdict.REFUSE: 2,
}


# ── PII scrubber + neutral-value replacement ──────────────────────────────────


def _detect_pii(content: str) -> list[tuple[str, str, int, int]]:
    """Scan ``content`` for PII patterns.

    Returns a list of ``(pii_type, matched_value, start, end)`` tuples
    sorted by ``start`` ascending. Overlapping matches are resolved by
    first-match precedence (earliest start wins; ties broken by pattern
    declaration order — email before hostname before IP).

    Matches that are exactly one of our own neutral replacement values
    (see :data:`_NEUTRAL_VALUES`) are **skipped** — they are already safe
    and must not be re-anonymized. This keeps the pipeline idempotent on
    already-sanitized content.
    """
    if not content:
        return []
    neutral_set = set(_NEUTRAL_VALUES.values())
    # Pre-locate all neutral value occurrences in the content so we can
    # skip PII matches that are substrings of a neutral value (e.g. the
    # unix-path regex matching ``/path/to/file`` inside the neutral
    # ``/example/path/to/file``). Without this, already-sanitized content
    # is not idempotent.
    neutral_spans: list[tuple[int, int]] = []
    for nv in neutral_set:
        start = 0
        while True:
            idx = content.find(nv, start)
            if idx < 0:
                break
            neutral_spans.append((idx, idx + len(nv)))
            start = idx + 1
    raw: list[tuple[str, str, int, int]] = []
    for pii_type, regex, _desc in _PII_PATTERNS:
        for m in regex.finditer(content):
            matched = m.group(0)
            if matched in neutral_set:
                continue
            # Skip matches contained within a neutral value span.
            if any(ns <= m.start() and m.end() <= ne for ns, ne in neutral_spans):
                continue
            raw.append((pii_type, matched, m.start(), m.end()))
    if not raw:
        return []
    # Sort by start, then by pattern declaration order (stable on ties).
    type_order = {name: i for i, (name, _rx, _d) in enumerate(_PII_PATTERNS)}
    raw.sort(key=lambda t: (t[2], type_order[t[0]]))
    # De-overlap: drop matches fully contained in an earlier match.
    accepted: list[tuple[str, str, int, int]] = []
    last_end = -1
    for entry in raw:
        _t, _v, start, end = entry
        if start >= last_end:
            accepted.append(entry)
            last_end = end
        # else: fully contained → dropped
    return accepted


def anonymize_pii(
    content: str,
    *,
    mapping: MappingTable,
) -> tuple[str, int, dict[str, int], int]:
    """Anonymize PII in ``content`` using RFC-reserved neutral values.

    Args:
        content: The text to anonymize.
        mapping: The per-run mapping table (see :class:`MappingTable`).

    Returns:
        ``(anonymized_content, total_anonymized, per_type_counts,
        original_chars_replaced)``. ``per_type_counts`` is
        ``{pii_type: count}`` suitable for logging (no raw values).
        ``original_chars_replaced`` is the total count of original
        content characters that were replaced by neutral values — used
        by :func:`moderate` to compute the redacted fraction.
    """
    findings = _detect_pii(content)
    if not findings:
        return content, 0, {}, 0
    pieces: list[str] = []
    cursor = 0
    per_type: dict[str, int] = {}
    total = 0
    original_chars = 0
    for pii_type, matched, start, end in findings:
        pieces.append(content[cursor:start])
        replacement = mapping.get_or_create(pii_type, matched)
        pieces.append(replacement)
        cursor = end
        per_type[pii_type] = per_type.get(pii_type, 0) + 1
        total += 1
        original_chars += end - start
    pieces.append(content[cursor:])
    return "".join(pieces), total, per_type, original_chars


# ── Refuse criteria ───────────────────────────────────────────────────────────
#
# A record is ``refuse`` when it cannot be shared even after redaction.
# Criteria (contract §2.2):
#
# 1. The record carries ``mnemos:no-federate`` (defence-in-depth —
#    double-check, even though the export filter already excludes such
#    records). This is the explicit-owner-opt-out case.
# 2. The content is *entirely* secret/PII with no useful remainder —
#    i.e. the redacted fraction (sum of original chars replaced by
#    redaction or anonymization) / (original content length) exceeds
#    the configured threshold. We track the original span lengths of
#    each secret finding and PII finding, sum them, and divide by the
#    original content length. This is accurate regardless of whether
#    the replacement marker is shorter or longer than the original
#    (e.g. ``<REDACTED:aws-key>`` is 18 chars but the original AWS key
#    is 20 chars; the *original* 20 chars were redacted, not 18).


def _redacted_fraction(
    original_len: int,
    secret_chars_replaced: int,
    pii_chars_replaced: int,
) -> float:
    """Fraction of the original content that was redacted/anonymized.

    Returns 0.0 for empty original. Capped at 1.0 — if findings overlap
    (shouldn't happen after de-overlap, but defensive), we treat it as
    fully redacted.
    """
    if original_len <= 0:
        return 0.0
    replaced = secret_chars_replaced + pii_chars_replaced
    ratio = replaced / original_len
    return min(ratio, 1.0)


# ── Main entry point ──────────────────────────────────────────────────────────


def moderate(
    content: str,
    *,
    tags: list[str] | None = None,
    refuse_threshold: float = 0.8,
    mapping_ttl_hours: int = 24,
    now: datetime | None = None,
) -> ModerationResult:
    """Moderate one record's content for federation export/pull.

    This is the **main entry point** and the stable public API reused by
    Phase 2 (mediated pull) unchanged. Pipeline:

    1. If ``mnemos:no-federate`` is in ``tags`` → verdict ``refuse``
       (defence-in-depth double-check; the export filter should already
       have excluded the record, but moderation is the final guard).
    2. Run :func:`detect_secrets` on ``content``. If secrets are found,
       redact them with :func:`redact_content` (reused from #86 — no
       duplication).
    3. Run :func:`anonymize_pii` on the (possibly redacted) content.
       PII is replaced with RFC-reserved neutral values via the per-run
       mapping table.
    4. Decide the verdict:
       - If ``mnemos:no-federate`` in tags → ``refuse``.
       - Else if the redacted fraction exceeds ``refuse_threshold``
         (default 0.8 = 80%) → ``refuse`` (content is entirely
         secret/PII with no useful remainder).
       - Else if any secrets or PII were found → ``redact``.
       - Else → ``allow``.

    Args:
        content: The record content to moderate. Empty → verdict
            ``allow`` with empty sanitized content.
        tags: The record's tags. Used for the ``mnemos:no-federate``
            double-check. ``None`` is treated as an empty list.
        refuse_threshold: Fraction of content that must be redacted/
            anonymized to trigger ``refuse`` (default 0.8). Config-driven
            via ``federation.moderation_refuse_threshold``.
        mapping_ttl_hours: TTL for the per-run mapping table (default 24h,
            config-driven via ``federation.moderation_mapping_ttl_hours``).
        now: Optional override for "current time" (for TTL tests). If
            ``None``, uses ``datetime.now(UTC)``.

    Returns:
        :class:`ModerationResult` with ``verdict``, ``sanitized_content``,
        and ``stats`` (counters only: ``secrets_redacted``,
        ``pii_anonymized``, ``verdict_code``). The mapping table is
        **NOT** in the result — it is a leak surface and is discarded.

    Logging:
        A single INFO line per call with counters only — never raw
        values, never the mapping. Example::

            moderation: redacted 2 secrets, anonymized 3 entities, verdict=redact
    """
    tags = tags or []
    mapping = MappingTable(ttl_hours=mapping_ttl_hours, now=now)

    # ── Criterion 1: explicit no-federate tag → refuse ──────────────────
    if NO_FEDERATE_TAG in tags:
        logger.info(
            "moderation: refused record (mnemos:no-federate tag present) — "
            "secrets=0, pii=0, verdict=refuse"
        )
        return ModerationResult(
            verdict=ModerationVerdict.REFUSE,
            sanitized_content="",
            stats={
                "secrets_redacted": 0,
                "pii_anonymized": 0,
                "verdict_code": _VERDICT_CODE[ModerationVerdict.REFUSE],
            },
        )

    if not content:
        return ModerationResult(
            verdict=ModerationVerdict.ALLOW,
            sanitized_content="",
            stats={
                "secrets_redacted": 0,
                "pii_anonymized": 0,
                "verdict_code": _VERDICT_CODE[ModerationVerdict.ALLOW],
            },
        )

    original_len = len(content)

    # ── Stage 1: secrets detector (REUSE #86) ───────────────────────────
    secret_findings = detect_secrets(content)
    redacted = redact_content(content, secret_findings) if secret_findings else content
    secrets_count = len(secret_findings)
    secret_chars_replaced = sum(f.end - f.start for f in secret_findings)
    secret_counts_by_pattern = findings_by_pattern(secret_findings)

    # ── Stage 2: PII scrubber + neutral-value replacement ─────────────
    anonymized, pii_count, _pii_counts_by_type, pii_chars_replaced = anonymize_pii(
        redacted, mapping=mapping
    )

    # ── Criterion 2: redacted fraction exceeds threshold → refuse ──────
    if (secrets_count > 0 or pii_count > 0) and _redacted_fraction(
        original_len, secret_chars_replaced, pii_chars_replaced
    ) > refuse_threshold:
        logger.info(
            "moderation: refused record (redacted fraction exceeds threshold) — "
            "secrets=%d, pii=%d, verdict=refuse",
            secrets_count,
            pii_count,
        )
        return ModerationResult(
            verdict=ModerationVerdict.REFUSE,
            sanitized_content="",
            stats={
                "secrets_redacted": secrets_count,
                "pii_anonymized": pii_count,
                "verdict_code": _VERDICT_CODE[ModerationVerdict.REFUSE],
            },
        )

    # ── Verdict ─────────────────────────────────────────────────────────
    if secrets_count > 0 or pii_count > 0:
        verdict = ModerationVerdict.REDACT
    else:
        verdict = ModerationVerdict.ALLOW

    logger.info(
        "moderation: redacted %d secrets, anonymized %d entities, verdict=%s, secret_patterns=%s",
        secrets_count,
        pii_count,
        verdict.value,
        secret_counts_by_pattern,
    )

    return ModerationResult(
        verdict=verdict,
        sanitized_content=anonymized,
        stats={
            "secrets_redacted": secrets_count,
            "pii_anonymized": pii_count,
            "verdict_code": _VERDICT_CODE[verdict],
        },
    )
