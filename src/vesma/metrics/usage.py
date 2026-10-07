"""Usage loop analytics (phase C, zone 2 — relevance ladder, informational tier).

Reads the ``usage_reports`` plane written by ``MetricsStore.record_usage``
and computes three global aggregates (docs/methodology.md §3.2, §7;
docs/architecture.md §2):

  assemble_usage_rate — the loop-closure rate: the share of assemble
    calls that ever received a harness response;
  touched_share       — the mean share of injected blocks the model
    actually touched per reported call (join usage_reports →
    assemble_metrics → injection_blocks; touched ids are counted against
    the opaque positional ordinals ``"<metrics_id>:<i>"``);
  wrong_tool_rate     — the share of reported calls flagged as routed to
    the wrong tool.

Kappa gate (structural, frozen pre-registration
``docs/experiments/touched-rate-kappa.md`` in the master library repo,
2026-09-25):

  the touched signal enters the relevance CORRIDORS only after Cohen's
  kappa calibration (κ̂ >= 0.6 against a ~50-turn ablation, bootstrap
  CI95 lower bound >= 0.5). Until that single-look ablation run lands
  and its verdict is accepted, touched_share is INFORMATIONAL ONLY (F8
  traffic light renders it NO-DATA for corridor purposes). This is
  encoded structurally: :func:`kappa_calibration_pending` returns True
  until an explicit code change flips it, and every ``touched_share()``
  output carries ``corridor_eligible: False`` plus the pending reason
  while it does. The flag can never flip from data accumulating in the
  sidecar — the born-final schema has no calibration table by design;
  the verdict lives on the benchmarks side (ablation runner report),
  and recalibration in either direction requires a NEW pre-registration.

Privacy by structure: all three aggregates are global counts/rates —
no session, project or principal column is read or emitted anywhere.

Non-fatal by contract: any read error degrades to a loud NO-DATA dict
with ``reasons`` — never an exception, never a silent zero. NO-DATA is
not zero: with no reports the value keys carry ``None``.
"""

# ── PROVENANCE ────────────────────────────────────────────────────────
# Vendored from mnemos-vitals main (phase C, 2026-09-29; includes the
# C1-review fixups of master commit 698a650: degraded touched_share
# carries the kappa reason). Master copy + methodology:
# ~/LABs/Projects/Project-Mnemos/mnemos-vitals. Sync rule: changes land
# there first, then are ported in the same wave (drift-guard tests on
# both sides must stay green).

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, cast

logger = logging.getLogger("vesma.metrics.usage")

#: Frozen calibration thresholds (docs/experiments/touched-rate-kappa.md
#: — the master doc lives in the mnemos-vitals repo).
KAPPA_MIN = 0.6
KAPPA_CI_FLOOR = 0.5
KAPPA_PREREGISTRATION = "docs/experiments/touched-rate-kappa.md"

#: The reason string the F8 renderer must surface while calibration is
#: pending (the pre-registration's honesty clause: touched stays
#: informational, corridors wait for the ladder's next rung).
KAPPA_PENDING_REASON = (
    "touched_rate is informational only until kappa calibration lands"
    f" (kappa >= {KAPPA_MIN} vs ~50-turn ablation, CI95 lower >= {KAPPA_CI_FLOOR};"
    f" frozen pre-registration {KAPPA_PREREGISTRATION})"
)


def kappa_calibration_pending() -> bool:
    """True while the frozen kappa calibration has not landed.

    Flipping this to False is an explicit code change gated by the
    ablation runner's single-look verdict and — for any recalibration —
    a NEW pre-registration (the frozen one forbids post-run edits). It
    can never flip from telemetry: the sidecar carries no calibration
    state, so the library structurally cannot mistake data volume for
    validation. NOTE master 2026-09-29 (H-K0 verdict): the decisive
    ablation look landed honest NO-DATA — kappa was NOT computable on
    the committed S5 tape (no blocks >= 40 tokens) — so touched_rate is
    NOT a v1 corridor metric and this gate stays True under the frozen
    pre-registration; a flip requires a NEW pre-registration.
    """
    return True


