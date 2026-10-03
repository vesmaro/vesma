"""Data models for the Vesma memory system.

Core models: TagContract (Vesma tag validation, M2), Memory (pipeline + Context
Filter fields, M4/M10), Trace (explainability layer, M6), AgentRecallQuery
(per-agent recall, M3).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from pydantic import BaseModel, Field, field_validator, model_validator

# ── Enums ──────────────────────────────────────────────────────────────────────


class MemoryType(StrEnum):
    NOTE = "note"
    FACT = "fact"
    SNIPPET = "snippet"
    BOOKMARK = "bookmark"
    CONVERSATION = "conversation"
    SESSION_CONTEXT = "session_context"


class MemorySource(StrEnum):
    MANUAL = "manual"
    WEB = "web"
    FILE = "file"
    MCP = "mcp"
    OBSIDIAN = "obsidian"
    CLI = "cli"
    RULE = "rule"  # M8: path-scoped rules ingest
    SYNTHESIZED = "synthesized"  # M4: output of synthesis worker


class MemoryStatus(StrEnum):
    RAW = "raw"
    PROCESSING = "processing"
    PROCESSED = "processed"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class PipelineState(StrEnum):
    """ADR-0019 Phase B — the ORTHOGONAL pipeline lifecycle column.

    Deliberately separate from :class:`MemoryStatus` (the committee
    rejected new statuses: "cascading edits to every gate, filter,
    statistic, and client for information one orthogonal field already
    carries"). Values:

    * ``pending``  — visible entry awaiting async refinement (the B1
      backfill also lands Hermes-bypass heritage here for re-healing).
    * ``processing`` — the background daemon owns the entry (B2).
    * ``refined``  — the served projection was swapped to the refined
      output; the ``refined_only`` query flag selects exactly these.
    * ``failed``   — quality/infra lane: entry stays visible raw with
      retry (B2).
    * ``quarantined`` — danger lane: terminal, manual release only;
      excluded from issuance by :func:`is_context_admissible`.
    * ``None`` (SQL NULL) — legacy rows written before ADR-0019 and
      entries not part of the optimistic-publication semantics.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    REFINED = "refined"
    FAILED = "failed"
    QUARANTINED = "quarantined"


# ADR-0018 entry invariant — the single status gate every path that
# surfaces memory-record content into working context must consult.
# It encodes the documented search default ("only 'published' knowledge
# units by default", plus 'processed'). Future LTM → context paths — the
# on_context_rewrite rehydrate channel, D2 graph expansion — gate on this
# set by construction instead of re-deriving their own: originals entering
# via the knowledge pipeline start at 'raw' and are context-reachable only
# after passing 'processed'/'published'. Deliberately NOT the full status
# enum: 'raw', 'processing' and 'archived' content must stay unreachable
# from default context issuance (an explicit status= query or the
# documented include_raw widening remains a caller decision).
CONTEXT_ADMISSIBLE_STATUSES: frozenset[MemoryStatus] = frozenset(
    {MemoryStatus.PUBLISHED, MemoryStatus.PROCESSED}
)


# ADR-0019 §5 — the quarantine predicate. The single exception the
# optimistic-publication decision carves into the ADR-0018 invariant:
# the admissibility set itself is NOT extended, but a row with
# ``pipeline_state='quarantined'`` (terminal danger-lane state, manual
# release only) is excluded from issuance regardless of its status.
# Every path that filters on the allowed status set composes THESE
# helpers instead of re-deriving the condition — no copy-pasted
# ``pipeline_state != 'quarantined'`` fragments.
def is_quarantined(memory: Memory) -> bool:
    """True when the entry sits in the terminal danger-lane state."""
    return memory.pipeline_state == PipelineState.QUARANTINED


def is_context_admissible(
    memory: Memory,
    *,
    statuses: frozenset[MemoryStatus] | set[MemoryStatus] = CONTEXT_ADMISSIBLE_STATUSES,
) -> bool:
    """ADR-0018 status gate + the ADR-0019 §5 quarantine exception.

    Args:
        memory: The row snapshot under test.
        statuses: The allowed status set. Defaults to
            ``CONTEXT_ADMISSIBLE_STATUSES`` (the issuance default).
            Callers running the documented widenings (``include_raw``
            search, the recall recency leg's "everything except
            archived") pass their own set — the quarantine exclusion is
            part of the predicate itself and always applies.
    """
    return memory.status in statuses and not is_quarantined(memory)


def render_retraction(memory: Memory) -> str:
    """ADR-0019 §5 (B2b) — the retraction render of a quarantined entry.

    ``[retracted: <iso-ts>]`` replaces the entry CONTENT on
    direct-access channels (manager/REST get-by-id, the CCR cached
    original) while the row keeps its identity — retraction is a state
    of the SAME record, never a separate tombstone row.

    Cause-NEUTRAL by ArchCom amendment (CWE-209): the render carries NO
    detector class — a content-side reason would be an oracle for
    iteratively probing which detectors fired. The reason stays
    available to the operator through the row metadata
    (``quarantine_reason`` on the direct-access response) and the
    quarantine audit line; the agent-facing channels never see it.
    ``<iso-ts>`` is the row's ``updated_at`` (the quarantine transition
    itself stamps it via ``update_fields``).

    Single construction site: every retraction surface composes THIS
    render so the format cannot drift between channels.
    """
    ts = memory.updated_at.isoformat() if memory.updated_at else "unknown"
    return f"[retracted: {ts}]"


# ── Vesma Tag Contract (M2) ──────────────────────────────────────────────────────


# Valid mnemos:* subtypes (enforced when strict_tag_contract=True)
VESMA_TAG_SUBTYPES: frozenset[str] = frozenset(
    {
        "session",
        "bug-pattern",
        "learning",
        "decision",
        "rule",
        "open-question",
        "checkpoint",
        "legacy",
        # Pipeline-synthesised entries (output of the synthesis worker, not
        # agent-authored). Mirrors MemorySource.SYNTHESIZED — the concept
        # already exists, the tag subtype now catches up so synthesised
        # memories can carry a valid mnemos: category instead of falling
        # back to mnemos:legacy.
        "synthesized",
        # Exclusion marker (ArchCom 2026-07-17 federation contract §4 КП-6):
        # ``mnemos:no-federate`` excludes a record from ALL external exchange
        # (batch sync + mediated pull). It is NOT a cognitive category — it
        # is an opt-out marker living in the ``mnemos:`` namespace so it
        # passes tag-contract validation without a new prefix. The auto-tagger
        # in ``secrets_detector`` (Layer 1) adds it on write when a secret is
        # detected. Owners can remove it with explicit confirmation.
        # Decision: option (a) — add to whitelist with a comment, rather than
        # a special-case bypass in ``validate_tag_contract``. Simpler, and
        # the contract explicitly says it is compatible with the tag contract
        # (mnemos: subtype namespace, not a new prefix).
        "no-federate",
    }
)


#: Tag that marks a record as excluded from all federation (batch export +
#: mediated pull). Auto-added by the write-path secrets scanner (Layer 1).
#: See ArchCom 2026-07-17 federation contract §4 КП-6 and §2.2.1.
NO_FEDERATE_TAG: str = "mnemos:no-federate"

# vesma #251 D0 — the five checkpoint payload fields in canonical order.
# Lives in models (shared vocabulary): mcp_server, api and manager all
# reference it. The order is load-bearing — it feeds the issuer-keyed
# checkpoint dedup hash and must never change without a dedup-key
# version bump.
CHECKPOINT_FIELDS: tuple[str, ...] = (
    "goals",
    "completed",
    "in_progress",
    "decisions",
    "context",
)

# vesma #251 security review (P1) — server-minted checkpoint identity
# stamps. Client surfaces must never set them: a forged
# ``checkpoint_dedup_key`` landing on a generic create would let the
# attacker's row satisfy a later genuine ``save_checkpoint`` dedup and
# serve their content as the victim's checkpoint (CWE-346/345 spoofed
# source). ``MemoryManager.add``/``update`` strip them from client
# metadata; only ``save_checkpoint`` mints them (trusted flag). The
# ``canon`` member strips TYPE-CONDITIONALLY (see the canon block
# below — cascade review SEC P2-2): checkpoint-type and malformed
# values strip, client task/decision/report envelopes persist.
CHECKPOINT_STAMP_KEYS: frozenset[str] = frozenset(
    {"checkpoint_agent", "checkpoint_session", "checkpoint_dedup_key", "canon"}
)

# ── vesma-canon v1.0.0 — server-minted checkpoint envelope (metadata.canon) ────
#
# Canon v1.0.0 (ratified 2026-09-26, tag canon-v1.0.0; verdicts in
# vesma-canon/docs/decisions/0003-archcom-v1-verdicts.md, engine
# obligations 1-4) makes the checkpoint record a canon record: the
# server stamps a canon envelope at ``metadata.canon`` and the body
# always carries all five EN sections (``CHECKPOINT_SECTION_TITLES``,
# pinned to the schemas' ``x-canon-sections`` annotation; empty fields
# get a deterministic per-language placeholder line). A CHECKPOINT-type
# ``metadata.canon`` is SERVER-MINTED like the identity stamps above —
# clients never set it: ``save_checkpoint`` mints it behind
# ``trusted_checkpoint_stamps``, every other create/update path strips
# client-supplied copies. Cascade review SEC P2-2 (TL ruling): the
# strip over the ``canon`` key is TYPE-CONDITIONAL at the call sites
# (``MemoryManager.add``/``update``) — canon §2 ratifies CLIENT-authored
# envelopes for the non-checkpoint types (task/decision/report carry
# extras like owner_slug/priority/size the server cannot know), so those
# PERSIST through the generic paths and flow into the canon gate. The
# key stays in this set for the checkpoint case and the malformed
# (non-dict) case: «ADR-0003 obligation 3 read over-broad in W2-S1;
# canon §2 ratified client envelopes for non-checkpoint types —
# corrected by cascade review SEC P2-2».
# Transitional rule (canon §9): a record WITHOUT ``metadata.canon`` is
# outside canon scope — pre-W2 checkpoint rows and imported/benchmark
# rows are legacy, not violations.

#: Envelope format version (canon §2, envelope.schema.json ``const "1"``).
CANON_SCHEMA_VERSION: Final[str] = "1"

#: Allowed body languages (canon §2/§6: one language per record).
CANON_LANGUAGES: frozenset[str] = frozenset({"ru", "en"})

#: Explicit field → EN section title map (ADR-0003 obligation 2). The
#: H2 titles are canon-frozen (canon §3/§10): byte-exact ``^## <Name>$``
#: lines, never re-derived from ``.title()`` — a Python-idiom change
#: (e.g. a new Unicode casing rule) must never drift the render. The
#: ORDER of the values is pinned against the ``x-canon-sections``
#: annotation of vesma-canon/schemas/checkpoint.schema.json by a
#: drift test (tests/test_checkpoint_canon_envelope.py).
CHECKPOINT_SECTION_TITLES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "goals": "Goals",
        "completed": "Completed",
        "in_progress": "In Progress",
        "decisions": "Decisions",
        "context": "Context",
    }
)

