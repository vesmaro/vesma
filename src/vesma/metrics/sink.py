"""MetricsStore — allowlist write path into the metrics.sqlite sidecar.

Phase A (docs/architecture.md §2-3): born-final schema, one insert per
assemble call + its blocks, keyed-HMAC fingerprints, non-fatal writes.

Host contract (the ONLY integration surface Vesma touches):
  - ``MetricsStore(path)`` — opens (and creates) the sidecar;
  - ``record_assemble(result, *, latency_ms)`` — one call after
    ``assemble_context`` built its result, at the hook/MCP boundary;
  - ``record_usage(metrics_id, *, block_ids_touched, ...)`` — phase C:
    one call after the model answered, closing the usage loop;
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
# Vendored from mnemos-vitals main (phase A2, 2026-09-22; phase C
# record_usage + overflow hardening ported 2026-09-29, master 698a650).
# Master copy + methodology: ~/LABs/Projects/Project-Mnemos/mnemos-vitals.
# Sync rule: changes land there first, then are ported in the same wave
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
    TURN_STATUSES,
    migrate_awareness_events_kinds,
    migrate_turn_usage_harness,
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

#: Phase C usage-loop limits (docs/architecture.md §2 ``usage_reports``).
#: The usage report is the ONLY client-supplied write into the plane —
#: every harness may call it — so its inputs are validated like hostile
#: input: refuse the WHOLE write on any non-conforming shape, loudly.
_BLOCK_ID_LIMIT = 128  # chars per opaque block id
_BLOCK_ID_MAX_ENTRIES = 256  # entries per report (before dedup)

#: nhi-15 wave 1 — turn-usage batch limits. Same hostile-input posture as
#: ``record_usage``: this is a client-supplied write into the plane.
_TURN_ID_LIMIT = 128  # chars per opaque turn id
_TURN_BATCH_MAX_ENTRIES = 500  # turns per signal (the 1 MB REST cap bounds too)
_TURN_TOKEN_LIMIT = 10**12  # same sane-report cap as ``tokens_out``
_TURN_TS_MAX = 4_102_444_800.0  # 2100-01-01 UTC — absurd-future refusal
_IDENTITY_LIMIT = 128  # harness/session/project/agent slug cap (awareness precedent)


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
                        # SEC-4 (ADR-0035 cascade): legacy wave-0 sidecars
                        # carry a five-kind CHECK that would refuse the
                        # conflict_hint_emitted funnel event — the
                        # introspection-gated rebuild migrates them onto
                        # the born-final enum (no-op on fresh/migrated
                        # files; raises sqlite3.Error into the same
                        # degrade-to-unavailable handling as above).
                        migrate_awareness_events_kinds(conn)
                        # nhi-15 wave 1: wave-0 turn_usage tables gain the
                        # harness columns + the dedup unique index (no-op
                        # on fresh/migrated files).
                        migrate_turn_usage_harness(conn)
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
        except (
            sqlite3.Error,
            ValueError,
            TypeError,
            AttributeError,
            OSError,
            OverflowError,
            ArithmeticError,
        ) as exc:
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
            # rollback discipline (restored post-rebase — review MAJOR #1):
            # a failed awareness write must not leave the thread's connection
            # holding an open WAL write transaction until its next commit.
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                with suppress(sqlite3.Error):
                    conn.rollback()
            return None

    # ── Phase C write path: the usage loop (post_llm_call annex) ──────────

    def record_usage(
        self,
        metrics_id: int,
        *,
        block_ids_touched: list[str],
        tokens_out: int | None = None,
        wrong_tool_flag: bool = False,
        ts: float | None = None,
    ) -> int | None:
        """Record one harness response for a previous assemble call. Non-fatal.

        Phase C loop closure (docs/architecture.md §2 ``usage_reports``):
        after the model call built on an assembled window, the harness
        reports which injected blocks it actually used (opaque block
        ordinals ``"<metrics_id>:<i>"`` as written by :meth:`_record_blocks`),
        the output token count, and whether the call went to the wrong
        tool. ``block_ids_touched`` is the ONLY client-supplied payload
        that ever enters the plane, so it is validated like hostile
        input — a non-conforming shape refuses the WHOLE write with a
        loud warning (never a silent partial drop, never a raise into
        the host):

          - ``block_ids_touched`` must be a ``list`` of non-empty strings,
            each <= 128 chars, no newlines, at most 256 entries (before
            dedup); duplicates are collapsed preserving first-seen order;
            an empty list is legitimate (the call used nothing);
          - ``tokens_out``: ``int >= 0`` or ``None`` (bools refused);
          - ``wrong_tool_flag``: strictly ``bool``;
          - ``metrics_id`` must reference an existing ``assemble_metrics``
            row — an unknown id is a loud refusal, not a silent orphan;
          - ``ts`` is accepted for call-site symmetry but NOT stored
            (same precedent as ``latency_ms`` in :meth:`record_assemble`):
            ``usage_reports`` is born-final without a ts column — the
            time anchor is the parent assemble row.

        Privacy by structure: the stored ids are opaque positional
        ordinals, never content; no text column exists to leak into.

        Returns the new ``usage_reports.id`` or ``None`` on refusal/failure.
        """
        try:
            if (
                not isinstance(metrics_id, int)
                or isinstance(metrics_id, bool)
                or metrics_id <= 0
                or metrics_id > 2**63 - 1
            ):
                # range bound included: a 2**64-scale int survives this check
                # only to die as OverflowError at bind — the client-supplied
                # boundary must degrade, never raise (review MAJOR)
                raise ValueError(f"metrics_id must be a positive int, got {metrics_id!r}")
            if ts is not None and not math.isfinite(float(ts)):
                raise ValueError(f"ts must be a finite number, got {ts!r}")
            if tokens_out is not None and (
                not isinstance(tokens_out, int)
                or isinstance(tokens_out, bool)
                or tokens_out < 0
                or tokens_out > 10**12
            ):
                # cap mirrors the counters limit in validate_meta — a sane
                # report never carries more; larger = garbage shape
                raise ValueError(f"tokens_out must be an int >= 0 or None, got {tokens_out!r}")
            if not isinstance(wrong_tool_flag, bool):
                raise ValueError(f"wrong_tool_flag must be strictly bool, got {wrong_tool_flag!r}")
            if not isinstance(block_ids_touched, list):
                raise ValueError("block_ids_touched must be a list of opaque strings")
            if len(block_ids_touched) > _BLOCK_ID_MAX_ENTRIES:
                raise ValueError(
                    f"block_ids_touched has {len(block_ids_touched)} entries"
                    f" (max {_BLOCK_ID_MAX_ENTRIES})"
                )
            for bid in block_ids_touched:
                # "" is refused: an empty string identifies no block and is
                # a drifted shape, not a legitimate "touched nothing" report.
                if not isinstance(bid, str) or not bid or len(bid) > _BLOCK_ID_LIMIT:
                    raise ValueError(
                        f"block id must be a non-empty string <= {_BLOCK_ID_LIMIT} chars,"
                        f" got {bid!r}"
                    )
                if "\n" in bid or "\r" in bid:
                    raise ValueError("block id must be a single line")
            unique_ids = list(dict.fromkeys(block_ids_touched))  # dedup, order preserved

            conn = self._conn()
            if conn is None:
                return None
            # FK validation by hand AND atomically: the sidecar does not
            # enable the foreign_keys pragma, and a check-then-insert race
            # against the retention job could strand an immortal orphan
            # (check and insert are NOT in one snapshot). The conditional
            # INSERT ... SELECT ... WHERE EXISTS makes validation and write
            # a single statement under one write lock; rowcount 0 = the
            # parent vanished between validation passes = loud refusal.
            cur = conn.execute(
                "INSERT INTO usage_reports (metrics_id, block_ids_touched_json,"
                " tokens_out, wrong_tool_flag)"
                " SELECT ?,?,?,? WHERE EXISTS"
                " (SELECT 1 FROM assemble_metrics WHERE id = ?)",
                (
                    metrics_id,
                    json.dumps(unique_ids, separators=(",", ":")),
                    tokens_out,
                    int(wrong_tool_flag),
                    metrics_id,
                ),
            )
            if cur.rowcount == 0:
                raise ValueError(
                    f"metrics_id {metrics_id} unknown — no assemble_metrics parent;"
                    " usage report refused"
                )
            conn.commit()
            return int(cur.lastrowid or 0)
        except (
            sqlite3.Error,
            ValueError,
            TypeError,
            AttributeError,
            OSError,
            OverflowError,
            ArithmeticError,
        ) as exc:
            self._fail("record_usage", exc)
            # Same rollback discipline as record_assemble/record_verb: a
            # partial write must never linger for the next commit.

            conn = getattr(self._local, "conn", None)
            if conn is not None:
                with suppress(sqlite3.Error):
                    conn.rollback()
            return None

    # ── nhi-15 wave 1 write path: the money plane (turn_usage) ────────────

    def record_turn_usage_batch(
        self,
        *,
        harness_id: str,
        session: str,
        turns: list[dict[str, Any]],
        project: str | None = None,
        agent: str | None = None,
    ) -> tuple[int, int] | None:
        """Record a batch of harness turns into ``turn_usage``. Non-fatal.

        The money plane's ONLY write path (nhi-15 wave 1): one harness
        signal carries the turns of ONE harness session; the unique
        index ``(harness_id, session, turn_id)`` makes replays
        idempotent — ``INSERT OR IGNORE`` dedups, so a harness may
        re-post its session's turns on every Stop event without
        double-counting a single billed token.

        Batch discipline: ALL rows of one signal go in ONE implicit
        transaction with a single COMMIT at the end (never a commit per
        turn) — and a failure rolls the WHOLE batch back (a partial
        money ledger would lie). Validation refuses the WHOLE batch
        BEFORE any write (loud, never a silent drop):

          - ``harness_id`` / ``session``: non-empty single-line strings
            (64 / 128 chars — the awareness identity posture);
          - ``project`` / ``agent``: optional caller-declared slugs,
            same shape;
          - each turn: ``turn_id`` (non-empty single-line, 128 chars),
            ``ts`` (finite epoch seconds, not absurdly future),
            ``status`` in the born-final enum (default ``completed``),
            optional ``model`` (single-line, 128 chars), and
            non-negative integer token counters (bools refused) capped
            at 10**12 — ids and counters only, never text (the C3
            posture of the whole sidecar).

        Failure semantics (DELIBERATE split from ``record_usage``'s
        swallow-all): a CONTRACT violation raises ``ValueError`` — this
        plane has a synchronous REST caller that must answer 4xx
        honestly, and a validation refusal is client's, not the host's,
        to own. Only INFRASTRUCTURE failures (sidecar unavailable,
        sqlite error) return ``None`` — the host never breaks on
        telemetry, and the caller answers 503.
        """
        try:
            for name, val in (("harness_id", harness_id), ("session", session)):
                if (
                    not isinstance(val, str)
                    or not val
                    or len(val) > _IDENTITY_LIMIT
                    or "\n" in val
                    or "\r" in val
                ):
                    raise ValueError(f"{name} must be a non-empty single-line string")
            for opt_name, opt_val in (("project", project), ("agent", agent)):
                if opt_val is None:
                    continue
                if (
                    not isinstance(opt_val, str)
                    or not opt_val
                    or len(opt_val) > _IDENTITY_LIMIT
                    or "\n" in opt_val
                    or "\r" in opt_val
                ):
                    raise ValueError(f"{opt_name} must be a non-empty single-line string")
            if not isinstance(turns, list) or not turns:
                raise ValueError("turns must be a non-empty list of turn objects")
            if len(turns) > _TURN_BATCH_MAX_ENTRIES:
                raise ValueError(f"turns has {len(turns)} entries (max {_TURN_BATCH_MAX_ENTRIES})")

            rows: list[tuple[Any, ...]] = []
            for turn in turns:
                if not isinstance(turn, dict):
                    raise ValueError("each turn must be an object")
                turn_id = turn.get("turn_id")
                if (
                    not isinstance(turn_id, str)
                    or not turn_id
                    or len(turn_id) > _TURN_ID_LIMIT
                    or "\n" in turn_id
                    or "\r" in turn_id
                ):
                    raise ValueError(f"turn_id must be a non-empty string, got {turn_id!r}")
                ts = turn.get("ts")
                if (
                    isinstance(ts, bool)
                    or not isinstance(ts, (int, float))
                    or not math.isfinite(float(ts))
                    or ts < 0
                    or ts > _TURN_TS_MAX
                ):
                    raise ValueError(f"ts must be finite epoch seconds, got {ts!r}")
                status = turn.get("status") or "completed"
                if status not in TURN_STATUSES:
                    raise ValueError(f"status must be one of {TURN_STATUSES}, got {status!r}")
                model = turn.get("model")
                if model is not None and (
                    not isinstance(model, str)
                    or not model
                    or len(model) > _TURN_ID_LIMIT
                    or "\n" in model
                    or "\r" in model
                ):
                    raise ValueError(f"model must be a short single-line string, got {model!r}")
                counters = []
                for key in (
                    "input_tokens",
                    "cached_read_tokens",
                    "cached_write_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                ):
                    value = turn.get(key, 0)
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value < 0
                        or value > _TURN_TOKEN_LIMIT
                    ):
                        raise ValueError(f"{key} must be an int >= 0, got {value!r}")
                    counters.append(value)
                rows.append(
                    (
                        session,
                        turn_id,
                        harness_id,
                        project,
                        agent,
                        float(ts),
                        status,
                        model,
                        *counters,
                    )
                )

            conn = self._conn()
            if conn is None:
                return None
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO turn_usage (session, turn_id, harness_id, project,"
                " agent, ts, status, model, input_tokens, cached_read_tokens,"
                " cached_write_tokens, output_tokens, reasoning_tokens)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            accepted = conn.total_changes - before
            conn.commit()
            return accepted, len(rows) - accepted
        except (
            sqlite3.Error,
            TypeError,
            AttributeError,
            OSError,
            OverflowError,
            ArithmeticError,
        ) as exc:
            # ValueError is DELIBERATELY absent: a contract refusal is
            # re-raised to the REST caller (4xx), only infrastructure
            # failures degrade to None (the docstring's failure split).
            self._fail("record_turn_usage_batch", exc)
            # Batch rollback discipline (the record_assemble precedent):
            # a failed money batch must never commit partially nor hold
            # the WAL write lock open until the next successful call.
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
