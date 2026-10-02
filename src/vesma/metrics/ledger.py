"""Universal verb ledger + hourly rollup (phase A2).

``record_verb`` — one row per call on any surface (mcp | rest |
background | cli), latency in ms, meta through the C5 allowlist gate
(``validate_meta``). Ten collection points on boundary surfaces
(docs/architecture.md §3); the assemble_context pipeline and the main
store are never touched.

``rollup_hourly`` — exact per-hour aggregation into
``verb_metrics_hourly``: count and p50/p95/p99/max quantiles computed
from the raw rows (interpolated, never averaged — means hide tails).
Each hour is fully recomputed (DELETE + INSERT), so re-runs are
idempotent. Two group axes per hour: per-(surface, verb, status,
project) rows for the private contour, plus a project-less GLOBAL row
per (surface, verb, status) that the public exposition reads (RL-S2:
project never crosses to Prometheus labels).

A2 wiring gates carried from the review (N4): every meta routes via
``validate_meta`` (counters keys identifier-capped and entry-count
capped, NaN/inf refused); rollup runs BEFORE retention so raw rows are
never deleted unrolled (raw TTL 30d ≫ hourly cadence).

A2 gate facts (measured on the live integration, not in CI): INSERT
p95 < 2 ms in the hooks path; volume/day; retention green.
"""

# ── PROVENANCE ────────────────────────────────────────────────────────
# Vendored from mnemos-vitals main (phase A2, 2026-09-22). Master copy
# + methodology: ~/LABs/Projects/Project-Mnemos/mnemos-vitals. Sync rule:
# changes land there first, then are ported in the same wave
# (drift-guard tests on both sides must stay green).

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from vesma.metrics.schema import validate_meta

logger = logging.getLogger("vesma.metrics.ledger")

#: Metered surfaces (born-final CHECK in the schema — mirrored here for
#: cheap refusal before the write).
SURFACES = ("mcp", "rest", "background", "cli")
VERB_STATUSES = ("ok", "error")

_VERB_LIMIT = 128
_ID_LIMIT = 128


def _quantile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated quantile of an already-sorted list."""
    if not sorted_vals:
        return 0.0
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


class VerbLedgerMixin:
    """Verb-ledger operations mixed into :class:`MetricsStore`.

    Kept in this module (the ledger is the module's contract) while the
    connection lifecycle stays in the sink — same-package composition,
    no runtime dependency from the sink on this file's specifics.
    """

    if TYPE_CHECKING:  # structural contract with the host sink class
        _conn: Callable[..., sqlite3.Connection | None]
        _fail: Callable[[str, Exception], None]
        _local: threading.local

    def record_verb(
        self,
        *,
        surface: str,
        verb: str,
        status: str,
        latency_ms: float,
        status_code: int | None = None,
        project: str | None = None,
        agent: str | None = None,
        meta: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> int | None:
        """Record one verb call. Non-fatal; returns the row id or None.

        C5 fail-closed: a meta that ``validate_meta`` refuses logs a
        warning and the WHOLE write is refused — never silently dropped,
        never raised into the host.
        """
        try:
            if surface not in SURFACES or status not in VERB_STATUSES:
                raise ValueError(f"surface/status not allowed: {surface!r}/{status!r}")
            if not isinstance(verb, str) or not verb or len(verb) > _VERB_LIMIT or "\n" in verb:
                raise ValueError("verb must be a short single-line string")
            latency = float(latency_ms)
            if not math.isfinite(latency) or latency < 0:
                raise ValueError(f"latency_ms must be finite >= 0, got {latency_ms!r}")
            clean_meta = validate_meta(meta)
            if clean_meta is None:
                raise ValueError("meta refused by the C5 allowlist")
            for name, val in (("project", project), ("agent", agent)):
                if val is not None and (
                    not isinstance(val, str) or len(val) > _ID_LIMIT or "\n" in val
                ):
                    raise ValueError(f"{name} must be a short single-line string")
            code = int(status_code) if status_code is not None else None

            conn = self._conn()
            if conn is None:
                return None
            cur = conn.execute(
                "INSERT INTO verb_metrics (ts, surface, verb, status, status_code,"
                " latency_ms, project, agent, meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ts if ts is not None else datetime.now(UTC).timestamp(),
                    surface,
                    verb,
                    status,
                    code,
                    latency,
                    project,
                    agent,
                    None if not clean_meta else json.dumps(clean_meta),
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        except (sqlite3.Error, ValueError, TypeError, AttributeError, OSError) as exc:
            self._fail("record_verb", exc)
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                with suppress(sqlite3.Error):
                    conn.rollback()
            return None

    def last_rolled_hour(self) -> int:
        """Latest hour present in verb_metrics_hourly (epoch hour; 0 if none).

        The tick uses this to catch up after downtime instead of silently
        skipping every missed hour (m3).
        """
        conn = self._conn()
        if conn is None:
            return 0
        row = conn.execute("SELECT MAX(hour) FROM verb_metrics_hourly").fetchone()
        return int(row[0] or 0)

    def rollup_hourly(self, *, hour: int | None = None) -> int:
        """Aggregate one hour of verb_metrics into verb_metrics_hourly.

        ``hour`` is a unix epoch hour (default: the previous COMPLETE
        hour, UTC). Idempotent — the hour is deleted and recomputed
        whole. Emits per-project rows AND a project-less global row per
        (surface, verb, status); the public exposition reads only the
        global rows (RL-S2). Fail-loud to the caller (job-level alert),
        with the failed writes rolled back.
        """
        conn = self._conn()
        if conn is None:
            return 0
        if hour is None:
            hour = int(datetime.now(UTC).timestamp() // 3600) - 1
        h0, h1 = hour * 3600, (hour + 1) * 3600
        try:
            rows = conn.execute(
                "SELECT surface, verb, status, project, latency_ms FROM verb_metrics"
                " WHERE ts >= ? AND ts < ?",
                (h0, h1),
            ).fetchall()
            groups: dict[tuple[str, ...], list[float]] = {}
            for r in rows:
                latency = float(r["latency_ms"])
                global_key: tuple[str, ...] = (r["surface"], r["verb"], r["status"])
                private_key: tuple[str, ...] = (*global_key, r["project"] or "")
                groups.setdefault(global_key, []).append(latency)
                groups.setdefault(private_key, []).append(latency)
            conn.execute("DELETE FROM verb_metrics_hourly WHERE hour = ?", (hour,))
            out: list[tuple[Any, ...]] = []
            for key, lats in sorted(groups.items()):
                lats.sort()
                project = key[3] if len(key) > 3 else None
                out.append(
                    (
                        hour,
                        key[0],
                        key[1],
                        key[2],
                        project,
                        len(lats),
                        _quantile(lats, 0.50),
                        _quantile(lats, 0.95),
                        _quantile(lats, 0.99),
                        lats[-1],
                    )
                )
            conn.executemany(
                "INSERT INTO verb_metrics_hourly (hour, surface, verb, status, project,"
                " count, p50_ms, p95_ms, p99_ms, max_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                out,
            )
            conn.commit()
            return len(out)
        except Exception:
            with suppress(sqlite3.Error):
                conn.rollback()
            raise


__all__ = ["SURFACES", "VERB_STATUSES", "VerbLedgerMixin"]
