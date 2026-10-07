"""``vesma scanner`` CLI — manual trigger + status for the background scanner.

Thin Typer wrapper over :class:`vesma.scanner.BackgroundScanner`. Two
subcommands:

* ``vesma scanner run [--full]`` — run one scan pass synchronously and
  print the :class:`~vesma.scanner.ScanResult` summary. ``--full``
  forces a non-incremental scan (every record in the corpus). Default
  is incremental (only records modified since the last successful scan).
* ``vesma scanner status`` — print the scanner's current state: last
  scan timestamp, cumulative records tagged, configured interval,
  enabled/disabled, background thread running/stopped.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console

from vesma.cli._manager import get_manager
from vesma.scanner_runtime import get_scanner

console = Console()

scanner_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="scanner",
    help=(
        "Background secrets scanner (Layer 2 defence-in-depth) — manual trigger + status.\n\n"
        "Detects credential-shaped content (API keys, tokens) in stored "
        "memories and flags those records. The scanner normally runs on its "
        "own schedule; `run` triggers a pass now, `status` reports liveness "
        "and the last scan. It reports — it never deletes content."
    ),
    no_args_is_help=True,
)


@scanner_app.command("run")
def scanner_run_cmd(
    full: Annotated[
        bool,
        typer.Option(
            "--full",
            help="Force a full corpus scan (ignore the incremental boundary).",
        ),
    ] = False,
    config: Annotated[
        str | None, typer.Option("--config", "-c", help="Path to config.yaml")
    ] = None,
) -> None:
    """Run one background scanner pass synchronously and print the summary.

    Scans the corpus for credential-shaped patterns and prints the counts
    (scanned, tagged, skipped) plus every pattern that matched. Incremental
    by default (only records past the last boundary); `--full` rescans
    everything — use it after enabling a new detection pattern.
    """
    mgr = get_manager(config)
    scanner = get_scanner(mgr)
    result = scanner.run_scan(incremental=not full)

    console.print(f"[green]✓[/green] Scan complete ({'full' if full else 'incremental'})")
    console.print(f"  records_scanned: {result.records_scanned}")
    console.print(f"  records_tagged:   {result.records_tagged}")
    console.print(f"  records_skipped:  {result.records_skipped}")
    console.print(f"  duration_sec:     {result.duration_sec:.2f}")
    if result.patterns_matched:
        console.print("  patterns_matched:")
        for name, count in sorted(result.patterns_matched.items()):
            console.print(f"    {name}: {count}")
    else:
        console.print("  patterns_matched: (none)")
    console.print(f"  timestamp:        {result.timestamp}")


@scanner_app.command("status")
def scanner_status_cmd(
    config: Annotated[
        str | None, typer.Option("--config", "-c", help="Path to config.yaml")
    ] = None,
) -> None:
    """Print the background scanner's current state.

    Shows whether the scheduled loop is running (and its interval), when the
    last pass finished, and when the next one fires. The first stop when
    `scanner run` finds nothing new but you suspect it never runs on its own.
    """
    mgr = get_manager(config)
    scanner = get_scanner(mgr)

    last = scanner.last_scan_ts
    last_str = last.isoformat().replace("+00:00", "Z") if last else "(never)"
    next_run = "(not scheduled)" if not scanner.running else f"every {scanner.interval_sec}s"

    console.print(f"  enabled:          {scanner.enabled}")
    console.print(f"  running:          {scanner.running}")
    console.print(f"  interval_hours:   {mgr.settings.scanner.interval_hours}")
    console.print(f"  incremental:      {mgr.settings.scanner.incremental}")
    console.print(f"  last_scan:        {last_str}")
    console.print(f"  total_tagged:     {scanner.total_tagged}")
    console.print(f"  next_scheduled:   {next_run}")
