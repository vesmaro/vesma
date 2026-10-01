"""ADR-0018 — ``on_context_rewrite`` lifecycle event (vesma #125, Wave 2).

The harness (zcode or any MCP-capable peer) owns the *replacement policy*:
when it rewrites a block inside its own context window it emits this event
so the original becomes losslessly recoverable. The provider owns the
*guarantees*: zero-loss storage, secret scan, provenance, status gate.

Event semantics (ADR-0018 §"on_context_rewrite", verbatim requirements):

* **Idempotent** — re-delivery of the same rewrite event performs no
  duplicate writes. The idempotency key is content-addressed (the CCR
  house style): SHA-256 over the length-prefixed canonical tuple
  ``(project, agent, session, supersedes, content)``. The advisory
  ``diff`` is DELIBERATELY excluded from the key — it is not load-bearing,
  so a re-delivery carrying a different advisory diff is still the same
  event and deduplicates into the first memory. The key is persisted as
  ``metadata["rewrite_event_key"]`` and looked up before any write.
* **Version-less** — the event promises NO ordering and NO version
  chains. What would be a "version pair" elsewhere is a ``supersedes``
  edge (``memory_edges``, kind ``supersedes`` — the Phase 1 minimal
  surface); traversal/expansion is Phase 2 (ADR-0017 D2) and runs under
  the entry invariant when it arrives.
* **Pipeline entry** — the original goes through the NORMAL knowledge
  path (``MemoryManager.add``): it enters at ``raw`` and is context-
  reachable only after the pipeline advances it to ``processed`` /
  ``published`` (``CONTEXT_ADMISSIBLE_STATUSES`` gate). The Layer-1
  write-path secret scan runs inside ``add`` (a hit auto-tags
  ``mnemos:no-federate``). Rehydrate is the EXISTING scanned/gated path —
  ``mnemos_retrieve`` / ``assemble_context`` — never a new one.
* **Advisory diff** — caller-supplied, stored as metadata
  (``rewrite_diff``), never load-bearing and never echoed in responses.
  Because the diff becomes part of the persisted record, it gets its own
  Layer-1 verdict: a secret in the diff also auto-tags the record
  ``mnemos:no-federate`` (otherwise the advisory payload would federate
  unflagged through a channel that only scans ``content``), and the
  verdict is recorded as ``rewrite_diff_scan_verdict`` (clean | hit |
  unknown — the P1-a ``ccr_cache`` vocabulary). Zero-loss: the diff is
  stored verbatim either way.
* **Marker** — the CCR marker stays in the harness window (caller-side).
  When the caller asks (``include_marker=True``) the event returns the
  compress marker for the original via the existing ``compress_content``
  (content-addressed, project-scoped, Layer-1 scanned at write).

Write-surface guardrails (#125 W2 security review, finding 1 — these are
preconditions for the W3 automation slice):

* **Rate limit** — two-level (C10, ArchCom 2026-08-27). PRIMARY:
  ``vesmaro.context_rewrite_rate_limit_per_minute`` (default 30, 0 disables)
  counts STORED events per ``(project, session)`` in a one-minute window
  (the #96 guardrail-5 SQL pattern over ``memories``). SECONDARY:
  ``vesmaro.context_rewrite_project_rate_limit_per_minute`` (default 300,
  0 disables) caps the per-project AGGREGATE — total stored events across
  all sessions of the project per minute, with the distinct-session count
  reported in the 429 message as the noisy-neighbor signal. Both count
  writes, not deliveries: a deduplicated re-delivery performs no write
  and consumes no quota, so at-least-once retry storms stay harmless.
  NULL-session events form their own bucket under both knobs. Over-limit
  raises :class:`ContextRewriteRateLimitError` — a backpressure signal,
  NOT a validation failure (REST maps it to 429, MCP to a clean error
  dict; ``ValueError`` remains 422/``{"error": …}``). Residual risk
  (ADR-0018-accepted): the aggregate ceiling introduces a noisy-neighbor
  channel — one busy project can starve its siblings on a shared node;
  accepted for the single-operator deployment, revisit on the first
  multi-harness adapter.
* **Size caps** — ``vesmaro.context_rewrite_max_content_chars`` (default
  1 MiB in chars) and ``vesmaro.context_rewrite_max_diff_chars`` (default
  256 KiB in chars) reject oversized payloads at the boundary before
  any write.

Supersedes scoping (#125 W2 security review, finding 2): the target must
exist AND belong to the caller's project — the pre-flight message is
deliberately identical for "no such memory" and "memory of another
project" (no global existence oracle, mirroring the P1-a ``ccr_get``
project-scoping semantics).

Design decisions (flagged for ArchCom ratification in the #125 report):

* **Idempotency key** — content-hash over the canonical tuple, not a
  caller-supplied event id: the harness cannot fabricate collisions, and
  identical re-deliveries dedupe even when the caller lost its event id.
  Two identical blocks replaced in two different sessions are two events
  (``session`` participates in the key) and store twice — correct, they
  are distinct windows.
* **Provenance shape** — ``metadata["source"] = "context-rewrite"`` plus
  ``metadata["rewrite_session"]``; the project/agent identity lives in
  the tag contract (``project:<slug>`` / ``agent:<slug>`` tags, enforced
  via ``validate_tag_contract`` with the caller's strictness knob).
  ``Memory.source`` stays ``MemorySource.MCP`` — the event arrives from
  the harness over the MCP/REST surface like every other tool call.
* **No new retrieval surface** — deliberately. The rehydrate roundtrip is
  verified in tests through ``assemble_context`` / ``mnemos_retrieve``.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus, validate_tag_contract

if TYPE_CHECKING:
    from vesmaro.manager import MemoryManager

logger = logging.getLogger(__name__)

#: Provenance discriminator for rewrite-stored originals (metadata key
#: ``source``). Names the ingestion channel; the identity lives in tags.
SOURCE_CONTEXT_REWRITE: Final[str] = "context-rewrite"

#: Diff-scan verdicts (mirrors the P1-a ``ccr_cache`` vocabulary).
_DIFF_VERDICT_CLEAN: Final[str] = "clean"
_DIFF_VERDICT_HIT: Final[str] = "hit"
_DIFF_VERDICT_UNKNOWN: Final[str] = "unknown"


class ContextRewriteRateLimitError(RuntimeError):
    """Rewrite-event write quota exceeded (#125 W2 review F1).

    Deliberately NOT a ``ValueError``: this is backpressure, not a
    validation failure — the REST surface maps it to HTTP 429 and the MCP
    surface to a clean ``{"error": …, "rate_limited": true}`` dict, while
    ``ValueError`` keeps its 422 / plain-error-dict semantics.
    """


# ── Idempotency key ───────────────────────────────────────────────────────────


def compute_event_key(
    *,
    project: str,
    agent: str,
    session: str | None,
    supersedes: str | None,
    content: str,
) -> str:
    """Content-addressed idempotency key for one rewrite event.

    SHA-256 over the length-prefixed canonical tuple ``(project, agent,
    session, supersedes, content)``. Length prefixing makes the encoding
    injective for arbitrary content (a delimiter scheme could be spoofed
    by content containing the delimiter). The advisory ``diff`` is
    excluded by design — see the module docstring.
    """
    parts = (project, agent, session or "", supersedes or "", content)
    canonical = "".join(f"{len(part)}:{part}" for part in parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Validation ─────────────────────────────────────────────────────────────────


def _validate(
    content: str,
    project: str,
    agent: str,
    session: str | None,
    supersedes: str | None,
    diff: str | None,
    *,
    max_content_chars: int,
    max_diff_chars: int,
) -> None:
    """Boundary validation — raises ``ValueError`` with actionable messages.

    Size caps (W2 review F1) reject oversized payloads BEFORE any write:
    the rewrite surface must not become a dump channel for whole-file
    blobs or multi-megabyte "advisory" diffs.
    """
    if not content or not content.strip():
        raise ValueError("content is required (non-empty string)")
    if len(content) > max_content_chars:
        raise ValueError(
            f"content exceeds the rewrite size cap: {len(content)} chars "
            f"> {max_content_chars} (vesmaro.context_rewrite_max_content_chars)"
        )
    if not project or not project.strip():
        raise ValueError("project is required (non-empty string)")
    if not agent or not agent.strip():
        raise ValueError("agent is required (non-empty string)")
    for name, value in (("session", session), ("supersedes", supersedes), ("diff", diff)):
        if value is not None and not value.strip():
            raise ValueError(f"{name} must be a non-empty string when provided")
    if diff is not None and len(diff) > max_diff_chars:
        raise ValueError(
            f"diff exceeds the rewrite size cap: {len(diff)} chars "
            f"> {max_diff_chars} (vesmaro.context_rewrite_max_diff_chars)"
        )


# ── Layer-1 verdict for the advisory diff ─────────────────────────────────────


def _scan_diff(diff: str | None) -> tuple[str, list[str]]:
    """Scan the advisory diff at write time; return ``(verdict, extra_tags)``.

    The diff is part of the persisted record but is NOT ``content`` — the
    ``add`` write-path scanner would never see it, and a secret there
    would federate unflagged. Same Layer-1 policy as ``add``: a hit
    auto-tags ``mnemos:no-federate`` (idempotent — ``add`` skips the tag
    when already present); the diff itself is stored verbatim
    (zero-loss). A scanner error degrades to verdict ``unknown`` with NO
    tag (the background Layer-2 scanner backstops, mirroring ``add``).
    Only pattern names and counts are ever logged.
    """
    if diff is None:
        return "", []
    from vesmaro.secrets_detector import detect_secrets, findings_by_pattern

    try:
        findings = detect_secrets(diff)
    except Exception as exc:  # pragma: no cover — defensive, mirrors add()
        logger.warning("rewrite diff scan failed (non-fatal, verdict=unknown): %s", exc)
        return _DIFF_VERDICT_UNKNOWN, []
    if not findings:
        return _DIFF_VERDICT_CLEAN, []
    counts = findings_by_pattern(findings)
    logger.warning(
        "rewrite diff secret hit: redactions=%d patterns=%s — raw values not logged",
        len(findings),
        counts,
    )
    return _DIFF_VERDICT_HIT, ["mnemos:no-federate"]


# ── The lifecycle event ───────────────────────────────────────────────────────


def context_rewrite(
    mgr: MemoryManager,
    *,
    content: str,
    project: str,
    agent: str,
    session: str | None = None,
    supersedes: str | None = None,
    diff: str | None = None,
    include_marker: bool = False,
) -> dict[str, Any]:
    """Handle one ``on_context_rewrite`` event (ADR-0018, idempotent).

    Args:
        mgr: The owning ``MemoryManager`` (storage via the normal
            knowledge-pipeline ``add`` path; edges via the P1-a store
            methods; marker via the existing ``compress_content``).
        content: The ORIGINAL text of the replaced block — the source of
            truth, stored to LTM unchanged (zero-loss).
        project: Project slug (tag ``project:<slug>``).
        agent: Agent slug (tag ``agent:<slug>``).
        session: Optional session identifier — provenance metadata and
            part of the idempotency key (two sessions replacing
            identical content are two events).
        supersedes: Optional memory id of the block being replaced —
            creates the ``supersedes`` edge ``new → old``. Must reference
            an existing memory (validated up front; the store FK is the
            backstop).
        diff: Optional advisory was→becomes diff — stored as metadata,
            never load-bearing, never echoed.
        include_marker: When True, also return the CCR compress marker
            for the original (the caller keeps the marker in its window;
            rehydrate goes through ``mnemos_retrieve``).

    Returns:
        Event receipt: ``status`` (``stored`` | ``deduplicated``),
        ``memory_id``, ``memory_status`` (pipeline status — ``raw`` on
        first delivery), ``event_key``, echoed ``project``/``agent``/
        ``session``, ``supersedes`` (``{"to_memory_id", "edge_created"}``
        or ``None``) and ``ccr_marker`` (the full ``compress_content``
        result, only when ``include_marker``).

    Raises:
        ValueError: Invalid boundary input (incl. size-cap violations), a
            tag-contract violation, or a ``supersedes`` target that is not
            found in the caller's project (existence and cross-project
            cases share one message — no global existence oracle).
        ContextRewriteRateLimitError: The per-(project, session) stored-event
            quota or the per-project aggregate ceiling is exhausted (W2
            review F1 + C10; REST → 429).
    """
    mnemos_cfg = mgr.settings.mnemos
    _validate(
        content,
        project,
        agent,
        session,
        supersedes,
        diff,
        max_content_chars=mnemos_cfg.context_rewrite_max_content_chars,
        max_diff_chars=mnemos_cfg.context_rewrite_max_diff_chars,
    )

    event_key = compute_event_key(
        project=project, agent=agent, session=session, supersedes=supersedes, content=content
    )

    # ── Idempotency: the same event re-delivered writes nothing new ──────
    existing_id = mgr.sqlite.get_memory_id_by_rewrite_event_key(event_key)
    if existing_id is not None:
        existing = mgr.get(existing_id)
        receipt: dict[str, Any] = {
            "status": "deduplicated",
            "memory_id": existing_id,
            "memory_status": existing.status.value if existing else "unknown",
            "event_key": event_key,
            "project": project,
            "agent": agent,
            "session": session,
        }
        if supersedes is not None:
            receipt["supersedes"] = _link_supersedes(mgr, existing_id, supersedes, project)
        else:
            receipt["supersedes"] = None
        if include_marker:
            # A2: the marker is minted in THIS event's issuer context so
            # strict marker validation can later prove provenance.
            receipt["ccr_marker"] = mgr.compress_content(
                content, project=project, agent=agent, session=session
            )
        logger.info(
            "context_rewrite: DEDUPLICATED event_key=%s… memory=%s project=%s agent=%s",
            event_key[:12],
            existing_id[:8],
            project,
            agent,
        )
        return receipt

    # ── Write quota (W2 review F1): STORED events per (project, session) ──
    # Runs only on the write path — a deduplicated delivery (above) never
    # reaches this check, so retry storms stay harmless by construction.
    minute_ago = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    limit = mnemos_cfg.context_rewrite_rate_limit_per_minute
    if limit > 0:
        recent = mgr.sqlite.count_recent_context_rewrites(project, session, minute_ago)
        if recent >= limit:
            logger.warning(
                "context_rewrite: RATE LIMITED project=%s agent=%s session=%s "
                "recent=%d limit=%d/min — content never logged",
                project,
                agent,
                session,
                recent,
                limit,
            )
            raise ContextRewriteRateLimitError(
                f"context rewrite rate limit exceeded: {recent} stored events for "
                f"project={project!r} session={session!r} in the last minute "
                f"(limit: {limit}/min). Wait or raise "
                f"vesmaro.context_rewrite_rate_limit_per_minute."
            )

    # ── C10 (ArchCom 2026-08-27): SECONDARY per-project aggregate ceiling ──
    # Total stored events across ALL sessions of the project in the last
    # minute. Closes the fan-out gap of the primary limiter (N sessions x
    # full per-session budget = N x budget rows from one caller identity)
    # while the residual noisy-neighbor risk — one busy project starving
    # its siblings on a shared node — is ADR-0018-accepted (single-
    # operator). The distinct-session count is the noisy-neighbor signal
    # carried in the log line and the 429 message. NULL-session events
    # count as rows and as their own session bucket. Same 429 shape.
    project_limit = mnemos_cfg.context_rewrite_project_rate_limit_per_minute
    if project_limit > 0:
        project_rows, project_sessions = mgr.sqlite.count_recent_context_rewrites_by_project(
            project, minute_ago
        )
        if project_rows >= project_limit:
            logger.warning(
                "context_rewrite: PROJECT RATE LIMITED project=%s agent=%s "
                "rows=%d sessions=%d limit=%d/min (noisy-neighbor guard) — "
                "content never logged",
                project,
                agent,
                project_rows,
                project_sessions,
                project_limit,
            )
            raise ContextRewriteRateLimitError(
                f"context rewrite project rate limit exceeded: {project_rows} "
                f"stored events across {project_sessions} session(s) for "
                f"project={project!r} in the last minute "
                f"(limit: {project_limit}/min). Wait or raise "
                f"vesmaro.context_rewrite_project_rate_limit_per_minute."
            )

    # ── Pre-flight the supersedes target (clean error before any write) ──
    # W2 review F2: project-scoped, and the message deliberately does NOT
    # distinguish "no such memory" from "memory of another project".
    if supersedes is not None:
        target = mgr.get(supersedes)
        if target is None or (target.project or "") != project:
            raise ValueError(f"supersedes target {supersedes!r} not found in project {project!r}")

    # ── Store the original via the NORMAL knowledge-pipeline path ────────
    diff_verdict, extra_tags = _scan_diff(diff)
    # mnemos:session — the original is live session material preserved
    # verbatim; closest existing subtype. A dedicated mnemos:context-rewrite
    # subtype would extend the shared tag-contract vocabulary (flagged for
    # ArchCom ratification in the #125 report instead of landing silently).
    tags = [f"project:{project}", f"agent:{agent}", "mnemos:session", *extra_tags]
    tags = validate_tag_contract(tags, strict=mgr.settings.mnemos.strict_tag_contract)

    metadata: dict[str, Any] = {
        "source": SOURCE_CONTEXT_REWRITE,
        "rewrite_event_key": event_key,
    }
    if session is not None:
        metadata["rewrite_session"] = session
    if diff is not None:
        metadata["rewrite_diff"] = diff
        metadata["rewrite_diff_scan_verdict"] = diff_verdict

    memory = mgr.add(
        MemoryCreate(
            content=content,
            tags=tags,
            source=MemorySource.MCP,
            metadata=metadata,
            # ADR-0018 entry invariant / ADR-0019 B2b: the stored original
            # ENTERS as raw and reaches context only after the pipeline
            # advances it — this path deliberately opts out of the
            # ``vesmaro.visibility`` immediate-publish default (a rewrite
            # archive payload is not agent-authored knowledge to surface
            # at once). Explicit status keeps the pre-B2b contract.
            status=MemoryStatus.RAW,
        ),
        project=project,
        agent=agent,
        # Trusted caller: ONLY this path may derive the denormalised
        # rewrite quota columns from metadata (C10 review round — generic
        # create surfaces never derive them, so planted client metadata
        # cannot mint quota counters).
        trusted_rewrite_provenance=True,
    )

    receipt = {
        "status": "stored",
        "memory_id": memory.id,
        "memory_status": memory.status.value,
        "event_key": event_key,
        "project": project,
        "agent": agent,
        "session": session,
        "supersedes": None,
    }
    if supersedes is not None:
        receipt["supersedes"] = _link_supersedes(mgr, memory.id, supersedes, project)
    if include_marker:
        # A2: issuer = this event's (agent, session) — see the dedup path.
        receipt["ccr_marker"] = mgr.compress_content(
            content, project=project, agent=agent, session=session
        )

    logger.info(
        "context_rewrite: stored memory=%s project=%s agent=%s edge=%s",
        memory.id[:8],
        project,
        agent,
        receipt["supersedes"] is not None,
    )
    return receipt


def _link_supersedes(mgr: MemoryManager, new_id: str, old_id: str, project: str) -> dict[str, Any]:
    """Create the ``new → old`` supersedes edge (idempotent by PK).

    The P1-a store method is ``INSERT OR IGNORE`` on the composite PK, so
    a re-delivery reports ``edge_created=False`` instead of failing. The
    FK backstop converts a vanished target into the SAME clean
    ``ValueError`` shape as the pre-flight in :func:`context_rewrite`
    (the race window between pre-flight and insert is the only gap;
    message stays project-scoped and oracle-free).
    """
    try:
        created = mgr.add_memory_edge(new_id, old_id, kind="supersedes")
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"supersedes target {old_id!r} not found in project {project!r}") from exc
    return {"to_memory_id": old_id, "edge_created": created}