#: Deterministic per-language placeholder line for an empty section
#: (ADR-0003 obligation 1, option A): the render always emits all five
#: sections, so an empty field gets THIS line instead of being skipped.
#: Keyed ``(field, language)``. The lines are deliberately SHORT and
#: lexically disjoint — no two share >= 2 non-stopword tokens under the
#: awareness conflict-hint tokenizer (``awareness._goal_tokens``), so
#: even if a placeholder ever leaked past the goal-title guard, two
#: placeholder-only goals could not manufacture a false conflict hint.
#: The read guard in ``awareness.checkpoint_goal_title`` treats the
#: lines as "no goal"; this disjointness is belt-and-braces.
CHECKPOINT_PLACEHOLDERS: Final[Mapping[tuple[str, str], str]] = MappingProxyType(
    {
        ("goals", "en"): "(no goals recorded.)",
        ("completed", "en"): "(nothing completed yet.)",
        ("in_progress", "en"): "(nothing in progress at this point.)",
        ("decisions", "en"): "(no decisions recorded.)",
        ("context", "en"): "(no continuation context recorded.)",
        ("goals", "ru"): "(цели не зафиксированы.)",
        ("completed", "ru"): "(завершённая работа не зафиксирована.)",
        ("in_progress", "ru"): "(на момент чекпоинта ничего не выполнялось.)",
        ("decisions", "ru"): "(решения не зафиксированы.)",
        ("context", "ru"): "(контекст продолжения не зафиксирован.)",
    }
)

