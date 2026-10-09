"""``vesma metrics`` — the money-baseline operator surface (nhi-15).

Two commands over the metrics plane, both read-first:

* ``vesma metrics baseline`` — the money baseline over a window
  (``--days N``, default 7): medians/p90 tokens per turn and per
  session, USD per day priced through the versioned tariff table
  (:mod:`vesma.metrics.tariffs`), the session-type breakdown and the
  cached-input share. The default source is the sidecar's own
  ``turn_usage`` table (NO-DATA until a harness write path lands —
  never a zero). ``--from-harness PATH`` instead attaches a harness
  ledger (ZCode ``db.sqlite``) STRICTLY READ-ONLY — the baseline must
  not mutate any store it reads. Every run writes a JSON snapshot
  under ``<data_dir>/baselines/`` (``--no-snapshot`` opts out); unknown
  models are FLAGGED in the table, never priced. ``--by-harness`` adds
  the cross-harness cut (USD/day + per-turn medians per ``harness_id``;
  the JSON snapshot carries it always).

* ``vesma metrics session-type`` — the operator's manual classification
  axis: ``vesma metrics session-type <session> task|chat|background``
  writes a row into the born-final ``session_labels`` table (the
  default is ``unclassified``; ``unclassified`` given explicitly clears
  the label); ``--list`` prints the current labels. The baseline groups
  unlabeled sessions under ``unclassified``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from vesma.cli._manager import get_manager
from vesma.metrics.baseline import (
    BaselineReport,
    BaselineSourceError,
    build_report,
    read_harness_session_labels,
    read_harness_turns,
    read_sidecar_turns,
)
from vesma.metrics.schema import (
    SESSION_LABELS,
    SESSION_TYPE_DEFAULT,
    SESSION_TYPES,
    sidecar_path,
)
from vesma.metrics.tariffs import load_tariffs

console = Console()

ConfigOption = typer.Option(None, "--config", "-c", help="Path to config.yaml")

_metrics_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="metrics",
    help=(
        "Money metrics over the turn-usage plane (nhi-15).\n\n"
        "`baseline` — medians/p90 tokens per turn/session, USD/day by the "
        "versioned tariff table, session-type breakdown, cached share; "
        "writes a JSON snapshot. `session-type` — manual task/chat/background "
        "labels for sessions. Read-only over every store it reads."
    ),
    no_args_is_help=True,
)


def _fmt(value: float | None, unit: str = "", digits: int = 0) -> str:
    """NO-DATA is loud: ``None`` renders as ``n/a``, never as ``0``."""
    if value is None:
        return "[yellow]n/a[/yellow]"
    return f"{value:,.{digits}f}{unit}"


def _print_report(report: BaselineReport, *, by_harness: bool = False) -> None:
    summary = Table(box=None, show_header=False, pad_edge=False)
    summary.add_column(style="bold", no_wrap=True)
    summary.add_column()
    summary.add_row("source", report.source)
    summary.add_row(
        "window",
        f"{report.window_days}d"
        f" ({datetime.fromtimestamp(report.since_ts, tz=UTC):%Y-%m-%d %H:%M} UTC"
        f" .. {datetime.fromtimestamp(report.until_ts, tz=UTC):%Y-%m-%d %H:%M} UTC)",
    )
    summary.add_row("turns / sessions", f"{report.turn_count:,} / {report.session_count:,}")
    cached_pct = report.cached_share * 100.0 if report.cached_share is not None else None
    summary.add_row(
        "tokens in (cached share)",
        f"{report.total_input_tokens:,} ({_fmt(cached_pct, '%', 1)})",
    )
    summary.add_row("tokens out", f"{report.total_output_tokens:,}")
    summary.add_row(
        "per turn in p50/p90",
        f"{_fmt(report.per_turn_input[0])} / {_fmt(report.per_turn_input[1])}",
    )
    summary.add_row(
        "per turn out p50/p90",
        f"{_fmt(report.per_turn_output[0])} / {_fmt(report.per_turn_output[1])}",
    )
    summary.add_row(
        "per turn total p50/p90",
        f"{_fmt(report.per_turn_total[0])} / {_fmt(report.per_turn_total[1])}",
    )
    summary.add_row(
        "per session in p50/p90",
        f"{_fmt(report.per_session_input[0])} / {_fmt(report.per_session_input[1])}",
    )
    summary.add_row(
        "per session out p50/p90",
        f"{_fmt(report.per_session_output[0])} / {_fmt(report.per_session_output[1])}",
    )
    cost = f"${report.cost_usd:,.2f} / ${report.cost_usd_per_day:,.2f}"
    if report.unknown_model_tokens:
        cost += f"  [red]+{report.unknown_model_tokens:,} in-tokens UNKNOWN[/red]"
    summary.add_row("USD window / per day", cost)
    console.print(summary)

    if report.models:
        models = Table(title="by model", title_justify="left", box=None)
        models.add_column("model")
        models.add_column("turns", justify="right")
        models.add_column("in (fresh)", justify="right")
        models.add_column("in (cached)", justify="right")
        models.add_column("out", justify="right")
        models.add_column("USD", justify="right")
        for m in report.models:
            models.add_row(
                m.model,
                f"{m.turns:,}",
                f"{max(0, m.input_tokens - m.cached_read_tokens):,}",
                f"{m.cached_read_tokens:,}",
                f"{m.output_tokens:,}",
                f"{m.cost_usd:,.2f}",
            )
        console.print(models)

    if report.session_types:
        types = Table(title="by session_type", title_justify="left", box=None)
        types.add_column("session_type")
        types.add_column("turns", justify="right")
        types.add_column("sessions", justify="right")
        types.add_column("median in/turn", justify="right")
        types.add_column("USD", justify="right")
        for t in report.session_types:
            types.add_row(
                t.session_type,
                f"{t.turns:,}",
                f"{len(t.sessions):,}",
                _fmt(t.median_input_per_turn),
                f"{t.cost_usd:,.2f}",
            )
        console.print(types)

    # nhi-15 wave 1: the cross-harness cut (--by-harness). Rows exist
    # only once harnesses POST turn-usage signals — an empty cut prints
    # the honest "no write-path data yet", never a zero.
    if by_harness:
        if report.harnesses:
            harness_table = Table(title="by harness", title_justify="left", box=None)
            harness_table.add_column("harness_id")
            harness_table.add_column("turns", justify="right")
            harness_table.add_column("sessions", justify="right")
            harness_table.add_column("median in/turn", justify="right")
            harness_table.add_column("median out/turn", justify="right")
            harness_table.add_column("USD", justify="right")
            harness_table.add_column("USD/day", justify="right")
            for h in report.harnesses:
                harness_table.add_row(
                    h.harness,
                    f"{h.turns:,}",
                    f"{len(h.sessions):,}",
                    _fmt(h.median_input_per_turn),
                    _fmt(h.median_output_per_turn),
                    f"${h.cost_usd:,.2f}",
                    f"${h.cost_usd_per_day:,.2f}",
                )
            console.print(harness_table)
        else:
            console.print(
                "[yellow]note[/yellow] by harness: no harness-attributed turns yet —"
                " the cut fills in as harnesses POST /signals/turn-usage"
            )

    for note in report.notes:
        console.print(f"[yellow]note[/yellow] {note}")


def _snapshot_path(data_dir: Path, until_ts: float) -> Path:
    stamp = datetime.fromtimestamp(until_ts, tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = data_dir / "baselines"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"baseline-{stamp}.json"


def _sidecar_session_labels(sidecar: Path) -> dict[str, str]:
    """Read operator labels from the sidecar (missing table = all default)."""
    if not sidecar.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{sidecar}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            f"SELECT session, session_type FROM {SESSION_LABELS.name}"  # nosec B608 - static table name
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {str(r[0]): str(r[1]) for r in rows}


@_metrics_app.command(name="baseline")
def metrics_baseline(
    days: Annotated[
        int,
        typer.Option(
            "--days", "-d", min=1, max=400, help="Window length in days, ending now (UTC)."
        ),
    ] = 7,
    from_harness: Annotated[
        Path | None,
        typer.Option(
            "--from-harness",
            help="Read turns from a harness ledger (ZCode db.sqlite) — STRICTLY READ-ONLY.",
        ),
    ] = None,
    no_snapshot: Annotated[
        bool,
        typer.Option(
            "--no-snapshot", help="Do not write the JSON snapshot under <data_dir>/baselines/."
        ),
    ] = False,
    by_harness: Annotated[
        bool,
        typer.Option(
            "--by-harness",
            help="Print the cross-harness cut: USD/day and per-turn medians per harness_id.",
        ),
    ] = False,
    config: str = ConfigOption,
) -> None:
    """Money baseline over the last --days: medians/p90, USD/day, cached share.

    Source: the sidecar's own ``turn_usage`` plane, or an explicit
    read-only harness ledger via ``--from-harness``. An empty plane is a
    loud NO-DATA, never a zero. Unknown models are flagged, not priced.
    ``--by-harness`` adds the harness_id cut (USD/day + medians) — the
    comparison the 2026-10-09 owner directive reads.
    """
    until_ts = time.time()
    since_ts = until_ts - days * 86_400.0
    tariffs = load_tariffs()
    mgr = get_manager(config)
    data_dir = mgr.settings.vesma.data_dir.expanduser()

    sidecar_missing = False
    try:
        if from_harness is not None:
            turns = read_harness_turns(from_harness, since_ts, until_ts)
            session_types = read_harness_session_labels(from_harness)
            source = f"harness:{from_harness}"
        else:
            sidecar = sidecar_path(data_dir)
            if sidecar.exists():
                turns = read_sidecar_turns(sidecar, since_ts, until_ts)
                session_types = _sidecar_session_labels(sidecar)
            else:
                # a fresh install has no sidecar yet — NO-DATA, not an error
                turns = []
                session_types = {}
                sidecar_missing = True
            source = f"sidecar:{sidecar}"
    except BaselineSourceError as exc:
        console.print(f"[red]✗[/red] baseline source unavailable: {exc}")
        raise typer.Exit(1) from exc

    notes = [f"tariffs as_of {tariffs.as_of} ({tariffs.currency}/1M tokens, list prices)"]
    if sidecar_missing:
        notes.append("metrics sidecar does not exist yet — baseline over an empty plane")
    report = build_report(
        source=source,
        turns=turns,
        session_types=session_types,
        tariffs=tariffs,
        window_days=days,
        since_ts=since_ts,
        until_ts=until_ts,
        notes=notes,
    )
    _print_report(report, by_harness=by_harness)

    if no_snapshot:
        return
    out = _snapshot_path(data_dir, until_ts)
    out.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    console.print(f"[green]✓[/green] snapshot: {out}")


@_metrics_app.command(name="session-type")
def metrics_session_type(
    session: Annotated[str | None, typer.Argument(help="Session id (e.g. sess_abc123).")] = None,
    session_type: Annotated[
        str | None, typer.Argument(help=f"One of: {', '.join(SESSION_TYPES)}.")
    ] = None,
    list_all: Annotated[
        bool, typer.Option("--list", help="List labeled sessions instead of setting a label.")
    ] = False,
    config: str = ConfigOption,
) -> None:
    """Label a session (task/chat/background) or list labels with --list.

    Writes the born-final ``session_labels`` table of the metrics
    sidecar. ``unclassified`` clears a label back to the default.
    """
    mgr = get_manager(config)
    sidecar = sidecar_path(mgr.settings.vesma.data_dir.expanduser())

    if list_all:
        rows = _read_labels(sidecar)
        if not rows:
            console.print(
                "no session labels yet — set one with"
                " `vesma metrics session-type <session> task|chat|background`"
            )
            return
        table = Table(box=None)
        table.add_column("session")
        table.add_column("session_type")
        table.add_column("updated (UTC)")
        for row in rows:
            table.add_row(
                row[0], row[1], datetime.fromtimestamp(row[2], tz=UTC).isoformat(timespec="seconds")
            )
        console.print(table)
        return

    if session is None or session_type is None:
        console.print(
            "[red]✗[/red] usage: vesma metrics session-type <session>"
            f" <{'|'.join(SESSION_TYPES)}> (or --list)"
        )
        raise typer.Exit(2)
    if session_type not in SESSION_TYPES:
        console.print(
            f"[red]✗[/red] unknown session_type {session_type!r} —"
            f" allowed: {', '.join(SESSION_TYPES)}"
        )
        raise typer.Exit(2)
    if not sidecar.exists():
        console.print(f"[red]✗[/red] no metrics sidecar at {sidecar} — start the server once first")
        raise typer.Exit(1)
    try:
        conn = sqlite3.connect(sidecar)
        with conn:
            conn.execute(
                f"INSERT INTO {SESSION_LABELS.name} (session, session_type, updated_ts, updated_by)"  # nosec B608 - static table name
                " VALUES (?, ?, ?, 'cli')"
                " ON CONFLICT(session) DO UPDATE SET"
                " session_type = excluded.session_type,"
                " updated_ts = excluded.updated_ts, updated_by = 'cli'",
                (session, session_type, time.time()),
            )
    except sqlite3.Error as exc:
        console.print(f"[red]✗[/red] sidecar write failed: {exc}")
        raise typer.Exit(1) from exc
    shown = (
        session_type
        if session_type != SESSION_TYPE_DEFAULT
        else f"{SESSION_TYPE_DEFAULT} (label cleared)"
    )
    console.print(f"[green]✓[/green] {session} → {shown}")


def _read_labels(sidecar: Path) -> list[tuple[str, str, float]]:
    if not sidecar.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{sidecar}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        return [
            (str(r[0]), str(r[1]), float(r[2]))
            for r in conn.execute(
                f"SELECT session, session_type, updated_ts FROM {SESSION_LABELS.name}"  # nosec B608 - static table name
                " ORDER BY updated_ts DESC"
            ).fetchall()
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


__all__ = ["_metrics_app", "metrics_baseline", "metrics_session_type"]
