"""``vesma service`` CLI sub-app — install/uninstall (wave W4).

Only ``install`` and ``uninstall`` exist in v1: status / logs / start /
stop / restart / run arrive with the supervisor wave (W3) — the sub-app
is the extension point. Convention (cli-architecture): subcommand =
function (WHAT), flag = configuration (HOW).
"""

from __future__ import annotations

from typing import Annotated, Optional

import typer
from rich.console import Console

from vesmaro.service.install import InstallError, install, uninstall
from vesmaro.service.errors import ManifestError

service_app = typer.Typer(
    name="service",
    help="Install/uninstall the vesma service (component manifests, venvs, "
    "the generated systemd user unit).",
    no_args_is_help=True,
)
console = Console()


def _fail(exc: Exception) -> None:
    console.print(f"[red]✗[/red] {exc}")
    raise typer.Exit(1) from exc


@service_app.command(name="install")
def service_install() -> None:
    """Install the service: pack manifests, data dirs, venvs, the unit.

    Idempotent — re-running regenerates every artifact (hand edits to the
    unit are overwritten by design, threat model "ручная правка юнита").
    Inside a container the filesystem hardening directives are loudly
    downgraded (marker in the unit + report lines).
    """
    try:
        result = install()
    except (InstallError, ManifestError) as exc:
        _fail(exc)
    for line in result.lines:
        style = "[yellow]⚠[/yellow]" if line.startswith("CONTAINER DOWNGRADE") else "[green]✓[/green]"
        console.print(f"{style} {line}")
    console.print(f"[green]Service installed ({result.unit_path}).[/green]")


@service_app.command(name="uninstall")
def service_uninstall(
    name: Annotated[
        Optional[str],
        typer.Argument(help="Component name (e.g. board, metrics).", show_default=False),
    ] = None,
    remove_all: Annotated[
        bool,
        typer.Option("--all", help="Uninstall everything: stop/disable + remove the unit and all components."),
    ] = False,
) -> None:
    """Uninstall one component, or the whole installation with --all.

    Only files the install flow owns are removed (manifest, env file,
    venv). Component data dirs are operator data and are preserved.
    """
    try:
        result = uninstall(name, remove_all=remove_all)
    except (InstallError, ManifestError) as exc:
        _fail(exc)
    for line in result.lines:
        console.print(f"[green]✓[/green] {line}")
    console.print("[green]Uninstall complete.[/green]")


if __name__ == "__main__":  # pragma: no cover — manual invocation
    service_app()
