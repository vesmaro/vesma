"""``mnemos import`` CLI subcommand — thin Typer wrapper over import logic.

Delegates to :mod:`vesmaro.cli.import_` for the actual import logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from vesmaro.cli._manager import get_manager
from vesmaro.cli.import_ import ImportMode, run_import

console = Console()

import_app = typer.Typer(
    name="import",
    help="Import memories from a JSON or SQLite export file.",
    no_args_is_help=True,
)


@import_app.callback(invoke_without_command=True)
def import_cmd(
    source: Annotated[Path, typer.Argument(help="Export file to import")],
    mode: Annotated[
        str,
        typer.Option("--mode", "-m", help="merge (idempotent) or restore (destructive)"),
    ] = ImportMode.MERGE,
    overwrite: Annotated[
        bool, typer.Option("--overwrite", help="Update existing memories in merge mode")
    ] = False,
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="Confirm destructive restore (required for restore mode)"),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate without writing")] = False,
    passphrase_file: Annotated[
        Path | None,
        typer.Option("--passphrase-file", help="Read decryption passphrase from this file"),
    ] = None,
    backup_dir: Annotated[
        Path | None,
        typer.Option("--backup-dir", help="Backup current DB here before restore"),
    ] = None,
    trusted_restore: Annotated[
        bool,
        typer.Option(
            "--trusted-restore",
            help=(
                "Operator asserts the export is a trusted self-backup: canon-line "
                "server-minted keys (checkpoint stamps, checkpoint-type canon "
                "envelope, canon warnings) are kept verbatim instead of stripped. "
                "Without this flag an import is UNTRUSTED input and those keys are "
                "stripped from every row (a forged checkpoint dedup key or canon "
                "envelope must never land as if the server had minted it)."
            ),
        ),
    ] = False,
    config: Annotated[
        str | None, typer.Option("--config", "-c", help="Path to config.yaml")
    ] = None,
) -> None:
    """Import memories from an export file (merge or restore)."""
    mgr = get_manager(config)

    if mode == ImportMode.RESTORE and not dry_run and not confirm:
        console.print(
            "[red]WARNING:[/red] restore mode will DELETE all existing memories, "
            "vectors, and projects. This cannot be undone."
        )
        console.print("Re-run with --confirm to proceed, or use --dry-run to preview.")
        raise typer.Exit(1)

    result = run_import(
        mgr,
        source,
        mode=mode,
        overwrite=overwrite,
        confirm=confirm,
        dry_run=dry_run,
        passphrase_file=passphrase_file,
        backup_dir=backup_dir,
        trusted_restore=trusted_restore,
    )

    label = "Dry run" if dry_run else "Imported"
    console.print(f"[green]✓[/green] {label}: {result.imported}")
    console.print(f"  skipped: {result.skipped}, updated: {result.updated}")
    if result.format_version:
        console.print(f"  format_version: {result.format_version}")
    if result.mnemos_version:
        console.print(f"  mnemos_version: {result.mnemos_version}")
    for w in result.warnings:
        console.print(f"  [yellow]⚠[/yellow] {w}")
    if result.errors:
        console.print(f"  [red]✗[/red] errors ({len(result.errors)}):")
        for err in result.errors[:10]:
            console.print(f"    - {err}")
        raise typer.Exit(1)
