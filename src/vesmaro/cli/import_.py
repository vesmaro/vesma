"""``mnemos import`` — restore memories from a JSON or SQLite export.

Two modes:

* **merge** (default, idempotent) — insert memories whose ID is absent;
  skip existing IDs (or update with ``--overwrite`` — JSON format only;
  the SQLite path always skips existing IDs). Projects are merged
  (create if absent, update paths if changed). Vectors are regenerated
  for published memories.
* **restore** (destructive) — wipe all memories, vectors, and projects,
  then import. Requires ``--confirm``. For SQLite format, the raw DB
  files are replaced (after an optional backup); because a file swap
  lands rows without any gate pass, a post-restore gate sweep (#259)
  re-runs the danger gate over every restored row — refusals are
  demoted RAW (zero-loss, invisible) exactly as on the other paths.

``--dry-run`` validates the export without writing.

Import validation (ArchCom 2026-07-17 federation contract §3.1, issue #86):

* **Content** — max length (configurable, default 1 MiB), no control
  characters except ``\\n`` and ``\\t``, valid UTF-8.
* **Tags** — reuses ``validate_tag_contract``; max 32 tags, max 128 chars
  per tag.
* **Title** — max 256 chars, no control characters.
* **Prompt-injection patterns** — ``[INST]``, ``<|im_start|>``,
  ``"ignore previous instructions"``, ``"system:"``, ``"</s>"`` — logged
  at WARNING (NOT blocked; legitimate content may discuss injection).
* **Danger gate (ADR-0019 Phase D, #166)** — validation alone does not
  gate VISIBILITY: every imported row also passes the Phase A
  publication gate (``path=federation-import``) before the write. A
  positive danger signal or scanner error stores the row RAW with
  ``pipeline_state=NULL`` (zero-loss, invisible); a clean row keeps the
  peer's status and joins the refine queue (``pipeline_state=pending``).
  The SQLite RESTORE path is the one exception to "before the write":
  it swaps raw DB files (#259), so the gate runs as an immediate
  post-restore sweep over every restored row — same verdicts, same
  warnings, applied in place (audited as ``path=federation-restore``).
* **Schema drift** — fields not in the current ``Memory`` schema are
  rejected with a field-level error.
"""

from __future__ import annotations

import functools
import io
import json
import logging
import tarfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vesmaro.canon_validate import canon_envelope_is_client_authored
from vesmaro.cli.export import (
    CompressMode,
    ExportFormat,
    decompress,
    decrypt,
    detect_sqlite_snapshot,
    is_encrypted,
    parse_json_export,
    restore_sqlite_snapshot,
)
from vesmaro.danger_detectors import (
    PROMPT_INJECTION_PATTERNS as _PROMPT_INJECTION_PATTERNS,
)
from vesmaro.models import (
    CHECKPOINT_STAMP_KEYS,
    Memory,
    MemoryStatus,
    PipelineState,
)

if TYPE_CHECKING:
    from vesmaro.manager import MemoryManager

logger = logging.getLogger(__name__)

__all__ = [
    "ImportMode",
    "ImportResult",
    "gate_imported_memory",
    "run_import",
    "validate_import_payload",
    "validate_import_record",
]


# ── Validation constants ───────────────────────────────────────────────────────

#: Maximum content length accepted on import (chars). Configurable via
#: ``Settings.import_validation.max_content_chars`` (see config.py); the
#: CLI uses this default when the manager does not override it.
DEFAULT_MAX_CONTENT_CHARS: int = 1_048_576  # 1 MiB

#: Maximum tag count per record.
MAX_TAGS: int = 32

#: Maximum tag length (chars).
MAX_TAG_LEN: int = 128

#: Maximum title length (chars).
MAX_TITLE_LEN: int = 256

#: Allowed control characters in content (everything else in the C0/C1
#: ranges is rejected). Stored as a frozenset for O(1) lookup.
_ALLOWED_CONTROL_CHARS: frozenset[int] = frozenset({ord("\n"), ord("\t")})

# Prompt-injection patterns: ``_PROMPT_INJECTION_PATTERNS`` (imported
# above) is the single source of truth shared with the ADR-0019 Phase A
# publication gate (vesmaro.danger_detectors). Here they stay a WARN-only
# screen — content may legitimately discuss prompt injection (security
# research, runbooks, training material).


# ── Validation report ─────────────────────────────────────────────────────────


