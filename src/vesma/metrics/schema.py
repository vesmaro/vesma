"""Born-final sidecar schema — the allowlist is the whole contract.

Phase A/A2 (docs/architecture.md §2; ADR-0026 vesma C1-C5). The metrics
sqlite sidecar is created with this FINAL schema on first open — no
migrations exist, by decision D-0002 (a sidecar with migrations would
re-create the very operational risk (migration trains) that the
sidecar exists to avoid).

Privacy is by structure, not by filter (ADR-0026):
  - no column anywhere may hold raw text: the recall ``query`` is never
    persisted, ``file`` is stored as a stem only, memory ``content`` never
    leaves the assemble pipeline;
  - the verb ledger has no session/principal columns — forever (C3);
  - content fingerprints are keyed HMAC under a per-install random key
    that is never stored in the sidecar (plain hashes of governance-class
    content are banned, CWE-759);
  - ``meta_json`` values pass an allowlist fail-closed (C5): unknown key
    → the write is refused, never silently dropped.
"""

# ── PROVENANCE ────────────────────────────────────────────────────────
# Vendored from mnemos-vitals main (phase A2, 2026-09-22). Master copy
# + methodology: ~/LABs/Projects/Project-Mnemos/mnemos-vitals. Sync rule:
# changes land there first, then are ported in the same wave
# (drift-guard tests on both sides must stay green).

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

SIDECAR_FILENAME = "metrics.sqlite"

#: Retention policy (days). Raw verb rows are cheap to lose; the hourly
#: rollup outlives them; the assemble family is the analysis corpus.
#: The awareness shadow events ride the assemble-family horizon (the
#: wave 0/1 funnel reads them over weeks, not months).
RETENTION_DAYS: dict[str, int] = {
    "verb_metrics": 30,
    "verb_metrics_hourly": 400,
    "assemble_metrics": 90,
    "injection_blocks": 90,
    "usage_reports": 90,
    "awareness_events": 90,
    #: nhi-15 (money baseline, 2026-10-08): the money baseline window is
    #: longitudinal by definition (the 2026-10-09 tick) — rides the same
    #: 400-day horizon as the hourly rollup.
    "turn_usage": 400,
}


@dataclass(frozen=True)
class Column:
    name: str
    decl: str

    @property
    def is_integer_pk(self) -> bool:
        return "INTEGER PRIMARY KEY" in self.decl


@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: tuple[Column, ...]

    @property
    def column_names(self) -> frozenset[str]:
        return frozenset(c.name for c in self.columns)

    @property
    def create_sql(self) -> str:
        body = ",\n    ".join(f"{c.name} {c.decl}" for c in self.columns)
        return f"CREATE TABLE IF NOT EXISTS {self.name} (\n    {body}\n)"


def _table(name: str, *cols: tuple[str, str]) -> TableSchema:
    return TableSchema(name=name, columns=tuple(Column(n, d) for n, d in cols))


# ── The five born-final tables ────────────────────────────────────────────────

#: Universal verb backbone — one row per call on any surface. C3: no
#: session/principal columns, ever; ``project``/``agent`` are slugs that
#: surface owners themselves declare, not principals.
VERB_METRICS = _table(
    "verb_metrics",
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("ts", "REAL NOT NULL"),
    ("surface", "TEXT NOT NULL CHECK (surface IN ('mcp','rest','background','cli'))"),
    ("verb", "TEXT NOT NULL"),
    ("status", "TEXT NOT NULL CHECK (status IN ('ok','error'))"),
    ("status_code", "INTEGER"),
    ("latency_ms", "REAL NOT NULL"),
    ("project", "TEXT"),
    ("agent", "TEXT"),
    ("meta_json", "TEXT"),
)

#: Hourly rollup — the only table the Prometheus exposer may read (survives
#: restarts; no per-event exposition). Percentiles are computed at rollup
#: time; ``avg`` is deliberately absent — means hide tails (§4).
VERB_HOURLY = _table(
    "verb_metrics_hourly",
    ("hour", "INTEGER NOT NULL"),  # unix epoch hours
    ("surface", "TEXT NOT NULL"),
    ("verb", "TEXT NOT NULL"),
    ("status", "TEXT NOT NULL"),
    ("project", "TEXT"),
    ("count", "INTEGER NOT NULL"),
    ("p50_ms", "REAL"),
    ("p95_ms", "REAL"),
    ("p99_ms", "REAL"),
    ("max_ms", "REAL"),
)

