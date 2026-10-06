"""vesma-canon v1.0.0 — write-path canon validator (warn/strict, canon §9).

Self-contained engine copy of the mechanical canon checks: aligned with
vesma-canon ``tools/validate.py`` (schema-pass + ``x-canon-sections`` +
title rules) and canon.md §5 (dates), but with NO runtime dependency on the
canon repo — the checks and the pinned literals live HERE. The schemas'
``x-canon-sections`` annotations stay pinned by the drift test in
``tests/test_checkpoint_canon_envelope.py`` (checkpoint) and by the
``CANON_REQUIRED_SECTIONS`` literal below compared against the same
annotation pattern in the canon repo when present (drift test here).

SCOPE (canon §9 transitional rule — HIGHEST priority): a record WITHOUT
``metadata.canon`` is outside canon scope — never validated, never warned,
never rejected (pre-canon and imported/benchmark rows are legacy, not
violations). Validation applies ONLY to records that carry the envelope.
This holds in BOTH modes: strict never rejects a legacy record retroactively
(canon §10 "new records only").

MODES (``mnemos.canon_mode``, default ``"warn"``):

* warn — every violation is a machine-parseable warning: one
  ``logger.warning`` line per violation AND the violation list attached to
  the stored record as ``metadata["canon_warnings"]`` (list of dicts with
  ``code``/``rule``/``detail``). The write always succeeds.
* strict — violations raise :class:`CanonViolationError` on the CREATE path
  only; update paths are warn-only (ADR-0003 obligation 6: the update of a
  pre-canon record must never fail retroactively).

Warn codes (stable strings — the raw material of the future strict-default
telemetry, canon §9; documented once, HERE):

===================  =====================================================
Code                 Rule
===================  =====================================================
CANON-E-ENVELOPE     schema-invalid envelope: unknown field, wrong type,
                     missing required field, bad per-type extras (canon §2)
CANON-E-SECTION      missing required ``^## <Name>$`` section header for
                     the declared type (canon §3)
CANON-E-STATUS       invalid status enum or status x type conflict —
                     ``resolved`` restricted to task/report (canon §2/§7)
CANON-E-TITLE        empty / too-long (> 80) / multi-line title (canon §3)
CANON-E-LANGUAGE     language outside ru/en (canon §2/§6)
CANON-E-DATE         non-ISO date in body (canon §5)
CANON-E-LINEAGE      schema-invalid ``lineage_marks`` array (wrong type,
                     empty array, unknown ``kind``, non-ISO ``at``, bad
                     ``ref``, unknown key inside a mark) — ADR-0037 Д2
===================  =====================================================
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final

# Envelope enums — pinned to schemas/envelope.schema.json (canon §2, freeze
# §10): the same enum sets the envelope schema validates against. Kept as
# literals here (self-contained copy) and drift-pinned by the tests against
# the canon repo annotation when the sibling checkout exists.
ENVELOPE_TYPES: Final[frozenset[str]] = frozenset({"checkpoint", "task", "decision", "report"})
ENVELOPE_STATUSES: Final[frozenset[str]] = frozenset(
    {"draft", "active", "superseded", "deprecated", "resolved"}
)
# Canon §7: status ``resolved`` is reserved for task and report types;
# checkpoint and decision close via superseded/deprecated instead.
RESOLVED_ALLOWED_TYPES: Final[frozenset[str]] = frozenset({"task", "report"})

#: Per-type REQUIRED extras (canon §2): required for their own type and
#: rejected for every other type (the envelope schema's conditional
#: composition on ``type``). ``session_ref`` additionally allows null; the
#: schema's ``type`` constraint per extra field is encoded in
#: _ENVELOPE_EXTRA_CHECKS below.
ENVELOPE_REQUIRED_EXTRAS: Final[Mapping[str, tuple[str, ...]]] = {
    "checkpoint": ("session_ref",),
    "task": ("owner_slug", "priority", "size"),
    "decision": ("reversible",),
    "report": ("period",),
}

#: Allowed keys per type — required extras plus nothing else inside the
#: envelope (``unevaluatedProperties: false``: a typo must fail loudly).
#: ``lineage_marks`` (canon schema v1.2 lineage_marks.schema.json, connected
#: to envelope.schema.json by ``$ref``) is allowed in EVERY type — ADR-0037
#: Д1: the canon-valid marks array must not fail the engine's own envelope
#: gate (its per-item shape is validated separately by
#: :func:`_lineage_violations`).
ENVELOPE_ALLOWED_KEYS: Final[Mapping[str, frozenset[str]]] = {
    "checkpoint": frozenset(
        {"schema_version", "type", "status", "language", "session_ref", "lineage_marks"}
    ),
    "task": frozenset(
        {
            "schema_version",
            "type",
            "status",
            "language",
            "owner_slug",
            "priority",
            "size",
            "lineage_marks",
        }
    ),
    "decision": frozenset(
        {"schema_version", "type", "status", "language", "reversible", "lineage_marks"}
    ),
    "report": frozenset(
        {"schema_version", "type", "status", "language", "period", "lineage_marks"}
    ),
}

#: Mark kinds (lineage_marks.schema.json ``items.properties.kind.enum``) —
#: the three ratified kinds (labeling-policy v1.1 §8.3; ADR-0037 Д2):
#: ``same-message-other-envelope`` (W3 sighting), ``merged-by-arbiter``
#: (W1 fold), ``split-from`` (W2 split). Extension is a canon-schema PR
#: with owner ratification first (the enum stays a pinned literal here).
LINEAGE_MARK_KINDS: Final[frozenset[str]] = frozenset(
    {"same-message-other-envelope", "merged-by-arbiter", "split-from"}
)

#: The ``at`` ISO-8601 date-time shape (lineage_marks.schema.json
#: ``items.properties.at.pattern``): full stamp with mandatory time part —
#: date-only and truncated forms never match. Kept as a literal (the
#: self-contained validator discipline); drift-pinned by tests against the
#: vendored sibling copy when available.
_LINEAGE_AT_RE: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)

#: Task extra enums (canon §2): priority P0-P3, size XS/S/M/L.
TASK_PRIORITIES: Final[frozenset[str]] = frozenset({"P0", "P1", "P2", "P3"})
TASK_SIZES: Final[frozenset[str]] = frozenset({"XS", "S", "M", "L"})

#: The canon §2 CLIENT-authored envelope types (cascade review SEC P2-2):
#: task/decision/report carry extras the server cannot know
#: (owner_slug/priority/size, reversible, period), so a client-supplied
#: ``metadata.canon`` of one of these types PERSISTS through the
#: client-facing write paths (generic create/update, JSON import) and
#: flows into this validator; only the CHECKPOINT type is server-minted
#: (``save_checkpoint`` is its single minter).
CLIENT_ENVELOPE_TYPES: Final[frozenset[str]] = frozenset({"task", "decision", "report"})


def canon_envelope_is_client_authored(canon: object) -> bool:
    """True when ``canon`` is a CLIENT-authored envelope (canon §2): a
    dict whose ``type`` is one of :data:`CLIENT_ENVELOPE_TYPES`.

    ``False`` for everything else — the checkpoint type (server-minted
    domain) and every malformed shape (non-dict, missing/unknown type):
    those strip on the client-facing paths exactly like forged stamps
    (cascade review SEC P2-2). Shared by the manager create/update
    strips and the JSON import strip so the type rule lives once.
    """
    return isinstance(canon, dict) and canon.get("type") in CLIENT_ENVELOPE_TYPES


def strip_client_lineage_marks(canon: object) -> tuple[object, bool]:
    """Remove a client-supplied ``lineage_marks`` array from an envelope.

    ADR-0037 Д5: marks are server-minted or arbitration-minted (W1-W3,
    the SAME trust class as the checkpoint stamps) — a client-supplied
    array on a generic create/update is forged evidence for the future
    merge-arbiter (input-forgery onto a destructive consolidation), so
    the client-facing paths strip it in the ``CHECKPOINT_STAMP_KEYS``
    discipline. Shared by the manager ``add``/``update`` strips so the
    rule lives once (the same module-locality as
    :func:`canon_envelope_is_client_authored`).

    Returns ``(canon, stripped)``: the envelope WITHOUT the key when one
    was present (a fresh dict, the input never mutated), else the input
    unchanged with ``False``. Non-dict ``canon`` passes through
    unchanged (its caller strips it whole).
    """
    if not (isinstance(canon, dict) and "lineage_marks" in canon):
        return canon, False
    clean = {k: v for k, v in canon.items() if k != "lineage_marks"}
    return clean, True


#: Required body sections per canon type (canon §3). The checkpoint tuple
#: must equal the CHECKPOINT_SECTION_TITLES render order — pinned by tests.
#: Mirrors schemas/*.schema.json ``x-canon-sections``.
CANON_REQUIRED_SECTIONS: Final[Mapping[str, tuple[str, ...]]] = {
    "checkpoint": ("Goals", "Completed", "In Progress", "Decisions", "Context"),
    "task": ("Why", "Acceptance", "Out of scope", "References"),
    "decision": ("Decision", "Why", "Alternatives"),
    "report": ("Summary", "Done", "Verification", "Awaiting owner"),
}

#: Title rules (canon §3): non-empty, single line, <= 80 chars.
#: Same limit as tools/validate.py TITLE_MAX.
TITLE_MAX: Final[int] = 80

#: Machine-parseable warn codes — the single documented home (module
#: docstring lists the human meaning; the values are the contract).
CANON_WARN_CODES: Final[Mapping[str, str]] = {
    "CANON-E-ENVELOPE": "canon §2 — schema-invalid envelope (unknown field, "
    "wrong type, missing required field, bad per-type extras)",
    "CANON-E-SECTION": "canon §3 — missing required '^## <Name>$' section "
    "header for the declared type",
    "CANON-E-STATUS": "canon §2/§7 — invalid status enum or status x type "
    "conflict (resolved restricted to task/report)",
    "CANON-E-TITLE": "canon §3 — empty, too-long (> 80 chars) or multi-line title",
    "CANON-E-LANGUAGE": "canon §2/§6 — language outside ru/en",
    "CANON-E-DATE": "canon §5 — non-ISO-8601 date in body (relative or "
    "truncated form, e.g. '27.09', 'вчера')",
    "CANON-E-LINEAGE": "lineage_marks.schema.json / ADR-0037 Д2 — "
    "schema-invalid lineage_marks array (wrong type, empty, bad mark kind, "
    "bad 'at' stamp, bad 'ref', unknown extra key in a mark)",
}


@dataclass(frozen=True)
class CanonViolation:
    """One canon violation — machine-parseable (canon §9).

    ``code`` is a stable CANON-E-* string (``CANON_WARN_CODES``), ``rule``
    the canon.md section the rule comes from, ``detail`` a human-readable
    description. These dicts land in ``metadata["canon_warnings"]`` and in
    the log line; the telemetry of the future strict-default decision reads
    exactly this shape.
    """

    code: str
    rule: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "rule": self.rule, "detail": self.detail}


class CanonViolationError(ValueError):
    """A canon violation REJECTED a write in strict mode (create path only).

    A ``ValueError`` so every existing channel (REST 422/400, MCP
    dispatch mapping, CLI) maps it without a new error class — the same
    channel discipline as the tag-contract ``TagContractError``.
    Carries the violations so callers can render them.
    """

    def __init__(self, violations: list[CanonViolation]) -> None:
        details = "; ".join(f"{v.code}: {v.detail}" for v in violations)
        super().__init__(f"canon strict mode: write rejected — {details}")
        self.violations = violations


@dataclass
class _CanonReport:
    violations: list[CanonViolation] = field(default_factory=list)

    def add(self, code: str, rule: str, detail: str) -> None:
        self.violations.append(CanonViolation(code=code, rule=rule, detail=detail))


def _envelope_violations(canon: Any) -> _CanonReport:
    """Envelope pass — schemas/envelope.schema.json semantics (canon §2)."""
    report = _CanonReport()
    if not isinstance(canon, dict):
        report.add(
            "CANON-E-ENVELOPE",
            "canon §2",
            f"metadata.canon must be an object, got {type(canon).__name__}",
        )
        return report

    schema_version = canon.get("schema_version")
    if schema_version != "1":
        report.add(
            "CANON-E-ENVELOPE",
            "canon §2",
            f"schema_version must be '1', got {schema_version!r}",
        )

    ctype = canon.get("type")
    if not isinstance(ctype, str) or ctype not in ENVELOPE_TYPES:
        report.add(
            "CANON-E-ENVELOPE",
            "canon §2",
            f"type must be one of {sorted(ENVELOPE_TYPES)}, got {ctype!r}",
        )
        ctype = None

    status = canon.get("status")
    if not isinstance(status, str) or status not in ENVELOPE_STATUSES:
        report.add(
            "CANON-E-STATUS",
            "canon §2",
            f"status must be one of {sorted(ENVELOPE_STATUSES)}, got {status!r}",
        )
        status = None
    elif ctype is not None and status == "resolved" and ctype not in RESOLVED_ALLOWED_TYPES:
        # status x type conflict — resolved restricted to task/report (canon §7).
        report.add(
            "CANON-E-STATUS",
            "canon §7",
            f"status 'resolved' is valid only for types {sorted(RESOLVED_ALLOWED_TYPES)}, "
            f"got type {ctype!r}",
        )

    language = canon.get("language")
    if language not in ("ru", "en"):
        report.add(
            "CANON-E-LANGUAGE",
            "canon §2/§6",
            f"language must be 'ru' or 'en', got {language!r}",
        )

    if ctype is not None:
        required = ENVELOPE_REQUIRED_EXTRAS[ctype]
        for key in required:
            if key not in canon:
                report.add(
                    "CANON-E-ENVELOPE",
                    "canon §2",
                    f"metadata.canon of type {ctype!r} misses required extra {key!r}",
                )
        allowed = ENVELOPE_ALLOWED_KEYS[ctype]
        # UnevaluatedProperties discipline (envelope.schema.json):
        # a typo must fail loudly — every unknown key listed.
        for key in sorted(set(canon) - allowed):
            report.add(
                "CANON-E-ENVELOPE",
                "canon §2",
                f"metadata.canon of type {ctype!r} forbids unknown field {key!r} "
                f"(allowed: {sorted(allowed)})",
            )
        # Per-extra type checks beyond presence.
        if ctype == "checkpoint" and "session_ref" in canon:
            if not isinstance(canon["session_ref"], (str, type(None))):
                report.add(
                    "CANON-E-ENVELOPE",
                    "canon §2",
                    "session_ref must be a string or null, got "
                    f"{type(canon['session_ref']).__name__}",
                )
        elif ctype == "task":
            if "owner_slug" in canon and (
                not isinstance(canon["owner_slug"], str) or len(canon["owner_slug"]) < 1
            ):
                report.add(
                    "CANON-E-ENVELOPE",
                    "canon §2",
                    f"owner_slug must be a non-empty string, got {canon['owner_slug']!r}",
                )
            if "priority" in canon and canon["priority"] not in TASK_PRIORITIES:
                report.add(
                    "CANON-E-ENVELOPE",
                    "canon §2",
                    f"priority must be one of {sorted(TASK_PRIORITIES)}, got {canon['priority']!r}",
                )
            if "size" in canon and canon["size"] not in TASK_SIZES:
                report.add(
                    "CANON-E-ENVELOPE",
                    "canon §2",
                    f"size must be one of {sorted(TASK_SIZES)}, got {canon['size']!r}",
                )
        elif ctype == "decision" and "reversible" in canon:
            if not isinstance(canon["reversible"], bool):
                report.add(
                    "CANON-E-ENVELOPE",
                    "canon §2",
                    f"reversible must be a boolean, got {type(canon['reversible']).__name__}",
                )
        elif (
            ctype == "report"
            and "period" in canon
            and not isinstance(canon["period"], (str, type(None)))
        ):
            report.add(
                "CANON-E-ENVELOPE",
                "canon §2",
                f"period must be a string or null, got {type(canon['period']).__name__}",
            )

    return report


def _title_violations(title: Any) -> list[CanonViolation]:
    """Title rules (canon §3): non-empty, single line, <= 80 chars.

    ``None`` is the engine's "server derives it" state (``Memory.auto_title``
    renders the served title from the content's first line — e.g. every
    ``save_checkpoint`` row mints with ``title=None``), so the served
    derivation is validated instead; flagging ``None`` itself would warn on
    every canonically-minted checkpoint, which warn-mode must not do
    (a valid minted record passes silently).
    """
    if title is None:
        return []
    out: list[CanonViolation] = []
    if not isinstance(title, str):
        return [
            CanonViolation(
                code="CANON-E-TITLE",
                rule="canon §3",
                detail=f"title must be a string or None (server-derived), got "
                f"{type(title).__name__}",
            )
        ]
    if len(title) == 0:
        out.append(
            CanonViolation(code="CANON-E-TITLE", rule="canon §3", detail="title must be non-empty")
        )
        return out
    if len(title) > TITLE_MAX:
        out.append(
            CanonViolation(
                code="CANON-E-TITLE",
                rule="canon §3",
                detail=f"title is {len(title)} chars, max {TITLE_MAX}",
            )
        )
    if "\n" in title or "\r" in title:
        out.append(
            CanonViolation(
                code="CANON-E-TITLE",
                rule="canon §3",
                detail="title must be a single line",
            )
        )
    return out


def _section_violations(content: Any, ctype: str) -> list[CanonViolation]:
    """Required ``^## <Name>$`` headers (canon §3, x-canon-sections)."""
    if not isinstance(content, str):
        return []
    missing = [
        name
        for name in CANON_REQUIRED_SECTIONS[ctype]
        if not re.search(rf"^## {re.escape(name)}\r?$", content, re.MULTILINE)
    ]
    if not missing:
        return []
    listed = ", ".join(f"'## {name}'" for name in missing)
    return [
        CanonViolation(
            code="CANON-E-SECTION",
            rule="canon §3",
            detail=f"body misses required section(s) for type {ctype!r}: {listed}",
        )
    ]