@dataclass
class ImportValidationReport:
    """Aggregate result of validating a payload (used by dry-run + real run)."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    records_validated: int = 0
    records_with_warnings: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "records_validated": self.records_validated,
            "records_with_warnings": self.records_with_warnings,
        }


# ── Per-record validation ─────────────────────────────────────────────────────


def _has_disallowed_control_chars(text: str) -> bool:
    """Return True if ``text`` contains a C0/C1 control char not in the allow-list."""
    for ch in text:
        o = ord(ch)
        # C0: 0x00-0x1F, C1: 0x7F-0x9F. Allow \n (0x0A) and \t (0x09).
        if (o < 0x20 or 0x7F <= o <= 0x9F) and o not in _ALLOWED_CONTROL_CHARS:
            return True
    return False


def validate_import_record(
    entry: dict[str, Any],
    *,
    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
    memory_id: str | None = None,
) -> tuple[list[str], list[str]]:
    """Validate a single memory entry from an export payload.

    Args:
        entry: The JSON dict for one memory record.
        max_content_chars: Maximum allowed content length.
        memory_id: Optional id hint for error messages (falls back to
            ``entry.get("id")``).

    Returns:
        ``(errors, warnings)`` — ``errors`` are fatal (record is rejected),
        ``warnings`` are non-fatal (prompt-injection mentions, etc.).
        Field-level error messages of the form
        ``"<id>:<field>: <reason>"`` so the caller can surface them
        directly.
    """
    errors: list[str] = []
    warnings: list[str] = []
    rid = memory_id or entry.get("id", "?")

    # ── Schema drift: reject unknown fields ──────────────────────────────
    # Build the allowed field set from the Memory model. We use a cached
    # frozenset computed once (lazy) to avoid re-reading the model on every
    # record. The model fields are stable across a process.
    allowed_fields = _memory_field_names()
    unknown = [k for k in entry if k not in allowed_fields]
    if unknown:
        errors.append(f"{rid}:schema: unknown fields: {sorted(unknown)}")

    # ── Content ───────────────────────────────────────────────────────────
    content = entry.get("content")
    if content is None:
        errors.append(f"{rid}:content: missing required field 'content'")
    elif not isinstance(content, str):
        errors.append(f"{rid}:content: must be a string, got {type(content).__name__}")
    else:
        if len(content) > max_content_chars:
            errors.append(
                f"{rid}:content: exceeds max length ({len(content)} > {max_content_chars} chars)"
            )
        # UTF-8 validity: a Python str is always valid Unicode, but the
        # caller may have decoded with errors='replace' — we re-encode to
        # UTF-8 and back to catch surrogate / lone surrogate issues.
        try:
            content.encode("utf-8").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError) as exc:
            errors.append(f"{rid}:content: invalid UTF-8: {exc}")
        if _has_disallowed_control_chars(content):
            errors.append(
                f"{rid}:content: contains disallowed control characters "
                "(only \\n and \\t permitted)"
            )
        # Prompt-injection scan — warn only.
        lower = content.lower()
        for pname, pat in _PROMPT_INJECTION_PATTERNS:
            if pat.lower() in lower:
                warnings.append(
                    f"{rid}:content: prompt-injection pattern '{pname}' detected "
                    "(logged, not blocked — content may legitimately discuss injection)"
                )

    # ── Title ─────────────────────────────────────────────────────────────
    title = entry.get("title")
    if title is not None:
        if not isinstance(title, str):
            errors.append(f"{rid}:title: must be a string, got {type(title).__name__}")
        else:
            if len(title) > MAX_TITLE_LEN:
                errors.append(
                    f"{rid}:title: exceeds max length ({len(title)} > {MAX_TITLE_LEN} chars)"
                )
            if _has_disallowed_control_chars(title):
                errors.append(f"{rid}:title: contains disallowed control characters")

    # ── Tags ──────────────────────────────────────────────────────────────
    tags = entry.get("tags", [])
    if not isinstance(tags, list):
        errors.append(f"{rid}:tags: must be a list, got {type(tags).__name__}")
    else:
        if len(tags) > MAX_TAGS:
            errors.append(f"{rid}:tags: exceeds max count ({len(tags)} > {MAX_TAGS})")
        for t in tags:
            if not isinstance(t, str):
                errors.append(f"{rid}:tags: every tag must be a string, got {type(t).__name__}")
                break
            if len(t) > MAX_TAG_LEN:
                errors.append(
                    f"{rid}:tags: tag exceeds max length ({len(t)} > {MAX_TAG_LEN}): {t[:32]!r}…"
                )
                break
        # Reuse the tag contract validator (lax mode → patchable errors
        # become warnings; strict mode raises which we surface as error).
        if tags and all(isinstance(t, str) for t in tags):
            from vesmaro.models import TagContractError, validate_tag_contract

            try:
                validate_tag_contract(tags, strict=True)
            except TagContractError as exc:
                errors.append(f"{rid}:tags: tag contract violation: {exc}")

    return errors, warnings


@functools.lru_cache(maxsize=1)
def _memory_field_names() -> frozenset[str]:
    """Return the set of allowed Memory model field names (cached)."""
    return frozenset(Memory.model_fields.keys())


# ── Payload-level validation ──────────────────────────────────────────────────


def validate_import_payload(
    payload: dict[str, Any],
    *,
    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
) -> ImportValidationReport:
    """Validate every record in a parsed JSON export payload.

    Does NOT write to the store. Used by ``--dry-run`` and as the first
    pass of a real import (so we reject the whole batch on schema drift
    rather than writing some records and aborting).
    """
    report = ImportValidationReport(valid=True)
    memories: list[dict[str, Any]] = payload.get("memories", [])
    if not isinstance(memories, list):
        report.errors.append("payload: 'memories' is not a list")
        report.valid = False
        return report

    for entry in memories:
        if not isinstance(entry, dict):
            report.errors.append(
                f"payload: memory entry must be an object, got {type(entry).__name__}"
            )
            report.valid = False
            continue
        report.records_validated += 1
        errs, warns = validate_import_record(entry, max_content_chars=max_content_chars)
        if errs:
            report.errors.extend(errs)
            report.valid = False
        if warns:
            report.warnings.extend(warns)
            report.records_with_warnings += 1

    return report


# ── Enums / result ────────────────────────────────────────────────────────────


class ImportMode:
    MERGE = "merge"
    RESTORE = "restore"


@dataclass
class ImportResult:
    mode: str
    dry_run: bool
    imported: int = 0
    skipped: int = 0
    updated: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    format_version: str | None = None
    mnemos_version: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "dry_run": self.dry_run,
            "imported": self.imported,
            "skipped": self.skipped,
            "updated": self.updated,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "format_version": self.format_version,
            "mnemos_version": self.mnemos_version,
        }


# ── Helpers ───────────────────────────────────────────────────────────────────


def _detect_format(data: bytes) -> ExportFormat:
    """Detect whether the payload is a SQLite snapshot or JSON."""
    if detect_sqlite_snapshot(data):
        return ExportFormat.SQLITE
    # Could be JSON (possibly compressed+encrypted); caller handles those layers.
    return ExportFormat.JSON


def _unwrap_payload(
    data: bytes,
    *,
    compress: CompressMode | None,
    encrypted: bool,
    passphrase: str | None,
) -> bytes:
    """Reverse encryption + compression to get the raw payload bytes."""
    if encrypted:
        if passphrase is None:
            raise ValueError("Payload is encrypted but no passphrase was provided.")
        data = decrypt(data, passphrase)
    if compress is not None and compress != CompressMode.NONE:
        data = decompress(data, compress)
    return data


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _memory_from_export(entry: dict[str, Any]) -> Memory:
    """Reconstruct a Memory from an export dict entry."""
    # created_at / updated_at come back as ISO strings from JSON.
    payload = dict(entry)
    if "created_at" in payload and isinstance(payload["created_at"], str):
        payload["created_at"] = _coerce_datetime(payload["created_at"]) or datetime.now()
    if "updated_at" in payload and isinstance(payload["updated_at"], str):
        payload["updated_at"] = _coerce_datetime(payload["updated_at"]) or datetime.now()
    # strict_tags is a transient field; never set it on import.
    payload.pop("strict_tags", None)
    return Memory.model_validate(payload)


def _strip_imported_canon_line_keys(memory: Memory) -> list[str]:
    """Strip canon-line SERVER-MINTED keys from an imported row's metadata
    (cascade review SEC P3-5) — in place; returns the stripped key names.

    An export file is client-supplied data: rows arrive with their
    ``metadata`` verbatim, so checkpoint stamps (``checkpoint_agent`` /
    ``checkpoint_session`` / ``checkpoint_dedup_key``), a CHECKPOINT-type
    ``canon`` envelope and ``canon_warnings`` (gate-owned) would land as
    if the server had minted them — a forged ``checkpoint_dedup_key``
    could then satisfy a later genuine ``save_checkpoint`` dedup, and a
    forged envelope puts a row inside canon scope it never earned.

    Kept deliberately: a client-authored task/decision/report envelope
    (canon §2, cascade review SEC P2-2 — same type rule as the manager
    create/update strips) — that is peer/client data, not a stamp.

    Bypassed only by ``--trusted-restore`` (operator asserts the export
    is a trusted self-backup).
    """
    meta = memory.metadata
    drop: set[str] = {k for k in CHECKPOINT_STAMP_KEYS if k in meta and k != "canon"}
    if "canon" in meta and not canon_envelope_is_client_authored(meta["canon"]):
        drop.add("canon")
    if "canon_warnings" in meta:
        drop.add("canon_warnings")
    if drop:
        memory.metadata = {k: v for k, v in meta.items() if k not in drop}
    return sorted(drop)


# ── Federation-import danger gate (#166, ADR-0019 Phase D) ────────────────────


def gate_imported_memory(
    mgr: MemoryManager, memory: Memory, *, path: str = "federation-import"
) -> tuple[Memory, bool]:
    """Run the Phase A danger gate over one imported/federated row (#166).

    The federated write paths (:func:`run_sync_import` in cli/sync.py and
    :func:`_import_json` / :func:`_import_sqlite` here) persist peer-status
    rows DIRECTLY through
    ``sqlite.save`` — a PUBLISHED record arriving from peering never met
    the publication gate, so a secret or injection payload could land
    visible. This helper routes every such row through the SAME single
    gate point the server uses (``MemoryManager._publish_gate_detection``
    — :func:`vesmaro.danger_detectors.detect` over the served projection
    and the title, fail-closed, audited as
    ``publish gate: … path=<path>``) and applies the verdict
    in place:

    * positive signal OR scanner error (fail-closed) → ``status=RAW`` +
      ``pipeline_state=None``: stored zero-loss, invisible, and OUTSIDE
      the refine intake (danger-class content must not auto-refine back
      into visibility);
    * clean → the peer's status is kept and a NULL ``pipeline_state`` is
      stamped ``pending`` so the local refine queue picks the row up.
      Non-NULL peer states (``refined``, ``quarantined``, …) are never
      clobbered — a peer's quarantine verdict must survive import.

    ``path`` is the audit discriminator passed through to
    ``_publish_gate_detection``: ``federation-import`` (the default) for
    the sync/JSON/merge import surfaces, ``federation-restore`` for the
    post-restore sweep (:func:`_gate_restored_rows`, #259). The refusal
    WARNING strings on every surface keep the historical
    ``federation-import`` label by convention.

    Returns ``(memory, admitted)`` — the mutated row (the caller saves
    it) and whether the gate admitted the peer's visibility. Never
    raises on a detector verdict: ``detect`` returns scanner errors
    inside its result.
    """
    detection = type(mgr)._publish_gate_detection(memory, path=path)
    if detection.error is not None or detection.positive:
        memory.status = MemoryStatus.RAW
        memory.pipeline_state = None
        return memory, False
    if memory.pipeline_state is None:
        memory.pipeline_state = PipelineState.PENDING
    return memory, True


# ── JSON import ───────────────────────────────────────────────────────────────


def _import_json(
    mgr: MemoryManager,
    payload: dict[str, Any],
    *,
    mode: str,
    overwrite: bool,
    dry_run: bool,
    trusted_restore: bool = False,
) -> ImportResult:
    result = ImportResult(mode=mode, dry_run=dry_run)
    result.format_version = payload.get("format_version")
    result.mnemos_version = payload.get("mnemos_version")

    memories: list[dict[str, Any]] = payload.get("memories", [])
    projects: list[dict[str, Any]] = payload.get("projects", [])

    # ── Validation pass (runs for both dry-run and real import) ──────────
    # Reject the whole batch on schema drift / contract violations rather
    # than writing some records and aborting. The validation report is
    # attached to the ImportResult so callers can surface field-level
    # errors. Prompt-injection warnings are added to result.warnings.
    validation = validate_import_payload(payload)
    if not validation.valid:
        result.errors.extend(validation.errors)
        # In dry-run we still return a report dict; in real mode we abort.
        if not dry_run:
            return result
    result.warnings.extend(validation.warnings)

    # ── Dry-run short-circuit ────────────────────────────────────────────
    # Dry-run validates and reports; it does NOT write. The summary carries
    # the validation report so the caller can decide whether to proceed.
    if dry_run:
        result.imported = validation.records_validated
        result.warnings.append(
            f"Dry-run: validated {validation.records_validated} records, "
            f"{len(validation.errors)} errors, "
            f"{validation.records_with_warnings} records with warnings."
        )
        return result

    # ── Restore mode: wipe first (only when not dry-run) ────────────────
    if mode == ImportMode.RESTORE and not dry_run:
        mgr.sqlite.wipe_all()
        mgr.sqlite.wipe_projects()
        mgr.vectors.wipe()

    # ── Projects ────────────────────────────────────────────────────────
    for p in projects:
        existing = mgr.sqlite.get_project_by_name(p.get("name", ""))
        if existing is None:
            if dry_run:
                result.imported += 0  # projects counted separately
            else:
                from vesmaro.models import Project

                proj = Project(
                    id=p.get("id", ""),
                    name=p["name"],
                    description=p.get("description", ""),
                    paths=p.get("paths", []),
                    created_at=_coerce_datetime(p.get("created_at")) or datetime.now(),
                    updated_at=_coerce_datetime(p.get("updated_at")) or datetime.now(),
                )
                if not proj.id:
                    import uuid

                    proj.id = str(uuid.uuid4())
                mgr.sqlite.save_project(proj)
        else:
            # Merge: update paths if changed.
            new_paths = p.get("paths", [])
            if new_paths and new_paths != existing.paths and not dry_run:
                existing.paths = new_paths
                mgr.sqlite.save_project(existing)

    # ── Memories ─────────────────────────────────────────────────────────
    # #254 review P2 — federation-origin stamp. In MERGE mode the rows
    # arriving here are FEDERATED (a peer's records joining this store):
    # each is stamped ``federated_origin`` so awareness
    # (``vesmaro.awareness.is_delta_excluded``) keeps cross-operator rows
    # out of presence/delta — otherwise operator B's imports read as
    # LOCAL neighbors under the "observed" header (CWE-359). RESTORE
    # mode is a SELF-restore of this operator's own backup (rows were
    # locally minted) and is deliberately NOT stamped. A first-class
    # ``origin=`` column remains a tracker item.
    stamp_federated = mode != ImportMode.RESTORE
    for entry in memories:
        try:
            memory = _memory_from_export(entry)
        except Exception as exc:
            result.errors.append(f"memory {entry.get('id', '?')}: {exc}")
            continue
        if stamp_federated and not dry_run:
            memory.metadata = {**memory.metadata, "federated_origin": "json-import"}
        # ── Cascade review SEC P3-5: strip canon-line server-minted ──────
        # keys (checkpoint stamps, checkpoint-type canon envelope,
        # gate-owned canon_warnings) from imported metadata — the export
        # is client-supplied data and must not land stamps the server
        # never minted. Client task/decision/report envelopes persist
        # (canon §2). ``trusted_restore`` (the --trusted-restore flag)
        # bypasses the strip: the operator asserts a trusted self-backup.
        if not trusted_restore and not dry_run:
            stripped_keys = _strip_imported_canon_line_keys(memory)
            if stripped_keys:
                result.warnings.append(
                    f"memory {memory.id}: stripped canon-line server-minted keys "
                    f"(import is untrusted by default; --trusted-restore keeps them): "
                    f"keys={stripped_keys}"
                )

        existing_mem = mgr.sqlite.get(memory.id)
        if existing_mem is not None:
            if overwrite and not dry_run:
                was_embedded = existing_mem.status == MemoryStatus.PUBLISHED
                memory, admitted = gate_imported_memory(mgr, memory)
                mgr.sqlite.save(memory)
                result.updated += 1
                if not admitted:
                    result.warnings.append(
                        f"memory {memory.id}: stored RAW — federation-import "
                        "danger-gate refusal (zero-loss, invisible)"
                    )
                    if was_embedded:
                        # N1 demotion hygiene (mirrors MemoryManager.update):
                        # the formerly-published row's embed is stale now.
                        # Non-fatal — the resolve-time status guard already
                        # keeps the row out of search results.
                        try:
                            mgr.vectors.delete(memory.id)
                        except Exception as exc:
                            logger.warning(
                                "stale embed delete on import refusal (non-fatal): %s", exc
                            )
                _reembed(mgr, memory)
            else:
                result.skipped += 1
            continue

        # New memory
        if not dry_run:
            memory, admitted = gate_imported_memory(mgr, memory)
            mgr.sqlite.save(memory)
            if not admitted:
                result.warnings.append(
                    f"memory {memory.id}: stored RAW — federation-import "
                    "danger-gate refusal (zero-loss, invisible)"
                )
            _reembed(mgr, memory)
        result.imported += 1

    return result


def _reembed(mgr: MemoryManager, memory: Memory) -> None:
    """Re-embed a published memory into the vector store (best-effort).

    ADR-0019 B2b (N1 review #163): routes through the SINGLE embedding
    write point (``MemoryManager.upsert_embedding``) so the upsert
    stamps the ``content_hash`` freshness key and emits the
    ``embed_upserted`` audit event — a raw ``vectors.upsert`` here would
    leave an unhealable, unhashed embed behind.
    """
    if memory.status != MemoryStatus.PUBLISHED:
        return
    try:
        mgr.upsert_embedding(memory)
    except Exception as exc:
        # Re-embedding is best-effort; the memory is still persisted.
        logger.warning("re-embedding failed for memory %s: %s", memory.id, exc)


def _gate_restored_rows(mgr: MemoryManager, result: ImportResult) -> None:
    """Post-restore danger-gate sweep (#259, ADR-0019 Phase D).

    ``restore_sqlite_snapshot`` swaps the raw DB files, so the snapshot's
    rows land in the live store WITHOUT ever meeting the publication
    gate — a tampered snapshot could restore PUBLISHED danger rows
    straight into visibility (the #259 trust-boundary hole; same class
    as the JSON gate #166 and the merge gate #245). Unlike those
    pre-write gates, a file swap can only be gated post-hoc: this sweep
    walks every restored row through the SAME single gate point
    (``gate_imported_memory``, idempotent — it judges content danger,
    not write-path origin) and persists the verdict in place through
    ``mgr.sqlite.save`` — the tested write path that keeps the FTS5
    external-content index consistent (see the merge-branch comment in
    :func:`_import_sqlite` about why raw SQL breaks it).

    RESTORE remains a SELF-restore (#254): rows are deliberately NOT
    stamped ``federated_origin`` — this sweep changes visibility
    enforcement only, not origin semantics.

    Column preservation (verified, deliberate): post-swap rows go
    through ``save()``'s UPDATE branch, which omits the ``workflow_*``
    and ``rewrite_*`` families from SET — snapshot-planted workflow
    locks and rewrite provenance therefore survive the sweep unchanged
    (the merge branch's DROP/NULL note applies only to new-row INSERTs).
    A malicious snapshot can still mint C10 rewrite columns via restore;
    accepted under the #254 self-restore position — visibility
    enforcement is this sweep's scope.
    """
    # The store's aggregate caches (tags/counts/graph) still describe
    # the PRE-swap DB — close() drops only the connection.
    mgr.sqlite._invalidate_caches()
    ungated = 0
    try:
        total = mgr.sqlite.count()
        # Raw rows through the STORE'S OWN connection — NOT list_all:
        # list_all materialises every row via ``_row_to_memory`` inside
        # one list comprehension, so a single corrupt row (invalid JSON
        # in a column, plantable by a tampered snapshot with ONE UPDATE)
        # raises before ANY row is returned and the whole store goes
        # ungated — fail-open, the exact #259 threat (review P2). The
        # raw fetch + per-row ``_row_to_memory`` below (the merge
        # branch's idiom) isolates the failure to the corrupt row.
        raw_rows = mgr.sqlite._get_conn().execute("SELECT * FROM memories").fetchall()
    except Exception as exc:
        # Connection/store-level failure only — no row was gated.
        result.errors.append(f"post-restore gate sweep: store unreadable: {exc}")
        return
    for row in raw_rows:
        # Label errors by id when the column is readable — a corrupt
        # snapshot row may not even carry one (keys-set idiom mirrors
        # the merge branch).
        cols = set(row.keys())
        mem_id = row["id"] if "id" in cols else "?"
        try:
            memory = mgr.sqlite._row_to_memory(row)
            # Status-based, mirroring the JSON overwrite branch: a
            # PUBLISHED restored row was embedded when the snapshot was
            # taken (the tar may carry vectors.db — the embed is live
            # after the swap).
            was_embedded = memory.status == MemoryStatus.PUBLISHED
            memory, admitted = gate_imported_memory(mgr, memory, path="federation-restore")
            mgr.sqlite.save(memory)
            if not admitted:
                result.warnings.append(
                    f"memory {memory.id}: stored RAW — federation-import "
                    "danger-gate refusal (zero-loss, invisible)"
                )
                if was_embedded:
                    # N1 demotion hygiene (mirrors the JSON refusal path
                    # and MemoryManager.update): the restored embed is
                    # stale the moment the row is demoted. Non-fatal —
                    # the resolve-time status guard already keeps the
                    # row out of search results.
                    try:
                        mgr.vectors.delete(memory.id)
                    except Exception as exc:
                        logger.warning("stale embed delete on restore refusal (non-fatal): %s", exc)
            else:
                _reembed(mgr, memory)
        except Exception as exc:
            # Per-row isolation, mirroring the merge loop: one bad row
            # (unreadable columns, invalid enum, failing save) is
            # recorded and skipped — every servable row is still gated.
            # An unreadable row is unservable by ANY canonical read path
            # (get/list/search all reconstruct via ``_row_to_memory``),
            # so it cannot leak into issuance.
            ungated += 1
            result.errors.append(f"memory {mem_id}: {exc}")
            continue
    if ungated:
        result.warnings.append(
            f"post-restore gate sweep: {ungated} of {total} rows left ungated (unreadable)"
        )
    mgr.sqlite._invalidate_caches()


# ── SQLite import ─────────────────────────────────────────────────────────────


def _import_sqlite(
    mgr: MemoryManager,
    snapshot: bytes,
    *,
    mode: str,
    dry_run: bool,
    backup_dir: Path | None,
) -> ImportResult:
    result = ImportResult(mode=mode, dry_run=dry_run)

    # Validate the snapshot by reading the memory count from it.
    with tarfile.open(fileobj=io.BytesIO(snapshot), mode="r:gz") as tar:
        mnemos_member = None
        for m in tar.getmembers():
            if m.name == "mnemos.db":
                mnemos_member = m
                break
        if mnemos_member is None:
            result.errors.append("SQLite snapshot missing mnemos.db.")
            return result
        extracted = tar.extractfile(mnemos_member)
        if extracted is None:
            result.errors.append("SQLite snapshot mnemos.db payload is empty.")
            return result
        db_bytes = extracted.read()

    # Count memories in the snapshot (works for both dry-run and real run).
    import sqlite3
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        tf.write(db_bytes)
        tmp_path = Path(tf.name)
    try:
        conn = sqlite3.connect(str(tmp_path))
        row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        snapshot_count = int(row[0]) if row else 0
        conn.close()
    finally:
        tmp_path.unlink(missing_ok=True)

    if dry_run:
        result.imported = snapshot_count
        result.warnings.append(
            f"SQLite dry-run: snapshot read, {snapshot_count} memories counted, no files replaced."
        )
        return result

    if mode == ImportMode.RESTORE:
        restore_sqlite_snapshot(mgr, snapshot, backup_current=backup_dir)
        # #259 (ADR-0019 Phase D): the swap above landed the snapshot's
        # rows in the live store without any gate pass — sweep every
        # restored row through the SAME publication gate as the
        # JSON/merge imports BEFORE the result is reported. RESTORE
        # rows stay unstamped (#254 self-restore semantics); the sweep
        # enforces visibility only.
        _gate_restored_rows(mgr, result)
        result.imported = snapshot_count
    else:
        # Merge for SQLite = read memories from snapshot, insert missing.
        # #245 (ADR-0019 Phase D): this is a federated write path — every
        # row passes the SAME publication gate as the JSON/sync imports
        # (``gate_imported_memory``, path=federation-import) before the
        # store write, and persists through ``mgr.sqlite.save`` — the
        # single tested write path, which keeps the FTS5 external-content
        # index consistent (raw INSERT OR REPLACE fires the insert trigger
        # with a new rowid while the FTS table still references the old
        # one → ``missing row N from content table``).
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            tf.write(db_bytes)
            snap_path = Path(tf.name)
        try:
            conn = sqlite3.connect(str(snap_path))
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT * FROM memories").fetchall()
                for row in rows:
                    # Label errors by id when the column is readable — a
                    # corrupt snapshot row may not even carry one. The
                    # keys-set idiom mirrors ``_row_to_memory``; plain
                    # ``"id" in row`` would test VALUES, not columns.
                    cols = set(row.keys())
                    mem_id = row["id"] if "id" in cols else "?"
                    try:
                        if mgr.sqlite.get(mem_id) is None:
                            # Reconstruct a Memory from the snapshot row. The
                            # store's own ``_row_to_memory`` is schema-drift safe:
                            # every post-day-1 column (pipeline_state,
                            # processed_at, rewrite_*, …) is read via a defaulted
                            # ``_get`` (missing key → default, NULL → None), so an
                            # older snapshot DB reconstructs fine. Two columns
                            # are deliberately DROPPED on this path: a locked
                            # peer row re-imports unlocked (``save()`` never
                            # writes workflow_status/locked_by/locked_at —
                            # ``set_workflow_status`` is the sole writer, a
                            # state-machine bypass guard), and rewrite
                            # provenance is nulled (``save()`` without the
                            # trusted flag stores NULL in rewrite_source/
                            # rewrite_session/rewrite_event_key per the C10
                            # anti-forgery gate, so merge-imported rows never
                            # mint rewrite quota).
                            memory = mgr.sqlite._row_to_memory(row)
                            # #254 review P2 — this MERGE branch is a
                            # federated write path (see the gate comment
                            # above): stamp the row so awareness keeps the
                            # peer's rows out of local presence/delta
                            # (CWE-359). The RESTORE branch above replaces
                            # the whole DB (self-restore) and is unstamped.
                            memory.metadata = {
                                **memory.metadata,
                                "federated_origin": "sqlite-merge-import",
                            }
                            memory, admitted = gate_imported_memory(mgr, memory)
                            mgr.sqlite.save(memory)
                            if not admitted:
                                result.warnings.append(
                                    f"memory {memory.id}: stored RAW — federation-import "
                                    "danger-gate refusal (zero-loss, invisible)"
                                )
                            _reembed(mgr, memory)
                            result.imported += 1
                        else:
                            result.skipped += 1
                    except Exception as exc:
                        # Per-row isolation (review P2-1): one malformed row
                        # (invalid status/pipeline_state enum, malformed JSON
                        # in tags/metadata/derived_from/filter_stats,
                        # non-ISO timestamps, NULL id → pydantic
                        # ValidationError, pre-#32 schema → IndexError) must
                        # not abort the whole merge — earlier rows are
                        # already committed (every ``save()`` commits).
                        # Mirrors the JSON path (``_import_json``): the row
                        # lands in ``result.errors``, counts as neither
                        # imported nor skipped, and the loop continues.
                        result.errors.append(f"memory {mem_id}: {exc}")
                        continue
            finally:
                conn.close()
            mgr.sqlite._invalidate_caches()
        finally:
            snap_path.unlink(missing_ok=True)

    return result


# ── Orchestrator ──────────────────────────────────────────────────────────────


def run_import(
    mgr: MemoryManager,
    source: Path,
    *,
    mode: str = ImportMode.MERGE,
    overwrite: bool = False,
    confirm: bool = False,
    dry_run: bool = False,
    passphrase: str | None = None,
    passphrase_file: Path | None = None,
    compress: CompressMode | None = None,
    backup_dir: Path | None = None,
    trusted_restore: bool = False,
) -> ImportResult:
    """Import an export file. See module docstring for mode semantics."""
    if mode not in (ImportMode.MERGE, ImportMode.RESTORE):
        raise ValueError(f"Unknown import mode: {mode}")

    if mode == ImportMode.RESTORE and not dry_run and not confirm:
        result = ImportResult(mode=mode, dry_run=dry_run)
        result.errors.append("Restore mode requires --confirm (it wipes all existing data).")
        return result

    if not source.exists():
        result = ImportResult(mode=mode, dry_run=dry_run)
        result.errors.append(f"Source file not found: {source}")
        return result

    raw = source.read_bytes()

    # ── Detect encryption ────────────────────────────────────────────────
    encrypted = is_encrypted(raw)
    if encrypted:
        if passphrase is None and passphrase_file is not None:
            passphrase = passphrase_file.read_text(encoding="utf-8").strip()
        if passphrase is None:
            result = ImportResult(mode=mode, dry_run=dry_run)
            result.errors.append("File is encrypted but no passphrase provided.")
            return result

    # ── Detect format BEFORE compression unwrapping ──────────────────────
    # A SQLite snapshot is a tar.gz — it starts with the gzip magic (1f 8b).
    # If we auto-decompressed it first, we'd corrupt the format detection.
    # So: detect SQLite on the raw (possibly encrypted) bytes; only fall
    # through to JSON + compression handling if it's not a SQLite snapshot.
    if not encrypted and detect_sqlite_snapshot(raw):
        return _import_sqlite(mgr, raw, mode=mode, dry_run=dry_run, backup_dir=backup_dir)

    # ── Detect compression (JSON path) ──────────────────────────────────
    # If the caller did not specify, try to auto-detect by magic bytes.
    if compress is None and not encrypted:
        compress = CompressMode.GZIP if raw[:2] == b"\x1f\x8b" else CompressMode.NONE

    try:
        payload_bytes = _unwrap_payload(
            raw, compress=compress, encrypted=encrypted, passphrase=passphrase
        )
    except ValueError as exc:
        result = ImportResult(mode=mode, dry_run=dry_run)
        result.errors.append(str(exc))
        return result

    # ── JSON path ────────────────────────────────────────────────────────
    try:
        payload = parse_json_export(payload_bytes)
    except (json.JSONDecodeError, ValueError) as exc:
        result = ImportResult(mode=mode, dry_run=dry_run)
        result.errors.append(f"Invalid JSON export: {exc}")
        return result

    return _import_json(
        mgr,
        payload,
        mode=mode,
        overwrite=overwrite,
        dry_run=dry_run,
        trusted_restore=trusted_restore,
    )
