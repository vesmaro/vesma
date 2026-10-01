"""``vesma export`` — backup memories to JSON or SQLite snapshot.

Design (owner-confirmed, 2026-06-20):

* **JSON format** — metadata only (memories + projects). Vectors are
  regenerated on import. Traces are NEVER included (audit/log data, not
  exportable).
* **SQLite format** — complete snapshot: copies the raw ``mnemos.db`` and
  ``vectors.db`` files into a ``.tar.gz`` archive. Fastest full backup.
* **Filters** (``--project``, ``--agent``, ``--status``, ``--tags``,
  ``--since``, ``--until``) apply to JSON only. SQLite is always a full
  snapshot.
* **Compression** — ``gzip`` (stdlib). ``zstd`` is reported as a future
  enhancement when the optional dependency is absent.
* **Encryption** — ``--encrypt`` with passphrase (PBKDF2 + AES-GCM via
  ``cryptography``). Passphrase is read from a prompt or
  ``--passphrase-file``.
* **Versioning** — every JSON export carries ``format_version`` +
  ``mnemos_version`` for forward compatibility.

The module exposes :func:`run_export` (pure logic, testable) and a Typer
command wired into ``cli/main.py``.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import sqlite3
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vesmaro import __version__
from vesmaro.models import MemoryStatus

if TYPE_CHECKING:
    from vesmaro.manager import MemoryManager
    from vesmaro.secrets_detector import SecretFinding

logger = logging.getLogger(__name__)

__all__ = [
    "CompressMode",
    "ExportFormat",
    "ExportResult",
    "run_export",
]

# ── Constants ─────────────────────────────────────────────────────────────────

#: JSON export schema version (forward-compat marker).
FORMAT_VERSION = "1.0"

#: PBKDF2 iterations for passphrase → AES key derivation.
_PBKDF2_ITERATIONS = 200_000

#: AES-GCM nonce length (bytes).
_NONCE_LEN = 12

#: Salt length for PBKDF2 (bytes).
_SALT_LEN = 16


# ── Enums ─────────────────────────────────────────────────────────────────────


class ExportFormat(StrEnum):
    JSON = "json"
    SQLITE = "sqlite"


class CompressMode(StrEnum):
    NONE = "none"
    GZIP = "gzip"
    ZSTD = "zstd"


# ── Result ────────────────────────────────────────────────────────────────────


@dataclass
class ExportResult:
    """Outcome of an export run — returned to the CLI for display."""

    path: Path
    format: ExportFormat
    compress: CompressMode
    encrypted: bool
    memory_count: int
    project_count: int
    bytes_written: int
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "format": self.format.value,
            "compress": self.compress.value,
            "encrypted": self.encrypted,
            "memory_count": self.memory_count,
            "project_count": self.project_count,
            "bytes_written": self.bytes_written,
            "warnings": list(self.warnings),
        }


# ── Filter model ──────────────────────────────────────────────────────────────


@dataclass
class ExportFilter:
    """Filter parameters for JSON export."""

    project: str | None = None
    agent: str | None = None
    status: MemoryStatus | None = None
    tags: list[str] | None = None
    since: datetime | None = None
    until: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "agent": self.agent,
            "status": self.status.value if self.status else None,
            "tags": self.tags,
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
        }


# ── JSON export ───────────────────────────────────────────────────────────────


def _memory_to_export_dict(memory: Any) -> dict[str, Any]:
    """Serialise a Memory to the export JSON shape (no raw_content by default).

    ``raw_content`` is included only when it is not None — the field is
    part of the schema and import needs it to reconstruct the memory.
    """
    data: dict[str, Any] = memory.model_dump(mode="json")
    # Ensure deterministic key ordering for reproducible exports.
    return data


def _redact_string_field(
    value: str,
    counts: dict[str, int],
) -> str:
    """Scan ``value`` for secrets and return the redacted replacement.

    Updates ``counts`` in place with per-pattern tallies (aggregated across
    all fields by the caller). Returns ``value`` unchanged when no secret
    is found. Uses :func:`detect_secrets` (which already de-overlaps) and
    :func:`redact_content` for the replacement.
    """
    from vesmaro.secrets_detector import detect_secrets, findings_by_pattern, redact_content

    if not value:
        return value
    findings = detect_secrets(value)
    if not findings:
        return value
    for name, c in findings_by_pattern(findings).items():
        counts[name] = counts.get(name, 0) + c
    return redact_content(value, findings)


def _redact_metadata(
    obj: Any,
    counts: dict[str, int],
) -> Any:
    """Recursively redact secrets in string values within ``metadata``.

    Walks dicts and lists; any string leaf that contains a secret pattern
    is replaced with its ``<REDACTED:<pattern_name>>`` form. Non-string
    leaves (int, float, bool, None) are passed through unchanged. Returns
    a new structure (the original is not mutated).
    """
    if isinstance(obj, str):
        return _redact_string_field(obj, counts)
    if isinstance(obj, dict):
        return {k: _redact_metadata(v, counts) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_metadata(v, counts) for v in obj]
    return obj


def build_json_payload(
    mgr: MemoryManager,
    filt: ExportFilter,
) -> dict[str, Any]:
    """Build the JSON export payload dict (not yet serialised).

    Federation defence-in-depth (Layer 3, partial — ArchCom 2026-07-17
    §2.2.1 / §3.1):

    * **Exclusion:** records tagged ``mnemos:no-federate`` are excluded
      from the export entirely (contract КП-6: "запись исключается из
      export И pull"). They never appear in the JSON payload.
    * **Redaction:** for records that DO pass the filter, the content,
      ``raw_content``, ``title``, ``source_url``, and string values in
      ``metadata`` are scanned with the secrets detector. Any detected
      secret is replaced with ``<REDACTED:<pattern_name>>`` so the export
      never ships a raw credential, even if the write-path scanner
      (Layer 1) missed it.

    The redaction summary is recorded in the payload ``redaction_summary``
    field for operator visibility (counts only — never raw values).
    """
    from vesmaro.models import NO_FEDERATE_TAG
    from vesmaro.secrets_detector import detect_secrets, findings_by_pattern, redact_content

    memories = mgr.sqlite.list_for_export(
        project=filt.project,
        agent=filt.agent,
        status=filt.status,
        tags=filt.tags,
        since=filt.since,
        until=filt.until,
    )

    excluded_no_federate = 0
    redacted_records = 0
    redaction_counts: dict[str, int] = {}
    export_memories: list[dict[str, Any]] = []

    for mem in memories:
        # ── Exclude no-federate records entirely ─────────────────────────
        if NO_FEDERATE_TAG in mem.tags:
            excluded_no_federate += 1
            continue

        # ── Redact secrets in content + raw_content ───────────────────────
        findings = detect_secrets(mem.content) if mem.content else []
        if mem.raw_content:
            findings.extend(detect_secrets(mem.raw_content))
        # De-overlap: re-sort + drop contained (detect_secrets already
        # returns de-overlapped findings per call; merging two calls may
        # re-introduce overlaps across content/raw_content boundaries,
        # so we re-sort + de-overlap the combined list).
        findings.sort(key=lambda f: f.start)
        deoverlapped: list[SecretFinding] = []
        last_end = -1
        for f in findings:
            if f.start >= last_end:
                deoverlapped.append(f)
                last_end = f.end
        field_redacted = False
        if deoverlapped:
            redacted_content = redact_content(mem.content, deoverlapped)
            mem = mem.model_copy(update={"content": redacted_content})
            if mem.raw_content:
                # Redact raw_content with its own findings (those that
                # fell within the raw_content span). For simplicity we
                # re-detect on raw_content alone — it's a separate string
                # and offsets differ from the combined list.
                raw_findings = detect_secrets(mem.raw_content)
                if raw_findings:
                    mem = mem.model_copy(
                        update={"raw_content": redact_content(mem.raw_content, raw_findings)}
                    )
                    # Merge raw_findings counts into the summary too.
                    for name, c in findings_by_pattern(raw_findings).items():
                        redaction_counts[name] = redaction_counts.get(name, 0) + c
            field_redacted = True
            for name, c in findings_by_pattern(deoverlapped).items():
                redaction_counts[name] = redaction_counts.get(name, 0) + c

        # ── Redact secrets in title / source_url / metadata ───────────────
        # S-1: these fields were previously unscanned, so a secret in the
        # title, source_url, or a metadata string value would ship
        # unredacted to the export JSON. Each is scanned independently.
        if mem.title:
            redacted_title = _redact_string_field(mem.title, redaction_counts)
            if redacted_title != mem.title:
                mem = mem.model_copy(update={"title": redacted_title})
                field_redacted = True
        if mem.source_url:
            redacted_url = _redact_string_field(mem.source_url, redaction_counts)
            if redacted_url != mem.source_url:
                mem = mem.model_copy(update={"source_url": redacted_url})
                field_redacted = True
        if mem.metadata:
            redacted_metadata = _redact_metadata(mem.metadata, redaction_counts)
            if redacted_metadata != mem.metadata:
                mem = mem.model_copy(update={"metadata": redacted_metadata})
                field_redacted = True

        if field_redacted:
            redacted_records += 1

        export_memories.append(_memory_to_export_dict(mem))

    projects = mgr.sqlite.list_projects()
    payload: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "mnemos_version": __version__,
        "exported_at": datetime.now(UTC).isoformat(),
        "filter": filt.to_dict(),
        "memories": export_memories,
        "projects": [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "paths": p.paths,
                "created_at": p.created_at.isoformat(),
                "updated_at": p.updated_at.isoformat(),
            }
            for p in projects
        ],
        "redaction_summary": {
            "excluded_no_federate": excluded_no_federate,
            "redacted_records": redacted_records,
            "patterns": redaction_counts,
        },
    }
    if excluded_no_federate or redacted_records:
        logger.info(
            "export redaction: excluded %d no-federate records, redacted %d records "
            "(patterns: %s) — raw values not logged",
            excluded_no_federate,
            redacted_records,
            redaction_counts,
        )
    return payload


# ── SQLite export ─────────────────────────────────────────────────────────────


def _build_sqlite_snapshot(mgr: MemoryManager) -> bytes:
    """Build an in-memory tar.gz containing mnemos.db + vectors.db.

    The raw DB files are copied byte-for-byte so the snapshot is a
    complete, restorable image. We checkpoint the WAL first so the
    on-disk file contains all committed transactions (WAL mode keeps
    recent writes in a ``-wal`` sidecar that would be lost otherwise).
    """
    # Checkpoint WAL so the main .db file is current.
    for store_conn in (mgr.sqlite._get_conn(), mgr.vectors._conn()):
        try:
            store_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            store_conn.commit()
        except sqlite3.Error as exc:
            # Non-fatal: the copy will still include whatever is in the
            # main file; the snapshot may miss the last few writes.
            logger.warning("WAL checkpoint failed during snapshot: %s", exc)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        mnemos_db_path = mgr.settings.db_path
        vectors_db_path = mgr.settings.mnemos.data_dir / "vectors.db"
        for name, path in (("mnemos.db", mnemos_db_path), ("vectors.db", vectors_db_path)):
            if not path.exists():
                continue
            data = path.read_bytes()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = datetime.now(UTC).timestamp()
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ── Compression ───────────────────────────────────────────────────────────────


def _compress(data: bytes, mode: CompressMode) -> tuple[bytes, list[str]]:
    """Compress ``data`` per ``mode``. Returns (payload, warnings)."""
    if mode == CompressMode.NONE:
        return data, []
    if mode == CompressMode.GZIP:
        return gzip.compress(data), []
    if mode == CompressMode.ZSTD:
        try:
            import zstandard
        except ImportError:
            try:
                import pyzstd

                return bytes(pyzstd.compress(data)), ["zstd via pyzstd"]
            except ImportError:
                # Fall back to gzip and warn — zstd is a future enhancement.
                return gzip.compress(data), [
                    "zstd requested but neither zstandard nor pyzstd is installed; "
                    "fell back to gzip. Install `zstandard` to enable zstd."
                ]
        return bytes(zstandard.compress(data)), []
    # Unreachable but keeps the return type exhaustive.
    return data, [f"unknown compress mode: {mode}"]


# ── Encryption ────────────────────────────────────────────────────────────────


def _encrypt(data: bytes, passphrase: str) -> bytes:
    """Encrypt ``data`` with a passphrase-derived AES-256-GCM key.

    Layout::

        b"MNEMOS1" | salt(16) | nonce(12) | ciphertext+tag

    PBKDF2-HMAC-SHA256 with 200k iterations derives a 32-byte key.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    key = kdf.derive(passphrase.encode("utf-8"))
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, data, None)
    return b"MNEMOS1" + salt + nonce + ciphertext


