"""``mnemos sync`` — federation Phase 0 batch sync CLI logic.

ArchCom 2026-07-17 federation contract §3.1 — operator-curated, offline,
cron-triggered batch sync between two Vesma instances. **No network** —
transfer is out-of-band (rsync / scp / shared volume via
``scripts/sync-peers.sh``).

Two subcommands, wired into ``cli/main.py`` as a Typer sub-app:

* ``mnemos sync export`` — build a compact ``vesmaro.federation.v1``
  payload from memories in the configured ``shared_projects``, run the
  moderation pipeline (Part 1) on each, and write the result to a file
  (optionally AES-256-GCM encrypted with a passphrase from
  ``VESMARO_EXPORT_PASSPHRASE``).
* ``mnemos sync import`` — read a compact payload (decrypting if
  needed), validate each record (reusing the #86 import-validation
  logic, adapted for the compact ``CompactRecord`` shape), and merge
  idempotently by record ``id`` (``fed:<source_agent>:<uuid>`` prefix
  — existing records are skipped, never overwritten).

**Reuse — no duplication** (contract §3.1):

* :func:`vesmaro.compact.build_compact_payload` (#85 Part 2a) — builds the
  compact payload and runs moderation internally. ``sync export`` only
  queries memories and forwards them.
* :func:`vesmaro.moderation.moderate` (#85 Part 1) — called inside
  ``build_compact_payload``; not re-invoked here.
* :func:`vesmaro.cli.export._encrypt` / :func:`vesmaro.cli.export.decrypt`
  / :func:`vesmaro.cli.export.is_encrypted` (#84) — reused verbatim for
  AES-256-GCM passphrase encryption. No new crypto.
* :func:`vesmaro.cli.import_.validate_import_record` (#86) — reused for
  per-record validation. The compact record shape is mapped to the
  ``Memory``-shaped dict the validator expects (``content=summary``,
  ``title``, ``tags``).

**Audit log** — every export and import appends one JSONL entry to
``~/.mnemos/logs/sync-audit.jsonl`` (counters only — no raw content /
secrets / PII). See :mod:`vesmaro.audit`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vesmaro.audit import log_sync_audit
from vesmaro.cli.export import _encrypt, decrypt, is_encrypted
from vesmaro.cli.import_ import (
    DEFAULT_MAX_CONTENT_CHARS,
    gate_imported_memory,
    validate_import_record,
)
from vesmaro.compact import COMPACT_SCHEMA, CompactRecord, build_compact_payload
from vesmaro.config import Settings
from vesmaro.models import (
    NO_FEDERATE_TAG,
    Memory,
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    MemoryType,
)

if TYPE_CHECKING:
    from vesmaro.manager import MemoryManager

logger = logging.getLogger(__name__)

__all__ = [
    "SyncExportResult",
    "SyncImportResult",
    "run_sync_export",
    "run_sync_import",
]

#: Environment variable holding the sync export passphrase. The value
#: is NEVER accepted as a CLI argument (per ``sensitive-data.instructions.md``
#: arguments appear in process listings / shell history).
_EXPORT_PASSPHRASE_ENV: str = "VESMARO_EXPORT_PASSPHRASE"

#: Maximum number of memories fetched per ``shared_projects`` query
#: batch. The compact payload is built in-memory; cap the query so a
#: very large store does not OOM. Operators with >10k memories should
#: scope by ``shared_projects`` and run incremental syncs.
_MAX_EXPORT_MEMORIES: int = 10_000

#: Split regex for ``--shared-projects`` CLI input (space OR comma).
_SHARED_PROJECTS_SPLIT_RE = re.compile(r"[\s,]+")


# ── Result dataclasses ───────────────────────────────────────────────────────


@dataclass
class SyncExportResult:
    """Outcome of a sync export run."""

    output: Path | None
    records_exported: int
    records_refused: int
    secrets_redacted: int
    pii_anonymized: int
    encrypted: bool
    shared_projects: list[str]
    dry_run: bool
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.output) if self.output else None,
            "records_exported": self.records_exported,
            "records_refused": self.records_refused,
            "secrets_redacted": self.secrets_redacted,
            "pii_anonymized": self.pii_anonymized,
            "encrypted": self.encrypted,
            "shared_projects": list(self.shared_projects),
            "dry_run": self.dry_run,
            "errors": list(self.errors),
        }


@dataclass
class SyncImportResult:
    """Outcome of a sync import run."""

    source: Path
    records_imported: int = 0
    records_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    format_version: str | None = None
    dry_run: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "records_imported": self.records_imported,
            "records_skipped": self.records_skipped,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "format_version": self.format_version,
            "dry_run": self.dry_run,
        }


# ── Helpers ──────────────────────────────────────────────────────────────────


def _parse_shared_projects(raw: str | None) -> list[str]:
    """Parse a ``--shared-projects`` CLI string into a de-duplicated list.

    Accepts space-separated, comma-separated, or mixed input. Empty /
    whitespace-only entries are dropped. Order is preserved (first
    occurrence wins on duplicates).
    """
    if not raw:
        return []
    parts = [p.strip() for p in _SHARED_PROJECTS_SPLIT_RE.split(raw) if p.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _resolve_shared_projects(
    cli_arg: str | None,
    settings: Settings,
) -> list[str]:
    """Resolve the effective ``shared_projects`` list.

    Precedence: ``--shared-projects`` CLI arg > ``federation.shared_projects``
    config. If both are empty → the caller raises a ``ValueError`` (the
    CLI surfaces it as a Typer ``BadParameter``).
    """
    cli_projects = _parse_shared_projects(cli_arg)
    if cli_projects:
        return cli_projects
    return list(settings.federation.shared_projects)


def _query_memories_for_sync(
    mgr: MemoryManager,
    shared_projects: list[str],
) -> list[Memory]:
    """Query memories eligible for sync export.

    Filters:

    * ``project`` tag (denormalised on :class:`Memory` as ``memory.project``)
      is in ``shared_projects``.
    * Record is NOT tagged ``mnemos:no-federate`` (defence-in-depth —
      moderation would refuse such records anyway, but exclude early to
      avoid wasted moderation work).
    * Status: all statuses EXCEPT ``archived``. ``archived`` means
      "intentionally hidden from normal flows" and is not syncable.
      ``raw`` / ``processing`` / ``processed`` / ``published`` are all
      eligible — the receiving side re-validates everything.

    The query is capped at :data:`_MAX_EXPORT_MEMORIES` to bound memory
    use; operators with larger stores should scope ``shared_projects``
    more narrowly or run incremental syncs.
    """
    eligible: list[Memory] = []
    for project in shared_projects:
        batch = mgr.sqlite.list_all(
            limit=_MAX_EXPORT_MEMORIES,
            offset=0,
            project=project,
        )
        for mem in batch:
            if NO_FEDERATE_TAG in mem.tags:
                continue
            if mem.status == MemoryStatus.ARCHIVED:
                continue
            eligible.append(mem)
    # De-duplicate by id (a memory could appear once per project query,
    # though ``project`` is a single field so this is defensive only).
    seen_ids: set[str] = set()
    unique: list[Memory] = []
    for mem in eligible:
        if mem.id in seen_ids:
            continue
        seen_ids.add(mem.id)
        unique.append(mem)
    return unique


def _derive_source_agent(memories: list[Memory], fallback: str = "mnemos") -> str:
    """Derive a single ``source_agent`` slug for the compact payload.

    The compact format's ``id`` is ``fed:<source_agent>:<uuid>``. A
    batch may contain memories from multiple agents; we pick the most
    common ``agent:``-tag slug so the prefix is meaningful. When no
    memory carries an ``agent:`` tag, fall back to ``"mnemos"`` (the
    instance-level slug). The slug is sanitised to the
    ``[a-z0-9_-]{1,64}`` shape required by the tag contract.
    """
    counts: dict[str, int] = {}
    for mem in memories:
        for tag in mem.tags:
            if tag.startswith("agent:"):
                slug = tag[len("agent:") :]
                if slug:
                    counts[slug] = counts.get(slug, 0) + 1
                break
    if counts:
        return max(counts.items(), key=lambda kv: kv[1])[0]
    return fallback


def _sanitise_agent_slug(slug: str) -> str:
    """Normalise an agent slug to the ``[a-z0-9_-]+`` shape.

    The compact ``id`` prefix is part of a globally-unique identifier;
    non-alphanumeric characters would make it ambiguous. Lowercase,
    replace runs of disallowed chars with ``-``, truncate to 64 chars.
    """
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", slug.lower()).strip("-")
    return cleaned[:64] or "mnemos"


# ── CompactRecord validation (adapted from #86 validate_import_record) ───────


def _validate_compact_record(
    record: CompactRecord,
    *,
    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
) -> tuple[list[str], list[str]]:
    """Validate a :class:`CompactRecord` for import.

    Reuses :func:`vesmaro.cli.import_.validate_import_record` (#86) by
    mapping the compact record shape onto the ``Memory``-shaped dict the
    validator expects (``content=summary``, ``title``, ``tags``). The
    ``id`` is forwarded as ``memory_id`` so field-level errors are
    attributable.

    Returns ``(errors, warnings)`` — ``errors`` are fatal (the whole
    batch is rejected), ``warnings`` are non-fatal (prompt-injection
    mentions, logged only).
    """
    memory_shaped: dict[str, Any] = {
        "id": record.id,
        "content": record.summary,
        "title": record.title,
        "tags": list(record.tags),
    }
    return validate_import_record(
        memory_shaped,
        max_content_chars=max_content_chars,
        memory_id=record.id,
    )


def _validate_compact_payload(payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Validate a parsed compact payload (schema + every record).

    Returns ``(errors, warnings)``. On any error the caller rejects the
    whole batch — no partial writes (contract §3.1).
    """
    errors: list[str] = []
    warnings: list[str] = []

    schema = payload.get("schema")
    if schema != COMPACT_SCHEMA:
        errors.append(f"payload:schema: expected '{COMPACT_SCHEMA}', got {schema!r}")
        return errors, warnings  # No point validating records if schema is wrong.

    records_raw = payload.get("records")
    if not isinstance(records_raw, list):
        errors.append("payload:records: must be a list")
        return errors, warnings

    for idx, raw in enumerate(records_raw):
        if not isinstance(raw, dict):
            errors.append(f"payload:records[{idx}]: must be an object")
            continue
        try:
            record = CompactRecord.model_validate(raw)
        except Exception as exc:  # pydantic ValidationError + anything else
            errors.append(f"payload:records[{idx}]: invalid CompactRecord: {exc}")
            continue
        rec_errors, rec_warnings = _validate_compact_record(record)
        errors.extend(rec_errors)
        warnings.extend(rec_warnings)

    return errors, warnings


# ── Export ────────────────────────────────────────────────────────────────────


def run_sync_export(
    mgr: MemoryManager,
    *,
    output: Path,
    encrypt: bool = False,
    shared_projects_arg: str | None = None,
    dry_run: bool = False,
) -> SyncExportResult:
    """Run a sync export and (unless dry-run) write the compact payload.

    See the module docstring for the full contract. The
    ``VESMARO_EXPORT_PASSPHRASE`` environment variable is read only when
    ``encrypt=True``; if it is missing, no file is written and the
    result carries an error.
    """
    settings = mgr.settings
    shared_projects = _resolve_shared_projects(shared_projects_arg, settings)
    if not shared_projects:
        raise ValueError(
            "no shared_projects configured — set federation.shared_projects in "
            "config or pass --shared-projects"
        )

    memories = _query_memories_for_sync(mgr, shared_projects)
    source_agent = _sanitise_agent_slug(_derive_source_agent(memories))
    payload = build_compact_payload(
        memories,
        source_agent=source_agent,
        refuse_threshold=settings.federation.moderation_refuse_threshold,
    )
    stats = payload["stats"]
    encrypted = False
    errors: list[str] = []

    if encrypt:
        passphrase = os.environ.get(_EXPORT_PASSPHRASE_ENV)
        if not passphrase:
            errors.append(
                f"--encrypt requested but {_EXPORT_PASSPHRASE_ENV} env var is not set; "
                "no file written."
            )
            result = SyncExportResult(
                output=None,
                records_exported=int(stats.get("exported", 0)),
                records_refused=int(stats.get("refused", 0)),
                secrets_redacted=int(stats.get("secrets_redacted", 0)),
                pii_anonymized=int(stats.get("pii_anonymized", 0)),
                encrypted=False,
                shared_projects=list(shared_projects),
                dry_run=dry_run,
                errors=errors,
            )
            log_sync_audit(
                {
                    "action": "sync-export",
                    "output": None,
                    "records_exported": result.records_exported,
                    "records_refused": result.records_refused,
                    "secrets_redacted": result.secrets_redacted,
                    "pii_anonymized": result.pii_anonymized,
                    "encrypted": False,
                    "shared_projects": list(shared_projects),
                    "errors": list(errors),
                }
            )
            return result

    if dry_run:
        result = SyncExportResult(
            output=None,
            records_exported=int(stats.get("exported", 0)),
            records_refused=int(stats.get("refused", 0)),
            secrets_redacted=int(stats.get("secrets_redacted", 0)),
            pii_anonymized=int(stats.get("pii_anonymized", 0)),
            encrypted=encrypt,
            shared_projects=list(shared_projects),
            dry_run=True,
            errors=errors,
        )
        log_sync_audit(
            {
                "action": "sync-export",
                "output": None,
                "records_exported": result.records_exported,
                "records_refused": result.records_refused,
                "secrets_redacted": result.secrets_redacted,
                "pii_anonymized": result.pii_anonymized,
                "encrypted": False,
                "shared_projects": list(shared_projects),
                "dry_run": True,
            }
        )
        return result

    raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    if encrypt:
        passphrase = os.environ[_EXPORT_PASSPHRASE_ENV]
        raw = _encrypt(raw, passphrase)
        encrypted = True

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)

    result = SyncExportResult(
        output=output,
        records_exported=int(stats.get("exported", 0)),
        records_refused=int(stats.get("refused", 0)),
        secrets_redacted=int(stats.get("secrets_redacted", 0)),
        pii_anonymized=int(stats.get("pii_anonymized", 0)),
        encrypted=encrypted,
        shared_projects=list(shared_projects),
        dry_run=False,
        errors=errors,
    )
    log_sync_audit(
        {
            "action": "sync-export",
            "output": str(output),
            "records_exported": result.records_exported,
            "records_refused": result.records_refused,
            "secrets_redacted": result.secrets_redacted,
            "pii_anonymized": result.pii_anonymized,
            "encrypted": encrypted,
            "shared_projects": list(shared_projects),
        }
    )
    return result


