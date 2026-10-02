"""MetricsStore — allowlist write path into the metrics.sqlite sidecar.

Phase A (docs/architecture.md §2-3): born-final schema, one insert per
assemble call + its blocks, keyed-HMAC fingerprints, non-fatal writes.

Host contract (the ONLY integration surface Vesma touches):
  - ``MetricsStore(path)`` — opens (and creates) the sidecar;
  - ``record_assemble(result, *, latency_ms)`` — one call after
    ``assemble_context`` built its result, at the hook/MCP boundary;
  - ``close()`` — idempotent shutdown.

Hard rules carried from the ArchCom decisions (7ec9dda3 + 061398fe):
  - write failure is non-fatal to the host: every public method swallows
    sqlite errors into a warning (TraceRecorder precedent) — a broken
    metrics plane must never break the memory server;
  - ``busy_timeout`` is 250 ms, not the prod store's 5000 — metric
    contention can never freeze the hook path;
  - the raw ``query`` is never persisted; ``file`` is stored as a stem;
  - the stats dict is never stored verbatim — an allowlisted projection
    (``_PROJECT_STAGE_STATS``) decides what stage telemetry survives;
  - content fingerprints are keyed HMAC under a per-install random key
    stored OUTSIDE the sidecar (no rotation — rotation breaks
    longitudinal uniqueness; plain hashes of governance content are
    banned, CWE-759).
"""

# ── PROVENANCE ────────────────────────────────────────────────────────
# Vendored from mnemos-vitals main (phase A2, 2026-09-22). Master copy
# + methodology: ~/LABs/Projects/Project-Mnemos/mnemos-vitals. Sync rule:
# changes land there first, then are ported in the same wave
# (drift-guard tests on both sides must stay green).

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from vesma.metrics.ledger import VerbLedgerMixin
from vesma.metrics.schema import (
    AWARENESS_EVENT_KINDS,
    RETENTION_DAYS,
    SCHEMA_SQL,
    TABLE_NAMES,
)
from vesma.metrics.schema import (
    validate_awareness_meta as validate_awareness_meta,  # re-export: sink contract
)
from vesma.metrics.schema import (
    validate_meta as validate_meta,  # re-export: part of the sink contract
)

logger = logging.getLogger("vesma.metrics.sink")

#: The assembled text is fingerprinted, never stored. Shingling for the
#: dynamism corridor (phase B) re-derives fingerprints from the same
#: keyed HMAC — the raw text never crosses this boundary.
_SHINGLE_RE = re.compile(r"\w+", re.UNICODE)
_SHINGLE_SIZE = 5

#: Stage stats that survive into ``stage_stats_json`` (allowlist, C5
#: applied to the stats dict — it is NOT persisted verbatim). Key set
#: mirrors src/vesma/assemble.py stage builders as of main `3c8f270`
#: (2026-09-20); the projection is drift-tolerant: any key not listed
#: here — including raw ``recall.query`` and future keys — is dropped.
_STAGE_STATS_ALLOWLIST = frozenset(
    {
        "stages",  # list of stage names (order contract), capped
        # recall (query itself is raw text — never listed)
        "recall.query_source",
        "recall.candidates",
        "recall.admissible",
        "recall.content_type_filtered",
        "recall.content_type_fallbacks",
        "recall.applyto_pinned",
        # ccr stage
        "ccr.enabled",
        "ccr.markers_found",
        "ccr.expanded",
        "ccr.skipped_missing",
        "ccr.skipped_budget",
        "ccr.skipped_refused",
        # filter stage (profile names are enums, capped list)
        "filter.profiles",
        # scan / align / budget stages
        "scan.blocks_scanned",
        "scan.blocks_refused",
        "align.blocks_aligned",
        "align.moved_chars",
        "budget.blocks_included",
        "budget.blocks_skipped",
        # ADR-0025/0027 optional telemetry — present only when flags on
        "recall.lanes.rules",
        "recall.lanes.decisions",
        "recall.lanes.knowledge",
        "recall.lanes.governance_excluded_from_knowledge",
        "recall.lanes.task_filtered",
        "recall.type_boost.boosted",
        "recall.lens.name",
        "recall.lens.active",
        "task_scoped",
    }
)

