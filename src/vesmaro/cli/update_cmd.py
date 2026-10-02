"""``vesma update`` CLI — one command for the whole update family (issue #445).

Subcommands (standing design rule: flags do not replace subcommands):

* ``vesma update`` — report every update surface found on THIS machine
  (installed vs latest pip dist, the global npm package, the host
  prod-venvs, the Go binaries) and — in an interactive terminal, when a
  pip update is pending — ask ``Apply update? [y/N]`` and apply on yes
  (issue #460). Non-TTY contexts (pipes/CI) stay check-only and print
  ``apply with: vesma update --yes``.
* ``vesma update check`` — the report only: never prompts, never
  applies (safe in pipes and CI).
* ``vesma update apply [--to VERSION] [--scope user] [-y] [--verbose]``
  — the apply path: upgrade the installed dist via
  ``<python> -m pip install --user --upgrade`` (``--break-system-packages``
  appended only under a PEP 668 externally-managed interpreter), update
  the global npm package best-effort, append a record to
  ``~/.local/share/vesma/update-history.json``. Pip output is captured —
  one summary line per surface; ``--verbose`` prints the full pip output
  (and it is shown as a tail automatically on failure). ``--to <version>``
  pins a specific (rollback) version.
* ``vesma update timer install|uninstall|status`` — manage or inspect the
  weekly systemd USER timer (``vesma-update.timer``). Inside a distrobox
  a naive install would schedule units the host user manager never
  loads (#468) — the install REFUSES there by default; ``--force`` opts
  into the box-aware host-home install (one ExecStart line per box).
* ``vesma update components`` — the component inventory (pip dist,
  integration pack, cortex bundle, embedder, npm package, timer,
  prod-venvs, Go binaries) with each piece's update path. No network.

Deprecated flag forms (``--check``, ``--yes``/``-y``, ``--to``,
``--scope``, ``--install-timer``, ``--uninstall-timer``) still work on
the plain form — hidden aliases with identical behavior and a one-line
stderr hint pointing at the subcommand; the shipped systemd unit's
``ExecStart`` (``vesma update --yes --scope=user``) depends on them.

The systemd unit files ship as ``contrib/vesma-update.{service,timer}``
AND as the ``_SERVICE_TEMPLATE`` / ``_TIMER_TEMPLATE`` constants below —
the constants are the install source (a pip install has no ``contrib/``
directory); a drift test pins the repo files byte-identical to them.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from vesmaro import __version__
from vesmaro.updates import (
    CANDIDATE_DISTS,
    HISTORY_FILENAME,
    UpdateInfo,
    check_for_update,
    detect_installed_dist,
    family_latest,
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
# Install with `vesma update timer install`, remove with
# `vesma update timer uninstall` (deprecated flag forms
# --install-timer/--uninstall-timer still work). The installer writes
# this file with the ExecStart chosen for this machine (%h launcher when
# present, /usr/bin/env fallback otherwise).
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


def _family_note(
    installed_as: str | None,
    installed: str | None,
    info: UpdateInfo | None,
    family: str | None,
) -> str:
    """The pip family row's Note cell (one row for all aliases, #W-C).

    ``family`` is the family max over the aliases' published versions;
    ``info`` is the per-dist answer for the installed alias. The note
    says UPDATE AVAILABLE whenever ANY known latest beats the installed
    version — the wording stays actionable by ``vesma update apply``,
    which re-fetches and reports per-dist honestly when the aliases
    diverge.
    """
    if installed is None or installed_as is None:
        return "not installed — pip install --user vesma"
    candidates = [v for v in ((info.latest if info is not None else None), family) if v]
    if not candidates:
        return "latest unknown (offline or check disabled)"
    latest = max(candidates, key=version_key)
    if info is not None and info.update_available:
        return "UPDATE AVAILABLE — run 'vesma update apply'"
    if version_key(latest) > version_key(installed):
        return "UPDATE AVAILABLE — run 'vesma update apply'"
    if version_key(installed) > version_key(latest):
        # installed > every known latest even after a fresh check (#460):
        # the honest wording for a local build or an unpublished release.
        return "newer than published latest (local build?)"
    if installed_as != CANDIDATE_DISTS[-1]:
        # The canonical PyPI name is the LAST candidate (`vesma`); an
        # install under an earlier alias is the same codebase, other name.
        return f"up to date (installed as {installed_as} — same codebase, alias package)"
    return "up to date"


def _print_check(console: Console) -> UpdateInfo | None:
    """Print the surfaces table; return the pip update info it was built from."""
    detected = detect_installed_dist()
    info = check_for_update()
    family = family_latest()

    table = Table(
        title="Vesma update surfaces (this machine)",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Surface")
    table.add_column("Installed")
    table.add_column("Latest")
    table.add_column("Note")

    # ONE row for the pip alias family: `vesma-memory-server` and `vesma`
    # are canonical PyPI names of the same codebase — listing each alias
    # separately showed the working binary's name as "not installed".
    # Latest = the family max (an alias lagging behind must not read as
    # "up to date" for the codebase).
    table.add_row(
        "pip: vesma (family)",
        detected[1] if detected is not None else "-",
        family or "?",
        _family_note(
            detected[0] if detected is not None else None,
            detected[1] if detected is not None else None,
            info,
            family,
        ),
    )

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
        "[dim]`vesma update apply` changes only the pip user-site (and the npm "
        "package); prod venvs and Go binaries are never touched. pip Latest = the "
        "family max over the alias dists (vesma-memory-server / vesma — same "
        "codebase; mnemos-memory-server is the deprecated legacy mirror).[/dim]"
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

#: Env variables set inside distrobox/docker containers (no codebase
#: surface used them before #W-C — grep-verified; the pre-existing
#: detection relied on the HOME ``/.distrobox`` layout and the marker
#: file only). Module-level seam for tests.
_CONTAINER_ENV_VARS = ("DISTROBOX_ENTER_ENV", "CONTAINER_ID")


def _container_env_detected() -> bool:
    """True when a container env variable or the podman marker says "box"."""
    if any(os.environ.get(var) for var in _CONTAINER_ENV_VARS):
        return True
    try:
        return _CONTAINERENV.exists()
    except OSError:  # pragma: no cover — unreadable /, treat as not a box
        return False


def _distrobox_context() -> tuple[bool, str | None, Path | None]:
    """Classify the distrobox context: ``(in_container, box, host_home)``.

    Detection: the resolved HOME carries the ``/.distrobox/<box>/...``
    layout (box name and host home are derivable), or a container signal
    fires — the ``DISTROBOX_ENTER_ENV``/``CONTAINER_ID`` env variables or
    the ``/run/.containerenv`` marker file (in a container, but the host
    home cannot be derived). The host home is the path prefix before the
    ``.distrobox`` component of the resolved HOME — computed from the
    path itself, no subprocess probing (#468).
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
    if _container_env_detected():
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


def _install_timer(console: Console, *, force: bool = False) -> None:
    """Install the weekly timer — on the host, or (with ``--force``) from a box.

    Issue #468: from inside a distrobox the box's ``systemctl --user``
    talks to the BOX session, so an install here would schedule units the
    HOST user manager never loads (dead units). The default inside a box
    is a loud refusal naming the host-side path; ``--force`` opts into
    the intentional box-aware install (units into the HOST home, one
    ExecStart line per box).
    """
    in_container, box, host_home = _distrobox_context()
    if in_container and (box is None or host_home is None):
        # A container whose host home cannot be derived: even --force
        # cannot install anything the host manager would load — the
        # exact manual recipe is the only honest answer (with or
        # without --force).
        prefix = "--force given, but " if force else ""
        console.print(
            f"[yellow]⚠[/yellow] {prefix}running in a container, but the host home cannot be "
            "derived from HOME — installing here would schedule units the host "
            "manager never loads (dead units, issue #468). On the HOST, copy "
            "contrib/vesma-update.{service,timer} into ~/.config/systemd/user/ and "
            "add one ExecStart line per box:"
        )
        console.print(
            "  ExecStart=<host-home>/.local/bin/distrobox-enter -n <box> -- /bin/sh -c "
            "'exec \"$HOME/.local/bin/vesma\" update --yes --scope=user'"
        )
        raise typer.Exit(1)
    if in_container:
        if not force:
            where = f"box '{box}'" if box else "a container"
            console.print(
                f"[red]✗[/red] running inside {where} — installing the timer here would "
                "schedule units the host user manager never loads (dead units, issue #468). "
                "Run `vesma update timer install` on the HOST instead (it wires one "
                "ExecStart line per box into the host units — see the upgrade runbook), "
                "or re-run here with --force if you intend to install from this box."
            )
            raise typer.Exit(1)
        _install_timer_in_box(console, box, host_home)  # type: ignore[arg-type]
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


# ── timer status (`vesma update timer status`, #W-C) ─────────────────────────


def _systemctl_property(unit: str, prop: str) -> str | None:
    """One ``systemctl --user show`` property value, or ``None``."""
    proc = _run_cmd(["systemctl", "--user", "show", unit, f"--property={prop}"], timeout=60)
    if proc is None or proc.returncode != 0:
        return None
    prefix = f"{prop}="
    for line in proc.stdout.splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip() or None
    return None


def _timer_state(unit_dir: Path, is_enabled: Callable[[], int | None]) -> str:
    """Human state for one unit dir + one manager: enabled / installed / not."""
    enabled_rc = is_enabled()
    files = (unit_dir / TIMER_UNIT).exists() or (unit_dir / SERVICE_UNIT).exists()
    if enabled_rc == 0:
        return "enabled"
    if files:
        return "installed (disabled)" if enabled_rc is not None else "installed (state unknown)"
    return "not installed"


def _timer_status() -> dict[str, object]:
    """The timer state: presence, enabled state, last trigger, box context.

    Local user manager first; inside a derivable distrobox the HOST
    units are reported too (the only place a loadable timer can live,
    #468). Every leg is best-effort — an absent systemctl degrades the
    cell, never the command.
    """
    in_container, box, host_home = _distrobox_context()
    unit_dir = _unit_dir()
    status: dict[str, object] = {
        "unit": TIMER_UNIT,
        "state": _timer_state(unit_dir, lambda: _systemctl("is-enabled", TIMER_UNIT)),
        "last_trigger": _systemctl_property(TIMER_UNIT, "LastTriggerUSec") or "never",
        "container": in_container,
        "box": box,
    }
    if in_container and host_home is not None:
        host_dir = host_home / ".config" / "systemd" / "user"
        status["host_state"] = _timer_state(
            host_dir, lambda: _systemctl_host("is-enabled", TIMER_UNIT)
        )
    return status


# ── component inventory (`vesma update components`, #W-C) ─────────────────────

#: The bundled cortex manifest (importlib.resources pattern, mirrors
#: ``decision_provider.CORTEX_ARTIFACT_DIR`` without importing numpy).
_CORTEX_MANIFEST_RESOURCE = "models/vesma-cortex-v1/manifest.json"


def _cortex_manifest() -> dict[str, Any]:
    """The shipped vesma-cortex-v1 manifest as a dict (raises on corruption)."""
    raw = resource_files("vesmaro").joinpath(_CORTEX_MANIFEST_RESOURCE).read_text(encoding="utf-8")
    data: object = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"cortex manifest is not an object: {_CORTEX_MANIFEST_RESOURCE}")
    return data


