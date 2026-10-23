"""``vesma logs`` — view pipeline traces (M6 explainability layer).

CLI topology v2 (ArchCom 2026-10-23, wave A): the trace viewer takes the
top-level ``vesma logs`` name (its previous ``task-logs`` spelling stays
as a hidden deprecated alias with a stderr hint). A positional argument
that names a KNOWN component answers the wave-61 habit — ``vesma logs
NAME`` used to mean component logs — with a loud redirect to ``vesma
service logs NAME`` on stderr while STILL running the trace view (no
silent substitution); a non-component positional stays a usage error
exactly as it was under ``task-logs``.

Reads the ``traces`` table via :meth:`SQLiteStore.list_traces` and prints
a compact, ``tail -f``-style table. Supports filtering by task label,
project, and a time boundary.

Usage::

    vesma logs                            # last 50 traces
    vesma logs --task cluster             # only cluster traces
    vesma logs --project vesma            # filter by project
    vesma logs --limit 100                # more rows
    vesma logs --follow                   # poll for new traces (tail -f)
"""

from __future__ import annotations

import functools
import time
from datetime import datetime
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from vesma.cli._manager import load_settings_or_exit
from vesma.storage.sqlite_store import SQLiteStore

# Use a wide console so table columns are not truncated in non-interactive
# contexts (CliRunner, piped output). 200 chars fits all columns comfortably.
console = Console(width=200)

logs_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="logs",
    help=(
        "View pipeline traces.\n\n"
        "Reads the trace table the knowledge pipeline writes per task: what "
        "ran, on which memories, with what outcome. Known task labels: "
        "cluster, synthesize, publish, recall, plus the tag-bulk verbs "
        "(tags_add / tags_remove / tags_rename). Filter by `--project`, "
        "cap with `--limit`, cut off with `--since`, or keep watching with "
        "`--follow`. Component (supervisor) logs live on `vesma service"
        " logs NAME`."
    ),
    no_args_is_help=False,
)


def _format_ts(ts: datetime) -> str:
    """Format a trace timestamp as ``YYYY-MM-DD HH:MM:SS``."""
    if ts.tzinfo is not None:
        # Convert to UTC for consistent display, then strip tz for formatting.
        ts = ts.astimezone().replace(tzinfo=None)
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _render_table(traces: list[Any], *, title: str = "Traces") -> Table:
    table = Table(title=title, show_lines=False)
    table.add_column("Timestamp", style="dim")
    table.add_column("Task", style="cyan")
    table.add_column("Project")
    table.add_column("Step")
    table.add_column("Item", overflow="fold")
    table.add_column("Latency", justify="right")
    table.add_column("LLM", justify="center")
    table.add_column("Cache", justify="center")
    table.add_column("Fallback", justify="center")

    for t in traces:
        table.add_row(
            _format_ts(t.created_at),
            t.task_label,
            t.project or "-",
            t.step,
            (t.item_id or "-")[:12],
            f"{t.latency_ms}ms",
            "yes" if t.llm_called else "-",
            "hit" if t.cache_hit else "-",
            "yes" if t.fallback_used else "-",
        )
    return table


@logs_app.callback(invoke_without_command=True)
def logs_cmd(
    name: Annotated[
        str | None,
        typer.Argument(
            help="Known component name — answers the old `vesma logs NAME` habit "
            "with a redirect to `vesma service logs NAME` (traces still print).",
            show_default=False,
        ),
    ] = None,
    task: Annotated[
        str | None,
        typer.Option(
            "--task",
            "-t",
            help="Filter by trace task label — free string; known: cluster, "
            "synthesize, publish, recall, tags_add, tags_remove, tags_rename.",
        ),
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Filter by project slug")
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", "-l", help="Maximum number of traces to show")
    ] = 50,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="Only traces after this ISO date (e.g. 2026-06-01)",
        ),
    ] = None,
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Poll for new traces (tail -f style)")
    ] = False,
    config: Annotated[
        str | None,
        typer.Option("--config", "-c", help="Path to config.yaml"),
    ] = None,
) -> None:
    """Show recent pipeline traces.

    Prints the newest traces (default 50) as a table — timestamp, stage,
    project and summary — newest first. Combine `--since`, `--project` and
    `--limit` to narrow the window; `--follow` keeps polling for new traces
    tail -f style until interrupted.

    A positional NAME that matches a known component does NOT filter
    traces — components are not trace subjects — it prints a loud stderr
    redirect to `vesma service logs NAME` (the component-logs home since
    the topology v2 wave) and still runs the trace view; a non-component
    positional is a usage error (exit 2), as it always was.
    """
    if name is not None:
        # CLI topology v2 wave A: `vesma logs NAME` was the component-logs
        # spelling for a wave; catch the habit loudly, never silently
        # substitute a different view. The registry listing is disk-state
        # (never raises); the deferred import breaks the import cycle
        # (components imports this module for its own legacy surfaces).
        from vesma.cli.components import _manifest_stems

        if name in _manifest_stems():
            typer.echo(
                f"[deprecated] component logs moved — use: `vesma service logs {name}` "
                "(this view shows pipeline traces)",
                err=True,
            )
        else:
            # A positional was never part of the trace-viewer surface —
            # answer as the parser would have: usage error, exit 2 (the
            # codebase idiom — a raised click.UsageError would traceback
            # under typer's non-standalone invocation).
            console.print(
                f"[red]error:[/red] got unexpected extra argument ({name}) — this view "
                "shows PIPELINE traces (no positional arguments); component logs: "
                "`vesma service logs NAME`",
            )
            raise typer.Exit(code=2) from None
    settings = load_settings_or_exit(config)
    settings.resolve_paths()
    store = SQLiteStore(settings.db_path)

    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            console.print(f"[red]Invalid --since date: {since}[/red]")
            raise typer.Exit(1) from None

    def _fetch() -> list[Any]:
        rows = store.list_traces(project=project, task_label=task, limit=limit)
        if since_dt is not None:
            rows = [r for r in rows if r.created_at >= since_dt]
        return rows

    if not follow:
        traces = _fetch()
        if not traces:
            console.print("[yellow]No traces found.[/yellow]")
        else:
            console.print(_render_table(traces))
        store.close()
        return

    # ── Follow mode: poll every 2s, print only new rows ─────────────────
    seen_ids: set[str] = set()
    # Seed with the current tail so we don't reprint the whole history.
    for t in _fetch():
        seen_ids.add(t.id)
    console.print("[dim]Following traces (Ctrl+C to stop)...[/dim]")
    try:
        while True:
            time.sleep(2)
            rows = _fetch()
            new = [r for r in rows if r.id not in seen_ids]
            for r in new:
                seen_ids.add(r.id)
            if new:
                console.print(_render_table(new, title="New traces"))
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
    finally:
        store.close()


@functools.wraps(logs_cmd)
def task_logs_alias(*args: Any, **kwargs: Any) -> None:
    """Hidden top-level registration wrapper — prints the deprecation hint."""
    typer.echo("[deprecated] `vesma task-logs` is deprecated — use: vesma logs", err=True)
    logs_cmd(*args, **kwargs)
