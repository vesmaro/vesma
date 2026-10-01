"""``vesma update`` CLI — one command for the whole update family (issue #445).

Modes:

* ``vesma update`` / ``vesma update --check`` — report every update
  surface found on THIS machine: the pip dist that ``pip --user`` would
  upgrade (installed vs latest), the global npm package, the host
  prod-venvs and the Go binaries. Only pip (+ npm with ``--yes``) is
  ever changed; prod-venvs and Go binaries are report-only by design.
* ``vesma update --yes --scope=user`` — upgrade the installed dist via
  ``<python> -m pip install --user --upgrade`` (``--break-system-packages``
  appended only under a PEP 668 externally-managed interpreter), update
  the global npm package best-effort, append a record to
  ``~/.local/share/vesma/update-history.json``.
* ``vesma update --to <version>`` — the rollback path: same, but pinned
  (``pip install --user <dist>==<version>``).
* ``vesma update --install-timer`` / ``--uninstall-timer`` — install or
  remove the weekly systemd USER timer (``vesma-update.timer``).

The systemd unit files ship as ``contrib/vesma-update.{service,timer}``
AND as the ``_SERVICE_TEMPLATE`` / ``_TIMER_TEMPLATE`` constants below —
the constants are the install source (a pip install has no ``contrib/``
directory); a drift test pins the repo files byte-identical to them.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from vesmaro.updates import (
    CANDIDATE_DISTS,
    HISTORY_FILENAME,
    check_for_update,
    detect_installed_dist,
    fetch_latest,
)

logger = logging.getLogger(__name__)

NPM_PACKAGE = "@vesmaro/vesma"
NPM_BIN = "npm"
SERVICE_UNIT = "vesma-update.service"
TIMER_UNIT = "vesma-update.timer"

# Known host location of the production venvs (design machine-map). The
# literal path covers distrobox contexts where Path.home() points at the
# box home while the prod venvs live under the HOST home.
_PROD_VENV_BASES = (
    Path.home() / ".local" / "share",
    Path("/var/home/abyss/.local/share"),
)
_PROD_VENV_GLOB = "mnemos-prod/venv*"

_GO_BIN_NAMES = ("vesmaro-agent", "vesma-agent", "mnemos-mesh", "vesma-mesh")

_DEFAULT_EXEC_START = "%h/.local/bin/vesma update --yes --scope=user"
_FALLBACK_EXEC_START = "/usr/bin/env vesma update --yes --scope=user"

_SERVICE_TEMPLATE = """\
# vesma-update.service — weekly user-site auto-update (issue #445).
#
# WHAT IS AUTO-UPDATED: the pip user-site copy of the installed Vesma
#   dist (vesma-memory-server / vesma), and the global npm package
#   @vesmaro/vesma when present.
# WHAT IS NEVER AUTO-UPDATED: the production API/dashboard venvs (system
#   units — manual gate, see the upgrade runbook), the Go binaries
#   (vesmaro-agent/vesma-agent, mnemos-mesh/vesma-mesh — goreleaser
#   releases with checksum verification), and container images (CI
#   release artifacts).
#
# Install with `vesma update --install-timer`, remove with
# `vesma update --uninstall-timer`. --install-timer writes this file
# with the ExecStart chosen for this machine (%h launcher when present,
# /usr/bin/env fallback otherwise).
#
# Distrobox hosts: a systemd USER timer on the HOST cannot reach the
# boxes' pip user-sites. Adapt this unit the way the prod units do —
# one ExecStart line per box, e.g.:
#   ExecStart=distrobox-enter ubuntu-box -- vesma update --yes --scope=user
#   ExecStart=distrobox-enter vscode-box -- vesma update --yes --scope=user
# (systemd accumulates ExecStart= lines; keep the primary one first).

[Unit]
Description=Vesma weekly user-site update (pip --user, npm -g)
Documentation=https://github.com/vesmaro/vesma

