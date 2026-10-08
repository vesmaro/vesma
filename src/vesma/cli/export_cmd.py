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
from vesma.models import MemoryStatus, normalize_tag_aliases

console = Console()

export_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="export",
    help=(
        "Export memories to a JSON or SQLite backup file.\n\n"
        "Writes a portable backup of the store: JSON (metadata, compactable "
        "with federation payloads) or a full SQLite snapshot, optionally "
        "compressed and AES-256-GCM encrypted. Filter the export by project, "
        "agent, status, tags or date window; `--dry-run` validates without "
        "writing. The counterpart is `vesma import`."
    ),
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
        typer.Option(
            "--tags",
            help=(
                "Comma-separated tags to filter by. `vesma:` is the canonical "
                "storage prefix, stable by contract; `mnemos:` is accepted as an "
                "input alias everywhere (dual-accept window, ADR-0044)."
            ),
        ),
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
    """Export memories to a backup file (JSON metadata or SQLite snapshot).

    Select the output with `--output`/`-o` and the format with `--format`
    (json or sqlite); `--compress` and `--encrypt` (passphrase via
    `--passphrase-file`) harden the artifact for off-machine storage. The
    filter flags (`--project`, `--agent`, `--status`, `--tags`, `--since`,
    `--until`) narrow what goes in; `--dry-run` checks inputs only.
    """
    mgr = get_manager(config)

    # Input boundary (6.0.0): vesma:* aliases normalize to the stored
    # mnemos:* form before the export filter matches against row tags.
    tag_list = (
        normalize_tag_aliases([t.strip() for t in tags.split(",") if t.strip()]) if tags else None
    )
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