#: Every placeholder line in one set — the single membership test for
#: "this line is template, not user prose" (used by the awareness goal
#: title guard).
CHECKPOINT_PLACEHOLDER_LINES: Final[frozenset[str]] = frozenset(CHECKPOINT_PLACEHOLDERS.values())


def checkpoint_canon_envelope(*, language: str, session_ref: str | None = None) -> dict[str, Any]:
    """Mint the server-owned canon envelope (``metadata.canon``) for a checkpoint.

    Canon v1.0.0 §2 shape — ``schema_version``/``type``/``status``/
    ``language`` plus the checkpoint extra ``session_ref`` (nullable).
    Single construction site: every envelope for a stored checkpoint is
    minted HERE (``save_checkpoint``), never by clients — generic
    create/update paths strip client-supplied copies. The envelope never
    duplicates server-owned fields (title/tags/agent/project/dates live
    on the record, canon §2).

    Raises ``ValueError`` on a language outside :data:`CANON_LANGUAGES`
    — fail loud, never normalize (a mislabeled record poisons every
    downstream language-aware consumer).
    """
    if language not in CANON_LANGUAGES:
        raise ValueError(f"language must be one of {sorted(CANON_LANGUAGES)} (got {language!r})")
    return {
        "schema_version": CANON_SCHEMA_VERSION,
        "type": "checkpoint",
        "status": "active",
        "language": language,
        "session_ref": session_ref,
    }


# Allowed optional tag prefixes beyond the required ones
ALLOWED_OPTIONAL_PREFIXES: frozenset[str] = frozenset(
    {"severity:", "stack:", "applyTo:", "source:"}
)

# Issue #250 F2 — prefixes of POLICY-bearing tags that are stripped from
# synthesized records at construction (strip-by-default): a source
# record's application scope / severity classification must not
# transitively pin the synthesis output (extends #248). Lives next to
# ALLOWED_OPTIONAL_PREFIXES as the single source of truth so the
# strip-list cannot drift from the whitelist it polices.
POLICY_TAG_PREFIXES: frozenset[str] = frozenset({"applyTo:", "severity:"})

# Anchored with ``\Z`` (absolute end), NOT ``$``: a ``$`` anchor also
# matches just before a trailing newline, so 'project:p1\n' / 'agent:a1\n'
# passed strict validation (the columnal ``m.project = ?`` lookup never
# sees the newline — no behavioral coupling, but a newline-carrying slug
# is unreachable at query time exactly like the task dead-tag, #368).
# Same anchor discipline as TASK_SLUG_RE / _TASK_RE (Ф1-PREP, #360).
_PROJECT_RE = re.compile(r"^project:[a-z0-9_\-]{1,64}\Z")
_AGENT_RE = re.compile(r"^agent:[a-z0-9_\-]{1,64}\Z")
# ADR-0027 Phase 0 (epic #308): the optional task-scope tag. Same slug
# alphabet/length as project/agent. Zero or one per record — a record
# belongs to at most one task scope (see validate_tag_contract docstring
# for the intersection doctrine). The bare-slug pattern is public
# (single source): ``assemble_context(task=...)`` validates its argument
# against exactly the alphabet the tag contract will accept when the
# slug is threaded through as ``task:<slug>``.
_TASK_SLUG_PATTERN = r"[a-z0-9_\-]{1,64}"
# Anchored with ``\Z`` (absolute end), NOT ``$``: a ``$`` anchor also
# matches just before a trailing newline, so task="t1\n" validated and
# minted a DEAD tag — the exact-membership tags filter ("task:t1" in
# row.tags) can never hit "task:t1\n" (Ф1-PREP, #360 review item 2).
TASK_SLUG_RE: re.Pattern[str] = re.compile(rf"^{_TASK_SLUG_PATTERN}\Z")
_TASK_RE = re.compile(rf"^task:{_TASK_SLUG_PATTERN}\Z")
_VESMA_TAG_RE = re.compile(r"^mnemos:[a-z][a-z0-9\-]*\Z")

# ── Input alias: vesma:* → mnemos:* (6.0.0 store block) ──────────────────────
# ArchCom 2026-10-03, 6.0.0 store-migration verdict option B (split): the
# tag prefix is a FROZEN storage data format — every record is still WRITTEN
# as ``mnemos:*`` — while users type the product prefix at every input
# surface. One shared helper here (next to ``_VESMA_TAG_RE``); the CLI
# (``add``/``search``/``export --tags``) and the HTTP API (tag-filter query
# parsing, plus every ``validate_tag_contract`` caller) reuse it — no
# duplicated prefix/regex logic at the boundaries.
#: ``mnemos:`` is the canonical storage prefix, stable by contract;
#: ``vesma:`` is accepted as an input alias everywhere.
INPUT_ALIAS_PREFIX: Final[str] = "vesma:"


def normalize_tag_alias(tag: str) -> str:
    """Normalize one ``vesma:<subtype>`` input alias to ``mnemos:<subtype>``.

    A tag is rewritten ONLY when its subtype is in
    :data:`VESMA_TAG_SUBTYPES` — this includes ``no-federate``, so the
    ``mnemos:no-federate`` trust marker stays byte-stable in storage no
    matter which spelling the caller typed. Anything else (other
    namespaces, unknown subtypes) is returned UNCHANGED so the tag
    contract validator refuses it loudly — this helper never mints,
    drops, or silently repairs a tag.
    """
    if not tag.startswith(INPUT_ALIAS_PREFIX):
        return tag
    subtype = tag[len(INPUT_ALIAS_PREFIX) :]
    if subtype in VESMA_TAG_SUBTYPES:
        return f"mnemos:{subtype}"
    return tag


