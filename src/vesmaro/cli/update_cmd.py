"""``vesma update`` CLI — one command for the whole update family (issue #445).

Modes:

* ``vesma update`` — report every update surface found on THIS machine
  (installed vs latest pip dist, the global npm package, the host
  prod-venvs, the Go binaries) and — in an interactive terminal, when a
  pip update is pending — ask ``Apply update? [y/N]`` and apply on yes
  (issue #460). Non-TTY contexts (pipes/CI) stay check-only and print
  ``apply with: vesma update --yes``. ``--check`` is always check-only.
  Only pip (+ npm) is ever changed; prod-venvs and Go binaries are
  report-only by design.
* ``vesma update --yes`` (alias ``-y``) — skip the prompt and apply:
  upgrade the installed dist via
  ``<python> -m pip install --user --upgrade`` (``--break-system-packages``
  appended only under a PEP 668 externally-managed interpreter), update
  the global npm package best-effort, append a record to
  ``~/.local/share/vesma/update-history.json``. Pip output is captured —
  one summary line per surface; ``--verbose`` prints the full pip output
  (and it is shown as a tail automatically on failure).
* ``vesma update --to <version>`` — the rollback path: same, but pinned
  (``pip install --user <dist>==<version>``).
* ``vesma update --install-timer`` / ``--uninstall-timer`` — install or
  remove the weekly systemd USER timer (``vesma-update.timer``). Inside
  a distrobox the units are written to the HOST home with one
  ExecStart line per box (issue #468) — units in the box home would
  never be loaded by the host user manager.

The systemd unit files ship as ``contrib/vesma-update.{service,timer}``
AND as the ``_SERVICE_TEMPLATE`` / ``_TIMER_TEMPLATE`` constants below —
the constants are the install source (a pip install has no ``contrib/``
directory); a drift test pins the repo files byte-identical to them.
"""

from __future__ import annotations

import getpass
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
    UpdateInfo,
    check_for_update,
    detect_installed_dist,
    fetch_latest,
    version_key,
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