def _pip_component() -> tuple[str, str]:
    detected = detect_installed_dist()
    if detected is None:
        return "-", "`pip install --user vesma`, then `vesma update apply`"
    return f"{detected[0]} {detected[1]}", "`vesma update apply`"


def _integration_component() -> tuple[str, str]:
    """Integration pack version + aggregate stale/missing state.

    Reuses the doctor's helpers (``IntegrationManager.verify`` across
    ``load_targets().detected()``) — no logic duplicated, no network.
    """
    try:
        from vesmaro.cli.integration import IntegrationManager, load_targets

        mgr = IntegrationManager(version=__version__)
        detected = load_targets().detected()
    except Exception:  # doctor reports, doesn't crash — same here
        return "-", "`vesma integration setup`"
    if not detected:
        return "-", "`vesma integration setup`"
    stale = 0
    missing = 0
    for target in detected:
        try:
            result = mgr.verify(target.name)
            stale += result.stale_count
            missing += result.missing_count
        except Exception:  # one target failing must not sink the row
            missing += 1
    state = "ok" if not (stale or missing) else f"{stale} stale, {missing} missing"
    path = "`vesma integration setup`" if missing else "`vesma integration update`"
    return f"pack v{__version__}, targets: {', '.join(t.name for t in detected)} — {state}", path


