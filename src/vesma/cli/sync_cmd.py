"""``vesma sync`` CLI subcommand — thin Typer wrapper over sync logic.

Delegates to :mod:`vesma.cli.sync` for the actual sync logic so the
logic stays testable without Typer's CliRunner. Two subcommands:

* ``vesma sync export`` — build + write a compact federation payload.
* ``vesma sync import`` — read + validate + merge a compact payload.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from vesma.cli._manager import get_manager
from vesma.cli.sync import run_sync_export, run_sync_import

console = Console()

sync_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="sync",
    help=(
        "Federation Phase 0 batch sync — export/import compact payloads "
        "between Vesma instances.\n\n"
        "Move shared projects between Vesma instances as file-based batches: "
        "`export` writes a compact `vesmaro.federation.v1` payload (optionally "
        "AES-256-GCM encrypted), `import` merges one idempotently by record "
        "id. Scheduled federation comes later — this is the operator-driven path."
    ),
    no_args_is_help=True,
)


@sync_app.command("export")
def sync_export_cmd(
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Output file path (absolute recommended)."),
    ] = Path("vesma-sync.json"),
    encrypt: Annotated[
        bool,
        typer.Option(
            "--encrypt",
            help=(
                "Encrypt with AES-256-GCM. Passphrase read from VESMA_EXPORT_PASSPHRASE env var."
            ),
        ),
    ] = False,
    shared_projects: Annotated[
        str | None,
        typer.Option(
            "--shared-projects",
            help=(
                "Space- or comma-separated project slugs to sync "
                "(overrides federation.shared_projects)."
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Build payload and print summary; do NOT write the file."),
    ] = False,
    config: Annotated[
        str | None, typer.Option("--config", "-c", help="Path to config.yaml")
    ] = None,
) -> None:
    """Export memories in the compact federation format (vesmaro.federation.v1).

    Writes the shared projects (config `federation.shared_projects`, or an
    explicit `--shared-projects` list) to `--output` as one compact payload.
    `--encrypt` seals it with AES-256-GCM using the VESMA_EXPORT_PASSPHRASE
    env var; `--dry-run` builds the payload and prints the summary without
    writing a file.
    """
    mgr = get_manager(config)
    try:
        result = run_sync_export(
            mgr,
            output=output,
            encrypt=encrypt,
            shared_projects_arg=shared_projects,
            dry_run=dry_run,
        )
    except ValueError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1) from exc

    label = "Dry-run" if dry_run else "Exported"
    console.print(f"[green]✓[/green] {label}: {result.records_exported} records")
    console.print(f"  refused: {result.records_refused}")
    console.print(f"  secrets_redacted: {result.secrets_redacted}")
    console.print(f"  pii_anonymized: {result.pii_anonymized}")
    console.print(f"  encrypted: {result.encrypted}")
    console.print(f"  shared_projects: {', '.join(result.shared_projects) or '(none)'}")
    if result.output is not None:
        console.print(f"  path: {result.output}")
    for err in result.errors:
        console.print(f"  [red]✗[/red] {err}")
    if result.errors:
        raise typer.Exit(1)


@sync_app.command("import")
def sync_import_cmd(
    source: Annotated[Path, typer.Argument(help="Compact payload file to import.")],
    passphrase_env: Annotated[
        str | None,
        typer.Option(
            "--passphrase-env",
            help=(
                "Name of the env var holding the decryption passphrase "
                "(default: VESMA_EXPORT_PASSPHRASE)."
            ),
        ),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate without writing.")] = False,
    config: Annotated[
        str | None, typer.Option("--config", "-c", help="Path to config.yaml")
    ] = None,
) -> None:
    """Import a compact federation payload (merge, idempotent by record id).

    Merges the payload's memories into the local store — re-importing the
    same file is a no-op, so retries after a partial run are safe. Encrypted
    payloads decrypt via the env var named by `--passphrase-env` (default
    VESMA_EXPORT_PASSPHRASE). Run it on the RECEIVING instance.
    """
    mgr = get_manager(config)
    result = run_sync_import(
        mgr,
        source=source,
        passphrase_env=passphrase_env,
        dry_run=dry_run,
    )

    label = "Dry-run" if dry_run else "Imported"
    console.print(f"[green]✓[/green] {label}: {result.records_imported} records")
    console.print(f"  skipped: {result.records_skipped}")
    if result.format_version:
        console.print(f"  format_version: {result.format_version}")
    for w in result.warnings:
        console.print(f"  [yellow]⚠[/yellow] {w}")
    if result.errors:
        console.print(f"  [red]✗[/red] errors ({len(result.errors)}):")
        for err in result.errors[:10]:
            console.print(f"    - {err}")
        raise typer.Exit(1)