def normalize_tag_aliases(tags: list[str]) -> list[str]:
    """Apply :func:`normalize_tag_alias` across a tag list (input boundary).

    The list-level form the CLI and API input boundaries call after
    splitting comma-separated ``--tags`` / query parameters. Legacy
    ``mnemos:*`` input passes through byte-identical, so the normalized
    value always equals what the legacy form produces (round-trip
    guarantee pinned by tests).
    """
    return [normalize_tag_alias(t) for t in tags]


def normalize_project_slug(value: str) -> str:
    """Normalize a project slug to the canonical ``_PROJECT_RE`` form.

    Single authority (#400, the save_checkpoint #263 single-authority
    doctrine applied to slugs): lowercases, strips surrounding
    whitespace, and replaces internal spaces with hyphens — the SAME
    normalization the tag-contract lax mode applies to ``project:`` tags
    (see ``validate_tag_contract``). Exposed so callers that build the
    denormalised ``project`` field directly (checkpoint saves, recall
    queries, ``_detect_project`` auto-derivation) can never diverge from
    what the tag contract would mint for the same logical project.

    Returns ``""`` for empty/whitespace-only input — callers decide
    whether absence is valid (``recall_context`` requires a project,
    ``search(project=None)`` is the explicit global mode).

    A non-empty result is GUARANTEED to match ``_PROJECT_RE`` minus the
    ``project:`` prefix (same slug alphabet), or the input is not a
    salvageable slug and the caller must reject it — this helper never
    mints a silently-different namespace.
    """
    return value.strip().lower().replace(" ", "-")


def normalize_task_slug(value: str) -> str:
    """Normalize a bare task slug to the canonical ``TASK_SLUG_RE`` form.

    Ф2 (epic #308, ADR-0027 Phase 2): the task twin of
    :func:`normalize_project_slug` — the SAME #400/#407 single-authority
    doctrine applied to the task dimension. The normalization is
    byte-identical (lowercase, strip, spaces→hyphens) so a task slug can
    never normalize differently from a project slug of the same shape;
    the task alphabet (``TASK_SLUG_RE``) shares its pattern source with
    the ``task:`` tag regex, so a salvaged slug always matches what the
    tag contract would mint for ``task:<slug>``.

    Returns ``""`` for empty/whitespace-only input — callers decide
    whether absence is valid (the ``task`` parameter is optional on
    every Ф2 surface). A non-empty result either matches
    ``TASK_SLUG_RE`` or the input is NOT salvageable and the caller
    must reject it fail-loud — this helper never mints a
    silently-different task namespace (a dead ``task:`` tag would be
    unreachable at query time exactly like the #368 dead-tag case).
    """
    return value.strip().lower().replace(" ", "-")


class TagContractError(ValueError):
    """Raised when a tag set violates the Vesma tag contract in strict mode."""


