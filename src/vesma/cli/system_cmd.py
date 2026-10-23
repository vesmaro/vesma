"""``vesma system`` CLI — the SYSTEM profile domain (install/uninstall/update).

Mental model = apt vs systemctl: ``vesma system`` owns the SYSTEM profile's
lifecycle — the FHS tree (``/etc/vesma``, ``/var/lib/vesma``,
``/usr/lib/vesma``, root required) — while ``vesma service`` owns the
daemon's RUNTIME control (status/health/start/stop/restart/logs/run) and
the user-profile install; the user profile updates via ``vesma self-update``.

Each verb delegates to the SAME implementation functions the deprecated
``vesma service <verb> --system`` spellings call (CLI-topology wave,
variant B — owner-ratified 2026-10-23): zero logic duplication, wiring
only. The old spellings remain as hidden deprecated aliases on ``vesma
service`` (identical behavior + a one-line stderr hint; removal not
before 9.0).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

import typer
from rich.console import Console
from rich.text import Text

from vesma.service.errors import ManifestError
from vesma.service.install import InstallError
from vesma.service.system_install import (
    SystemInstallError,
    install_system,
    uninstall_system,
)
from vesma.service.system_update import update_system

if TYPE_CHECKING:
    from vesma.service.system_install import SystemInstallResult, SystemUninstallResult
    from vesma.service.system_update import SystemUpdateResult

system_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="system",
    help=(
        "Install, update, or uninstall the vesma SYSTEM profile (FHS: "
        "/etc/vesma, /var/lib/vesma, /usr/lib/vesma) — requires root (sudo).\n\n"
        "The system domain owns the SYSTEM profile's lifecycle: `install` "
        "lays down the FHS tree, the sysusers.d service account, root-owned "
        "venv revisions and the systemd system unit; `update` materializes "
        "the running build's revisions beside the current ones with an "
        "atomic symlink swap and a health gate; `uninstall` removes exactly "
        "the recorded install-state artifacts. Runtime control of the daemon "
        "(status/health/start/stop/restart/logs/run) and the user-profile "
        "install stay on `vesma service`; the user profile updates via "
        "`vesma self-update`."
    ),
    no_args_is_help=True,
)

console = Console()


def _fail_system(exc: Exception) -> NoReturn:
    # Text(exc): report DATA must never pass through rich markup — an
    # exception message containing '[' is operator text, not formatting.
    console.print("[red]✗[/red]", Text(str(exc)))
    raise typer.Exit(1) from exc


def _print_report(lines: tuple[str, ...], warn_prefixes: tuple[str, ...]) -> None:
    """Render impl report lines: ⚠ for warnings, ✓ for the rest.

    Text(line): report lines carry operator data (paths, names) — rendered
    without markup, only the prefix is styling.
    """
    for line in lines:
        warn_line = line.startswith(warn_prefixes)
        style = "[yellow]⚠[/yellow]" if warn_line else "[green]✓[/green]"
        console.print(style, Text(line))


@system_app.command(name="install")
def system_install() -> None:
    """Install the SYSTEM profile: the FHS tree, the account, the unit.

    The canonical system-profile installer (specs/layout/v2 §3.13):
    sysusers.d service account, the FHS tree from the ONE declarative
    manifest, root-owned venv revisions materialized by uv, install-state
    with exact-inverse uninstall. Requires root (sudo).

    Idempotent — re-running regenerates every artifact (hand edits to the
    unit are overwritten by design). Inside a container the filesystem
    hardening directives are loudly downgraded (marker in the unit +
    report lines).
    """
    try:
        result: SystemInstallResult = install_system()
    except (InstallError, ManifestError, SystemInstallError) as exc:
        _fail_system(exc)
    for line in result.lines:
        downgraded_line = line.startswith("CONTAINER DOWNGRADE")
        warn_line = line.startswith(("WARN:", "note:", "preserved"))
        style = "[yellow]⚠[/yellow]" if (downgraded_line or warn_line) else "[green]✓[/green]"
        console.print(style, Text(line))
    console.print(f"[green]Service installed ({result.unit_path}).[/green]")


@system_app.command(name="uninstall")
def system_uninstall() -> None:
    """Uninstall the SYSTEM profile: the exact inverse of the install-state.

    Removes the recorded artifacts only, in reverse creation order (specs/
    layout/v2 §3.13); the /var/lib/vesma data plane is preserved; state ↔
    filesystem drift refuses loudly WITHOUT touching the tree. The whole
    profile is one unit — there are no per-component or --all forms here
    (that is the user-profile `vesma service uninstall`). Requires root
    (sudo).
    """
    try:
        result: SystemUninstallResult = uninstall_system()
    except (InstallError, ManifestError, SystemInstallError) as exc:
        _fail_system(exc)
    _print_report(result.lines, ("WARN:", "preserved"))
    console.print("[green]Uninstall complete.[/green]")


@system_app.command(name="update")
def system_update() -> None:
    """Update the SYSTEM profile in place (root required).

    Materializes the running build's venv revisions beside the current
    ones (specs/layout/v2 §3.13): atomic symlink swap, health gate, the
    previous revision retired only after health passes. Drift refuses
    loudly BEFORE any action; a failed health gate rolls back to the
    previous revision; re-runs are idempotent. The user profile updates
    via `vesma self-update`.
    """
    try:
        result: SystemUpdateResult = update_system()
    except (InstallError, ManifestError, SystemInstallError) as exc:
        _fail_system(exc)
    _print_report(
        result.lines,
        ("WARN:", "note:", "preserved", "rollback:", "conffile kept"),
    )
    console.print(f"[green]System profile updated ({result.engine_venv}).[/green]")


if __name__ == "__main__":  # pragma: no cover — manual invocation
    system_app()