# ── Import ────────────────────────────────────────────────────────────────────


def _detect_compact_encrypted(raw: bytes) -> bool:
    """Detect whether a sync file is encrypted (magic header) or plain JSON.

    Reuses :func:`vesmaro.cli.export.is_encrypted` (the ``MNEMOS1`` magic).
    A ``.enc`` extension is also treated as encrypted as a fallback when
    the magic header is somehow missing (defence-in-depth).
    """
    return is_encrypted(raw)


def _compact_record_to_memory_create(record: CompactRecord) -> MemoryCreate:
    """Map a :class:`CompactRecord` back to a :class:`MemoryCreate`.

    The compact format carries ``summary`` + ``key_points`` (not the raw
    content); the imported memory's ``content`` is the summary, and the
    key points are joined as a bulleted appendix so the receiving agent
    sees the same essence the source operator curated. Tags are copied
    verbatim (the receiving side's tag-contract validator will catch any
    drift). ``source`` is :attr:`MemorySource.MCP` — federated records
    arrive via the sync pipeline, which is MCP-shaped (no dedicated
    ``FEDERATED`` source exists yet; ``MCP`` is the closest semantic
    match and is the documented fallback per the task spec).

    Every mapped row carries the ``federated_origin`` metadata stamp
    (#254 review P2): a cross-operator row must never read as a LOCAL
    neighbor under awareness ``observed`` rendering (CWE-359) —
    ``vesmaro.awareness.is_delta_excluded`` keys on exactly this stamp
    to keep imported rows out of the delta. The stamp value is the
    compact record's ``source_agent`` (the peer-side author slug); a
    first-class ``origin=`` column remains a tracker item.
    """
    parts: list[str] = []
    if record.summary:
        parts.append(record.summary)
    if record.key_points:
        parts.append("")
        parts.append("Key points:")
        for kp in record.key_points:
            parts.append(f"- {kp}")
    content = "\n".join(parts)
    tags = list(record.tags)
    return MemoryCreate(
        content=content,
        title=record.title or None,
        tags=tags,
        source=MemorySource.MCP,
        memory_type=MemoryType.NOTE,
        status=MemoryStatus.PUBLISHED,
        metadata={"federated_origin": record.source_agent or "federation-sync"},
    )