def _cortex_component() -> tuple[str, str]:
    """Cortex bundle identity from the shipped manifest (report-only)."""
    try:
        manifest = _cortex_manifest()
    except Exception:
        return "-", "ships with the wheel"
    name = str(manifest.get("name") or "vesma-cortex-v1")
    rev = str(manifest.get("weights_sha256") or "")[:12]
    trained = str(manifest.get("trained_at") or "")[:10]
    return f"{name} (rev {rev}, trained {trained})", "ships with the wheel"


def _stored_vector_fingerprint(settings: Any) -> str | None:
    """One stored ``model_fingerprint`` from vectors.db (cheap read-only leg)."""
    try:
        import sqlite3

        vectors = Path(str(settings.mnemos.data_dir)).expanduser() / "vectors.db"
        if not vectors.exists():
            return None
        conn = sqlite3.connect(f"file:{vectors}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT metadata FROM embeddings LIMIT 1").fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return None
        meta: object = json.loads(row[0])
        fp = meta.get("model_fingerprint") if isinstance(meta, dict) else None
        return fp if isinstance(fp, str) else None
    except Exception:
        return None


def _embedder_component() -> tuple[str, str]:
    """Embedder model id + fingerprint; vector-store vintage when readable."""
    try:
        from vesmaro.config import load_settings
        from vesmaro.embeddings import config_fingerprint

        settings = load_settings()
        cfg = settings.embedding
        fp = config_fingerprint(cfg)
    except Exception:
        return "-", "`vesma reindex` after a model switch"
    cell = f"{cfg.provider}:{cfg.model} ({fp[:26]})"
    stored = _stored_vector_fingerprint(settings)
    if stored is not None and stored != fp:
        return f"{cell} — VINTAGE MISMATCH", "`vesma reindex`"
    return cell, "`vesma reindex` after a model switch"


