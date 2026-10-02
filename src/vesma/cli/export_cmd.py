"""``vesma export`` CLI subcommand — thin Typer wrapper over export logic.

Delegates to :mod:`vesma.cli.export` for the actual export logic so the
logic stays testable without Typer's CliRunner.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from vesma.cli._manager import get_manager
from vesma.cli.export import (
    CompressMode,
    ExportFilter,
    ExportFormat,
    run_export,
)
from vesma.models import MemoryStatus

console = Console()

export_app = typer.Typer(
    name="export",
    help="Export memories to a JSON or SQLite backup file.",
    no_args_is_help=True,
)


def _parse_since(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"Invalid date: {value}") from exc


@export_app.callback(invoke_without_command=True)
def export_cmd(
    output: Annotated[Path, typer.Option("--output", "-o", help="Output file path")] = Path(
        "vesma-export.json"
    ),
    format: Annotated[
        ExportFormat, typer.Option("--format", "-f", help="Export format")
    ] = ExportFormat.JSON,
    compress: Annotated[
        CompressMode, typer.Option("--compress", help="Compression mode")
    ] = CompressMode.NONE,
    encrypt: Annotated[
        bool, typer.Option("--encrypt", help="Encrypt with passphrase (AES-256-GCM)")
    ] = False,
    passphrase_file: Annotated[
        Path | None,
        typer.Option("--passphrase-file", help="Read passphrase from this file"),
    ] = None,
    project: Annotated[str | None, typer.Option("--project", help="Filter by project slug")] = None,
    agent: Annotated[str | None, typer.Option("--agent", help="Filter by agent slug")] = None,
    status: Annotated[
        MemoryStatus | None, typer.Option("--status", help="Filter by memory status")
    ] = None,
    tags: Annotated[
        str | None,
        typer.Option("--tags", help="Comma-separated tags to filter by"),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only memories created/updated after this ISO date"),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option("--until", help="Only memories created/updated before this ISO date"),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate inputs without writing")
    ] = False,
    config: Annotated[
        str | None, typer.Option("--config", "-c", help="Path to config.yaml")
    ] = None,
) -> None:
    """Export memories to a backup file (JSON metadata or SQLite snapshot)."""
    mgr = get_manager(config)

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    filt = ExportFilter(
        project=project,
        agent=agent,
        status=status,
        tags=tag_list,
        since=_parse_since(since),
        until=_parse_since(until),
    )

    passphrase: str | None = None
    if encrypt and passphrase_file is None:
        passphrase = typer.prompt("Passphrase", hide_input=True, confirmation_prompt=True)

    if dry_run:
        # Validate filters + format without writing.
        console.print(f"[cyan]Dry run: would export {format.value} → {output}[/cyan]")
        console.print(f"  filter: {filt.to_dict()}")
        console.print(f"  compress: {compress.value}, encrypt: {encrypt}")
        return

    result = run_export(
        mgr,
        fmt=format,
        output=output,
        compress=compress,
        encrypt=encrypt,
        passphrase=passphrase,
        passphrase_file=passphrase_file,
        filt=filt,
    )
    console.print(f"[green]✓[/green] Exported {result.memory_count} memories → {result.path}")
    console.print(f"  format: {result.format.value}, compress: {result.compress.value}")
    console.print(f"  encrypted: {result.encrypted}, bytes: {result.bytes_written}")
    for w in result.warnings:
        console.print(f"  [yellow]⚠[/yellow] {w}")