# Canon §5 — the forbidden relative/truncated date forms are checked by
# pattern; the positive ISO forms (full date, full UTC stamp, ISO week) are
# accepted via datetime.fromisoformat / date.fromisoformat / the W-week rule.
_ISO_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
# Relative/truncated forms that must never appear in a canon body (canon §5):
# dotted day.month(/year) dates like "27.09"/"27.09.2026", month names with
# a bare day like "27 сентября", and the words "вчера"/"сегодня"/"позавчера"
# plus their English equivalents and week phrases as STANDALONE words
# (case-insensitive: «Вчера» at sentence start is the same §5 violation).
# The dotted third component allows {1,4} digits so the truncated
# «27.9.2026» (no zero padding) is caught too.
_RELATIVE_DATE_RE = re.compile(
    r"\b\d{1,2}\.\d{1,2}(?:\.\d{1,4})?\b"
    r"|\b(?:вчера|сегодня|позавчера|на прошлой неделе|на следующей неделе)\b"
    r"|\b(?:yesterday|today|tomorrow|last week|next week)\b",
    re.IGNORECASE,
)
# Standalone relative words/phrases, flagged with a detail that names the
# exact token. Case-insensitive (cascade review QA P2-1/SEC P3-2: «Вчера»
# with a capital letter is the same violation, not sentence-start luck).
_STANDALONE_RELATIVE_RE = re.compile(
    r"\b(?:вчера|сегодня|позавчера|на прошлой неделе|на следующей неделе"
    r"|yesterday|today|tomorrow|last week|next week)\b",
    re.IGNORECASE,
)


