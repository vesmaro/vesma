"""Money baseline over the turn-usage plane (nhi-15, wave 0).

Turns are the billing unit of record: one user request → one model
answer. Two read-only sources are supported:

  ``sidecar`` — the native ``turn_usage`` table of the vesma metrics
  sidecar (born-final, additive: existing stores gain it on next open).
  The table is empty until a harness write path lands — a report over
  an empty plane is a loud NO-DATA, never a zero.

  ``harness`` — an explicit, READ-ONLY attach of a harness-side
  ledger. v1 knows the ZCode shape (``turn_usage`` in epoch-ms with
  ``cache_read_input_tokens`` inside ``input_tokens``, dominant model
  per turn resolved from ``model_usage``, session task kinds from
  ``session.task_type``). Nothing is ever written to a harness store.

Aggregates follow the vitals honesty rules: percentiles over real
distributions (medians and p90 — means hide tails), NO-DATA is
``None`` and never renders as ``0``, unknown models are flagged (the
tariff table refuses to invent numbers), and the cached share is
``sum(cached_read) / sum(input)`` — the money-visibility number the
owner directive of 2026-10-04 (token economy = metric #1) reads first.

Privacy by structure: only integer counters, ids and slugs are read;
no prompt or output text exists in either source table.
"""

# ── PROVENANCE ────────────────────────────────────────────────────────
# Vesma-wave addition (nhi-15 money baseline, 2026-10-08) — not part of
# the mnemos-vitals vendored surface.

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from vesma.metrics.tariffs import TariffTable, turn_cost_usd

logger = logging.getLogger("vesma.metrics.baseline")

SECONDS_PER_DAY = 86_400.0


class BaselineSourceError(RuntimeError):
    """A source store cannot be read — loud NO-DATA for the caller."""


@dataclass(frozen=True)
class TurnRow:
    """One billed turn, already normalized (ts in epoch seconds)."""

    session: str | None
    ts: float
    model: str | None
    status: str
    input_tokens: int
    cached_read_tokens: int
    output_tokens: int
    #: Which harness produced the turn (nhi-15 wave 1) — 'unknown' for
    #: rows from a sidecar predating the harness columns.
    harness: str = "unknown"


# ── Read-only sources ──────────────────────────────────────────────────────


def _open_readonly(path: Any) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise BaselineSourceError(f"cannot open store readonly: {path} ({exc})") from exc


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def read_sidecar_turns(path: Any, since_ts: float, until_ts: float) -> list[TurnRow]:
    """Native plane: vesma sidecar ``turn_usage`` (epoch seconds).

    A store without the table (an old sidecar never re-opened by the
    6.x connect script) is a loud NO-DATA source error, not an empty
    list — the caller must be able to tell "no data yet" from "wrong
    store".
    """
    conn = _open_readonly(path)
    try:
        if not _table_exists(conn, "turn_usage"):
            raise BaselineSourceError(
                f"no turn_usage table in sidecar {path} — sidecar predates the"
                " money plane (re-open the store once with vesma >= 6.1)"
            )
        # A sidecar opened by wave-0 code only carries the turn_usage
        # shape WITHOUT the harness columns — read it tolerantly and
        # label those rows 'unknown' instead of refusing the baseline.
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(turn_usage)").fetchall()}
        has_harness = "harness_id" in cols
        harness_col = ", harness_id" if has_harness else ""
        rows = conn.execute(
            f"SELECT session, ts, model, status, input_tokens,"  # nosec B608 - static table + column literals
            f" cached_read_tokens, output_tokens{harness_col}"
            " FROM turn_usage WHERE ts > ? AND ts <= ? ORDER BY ts",
            (since_ts, until_ts),
        ).fetchall()
    finally:
        conn.close()
    return [
        TurnRow(
            session=r[0],
            ts=float(r[1]),
            model=r[2],
            status=str(r[3]),
            input_tokens=int(r[4]),
            cached_read_tokens=int(r[5]),
            output_tokens=int(r[6]),
            harness=str(r[7]) if has_harness and r[7] is not None else "unknown",
        )
        for r in rows
    ]


#: ZCode ``session.task_type`` → baseline ``session_type`` axis.
#: ``interactive`` is the owner working a task; the rest delegate to the
#: background (subagent children, forks) or are side chats.
HARNESS_TASK_TYPE_MAP: dict[str, str] = {
    "interactive": "task",
    "selection_side_chat": "chat",
    "subagent_child": "background",
    "fork": "background",
}