class UsageAnalyzer:
    """Global usage-loop aggregates computed from the sidecar only.

    Non-fatal: every public method returns a fully-keyed dict whose
    ``status`` is ``"OK"`` or ``"NO-DATA"`` (with ``reasons``); read
    failures degrade, never raise — the caller may sit on a server path.
    """

    def __init__(self, store: Any) -> None:
        # store: MetricsStore — typed Any to keep this module import-light;
        # reads go through the store's own connection plane (same-package).
        self._store = store

    # ── Loop closure ──────────────────────────────────────────────────────

    def assemble_usage_rate(self) -> dict[str, Any]:
        """Share of assemble calls with >= 1 usage report. Never raises."""
        try:
            return self._assemble_usage_rate()
        except (sqlite3.Error, RuntimeError, ValueError, TypeError, KeyError) as exc:
            logger.warning("vitals: assemble_usage_rate degraded to NO-DATA: %s", exc)
            return self._degraded(
                {
                    "assemble_usage_rate": None,
                    "assemble_calls": 0,
                    "closed_calls": 0,
                },
                exc,
            )

    def _assemble_usage_rate(self) -> dict[str, Any]:
        conn = self._conn()
        total = conn.execute("SELECT COUNT(*) FROM assemble_metrics").fetchone()[0]
        out: dict[str, Any] = {
            "assemble_usage_rate": None,
            "assemble_calls": total,
            "closed_calls": 0,
            "status": "NO-DATA",
            "reasons": [],
        }
        if total == 0:
            out["reasons"] = ["no assemble calls recorded"]
            return out
        closed = conn.execute("SELECT COUNT(DISTINCT metrics_id) FROM usage_reports").fetchone()[0]
        out["closed_calls"] = closed
        out["assemble_usage_rate"] = closed / total
        out["status"] = "OK"
        return out

    # ── Touched share (informational until kappa lands) ───────────────────

    def touched_share(self) -> dict[str, Any]:
        """Mean share of injected blocks touched per reported call. Never raises.

        Per report: ``|touched ∩ injected| / |injected|`` where injected
        blocks are the assemble row's ``injection_blocks`` ordinals
        (``"<metrics_id>:<i>"``, the writer's born invariant). Reports
        whose parent assembly injected nothing, or whose stored JSON is
        unreadable garbage, are SKIPPED and counted — a garbage shape is
        not a zero-touch call (NO-DATA is not zero, §1).

        ``corridor_eligible`` is False while :func:`kappa_calibration_pending`
        — the structural kappa gate; the value itself stays informational.
        """
        try:
            return self._touched_share()
        except (sqlite3.Error, RuntimeError, ValueError, TypeError, KeyError) as exc:
            logger.warning("vitals: touched_share degraded to NO-DATA: %s", exc)
            return self._degraded(
                {
                    "touched_share": None,
                    "reports": 0,
                    "calls_measured": 0,
                    "skipped_no_blocks": 0,
                    "skipped_unreadable": 0,
                    "corridor_eligible": False,
                    "kappa_calibration_pending": kappa_calibration_pending(),
                },
                exc,
            )

    def _touched_share(self) -> dict[str, Any]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT u.metrics_id AS metrics_id, u.block_ids_touched_json AS touched_json"
            " FROM usage_reports u ORDER BY u.id"
        ).fetchall()
        out: dict[str, Any] = {
            "touched_share": None,
            "reports": len(rows),
            "calls_measured": 0,
            "skipped_no_blocks": 0,
            "skipped_unreadable": 0,
            "corridor_eligible": not kappa_calibration_pending(),
            "kappa_calibration_pending": kappa_calibration_pending(),
            "status": "OK",
            "reasons": [],
        }
        if not rows:
            out["status"] = "NO-DATA"
            out["reasons"] = ["no usage reports recorded"]
            out["corridor_eligible"] = False
            return out
        if kappa_calibration_pending():
            out["reasons"].append(KAPPA_PENDING_REASON)

        shares: list[float] = []
        for r in rows:
            metrics_id = int(r["metrics_id"])
            injected_n = conn.execute(
                "SELECT COUNT(*) FROM injection_blocks WHERE metrics_id = ?",
                (metrics_id,),
            ).fetchone()[0]
            if injected_n == 0:
                # Nothing was injected: the touched share is 0/0 undefined —
                # excluded from the mean, never counted as 0 or 1.
                out["skipped_no_blocks"] += 1
                continue
            try:
                touched = json.loads(r["touched_json"])
            except (ValueError, TypeError):
                touched = None
            if not isinstance(touched, list):
                out["skipped_unreadable"] += 1
                continue
            injected = {f"{metrics_id}:{i}" for i in range(injected_n)}
            hits = len({t for t in touched if isinstance(t, str)} & injected)
            shares.append(hits / injected_n)
        measured = len(shares)
        out["calls_measured"] = measured
        if measured:
            out["touched_share"] = sum(shares) / measured
        elif out["reports"]:
            # Reports exist but none was measurable — loud NO-DATA, not 0.0.
            out["status"] = "NO-DATA"
            out["reasons"].append(
                "no measurable reports (empty assemblies or unreadable touched ids)"
            )
        return out

    # ── Wrong-tool flag ───────────────────────────────────────────────────

    def wrong_tool_rate(self) -> dict[str, Any]:
        """Share of usage reports flagged wrong_tool_flag=True. Never raises."""
        try:
            return self._wrong_tool_rate()
        except (sqlite3.Error, RuntimeError, ValueError, TypeError, KeyError) as exc:
            logger.warning("vitals: wrong_tool_rate degraded to NO-DATA: %s", exc)
            return self._degraded(
                {
                    "wrong_tool_rate": None,
                    "reports": 0,
                    "wrong_tool_calls": 0,
                },
                exc,
            )

    def _wrong_tool_rate(self) -> dict[str, Any]:
        conn = self._conn()
        total = conn.execute("SELECT COUNT(*) FROM usage_reports").fetchone()[0]
        out: dict[str, Any] = {
            "wrong_tool_rate": None,
            "reports": total,
            "wrong_tool_calls": 0,
            "status": "NO-DATA",
            "reasons": [],
        }
        if total == 0:
            out["reasons"] = ["no usage reports recorded"]
            return out
        flagged = conn.execute(
            "SELECT COUNT(*) FROM usage_reports WHERE wrong_tool_flag = 1"
        ).fetchone()[0]
        out["wrong_tool_calls"] = flagged
        out["wrong_tool_rate"] = flagged / total
        out["status"] = "OK"
        return out

    # ── Shared plumbing ───────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        # store is typed Any (import-light contract) — the read plane rides
        # the sink's own per-thread connection; cast keeps mypy strict quiet
        # without loosening the caller-side contract.
        conn = cast("sqlite3.Connection | None", self._store._conn())
        if conn is None:
            raise RuntimeError("sidecar unavailable")
        return conn

    def _degraded(self, keys: dict[str, Any], exc: Exception) -> dict[str, Any]:
        """A fully-keyed NO-DATA dict for a degraded read (key-set contract)."""
        out: dict[str, Any] = dict(keys)
        out["status"] = "NO-DATA"
        out["reasons"] = [
            f"analyzer degraded: {type(exc).__name__}",
            # the frozen-prereg reason survives degradation — it is the exact
            # line the operator reads while the gate is soft-locked
            *([KAPPA_PENDING_REASON] if "touched_share" in keys else []),
        ]
        return out


__all__ = [
    "KAPPA_CI_FLOOR",
    "KAPPA_MIN",
    "KAPPA_PENDING_REASON",
    "KAPPA_PREREGISTRATION",
    "UsageAnalyzer",
    "kappa_calibration_pending",
]