def validate_tag_contract(tags: list[str], *, strict: bool = True) -> list[str]:
    """Validate tags against the Vesma tag contract.

    Scope hierarchy doctrine (ADR-0027 Phase 0, epic #308): inheritance is
    **INTERSECTION, not union** — ``project x agent x session x task``.
    A ``task:`` tag NARROWS the admissible set, never widens it: a
    task-scoped query sees a row only where the project, agent AND task
    admissibility sets already overlap. Consequences enforced here:

    * ``task:<slug>`` is OPTIONAL (zero or one). A record without a
      ``task:`` tag implies no global task — it belongs to the enclosing
      project/agent scope only.
    * MORE THAN ONE ``task:`` tag is always fatal (strict and lax): an
      entry visible in two task scopes is a union, which the doctrine
      forbids — the same ambiguity rule as duplicate ``project:`` /
      ``agent:`` tags.

    Args:
        tags: The list of tag strings to validate.
        strict: When True, raises TagContractError on any violation.
                When False (lax mode), patches the tag list with legacy
                defaults and returns it with a warning-level log entry.
                An invalid-but-salvageable ``task:`` slug is normalized;
                an unsalvageable one is DROPPED (lax mode never mints a
                fake ``task:unknown`` scope — for an optional tag, absence
                is the honest fallback).

    Returns:
        The (possibly augmented) tag list.

    Raises:
        TagContractError: If strict=True and any contract requirement is not met,
            or (always) on ambiguous tag sets (multiple project:/agent:/task:).
    """
    # Backward compat: gcw: is accepted as an alias for mnemos:
    # Old memories with gcw: tags are auto-migrated to mnemos: on validation.
    # 6.0.0 input alias: vesma:<valid-subtype> normalizes the same way
    # (normalize_tag_alias — the single alias authority); unknown vesma:
    # subtypes survive the migration and are refused below with the alias
    # named, never silently repaired.
    _migrated: list[str] = []
    for t in tags:
        if t.startswith("gcw:"):
            subtype = t[4:]
            if subtype in VESMA_TAG_SUBTYPES:
                _migrated.append(f"mnemos:{subtype}")
            else:
                _migrated.append(t)  # invalid gcw: subtype, keep as-is for error msg
        else:
            _migrated.append(normalize_tag_alias(t))
    tags = _migrated

    project_tags = [t for t in tags if t.startswith("project:")]
    agent_tags = [t for t in tags if t.startswith("agent:")]
    task_tags = [t for t in tags if t.startswith("task:")]
    mnemos_tags = [t for t in tags if t.startswith("mnemos:")]

    # Errors that are fatal even in lax mode (ambiguous context, can't auto-patch)
    fatal_errors: list[str] = []
    # Errors that can be patched in lax mode
    patchable_errors: list[str] = []

    # --- Require exactly one project:* tag ---
    if not project_tags:
        patchable_errors.append("missing required tag: project:<slug> (exactly one required)")
    elif len(project_tags) > 1:
        fatal_errors.append(
            f"exactly one project: tag required, got {len(project_tags)}: {project_tags}"
        )
    elif not _PROJECT_RE.match(project_tags[0]):
        patchable_errors.append(
            f"invalid project: tag format '{project_tags[0]}' "
            "(must match project:[a-z0-9_-]{1,64})"
        )

    # --- Require exactly one agent:* tag ---
    if not agent_tags:
        patchable_errors.append("missing required tag: agent:<slug> (exactly one required)")
    elif len(agent_tags) > 1:
        fatal_errors.append(f"exactly one agent: tag required, got {len(agent_tags)}: {agent_tags}")
    elif not _AGENT_RE.match(agent_tags[0]):
        patchable_errors.append(
            f"invalid agent: tag format '{agent_tags[0]}' (must match agent:[a-z0-9_-]{{1,64}})"
        )

    # --- Optional task:* scope tag (ADR-0027 Phase 0, at most one) ---
    # Intersection doctrine: a task tag may only narrow. Multiple task
    # tags would make the record visible in several task scopes (a
    # union) — always fatal, mirroring the project/agent ambiguity rule.
    if len(task_tags) > 1:
        fatal_errors.append(f"at most one task: tag allowed, got {len(task_tags)}: {task_tags}")
    elif len(task_tags) == 1 and not _TASK_RE.match(task_tags[0]):
        patchable_errors.append(
            f"invalid task: tag format '{task_tags[0]}' (must match task:[a-z0-9_-]{{1,64}})"
        )

    # --- Require at least one mnemos:* tag ---
    if not mnemos_tags:
        patchable_errors.append(
            "missing required tag: mnemos:<subtype> "
            f"(valid subtypes: {', '.join(sorted(VESMA_TAG_SUBTYPES))})"
        )
    else:
        for mnemos_tag in mnemos_tags:
            if not _VESMA_TAG_RE.match(mnemos_tag):
                patchable_errors.append(f"invalid mnemos: tag format: '{mnemos_tag}'")
            else:
                subtype = mnemos_tag[len("mnemos:") :]
                if subtype not in VESMA_TAG_SUBTYPES:
                    patchable_errors.append(
                        f"invalid mnemos: subtype '{subtype}' — "
                        f"allowed: {', '.join(sorted(VESMA_TAG_SUBTYPES))}"
                    )

    # 6.0.0 input alias: any vesma:-prefixed tag that SURVIVED the alias
    # migration carries an unknown subtype — refuse it with the offending
    # tag named (instead of the generic missing-mnemos error), so a typo
    # like vesma:leanring fails loud at every input boundary.
    for stray_alias in (t for t in tags if t.startswith(INPUT_ALIAS_PREFIX)):
        patchable_errors.append(
            f"invalid vesma: alias '{stray_alias}' — unknown subtype "
            f"(allowed: {', '.join(sorted(VESMA_TAG_SUBTYPES))})"
        )

    # Always fatal errors raise regardless of strict flag
    if fatal_errors:
        raise TagContractError(
            "Vesma tag contract violation(s) (always fatal):\n"
            + "\n".join(f"  - {e}" for e in fatal_errors)
        )

    if not patchable_errors:
        return list(tags)

    if strict:
        raise TagContractError(
            "Vesma tag contract violation(s):\n" + "\n".join(f"  - {e}" for e in patchable_errors)
        )

    # Lax mode: patch the tag list rather than reject
    import logging

    logger = logging.getLogger(__name__)
    logger.warning("Tag contract violations (lax mode — auto-patching): %s", patchable_errors)
    patched = list(tags)

    # Normalize case for project/agent tags instead of dropping to "unknown".
    # This prevents duplicate namespaces (project:Project-Umbra vs
    # project:project-umbra) when callers pass mixed-case slugs.
    def _normalize_slug(tag: str, regex: re.Pattern[str], prefix: str) -> str | None:
        """Return a normalized form of ``tag`` if it can be salvaged, else None.

        Delegates to :func:`normalize_project_slug` (single authority,
        #400) — the lax-mode tag patch and the direct-field callers
        (checkpoint saves, recall queries) share ONE normalization, so
        they can never diverge. If the normalized form still does not
        match ``regex``, the tag is not recoverable and the caller falls
        back to the ``<prefix>unknown`` default.
        """
        normalized = prefix + normalize_project_slug(tag[len(prefix) :])
        return normalized if regex.match(normalized) else None

    if project_tags and not _PROJECT_RE.match(project_tags[0]):
        normalized = _normalize_slug(project_tags[0], _PROJECT_RE, "project:")
        if normalized is not None:
            patched = [normalized if t == project_tags[0] else t for t in patched]
            logger.warning("Normalized project tag: %s → %s", project_tags[0], normalized)
        else:
            patched = [t for t in patched if t != project_tags[0]] + ["project:unknown"]
    elif not project_tags:
        patched.append("project:unknown")

    if agent_tags and not _AGENT_RE.match(agent_tags[0]):
        normalized = _normalize_slug(agent_tags[0], _AGENT_RE, "agent:")
        if normalized is not None:
            patched = [normalized if t == agent_tags[0] else t for t in patched]
            logger.warning("Normalized agent tag: %s → %s", agent_tags[0], normalized)
        else:
            patched = [t for t in patched if t != agent_tags[0]] + ["agent:unknown"]
    elif not agent_tags:
        patched.append("agent:unknown")

    # ADR-0027 Phase 0: task is optional, so the honest lax fallback for
    # an unsalvageable task slug is DROPPING it (a task-less record),
    # never minting a fake ``task:unknown`` scope — unlike the required
    # project/agent families, absence carries no ambiguity.
    if task_tags and not _TASK_RE.match(task_tags[0]):
        normalized = _normalize_slug(task_tags[0], _TASK_RE, "task:")
        if normalized is not None:
            patched = [normalized if t == task_tags[0] else t for t in patched]
            logger.warning("Normalized task tag: %s → %s", task_tags[0], normalized)
        else:
            patched = [t for t in patched if t != task_tags[0]]
            logger.warning(
                "Dropped unsalvageable task tag (lax mode — optional scope): %s",
                task_tags[0],
            )

    if not mnemos_tags:
        patched.append("mnemos:legacy")
    return patched