#: Specialized domain table of assemble calls (NOT a view over the ledger —
#: carries tokens, stage counts and keyed fingerprints). Linked to its verb
#: row when the call crossed a metered surface; the hook path may have no
#: verb row (hook-only writes are legit and the link stays NULL).
ASSEMBLE_METRICS = _table(
    "assemble_metrics",
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("verb_row_id", "INTEGER REFERENCES verb_metrics(id)"),
    ("session", "TEXT"),
    ("project", "TEXT NOT NULL"),
    ("agent", "TEXT"),
    ("ts", "REAL NOT NULL"),
    ("mode", "TEXT NOT NULL"),
    ("budget", "INTEGER NOT NULL"),
    ("tokens_estimated", "INTEGER NOT NULL"),
    ("blocks_count", "INTEGER NOT NULL"),
    ("blocks_refused", "INTEGER NOT NULL"),
    ("redactions", "INTEGER NOT NULL"),
    ("ccr_expanded", "INTEGER NOT NULL DEFAULT 0"),
    ("query_source", "TEXT NOT NULL CHECK (query_source IN ('explicit','derived'))"),
    ("file_stem", "TEXT"),
    ("stage_stats_json", "TEXT"),
    ("fingerprint", "TEXT"),  # keyed-HMAC of the assembled block text
)

#: One row per injected block — the composition plane (what the window was
#: made of). ``ccr_origin`` holds origin hashes of expanded markers; they
#: are already digests produced by the host, not sidecar secrets.
INJECTION_BLOCKS = _table(
    "injection_blocks",
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("metrics_id", "INTEGER NOT NULL REFERENCES assemble_metrics(id)"),
    ("block_id", "TEXT NOT NULL"),
    ("memory_id", "TEXT NOT NULL"),
    ("source", "TEXT NOT NULL"),
    ("score", "REAL NOT NULL"),
    ("tokens", "INTEGER NOT NULL"),
    ("ccr_origin", "TEXT"),
)

#: Phase C loop — one row per harness response. ``block_ids_touched_json``
#: is an allowlisted JSON array of opaque block ids, never text.
USAGE_REPORTS = _table(
    "usage_reports",
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("metrics_id", "INTEGER NOT NULL REFERENCES assemble_metrics(id)"),
    ("block_ids_touched_json", "TEXT"),
    ("tokens_out", "INTEGER"),
    ("wrong_tool_flag", "INTEGER"),
)

#: W2a (ADR-0035): native-heartbeat shadow events — one row per observed
#: contour fact (peer_write / delta_available / heartbeat_delivery /
#: heartbeat_suppressed / tool_call / conflict_hint_emitted). Vesma-wave
#: addition (not part of the mnemos-vitals vendored surface): additive
#: table in the born-final sidecar — ``CREATE TABLE IF NOT EXISTS`` in
#: the connect script, so existing sidecars gain it on their next open,
#: no migration machinery. ZERO PEER CONTENT (CWE-359):
#: ``project``/``agent``/``session`` carry the CALLER's identity slugs
#: (surface-declared, not principals), and ``meta_json`` passes the
#: awareness allowlist below — tool ids, int counters, state/reason
#: enums and server-side cursor timestamps. Peer agent ids, goals and
#: record text never land here (the verb-ledger C3 posture: awareness
#: events add the session the ADR-0035 funnel needs, nothing else).
AWARENESS_EVENTS = _table(
    "awareness_events",
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("ts", "REAL NOT NULL"),
    (
        "kind",
        "TEXT NOT NULL CHECK (kind IN ('peer_write','delta_available',"
        "'heartbeat_delivery','heartbeat_suppressed','tool_call',"
        "'conflict_hint_emitted'))",
    ),
    ("project", "TEXT"),
    ("agent", "TEXT"),
    ("session", "TEXT"),
    ("meta_json", "TEXT"),
)