def decrypt(data: bytes, passphrase: str) -> bytes:
    """Decrypt a payload produced by :func:`_encrypt`.

    Raises ``ValueError`` on a wrong passphrase (AES-GCM tag mismatch)
    or on a malformed header.
    """
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    if not data.startswith(b"MNEMOS1"):
        raise ValueError("Not a Vesma encrypted export (missing magic header).")
    body = data[len(b"MNEMOS1") :]
    if len(body) < _SALT_LEN + _NONCE_LEN:
        raise ValueError("Truncated encrypted payload.")
    salt = body[:_SALT_LEN]
    nonce = body[_SALT_LEN : _SALT_LEN + _NONCE_LEN]
    ciphertext = body[_SALT_LEN + _NONCE_LEN :]
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    key = kdf.derive(passphrase.encode("utf-8"))
    aesgcm = AESGCM(key)
    try:
        return aesgcm.decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise ValueError("Wrong passphrase or corrupted payload.") from exc


# ── Orchestrator ──────────────────────────────────────────────────────────────


def run_export(
    mgr: MemoryManager,
    *,
    fmt: ExportFormat,
    output: Path,
    compress: CompressMode = CompressMode.NONE,
    encrypt: bool = False,
    passphrase: str | None = None,
    passphrase_file: Path | None = None,
    filt: ExportFilter | None = None,
) -> ExportResult:
    """Run an export and write the result to ``output``.

    The caller is responsible for confirming destructive operations; this
    function only writes to ``output`` (it does not touch the live DB).
    """
    filt = filt or ExportFilter()
    warnings: list[str] = []

    # ── Build the raw payload ────────────────────────────────────────────
    if fmt == ExportFormat.JSON:
        payload = build_json_payload(mgr, filt)
        memory_count = len(payload["memories"])
        project_count = len(payload["projects"])
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    elif fmt == ExportFormat.SQLITE:
        # SQLite format is always a full snapshot — filters are ignored.
        if filt.project or filt.agent or filt.status or filt.tags or filt.since or filt.until:
            warnings.append("Filters are ignored for sqlite format (full snapshot).")
        raw = _build_sqlite_snapshot(mgr)
        memory_count = mgr.sqlite.count()
        project_count = len(mgr.sqlite.list_projects())
    else:  # pragma: no cover — exhaustive enum
        raise ValueError(f"Unknown export format: {fmt}")

    # ── Compress ─────────────────────────────────────────────────────────
    payload_bytes, comp_warnings = _compress(raw, compress)
    warnings.extend(comp_warnings)

    # ── Encrypt ──────────────────────────────────────────────────────────
    if encrypt:
        if passphrase is None and passphrase_file is not None:
            passphrase = passphrase_file.read_text(encoding="utf-8").strip()
        if passphrase is None:
            raise ValueError(
                "Encryption requested but no passphrase provided. "
                "Use --passphrase-file or pass passphrase via the API."
            )
        payload_bytes = _encrypt(payload_bytes, passphrase)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload_bytes)

    return ExportResult(
        path=output,
        format=fmt,
        compress=compress,
        encrypted=encrypt,
        memory_count=memory_count,
        project_count=project_count,
        bytes_written=len(payload_bytes),
        warnings=warnings,
    )