class TagContract(BaseModel):
    """Validated tag set with denormalised project + agent (+ task) slugs.

    ``task`` (ADR-0027 Phase 0) is ``""`` when the entry carries no
    ``task:`` scope tag — absence means "no global task", never a fake
    ``unknown`` scope.
    """

    tags: list[str]
    strict: bool = Field(default=True, exclude=True)
    project: str = ""
    agent: str = ""
    task: str = ""
    mnemos_subtypes: frozenset[str] = Field(default_factory=frozenset, exclude=True)

    @model_validator(mode="after")
    def _validate_and_extract(self) -> TagContract:
        # May raise TagContractError (→ caught by Pydantic as ValidationError)
        validated = validate_tag_contract(self.tags, strict=self.strict)
        self.tags = validated
        subtypes: set[str] = set()
        for tag in validated:
            if tag.startswith("project:") and not self.project:
                self.project = tag[len("project:") :]
            elif tag.startswith("agent:") and not self.agent:
                self.agent = tag[len("agent:") :]
            elif tag.startswith("task:") and not self.task:
                self.task = tag[len("task:") :]
            elif tag.startswith("mnemos:"):
                subtypes.add(tag[len("mnemos:") :])
        self.mnemos_subtypes = frozenset(subtypes)
        return self


# ── Multi-context doc grouping (ADR-0027 Phase 0 — convention) ─────────────────
#
# Docs-as-memory grouping rides the EXISTING ``Memory.metadata`` JSON
# column: a memory row that is a chunk of an ingested document carries
# ``{doc_id, chunk_idx, heading_path}`` in its metadata dict. This is a
# CONVENTION, not enforced schema (ADR-0027: a separate ``documents``
# table only if a measured parent→chunks query pattern appears) — hence
# zero migration: no column, no index, no backfill. ``file_path`` /
# ``source_url`` remain the existing first-class columns; ``doc_id`` is
# the logical document identity that survives re-chunking.
DOC_GROUPING_METADATA_FIELDS: tuple[str, ...] = ("doc_id", "chunk_idx", "heading_path")

#: Defensive bound on ``doc_id`` length (no external spec; matches the
#: conservative slug sizes used across the tag contract).
_DOC_ID_MAX_LEN = 256


def build_doc_grouping_metadata(
    doc_id: str, chunk_idx: int, heading_path: Sequence[str]
) -> dict[str, Any]:
    """Build the doc-grouping metadata convention dict (ADR-0027 Phase 0).

    Validates the triple's shape at the single construction site so the
    convention cannot drift between writers:

    * ``doc_id`` — non-empty string (stripped), at most 256 chars;
    * ``chunk_idx`` — 0-based, non-negative ``int`` (``bool`` rejected);
    * ``heading_path`` — ordered root→leaf heading chain, a sequence of
      strings (an empty chain is legal: a chunk before the first heading).

    Returns a fresh dict meant to be merged into ``Memory.metadata``
    (the caller owns the rest of the metadata dict). Raises
    ``ValueError`` on any shape violation.
    """
    if not isinstance(doc_id, str):
        raise ValueError(f"doc_id must be a string, got {type(doc_id).__name__}")
    stripped = doc_id.strip()
    if not stripped or len(stripped) > _DOC_ID_MAX_LEN:
        raise ValueError(f"doc_id must be a non-empty string of at most {_DOC_ID_MAX_LEN} chars")
    if isinstance(chunk_idx, bool) or not isinstance(chunk_idx, int) or chunk_idx < 0:
        raise ValueError(f"chunk_idx must be a non-negative int, got {chunk_idx!r}")
    if not isinstance(heading_path, Sequence) or isinstance(heading_path, (str, bytes)):
        raise ValueError("heading_path must be a sequence of strings")
    headings = list(heading_path)
    for h in headings:
        if not isinstance(h, str):
            raise ValueError(f"heading_path entries must be strings, got {h!r}")
    return {"doc_id": stripped, "chunk_idx": chunk_idx, "heading_path": headings}