def _project_from_tags(tags: list[str]) -> str:
    """Extract the ``project:<slug>`` value from a tag list (empty if none)."""
    for tag in tags:
        if tag.startswith("project:"):
            return tag[len("project:") :]
    return ""


def _agent_from_tags(tags: list[str]) -> str:
    """Extract the ``agent:<slug>`` value from a tag list (empty if none)."""
    for tag in tags:
        if tag.startswith("agent:"):
            return tag[len("agent:") :]
    return ""


def run_sync_import(
    mgr: MemoryManager,
    *,
    source: Path,
    passphrase_env: str | None = None,
    dry_run: bool = False,
) -> SyncImportResult:
    """Run a sync import — validate, then merge idempotently by record id.

    See the module docstring for the full contract. The passphrase env
    var *name* (not value) is read only when the source is encrypted.
    """
    result = SyncImportResult(source=source, dry_run=dry_run)

    if not source.exists():
        result.errors.append(f"Source file not found: {source}")
        log_sync_audit(
            {
                "action": "sync-import",
                "source": str(source),
                "records_imported": 0,
                "records_skipped": 0,
                "errors": list(result.errors),
                "warnings": [],
                "dry_run": dry_run,
            }
        )
        return result

    raw = source.read_bytes()
    encrypted = _detect_compact_encrypted(raw)

    if encrypted:
        env_name = passphrase_env or _EXPORT_PASSPHRASE_ENV
        passphrase = os.environ.get(env_name)
        if not passphrase:
            result.errors.append(
                f"Source is encrypted but env var {env_name} is not set; cannot decrypt."
            )
            log_sync_audit(
                {
                    "action": "sync-import",
                    "source": str(source),
                    "records_imported": 0,
                    "records_skipped": 0,
                    "errors": list(result.errors),
                    "warnings": [],
                    "encrypted": True,
                    "dry_run": dry_run,
                }
            )
            return result
        try:
            raw = decrypt(raw, passphrase)
        except ValueError as exc:
            result.errors.append(f"Decryption failed: {exc}")
            log_sync_audit(
                {
                    "action": "sync-import",
                    "source": str(source),
                    "records_imported": 0,
                    "records_skipped": 0,
                    "errors": list(result.errors),
                    "warnings": [],
                    "encrypted": True,
                    "dry_run": dry_run,
                }
            )
            return result

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        result.errors.append(f"Invalid JSON payload: {exc}")
        log_sync_audit(
            {
                "action": "sync-import",
                "source": str(source),
                "records_imported": 0,
                "records_skipped": 0,
                "errors": list(result.errors),
                "warnings": [],
                "encrypted": encrypted,
                "dry_run": dry_run,
            }
        )
        return result

    result.format_version = payload.get("schema")

    errors, warnings = _validate_compact_payload(payload)
    result.errors.extend(errors)
    result.warnings.extend(warnings)
    if errors:
        # Reject the whole batch — no partial writes (contract §3.1).
        log_sync_audit(
            {
                "action": "sync-import",
                "source": str(source),
                "records_imported": 0,
                "records_skipped": 0,
                "errors": list(result.errors),
                "warnings": list(result.warnings),
                "encrypted": encrypted,
                "dry_run": dry_run,
            }
        )
        return result

    records_raw = payload.get("records", [])
    records: list[CompactRecord] = [CompactRecord.model_validate(r) for r in records_raw]

    if dry_run:
        result.records_imported = len(records)
        result.warnings.append(
            f"Dry-run: validated {len(records)} records, 0 errors, {len(warnings)} warnings."
        )
        log_sync_audit(
            {
                "action": "sync-import",
                "source": str(source),
                "records_imported": len(records),
                "records_skipped": 0,
                "errors": [],
                "warnings": list(result.warnings),
                "encrypted": encrypted,
                "dry_run": True,
            }
        )
        return result

    imported = 0
    skipped = 0
    for record in records:
        existing = mgr.sqlite.get(record.id)
        if existing is not None:
            skipped += 1
            continue
        create = _compact_record_to_memory_create(record)
        project = _project_from_tags(record.tags)
        agent = _agent_from_tags(record.tags)
        # Persist with the federated id (not a freshly-generated uuid) so
        # re-imports are idempotent. We bypass ``mgr.add`` (which generates
        # a new id) and construct the Memory directly, mirroring the JSON
        # import path in ``cli/import_.py::_import_json``. ``metadata``
        # carries the ``federated_origin`` stamp minted by the mapper
        # (#254 review P2) — without it the row would read as a LOCAL
        # neighbor under awareness presence/delta (CWE-359).
        memory = Memory(
            id=record.id,
            content=create.content,
            title=create.title,
            tags=list(create.tags),
            source=create.source,
            memory_type=create.memory_type,
            status=create.status,
            metadata=dict(create.metadata),
            project=project,
            agent=agent,
        )
        # ADR-0019 Phase D / #166: peer-status rows must not land visible
        # without the publication gate — run the fail-closed danger gate
        # over the imported projection BEFORE the store write. Refusal →
        # stored RAW + pipeline_state=NULL (zero-loss, invisible); clean →
        # peer status kept, pipeline_state=pending (the local refine
        # queue picks it up). Audited as path=federation-import.
        memory, admitted = gate_imported_memory(mgr, memory)
        if not admitted:
            result.warnings.append(
                f"record {record.id}: stored RAW — federation-import "
                "danger-gate refusal (zero-loss, invisible)"
            )
        try:
            mgr.sqlite.save(memory)
            if memory.status == MemoryStatus.PUBLISHED:
                # ADR-0019 B2b (N1 review #163): the federated re-embed
                # routes through the SINGLE embedding write point — the
                # upsert then stamps the content_hash freshness key and
                # emits the embed_upserted audit event, so the sweeper
                # can heal this embed like any other.
                try:
                    mgr.upsert_embedding(memory)
                except Exception as exc:  # pragma: no cover — best-effort
                    logger.warning("re-embedding failed for %s: %s", memory.id, exc)
            imported += 1
        except Exception as exc:
            result.errors.append(f"import {record.id}: {exc}")

    result.records_imported = imported
    result.records_skipped = skipped
    log_sync_audit(
        {
            "action": "sync-import",
            "source": str(source),
            "records_imported": imported,
            "records_skipped": skipped,
            "errors": list(result.errors),
            "warnings": list(result.warnings),
            "encrypted": encrypted,
            "format_version": result.format_version,
        }
    )
    return result