#: String values allowed through the projection: short enums only.
_STR_VALUE_LIMIT = 32
_LIST_VALUE_LIMIT = 16
_ENUM_STR_PATHS = frozenset({"recall.query_source", "recall.lens.name"})


def _project_stage_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Allowlisted flattening of the assemble stats dict (never verbatim).

    Walks ``stats`` two levels deep into dotted ``stage.key`` paths and
    keeps only allowlisted paths with scalar values (numbers/bools), a
    capped list of profile names, or capped enum strings. Raw text —
    ``recall.query`` above all — is structurally absent from the
    allowlist, so no code path can leak it.
    """
    if not isinstance(stats, dict):
        return {}
    out: dict[str, Any] = {}
    for stage, payload in stats.items():
        if stage == "stages":
            if isinstance(payload, list):
                # stage names are enums: drop anything else / over-length —
                # truncating drifted free text would leak a raw prefix
                names = [s for s in payload if isinstance(s, str) and len(s) <= _STR_VALUE_LIMIT]
                if names:
                    out["stages"] = names[:_LIST_VALUE_LIMIT]
            continue
        if stage in _STAGE_STATS_ALLOWLIST and (
            payload is None or isinstance(payload, (int, float, bool))
        ):
            out[stage] = payload  # bare top-level scalars (task_scoped)
            continue
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            path = f"{stage}.{key}"
            self_ok = path in _STAGE_STATS_ALLOWLIST
            if self_ok and (value is None or isinstance(value, (bool, int))):
                out[path] = value
                continue
            if self_ok and isinstance(value, float) and math.isfinite(value):
                out[path] = value
                continue  # NaN/inf never land (json would emit non-standard)
            if self_ok and path == "filter.profiles" and isinstance(value, list):
                # profile names are enums — drop strays, never truncate
                out[path] = [p for p in value if isinstance(p, str) and len(p) <= _STR_VALUE_LIMIT][
                    :_LIST_VALUE_LIMIT
                ]
                continue
            if self_ok and path in _ENUM_STR_PATHS and isinstance(value, str):
                out[path] = value[:_STR_VALUE_LIMIT]
                continue
            if isinstance(value, dict):
                # sub-dicts (recall.lanes.*, recall.lens.*) — one more level
                for sub, sub_value in value.items():
                    sub_path = f"{path}.{sub}"
                    if sub_path not in _STAGE_STATS_ALLOWLIST:
                        continue  # drift-tolerant: unknown keys dropped
                    if sub_value is None or isinstance(sub_value, (bool, int)):
                        out[sub_path] = sub_value
                    elif isinstance(sub_value, float) and not math.isfinite(sub_value):
                        continue  # NaN/inf never land
                    elif sub_path in _ENUM_STR_PATHS and isinstance(sub_value, str):
                        out[sub_path] = sub_value[:_STR_VALUE_LIMIT]
            # anything else (raw text, deeper nesting) never leaves
    return out


class MetricsStore(VerbLedgerMixin):
    """Allowlist sink into the sidecar — the guest's ONLY write path.

    Thread-safety mirrors the host's ``SQLiteStore`` bootstrap pattern
    (per-thread connections, a lock only for first-connect schema
    creation) so concurrent hook/MCP/background threads can record
    without serialising, while a broken sidecar degrades to a warning.
    """

    def __init__(self, db_path: Path, *, hmac_key: bytes | None = None) -> None:
        self.db_path = Path(db_path)
        self._local = threading.local()
        self._bootstrap_lock = threading.Lock()
        self._closed = False
        self._hmac_key = hmac_key if hmac_key is not None else self._load_or_create_key()

    # ── Key management (key lives OUTSIDE the sidecar) ───────────────────

    def _load_or_create_key(self) -> bytes:
        """Per-install random key, sibling file ``metrics.sqlite.hkey``.

        Never inside the sidecar DB: storing the key next to the
        fingerprints it protects would make them plain hashes. Not in
        config either — this file IS per-install identity for the
        plane. No rotation: rotating breaks longitudinal uniqueness of
        fingerprints (the dynamism corridor compares them across days).
        chmod 0600 (C2 parity with the main store).
        """
        key_path = self.db_path.parent / (self.db_path.name + ".hkey")
        created = False
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            if key_path.exists():
                key = key_path.read_bytes()
                if len(key) == 32:
                    return key
                logger.warning(
                    "vitals: hmac key file corrupt (len=%d) — fingerprints"
                    " disabled until the corrupt file is removed manually"
                    " (no silent rotation: forensics first)",
                    len(key),
                )
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                # concurrent bootstrap — adopt the winning writer's key
                key = key_path.read_bytes()
                return key if len(key) == 32 else b""
            created = True  # we own the file: on failure below, remove residue
            with os.fdopen(fd, "wb") as f:
                f.write(secrets.token_bytes(32))
            os.chmod(key_path, 0o600)  # O_EXCL mode may still be widened by umask
            return key_path.read_bytes()
        except OSError as exc:
            logger.warning("vitals: hmac key unavailable (fingerprints disabled): %s", exc)
            if created:
                with suppress(OSError):
                    key_path.unlink()  # never leave an unused key on disk
            return b""

    def fingerprint(self, text: str) -> str | None:
        """Keyed HMAC-SHA256 of ``text`` — the only trace of content.

        Returns ``None`` when no key is available (degraded, never fatal).
        """
        if not self._hmac_key:
            return None
        return hmac.new(self._hmac_key, text.encode("utf-8"), hashlib.sha256).hexdigest()

    def shingles(self, text: str) -> list[str]:
        """Keyed-HMAC word shingles (dynamism corridor input, phase B).

        Each shingle is HMAC'd with the same install key, so cross-request
        Jaccard is computable from the sidecar alone while raw text never
        enters it.
        """
        words = _SHINGLE_RE.findall(text)
        if not self._hmac_key:
            return []
        n = max(1, len(words) - _SHINGLE_SIZE + 1)
        raw = [" ".join(words[i : i + _SHINGLE_SIZE]) for i in range(n)]
        return [
            hmac.new(self._hmac_key, r.encode("utf-8"), hashlib.sha256).hexdigest() for r in raw
        ]

    # ── Connection (TraceRecorder-grade degradation) ──────────────────────

    def _chmod_sidecar_files(self) -> None:
        """C2 parity: the sidecar's on-disk footprint is THREE files."""
        for path in (
            self.db_path,
            self.db_path.with_name(self.db_path.name + "-wal"),
            self.db_path.with_name(self.db_path.name + "-shm"),
        ):
            if path.exists():
                os.chmod(path, 0o600)

    def _conn(self) -> sqlite3.Connection | None:
        if self._closed:
            return None
        conn = getattr(self._local, "conn", None)
        if conn is None:
            with self._bootstrap_lock:
                conn = getattr(self._local, "conn", None)
                if conn is None:
                    try:
                        self.db_path.parent.mkdir(parents=True, exist_ok=True)
                        conn = sqlite3.connect(str(self.db_path), timeout=0.25)
                        conn.row_factory = sqlite3.Row
                        # chmod BEFORE the WAL pragma: SQLite propagates the
                        # mode to -wal/-shm when it creates them.
                        os.chmod(self.db_path, 0o600)
                        conn.execute("PRAGMA journal_mode=WAL")
                        conn.execute("PRAGMA busy_timeout=250")
                        for stmt in SCHEMA_SQL:
                            conn.execute(stmt)
                        conn.commit()
                        self._chmod_sidecar_files()  # belt and braces
                        self._local.conn = conn
                    except (sqlite3.Error, OSError) as exc:
                        # OSError: mkdir/chmod can fail on FUSE/NFS mounts —
                        # the sink must degrade, never break the host.
                        logger.warning("vitals: sidecar unavailable (non-fatal): %s", exc)
                        return None
        return conn

    def _fail(self, op: str, exc: Exception) -> None:
        logger.warning("vitals: %s failed (non-fatal): %s", op, exc)

    # ── Phase A write path: the assemble domain ───────────────────────────

    def record_assemble(
        self,
        result: dict[str, Any],
        *,
        verb_row_id: int | None = None,
        latency_ms: float | None = None,
    ) -> int | None:
        """Record one assemble call + its injected blocks. Non-fatal.

        ``result`` is the ContextBlock dict returned by the host's
        ``assemble_context`` (see vesma ``assemble.py``): ``text``,
        ``blocks`` (with memory_id/score/tokens/redactions/ccr_hashes),
        ``tokens`` and ``stats``. This is the ONLY place the host needs
        to call; the boundary rule (§3) keeps the assemble pipeline
        itself write-free — recording happens after the result exists.

        ``latency_ms`` is accepted for call-site symmetry but not stored:
        per the born-final contract latency lives on the VERB row (phase
        A2); ``assemble_metrics`` carries no latency column.

        Returns the new ``assemble_metrics.id`` or ``None`` on failure.
        """
        try:
            conn = self._conn()
            if conn is None:
                return None
            ts = datetime.now(UTC).timestamp()
            tokens = result.get("tokens") or {}
            stats = result.get("stats") or {}
            blocks = result.get("blocks") or []
            file_val = result.get("file")
            session = result.get("session")
            project = result.get("project")
            mode = result.get("mode") or "sync"

            cur = conn.execute(
                "INSERT INTO assemble_metrics (verb_row_id, session, project, agent, ts,"
                " mode, budget, tokens_estimated, blocks_count, blocks_refused, redactions,"
                " ccr_expanded, query_source, file_stem, stage_stats_json, fingerprint)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    verb_row_id,
                    session,
                    project,
                    result.get("agent"),
                    ts,
                    mode,
                    tokens.get("budget", 0),
                    tokens.get("estimated", 0),
                    len(blocks),
                    (stats.get("scan") or {}).get("blocks_refused", 0),
                    sum(int(b.get("redactions") or 0) for b in blocks),
                    sum(1 for b in blocks if b.get("ccr_expanded")),
                    (stats.get("recall") or {}).get("query_source", "derived"),
                    Path(file_val).stem if file_val else None,  # stem only — never the path
                    json.dumps(_project_stage_stats(stats), separators=(",", ":")),
                    self.fingerprint(result.get("text") or ""),
                ),
            )
            metrics_id = int(cur.lastrowid or 0)  # rowids start at 1; 0 = absent
            self._record_blocks(conn, metrics_id, blocks)
            conn.commit()
            return metrics_id
        except (sqlite3.Error, ValueError, TypeError, AttributeError, OSError) as exc:
            self._fail("record_assemble", exc)
            # Roll the partial write back NOW: an open transaction would
            # commit a phantom assemble row on the NEXT successful call
            # (dishonest telemetry) and hold the WAL write lock, silently
            # dropping other threads' 250 ms-timeout writes until then.
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                with suppress(sqlite3.Error):
                    conn.rollback()
            return None

    def _record_blocks(
        self, conn: sqlite3.Connection, metrics_id: int, blocks: list[dict[str, Any]]
    ) -> None:
        for i, b in enumerate(blocks):
            conn.execute(
                "INSERT INTO injection_blocks (metrics_id, block_id, memory_id, source,"
                " score, tokens, ccr_origin) VALUES (?,?,?,?,?,?,?)",
                (
                    metrics_id,
                    f"{metrics_id}:{i}",  # opaque positional id (no content echo)
                    # "unknown" sentinel keeps row parity with blocks_count
                    # (assembly-coverage invariant) when upstream drifts;
                    # a literal "None" string would lie about a real id.
                    str(b.get("memory_id") or "unknown"),
                    str(b.get("content_type") or "memory"),
                    float(b.get("score") or 0.0),
                    int(b.get("tokens") or 0),
                    json.dumps(b.get("ccr_hashes") or [], separators=(",", ":")),
                ),
            )

    # ── W2a (ADR-0035): native-heartbeat events ──────────────────────────

    def record_awareness_event(
        self,
        *,
        kind: str,
        project: str | None = None,
        agent: str | None = None,
        session: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int | None:
        """Record one heartbeat contour event. Non-fatal; row id or None.

        The ``record_verb`` discipline over the ``awareness_events``
        table: kind checked against the born-final enum BEFORE the write,
        identity columns shape-checked (short single-line slugs), meta
        through the awareness allowlist gate — a refused meta logs a
        warning and the WHOLE write is refused, never silently dropped,
        never raised into the host (the guest contract: telemetry must
        not stop the product, and the heartbeat must never break the
        tool call it rides).
        """
        try:
            if kind not in AWARENESS_EVENT_KINDS:
                raise ValueError(f"awareness event kind not allowed: {kind!r}")
            clean_meta = validate_awareness_meta(meta)
            if clean_meta is None:
                raise ValueError("meta refused by the awareness allowlist")
            for name, val in (("project", project), ("agent", agent), ("session", session)):
                if val is not None and (not isinstance(val, str) or len(val) > 128 or "\n" in val):
                    raise ValueError(f"{name} must be a short single-line string")
            conn = self._conn()
            if conn is None:
                return None
            cur = conn.execute(
                "INSERT INTO awareness_events (ts, kind, project, agent, session, meta_json)"
                " VALUES (?,?,?,?,?,?)",
                (
                    datetime.now(UTC).timestamp(),
                    kind,
                    project,
                    agent,
                    session,
                    None if not clean_meta else json.dumps(clean_meta),
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        except (sqlite3.Error, ValueError, TypeError, AttributeError, OSError) as exc:
            self._fail("record_awareness_event", exc)
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                with suppress(sqlite3.Error):
                    conn.rollback()
            return None

    # ── Retention (C4: nightly DELETE + VACUUM; refusal to run = alert) ──

    def run_retention(self, *, now: datetime | None = None) -> dict[str, int]:
        """Apply per-table TTLs. Fail-loud to the CALLER (returns counts).

        The nightly job's failure is itself an alert (C4) — so unlike the
        write path, a retention exception propagates; a dict is returned
        on success with per-table deleted counts (0 is a healthy night).
        """
        now = now or datetime.now(UTC)
        conn = self._conn()
        if conn is None:
            raise RuntimeError("vitals: retention job cannot run — sidecar unavailable")
        deleted: dict[str, int] = {}
        # usage_reports/injection_blocks have no ts of their own — they are
        # deleted as children of assemble_metrics inside that branch.
        child_tables = {"injection_blocks", "usage_reports"}
        try:
            for table, days in RETENTION_DAYS.items():
                if table in child_tables:
                    continue  # counted implicitly via the parent delete
                if table == "verb_metrics_hourly":
                    cutoff_hour = int((now - timedelta(days=days)).timestamp() // 3600)
                    cur = conn.execute(f"DELETE FROM {table} WHERE hour < ?", (cutoff_hour,))
                elif table == "assemble_metrics":
                    cutoff: float = (now - timedelta(days=days)).timestamp()
                    # children first — their only time anchor is the parent's ts
                    conn.execute(
                        "DELETE FROM injection_blocks WHERE metrics_id IN"
                        " (SELECT id FROM assemble_metrics WHERE ts < ?)",
                        (cutoff,),
                    )
                    conn.execute(
                        "DELETE FROM usage_reports WHERE metrics_id IN"
                        " (SELECT id FROM assemble_metrics WHERE ts < ?)",
                        (cutoff,),
                    )
                    cur = conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                else:
                    cutoff_ts = (now - timedelta(days=days)).timestamp()
                    cur = conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff_ts,))
                deleted[table] = cur.rowcount
            conn.commit()
        except Exception:
            # fail-loud, but never leave the failed deletes half-open —
            # an open write transaction would freeze the hook path.
            with suppress(sqlite3.Error):
                conn.rollback()
            raise
        conn.execute("VACUUM")  # quiet-window contract; failure = job alert
        try:
            self._chmod_sidecar_files()  # WAL/SHM recreated by VACUUM — re-pin
        except OSError as exc:
            logger.warning("vitals: post-vacuum chmod failed (non-fatal): %s", exc)
        return deleted

    def close(self) -> None:
        """Close the calling thread's connection (idempotent).

        Other threads' connections close when their threads exit (the
        host owns their lifecycle; sqlite3.Connection is GC-safe).
        """
        if not self._closed:
            self._closed = True
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                with suppress(sqlite3.Error):
                    conn.close()
                self._local.conn = None

    @property
    def tables(self) -> tuple[str, ...]:
        return TABLE_NAMES