def doc_grouping_from_metadata(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    """Extract the doc-grouping triple from a memory metadata dict.

    Returns ``None`` when the metadata carries no doc grouping at all
    (the default for every non-document row). When ANY of the three
    convention keys is present, ALL must be present and well-formed —
    a half-written grouping is corruption, so a ``ValueError`` is raised
    (loud) rather than silently treating the row as a non-document:
    Phase-3 assembly groups chunks by ``doc_id`` and a silent ``None``
    would split a document without a trace.
    """
    present = [k for k in DOC_GROUPING_METADATA_FIELDS if k in metadata]
    if not present:
        return None
    missing = [k for k in DOC_GROUPING_METADATA_FIELDS if k not in metadata]
    if missing:
        raise ValueError(
            f"partial doc grouping metadata: present {present}, missing {missing} "
            "(the convention is all-or-nothing)"
        )
    # Delegate shape validation to the single construction site. Raw
    # values, no coercion: a chunk_idx stored as "3" or 1.5 is malformed
    # metadata and must raise, not silently normalize.
    return build_doc_grouping_metadata(
        metadata["doc_id"],
        metadata["chunk_idx"],
        metadata["heading_path"],
    )


# ── Core memory model ──────────────────────────────────────────────────────────


class Memory(BaseModel):
    """Single unified memory entry — status-driven pipeline model.

    Field groups:
      - Vesma tag contract denormalisations (project, agent)
      - Knowledge pipeline fields (quality_score, confidence, cluster_id, derived_from …)
      - Context Filter fields (raw_content, clean_content, filter_profile …) — M10
      - Embedding tracking (embedding_id)
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    content: str

    # ── Basic metadata ──────────────────────────────────────────────────────
    title: str | None = None
    tags: list[str] = Field(default_factory=list)
    source: MemorySource = MemorySource.MANUAL
    source_url: str | None = None
    memory_type: MemoryType = MemoryType.NOTE
    file_path: str | None = None
    category: str | None = None

    # ── Vesma tag contract (denormalised from tags, set by MCP/TagContract layer) ──
    project: str = ""
    agent: str = ""

    # ── Knowledge pipeline status (M4) ─────────────────────────────────────
    status: MemoryStatus = MemoryStatus.RAW
    quality_score: float | None = None
    confidence: float | None = None
    source_coverage: int | None = None  # number of distinct source URLs/paths in cluster
    cluster_id: str | None = None
    derived_from: list[str] = Field(default_factory=list)  # source Memory ids
    embedding_id: str | None = None  # ChromaDB id; set when status = published

    # ── Context Filter (M10) ───────────────────────────────────────────────
    # Fields present from day 1; filter logic wired in M10.
    # Invariant: raw_content is never mutated after first write.
    raw_content: str | None = None  # immutable source payload (logs, HTML, etc.)
    clean_content: str | None = None  # filtered projection for model-facing flows
    filter_profile: str | None = None  # log | terminal | code | docs | web | default
    filter_stats: dict[str, Any] | None = None  # token + dedup reduction stats
    filter_version: str | None = None  # filter pipeline version used

    # ── ADR-0019 Phase B (B1): orthogonal pipeline lifecycle ───────────────
    # Visibility semantics stay owned by ``status`` (ADR-0018 invariant,
    # unchanged); this group carries the async-refinement lifecycle.
    # NOT settable through MemoryCreate/MemoryUpdate — external surfaces
    # must not forge pipeline provenance. Writers: the schema backfill,
    # store-internal update_fields callers (the B2 daemon / swap path),
    # and the N1 demotions (which only ever write RAW-status side
    # effects, never a pipeline_state).
    pipeline_state: PipelineState | None = None
    processed_at: datetime | None = None  # TS of the refined swap (B2)
    swap_key: str | None = None  # hash(cluster_id, prompt_version, processed_hash)
    quarantine_reason: str | None = None  # detector class code (lane b)
    marker_version: int = 1  # provenance marker version, incremented on swap

    # ── Workflow lifecycle (vesma #96) ────────────────────────────────────
    # Read-only projection of the workflow state. Writes go through
    # MemoryManager.workflow_set → SQLiteStore.set_workflow_status so the
    # state machine cannot be bypassed by a generic update. ``None`` on
    # legacy rows created before the migration and on freshly-created
    # memories (defaults to ``open`` when read via workflow APIs).
    workflow_status: str | None = None
    locked_by: str | None = None
    locked_at: str | None = None

    # ── Timestamps ──────────────────────────────────────────────────────────
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # ── Compat (retained for migration tooling) ─────────────────────────────
    metadata: dict[str, Any] = Field(default_factory=dict)

    # ── Validation control (not stored) ────────────────────────────────────
    # Set strict_tags=True to enforce Vesma tag contract on construction.
    strict_tags: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _maybe_validate_tags(self) -> Memory:
        if self.strict_tags and self.tags:
            validate_tag_contract(self.tags, strict=True)
        return self

    def auto_title(self) -> str:
        """Generate a title from the first line of content if not set."""
        if self.title:
            return self.title
        first_line = self.content.strip().split("\n")[0][:100]
        return first_line.lstrip("# ").strip() or "Untitled"

    def effective_content(self) -> str:
        """Return clean_content if available, otherwise fall back to content.

        This is the default payload for retrieval and model-facing flows.
        Use raw_content for audit / drill-down.
        """
        return self.clean_content or self.content


class MemoryCreate(BaseModel):
    content: str
    title: str | None = None
    tags: list[str] = Field(default_factory=list)
    source: MemorySource = MemorySource.MANUAL
    source_url: str | None = None
    memory_type: MemoryType = MemoryType.NOTE
    metadata: dict[str, Any] = Field(default_factory=dict)
    category: str | None = None
    # M10: explicit filter profile; if omitted, heuristics select one
    filter_profile: str | None = None
    # Allow override for path-scoped rules ingest (M8) and migrations (M13)
    status: MemoryStatus = MemoryStatus.RAW

    @field_validator("metadata")
    @classmethod
    def _validate_doc_grouping(cls, v: dict[str, Any]) -> dict[str, Any]:
        """ADR-0027 Phase 0 (epic #308, slice-1 review item 1) — write-side
        doc-grouping validation at the DTO boundary.

        The convention is all-or-nothing: a metadata dict carrying ANY of
        ``{doc_id, chunk_idx, heading_path}`` must carry all three
        well-formed, or construction fails (a persisted half-triple would
        split a document silently once the Phase-3 reader groups by
        ``doc_id``). Dicts without any convention key pass untouched —
        the metadata column stays free-form for everything else.
        """
        doc_grouping_from_metadata(v)
        return v


class RuleIngestRequest(BaseModel):
    """Request body for POST /rules/ingest."""

    rules_dir: str
    project: str = ""
    agent: str = ""
    pattern: str = "*.instructions.md"


class RuleRemoveRequest(BaseModel):
    """Request body for DELETE /rules/ingest."""

    file_path: str


class FilterRequest(BaseModel):
    """Request body for POST /filter/{memory_id}."""

    profile: str | None = None
    budget: int | None = None
    # M1 (final review): caller project scope — with it, the memory must
    # belong to the project (fail-closed on mismatch); without it the call
    # is explicit operator semantics (mirrors GET /memories/{id}).
    project: str | None = None


class MemoryUpdate(BaseModel):
    content: str | None = None
    title: str | None = None
    tags: list[str] | None = None
    memory_type: MemoryType | None = None
    metadata: dict[str, Any] | None = None
    status: MemoryStatus | None = None
    category: str | None = None
    quality_score: float | None = None
    confidence: float | None = None
    cluster_id: str | None = None

    @field_validator("metadata")
    @classmethod
    def _validate_doc_grouping(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        """MemoryUpdate twin of ``MemoryCreate._validate_doc_grouping`` —
        an external ``metadata=`` REPLACES the dict wholesale (see
        ``MemoryManager.update``), so the replacement dict validates at
        construction exactly like a create would."""
        if v is not None:
            doc_grouping_from_metadata(v)
        return v


class SearchQuery(BaseModel):
    query: str
    tags: list[str] | None = None
    source: MemorySource | None = None
    memory_type: MemoryType | None = None
    status: MemoryStatus | None = None
    project: str | None = None
    agent: str | None = None  # M3: per-agent filter
    # Ф2 (epic #308, ADR-0027 Phase 2): the first-class task-scope
    # switcher — the bare slug, byte-identical to appending
    # ``"task:<slug>"`` to ``tags`` (the F1 arm-C surface). Normalized /
    # fail-loud at the ``MemoryManager.search`` boundary (the #407
    # canon); composes with ``tags`` by intersection.
    task: str | None = None
    current_file_path: str | None = None  # M8: file-context boost
    limit: int = 20
    hybrid_alpha: float | None = None  # override config default
    include_raw: bool = False  # M10: drill-down to raw_content
    # ADR-0019 §4 — "refined only" query flag: issue only entries whose
    # served projection is the refined one (``pipeline_state='refined'``).
    # A query flag, NOT status ontology. NULL/legacy pipeline_state rows
    # (pre-ADR-0019) never match — federation / multi-principal boundaries
    # export refined projections only.
    refined_only: bool = False


# ── Per-agent recall (M3) ──────────────────────────────────────────────────────


class AgentRecallQuery(BaseModel):
    """M3 — first-class per-agent recall query.

    Ф2 (epic #308, ADR-0027 Phase 2): ``task`` is the optional task
    scope — the bare slug, byte-identical semantics to filtering by the
    ``task:<slug>`` tag (the F1 arm-C surface). Normalized/fail-loud at
    the ``MemoryManager.agent_recall`` boundary (the #407 canon).
    """

    agent: str
    project: str | None = None
    query: str | None = None  # if None: return most recent N entries for agent
    task: str | None = None
    limit: int = 20
    include_raw: bool = False


class SearchResult(BaseModel):
    memory: Memory
    score: float
    search_type: str  # "semantic" | "fts" | "hybrid"
    # Search v2 provenance (issue #313) — optional, backward-compatible:
    # absent fields mean "ordinary fused hit" (the pre-v2 contract).
    # ``project_scope_fallback``: the row surfaced because the scoped
    # search found nothing and the caller retried WITHOUT the project
    # scope (soft fallback) — the row is CROSS-PROJECT relative to the
    # original request and the caller must be able to see that.
    project_scope_fallback: bool = False
    # ``task_tag_fallback`` (#455): the row surfaced because a BARE-SLUG
    # tags filter (``tags=["my-task"]``) found nothing and the search
    # retried with the exact minted tag (``task:my-task``) — the row is
    # task-scoped relative to a request that literally asked for the
    # bare tag, and the caller must be able to see that. Mirrors the
    # project_scope_fallback precedent (flag + counter pattern).
    task_tag_fallback: bool = False
    # ``via_graph``: the row was appended by the 1-hop memory_edges
    # expansion (edge neighbours of fused hits), not by lexical /
    # vector matching. Edge-sourced rows pass the same status /
    # quarantine / refined_only gates as every other result.
    via_graph: bool = False
    # ``via_graph_kind`` (#324 review fix): the edge kind of the row's
    # FIRST-ANCHOR discovery — "supersedes" (the unconditional v1 leg,
    # always on) or "relates_to" (the flag-gated A0 walk). ``None`` for
    # ordinary fused hits. Splits the enrichment telemetry by SOURCE
    # LEG so the flag-gated walk share is measurable without conflating
    # it with the unconditional supersedes leg (a neighbour reachable
    # via both kinds from its first anchor counts as supersedes — the
    # unconditional leg reached it; the walk claims only what it alone
    # surfaced).
    via_graph_kind: str | None = None


# ── Trace model (M6 — explainability layer) ────────────────────────────────────


class Trace(BaseModel):
    """Per-pipeline-step audit record.

    Security note: rationale_summary is a ≤200-char human-readable summary.
    Raw LLM chain-of-thought is NEVER stored here.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_label: str  # cluster | synthesize | publish | recall
    project: str
    step: str
    item_id: str | None = None  # Memory id being processed
    llm_called: bool = False
    llm_done: bool = False
    cache_hit: bool = False
    fallback_used: bool = False
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_per_sec: float = 0.0
    rationale_summary: str = ""  # ≤200 chars — NO chain-of-thought
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("rationale_summary")
    @classmethod
    def _truncate_rationale(cls, v: str) -> str:
        return v[:200]


# ── Project model ─────────────────────────────────────────────────────────────


class Project(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str = ""
    paths: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    paths: list[str] = Field(default_factory=list)


class BulkDeleteRequest(BaseModel):
    ids: list[str]


class BulkTagRequest(BaseModel):
    ids: list[str]
    add_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)


# ── Pipeline models (M4) ───────────────────────────────────────────────────────


class ClusterResult(BaseModel):
    """Output of the cluster worker."""

    cluster_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    memory_ids: list[str] = Field(default_factory=list)
    centroid: list[float] | None = None  # mean embedding of cluster members
    representative_id: str | None = None  # id of the most central memory


class SynthesisResult(BaseModel):
    """Output of the LLM synthesis worker."""

    draft_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    cluster_id: str
    content: str
    title: str | None = None
    quality_score: float = 0.0
    confidence: float = 0.0
    source_coverage: int = 0
    model_used: str = ""
    prompt_version: str = ""
    cache_hit: bool = False
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


class QualityResult(BaseModel):
    """Output of the quality gate stage."""

    passed: bool
    memory_id: str
    quality_score: float = 0.0
    confidence: float = 0.0
    source_coverage: int = 0
    failures: list[str] = Field(default_factory=list)
    rationale: str = ""  # ≤200 chars


class PublishResult(BaseModel):
    """Output of the publish stage."""

    memory_id: str
    published: bool
    vector_indexed: bool = False
    previous_status: str = ""