def read_harness_turns(path: Any, since_ts: float, until_ts: float) -> list[TurnRow]:
    """Harness plane: ZCode ``db.sqlite`` (READ-ONLY attach, ms epoch).

    The dominant model of a turn is the model with the largest billed
    input across the turn's completed model requests (multi-model
    attribution stays with the harness's model-level ledger; the turn
    row carries the money-dominant one). Cache semantics verified on
    the live ledger: ``cache_read_input_tokens <= input_tokens`` in
    100% of turns (cached input is reported INSIDE the input total).
    """
    conn = _open_readonly(path)
    try:
        for table in ("turn_usage", "model_usage"):
            if not _table_exists(conn, table):
                raise BaselineSourceError(
                    f"no {table} table in harness store {path} — not a ZCode ledger?"
                )
        # Epoch-ms → epoch-seconds at the boundary.
        raw = conn.execute(
            "SELECT session_id, turn_id, started_at / 1000.0, status, input_tokens,"
            " cache_read_input_tokens, output_tokens FROM turn_usage"
            " WHERE started_at > ? AND started_at <= ? ORDER BY started_at",
            (since_ts * 1000.0, until_ts * 1000.0),
        ).fetchall()
        dominant: dict[tuple[str, str], tuple[str, int]] = {}
        if raw:
            per_model = conn.execute(
                "SELECT session_id, turn_id, model_id, SUM(input_tokens)"
                " FROM model_usage WHERE status = 'completed' GROUP BY 1, 2, 3"
            ).fetchall()
            for session_id, turn_id, model_id, total in per_model:
                if session_id is None or turn_id is None or model_id is None:
                    continue
                key = (str(session_id), str(turn_id))
                best = dominant.get(key)
                if best is None or (total or 0) > best[1]:
                    dominant[key] = (str(model_id), total or 0)
    finally:
        conn.close()
    return [
        TurnRow(
            session=r[0],
            ts=float(r[2]),
            model=dominant.get((str(r[0]), str(r[1])), (None, 0))[0],
            status=str(r[3]),
            input_tokens=int(r[4]),
            cached_read_tokens=int(r[5]),
            output_tokens=int(r[6]),
            harness="zcode",  # v1 knows only the ZCode ledger shape
        )
        for r in raw
    ]


def read_harness_session_labels(path: Any) -> dict[str, str]:
    """Harness session task kinds mapped onto the baseline axis.

    Unknown/NULL kinds map to ``unclassified`` (the born-final default).
    """
    conn = _open_readonly(path)
    try:
        rows = conn.execute("SELECT id, task_type FROM session").fetchall()
    except sqlite3.Error as exc:
        raise BaselineSourceError(f"cannot read session kinds: {exc}") from exc
    finally:
        conn.close()
    labels: dict[str, str] = {}
    for session_id, task_type in rows:
        if session_id is None:
            continue
        labels[str(session_id)] = HARNESS_TASK_TYPE_MAP.get(
            str(task_type) if task_type is not None else "", "unclassified"
        )
    return labels


# ── Aggregation ────────────────────────────────────────────────────────────


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolation percentile (numpy default), ``q`` in ``[0, 1]``.

    ``None`` on empty input — NO-DATA is not a zero.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return float(ordered[low] * (1.0 - frac) + ordered[high] * frac)


@dataclass
class ModelSlice:
    """Token + money totals for one model over the window."""

    model: str
    turns: int = 0
    input_tokens: int = 0
    cached_read_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def known(self) -> bool:
        return self.cost_usd > 0.0 or self.input_tokens == 0


@dataclass
class TypeSlice:
    """One ``session_type`` bucket over the window."""

    session_type: str
    turns: int = 0
    sessions: set[str] = field(default_factory=set)
    input_tokens: int = 0
    cost_usd: float = 0.0
    median_input_per_turn: float | None = None


@dataclass
class HarnessSlice:
    """One ``harness_id`` bucket over the window (nhi-15 wave 1).

    The cross-harness comparison axis of the owner directive: which
    harness burns what — $ over the window, $/day, and the per-turn
    medians, side by side.
    """

    harness: str
    turns: int = 0
    sessions: set[str] = field(default_factory=set)
    input_tokens: int = 0
    cached_read_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cost_usd_per_day: float = 0.0
    unknown_model_tokens: int = 0
    median_input_per_turn: float | None = None
    median_output_per_turn: float | None = None
    daily_costs: dict[str, float] = field(default_factory=dict)