def _npm_component() -> tuple[str, str]:
    npm = shutil.which(NPM_BIN)
    if npm is None:
        return "-", "`vesma update apply` (npm not found — report only)"
    version = _npm_global_version(npm)
    return version or "not installed", "`vesma update apply` (npm leg, best-effort)"


def _timer_component() -> tuple[str, str]:
    status = _timer_status()
    cell = str(status["state"])
    if status.get("container"):
        box = status.get("box")
        cell += f" — in distrobox '{box}'" if box else " — in a container"
    return cell, "`vesma update timer install`"


def _prod_venv_component() -> tuple[str, str]:
    venvs = _prod_venv_paths()
    if not venvs:
        return "-", "MANUAL GATE — upgrade runbook (none found)"
    return ", ".join(v.name for v in venvs), "MANUAL GATE — upgrade runbook"


def _go_component() -> tuple[str, str]:
    bins = _go_binaries()
    if not bins:
        return "-", "goreleaser releases (report only)"
    return ", ".join(bins), "goreleaser releases (report only)"


def _component_rows() -> list[tuple[str, str, str]]:
    """Inventory rows ``(component, installed, update_path)`` — no network."""
    return [
        ("pip dist", *_pip_component()),
        ("integration pack", *_integration_component()),
        ("cortex bundle", *_cortex_component()),
        ("embedder", *_embedder_component()),
        (f"npm package {NPM_PACKAGE}", *_npm_component()),
        (f"update timer ({TIMER_UNIT})", *_timer_component()),
        ("prod venvs", *_prod_venv_component()),
        ("go binaries", *_go_component()),
    ]


# ── the command ──────────────────────────────────────────────────────────────


def _deprecated_flag_hint(old_form: str, new_form: str) -> None:
    """One-line deprecation hint for a hidden legacy flag form (stderr).

    Standing design rule: flags do not replace subcommands. Legacy flag
    forms keep working (the shipped systemd unit depends on them) but
    point at the canonical subcommand spelling. stderr only — stdout
    stays clean for pipes and JSON consumers.
    """
    typer.echo(f"[deprecated] `{old_form}` is deprecated — use: {new_form}", err=True)


