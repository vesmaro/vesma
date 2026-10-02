"""Federation access log — B-side audit log (Phase 1 prerequisite).

ArchCom 2026-07-17 federation contract §10. A field/log on the B side
that records who queried what, when, with what trigger code, and which
records were returned. Used for **anti-correlation tracking**: B sees
that A already got ``EXHAUSTIVE`` on topic X → the next request on the
same topic returns ``ALREADY_EXHAUSTED`` (contract §9).

Privacy (КП-5, contract §0.п.8): the log stores **only** a SHA-256 hash
of the query topic, never the plaintext topic. This preserves the
query intent from a log leak while still allowing anti-correlation
matching (the same topic hashes to the same digest).

Storage (contract §10 "Где хранится"): the log lives **only on B**.
It is **never replicated**, never exported, never synced — like the
moderation mapping table, it is a leak surface. Phase 1 stores it as
an append-only JSONL file (``~/.mnemos/logs/federation-access.jsonl``)
so that an operator can inspect it with standard tools. Phase 2 will
wire the log into the federation server's request path.

Reference:
    - Contract §10: ``.archcom/sessions/2026-07-17-federation-contract.md``
    - КП-5 (query topic hashing): contract §0.п.8
    - ADR-0016: ``docs/project/adr/0016-federation-threat-model.md``
"""

from __future__ import annotations

import hashlib
import os
import threading
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from vesma.trigger_codes import TriggerCode

# Default log location — under ~/.mnemos/logs/ alongside the other
# logs (sync-audit.jsonl, vesma.log). Resolved lazily so the
# module import never touches the filesystem.
DEFAULT_LOG_PATH = Path("~/.mnemos/logs/federation-access.jsonl")


def hash_topic(topic: str) -> str:
    """Return the SHA-256 hex digest of a query topic.

    Per КП-5 (contract §0.п.8) the access log stores only the hash,
    never the plaintext topic. The same topic hashes to the same
    digest, so B can match a repeat request to a prior ``EXHAUSTIVE``
    answer without ever learning the query intent.
    """
    return hashlib.sha256(topic.encode("utf-8")).hexdigest()


class AccessLogEntry(BaseModel):
    """One row in the federation access log (contract §10).

    Frozen so a logged entry cannot be mutated after the fact — the
    log is an audit trail.

    Fields:
        peer_id: A2A id of the requesting agent (who).
        topic_hash: SHA-256 hex of the query topic (КП-5 — never
            plaintext).
        timestamp: UTC ISO-8601 timestamp of the request.
        project_scope: project slug that was requested.
        trigger_code: trigger code returned to the peer (§9).
        record_ids_accessed: record ids that were returned (for
            forensic audit).
    """

    model_config = {"frozen": True}

    peer_id: str = Field(..., min_length=1, max_length=256)
    topic_hash: str = Field(..., min_length=64, max_length=64)
    timestamp: datetime = Field(...)
    project_scope: str = Field(..., min_length=1, max_length=256)
    trigger_code: TriggerCode
    record_ids_accessed: list[str] = Field(default_factory=list)