#: Legal ``kind`` values (mirrored from the born-final CHECK for cheap
#: refusal before the write — the ``SURFACES`` precedent).
AWARENESS_EVENT_KINDS: tuple[str, ...] = (
    "peer_write",
    "delta_available",
    "heartbeat_delivery",
    "heartbeat_suppressed",
    "tool_call",
    "conflict_hint_emitted",
)

# ── nhi-15 (2026-10-08): the money plane — additive born-final tables ─────────
#
# Vesma-wave additions (the ``AWARENESS_EVENTS`` precedent: additive
# ``CREATE TABLE IF NOT EXISTS`` in the connect script, so EXISTING
# sidecars gain them on their next open — no migration machinery, the
# D-0002 rule). Two tables:

#: One row per HARNESS TURN (one user request → one model answer, the
#: billing unit of record). The harness-side ``turn_usage`` (ZCode
#: db.sqlite, 6.0.0 era) already proves the shape: input_tokens is the
#: CUMULATIVE billed input of the turn (history is re-read every
#: request) — that is what providers bill, so it is what the money
#: baseline counts. ``cached_read_tokens`` is carried FROM BIRTH (the
#: nhi-15 v1 requirement: providers report cached input; when a write
#: path lands, the cached share is already ledgerable — no ALTER TABLE
#: train). ``model`` is the turn's DOMINANT model by input tokens when
#: the harness splits a turn across models (nullable — multi-model
#: attribution stays with the harness's model-level ledger).
#: ``cached_write_tokens`` is stored for the future cache-write rate
#: (Z.AI storage is limited-time free) but NOT billed in v1.
#: PRIVACY: ids and integer counters only — no prompt, no output text
#: (the C3 posture of the whole sidecar).
TURN_USAGE = _table(
    "turn_usage",
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("session", "TEXT"),
    ("turn_id", "TEXT"),
    # nhi-15 wave 1 (2026-10-08): harness attribution — WHICH harness
    # produced the turn (owner directive: the store must stay independent
    # of any one harness's ledger, and money baselines must be comparable
    # ACROSS harnesses). 'unknown' keeps legacy rows meaningful without a
    # backfill; wave-0 sidecars gain the columns via the idempotent
    # ALTER migration below (``migrate_turn_usage_harness``).
    ("harness_id", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("project", "TEXT"),  # caller-declared slug (the verb-ledger convention)
    ("agent", "TEXT"),
    ("ts", "REAL NOT NULL"),  # unix epoch SECONDS (sidecar convention)
    (
        "status",
        "TEXT NOT NULL DEFAULT 'completed'"
        " CHECK (status IN ('completed','error','cancelled','other'))",
    ),
    ("model", "TEXT"),
    ("input_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("cached_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("cached_write_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("output_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("reasoning_tokens", "INTEGER NOT NULL DEFAULT 0"),
)

#: Legal ``turn_usage.status`` values (mirrored from the CHECK — the
#: ``SESSION_TYPES`` precedent: cheap refusal before the write).
TURN_STATUSES: tuple[str, ...] = ("completed", "error", "cancelled", "other")

#: Deduplication — one row per (harness, session, turn). Created by
#: :func:`migrate_turn_usage_harness` (NOT in ``INDEXES_SQL``: the flat
#: SCHEMA_SQL pass runs before the column migration, and a wave-0 table
#: without ``harness_id`` would fail the index create and take the whole
#: connect down). NULL session/turn_id never collide (SQLite NULL
#: semantics), but the write path requires both, so replayed signals
#: dedup exactly.
TURN_USAGE_DEDUPE_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_turn_dedupe ON turn_usage(harness_id, session, turn_id)"
)

#: Wave-0 sidecars carry ``turn_usage`` WITHOUT the harness columns; the
#: additive ALTER set below brings them onto the born-final shape
#: (idempotent: each column is added only when ``PRAGMA table_info``
#: lacks it).
TURN_USAGE_HARNESS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("harness_id", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("project", "TEXT"),
    ("agent", "TEXT"),
)

#: Operator-assigned session labels — the ``session_type`` axis of the
#: money baseline (task / chat / background). Rows are written ONLY by
#: the explicit ``vesma metrics session-type`` CLI (and tests); nothing
#: in the server writes here, so the table is NOT in ``RETENTION_DAYS``
#: (operator curation is not telemetry — TTL must never eat a label
#: the owner set by hand).
SESSION_LABELS = _table(
    "session_labels",
    ("session", "TEXT PRIMARY KEY"),
    (
        "session_type",
        "TEXT NOT NULL DEFAULT 'unclassified'"
        " CHECK (session_type IN ('task','chat','background','unclassified'))",
    ),
    ("updated_ts", "REAL NOT NULL"),
    ("updated_by", "TEXT"),  # e.g. 'cli', 'harness:zcode' — provenance slug
)

#: Legal ``session_type`` values (mirrored from the CHECK — cheap
#: refusal before the write).
SESSION_TYPES: tuple[str, ...] = ("task", "chat", "background", "unclassified")

#: Harness-agnostic default for unlabeled sessions.
SESSION_TYPE_DEFAULT = "unclassified"


def sidecar_path(data_dir: Any) -> Any:
    """Resolve the sidecar's path under ``data_dir`` — the ONLY way code
    outside the vendored metrics package may reach for the file. The C1
    tripwire (``test_sidecar_referenced_only_by_metrics_package``)
    keeps the sidecar's NAME itself vendored: bug-report / backup /
    export / federation paths stay blind to the sidecar, and operator
    surfaces (the ``vesma metrics`` CLI) go through this helper.
    """
    from pathlib import Path

    return Path(data_dir) / SIDECAR_FILENAME


TABLE_SCHEMAS: dict[str, TableSchema] = {
    t.name: t
    for t in (
        VERB_METRICS,
        VERB_HOURLY,
        ASSEMBLE_METRICS,
        INJECTION_BLOCKS,
        USAGE_REPORTS,
        AWARENESS_EVENTS,
        TURN_USAGE,
        SESSION_LABELS,
    )
}
TABLE_NAMES: tuple[str, ...] = tuple(TABLE_SCHEMAS)

INDEXES_SQL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_verb_ts ON verb_metrics(ts)",
    "CREATE INDEX IF NOT EXISTS idx_verb_surface_verb_ts ON verb_metrics(surface, verb, ts)",
    "CREATE INDEX IF NOT EXISTS idx_hourly_key"
    " ON verb_metrics_hourly(hour, surface, verb, status, project)",
    "CREATE INDEX IF NOT EXISTS idx_assemble_session_ts ON assemble_metrics(session, ts)",
    "CREATE INDEX IF NOT EXISTS idx_assemble_project_ts ON assemble_metrics(project, ts)",
    "CREATE INDEX IF NOT EXISTS idx_injection_metrics ON injection_blocks(metrics_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_metrics ON usage_reports(metrics_id)",
    "CREATE INDEX IF NOT EXISTS idx_awareness_kind_ts ON awareness_events(kind, ts)",
    "CREATE INDEX IF NOT EXISTS idx_awareness_project_ts ON awareness_events(project, ts)",
    "CREATE INDEX IF NOT EXISTS idx_turn_ts ON turn_usage(ts)",
    "CREATE INDEX IF NOT EXISTS idx_turn_session_ts ON turn_usage(session, ts)",
)

#: C5 — ``meta_json`` allowlist, fail-closed. Unknown key → the write is
#: refused with a warning, never silently dropped. Values are validated to
#: be scalar/JSON-safe by the sink before they ever reach SQLite.
META_ALLOWLIST: frozenset[str] = frozenset(
    {
        "error_type",  # exception CLASS name only — text never
        "budget",  # mcp-only: the requested budget
        "retry",  # int, background surfaces
        "exit_code",  # int, CLI surface
        "queue_depth",  # int, processor/federation points
        "items",  # int count, background points
        "rule_id",  # scanner/watcher rule identifier (id, not text)
        "detector",  # scanner detector NAME (id, not matched text)
        "peer_id",  # federation peer slug
        "trigger_code",  # federation pull code
        "counters",  # dict of small ints (e.g. ccr cleanup tallies)
    }
)

SCHEMA_SQL: tuple[str, ...] = tuple([*(t.create_sql for t in TABLE_SCHEMAS.values()), *INDEXES_SQL])


# ── SEC-4 (ADR-0035 cascade 2026-10-01): the awareness kind-CHECK migration ────

#: Marker substring that identifies the CURRENT awareness_events CHECK.
#: A sidecar whose ``sqlite_master.sql`` for the table lacks it carries
#: the wave-0 five-kind CHECK and is rebuilt onto the born-final shape.
_AWARENESS_KINDS_MARKER: str = "conflict_hint_emitted"

#: Rebuild-table name (the ``memory_edges_a0_rebuild`` pattern — a crash
#: before COMMIT can only leave this orphan, converged on the next open).
_AWARENESS_KINDS_REBUILD_TABLE: str = "awareness_events_sec4_rebuild"


def migrate_awareness_events_kinds(conn: Any) -> None:
    """Extend ``awareness_events`` onto the six-kind CHECK — sidecar twin
    of the main store's ``_migrate_a0_edges_kinds`` (ADR-0030 A0).

    SQLite CHECK constraints cannot be ALTERed, and the metrics sidecar
    gains ``CREATE TABLE IF NOT EXISTS`` updates only on FRESH files —
    an existing wave-0 sidecar would keep refusing the
    ``conflict_hint_emitted`` kind forever (SEC-4: the funnel event the
    ADR-0035 delivery notes list gets silently dropped by the sink's own
    fail-closed write). Detection is by the stored DDL text: fresh
    sidecars (created in the final shape by ``SCHEMA_SQL``) and
    already-migrated sidecars skip; a missing table is left to
    ``SCHEMA_SQL``.

    The rebuild is the create+copy+drop+rename sequence inside ONE
    explicit transaction (SQLite DDL is transactional): a crash before
    COMMIT rolls back to the intact legacy table and converges a
    possible orphan rebuild table first; a re-check inside the
    transaction keeps two racing thread-local connects from double
    rebuilding. ALL rows copy (every legacy kind is legal in the new
    enum — the 90-day awareness funnel must not lose its denominator).

    Raises whatever sqlite raises — the caller (``MetricsStore._conn``)
    degrades to "sidecar unavailable" the same way it does for any other
    bootstrap fault; telemetry never stops the host.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='awareness_events'"
    ).fetchone()
    if row is None or row[0] is None or _AWARENESS_KINDS_MARKER in str(row[0]):
        # Fresh/migrated/absent — converge a possible orphan and no-op.
        conn.execute(f"DROP TABLE IF EXISTS {_AWARENESS_KINDS_REBUILD_TABLE}")  # nosec B608
        conn.commit()
        return
    new_ddl = AWARENESS_EVENTS.create_sql.replace(
        f"CREATE TABLE IF NOT EXISTS {AWARENESS_EVENTS.name} (",
        f"CREATE TABLE {_AWARENESS_KINDS_REBUILD_TABLE} (",
    )
    try:
        conn.executescript(
            "BEGIN IMMEDIATE;\n"  # nosec B608 - static literal
            f"DROP TABLE IF EXISTS {_AWARENESS_KINDS_REBUILD_TABLE};\n"  # nosec B608
            + new_ddl.rstrip().rstrip(";")
            + ";\n"  # nosec B608 - derived from the static table schema
            + "INSERT INTO "
            + _AWARENESS_KINDS_REBUILD_TABLE  # nosec B608 - static copy stmt
            + " (id, ts, kind, project, agent, session, meta_json) "
            "SELECT id, ts, kind, project, agent, session, meta_json "
            "FROM awareness_events;\n"
            "DROP TABLE awareness_events;\n"
            f"ALTER TABLE {_AWARENESS_KINDS_REBUILD_TABLE} RENAME TO awareness_events;\n"
            "COMMIT;"
        )
    except Exception:
        if getattr(conn, "in_transaction", False):
            conn.execute("ROLLBACK")
        raise
    # The rename orphans the rebuilt table from its indexes (they died
    # with the dropped legacy table) — re-assert the full index set
    # (idempotent IF NOT EXISTS, the post-rename re-exec of the A0
    # pattern).
    for stmt in INDEXES_SQL:
        conn.execute(stmt)
    conn.commit()


# ── nhi-15 wave 1: the turn_usage harness-attribution migration ───────────────


def migrate_turn_usage_harness(conn: Any) -> None:
    """Bring wave-0 ``turn_usage`` tables onto the harness-attribution
    shape — idempotent, additive, runs on EVERY sidecar open.

    Fresh sidecars are created in the final shape by ``SCHEMA_SQL``, so
    the PRAGMA check finds every column and the function costs one
    pragma + one ``CREATE UNIQUE INDEX IF NOT EXISTS`` no-op. Wave-0
    tables (born between the money-plane schema and this write path)
    gain ``harness_id``/``project``/``agent`` via ``ALTER TABLE ADD
    COLUMN`` — additive, row-preserving, and convergent under crashes
    (an interrupted ALTER rolls back whole; the next open re-checks).

    The dedup index is created HERE, not in ``INDEXES_SQL``: the flat
    SCHEMA_SQL pass runs before this migration, and a wave-0 table
    without ``harness_id`` would fail the index create and take the
    whole connect down.

    Raises whatever sqlite raises — the caller (``MetricsStore._conn``)
    degrades to "sidecar unavailable", same as every bootstrap fault.
    """
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(turn_usage)").fetchall()}
    if not cols:
        return  # absent — SCHEMA_SQL owns creation
    for name, decl in TURN_USAGE_HARNESS_COLUMNS:
        if name not in cols:
            # Static schema literals from the born-final table definition
            # (no user input can reach here).
            conn.execute(f"ALTER TABLE turn_usage ADD COLUMN {name} {decl}")  # nosec B608
    conn.execute(TURN_USAGE_DEDUPE_INDEX_SQL)


#: Security invariant inputs — the exposer computes these from gates and
#: canaries, never from non-fatal telemetry (RL-S4).
__all__ = [
    "AWARENESS_EVENT_KINDS",
    "AWARENESS_META_ALLOWLIST",
    "META_ALLOWLIST",
    "RETENTION_DAYS",
    "SCHEMA_SQL",
    "SESSION_LABELS",
    "SESSION_TYPES",
    "SESSION_TYPE_DEFAULT",
    "SIDECAR_FILENAME",
    "TABLE_NAMES",
    "TABLE_SCHEMAS",
    "TURN_STATUSES",
    "TURN_USAGE",
    "TURN_USAGE_DEDUPE_INDEX_SQL",
    "TURN_USAGE_HARNESS_COLUMNS",
    "sidecar_path",
]


# ── C5 meta gate (pure, enforced before any verb write) ──────────────────────

#: ``error_type`` must look like an exception CLASS name — exception text
#: never enters the sidecar.
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,63}$")
#: ``counters`` keys are identifier-capped (N4 — same drift-leak class the
#: stats projection closed).
_COUNTER_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")

_META_STR_LIMIT = 64
#: Keys whose values are integers by definition — strings refused.
_INT_ONLY_KEYS = frozenset({"exit_code", "retry", "queue_depth", "items", "budget"})


def validate_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """C5 gate for verb ``meta_json`` — fail-closed, enforced.

    Returns the sanitised dict, or ``None`` when the meta must be
    REFUSED: unknown key (not in ``META_ALLOWLIST``), non-scalar value,
    non-finite float, over-long string, ``error_type`` that is not a
    class name, or a ``counters`` dict with bad keys/sizes. The caller
    logs the refusal — loud, never a silent drop, never fatal to the
    host.
    """
    if meta is None:
        return {}
    if not isinstance(meta, dict):
        return None
    clean: dict[str, Any] = {}
    for key, value in meta.items():
        if key not in META_ALLOWLIST:
            return None
        if key == "counters":
            # check BEFORE the scalar branch: bool/int would otherwise slip
            # past the dict validation (m6)
            if (
                not isinstance(value, dict)
                or len(value) > 16
                or not all(isinstance(k, str) and _COUNTER_KEY_RE.match(k) for k in value)
                or not all(
                    isinstance(v, int) and not isinstance(v, bool) and abs(v) <= 10**12
                    for v in value.values()
                )
            ):
                return None
            clean[key] = dict(value)
        elif value is None or isinstance(value, (bool, int)):
            clean[key] = value
        elif isinstance(value, float):
            if not math.isfinite(value):  # NaN/inf: json would emit garbage
                return None
            clean[key] = value
        elif isinstance(value, str) and len(value) <= _META_STR_LIMIT:
            if key == "error_type" and not _ERROR_TYPE_RE.match(value):
                return None
            if key in _INT_ONLY_KEYS:
                return None  # integer-by-definition key carrying a string
            clean[key] = value
        else:
            return None
    return clean


# ── W2a (ADR-0035): the awareness-event meta gate ─────────────────────────────


#: C5 twin for ``awareness_events.meta_json`` — fail-closed allowlist.
#: Values are the heartbeat contour's observable facts only: the
#: canonical tool id, integer line/token counters, the calm|delta state
#: enum, the born-final suppression reason enum, and the server-side
#: cursor timestamps (high-water marks of the caller's own consumption —
#: never peer content). Keys outside this set refuse the WHOLE write.
AWARENESS_META_ALLOWLIST: frozenset[str] = frozenset(
    {
        "tool",  # canonical tool id (id, not text)
        "lines",  # int, rendered line count
        "tokens_est",  # int, same tokenizer as the render (honest ledger)
        "state",  # "calm" | "delta" (heartbeat_delivery)
        "cursor_before",  # server-side ISO cursor timestamp
        "cursor_after",
        "reason",  # suppression reason enum (heartbeat_suppressed)
    }
)

#: Enum domains enforced inside the awareness meta gate.
AWARENESS_STATES: frozenset[str] = frozenset({"calm", "delta"})
AWARENESS_SUPPRESS_REASONS: frozenset[str] = frozenset(
    {
        "no_delta",
        "rate_cap",
        "budget",
        "presence_stale",
        "probe_error",
    }
)

#: Integer-by-definition keys (strings refused) and enum-by-definition
#: keys (values checked against their domain).
_AWARENESS_INT_ONLY_KEYS = frozenset({"lines", "tokens_est"})
_AWARENESS_ENUM_KEYS: dict[str, frozenset[str]] = {
    "state": AWARENESS_STATES,
    "reason": AWARENESS_SUPPRESS_REASONS,
}

#: Cursor stamps are ISO-8601 with offset — bounded shape, bounded length.
_AWARENESS_CURSOR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.+]+Z?$")


def validate_awareness_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """Awareness-event meta gate — same fail-closed contract as
    :func:`validate_meta` over the awareness allowlist.

    Returns the sanitised dict, or ``None`` when the meta must be
    REFUSED: unknown key, non-scalar value, non-finite float, over-long
    string, an integer-by-definition key carrying a string, an enum key
    outside its born-final domain, or a cursor stamp that is not an
    ISO-shaped server timestamp. The caller logs the refusal — loud,
    never silently dropped, never fatal to the host.
    """
    if meta is None:
        return {}
    if not isinstance(meta, dict):
        return None
    clean: dict[str, Any] = {}
    for key, value in meta.items():
        if key not in AWARENESS_META_ALLOWLIST:
            return None
        if key in _AWARENESS_INT_ONLY_KEYS:
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 10**9:
                return None
            clean[key] = value
        elif key in _AWARENESS_ENUM_KEYS:
            if value not in _AWARENESS_ENUM_KEYS[key]:
                return None
            clean[key] = value
        elif key in ("cursor_before", "cursor_after"):
            if value is not None and (
                not isinstance(value, str)
                or len(value) > _META_STR_LIMIT
                or not _AWARENESS_CURSOR_RE.match(value)
            ):
                return None
            clean[key] = value
        elif value is None or isinstance(value, (bool, int)):
            clean[key] = value
        elif isinstance(value, float):
            if not math.isfinite(value):
                return None
            clean[key] = value
        elif isinstance(value, str) and len(value) <= _META_STR_LIMIT:
            clean[key] = value
        else:
            return None
    return clean