[Service]
Type=oneshot
ExecStart={exec_start}
# No Restart=: a failed run simply waits for the next scheduled tick
# (no auto-retry storms, per the auto-update design).

[Install]
WantedBy=default.target
"""

_TIMER_TEMPLATE = """\
# vesma-update.timer — schedule for vesma-update.service (issue #445).
# Weekly with a 1h random delay; Persistent=true replays a tick missed
# while the machine was off at the next boot.

[Unit]
Description=Weekly Vesma user-site update

[Timer]
OnCalendar=weekly
Persistent=true
RandomizedDelaySec=1h
Unit=vesma-update.service

[Install]
WantedBy=timers.target
"""


# ── surface detection (report-only surfaces included) ────────────────────────


def _npm_global_version(npm: str) -> str | None:
    """Installed version of the global npm package, or ``None``."""
    proc = _run_cmd([npm, "ls", "-g", "--depth=0", "--json", NPM_PACKAGE], timeout=30)
    if proc is None or proc.returncode != 0:
        return None
    try:
        deps = json.loads(proc.stdout).get("dependencies", {})
        entry = deps.get(NPM_PACKAGE)
    except Exception:
        return None
    return entry.get("version") if isinstance(entry, dict) else None


def _prod_venv_paths() -> list[Path]:
    """Existing prod-venv markers (report-only: manual gate, see runbook)."""
    found: list[Path] = []
    for base in dict.fromkeys(_PROD_VENV_BASES):
        try:
            found.extend(sorted(base.glob(_PROD_VENV_GLOB)))
        except OSError:  # pragma: no cover — unreadable base dir
            continue
    return found


def _go_binaries() -> list[str]:
    """Go binaries of the family found in ``~/.local/bin`` (report-only)."""
    bin_dir = Path.home() / ".local" / "bin"
    found: list[str] = []
    for name in _GO_BIN_NAMES:
        try:
            if (bin_dir / name).exists():
                found.append(name)
        except OSError:  # pragma: no cover — unreadable bin dir
            continue
    return found


# ── subprocess helpers (injection points for tests) ──────────────────────────


def _run_cmd(
    cmd: list[str], timeout: int = 900
) -> subprocess.CompletedProcess[str] | None:
    """Run a subprocess, returning ``None`` instead of raising on failure."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def _systemctl(*args: str) -> int | None:
    """Best-effort ``systemctl --user``; ``None`` when unavailable/failed hard."""
    proc = _run_cmd(["systemctl", "--user", *args], timeout=60)
    return proc.returncode if proc is not None else None


def _pep668_externally_managed() -> bool:
    """True when the RUNNING interpreter's stdlib carries the PEP 668 marker."""
    import sysconfig

    stdlib = sysconfig.get_path("stdlib")
    return bool(stdlib) and (Path(stdlib) / "EXTERNALLY-MANAGED").exists()


def _append_history(*, dist: str, from_version: str, to_version: str, rc: int) -> None:
    """Append one record to ``~/.local/share/vesma/update-history.json``."""
    path = Path.home() / ".local" / "share" / "vesma" / HISTORY_FILENAME
    try:
        entries: list[object] = []
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    entries = loaded
            except Exception:
                entries = []
        entries.append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "from": from_version,
                "to": to_version,
                "dist": dist,
                "rc": rc,
            }
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    except Exception:
        logger.debug("update-history append failed", exc_info=True)


# ── check mode ───────────────────────────────────────────────────────────────