def _date_violations(content: Any) -> list[CanonViolation]:
    """Canon §5 — dates in the body must be ISO-8601 (full date, full UTC
    stamp or ISO week); relative and truncated forms are forbidden.

    Deliberately NARROW (a false rejection is worse than a pass-through):
    dotted fragments are flagged only when they are UNAMBIGUOUS dates —

    * two components: RU day-first with a month that cannot be a decimal
      fraction (``27.09`` flags; ``1.5``/``6.0``/``12.05``-style ambiguous
      decimals pass);
    * three components: a year component makes it a dotted date
      (``27.09.2026`` flags; ``3.14.3``/``0.0.31`` versions pass);
    * four or more components: always version-like (dotted quads).

    Standalone relative words/phrases (case-insensitive) are flagged with
    a detail naming the exact token.
    """
    if not isinstance(content, str):
        return []
    out: list[CanonViolation] = []
    for match in _RELATIVE_DATE_RE.finditer(content):
        fragment = match.group(0)
        # Standalone relative words (no digits, no dots) — flagged
        # separately below so the detail names the exact word.
        if "." not in fragment:
            continue
        if _is_version_literal(fragment, content, match.start(), match.end()):
            continue
        out.append(
            CanonViolation(
                code="CANON-E-DATE",
                rule="canon §5",
                detail=f"non-ISO date {fragment!r} in body — use YYYY-MM-DD "
                "(full date) or a full UTC stamp",
            )
        )
    # Standalone relative words carry no digits — the version filter does
    # not apply; flagged separately so the detail names the exact word.
    for match in _STANDALONE_RELATIVE_RE.finditer(content):
        out.append(
            CanonViolation(
                code="CANON-E-DATE",
                rule="canon §5",
                detail=f"relative date {match.group(0)!r} in body — a date must be "
                "understandable without context (canon §5)",
            )
        )
    return out