# ── Validation helpers (used by import dry-run) ───────────────────────────────


def parse_json_export(data: bytes) -> dict[str, Any]:
    """Parse and validate a JSON export payload. Raises ValueError on malformed."""
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Export root is not a JSON object.")
    if "format_version" not in payload:
        raise ValueError("Missing 'format_version' in export.")
    if "memories" not in payload:
        raise ValueError("Missing 'memories' array in export.")
    if not isinstance(payload["memories"], list):
        raise ValueError("'memories' is not a list.")
    return payload


def is_encrypted(data: bytes) -> bool:
    """Detect the Vesma encryption magic header."""
    return data.startswith(b"MNEMOS1")


def decompress(data: bytes, mode: CompressMode) -> bytes:
    """Decompress ``data`` per ``mode`` (inverse of :func:`_compress`)."""
    if mode == CompressMode.NONE:
        return data
    if mode == CompressMode.GZIP:
        return gzip.decompress(data)
    if mode == CompressMode.ZSTD:
        try:
            import zstandard

            return bytes(zstandard.decompress(data))
        except ImportError:
            import pyzstd

            return bytes(pyzstd.decompress(data))
    raise ValueError(f"Unknown compress mode: {mode}")


def detect_sqlite_snapshot(data: bytes) -> bool:
    """Return True if ``data`` is a gzip tar containing mnemos.db."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            names = tar.getnames()
            return "mnemos.db" in names
    except (tarfile.TarError, OSError):
        return False


def restore_sqlite_snapshot(
    mgr: MemoryManager,
    data: bytes,
    *,
    backup_current: Path | None = None,
) -> int:
    """Replace the live mnemos.db + vectors.db with the snapshot contents.

    Destructive: overwrites the current DB files. The caller MUST confirm.
    If ``backup_current`` is set, the current files are copied there first.

    Returns the number of memories in the restored snapshot.
    """
    # Checkpoint WAL so the on-disk .db file is current before backup/copy.
    for store_conn in (mgr.sqlite._get_conn(), mgr.vectors._conn()):
        try:
            store_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            store_conn.commit()
        except sqlite3.Error as exc:
            logger.warning("WAL checkpoint failed before restore: %s", exc)
    # Flush any open connection so the on-disk file is current.
    mgr.sqlite.close()
    mgr.vectors.close()

    data_dir = mgr.settings.mnemos.data_dir
    mnemos_db = mgr.settings.db_path
    vectors_db = data_dir / "vectors.db"

    if backup_current is not None:
        backup_current.mkdir(parents=True, exist_ok=True)
        for src in (mnemos_db, vectors_db):
            if src.exists():
                (backup_current / src.name).write_bytes(src.read_bytes())

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.name == "mnemos.db":
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise ValueError("SQLite snapshot missing mnemos.db payload")
                mnemos_db.write_bytes(extracted.read())
            elif member.name == "vectors.db":
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise ValueError("SQLite snapshot missing vectors.db payload")
                vectors_db.write_bytes(extracted.read())

    # Re-open the store on the restored file and count rows.
    conn = sqlite3.connect(str(mnemos_db))
    try:
        row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()