update_app = typer.Typer(
    help="Check for updates / update the user-site install (issues #445, #460).\n\n"
    "Subcommands: `check` (report only), `apply` (the update path), `timer "
    "install|uninstall|status` (the weekly systemd timer), `components` (the "
    "component inventory). Plain `vesma update` keeps the 5.2.0 behavior: report "
    "+ interactive apply prompt in a TTY. The old flag forms (--check, --yes/-y, "
    "--to, --scope, --install-timer, --uninstall-timer) still work as hidden "
    "deprecated aliases with a stderr hint (scripts and the shipped systemd unit "
    "depend on them).",
    no_args_is_help=False,
)


@update_app.callback(invoke_without_command=True)
def update(
    ctx: typer.Context,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Deprecated flag form — use: `vesma update check`.",
            hidden=True,
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Deprecated flag form — use: `vesma update apply`.",
            hidden=True,
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
        str | None,
        typer.Option(
            "--scope",
            help="Deprecated flag form — use: `vesma update apply --scope user`.",
            hidden=True,
        ),
    ] = None,
    to: Annotated[
        str | None,
        typer.Option(
            "--to",
            help="Deprecated flag form — use: `vesma update apply --to VERSION`.",
            hidden=True,
        ),
    ] = None,
    install_timer: Annotated[
        bool,
        typer.Option(
            "--install-timer",
            help="Deprecated flag form — use: `vesma update timer install`.",
            hidden=True,
        ),
    ] = False,
    uninstall_timer: Annotated[
        bool,
        typer.Option(
            "--uninstall-timer",
            help="Deprecated flag form — use: `vesma update timer uninstall`.",
            hidden=True,
        ),
    ] = False,
) -> None:
    """Check for updates / update the user-site install (issues #445, #460).

    Plain `vesma update`: the surfaces report, then — in an interactive
    terminal, when a pip update is pending — a confirmation prompt before
    applying (pipes/CI stay check-only and print `apply with: vesma
    update apply`).

    Subcommands: `check` (report only), `apply` (the update path),
    `timer install|uninstall|status` (the weekly systemd timer),
    `components` (the component inventory).

    Deprecated flag forms (--check, --yes/-y, --to, --scope,
    --install-timer, --uninstall-timer) still work — hidden aliases with
    identical behavior and a stderr hint; scripts (incl. the shipped
    systemd unit) keep running.
    """
    console = Console()
    if ctx.invoked_subcommand is not None:
        # The subcommand runs its own logic; options placed BEFORE the
        # subcommand word would be silently dropped otherwise.
        deprecated_used = (
            check
            or yes
            or verbose
            or scope is not None
            or to is not None
            or install_timer
            or uninstall_timer
        )
        if deprecated_used:
            typer.echo(
                "note: options placed before the subcommand are ignored — "
                "pass them after it (e.g. `vesma update apply --yes`)",
                err=True,
            )
        return
    if install_timer and uninstall_timer:
        console.print("[red]✗[/red] --install-timer and --uninstall-timer are mutually exclusive")
        raise typer.Exit(1)
    if install_timer:
        _deprecated_flag_hint("vesma update --install-timer", "vesma update timer install")
        _install_timer(console)
        return
    if uninstall_timer:
        _deprecated_flag_hint("vesma update --uninstall-timer", "vesma update timer uninstall")
        _uninstall_timer(console)
        return
    if scope is not None and scope != "user":
        console.print(
            "[red]✗[/red] only --scope=user is supported: prod venvs, Go binaries "
            "and containers are manual surfaces by design"
        )
        raise typer.Exit(1)
    if yes:
        _deprecated_flag_hint("vesma update --yes", "vesma update apply")
        _run_user_update(console, to, verbose=verbose)
        return
    # Hints precede the report: the first line a human sees names the
    # canonical spelling; stdout itself stays hint-free for pipes/JSON.
    if to is not None:
        _deprecated_flag_hint("vesma update --to VERSION", "vesma update apply --to VERSION")
    if scope is not None:
        _deprecated_flag_hint("vesma update --scope user", "vesma update apply --scope user")
    if check:
        _deprecated_flag_hint("vesma update --check", "vesma update check")
    info = _print_check(console)
    if check:
        return
    if info is None or not info.update_available:
        return  # nothing pending — the report stands as-is
    if _stdin_is_tty():
        if typer.confirm("Apply update?", default=False):
            _run_user_update(console, to, verbose=verbose)
        return
    console.print("[dim]apply with: vesma update apply[/dim]")