def _print_check(console: Console) -> None:
    detected = detect_installed_dist()
    info = check_for_update()
    latest = info.latest if info is not None else None

    table = Table(
        title="Vesma update surfaces (this machine)",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Surface")
    table.add_column("Installed")
    table.add_column("Latest")
    table.add_column("Note")

    for dist in CANDIDATE_DISTS:
        if detected is not None and dist == detected[0]:
            installed = detected[1]
            if latest is None:
                note = "latest unknown (offline or check disabled)"
            elif info is not None and info.update_available:
                note = "UPDATE AVAILABLE — run 'vesma update --yes --scope=user'"
            else:
                note = "up to date"
            table.add_row(f"pip: {dist}", installed, latest or "?", note)
        else:
            table.add_row(f"pip: {dist}", "-", "-", "not installed")

    npm = shutil.which(NPM_BIN)
    if npm is None:
        table.add_row(f"npm: {NPM_PACKAGE}", "-", "-", "npm not found (report only)")
    else:
        npm_version = _npm_global_version(npm)
        table.add_row(
            f"npm: {NPM_PACKAGE}",
            npm_version or "not installed",
            "-",
            "report only (updated with --yes when installed)",
        )

    venvs = _prod_venv_paths()
    for venv in venvs:
        table.add_row(f"prod venv: {venv}", "-", "-", "MANUAL GATE — see upgrade runbook")
    go_bins = _go_binaries()
    if go_bins:
        table.add_row(
            f"go binaries: {', '.join(go_bins)}",
            "-",
            "-",
            "report only — goreleaser releases (checksums)",
        )

    console.print(table)
    console.print(
        "[dim]`vesma update --yes --scope=user` changes only the pip user-site "
        "(and the npm package); prod venvs and Go binaries are never touched.[/dim]"
    )


# ── install mode (--yes) ─────────────────────────────────────────────────────


def _build_pip_cmd(dist: str, target: str, pin: bool) -> list[str]:
    cmd = [sys.executable, "-m", "pip", "install", "--user"]
    if pin:
        cmd.append(f"{dist}=={target}")
    else:
        cmd.extend(["--upgrade", dist])
    if _pep668_externally_managed():
        cmd.append("--break-system-packages")
    return cmd


def _run_user_update(console: Console, to: str | None) -> None:
    detected = detect_installed_dist()
    if detected is None:
        console.print(
            "[red]✗[/red] no pip-installed Vesma dist found "
            "(source checkout?) — nothing for pip --user to upgrade"
        )
        raise typer.Exit(1)
    dist, installed = detected

    if to is None:
        try:
            target = fetch_latest(dist)
        except Exception as exc:
            console.print(f"[red]✗[/red] cannot reach PyPI for {dist}: {exc}")
            raise typer.Exit(1) from None
        pin = False
    else:
        target = to
        pin = True

    cmd = _build_pip_cmd(dist, target, pin)
    console.print(f"[cyan]$[/cyan] {' '.join(cmd)}")
    proc = _run_cmd(cmd)
    rc = proc.returncode if proc is not None else 1
    if proc is not None and proc.stdout:
        console.print(proc.stdout.rstrip())
    if proc is not None and proc.returncode != 0 and proc.stderr:
        console.print(f"[red]{proc.stderr.rstrip()}[/red]")
    _append_history(dist=dist, from_version=installed, to_version=target, rc=rc)
    if rc == 0:
        console.print(f"[green]✓[/green] pip: {dist} {installed} → {target}")
    else:
        console.print(f"[red]✗[/red] pip upgrade failed (rc={rc}); history recorded")

    npm = shutil.which(NPM_BIN)
    if npm is not None and _npm_global_version(npm) is not None:
        console.print(f"[cyan]$[/cyan] {npm} install -g {NPM_PACKAGE}@latest")
        npm_proc = _run_cmd([npm, "install", "-g", f"{NPM_PACKAGE}@latest"], timeout=600)
        if npm_proc is not None and npm_proc.returncode == 0:
            console.print(f"[green]✓[/green] npm: {NPM_PACKAGE} updated")
        else:
            console.print(
                "[yellow]⚠[/yellow] npm update failed (best-effort) — "
                "run it manually: npm install -g " + f"{NPM_PACKAGE}@latest"
            )

    if rc == 0:
        console.print(
            "[yellow]restart clients (MCP/serve) to pick up the new version[/yellow]"
        )
    else:
        raise typer.Exit(1)


# ── timer install / removal ──────────────────────────────────────────────────


def _exec_start_for_machine() -> str:
    launcher = Path.home() / ".local" / "bin" / "vesma"
    return _DEFAULT_EXEC_START if launcher.exists() else _FALLBACK_EXEC_START


def _unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _install_timer(console: Console) -> None:
    unit_dir = _unit_dir()
    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / SERVICE_UNIT).write_text(
            _SERVICE_TEMPLATE.format(exec_start=_exec_start_for_machine()), encoding="utf-8"
        )
        (unit_dir / TIMER_UNIT).write_text(_TIMER_TEMPLATE, encoding="utf-8")
    except Exception as exc:
        console.print(f"[red]✗[/red] cannot write unit files to {unit_dir}: {exc}")
        raise typer.Exit(1) from None

    reloaded = _systemctl("daemon-reload")
    enabled = _systemctl("enable", "--now", TIMER_UNIT)
    if reloaded == 0 and enabled == 0:
        console.print(
            f"[green]✓[/green] {TIMER_UNIT} installed and enabled "
            "(weekly, Persistent — survives reboot)"
        )
        return
    console.print(
        f"[yellow]⚠[/yellow] unit files written to {unit_dir}, but systemctl --user "
        "is not usable here. Run manually:\n"
        "  systemctl --user daemon-reload\n"
        f"  systemctl --user enable --now {TIMER_UNIT}"
    )


