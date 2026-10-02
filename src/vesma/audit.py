"""Sync audit log — append-only JSONL of federation sync operations.

ArchCom 2026-07-17 federation contract §3.2 — the audit log records
*counters only*: records exported/imported, records refused, secrets
redacted, PII anonymized, errors, warnings. **No raw content, no
secrets, no PII values** ever enter the audit log.

The log is append-only JSONL at ``~/.mnemos/logs/sync-audit.jsonl`` —
one JSON object per line. An operator can ``tail -f`` it for live
monitoring, ``jq`` it for aggregates, or ship it to a SIEM.

Entry shapes (contract §3.2):

* **Export** — ``{"timestamp", "action": "sync-export", "output",
  "records_exported", "records_refused", "secrets_redacted",
  "pii_anonymized", "encrypted", "shared_projects"}``.
* **Import** — ``{"timestamp", "action": "sync-import", "source",
  "records_imported", "records_skipped", "errors", "warnings"}``.

The ``timestamp`` is ISO 8601 UTC with a ``Z`` suffix.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "SCANNER_AUDIT_FILENAME",
    "SYNC_AUDIT_FILENAME",
    "log_scanner_audit",
    "log_sync_audit",
    "scanner_audit_path",
    "sync_audit_path",
]

#: Relative path of the audit log under the user's home directory.
SYNC_AUDIT_FILENAME: str = ".mnemos/logs/sync-audit.jsonl"

#: Relative path of the background scanner audit log under the user's home
#: directory. One JSON object per scan pass — counters only (records scanned
#: / tagged / skipped, pattern-name counts, duration, incremental flag). No
#: raw content, no secrets, no PII values ever enter this log.
SCANNER_AUDIT_FILENAME: str = ".mnemos/logs/scanner-audit.jsonl"


def sync_audit_path() -> Path:
    """Return the absolute path of the sync audit log.

    Resolved against the user's home directory (``~/.mnemos/logs/
    sync-audit.jsonl``). The directory is created on first write by
    :func:`log_sync_audit`.
    """
    return Path.home() / SYNC_AUDIT_FILENAME


def scanner_audit_path() -> Path:
    """Return the absolute path of the background scanner audit log.

    Resolved against the user's home directory (``~/.mnemos/logs/
    scanner-audit.jsonl``). The directory is created on first write by
    :func:`log_scanner_audit`.
    """
    return Path.home() / SCANNER_AUDIT_FILENAME


def log_sync_audit(entry: dict[str, Any]) -> None:
    """Append a sync audit entry to ``~/.mnemos/logs/sync-audit.jsonl``.

    Adds an ISO 8601 UTC ``timestamp`` field if the caller did not
    supply one. The entry is serialised with ``json.dumps(...,
    default=str)`` so ``datetime`` / ``Path`` / ``set`` values degrade
    gracefully rather than raising.

    Args:
        entry: The audit entry dict. The caller is responsible for
            ensuring no raw content, secrets, or PII values are present
            — only counters, paths, and status flags. The
            ``"timestamp"`` and ``"action"`` fields are conventional;
            see the module docstring for the canonical entry shapes.
    """
    _append_jsonl(sync_audit_path(), entry)


def log_scanner_audit(entry: dict[str, Any]) -> None:
    """Append a background scanner audit entry to ``scanner-audit.jsonl``.

    Contract: counters only — ``records_scanned``, ``records_tagged``,
    ``records_skipped``, ``patterns_matched`` (``{pattern_name: count}``),
    ``duration_sec``, ``incremental``. The caller MUST NOT pass raw
    content, secrets, or PII values. The ``timestamp`` field is added
    if missing (ISO 8601 UTC with a ``Z`` suffix).
    """
    _append_jsonl(scanner_audit_path(), entry)


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    """Append one JSON object as a line to ``path`` (creating parents)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(entry)
    record.setdefault("timestamp", datetime.now(UTC).isoformat().replace("+00:00", "Z"))
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