@dataclass
class BaselineReport:
    """Everything the stdout table and the JSON snapshot render."""

    source: str
    since_ts: float
    until_ts: float
    window_days: int
    turn_count: int = 0
    session_count: int = 0
    total_input_tokens: int = 0
    total_cached_read_tokens: int = 0
    total_output_tokens: int = 0
    cached_share: float | None = None
    per_turn_input: tuple[float | None, float | None] = (None, None)
    per_turn_output: tuple[float | None, float | None] = (None, None)
    per_turn_total: tuple[float | None, float | None] = (None, None)
    per_session_input: tuple[float | None, float | None] = (None, None)
    per_session_output: tuple[float | None, float | None] = (None, None)
    cost_usd: float = 0.0
    cost_usd_per_day: float = 0.0
    unknown_model_tokens: int = 0
    models: list[ModelSlice] = field(default_factory=list)
    session_types: list[TypeSlice] = field(default_factory=list)
    harnesses: list[HarnessSlice] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-snapshot shape (round-trippable, no live objects)."""
        return {
            "schema_version": 1,
            "source": self.source,
            "window": {
                "days": self.window_days,
                "since_ts": self.since_ts,
                "until_ts": self.until_ts,
            },
            "turns": self.turn_count,
            "sessions": self.session_count,
            "tokens": {
                "input": self.total_input_tokens,
                "cached_read": self.total_cached_read_tokens,
                "output": self.total_output_tokens,
                "cached_share": self.cached_share,
            },
            "per_turn": {
                "input": {"p50": self.per_turn_input[0], "p90": self.per_turn_input[1]},
                "output": {"p50": self.per_turn_output[0], "p90": self.per_turn_output[1]},
                "total": {"p50": self.per_turn_total[0], "p90": self.per_turn_total[1]},
            },
            "per_session": {
                "input": {"p50": self.per_session_input[0], "p90": self.per_session_input[1]},
                "output": {"p50": self.per_session_output[0], "p90": self.per_session_output[1]},
            },
            "cost_usd": {
                "window": self.cost_usd,
                "per_day": self.cost_usd_per_day,
                "unknown_model_tokens": self.unknown_model_tokens,
            },
            "models": [
                {
                    "model": m.model,
                    "turns": m.turns,
                    "input_tokens": m.input_tokens,
                    "cached_read_tokens": m.cached_read_tokens,
                    "output_tokens": m.output_tokens,
                    "cost_usd": m.cost_usd,
                }
                for m in self.models
            ],
            "session_types": [
                {
                    "session_type": t.session_type,
                    "turns": t.turns,
                    "sessions": len(t.sessions),
                    "input_tokens": t.input_tokens,
                    "cost_usd": t.cost_usd,
                    "median_input_per_turn": t.median_input_per_turn,
                }
                for t in self.session_types
            ],
            "by_harness": [
                {
                    "harness_id": h.harness,
                    "turns": h.turns,
                    "sessions": len(h.sessions),
                    "input_tokens": h.input_tokens,
                    "cached_read_tokens": h.cached_read_tokens,
                    "output_tokens": h.output_tokens,
                    "cost_usd": h.cost_usd,
                    "cost_usd_per_day": h.cost_usd_per_day,
                    "unknown_model_tokens": h.unknown_model_tokens,
                    "median_input_per_turn": h.median_input_per_turn,
                    "median_output_per_turn": h.median_output_per_turn,
                    "daily_costs": dict(sorted(h.daily_costs.items())),
                }
                for h in self.harnesses
            ],
            "notes": list(self.notes),
        }


def _pair(
    values: Sequence[float], q50: float = 0.5, q90: float = 0.9
) -> tuple[float | None, float | None]:
    return percentile(values, q50), percentile(values, q90)


def build_report(
    *,
    source: str,
    turns: Sequence[TurnRow],
    session_types: dict[str, str],
    tariffs: TariffTable,
    window_days: int,
    since_ts: float,
    until_ts: float,
    notes: Iterable[str] = (),
) -> BaselineReport:
    """Aggregate turns into the money baseline. Pure — no IO."""
    report = BaselineReport(
        source=source,
        since_ts=since_ts,
        until_ts=until_ts,
        window_days=window_days,
        notes=list(notes),
    )
    if not turns:
        report.notes.append("NO-DATA: zero turns in the window (not a zero baseline)")
        return report

    report.turn_count = len(turns)
    sessions: set[str] = set()
    turn_inputs: list[float] = []
    turn_outputs: list[float] = []
    turn_totals: list[float] = []
    session_inputs: dict[str, list[float]] = {}
    session_outputs: dict[str, list[float]] = {}
    models: dict[str, ModelSlice] = {}
    type_slices: dict[str, TypeSlice] = {}
    type_inputs: dict[str, list[float]] = {}
    harness_slices: dict[str, HarnessSlice] = {}
    harness_inputs: dict[str, list[float]] = {}
    harness_outputs: dict[str, list[float]] = {}

    for turn in turns:
        session = turn.session or "<anonymous>"
        sessions.add(session)
        report.total_input_tokens += turn.input_tokens
        report.total_cached_read_tokens += turn.cached_read_tokens
        report.total_output_tokens += turn.output_tokens
        turn_inputs.append(float(turn.input_tokens))
        turn_outputs.append(float(turn.output_tokens))
        turn_totals.append(float(turn.input_tokens + turn.output_tokens))
        session_inputs.setdefault(session, []).append(float(turn.input_tokens))
        session_outputs.setdefault(session, []).append(float(turn.output_tokens))

        model_key = turn.model or "<unknown>"
        slice_ = models.setdefault(model_key, ModelSlice(model=model_key))
        slice_.turns += 1
        slice_.input_tokens += turn.input_tokens
        slice_.cached_read_tokens += turn.cached_read_tokens
        slice_.output_tokens += turn.output_tokens

        stype = session_types.get(session, "unclassified")
        tslice = type_slices.setdefault(stype, TypeSlice(session_type=stype))
        tslice.turns += 1
        tslice.sessions.add(session)
        tslice.input_tokens += turn.input_tokens
        type_inputs.setdefault(stype, []).append(float(turn.input_tokens))

        hslice = harness_slices.setdefault(turn.harness, HarnessSlice(harness=turn.harness))
        hslice.turns += 1
        hslice.sessions.add(session)
        hslice.input_tokens += turn.input_tokens
        hslice.cached_read_tokens += turn.cached_read_tokens
        hslice.output_tokens += turn.output_tokens
        harness_inputs.setdefault(turn.harness, []).append(float(turn.input_tokens))
        harness_outputs.setdefault(turn.harness, []).append(float(turn.output_tokens))

        rate = tariffs.rate(turn.model)
        if rate is None:
            report.unknown_model_tokens += turn.input_tokens
            hslice.unknown_model_tokens += turn.input_tokens
        else:
            cost = turn_cost_usd(
                rate,
                input_tokens=turn.input_tokens,
                cached_read_tokens=turn.cached_read_tokens,
                output_tokens=turn.output_tokens,
            )
            day = _utc_date(turn.ts)
            slice_.cost_usd += cost
            tslice.cost_usd += cost
            hslice.cost_usd += cost
            hslice.daily_costs[day] = hslice.daily_costs.get(day, 0.0) + cost
            report.cost_usd += cost

    report.session_count = len(sessions)
    report.cached_share = (
        report.total_cached_read_tokens / report.total_input_tokens
        if report.total_input_tokens > 0
        else None
    )
    report.per_turn_input = _pair(turn_inputs)
    report.per_turn_output = _pair(turn_outputs)
    report.per_turn_total = _pair(turn_totals)
    report.per_session_input = _pair([sum(v) for v in session_inputs.values()])
    report.per_session_output = _pair([sum(v) for v in session_outputs.values()])
    report.models = sorted(models.values(), key=lambda m: m.input_tokens, reverse=True)
    for stype, values in type_inputs.items():
        type_slices[stype].median_input_per_turn = percentile(values, 0.5)
    report.session_types = sorted(type_slices.values(), key=lambda t: t.input_tokens, reverse=True)
    days = max(window_days, 1)
    for harness, slice_h in harness_slices.items():
        slice_h.median_input_per_turn = percentile(harness_inputs[harness], 0.5)
        slice_h.median_output_per_turn = percentile(harness_outputs[harness], 0.5)
        slice_h.cost_usd_per_day = slice_h.cost_usd / days
    report.harnesses = sorted(harness_slices.values(), key=lambda h: h.cost_usd, reverse=True)
    report.cost_usd_per_day = report.cost_usd / days
    return report


def iter_baseline_daily_costs(
    turns: Sequence[TurnRow], tariffs: TariffTable
) -> Iterator[tuple[str, float]]:
    """Yield ``(date, cost_usd)`` per UTC day — the $/day series."""
    by_day: dict[str, float] = {}
    for turn in turns:
        rate = tariffs.rate(turn.model)
        if rate is None:
            continue
        day = _utc_date(turn.ts)
        by_day[day] = by_day.get(day, 0.0) + turn_cost_usd(
            rate,
            input_tokens=turn.input_tokens,
            cached_read_tokens=turn.cached_read_tokens,
            output_tokens=turn.output_tokens,
        )
    yield from sorted(by_day.items())


def _utc_date(ts: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def session_cost_usd(turns: Iterable[TurnRow], tariffs: TariffTable) -> float:
    """Total window cost (shared by CLI sanity paths and tests)."""
    total = 0.0
    for turn in turns:
        rate = tariffs.rate(turn.model)
        if rate is None:
            continue
        total += turn_cost_usd(
            rate,
            input_tokens=turn.input_tokens,
            cached_read_tokens=turn.cached_read_tokens,
            output_tokens=turn.output_tokens,
        )
    return total


__all__ = [
    "HARNESS_TASK_TYPE_MAP",
    "BaselineReport",
    "BaselineSourceError",
    "HarnessSlice",
    "ModelSlice",
    "TurnRow",
    "TypeSlice",
    "build_report",
    "iter_baseline_daily_costs",
    "percentile",
    "read_harness_session_labels",
    "read_harness_turns",
    "read_sidecar_turns",
    "session_cost_usd",
]