def _uninstall_timer(console: Console) -> None:
    _systemctl("disable", "--now", TIMER_UNIT)
    unit_dir = _unit_dir()
    for name in (SERVICE_UNIT, TIMER_UNIT):
        try:
            (unit_dir / name).unlink(missing_ok=True)
        except OSError as exc:
            console.print(f"[yellow]⚠[/yellow] could not remove {unit_dir / name}: {exc}")
    _systemctl("daemon-reload")
    console.print(f"[green]✓[/green] {TIMER_UNIT} removed")


# ── the command ──────────────────────────────────────────────────────────────


def update(
    check: Annotated[
        bool,
        typer.Option("--check", help="Report surfaces without changing anything."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Perform the update (pip --user; npm best-effort)."),
    ] = False,
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Update scope. Only 'user' exists: prod venvs, Go binaries and "
            "containers are NEVER auto-updated.",
        ),
    ] = "user",
    to: Annotated[
        str | None,
        typer.Option("--to", help="Pin the pip target version (rollback path), e.g. --to 5.1.1."),
    ] = None,
    install_timer: Annotated[
        bool,
        typer.Option(
            "--install-timer",
            help="Install and enable the weekly systemd user update timer.",
        ),
    ] = False,
    uninstall_timer: Annotated[
        bool,
        typer.Option(
            "--uninstall-timer",
            help="Disable and remove the weekly systemd user update timer.",
        ),
    ] = False,
) -> None:
    """Check for updates / update the user-site install (issue #445).

    Without flags: a report of every update surface found on this machine
    (pip dists, npm package, prod venvs, Go binaries). Add --yes
    --scope=user to actually upgrade the pip user-site (and npm), or --to
    <version> to pin a specific (rollback) version.
    """
    console = Console()
    if install_timer and uninstall_timer:
        console.print("[red]✗[/red] --install-timer and --uninstall-timer are mutually exclusive")
        raise typer.Exit(1)
    if install_timer:
        _install_timer(console)
        return
    if uninstall_timer:
        _uninstall_timer(console)
        return
    if scope != "user":
        console.print(
            "[red]✗[/red] only --scope=user is supported: prod venvs, Go binaries "
            "and containers are manual surfaces by design"
        )
        raise typer.Exit(1)
    if yes:
        _run_user_update(console, to)
        return
    _print_check(console)