class FederationAccessLog:
    """Append-only JSONL access log — B-side only, never replicated.

    Thread-safe via a process-local lock; appends are atomic at the
    Python level (``lock + open(..., "a") + write + flush + fsync``).
    For multi-process deployments the operator should use a single
    writer process (the federation server) — the file format itself
    is line-delimited JSON, so a multi-writer race would at worst
    interleave lines, not corrupt individual entries.

    The log is **never** exported, **never** synced to peers, and
    **never** included in ``vesma export`` (CLI legacy: ``vesma export``). It is a leak surface
    (contract §10 "Где хранится").
    """

    def __init__(self, path: Path | str = DEFAULT_LOG_PATH) -> None:
        self._path: Path = Path(path).expanduser()
        # Process-local lock — guards concurrent appends from multiple
        # threads within one federation server process.
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """Resolved path to the JSONL log file."""
        return self._path

    def append(self, entry: AccessLogEntry) -> None:
        """Append one entry as a single JSON line, fsync, no buffering.

        Creates the parent directory if missing. The write is atomic
        at the line level: the lock prevents interleaving with other
        threads, and ``flush()`` + ``os.fsync()`` force the bytes to
        disk before returning (audit-log integrity).
        """
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = entry.model_dump_json()
            # Open in binary append mode and encode explicitly so we
            # control exactly what hits disk (no platform newline
            # translation, no buffering surprises).
            with open(self._path, "ab") as fh:
                fh.write((line + "\n").encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())

    def _iter_entries(self) -> list[AccessLogEntry]:
        """Read and parse every line of the log file.

        Returns an empty list if the file does not exist yet. Skips
        blank lines. Raises on malformed JSON — a corrupted audit log
        is a signal, not something to silently swallow (per
        ``lint-and-validate.instructions.md``: fix the cause, do not
        suppress).
        """
        if not self._path.exists():
            return []
        entries: list[AccessLogEntry] = []
        with open(self._path, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    entries.append(AccessLogEntry.model_validate_json(stripped))
                except Exception as exc:
                    msg = f"federation_access_log: malformed line {lineno}: {exc}"
                    raise ValueError(msg) from exc
        return entries

    def query(self, peer_id: str, topic_hash: str) -> AccessLogEntry | None:
        """Return the most recent entry for a (peer_id, topic_hash) pair.

        Used by the federation server (Phase 2) to decide whether to
        return ``ALREADY_EXHAUSTED`` — if B already answered
        ``EXHAUSTIVE`` for this peer on this topic, the repeat request
        is short-circuited (contract §9, §10). Returns ``None`` when
        no prior entry exists.
        """
        matches = [
            e for e in self._iter_entries() if e.peer_id == peer_id and e.topic_hash == topic_hash
        ]
        if not matches:
            return None
        # The log is append-only, so the last match in file order is
        # the most recent. Fall back to timestamp comparison for safety
        # in case the file was concatenated out of order.
        return max(matches, key=lambda e: e.timestamp)

    def query_recent(self, peer_id: str, *, since: datetime) -> list[AccessLogEntry]:
        """Return all entries for a peer since a UTC timestamp.

        Used for audit reports — e.g. "what did peer mnemos-A pull in
        the last 24h?". Naive timestamps are assumed to be UTC (the
        log stores UTC); callers should pass a UTC-aware datetime.
        """
        return [e for e in self._iter_entries() if e.peer_id == peer_id and e.timestamp >= since]

    def count_by_trigger_code(self, peer_id: str, *, since: datetime) -> dict[TriggerCode, int]:
        """Aggregate entry counts per trigger code for a peer since a UTC time.

        Used for metrics/audit — e.g. "peer mnemos-A: 12 EXHAUSTIVE, 3
        PARTIAL, 1 REFUSED in the last 24h". Returns all five trigger
        codes keyed by the enum (zero-filled) so the caller does not
        have to handle missing keys.
        """
        counts: dict[TriggerCode, int] = {code: 0 for code in TriggerCode}
        for e in self.query_recent(peer_id, since=since):
            counts[e.trigger_code] += 1
        return counts


__all__ = [
    "DEFAULT_LOG_PATH",
    "AccessLogEntry",
    "FederationAccessLog",
    "hash_topic",
]


# A note on timezones: the contract stores timestamps in ISO-8601 UTC.
# Pydantic v2 serializes ``datetime`` fields as ISO-8601 by default.
# Callers constructing :class:`AccessLogEntry` should pass UTC-aware
# datetimes — e.g. ``datetime.now(UTC)``. This module does not coerce
# naive datetimes to UTC because doing so silently would mask a
# caller bug. A naive datetime round-trips through JSON as-is; the
# federation server (Phase 2) is responsible for passing UTC.
_ = datetime  # re-exported symbol kept for type-checkers
