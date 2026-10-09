"""``vesma import`` CLI subcommand — thin Typer wrapper over import logic.

Delegates to :mod:`vesma.cli.import_` for the actual import logic.

Registered as a PLAIN command (one positional + options), not a Typer
group: a group parses options only BEFORE the first positional argument
(click's ``allow_interspersed_args=False`` on MultiCommand), so
``vesma import f.json --mode merge`` used to die with exit 2 "Missing
argument 'source'" (cli-audit 2026-10-08, finding #4). A plain command
intersperses options and arguments freely — both orders parse.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from vesma.cli._manager import get_manager
from vesma.cli.import_ import ImportMode, run_import

console = Console()


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
    """Import memories from an export file (merge or restore).

    Merge mode is the safe default: idempotent by record id, `--overwrite`
    refreshes rows that already exist. Restore mode DELETES everything
    first — it refuses to run without `--confirm`, and `--backup-dir` takes
    a safety snapshot of the current DB before the clobber. Encrypted
    exports decrypt via `--passphrase-file`; `--dry-run` validates the
    file without writing.
    """
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
    if result.vesma_version:
        console.print(f"  vesma_version: {result.vesma_version}")
    for w in result.warnings:
        console.print(f"  [yellow]⚠[/yellow] {w}")
    if result.errors:
        console.print(f"  [red]✗[/red] errors ({len(result.errors)}):")
        for err in result.errors[:10]:
            console.print(f"    - {err}")
        raise typer.Exit(1)