def _run_cmd(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess[str] | None:
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


def _stdin_is_tty() -> bool:
    """True when stdin is an interactive terminal (gates the confirm prompt).

    Injection point for tests: CliRunner stdin is never a TTY, so tests
    monkeypatch this to simulate an interactive ``vesma update``.
    """
    try:
        return bool(sys.stdin.isatty())
    except Exception:  # pragma: no cover — exotic stdin must never crash the CLI
        return False


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


def _print_check(console: Console) -> UpdateInfo | None:
    """Print the surfaces table; return the pip update info it was built from."""
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
            elif version_key(installed) > version_key(latest):
                # installed > latest even after a fresh check (#460): the
                # honest wording for a local build or an unpublished release.
                note = "newer than published latest (local build?)"
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
    return info


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


def _run_user_update(console: Console, to: str | None, *, verbose: bool = False) -> None:
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
    if verbose:
        console.print(f"[cyan]$[/cyan] {' '.join(cmd)}")
    # Output is ALWAYS captured (#460: no pip firehose); --verbose chooses
    # whether it is echoed in full or condensed to a one-line summary.
    proc = _run_cmd(cmd)
    rc = proc.returncode if proc is not None else 1
    if verbose:
        if proc is not None and proc.stdout:
            console.print(proc.stdout.rstrip())
        if proc is not None and rc != 0 and proc.stderr:
            console.print(f"[red]{proc.stderr.rstrip()}[/red]")
    _append_history(dist=dist, from_version=installed, to_version=target, rc=rc)
    if rc == 0:
        if verbose:
            console.print(f"[green]✓[/green] pip: {dist} {installed} → {target}")
        else:
            upgraded = detect_installed_dist()
            new_version = upgraded[1] if upgraded is not None and upgraded[0] == dist else installed
            if version_key(new_version) > version_key(installed):
                console.print(f"[green]✓[/green] pip: {dist} {installed} → {new_version}")
            else:
                console.print(f"[green]✓[/green] pip: {dist} {installed} — already current")
    else:
        if verbose:
            console.print(f"[red]✗[/red] pip upgrade failed (rc={rc}); history recorded")
        else:
            console.print(f"[red]✗[/red] pip upgrade failed (rc={rc}) — last pip output:")
            if proc is not None:
                combined = "\n".join(
                    chunk.rstrip()
                    for chunk in (proc.stdout or "", proc.stderr or "")
                    if chunk.strip()
                )
                for line in combined.splitlines()[-15:]:
                    console.print(line)
            console.print("[dim]re-run with --verbose for the full pip log[/dim]")

    npm = shutil.which(NPM_BIN)
    if npm is not None and _npm_global_version(npm) is not None:
        if verbose:
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
        console.print("[yellow]restart clients (MCP/serve) to pick up the new version[/yellow]")
    else:
        raise typer.Exit(1)


# ── timer install / removal ──────────────────────────────────────────────────


def _exec_start_for_machine() -> str:
    launcher = Path.home() / ".local" / "bin" / "vesma"
    return _DEFAULT_EXEC_START if launcher.exists() else _FALLBACK_EXEC_START


def _unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


# ── distrobox context (#468) ──────────────────────────────────────────────────

#: Distrobox/podman container marker. Module constant so tests can fake a
#: host that is not a container (this dev machine runs inside a box, where
#: the marker exists — tests must stay deterministic).
_CONTAINERENV = Path("/run/.containerenv")


def _distrobox_context() -> tuple[bool, str | None, Path | None]:
    """Classify the distrobox context: ``(in_container, box, host_home)``.

    Detection: the resolved HOME carries the ``/.distrobox/<box>/...``
    layout (box name and host home are derivable), or the container
    marker file exists (in a container, but the host home cannot be
    derived). The host home is the path prefix before the ``.distrobox``
    component of the resolved HOME — computed from the path itself, no
    subprocess probing (#468).
    """
    home = Path.home().resolve()
    parts = home.parts
    if ".distrobox" in parts:
        idx = parts.index(".distrobox")
        box = parts[idx + 1] if idx + 1 < len(parts) else None
        host = Path(*parts[:idx]) if idx > 0 else None
        if box is not None and host is not None:
            return True, box, host
        return True, None, None
    if _CONTAINERENV.exists():
        return True, None, None
    return False, None, None


def _host_user() -> str:
    """Host username for the ``-M <user>@.host`` systemctl transport."""
    try:
        return getpass.getuser()
    except Exception:  # environments without USER/LOGNAME must not crash
        return ""


def _systemctl_host(*args: str) -> int | None:
    """Best-effort ``systemctl --user -M <user>@.host`` (host user manager)."""
    proc = _run_cmd(["systemctl", "--user", "-M", f"{_host_user()}@.host", *args], timeout=60)
    return proc.returncode if proc is not None else None


def _box_exec_start_value(box: str, host_home: Path) -> str:
    """ExecStart VALUE (after ``ExecStart=``) scheduling one box (#468).

    Mirrors the live host units: ``distrobox-enter`` resolves ``$HOME``
    to the box home inside the shell, so each box updates its own pip
    user-site while the unit itself lives in the HOST home.
    """
    enter = host_home / ".local" / "bin" / "distrobox-enter"
    return (
        f"{enter} -n {box} -- /bin/sh -c "
        "'exec \"$HOME/.local/bin/vesma\" update --yes --scope=user'"
    )


def _append_exec_start_line(text: str, line: str) -> str:
    """Insert an ``ExecStart=`` line after the last existing one.

    systemd accumulates ExecStart= entries: a second box must ADD its
    line, never replace earlier boxes'. Falls back to inserting after
    ``Type=oneshot`` (or at EOF) when the unit carries no ExecStart yet.
    """
    lines = text.splitlines(keepends=True)
    exec_idxs = [i for i, ln in enumerate(lines) if ln.startswith("ExecStart=")]
    if exec_idxs:
        insert_at = exec_idxs[-1] + 1
    else:
        try:
            insert_at = next(i for i, ln in enumerate(lines) if ln.strip() == "Type=oneshot") + 1
        except StopIteration:
            insert_at = len(lines)
    lines.insert(insert_at, line if line.endswith("\n") else line + "\n")
    return "".join(lines)


# ── timer install / removal ──────────────────────────────────────────────────


def _install_timer(console: Console) -> None:
    """Install the weekly timer — on the host, or into the HOST home from a box."""
    in_container, box, host_home = _distrobox_context()
    if in_container:
        if box is None or host_home is None:
            # A container whose host home cannot be derived: writing units
            # here would schedule dead units (#468) — refuse with the recipe.
            console.print(
                "[yellow]⚠[/yellow] running in a container, but the host home cannot be "
                "derived from HOME — installing here would schedule units the host manager "
                "never loads. On the HOST, copy contrib/vesma-update.{service,timer} into "
                "~/.config/systemd/user/ and add one ExecStart line per box:"
            )
            console.print(
                "  ExecStart=<host-home>/.local/bin/distrobox-enter -n <box> -- /bin/sh -c "
                "'exec \"$HOME/.local/bin/vesma\" update --yes --scope=user'"
            )
            return
        _install_timer_in_box(console, box, host_home)
        return
    _install_timer_on_host(console)


def _install_timer_on_host(console: Console) -> None:
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


def _install_timer_in_box(console: Console, box: str, host_home: Path) -> None:
    """Install the weekly timer into the HOST user manager for one box (#468).

    The units live in the HOST home — the host user manager loads units
    from there only, so writing inside the box would schedule nothing.
    The service accumulates one ExecStart line per box (idempotent: a
    re-run never duplicates a line). Enabling goes through the
    ``-M <user>@.host`` transport; when the host manager is not
    reachable from inside the box, the exact manual host-side commands
    (incl. the linger hint) are printed and the command still succeeds —
    the units are in place.
    """
    unit_dir = host_home / ".config" / "systemd" / "user"
    exec_value = _box_exec_start_value(box, host_home)
    line = f"ExecStart={exec_value}"
    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        service_path = unit_dir / SERVICE_UNIT
        if service_path.exists():
            text = service_path.read_text(encoding="utf-8")
            if line not in text:  # idempotent — never duplicate a box line
                service_path.write_text(_append_exec_start_line(text, line), encoding="utf-8")
        else:
            service_path.write_text(
                _SERVICE_TEMPLATE.format(exec_start=exec_value), encoding="utf-8"
            )
        timer_path = unit_dir / TIMER_UNIT
        if not timer_path.exists():
            timer_path.write_text(_TIMER_TEMPLATE, encoding="utf-8")
    except Exception as exc:
        console.print(f"[red]✗[/red] cannot write unit files to {unit_dir}: {exc}")
        raise typer.Exit(1) from None

    reloaded = _systemctl_host("daemon-reload")
    enabled = _systemctl_host("enable", "--now", TIMER_UNIT)
    if reloaded == 0 and enabled == 0:
        console.print(
            f"[green]✓[/green] {TIMER_UNIT} installed on the HOST for box '{box}' and "
            "enabled (weekly, Persistent — survives reboot)"
        )
        return
    user = _host_user()
    console.print(
        f"[yellow]⚠[/yellow] unit files written to {unit_dir} (host), but the host user "
        f"manager is not reachable from box '{box}'. Run on the HOST:\n"
        f"  systemctl --user -M {user}@.host daemon-reload\n"
        f"  systemctl --user -M {user}@.host enable --now {TIMER_UNIT}\n"
        f"  sudo loginctl enable-linger {user}  # so user timers run without an active session"
    )


def _remove_timer_units(console: Console, unit_dir: Path, *, host_manager: bool) -> None:
    """Disable + remove the timer units via the local or the host manager."""
    systemctl = _systemctl_host if host_manager else _systemctl
    systemctl("disable", "--now", TIMER_UNIT)
    for name in (SERVICE_UNIT, TIMER_UNIT):
        try:
            (unit_dir / name).unlink(missing_ok=True)
        except OSError as exc:
            console.print(f"[yellow]⚠[/yellow] could not remove {unit_dir / name}: {exc}")
    systemctl("daemon-reload")
    console.print(f"[green]✓[/green] {TIMER_UNIT} removed")


def _uninstall_timer(console: Console) -> None:
    """Remove the weekly timer — symmetric to :func:`_install_timer` (#468)."""
    in_container, box, host_home = _distrobox_context()
    if in_container:
        if box is None or host_home is None:
            console.print(
                "[yellow]⚠[/yellow] running in a container, but the host home cannot be "
                f"derived from HOME — remove the host units manually on the HOST "
                f"(~/.config/systemd/user/{SERVICE_UNIT}, {TIMER_UNIT})."
            )
            return
        _uninstall_timer_in_box(console, box, host_home)
        return
    _uninstall_timer_on_host(console)


def _uninstall_timer_on_host(console: Console) -> None:
    _remove_timer_units(console, _unit_dir(), host_manager=False)


def _uninstall_timer_in_box(console: Console, box: str, host_home: Path) -> None:
    """Remove THIS box's scheduled update from the HOST units (#468).

    Only this box's ExecStart line is removed; the units survive while
    other box entries remain (uninstalling one box must never
    unschedule the others) and are disabled+removed only when the last
    box line goes. The host's own (non-box) ExecStart is never touched.
    """
    unit_dir = host_home / ".config" / "systemd" / "user"
    service_path = unit_dir / SERVICE_UNIT
    try:
        text = service_path.read_text(encoding="utf-8")
    except OSError:
        console.print(
            f"[yellow]⚠[/yellow] no {SERVICE_UNIT} in {unit_dir} (host) — nothing to remove"
        )
        return
    ours = f"distrobox-enter -n {box} --"
    lines = text.splitlines(keepends=True)
    kept = [ln for ln in lines if not (ln.startswith("ExecStart=") and ours in ln)]
    if len(kept) == len(lines):
        console.print(
            f"[yellow]⚠[/yellow] box '{box}' is not scheduled in host {SERVICE_UNIT} — "
            "nothing to remove"
        )
        return
    others_left = [ln for ln in kept if ln.startswith("ExecStart=") and "distrobox-enter" in ln]
    if others_left:
        service_path.write_text("".join(kept), encoding="utf-8")
        _systemctl_host("daemon-reload")
        console.print(
            f"[green]✓[/green] box '{box}' removed from host {SERVICE_UNIT}; other box "
            "entries remain — the timer stays enabled"
        )
        return
    _remove_timer_units(console, unit_dir, host_manager=True)


# ── the command ──────────────────────────────────────────────────────────────


def update(
    check: Annotated[
        bool,
        typer.Option("--check", help="Report surfaces without changing anything."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Apply the update without prompting (pip --user; npm best-effort).",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            help="Print the full pip output instead of a one-line summary per surface "
            "(on failure the last pip lines are shown either way).",
        ),
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
    """Check for updates / update the user-site install (issues #445, #460).

    Without flags: the surfaces report, then — in an interactive terminal,
    when a pip update is pending — a confirmation prompt before applying
    (pipes/CI stay check-only and print ``apply with: vesma update --yes``).
    ``--yes``/``-y`` applies without prompting; ``--verbose`` prints the
    full pip output; ``--to <version>`` pins a specific (rollback) version
    (apply it with ``--yes``/``-y``). ``--check`` never applies or prompts.
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
        _run_user_update(console, to, verbose=verbose)
        return
    info = _print_check(console)
    if check:
        return
    if info is None or not info.update_available:
        return  # nothing pending — the report stands as-is
    if _stdin_is_tty():
        if typer.confirm("Apply update?", default=False):
            _run_user_update(console, to, verbose=verbose)
        return
    console.print("[dim]apply with: vesma update --yes[/dim]")