def _is_version_literal(fragment: str, content: str, start: int, end: int) -> bool:
    """True when a dotted fragment is a software version, not a date.

    Conservative disambiguation (cascade review QA P2-1 / issue #437):

    * embedded in a longer dotted/digit chain (``10.0.0.1``, ``1.2.3.4``)
      → version-like, never a date;
    * four or more components: a version (dotted quad / long version);
    * three components: a version (``3.14.3``, ``0.0.31``) UNLESS any
      component is a plausible year (``27.09.2026`` — day-first-with-year
      is a dotted date and stays a §5 violation);
    * two components: a DATE only when UNAMBIGUOUSLY RU day-first —
      first ∈ 13..31 AND second ∈ 1..12 (``27.09``). Everything else
      (``1.5``, ``3.0``, ``6.0``, ``12.05``) is treated as a decimal or
      ambiguous — passes (a false rejection is worse than a pass-through;
      issue #437: ``6.0``/``1.5`` are versions/decimals, not dates).
    """
    parts = fragment.split(".")
    before = content[start - 1 : start] if start > 0 else ""
    after = content[end : end + 1] if end < len(content) else ""
    if before.isdigit() or after.isdigit() or before == "." or after == ".":
        return True
    if len(parts) >= 4:
        return True
    if len(parts) == 3:
        return not any(1900 <= int(part) <= 2099 for part in parts)
    first, second = int(parts[0]), int(parts[1])
    return not (13 <= first <= 31 and 1 <= second <= 12)


