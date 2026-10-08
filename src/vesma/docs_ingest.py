"""ADR-0027 Phase 3 (epic #308) — docs-as-memory: document ingest.

Ingested documents are UNTRUSTED CONTENT (ADR-0027 invariant 8): the
document-ingest path chunks a fetched document into memory rows that
are BORN QUARANTINED — the ADR-0019 §5 terminal danger-lane state,
excluded from every issuance path by the single ``is_quarantined``
predicate — until the danger-sweep clears them.

The sweep (invariant 8's release mechanism): the ADR-0019 Phase A
danger detector (:func:`vesma.danger_detectors.detect` — the SAME
enumerated positive-signal set that powers the publication gate) runs
over every chunk's text and title at ingest-completion, all chunks of
a document together. Release semantics (PINNED, chunk-atomic):

* a chunk whose detection is CLEAN is RELEASED: ``pipeline_state=None``
  + ``status=PUBLISHED`` — an ordinary admissible memory row, with
  ``doc_swept_at`` recorded in its metadata for the audit trail;
* a chunk with a POSITIVE detector signal STAYS QUARANTINED with the
  detector's reason in ``quarantine_reason`` (the §5.1 discipline:
  quarantine is absorbing, release is explicit, both audited);
* a chunk with a SCANNER ERROR stays quarantined with reason
  ``detector-error`` (fail-closed in both directions — the ADR-0019
  ambiguity lane).

Chunk-atomic, not doc-atomic: a clean chunk of a document is released
EVEN IF a sibling chunk is flagged. The flagged chunk stays
quarantined and excluded; its clean siblings are ordinary rows. The
rationale: the danger detector's findings are per-chunk facts —
quarantining a clean chunk over a sibling's finding would punish
content with no positive signal and give the attacker a one-chunk
denial-of-service over a whole document. A document is never
HALF-RELEASED in the visibility sense: pre-sweep, no chunk is
admissible; post-sweep, exactly the clean chunks are.

Rejected/flagged content NEVER enters the recall corpus silently: the
born-quarantined start guarantees the row is invisible before the
sweep, and the sweep itself can only release a chunk the detector
cleared.

Issuance remains the last line (invariant 7): a RELEASED chunk that
carries a planted danger pattern (planted AFTER release —
post-sweep contamination) is refused AT ISSUANCE by the existing
``scan_issuance`` / ``scan_issuance_item`` machinery that every
content-echoing channel already runs. Document chunks are ordinary
memory rows post-release — no special casing; the repeat scan covers
them by construction. The pin lives in the Ф3 test suite.

Re-ingest / re-fragmentation (invariant 4): re-ingesting the SAME
``doc_id`` REPLACES the document's chunk rows and bumps the ccr_cache
doc-chunk version key IN THE SAME TRANSACTION
(:meth:`vesma.storage.sqlite_store.SQLiteStore.replace_doc_chunks`
— ``BEGIN IMMEDIATE``). HONEST SCOPE (review round P2-2, the
``graph_epoch`` posture the committee accepted for A1): the key is a
CONSUMER-FACING INVALIDATION COUNTER — it is bumped transactionally on
every re-fragmentation and readable by any assembly-cache consumer, but
NO in-repo consumer keys on it YET (today's read paths do not consult
it), so the "no window where the old cache serves post-re-chunk
assemblies" holds by construction of the CURRENT readers, not by an
enforced cache-bypass. The first consumer that caches doc-chunk-derived
assemblies MUST read this counter and treat a change as a full
invalidation; until then the counter is an exposed, bumped, test-pinned
contract — not a wired defence.

Boundary with the existing single-URL ingest: ``vesma_ingest_url``
(REST ``POST /ingest-url``, CLI ``vesma add --url``) keeps its
pre-Ф3 semantics UNTOUCHED — a fetched page saved as ONE memory row
through the ordinary visibility policy, no born-quarantine. The
document path is the SEPARATE ``ingest_document`` surface (MCP
``vesma_ingest_document``, REST ``POST /ingest-document``); the
existing tool is not retroactively quarantined.

Zero migration: chunk rows are ordinary ``memories`` rows carrying the
Ф0 doc-grouping metadata convention
(:func:`vesma.models.build_doc_grouping_metadata`); the cache version
key is a ``meta`` table row, not a schema change.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from vesma.danger_detectors import DetectionResult, detect
from vesma.models import (
    Memory,
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    PipelineState,
    build_doc_grouping_metadata,
)
from vesma.pipeline.refine import QUARANTINE_REASON_DETECTOR_ERROR

if TYPE_CHECKING:  # pragma: no cover — typing only
    from vesma.manager import MemoryManager

__all__ = [
    "BORN_QUARANTINE_REASON",
    "DOC_CHUNK_CACHE_VERSION_META_KEY",
    "DOC_SWEEP_REASON_DETECTOR_PREFIX",
    "DOC_SWEEP_STAMP_KEYS",
    "DOC_SWEEP_STAMP_METADATA_KEY",
    "DocChunk",
    "DocIngestResult",
    "chunk_document",
    "ingest_document",
    "sweep_document_chunks",
]

logger = logging.getLogger(__name__)

#: Why a freshly ingested doc chunk sits in the danger lane before the
#: sweep: the born-quarantine reason. Cause-neutral toward the content
#: (nothing fired yet) — it names the LIFECYCLE state, not a detector.
BORN_QUARANTINE_REASON: Final[str] = "doc-ingest-born-quarantine"

#: The ``meta`` key holding the doc-chunk ccr_cache version (invariant 4).
#: ONE global monotonic counter — the version is about WHICH chunk rows
#: exist for any doc_id, and a re-fragmentation of ANY document must
#: invalidate assemblies that may combine chunks across documents
#: (invariant 7's "combining clean records creates a new context" cuts
#: both ways: the assembly cache must never outlive the corpus shape).
#: HONEST SCOPE (review round P2-2, the ``graph_epoch`` posture the
#: committee accepted for A1): a CONSUMER-FACING INVALIDATION COUNTER —
#: bumped transactionally on every re-fragmentation, exposed through
#: ``MemoryManager.stats``, and every caching consumer MUST read it; no
#: in-repo consumer keys on it YET, so today the guarantee it carries is
#: the contract the counter exposes, not a wired cache-bypass.
DOC_CHUNK_CACHE_VERSION_META_KEY: Final[str] = "ccr_cache_doc_chunk_version"

#: Metadata stamp written on RELEASE (the explicit-release audit trail
#: required by the §5.1 discipline — release is explicit AND audited).
DOC_SWEEP_STAMP_METADATA_KEY: Final[str] = "doc_swept_at"

#: The server-minted doc-sweep stamp keys (the vesma #251
#: checkpoint-stamp discipline, review round P3-1): only the doc sweep
#: (:func:`sweep_document_chunks`) may write ``doc_swept_at`` — a client
#: forging the stamp on ``add``/``update`` would fabricate a release
#: audit trail. Every generic create/update path strips client-supplied
#: copies; ``MemoryManager.update`` merge-protects an existing stamp
#: (a client metadata dict cannot erase it either).
DOC_SWEEP_STAMP_KEYS: frozenset[str] = frozenset({DOC_SWEEP_STAMP_METADATA_KEY})

#: ``quarantine_reason`` prefix for swept-and-flagged chunks. The reason
#: carries the detector class codes (``doc-sweep:prompt-injection,secret``)
#: — operator-side forensics; agent-facing channels never see it (the
#: §5 cause-neutrality contract: the retraction render stays classless).
DOC_SWEEP_REASON_DETECTOR_PREFIX: Final[str] = "doc-sweep:"

#: Maximum chunks a single document may produce (a defensive bound on
#: untrusted content — a hostile "document" must not mint unbounded rows).
_MAX_CHUNKS_PER_DOC: Final[int] = 500

#: Soft cap on lines per chunk inside one heading section (the split
#: of a monster section so one chunk is not the whole document).
_MAX_SECTION_LINES: Final[int] = 120

#: Heading-line pattern for the structure-preserving split (markdown
#: ATX headings ``#``…``######``). Deliberately simple: no setext, no
#: HTML — the Ф0 heading_path convention records what the chunker saw.
_HEADING_RE: re.Pattern[str] = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True, slots=True)
class DocChunk:
    """One chunk of a document, ready to become a memory row."""

    content: str
    chunk_idx: int
    heading_path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DocIngestResult:
    """Outcome of one document ingest (all chunks of one doc_id).

    ``truncated`` (review round P3-2): True when the document produced
    more chunks than the per-document cap (``_MAX_CHUNKS_PER_DOC``) and
    only the first ``chunks_total`` were ingested. The ingest logs a
    WARNING in the same case — the cap is a defensive bound on
    untrusted content, never a silent content drop.
    """

    doc_id: str
    chunks_total: int
    released: int
    quarantined: int
    memory_ids: tuple[str, ...]
    reingest: bool
    cache_version: int
    truncated: bool = False


# ── Structure-preserving chunking ────────────────────────────────────────────


def chunk_document(text: str) -> list[DocChunk]:
    """Split ``text`` into heading-scoped chunks (Ф0 metadata semantics).

    Structure-preserving (ADR-0027 Phase 3): the split walks the lines,
    tracks the heading stack (``# H1`` → ``## H2`` nests), and cuts a
    new chunk at every heading; within one heading section a soft line
    cap splits long prose. Each chunk carries its ``chunk_idx`` (0-based,
    document order) and ``heading_path`` (the root→leaf heading chain —
    ``()`` for content before the first heading).

    Pure function: no store, no manager, no logging — deterministic for
    the same input (the invariant-4 cache-version discipline assumes
    chunking is a function of the document text).
    """
    if not text or not text.strip():
        return []
    chunks: list[DocChunk] = []
    heading_stack: list[tuple[int, str]] = []
    pending: list[str] = []
    pending_path: tuple[str, ...] = ()

    def emit(section_lines: list[str], path: tuple[str, ...]) -> None:
        """Emit one chunk (soft-capped) if the section has content."""
        section = "\n".join(section_lines).strip()
        if not section:
            return
        lines = section.splitlines()
        if len(lines) <= _MAX_SECTION_LINES:
            chunks.append(DocChunk(content=section, chunk_idx=len(chunks), heading_path=path))
            return
        for start in range(0, len(lines), _MAX_SECTION_LINES):
            piece = "\n".join(lines[start : start + _MAX_SECTION_LINES]).strip()
            if piece:
                chunks.append(DocChunk(content=piece, chunk_idx=len(chunks), heading_path=path))

    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            emit(pending, pending_path)
            pending = []
            level = len(m.group(1))
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, m.group(2)))
            pending_path = tuple(h for _, h in heading_stack)
        else:
            pending.append(line)
    emit(pending, pending_path)
    # Review round P3-2: the per-document chunk cap is enforced by the
    # CALLER (ingest_document) so the truncation is observable — a pure
    # function must not silently drop data the caller cannot see. The
    # cap itself stays (a hostile "document" must not mint unbounded
    # rows); what changed is that the caller now logs a warning and
    # carries ``truncated`` in the result.
    return chunks


# ── The danger sweep ─────────────────────────────────────────────────────────


def _detect_chunk(memory: Memory) -> DetectionResult:
    """Run the enumerated danger detectors over one chunk's projection+title."""
    return detect(memory.effective_content(), title=memory.auto_title())


def sweep_document_chunks(manager: MemoryManager, memory_ids: list[str]) -> dict[str, int]:
    """Run the danger sweep over the given doc-chunk rows (release/refuse).

    All chunks of ONE document are swept together at ingest-completion.
    Per chunk (the PINNED chunk-atomic semantics — see the module
    docstring):

    * detection clean → RELEASE: ``pipeline_state=None``,
      ``status=PUBLISHED`` (an ordinary admissible row), the sweep
      stamp ``doc_swept_at`` recorded in metadata (the explicit-release
      audit trail), vector embed upserted (the row is now recallable
      through the vector leg);
    * detection positive → STAYS QUARANTINED, ``quarantine_reason``
      rewritten to the detector class codes under the ``doc-sweep:``
      prefix (operator forensics; the row was already quarantined from
      birth — this is the sweep's verdict, an audited state change on
      the same lane);
    * scanner error → STAYS QUARANTINED with reason ``detector-error``
      (fail-closed, the ADR-0019 ambiguity lane — a chunk the scanner
      could not read is never released).

    The verdict log lines follow the ``publish gate:`` convention of the
    ADR-0019 Phase A audit (verdict + pattern names/counts only — raw
    values never enter the log, the danger-detector contract).

    Sweep authority guard: only rows still carrying the born-quarantine
    reason are sweepable — the sweep must never "release" a row that
    entered the danger lane by another path (ADR-0019 §5: the doc lane's
    release authority is this sweep and nothing else).

    Returns ``{"released": N, "quarantined": M}`` over the given rows.
    """
    released = 0
    quarantined = 0
    for memory_id in memory_ids:
        memory = manager.sqlite.get(memory_id)
        if memory is None:
            logger.warning("doc sweep: chunk %s missing at sweep time", memory_id[:8])
            continue
        if memory.pipeline_state != PipelineState.QUARANTINED:
            logger.warning(
                "doc sweep: chunk %s not in quarantine state (state=%s) — skipped",
                memory_id[:8],
                memory.pipeline_state,
            )
            continue
        if memory.quarantine_reason != BORN_QUARANTINE_REASON:
            logger.warning(
                "doc sweep: chunk %s carries a foreign quarantine reason (%s) — skipped",
                memory_id[:8],
                memory.quarantine_reason,
            )
            continue
        detection = _detect_chunk(memory)
        if detection.error is not None:
            manager.sqlite.update_fields(
                memory_id, quarantine_reason=QUARANTINE_REASON_DETECTOR_ERROR
            )
            quarantined += 1
            logger.warning(
                "doc sweep: %s verdict=refused reason=scanner-error error=%s "
                "— chunk stays quarantined",
                memory_id[:8],
                detection.error,
            )
            continue
        if detection.positive:
            reason = DOC_SWEEP_REASON_DETECTOR_PREFIX + ",".join(
                sorted({f.detector_class for f in detection.findings})
            )
            manager.sqlite.update_fields(memory_id, quarantine_reason=reason)
            quarantined += 1
            logger.warning(
                "doc sweep: %s verdict=refused reason=danger-detector "
                "classes=%s patterns=%s — raw values not logged",
                memory_id[:8],
                sorted({f.detector_class for f in detection.findings}),
                detection.patterns_by_class(),
            )
            continue
        # Clean → RELEASE. The explicit release, audited: state cleared,
        # status PUBLISHED (the sweep IS the chunk's publication gate —
        # the same Phase A detector the ordinary publication path uses),
        # quarantine_reason cleared (a released row is NOT a quarantined
        # row — a stale reason on an admissible row is forensics noise
        # and would leak the birth lane into operator reads), sweep
        # stamp in metadata, vector embed upserted.
        stamped = dict(memory.metadata)
        stamped[DOC_SWEEP_STAMP_METADATA_KEY] = datetime.now(UTC).isoformat()
        ok = manager.sqlite.update_fields(
            memory_id,
            pipeline_state=None,
            status=MemoryStatus.PUBLISHED,
            quarantine_reason=None,
            metadata=stamped,
        )
        if not ok:
            logger.warning("doc sweep: release write failed for %s", memory_id[:8])
            quarantined += 1
            continue
        released += 1
        logger.info("doc sweep: %s verdict=pass — chunk released", memory_id[:8])
        try:
            released_row = manager.sqlite.get(memory_id)
            if released_row is not None:
                manager.upsert_embedding(released_row)
        except Exception as exc:  # non-fatal — the vector sweeper heals
            logger.warning(
                "doc sweep: embed upsert failed for %s (sweeper will heal): %s",
                memory_id[:8],
                exc,
            )
    return {"released": released, "quarantined": quarantined}


# ── The document ingest ──────────────────────────────────────────────────────


def ingest_document(
    manager: MemoryManager,
    text: str,
    *,
    doc_id: str,
    title: str | None,
    tags: list[str],
    project: str,
    agent: str,
    source_url: str | None = None,
) -> DocIngestResult:
    """Ingest one document as chunked, born-quarantined memory rows.

    The full Ф3 pipeline: chunk (structure-preserving), write every
    chunk row QUARANTINED (``pipeline_state=QUARANTINED``,
    ``quarantine_reason=doc-ingest-born-quarantine``, status RAW —
    invisible everywhere), then sweep all chunks together at
    ingest-completion (:func:`sweep_document_chunks` — release or
    refuse per the chunk-atomic semantics).

    First ingest: the rows go through :meth:`MemoryManager.add
    <vesma.manager.MemoryManager.add>` (the ordinary write path —
    secrets auto-tag, canon gate, FTS bookkeeping) and are quarantined
    IMMEDIATELY after each write, before the sweep — no window exists
    where a doc chunk is admissible pre-sweep. Two belts hold this
    (review round P3-3 — the docstring now matches the code, which is
    SAFER than the previously described single belt): the chunk is
    created with an EXPLICIT ``status=RAW``, so the visibility-policy
    branch never runs (an explicit status keeps the pre-B2b contract —
    the row is born invisible by its own status), and the
    ``quarantine_entry`` that follows pulls the lifecycle lane to
    QUARANTINED and REMOVES the embed in the same ingest step. Even a
    future policy change that publishes explicit-RAW seeds cannot make
    the chunk admissible: the quarantine predicate composes with the
    status gate.

    Re-ingest of an existing ``doc_id`` (invariant 4): the document's
    rows are REPLACED through the store's transactional
    ``replace_doc_chunks`` — DELETE of the old chunk rows + INSERT of
    the new (quarantined) ones + the ccr_cache doc-chunk version bump
    in ONE ``BEGIN IMMEDIATE`` transaction. The sweep then runs over
    the new rows exactly as on first ingest.

    Returns a :class:`DocIngestResult` with per-chunk ids and counts.
    """
    all_chunks = chunk_document(text)
    if not all_chunks:
        raise ValueError(f"ingest_document: document {doc_id!r} produced no chunks")
    # Review round P3-2: the per-document cap is enforced HERE (visible
    # to the caller), never silently inside the pure chunker. A
    # truncation is logged — the cap is a defensive bound on untrusted
    # content, not a silent content drop.
    truncated = len(all_chunks) > _MAX_CHUNKS_PER_DOC
    chunks = all_chunks[:_MAX_CHUNKS_PER_DOC]
    if truncated:
        logger.warning(
            "doc ingest: doc_id=%s truncated %d -> %d chunks (per-document cap %d)",
            doc_id[:32],
            len(all_chunks),
            len(chunks),
            _MAX_CHUNKS_PER_DOC,
        )

    existing_ids = manager.sqlite.list_doc_chunk_ids(doc_id)
    if existing_ids:
        # ── Re-ingest (invariant 4): the transactional replace path ────
        rows = [
            _build_chunk_row(
                chunk,
                doc_id=doc_id,
                title=title,
                tags=tags,
                project=project,
                agent=agent,
                source_url=source_url,
            )
            for chunk in chunks
        ]
        counts = manager.sqlite.replace_doc_chunks(
            doc_id,
            rows,
            cache_version_key=DOC_CHUNK_CACHE_VERSION_META_KEY,
        )
        # ── Post-commit external hygiene (review round P2-1) ────────────
        # The transaction removed the SQL rows; the replaced chunks' side
        # stores must not keep their ids warm (the stale-embed class) or
        # orphan their vault files. Mirrors MemoryManager.delete's
        # hygiene (vectors.delete + vault.delete_file), run AFTER the
        # commit: a transaction rollback must not have destroyed the
        # side-store entries of rows that then survive.
        _cleanup_replaced_chunks(manager, counts["deleted_rows"])
        memory_ids = [r.id for r in rows]
        result = sweep_document_chunks(manager, memory_ids)
        logger.info(
            "doc ingest (re-ingest): doc_id=%s chunks=%d released=%d quarantined=%d "
            "cache_version=%d",
            doc_id[:32],
            len(memory_ids),
            result["released"],
            result["quarantined"],
            counts["cache_version"],
        )
        return DocIngestResult(
            doc_id=doc_id,
            chunks_total=len(memory_ids),
            released=result["released"],
            quarantined=result["quarantined"],
            memory_ids=tuple(memory_ids),
            reingest=True,
            cache_version=counts["cache_version"],
            truncated=truncated,
        )

    # ── First ingest: add() per chunk, quarantine immediately ────────────
    memory_ids = []
    for chunk in chunks:
        grouping = build_doc_grouping_metadata(doc_id, chunk.chunk_idx, chunk.heading_path)
        data = MemoryCreate(
            content=chunk.content,
            title=title,
            tags=list(tags),
            source=MemorySource.WEB if source_url else MemorySource.MANUAL,
            source_url=source_url,
            status=MemoryStatus.RAW,
            metadata=dict(grouping),
        )
        memory = manager.add(data, project=project, agent=agent, mint_relates_to=False)
        # Born-quarantine (invariant 8): quarantined from the first moment
        # the row exists — before the sweep, no path can issue it. add()
        # may have published it (immediate policy, clean content);
        # quarantine_entry pulls the lifecycle lane to QUARANTINED and
        # REMOVES the embed in the same step, so the row is inadmissible
        # through every leg before the sweep runs.
        manager.quarantine_entry(memory.id, reason=BORN_QUARANTINE_REASON, source="doc-ingest")
        memory_ids.append(memory.id)

    result = sweep_document_chunks(manager, memory_ids)
    logger.info(
        "doc ingest: doc_id=%s chunks=%d released=%d quarantined=%d",
        doc_id[:32],
        len(memory_ids),
        result["released"],
        result["quarantined"],
    )
    return DocIngestResult(
        doc_id=doc_id,
        chunks_total=len(memory_ids),
        released=result["released"],
        quarantined=result["quarantined"],
        memory_ids=tuple(memory_ids),
        reingest=False,
        cache_version=manager.sqlite.doc_chunk_cache_version(DOC_CHUNK_CACHE_VERSION_META_KEY),
        truncated=truncated,
    )


def _build_chunk_row(
    chunk: DocChunk,
    *,
    doc_id: str,
    title: str | None,
    tags: list[str],
    project: str,
    agent: str,
    source_url: str | None,
) -> Memory:
    """Construct a fresh born-quarantined chunk row for the replace path.

    The re-ingest path writes rows directly through the store's
    transactional ``replace_doc_chunks`` (NOT through ``add`` — the
    ordinary add() path publishes clean content immediately, and the
    replace must land quarantined from the first statement, inside the
    invariant-4 transaction). The row is RAW + QUARANTINED +
    born-quarantine reason: same initial state the first-ingest path
    reaches after its add()+quarantine_entry pair.
    """
    grouping = build_doc_grouping_metadata(doc_id, chunk.chunk_idx, chunk.heading_path)
    return Memory(
        content=chunk.content,
        title=title,
        tags=list(tags),
        source=MemorySource.WEB if source_url else MemorySource.MANUAL,
        source_url=source_url,
        metadata=dict(grouping),
        project=project,
        agent=agent,
        status=MemoryStatus.RAW,
        pipeline_state=PipelineState.QUARANTINED,
        quarantine_reason=BORN_QUARANTINE_REASON,
    )


def _cleanup_replaced_chunks(manager: MemoryManager, deleted_rows: list[Memory]) -> None:
    """Post-commit side-store hygiene for the re-ingest's replaced chunks.

    Review round P2-1: ``replace_doc_chunks`` removed the SQL rows, but
    the old chunk ids would otherwise stay warm in the vector store and
    their vault files would orphan on disk (the stale-embed-keeps-id-warm
    class — product search stays correct through the resolve-time guard,
    but a warm id is a stale index entry waiting to mis-resolve).

    Mirrors :meth:`MemoryManager.delete
    <vesma.manager.MemoryManager.delete>` hygiene: ``vectors.delete`` +
    ``vault.delete_file`` per replaced row. Runs AFTER the replace
    transaction commits — a rollback must not destroy side-store entries
    of rows that then survive. Each item is best-effort (a failure is
    logged, never raised: the row is already gone from SQL; a lingering
    vector id is inert for ranking and the vector sweeper heals it).
    """
    for row in deleted_rows:
        try:
            manager.vectors.delete(row.id)
        except Exception as exc:  # non-fatal — sweeper heals stale ids
            logger.warning(
                "doc re-ingest hygiene: vector delete failed for %s (non-fatal): %s",
                row.id[:8],
                exc,
            )
        if row.file_path:
            try:
                manager.vault.delete_file(row.file_path)
            except Exception as exc:  # non-fatal — an orphan file is inert
                logger.warning(
                    "doc re-ingest hygiene: vault file delete failed for %s (non-fatal): %s",
                    row.id[:8],
                    exc,
                )