@update_app.command(name="check")
def update_check() -> None:
    """Report every update surface without changing anything.

    Never prompts, never applies — identical to the deprecated
    `vesma update --check`, safe in pipes and CI.
    """
    _print_check(Console())


@update_app.command(name="apply")
def update_apply(
    to: Annotated[
        str | None,
        typer.Option("--to", help="Pin the pip target version (rollback path), e.g. --to 5.1.1."),
    ] = None,
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Update scope. Only 'user' exists: prod venvs, Go binaries and "
            "containers are NEVER auto-updated.",
        ),
    ] = "user",
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="No-op: `apply` already is the explicit, prompt-free path "
            "(accepted for muscle memory and scripted migrations).",
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
) -> None:
    """Apply the update now: pip --user upgrade (+ npm best-effort).

    The apply path behind the deprecated `vesma update --yes`. Never
    prompts — invoking `apply` IS the confirmation; `-y/--yes` is
    accepted but changes nothing. `--to VERSION` pins a (rollback)
    version. Every run appends a record to the update history.
    """
    console = Console()
    if scope != "user":
        console.print(
            "[red]✗[/red] only --scope=user is supported: prod venvs, Go binaries "
            "and containers are manual surfaces by design"
        )
        raise typer.Exit(1)
    _run_user_update(console, to, verbose=verbose)


# ── timer subcommands (`vesma update timer ...`) ──────────────────────────────


timer_app = typer.Typer(help="Install, remove or inspect the weekly update timer.")


@timer_app.command(name="install")
def timer_install(
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Inside a distrobox/container: install anyway (host-home units, "
            "one ExecStart line per box). Without it a box install is refused "
            "— the units would be dead (issue #468).",
        ),
    ] = False,
) -> None:
    """Install and enable the weekly systemd user update timer.

    On the host: units into ~/.config/systemd/user, then enable --now.
    Inside a distrobox this REFUSES by default (dead units, #468) — run
    it on the host, or pass --force for the intentional box-aware
    install into the HOST home.
    """
    _install_timer(Console(), force=force)


@timer_app.command(name="uninstall")
def timer_uninstall() -> None:
    """Disable and remove the weekly timer and its service unit.

    Symmetric to `timer install`: on the host removes the local units;
    from a distrobox removes THIS box's ExecStart line from the HOST
    units (the units survive while other boxes remain scheduled).
    """
    _uninstall_timer(Console())


@timer_app.command(name="status")
def timer_status(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the timer state as JSON (for scripting / CI)."),
    ] = False,
) -> None:
    """Show timer presence, enabled state and last trigger.

    Reports the local systemd user manager; inside a distrobox also the
    HOST units (a loadable timer can only live there, #468) and a note
    that this machine appears to be a box.
    """
    status = _timer_status()
    console = Console()
    if json_output:
        console.print_json(json.dumps(status))
        return
    table = Table(title=f"Update timer ({TIMER_UNIT})", show_header=True, header_style="bold")
    table.add_column("Field")
    table.add_column("Value")
    for key, value in status.items():
        table.add_row(key, str(value))
    console.print(table)


update_app.add_typer(timer_app, name="timer")


# ── components subcommand (`vesma update components`, #W-C) ───────────────────


@update_app.command(name="components")
def update_components(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the inventory as JSON (for scripting / CI)."),
    ] = False,
) -> None:
    """Show the component inventory: what is installed and how it updates.

    Local state only — no network, no PyPI, no registry calls. Honest
    "-" where a component is absent or its version is not cheaply
    readable. Covers: pip dist, integration pack, cortex bundle,
    embedder, the npm package, the update timer, prod venvs (manual
    gate) and the Go binaries (report only).
    """
    rows = _component_rows()
    console = Console()
    if json_output:
        console.print_json(
            json.dumps(
                {
                    "components": [
                        {"component": c, "installed": i, "update_path": p} for c, i, p in rows
                    ]
                }
            )
        )
        return
    table = Table(title="Vesma components (this machine)", show_header=True, header_style="bold")
    table.add_column("Component")
    table.add_column("Installed")
    table.add_column("Update path")
    for component, installed, path in rows:
        table.add_row(component, installed, path)
    console.print(table)


if __name__ == "__main__":  # pragma: no cover — manual invocation
    update_app()