def _lineage_violations(canon: Mapping[str, Any]) -> _CanonReport:
    """Per-item lineage_marks pass — schemas/lineage_marks.schema.json as
    connected by ``$ref`` into envelope.schema.json (ADR-0037 Д2).

    Shape (the frozen canon schema, no semantic edits here):
    non-empty array; per mark ``kind`` ∈ :data:`LINEAGE_MARK_KINDS`,
    ``at`` the ISO-8601 date-time pattern's shape, ``ref`` a non-empty
    string WHEN PRESENT, and ``additionalProperties: false`` — no other
    keys. Failures surface as ``CANON-E-LINEAGE`` through the existing
    warn/strict gate (no new enforcement machinery); records without the
    key pass unchanged (zero-diff, canon §9 zero-loss discipline).
    """
    report = _CanonReport()
    marks = canon.get("lineage_marks")
    if marks is None:
        return report  # absent — zero-diff, never validated
    index_path = "metadata.canon.lineage_marks"
    if not isinstance(marks, list):
        report.add(
            "CANON-E-LINEAGE",
            "lineage_marks.schema.json",
            f"{index_path} must be an array, got {type(marks).__name__}",
        )
        return report
    if not marks:
        report.add(
            "CANON-E-LINEAGE",
            "lineage_marks.schema.json",
            f"{index_path} must carry at least one mark (minItems 1)",
        )
        return report
    for i, mark in enumerate(marks):
        at_path = f"{index_path}[{i}]"
        if not isinstance(mark, dict):
            report.add(
                "CANON-E-LINEAGE",
                "lineage_marks.schema.json",
                f"{at_path} must be an object, got {type(mark).__name__}",
            )
            continue
        kind = mark.get("kind")
        if not isinstance(kind, str) or not kind:  # defensive: enum check below
            kind = None
        if kind not in LINEAGE_MARK_KINDS:
            report.add(
                "CANON-E-LINEAGE",
                "lineage_marks.schema.json",
                f"{at_path}.kind must be one of {sorted(LINEAGE_MARK_KINDS)}, "
                f"got {mark.get('kind')!r}",
            )
        at = mark.get("at")
        if not isinstance(at, str) or not _LINEAGE_AT_RE.match(at):
            report.add(
                "CANON-E-LINEAGE",
                "lineage_marks.schema.json",
                f"{at_path}.at must be a full ISO-8601 date-time stamp "
                f"(YYYY-MM-DDThh:mm:ss..., e.g. '2026-10-05T09:30:00Z'), got {at!r}",
            )
        if "ref" in mark:
            ref = mark["ref"]
            if not isinstance(ref, str) or not ref:
                report.add(
                    "CANON-E-LINEAGE",
                    "lineage_marks.schema.json",
                    f"{at_path}.ref must be a non-empty string when present, got {ref!r}",
                )
        extras = sorted(set(mark) - {"kind", "at", "ref"})
        if extras:
            report.add(
                "CANON-E-LINEAGE",
                "lineage_marks.schema.json",
                f"{at_path} forbids unknown field(s) {extras} (additionalProperties: false)",
            )
    return report


