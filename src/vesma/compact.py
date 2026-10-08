"""Compact exchange format for Vesma federation (Phase 0, issue #85 part 2a).

Implements the ``vesmaro.federation.v1`` compact record format (ArchCom
2026-07-17 federation contract §2.3). The compact format is a list of
*published summaries* — not raw JSON dumps of memory content. Each
record carries the essence of a memory (≤500-char summary, key points,
tags, provenance) so the receiving instance can ingest it without
re-reading the full content. Token savings ~10x vs raw content.

The builder runs each memory through the moderation pipeline
(:func:`vesma.moderation.moderate`, #85 Part 1) *first*:

* ``refuse`` → the record is excluded (function returns ``None`` / the
  record is skipped at the payload level).
* ``redact`` → the sanitized content (secrets redacted, PII anonymized
  to RFC-reserved values) is used as the source of ``summary`` and
  ``key_points``.
* ``allow`` → the original content is used as-is.

This is Layer 3 of the federation defence-in-depth (contract §2.2.1):

1. Layer 1 — write-path ``mnemos:no-federate`` auto-tag (#86).
2. Layer 2 — background scanner (#89, future).
3. Layer 3 — moderation pipeline on export (this module, #85 Part 2a).

Public API:

* :data:`COMPACT_SCHEMA` — schema version string (``vesmaro.federation.v1``).
* :class:`CompactRecord` — one compact exchange record.
* :func:`build_compact_record` — build a single record from one
  :class:`~vesma.models.Memory` (runs moderation first; returns
  ``None`` when moderation refuses).
* :func:`build_compact_payload` — aggregate builder: run moderation on
  a list of memories, skip refused, return
  ``{"schema": ..., "records": [...], "stats": {...}}``.
* :func:`derive_record_type` — map a memory's subtype tag
  to the compact ``type`` field.
* :func:`summarize_content` — truncate content to ≤500 chars at a word
  boundary with ``...``.
* :func:`extract_key_points` — heuristic bullet/numbered list
  extraction from content.

S2 meta-mirror substrate (ADR-0021 Q10.3, archcom 2026-09-20):

* :data:`METADATA_SCHEMA` — metadata-record schema version.
* :class:`FederationIndexEntry` — one ``federation_index`` row (the
  Python-side ``MetadataRecord`` + storage-side ``origin_peer`` /
  ``content_state`` / ``received_at``).
* :func:`build_metadata_entry` — build an index-only entry from a local
  memory (``None`` = excluded: no-federate tag, moderation refuse, or
  no project).
* :func:`canonical_metadata_timestamp` — import-boundary ISO-8601 →
  canonical-UTC normaliser (LWW anti-poisoning, review blocker 2).
* :func:`title_matches_blocklist` — Q10.9 title-regex gate, applied at
  BOTH the serve and import gates.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field, field_validator

from vesma.models import NO_FEDERATE_TAG, Memory
from vesma.moderation import ModerationResult, ModerationVerdict, moderate

logger = logging.getLogger(__name__)

__all__ = [
    "COMPACT_SCHEMA",
    "CONTENT_STATES",
    "CONTENT_STATE_AVAILABLE",
    "CONTENT_STATE_TOMBSTONED",
    "MAX_KEY_POINTS",
    "MAX_SUMMARY_LEN",
    "MAX_TITLE_LEN",
    "METADATA_SCHEMA",
    "TIMESTAMP_FUTURE_SLACK",
    "CompactRecord",
    "FederationIndexEntry",
    "build_compact_payload",
    "build_compact_record",
    "build_metadata_entry",
    "canonical_metadata_timestamp",
    "derive_record_type",
    "extract_key_points",
    "summarize_content",
    "title_matches_blocklist",
]

#: Compact format schema version (forward-compat marker, contract §2.3).
COMPACT_SCHEMA: str = "vesmaro.federation.v1"

#: Maximum ``title`` length (chars). Contract §2.3 — short headline.
MAX_TITLE_LEN: int = 256

#: Maximum ``summary`` length (chars). Contract §2.3: "≤500 chars".
MAX_SUMMARY_LEN: int = 500

#: Maximum number of ``key_points`` per record (contract §2.3 — "Max 5").
MAX_KEY_POINTS: int = 5

# ── Type mapping ──────────────────────────────────────────────────────────────
#
# Map a memory's subtype tag to the compact record ``type``
# field. The compact format's type vocabulary (contract §2.3) is a
# subset of the vesma tag subtypes. ``vesma:legacy`` and
# ``vesma:synthesized`` (pipeline artefacts) fall back to ``session``
# (sensible default — they are not categorical decisions). The
# ``no-federate`` (either spelling) is NOT mapped — records with that tag are
# refused by moderation and never reach the builder.

_TYPE_MAP: dict[str, str] = {
    "decision": "decision",
    "learning": "learning",
    "bug-pattern": "bug-pattern",
    "rule": "rule",
    "open-question": "open-question",
    "checkpoint": "checkpoint",
    "session": "session",
    # Pipeline artefacts → session (sensible default, contract §2.3).
    "legacy": "session",
    "synthesized": "session",
}

#: Default record type when no known subtype tag is present.
_DEFAULT_TYPE: str = "session"

# ── Heuristics for summary + key points ──────────────────────────────────────

#: Bullet line regex — matches ``- `` / ``* `` / ``• `` at line start.
_BULLET_RE = re.compile(r"^\s*[-*•]\s+(.+)$", re.MULTILINE)

#: Numbered list line regex — matches ``1. `` / ``2. `` at line start.
_NUMBERED_RE = re.compile(r"^\s*\d+\.\s+(.+)$", re.MULTILINE)


# ── Compact record ────────────────────────────────────────────────────────────


class CompactRecord(BaseModel):
    """One compact exchange record (contract §2.3).

    Fields:
        id: ``fed:<source_agent>:<local_uuid>`` — globally unique and
            idempotent on import (the receiving side keys on this id).
        type: Compact format type vocabulary — derived from the
            memory's subtype tag (see :data:`_TYPE_MAP`).
        title: Short headline (≤ :data:`MAX_TITLE_LEN` chars).
        summary: ≤ :data:`MAX_SUMMARY_LEN` chars — the essence of the
            record, not raw content.
        key_points: List of strings the reader should take away
            (≤ :data:`MAX_KEY_POINTS` entries, may be empty).
        tags: Copy of the memory's tags minus ``mnemos:no-federate``.
        source_agent: Slug of the agent that authored the memory
            (from the ``agent:<slug>`` tag, prefix stripped).
        timestamp: ISO 8601 UTC string of ``memory.created_at``.
    """

    id: str
    type: str
    title: str
    summary: str = Field(default="")
    key_points: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source_agent: str = Field(default="")
    timestamp: str = Field(default="")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _truncate_at_word_boundary(text: str, max_len: int) -> str:
    """Truncate ``text`` to ≤ ``max_len`` chars at a word boundary.

    If the text fits, return it unchanged. Otherwise cut at the last
    whitespace boundary within ``max_len - 3`` chars and append ``...``.
    If there is no whitespace boundary (a single very long word),
    hard-cut at ``max_len - 3`` and append ``...``. The returned string
    is always ≤ ``max_len`` chars.
    """
    if len(text) <= max_len:
        return text
    cut = max_len - 3
    if cut <= 0:
        return "..."
    # Find the last whitespace boundary within the cut window.
    boundary = text.rfind(" ", 0, cut)
    if boundary <= 0:
        # No whitespace in the window → hard cut.
        return text[:cut] + "..."
    return text[:boundary] + "..."


def summarize_content(content: str, *, max_len: int = MAX_SUMMARY_LEN) -> str:
    """Truncate ``content`` to ≤ ``max_len`` chars at a word boundary.

    If the content fits, return it unchanged. Otherwise cut at the last
    whitespace boundary ≤ ``max_len - 3`` chars and append ``...``.
    """
    return _truncate_at_word_boundary(content, max_len)


def _truncate_title(text: str) -> str:
    """Truncate ``text`` to ≤ :data:`MAX_TITLE_LEN` chars at a word boundary."""
    return _truncate_at_word_boundary(text, MAX_TITLE_LEN)


def extract_key_points(content: str, *, max_points: int = MAX_KEY_POINTS) -> list[str]:
    """Heuristically extract key points from ``content``.

    Strategy (contract §2.3 — "simple heuristic, don't over-engineer"):

    1. If the content has bullet (``- `` / ``* `` / ``• ``) lines,
       collect their text (stripped).
    2. Else if the content has numbered (``1. `` / ``2. ``) lines,
       collect their text (stripped).
    3. Else fall back to the first 1-2 sentences (split on ``. ``).

    Returns at most ``max_points`` entries. Each entry is a single
    string. Empty / whitespace-only entries are dropped.
    """
    if not content:
        return []
    points: list[str] = []
    # Bullets first.
    for m in _BULLET_RE.finditer(content):
        text = m.group(1).strip()
        if text:
            points.append(text)
        if len(points) >= max_points:
            return points
    if points:
        return points
    # Numbered lines.
    for m in _NUMBERED_RE.finditer(content):
        text = m.group(1).strip()
        if text:
            points.append(text)
        if len(points) >= max_points:
            return points
    if points:
        return points
    # Fallback: first 1-2 sentences (split on ". ").
    sentences = [s.strip() for s in content.split(". ") if s.strip()]
    if not sentences:
        return []
    out: list[str] = []
    for s in sentences[:2]:
        # The split drops the trailing period; re-add for readability
        # unless the sentence already ends with terminal punctuation.
        out.append(s if s.endswith((".", "!", "?")) else s + ".")
    return out


def derive_record_type(tags: list[str]) -> str:
    """Map a memory's subtype tag to the compact record type.

    Scans ``tags`` for a subtype-prefixed tag whose suffix is a known
    subtype (see :data:`_TYPE_MAP`) — both the canonical ``vesma:``
    spelling and the legacy ``mnemos:`` spelling (rows written by 5.x
    builds before the 6.0 mover re-slugs them). The first matching
    subtype wins. ``no-federate`` is explicitly skipped under either
    spelling (records with that tag are refused by moderation before
    reaching the builder). Returns :data:`_DEFAULT_TYPE` (``session``)
    when no known subtype tag is present.
    """
    for tag in tags:
        if tag.startswith("vesma:"):
            suffix = tag[len("vesma:") :]
        elif tag.startswith("mnemos:"):
            suffix = tag[len("mnemos:") :]
        else:
            continue
        if suffix == "no-federate":
            continue
        if suffix in _TYPE_MAP:
            return _TYPE_MAP[suffix]
    return _DEFAULT_TYPE


def _memory_timestamp(created_at: datetime) -> str:
    """Return ``created_at`` as an ISO 8601 UTC string.

    Naive datetimes are assumed to be UTC (defensive — :class:`Memory`
    defaults to ``datetime.now(UTC)``). The ``Z`` suffix is used when the
    offset is exactly UTC for readability (contract §2.3 example uses
    ``2026-06-20T14:30:00Z``).
    """
    ts = created_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    ts_utc = ts.astimezone(UTC)
    iso = ts_utc.isoformat()
    # Normalise ``+00:00`` to ``Z`` for the canonical UTC form.
    if iso.endswith("+00:00"):
        iso = iso[:-6] + "Z"
    return iso


def _derive_title(memory: Memory, *, content_source: str) -> str:
    """Pick the compact record title.

    Preference (contract §2.3):

    1. ``memory.title`` if set (non-empty).
    2. First line of the (sanitized) content, stripped.
    3. ``memory.auto_title()`` (which itself falls back to "Untitled").

    The result is truncated to :data:`MAX_TITLE_LEN` chars at a word
    boundary with ``...``.
    """
    if memory.title:
        return _truncate_title(memory.title)
    first_line = content_source.strip().split("\n", 1)[0].lstrip("# ").strip()
    if first_line:
        return _truncate_title(first_line)
    return _truncate_title(memory.auto_title())


# ── Per-record builder ────────────────────────────────────────────────────────


def build_compact_record(
    memory: Memory,
    *,
    source_agent: str,
    refuse_threshold: float = 0.8,
    moderation_result: ModerationResult | None = None,
) -> CompactRecord | None:
    """Build a compact exchange record from a :class:`Memory`.

    Runs :func:`vesma.moderation.moderate` on ``memory.content`` first
    (Layer 3 defence-in-depth, #85 Part 1):

    * ``refuse`` → return ``None`` (caller skips this record).
    * ``redact`` → use ``result.sanitized_content`` as the source for
      ``summary`` and ``key_points``.
    * ``allow`` → use ``memory.content`` as-is.

    Args:
        memory: The source memory to compact.
        source_agent: Slug of the agent that authored the memory. Used
            as the ``source_agent`` field and as the prefix of the
            record ``id`` (``fed:<source_agent>:<memory.id>``). Per
            contract §2.3 this is the provenance — it should come from
            the memory's ``agent:<slug>`` tag, but the caller passes it
            explicitly so the receiving side can verify it against the
            tag (defence against a forged tag).

            .. note::
                The ``source_agent`` slug is sanitised (lowercased,
                non-``[a-z0-9_-]`` replaced with ``-``) and the
                sanitisation is **not injective** — two distinct slugs
                like ``Foo.Bar`` and ``foo-bar`` collapse to the same
                string. The ``fed:`` prefix uniqueness therefore relies
                on ``memory.id`` (a UUID) being globally unique, NOT
                on ``source_agent``. Re-import idempotency skips by
                ``record.id``, so a cross-payload collision on the
                prefix would silently drop the second record — the
                ``memory.id`` UUID is the load-bearing uniqueness key.
        refuse_threshold: Fraction of content that must be redacted/
            anonymized to trigger ``refuse`` (default 0.8 = 80%).
            Forwarded to :func:`moderate` when ``moderation_result`` is
            ``None``; ignored otherwise.
        moderation_result: Optional pre-computed moderation result. When
            provided, moderation is NOT re-run — this lets
            :func:`build_compact_payload` moderate once per memory and
            thread the result through to both the record builder and
            the stats aggregator (avoids a wasteful double moderation
            call). When ``None``, moderation is run inside this call.

    Returns:
        A :class:`CompactRecord`, or ``None`` when moderation refuses
        the record (the caller should skip ``None`` results).
    """
    result = moderation_result or moderate(
        memory.content, tags=memory.tags, refuse_threshold=refuse_threshold
    )

    if result.verdict == ModerationVerdict.REFUSE:
        logger.info(
            "compact: refused record id=%s (moderation verdict=refuse) — secrets=%d, pii=%d",
            memory.id,
            result.stats.get("secrets_redacted", 0),
            result.stats.get("pii_anonymized", 0),
        )
        return None

    # ``redact`` → use sanitized content; ``allow`` → use original.
    content_source = (
        result.sanitized_content if result.verdict == ModerationVerdict.REDACT else memory.content
    )

    summary = summarize_content(content_source)
    key_points = extract_key_points(content_source)
    record_type = derive_record_type(memory.tags)

    # Tags: keep project/agent/subtype tags, defensively drop
    # ``mnemos:no-federate`` (moderation should have refused such records
    # already, but strip anyway — belt and braces).
    tags = [t for t in memory.tags if t != NO_FEDERATE_TAG]

    timestamp = _memory_timestamp(memory.created_at)
    title = _derive_title(memory, content_source=content_source)

    return CompactRecord(
        id=f"fed:{source_agent}:{memory.id}",
        type=record_type,
        title=title,
        summary=summary,
        key_points=key_points,
        tags=tags,
        source_agent=source_agent,
        timestamp=timestamp,
    )


# ── Payload builder ──────────────────────────────────────────────────────────


def build_compact_payload(
    memories: list[Memory],
    *,
    source_agent: str,
    refuse_threshold: float = 0.8,
) -> dict[str, Any]:
    """Build the full compact exchange payload from a list of memories.

    Runs :func:`moderate` on each memory, skips refused records, and
    returns the ``vesmaro.federation.v1`` payload:

    .. code-block:: json

        {
          "schema": "vesmaro.federation.v1",
          "records": [ {CompactRecord.dict()}, ... ],
          "stats": {
            "total": <int>,
            "exported": <int>,
            "refused": <int>,
            "secrets_redacted": <int>,
            "pii_anonymized": <int>
          }
        }

    ``stats`` carries counters only — no raw values, no mappings (the
    per-run mapping table is a leak surface, see
    :mod:`vesma.moderation`).

    Args:
        memories: The source memories to compact.
        source_agent: Slug of the agent that authored the memories
            (forwarded to :func:`build_compact_record`).
        refuse_threshold: Fraction of content that must be redacted to
            trigger ``refuse`` (default 0.8, forwarded to
            :func:`moderate`).

    Returns:
        The compact payload dict, ready for JSON serialisation.
    """
    records: list[dict[str, Any]] = []
    total = len(memories)
    refused = 0
    secrets_redacted = 0
    pii_anonymized = 0

    for memory in memories:
        # Moderate once per memory and thread the result through to both
        # the record builder and the stats aggregator. This avoids the
        # wasteful double-moderation call (moderation is deterministic
        # but not free — secrets detector + PII scrubber + mapping table).
        result = moderate(
            memory.content,
            tags=memory.tags,
            refuse_threshold=refuse_threshold,
        )
        record = build_compact_record(
            memory,
            source_agent=source_agent,
            refuse_threshold=refuse_threshold,
            moderation_result=result,
        )
        if record is None:
            refused += 1
            continue
        secrets_redacted += result.stats.get("secrets_redacted", 0)
        pii_anonymized += result.stats.get("pii_anonymized", 0)
        records.append(record.model_dump())

    exported = len(records)
    return {
        "schema": COMPACT_SCHEMA,
        "records": records,
        "stats": {
            "total": total,
            "exported": exported,
            "refused": refused,
            "secrets_redacted": secrets_redacted,
            "pii_anonymized": pii_anonymized,
        },
    }


# ── S2 federation index entry (ADR-0021 Q10.3, archcom 2026-09-20) ───────────
#
# The meta-mirror substrate: an index-only row describing a record that
# exists SOMEWHERE in the mesh — either a local memory (origin_peer="self")
# or a record mirrored from a peer (origin_peer=<peer A2A id>). It carries
# NO content: no summary, no key points (a record's existence is itself an
# inference surface — position paper §4, session-2 design). Rows live in
# the SQLite ``federation_index`` table, OUTSIDE ``vesma_search``.

#: Metadata-record schema version — mirrors
#: ``federation.proto::MetadataRecord.schema_version`` (distinct from
#: :data:`COMPACT_SCHEMA` so the metadata-sync protocol evolves
#: independently from the full-record pull protocol).
METADATA_SCHEMA: str = "mnemos.federation.metadata.v1"

#: ``content_state`` vocabulary (paper §4, additive field Q10.3):
#: mirrors age out pointers without learning why; no deletion reason
#: crosses servers. Tombstoned rows are kept (they suppress re-import)
#: but are not advertised as available content.
CONTENT_STATE_AVAILABLE: str = "available"
CONTENT_STATE_TOMBSTONED: str = "tombstoned"

#: Allowed ``content_state`` values.
CONTENT_STATES: frozenset[str] = frozenset({CONTENT_STATE_AVAILABLE, CONTENT_STATE_TOMBSTONED})

#: Canonical storage form for index timestamps: fixed-width UTC with a
#: ``Z`` suffix. Fixed width (always 6 microsecond digits) makes the
#: LEXICOGRAPHIC string order equal the chronological order, which is
#: what the store's LWW comparison and the ``since`` watermark rely on
#: (review blocker 2: raw wire forms like ``+03:00`` offsets or a
#: missing fraction made lexicographic compare lie).
METADATA_TS_FORMAT: str = "%Y-%m-%dT%H:%M:%S.%fZ"

#: How far ahead of local ``now`` an imported timestamp may sit before
#: it is rejected as future-dated (clock-skew slack between peers; a
#: record stamped beyond it would win LWW forever — review blocker 2).
TIMESTAMP_FUTURE_SLACK: timedelta = timedelta(minutes=5)


def canonical_metadata_timestamp(raw: str, *, now: datetime | None = None) -> str:
    """Parse an untrusted ISO-8601 timestamp into the canonical index form.

    Import-boundary normaliser (the wire is untrusted): parses ISO-8601
    with :meth:`datetime.datetime.fromisoformat` (after normalising a
    trailing ``Z``/``z`` to ``+00:00``), converts to UTC and formats as
    :data:`METADATA_TS_FORMAT` so that every stored timestamp compares
    correctly as a plain string. A naive (offset-less) timestamp is
    assumed UTC — the same defensive posture as
    :func:`_memory_timestamp`.

    Args:
        raw: The wire ``timestamp`` field, verbatim.
        now: Reference ``now`` for the future-slack gate (injectable for
            tests); defaults to ``datetime.now(UTC)``.

    Returns:
        The canonical UTC string, e.g. ``2026-09-01T07:00:00.000000Z``.

    Raises:
        ValueError: ``raw`` is empty or unparseable, or it sits further
            than :data:`TIMESTAMP_FUTURE_SLACK` in the future. The RPC
            layer counts these in ``rejected_by_gate``.
    """
    text = raw.strip()
    if not text:
        raise ValueError("empty timestamp")
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"unparseable timestamp {raw!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    reference = now if now is not None else datetime.now(UTC)
    if parsed > reference + TIMESTAMP_FUTURE_SLACK:
        raise ValueError(f"future timestamp {raw!r} beyond {TIMESTAMP_FUTURE_SLACK} slack")
    return parsed.astimezone(UTC).strftime(METADATA_TS_FORMAT)


class FederationIndexEntry(BaseModel):
    """One ``federation_index`` row — the Python-side MetadataRecord.

    Wire shape (``federation.proto::MetadataRecord``): ``id``, ``type``,
    ``title`` ≤ :data:`MAX_TITLE_LEN`, ``tags``, ``project``,
    ``source_agent``, ``source_peer`` (last-hop provenance for multi-hop
    dedup), ``timestamp``, ``schema_version``, plus the Q10.3 additive
    fields (wire 10/11, chairman ruling 2026-09-20):

    * ``origin_peer`` — where the content body lives. With >2 stores it
      diverges from ``source_peer``; conflating the two breaks lazy
      fetch. ``"self"`` = the responding core's local corpus (an
      importer re-stamps a foreign ""/"self" to the authenticated
      sender id).
    * ``content_state`` — ``available`` / ``tombstoned``.
    * ``received_at`` — ISO 8601 UTC, stamped by the STORE at upsert
      time (when THIS core first/most-recently received the row);
      storage-side only, never on the wire.

    Validation at this boundary (peers are untrusted): ``id`` is
    non-empty, ``title`` ≤ 256 chars, ``content_state`` is a known
    value. Anything else fails the Pydantic contract and the upsert
    skips the entry (fail-closed import gate).
    """

    id: str = Field(min_length=1, max_length=512)
    type: str = ""
    title: str = Field(default="", max_length=MAX_TITLE_LEN)
    tags: list[str] = Field(default_factory=list)
    project: str = ""
    source_agent: str = ""
    source_peer: str = ""
    origin_peer: str = "self"
    content_state: str = CONTENT_STATE_AVAILABLE
    timestamp: str = ""
    schema_version: str = METADATA_SCHEMA
    received_at: str = ""

    @field_validator("content_state")
    @classmethod
    def _content_state_known(cls, value: str) -> str:
        """Reject unknown ``content_state`` values (forward-compat fail-closed)."""
        if value not in CONTENT_STATES:
            raise ValueError(f"unknown content_state {value!r} (known: {sorted(CONTENT_STATES)})")
        return value


def build_metadata_entry(
    memory: Memory,
    *,
    origin_peer: str = "self",
    source_peer: str = "self",
    refuse_threshold: float = 0.8,
    moderation_result: ModerationResult | None = None,
) -> FederationIndexEntry | None:
    """Build an index-only :class:`FederationIndexEntry` from a local memory.

    The metadata leg of :func:`build_compact_record`: same id scheme
    (``fed:<source_agent>:<memory.id>``), same type/title derivation,
    same tag filtering — but NO summary, NO key points, NO content. The
    title is derived from the moderation-processed content source so the
    headline (itself an export surface, Q10.9) never leaks a secret that
    moderation would have redacted.

    Returns ``None`` — the record must NOT enter the index at all — when:

    * the memory carries ``mnemos:no-federate`` (Q10.9: the tag filters
      the INDEX, defence-in-depth before any fan-out), or
    * moderation refuses the content (verdict ``REFUSE``), or
    * the memory carries no ``project`` — a project-less row is dead
      weight locally and POISONS the import leg remotely (the importer
      cannot ACL an entry without a project and aborts the whole RPC,
      review nit 2).

    Args:
        memory: The local memory to advertise.
        origin_peer: Where the content body lives. ``"self"`` (default)
            for the local corpus; the value is stored in the index row.
        source_peer: Last-hop provenance on the wire record. Defaults to
            ``"self"`` for locally-originated rows.
        refuse_threshold: Moderation refuse threshold (same semantics as
            :func:`build_compact_record`).
        moderation_result: Optional pre-computed moderation result (same
            reuse pattern as :func:`build_compact_record` — moderate
            once, thread through both record builders).
    """
    if NO_FEDERATE_TAG in memory.tags:
        logger.info("compact: metadata entry excluded — no-federate tag (memory id=%s)", memory.id)
        return None
    if not memory.project:
        logger.info("compact: metadata entry excluded — no project (memory id=%s)", memory.id)
        return None
    result = moderation_result or moderate(
        memory.content, tags=memory.tags, refuse_threshold=refuse_threshold
    )
    if result.verdict == ModerationVerdict.REFUSE:
        logger.info(
            "compact: metadata entry excluded — moderation refuse (memory id=%s)", memory.id
        )
        return None
    content_source = (
        result.sanitized_content if result.verdict == ModerationVerdict.REDACT else memory.content
    )
    return FederationIndexEntry(
        id=f"fed:{memory.agent or 'unknown'}:{memory.id}",
        type=derive_record_type(memory.tags),
        title=_derive_title(memory, content_source=content_source),
        tags=[t for t in memory.tags if t != NO_FEDERATE_TAG],
        project=memory.project or "",
        source_agent=memory.agent or "",
        source_peer=source_peer,
        origin_peer=origin_peer,
        content_state=CONTENT_STATE_AVAILABLE,
        timestamp=_memory_timestamp(memory.created_at),
        schema_version=METADATA_SCHEMA,
        received_at="",  # stamped by the store at upsert time
    )


def title_matches_blocklist(title: str, patterns: Sequence[str]) -> bool:
    """Q10.9 title-regex gate: does ``title`` match any blocklist pattern?

    Used at BOTH gates (ADR-0021 ruling 9 — metadata distribution IS
    export): the SERVE path drops matching index rows from
    metadata-sync answers, the IMPORT path refuses matching entries at
    upsert. Patterns are Python ``re`` expressions matched with
    ``re.search`` (substring semantics, case-sensitive). An empty
    pattern list blocks nothing.

    Config validation (:attr:`vesma.config.FederationConfig.
    index_title_blocklist`) guarantees every pattern compiles; a pattern
    that somehow does not raises :class:`re.error` loudly here rather
    than being silently skipped (fail-closed on the operator's intent).
    """
    return any(re.search(pattern, title) for pattern in patterns)