def validate_canon_record(
    *,
    content: str,
    title: str | None,
    metadata: Mapping[str, Any],
) -> list[CanonViolation]:
    """Validate ONE record against canon v1.0.0 — violations out, never raises.

    Scope rule FIRST (canon §9 transitional): no ``metadata.canon`` →
    ``[]`` (out of canon scope, legacy is not a violation). A record WITH
    the envelope is checked for: envelope shape (§2), the per-item
    ``lineage_marks`` shape (lineage_marks.schema.json, ADR-0037 Д2),
    required body sections (§3), title rules (§3) and ISO dates in the
    body (§5).
    """
    canon = metadata.get("canon") if isinstance(metadata, Mapping) else None
    if not isinstance(canon, dict):
        return []  # out of canon scope — transitional rule, canon §9

    violations: list[CanonViolation] = []
    report = _envelope_violations(canon)
    violations.extend(report.violations)
    violations.extend(_lineage_violations(canon).violations)

    ctype = canon.get("type")
    if isinstance(ctype, str) and ctype in ENVELOPE_TYPES:
        violations.extend(_section_violations(content, ctype))
    violations.extend(_title_violations(title))
    violations.extend(_date_violations(content))
    return violations


def canon_date_is_iso(token: str) -> bool:
    """True when ``token`` parses as a full ISO-8601 date or UTC stamp.

    Exported for tests and the future telemetry tooling: the positive form
    of the canon §5 check (``2026-09-25``, ``2026-09-25T14:30:00Z``).
    ISO weeks (``2026-W38``) count as valid periods.
    """
    if _ISO_WEEK_RE.match(token):
        return True
    try:
        datetime.fromisoformat(token.replace("Z", "+00:00"))
        return True
    except ValueError:
        pass
    try:
        date.fromisoformat(token)
        return True
    except ValueError:
        return False
