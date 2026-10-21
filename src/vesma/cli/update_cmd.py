"""``vesma self-update`` CLI — the APPLICATION self-update family (#445).

Wave 61 component UX: the top-level ``vesma update`` name now belongs to
the COMPONENT manifest verb (``vesma.cli.components``); THIS family moved
to ``vesma self-update`` — same subcommands, same behavior, new name.

Subcommands (standing design rule: flags do not replace subcommands):

* ``vesma self-update`` — report every update surface found on THIS
  machine (installed vs latest pip dist, the global npm package, the
  host prod-venvs, the Go binaries) and — in an interactive terminal,
  when a pip update is pending — ask ``Apply update? [y/N]`` and apply
  on yes (issue #460). Non-TTY contexts (pipes/CI) stay check-only and
  print ``apply with: vesma self-update apply``.
* ``vesma self-update check`` — the report only: never prompts, never
  applies (safe in pipes and CI).
* ``vesma self-update apply [--to VERSION] [--scope user] [-y]
  [--verbose]`` — the apply path. Install-shape aware (wave 61
  update-apply fix, owner directive 2026-10-18): an OWNED venv (the
  ``.vesma-venv`` ownership marker, or the canonical engine venv from the
  layout contract) is upgraded IN PLACE via
  ``<venv-python> -m pip install --upgrade`` — never ``--user``, which
  fails inside any venv ("User site-packages are not visible in this
  virtualenv", the 6.2.0 field incident); a bare user-site install keeps
  the classic ``pip install --user`` path; a FOREIGN venv (no marker, not
  canonical — e.g. another product's venv) and a pipx / ``uv tool``
  manager environment are refused LOUDLY with exit 1 — self-update never
  writes into an environment vesma does not own. The global npm package
  updates best-effort, and every apply appends a record to
  ``~/.local/share/vesma/update-history.json`` (refusals record nothing).
  Pip output is captured —
  one summary line per surface; ``--verbose`` prints the full pip output
  (and it is shown as a tail automatically on failure). ``--to <version>``
  pins a specific (rollback) version.
* ``vesma self-update timer install|uninstall|status`` — manage or
  inspect the weekly systemd USER timer (``vesma-update.timer``). Inside
  a distrobox a naive install would schedule units the host user manager
  never loads (#468) — the install REFUSES there by default; ``--force``
  opts into the box-aware host-home install (one ExecStart line per box).
* ``vesma self-update components`` — the component inventory (pip dist,
  integration pack, cortex bundle, embedder, npm package, timer,
  prod-venvs, Go binaries) with each piece's update path. No network.

Deprecated flag forms (``--check``, ``--yes``/``-y``, ``--to``,
``--scope``, ``--install-timer``, ``--uninstall-timer``) still work on
the plain form — hidden aliases with identical behavior and a one-line
stderr hint pointing at the subcommand. The same forms remain hidden
aliases on the COMPONENT ``vesma update`` verb (delegating HERE) — units
installed before the rename keep running (``vesma update --yes
--scope=user`` in a shipped ExecStart applies the self-update).

The systemd unit files ship as ``contrib/vesma-update.{service,timer}``
AND as the ``_SERVICE_TEMPLATE`` / ``_TIMER_TEMPLATE`` constants below —
the constants are the install source (a pip install has no ``contrib/``
directory); a drift test pins the repo files byte-identical to them.
"""

from __future__ import annotations

import dataclasses
import getpass
import json
import logging
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv systemctl/pip flows, no shell
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from vesma import __version__
from vesma.updates import (
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

# Known host locations of the production venvs (design machine-map). The
# literal path covers distrobox contexts where Path.home() points at the
# box home while the prod venvs live under the HOST home. A machine with
# a different layout overrides the base list via VESMA_PROD_VENV_BASES
# (os.pathsep-separated entries, ``~`` expanded); unset/empty keeps the
# defaults below.
_PROD_VENV_GLOB = "mnemos-prod/venv*"


def _prod_venv_bases() -> tuple[Path, ...]:
    """Prod-venv base dirs to scan; machine-portable via VESMA_PROD_VENV_BASES."""
    if bases := os.environ.get("VESMA_PROD_VENV_BASES", "").strip():
        return tuple(
            Path(part.strip()).expanduser() for part in bases.split(os.pathsep) if part.strip()
        )
    return (
        Path.home() / ".local" / "share",
        Path("/var/home/abyss/.local/share"),
    )


_GO_BIN_NAMES = ("vesmaro-agent", "vesma-agent", "mnemos-mesh", "vesma-mesh")

_DEFAULT_EXEC_START = "%h/.local/bin/vesma self-update --yes --scope=user"
_FALLBACK_EXEC_START = "/usr/bin/env vesma self-update --yes --scope=user"

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
# Install with `vesma self-update timer install`, remove with
# `vesma self-update timer uninstall`. The installer writes
# this file with the ExecStart chosen for this machine (%h launcher when
# present, /usr/bin/env fallback otherwise).
#
# Distrobox hosts: a systemd USER timer on the HOST cannot reach the
# boxes' pip user-sites. Adapt this unit the way the prod units do —
# one ExecStart line per box, e.g.:
#   ExecStart=distrobox-enter ubuntu-box -- vesma self-update --yes --scope=user
#   ExecStart=distrobox-enter vscode-box -- vesma self-update --yes --scope=user
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
    for base in dict.fromkeys(_prod_venv_bases()):
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
        return subprocess.run(  # nosec B603 - internal systemctl/pip argv, no shell
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
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
    monkeypatch this to simulate an interactive ``vesma self-update``.
    """
    try:
        return bool(sys.stdin.isatty())
    except Exception:  # pragma: no cover — exotic stdin must never crash the CLI
        return False


def _append_history(*, dist: str, from_version: str, to_version: str, rc: int) -> None:
    """Append one record to ``~/.local/share/vesma/update-history.json``."""
    from vesma.fs_hardening import ensure_private_dir, harden_file

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
        # Cascade F3 (2026-10-18): 0700 dir chain + 0600 file — the update
        # history names environments and versions and must not land 0755/0644
        # (mirrors the update-check cache write in updates.py).
        ensure_private_dir(path.parent)
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        harden_file(path)
    except Exception:
        logger.debug("update-history append failed", exc_info=True)


# ── legacy-home detection (home-flip, owner directive 2026-10-08) ────────────


def _legacy_home_hint() -> str | None:
    """One-line migration hint when a legacy ``~/.mnemos`` home has content.

    Best-effort by contract: ANY failure to inspect the home degrades to
    ``None`` (no hint line) — the update surfaces must never crash over a
    diagnostics nicety. The hint text is the SAME one the fork-refusal gate
    and the legacy-config diagnostics print (single UX contract).
    """
    try:
        from vesma.config import MIGRATE_HINT, legacy_home_is_substantial

        if legacy_home_is_substantial():
            return f"legacy store home ~/.mnemos detected — migrate with: {MIGRATE_HINT}"
    except Exception:  # diagnostics must never break the report
        logger.debug("legacy-home detection failed", exc_info=True)
    return None


def _print_legacy_home_hint(console: Console) -> None:
    """Print the one-line migration hint when a legacy home is detected."""
    hint = _legacy_home_hint()
    if hint is not None:
        console.print(f"[yellow]⚠[/yellow] {hint}")


# ── install-shape detection (wave 61 update-apply fix, 2026-10-18) ───────────

#: The venv-ownership flag file the canonical installer writes into the
#: venv root (`installer=... version=... date=...` inside). Its presence
#: is the PRIMARY ownership proof for ``self-update apply``.
_OWNED_VENV_MARKER = ".vesma-venv"

#: The user-site stamp the ``--no-venv`` installer writes (a user-site
#: has no venv root to carry ``.vesma-venv``, so the stamp lives in the
#: vesma data root). WRITE-ONLY audit trail: the shape detection
#: classifies user-site installs unconditionally and never reads it.
_USER_SITE_MARKER = ".user-site-install"

#: Shape kinds — the complete partition of how the RUNNING vesma was
#: installed (``InstallShape.kind``).
OWNED_VENV = "owned-venv"  # own marked/canonical venv — upgrade in place
FOREIGN_VENV = "foreign-venv"  # someone else's venv — loud refusal
TOOL_MANAGER = "tool-manager"  # pipx / uv tool — refuse, name the manager
USER_SITE = "user-site"  # bare user-site install — classic pip --user


@dataclasses.dataclass(frozen=True)
class InstallShape:
    """How THIS vesma process is installed (the apply-shape matrix).

    ``venv_root`` is ``sys.prefix`` for the three venv shapes, ``None``
    for a user-site install. ``marker_path`` is where the ownership
    marker lives (or is missing — the refusal messages cite it); for a
    user-site shape it is the data-root flag file.
    """

    kind: str
    venv_root: Path | None = None
    marker_path: Path | None = None


def _in_venv() -> bool:
    """True when the running interpreter lives inside a virtualenv."""
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _looks_like_tool_manager() -> bool:
    """True when the interpreter lives in a pipx / ``uv tool`` venv.

    Path layout is the signal: pipx venvs carry a ``/pipx/`` segment
    (``~/.local/pipx/venvs/<name>``), uv tools a ``/uv/tools/`` segment
    (``~/.local/share/uv/tools/<name>``). Checked against BOTH
    ``sys.prefix`` and ``sys.executable`` (a wrapper on ``PATH`` may sit
    outside the venv). Deliberate contract deviation: ``pyvenv.cfg``
    carries no stable manager marker on either tool, so the file-hint
    leg of the design contract is not implementable without a fragile
    parser — the path check covers the real layouts on every supported
    platform.
    """
    for candidate in (sys.prefix, sys.executable):
        text = str(candidate)
        if "/pipx/" in text or "/uv/tools/" in text:
            return True
    return False


def _canonical_engine_venv() -> Path:
    """The layout-contract engine venv of the detected generation (lazy import)."""
    from vesma.service.layout import engine_venv_dir  # lazy: light import, CLI startup cost

    return engine_venv_dir()


def _is_canonical_engine_venv(prefix: Path) -> bool:
    """True when ``prefix`` IS a canonical engine venv from the layout contract.

    Ownership is generation-INDEPENDENT (layout v2 wave, ADR-0045): the
    check accepts BOTH canonical slots — the generation's contract slot
    AND the legacy v1.5 slot (``~/.local/share/vesma/venv``). A venv
    installed at the legacy slot predates the generation question; the
    canonical-path fallback must keep it owned (the 6.2.0 field case: a
    service-installed engine venv predates the ``.vesma-venv`` marker —
    the canonical path is the ownership proof there).
    """
    from vesma.service.layout import legacy_engine_venv_dir  # lazy, same reason

    for slot in (_canonical_engine_venv(), legacy_engine_venv_dir()):
        try:
            if prefix.resolve() == slot.resolve():
                return True
        except OSError:  # pragma: no cover — an unresolvable slot is not canonical
            continue
    return False


def _user_site_marker_path() -> Path:
    """``<data_root>/.user-site-install`` (XDG-honored; the generation's
    data root — WRITE-ONLY audit stamp, never read back)."""
    from vesma.service.layout import data_root  # lazy, same reason as above

    return data_root() / _USER_SITE_MARKER


def _install_shape() -> InstallShape:
    """Classify the running install (the shape matrix of the apply fix).

    Order is load-bearing:

    1. NOT in a venv → :data:`USER_SITE` (the classic pip ``--user`` case).
    2. Tool-manager path layout → :data:`TOOL_MANAGER`. Priority over the
       ownership checks: even a stray marker inside a managed venv must
       never authorize self-writes into a pipx/uv-tool environment.
    3. ``.vesma-venv`` marker present OR the canonical engine venv path →
       :data:`OWNED_VENV`.
    4. Everything else → :data:`FOREIGN_VENV` (apply refuses).
    """
    prefix = Path(sys.prefix)
    if not _in_venv():
        return InstallShape(USER_SITE, None, _user_site_marker_path())
    marker = prefix / _OWNED_VENV_MARKER
    if _looks_like_tool_manager():
        return InstallShape(TOOL_MANAGER, prefix, marker)
    if marker.exists() or _is_canonical_engine_venv(prefix):
        return InstallShape(OWNED_VENV, prefix, marker)
    return InstallShape(FOREIGN_VENV, prefix, marker)


def _refuse_apply(console: Console, shape: InstallShape) -> None:
    """Loud refusal for an environment vesma does not own (no writes).

    Refusals happen BEFORE any subprocess or history append: a refused
    apply must leave zero traces. ``typer.Exit(1)`` — the operator picks
    the named remediation.
    """
    if shape.kind == TOOL_MANAGER:
        console.print(
            "[red]✗[/red] this vesma is managed by an external tool manager (pipx / "
            "uv tool) — self-update never writes into a manager-owned venv. Update "
            "with the manager instead:"
        )
        console.print("  pipx upgrade vesma")
        console.print("  uv tool upgrade vesma")
        raise typer.Exit(1)
    if shape.kind == FOREIGN_VENV:
        console.print(
            "[red]✗[/red] vesma in this environment was not installed by vesma "
            f"(no ownership marker at {shape.marker_path}) — refusing to upgrade a "
            "foreign environment; install via the canonical installer and upgrade "
            "from there"
        )
        if shape.venv_root is not None and _is_legacy_installer_venv(shape.venv_root):
            console.print(
                "[yellow]⚠[/yellow] this looks like the pre-6.2 install.sh default "
                "(~/.mnemos/venv) — re-run scripts/install.sh once: it installs into "
                "the canonical isolated venv (~/.local/share/vesma/venv) and future "
                "upgrades apply in place."
            )
        raise typer.Exit(1)


def _is_legacy_installer_venv(venv_root: Path) -> bool:
    """True for the pre-6.2 install.sh default ``~/.mnemos/venv`` (best-effort)."""
    try:
        return venv_root.resolve() == (Path.home() / ".mnemos" / "venv").resolve()
    except OSError:  # pragma: no cover — unresolvable paths carry no hint
        return False


def _refresh_owned_marker(shape: InstallShape, version: str) -> None:
    """Touch/refresh the ``.vesma-venv`` marker after a successful in-place upgrade.

    Creates the marker when missing — canonical-path-owned installs
    bootstrap it on their first successful upgrade. Best-effort: a
    marker write failure downgrades to a debug log, never to a failed
    apply (the upgrade itself succeeded).
    """
    if shape.kind != OWNED_VENV or shape.venv_root is None:
        return
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        (shape.venv_root / _OWNED_VENV_MARKER).write_text(
            f"installer=self-update version={version} date={stamp}\n", encoding="utf-8"
        )
    except OSError:
        logger.debug("ownership marker refresh failed", exc_info=True)


def _shape_note(kind: str) -> str:
    """The check-report pip row note for a shape apply would refuse."""
    if kind == TOOL_MANAGER:
        return (
            "managed by pipx/uv tool — update with `pipx upgrade vesma` / `uv tool upgrade vesma`"
        )
    if kind == FOREIGN_VENV:
        return (
            "foreign environment — apply refuses (no ownership marker); "
            "reinstall via the canonical installer"
        )
    return ""  # pragma: no cover — only called for the refusing shapes


# ── legacy-dist cleanup (field incident 2026-10-18) ──────────────────────────

#: Pre-rebrand dist names (the same codebase under old PyPI slots)
#: removed from THIS environment after a successful apply. The uninstall
#: runs ONLY AFTER the new vesma is installed — order discipline: a
#: legacy uninstall removes the SHARED console script per that dist's
#: own RECORD (both dists claim ``bin/vesma``), so with no new dist in
#: place the CLI entry would die. Verified live on prod 6.2.0.
_LEGACY_DISTS = ("vesma-memory-server", "mnemos-memory-server")

#: Console scripts the ``vesma`` dist claims (pyproject [project.scripts]);
#: self-checked after a legacy removal (the shared-script collision class).
_OWNED_SCRIPTS = ("vesma", "vesma-train")


def _installed_legacy_dists() -> list[tuple[str, str]]:
    """Legacy dist names visible to THIS interpreter, as ``(name, version)``.

    The running interpreter's metadata IS the target environment for
    both applyable shapes (owned-venv: the process runs in that venv;
    user-site: user-site packages are on sys.path) — a valid pre-filter
    for the cleanup leg.
    """
    import importlib.metadata as md

    found: list[tuple[str, str]] = []
    for legacy in _LEGACY_DISTS:
        try:
            found.append((legacy, md.version(legacy)))
        except Exception:
            logger.debug("legacy dist probe: %s unavailable", legacy)
            continue
    return found


def _legacy_cleanup_cmd(legacy: str) -> list[str]:
    """The uninstall command for one legacy dist (own python, NO --user).

    ``pip uninstall`` has no ``--user`` flag — it removes wherever the
    dist lives on this interpreter's sys.path, which is exactly the
    environment the apply just upgraded. Idempotent: an absent dist is
    a rc=0 skip-warn. Under a PEP 668 externally-managed interpreter the
    ``--break-system-packages`` flag rides along (cascade P2-1,
    2026-10-18): pip gates uninstalls on the marker too, and a cleanup
    leg that refuses on every apply is worse than no cleanup leg.
    """
    cmd = [sys.executable, "-m", "pip", "uninstall", "-y", legacy]
    if _pep668_externally_managed():
        cmd.append("--break-system-packages")
    return cmd


def _owned_script_paths(shape: InstallShape) -> list[Path]:
    """Where the ``vesma`` console scripts must live for THIS shape.

    Posix layouts only; on other platforms the self-check degrades to an
    empty list (honest: no check, no false alarm).
    """
    if os.name != "posix":
        return []
    base: Path | None = None
    if shape.kind == OWNED_VENV and shape.venv_root is not None:
        base = shape.venv_root / "bin"
    elif shape.kind == USER_SITE:
        import sysconfig

        scripts = sysconfig.get_path("scripts", f"{os.name}_user")
        base = Path(scripts) if scripts else None
    if base is None:
        return []
    return [base / name for name in _OWNED_SCRIPTS]


def _cleanup_legacy_dists(
    console: Console,
    shape: InstallShape,
    *,
    dist: str,
    target: str,
    verbose: bool,
) -> None:
    """Uninstall the pre-rebrand dist names from THIS environment (best-effort).

    Runs ONLY after the new vesma installed successfully (rc==0) — the
    order discipline of the 2026-10-18 field incident. ``dist`` (the
    name apply just upgraded) is EXEMPT: an alias-only install upgrades
    through its legacy channel, and the cleanup must never remove the
    dist it just updated. A failed legacy uninstall is a WARN plus one
    update-history record, never an apply failure: the new vesma is
    already in place. After any successful removal the shared console
    scripts are self-checked; a script the legacy uninstall took with it
    is repaired by re-installing the new dist
    (see :func:`_repair_shared_scripts`).
    """
    present = [(name, ver) for name, ver in _installed_legacy_dists() if name != dist]
    if not present:
        return
    removed_any = False
    for legacy, version in present:
        if verbose:
            console.print(f"[cyan]$[/cyan] {' '.join(_legacy_cleanup_cmd(legacy))}")
        proc = _run_cmd(_legacy_cleanup_cmd(legacy))
        rc = proc.returncode if proc is not None else 1
        if rc == 0:
            removed_any = True
            console.print(f"[green]✓[/green] legacy dist removed: {legacy} {version}")
        else:
            manual = f"{sys.executable} -m pip uninstall {legacy}"
            if _pep668_externally_managed():  # the hint must actually work (P2-1)
                manual += " --break-system-packages"
            console.print(
                "[yellow]⚠[/yellow] could not remove legacy dist "
                f"{legacy} {version} (rc={rc}) — the apply stands; remove it "
                f"manually: {manual}"
            )
            _append_history(dist=legacy, from_version=version, to_version="cleanup", rc=rc)
    if removed_any:
        _repair_shared_scripts(console, shape, dist=dist, target=target)


def _repair_shared_scripts(
    console: Console, shape: InstallShape, *, dist: str, target: str
) -> None:
    """Re-install the new dist when a legacy uninstall took a shared script.

    pip uninstalls strictly per the removed dist's own RECORD — a
    console script BOTH dists claim dies with the legacy removal even
    though the new dist owns it too (prod 6.2.0: ``pip uninstall
    vesma-memory-server`` killed ``bin/vesma``). The pinned
    force-reinstall restores every script; the wheel is already in pip's
    cache from the apply leg, so this is near-local. A failed repair is
    a loud manual fix — the dist itself is fine, only the launcher is
    gone.
    """
    missing = [p for p in _owned_script_paths(shape) if not p.exists()]
    if not missing:
        return
    repair = [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps"]
    if shape.kind == USER_SITE:
        repair.append("--user")
    repair.append(f"{dist}=={target}")
    if _pep668_externally_managed():
        repair.append("--break-system-packages")
    proc = _run_cmd(repair)
    rc = proc.returncode if proc is not None else 1
    if rc == 0 and not [p for p in _owned_script_paths(shape) if not p.exists()]:
        console.print(
            "[green]✓[/green] shared console script(s) restored: "
            + ", ".join(m.name for m in missing)
        )
        return
    console.print(
        "[red]✗[/red] the legacy cleanup removed the shared console script(s) "
        + ", ".join(m.name for m in missing)
        + " and the automatic repair failed — restore them manually:\n"
        + "  "
        + " ".join(repair)
    )


# ── check mode ───────────────────────────────────────────────────────────────


def _family_note(
    installed_as: str | None,
    installed: str | None,
    info: UpdateInfo | None,
    family: str | None,
    shape: InstallShape | None = None,
) -> str:
    """The pip family row's Note cell (one row for all aliases, #W-C).

    ``family`` is the family max over the aliases' published versions;
    ``info`` is the per-dist answer for the installed alias. The note
    says UPDATE AVAILABLE whenever ANY known latest beats the installed
    version — the wording stays actionable by ``vesma self-update apply``,
    which re-fetches and reports per-dist honestly when the aliases
    diverge. Install-shape aware: when apply would refuse (foreign venv,
    tool manager), the note names the real update path instead of
    advertising a command that exits 1.
    """
    if installed is None or installed_as is None:
        return "not installed — pip install --user vesma"
    if shape is not None and shape.kind in (FOREIGN_VENV, TOOL_MANAGER):
        return _shape_note(shape.kind)
    candidates = [v for v in ((info.latest if info is not None else None), family) if v]
    if not candidates:
        return "latest unknown (offline or check disabled)"
    latest = max(candidates, key=version_key)
    if info is not None and info.update_available:
        return "UPDATE AVAILABLE — run 'vesma self-update apply'"
    if version_key(latest) > version_key(installed):
        return "UPDATE AVAILABLE — run 'vesma self-update apply'"
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
    shape = _install_shape()

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
            shape,
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
        "[dim]`vesma self-update apply` changes only the pip copy this vesma owns "
        "(the user-site, or its own marked venv) and the npm package; foreign "
        "environments, prod venvs and Go binaries are never touched. pip Latest = the "
        "family max over the alias dists (vesma-memory-server / vesma — same "
        "codebase; mnemos-memory-server is the deprecated legacy mirror).[/dim]"
    )
    _print_legacy_home_hint(console)
    return info


# ── install mode (--yes) ─────────────────────────────────────────────────────


def _build_pip_cmd(dist: str, target: str, pin: bool, *, user_site: bool = True) -> list[str]:
    """The pip command for one apply leg.

    ``user_site=False`` is the OWNED-venv path: upgrade IN the venv the
    running interpreter belongs to (``sys.executable -m pip install``).
    ``--user`` inside a venv is impossible by definition — pip answers
    "User site-packages are not visible in this virtualenv" (rc=1), the
    exact 6.2.0 field incident this flag removes. Pinned targets install
    without ``--upgrade`` (an exact-version rollback must move down too).
    """
    cmd = [sys.executable, "-m", "pip", "install"]
    if user_site:
        cmd.append("--user")
    if pin:
        cmd.append(f"{dist}=={target}")
    else:
        cmd.extend(["--upgrade", dist])
    if _pep668_externally_managed():
        cmd.append("--break-system-packages")
    return cmd


#: Cascade F2 (2026-10-18): the ``--to`` pin is operator input that lands in
#: the pip spec, the ownership-marker content and the update history. A
#: character-class check keeps all three injection-free. Deliberate choice
#: over ``packaging.version.Version``: the regex is dependency-free, accepts
#: the full legacy spelling range pip resolves (pre-PEP 440 tags, wildcards,
#: local versions) and structurally blocks whitespace/newlines — the
#: injection vector — while ``Version()`` would reject some installable
#: historic spellings for no security gain.
_PIN_SPEC_RE = re.compile(r"^[0-9A-Za-z.!+~*_-]+$")
_PIN_SPEC_MAX_LEN = 64


def _valid_pin_spec(value: str) -> bool:
    """True when ``--to`` is a safe single-token pip version spec."""
    return len(value) <= _PIN_SPEC_MAX_LEN and bool(_PIN_SPEC_RE.match(value))


def _run_user_update(console: Console, to: str | None, *, verbose: bool = False) -> None:
    if to is not None and not _valid_pin_spec(to):
        # Before anything else — no pip call, no marker touch, no history row.
        console.print(
            f"[red]✗[/red] invalid --to value {to!r} — expected a pip version spec "
            f"(digits, letters and . ! + ~ * _ - only, max {_PIN_SPEC_MAX_LEN} chars, "
            "no whitespace)"
        )
        raise typer.Exit(1)
    shape = _install_shape()
    if shape.kind in (FOREIGN_VENV, TOOL_MANAGER):
        _refuse_apply(console, shape)  # raises Exit(1) — nothing run, nothing recorded

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

    cmd = _build_pip_cmd(dist, target, pin, user_site=shape.kind == USER_SITE)
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
        _refresh_owned_marker(shape, target)
        if verbose:
            console.print(f"[green]✓[/green] pip: {dist} {installed} → {target}")
        else:
            upgraded = detect_installed_dist()
            new_version = upgraded[1] if upgraded is not None and upgraded[0] == dist else installed
            if version_key(new_version) > version_key(installed):
                console.print(f"[green]✓[/green] pip: {dist} {installed} → {new_version}")
            else:
                console.print(f"[green]✓[/green] pip: {dist} {installed} — already current")
        _cleanup_legacy_dists(console, shape, dist=dist, target=target, verbose=verbose)
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
        # P0 (cli-audit 2026-10-08): after an engine upgrade the installed
        # components.d manifests must match THIS engine — stale ones made
        # the next `vesma service run` fail-closed (REQUIREMENTS_INVALID)
        # and any supervisor restart meant downtime. Reconcile NOW.
        _reconcile_service_manifests(console)
        _print_legacy_home_hint(console)
    else:
        raise typer.Exit(1)


def _reconcile_service_manifests(console: Console) -> None:
    """Post-upgrade manifest reconciliation (P0, cli-audit 2026-10-08).

    Best-effort in shape but loud in outcome: stale bundled manifests
    (board/metrics) are regenerated from the new engine's bundle,
    operator-authored manifests (mesh.yaml) are only WARN-checked, and a
    residual fail-closed validation failure prints the manual remediation
    and exits 1 — an upgrade that leaves a service unable to restart is
    not a successful apply.
    """
    from vesma.service.errors import ManifestError
    from vesma.service.install import InstallError, regenerate_stale_manifests

    try:
        lines, regenerated = regenerate_stale_manifests()
    except (InstallError, ManifestError, OSError) as exc:
        console.print(
            f"[red]✗[/red] service manifest reconciliation failed: {exc}\n"
            "[dim]run `vesma service install` BEFORE restarting vesma.service[/dim]"
        )
        raise typer.Exit(1) from exc
    if not lines:
        return  # no components.d installed — no service surface to reconcile
    for line in lines:
        warn_line = line.startswith("WARN:")
        style = "[yellow]⚠[/yellow]" if warn_line else "[cyan]·[/cyan]"
        # Text(line): report lines carry operator data (paths, codes) —
        # rendered without markup, only the prefix is styling.
        console.print(style, Text(line))
    if regenerated:
        console.print(
            f"[green]service manifests regenerated for {__version__} — "
            "restart vesma.service[/green]"
        )
    else:
        console.print(f"[cyan]service manifests current for {__version__}[/cyan]")


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
        "'exec \"$HOME/.local/bin/vesma\" self-update --yes --scope=user'"
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
            "'exec \"$HOME/.local/bin/vesma\" self-update --yes --scope=user'"
        )
        raise typer.Exit(1)
    if in_container:
        if not force:
            where = f"box '{box}'" if box else "a container"
            console.print(
                f"[red]✗[/red] running inside {where} — installing the timer here would "
                "schedule units the host user manager never loads (dead units, issue #468). "
                "Run `vesma self-update timer install` on the HOST instead (it wires one "
                "ExecStart line per box into the host units — see the upgrade runbook), "
                "or re-run here with --force if you intend to install from this box."
            )
            raise typer.Exit(1)
        _install_timer_in_box(console, box, host_home)  # type: ignore[arg-type]
        return
    _install_timer_on_host(console)


def _install_timer_on_host(console: Console) -> None:
    if sys.platform != "linux" and shutil.which("systemctl") is None:
        # Portability F8: refuse BEFORE writing any unit files — on macOS/BSD
        # the units would be dead weight and the old post-write warning read
        # like a partial success. Linux without systemctl keeps the old
        # write-then-hint path (containers may enable systemd later).
        console.print(
            "[yellow]![/yellow] self-update timer install is not supported on "
            "this platform: systemd user units are a Linux mechanism."
        )
        raise typer.Exit(1)
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


# ── timer status (`vesma self-update timer status`, #W-C) ─────────────────────────


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


# ── component inventory (`vesma self-update components`, #W-C) ─────────────────────

#: The bundled cortex manifest (importlib.resources pattern, mirrors
#: ``decision_provider.CORTEX_ARTIFACT_DIR`` without importing numpy).
_CORTEX_MANIFEST_RESOURCE = "models/vesma-cortex-v1/manifest.json"


def _cortex_manifest() -> dict[str, Any]:
    """The shipped vesma-cortex-v1 manifest as a dict (raises on corruption)."""
    raw = resource_files("vesma").joinpath(_CORTEX_MANIFEST_RESOURCE).read_text(encoding="utf-8")
    data: object = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"cortex manifest is not an object: {_CORTEX_MANIFEST_RESOURCE}")
    return data


def _pip_component() -> tuple[str, str]:
    detected = detect_installed_dist()
    if detected is None:
        return "-", "`pip install --user vesma`, then `vesma self-update apply`"
    return f"{detected[0]} {detected[1]}", "`vesma self-update apply`"


def _integration_component() -> tuple[str, str]:
    """Integration pack version + aggregate stale/missing state.

    Reuses the doctor's helpers (``IntegrationManager.verify`` across
    ``load_targets().detected()``) — no logic duplicated, no network.
    """
    try:
        from vesma.cli.integration import IntegrationManager, load_targets

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
    # B2 manifests key the weights hash `sha256`; pre-B2 carried
    # `weights_sha256` — read both so older installed bundles keep reporting.
    rev = str(manifest.get("sha256") or manifest.get("weights_sha256") or "")[:12]
    trained = str(manifest.get("trained_at") or "")[:10]
    return f"{name} (rev {rev}, trained {trained})", "ships with the wheel"


def _stored_vector_fingerprint(settings: Any) -> str | None:
    """One stored ``model_fingerprint`` from vectors.db (cheap read-only leg)."""
    try:
        import sqlite3

        vectors = Path(str(settings.vesma.data_dir)).expanduser() / "vectors.db"
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
        from vesma.config import load_settings
        from vesma.embeddings import config_fingerprint

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
        return "-", "`vesma self-update apply` (npm not found — report only)"
    version = _npm_global_version(npm)
    return version or "not installed", "`vesma self-update apply` (npm leg, best-effort)"


def _timer_component() -> tuple[str, str]:
    status = _timer_status()
    cell = str(status["state"])
    if status.get("container"):
        box = status.get("box")
        cell += f" — in distrobox '{box}'" if box else " — in a container"
    return cell, "`vesma self-update timer install`"


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
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Check for updates / update the installed vesma (issues #445, #460).\n\n"
    "Subcommands: `check` (report only), `apply` (the update path), `timer "
    "install|uninstall|status` (the weekly systemd timer), `components` (the "
    "component inventory). Plain `vesma self-update` keeps the 5.2.0 behavior: report "
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
            help="Deprecated flag form — use: `vesma self-update check`.",
            hidden=True,
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Deprecated flag form — use: `vesma self-update apply`.",
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
            help="Deprecated flag form — use: `vesma self-update apply --scope user`.",
            hidden=True,
        ),
    ] = None,
    to: Annotated[
        str | None,
        typer.Option(
            "--to",
            help="Deprecated flag form — use: `vesma self-update apply --to VERSION`.",
            hidden=True,
        ),
    ] = None,
    install_timer: Annotated[
        bool,
        typer.Option(
            "--install-timer",
            help="Deprecated flag form — use: `vesma self-update timer install`.",
            hidden=True,
        ),
    ] = False,
    uninstall_timer: Annotated[
        bool,
        typer.Option(
            "--uninstall-timer",
            help="Deprecated flag form — use: `vesma self-update timer uninstall`.",
            hidden=True,
        ),
    ] = False,
) -> None:
    """Check for updates / update the installed vesma (issues #445, #460).

    Plain `vesma self-update`: the surfaces report, then — in an interactive
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
                "pass them after it (e.g. `vesma self-update apply --yes`)",
                err=True,
            )
        return
    _legacy_flag_dispatch(
        console,
        check=check,
        yes=yes,
        verbose=verbose,
        scope=scope,
        to=to,
        install_timer=install_timer,
        uninstall_timer=uninstall_timer,
    )


def _legacy_flag_dispatch(
    console: Console,
    *,
    check: bool,
    yes: bool,
    verbose: bool,
    scope: str | None,
    to: str | None,
    install_timer: bool,
    uninstall_timer: bool,
) -> None:
    """The deprecated FLAG forms' dispatch — one code path, two surfaces.

    Shared by the ``vesma self-update`` group callback AND the hidden
    legacy flags on the component ``vesma update`` verb (units installed
    before the rename keep working, with a stderr hint naming the
    canonical spelling).
    """
    if install_timer and uninstall_timer:
        console.print("[red]✗[/red] --install-timer and --uninstall-timer are mutually exclusive")
        raise typer.Exit(1)
    if install_timer:
        _deprecated_flag_hint(
            "vesma self-update --install-timer", "vesma self-update timer install"
        )
        _install_timer(console)
        return
    if uninstall_timer:
        _deprecated_flag_hint(
            "vesma self-update --uninstall-timer", "vesma self-update timer uninstall"
        )
        _uninstall_timer(console)
        return
    if scope is not None and scope != "user":
        console.print(
            "[red]✗[/red] only --scope=user is supported: prod venvs, Go binaries "
            "and containers are manual surfaces by design"
        )
        raise typer.Exit(1)
    if yes:
        _deprecated_flag_hint("vesma self-update --yes", "vesma self-update apply")
        _run_user_update(console, to, verbose=verbose)
        return
    # Hints precede the report: the first line a human sees names the
    # canonical spelling; stdout itself stays hint-free for pipes/JSON.
    if to is not None:
        _deprecated_flag_hint(
            "vesma self-update --to VERSION", "vesma self-update apply --to VERSION"
        )
    if scope is not None:
        _deprecated_flag_hint(
            "vesma self-update --scope user", "vesma self-update apply --scope user"
        )
    if check:
        _deprecated_flag_hint("vesma self-update --check", "vesma self-update check")
    info = _print_check(console)
    if check:
        return
    if info is None or not info.update_available:
        return  # nothing pending — the report stands as-is
    if _stdin_is_tty():
        if typer.confirm("Apply update?", default=False):
            _run_user_update(console, to, verbose=verbose)
        return
    console.print("[dim]apply with: vesma self-update apply[/dim]")


@update_app.command(name="check")
def update_check() -> None:
    """Report every update surface without changing anything.

    Never prompts, never applies — identical to the deprecated
    `vesma self-update --check`, safe in pipes and CI.
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
    """Apply the update now: upgrade the pip copy this vesma owns (+ npm).

    The apply path behind the deprecated `vesma self-update --yes`. Never
    prompts — invoking `apply` IS the confirmation; `-y/--yes` is
    accepted but changes nothing. `--to VERSION` pins a (rollback)
    version. Install-shape aware: a marked/canonical venv upgrades in
    place (no --user), a bare user-site install uses pip --user, a
    foreign venv or a pipx/uv-tool environment is refused with the
    proper update path named. Every run appends a record to the update
    history and removes pre-rebrand legacy dists from this environment.
    """
    console = Console()
    if scope != "user":
        console.print(
            "[red]✗[/red] only --scope=user is supported: prod venvs, Go binaries "
            "and containers are manual surfaces by design"
        )
        raise typer.Exit(1)
    _run_user_update(console, to, verbose=verbose)


# ── timer subcommands (`vesma self-update timer ...`) ──────────────────────────────


timer_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "Install, remove or inspect the weekly update timer.\n\n"
        "The timer is the scheduled side of `vesma self-update`: a systemd user "
        "timer (or the platform equivalent) that runs the weekly check+apply "
        "pass. `install` sets it up, `uninstall` tears it down, `status` shows "
        "whether it is scheduled and when it last fired."
    ),
)


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


# Legend lines for the timer state fields (cli-audit 2026-10-08 P2:
# ``state: not installed`` next to ``host_state: installed (disabled)``
# with no explanation of which manager actually fires). Only shown when
# a HOST leg exists — on a plain host the single ``state`` is
# self-explanatory.
_TIMER_BOX_LEGEND_TEXT = (
    "[dim]legend: state = this box's systemd user manager — units there are never "
    "loaded, the timer cannot fire from the box; host_state = the HOST user units, "
    "the only place the update timer actually runs. It fires where the unit is "
    "ENABLED (host_state); install or change it on the HOST with "
    "`vesma self-update timer install`.[/dim]"
)
_TIMER_BOX_LEGEND_JSON = {
    "state": "this box's systemd user manager (never loaded inside the box)",
    "host_state": "the HOST user units — where the update timer actually fires",
    "fires_when": "the HOST unit is enabled; manage it on the host via "
    "`vesma self-update timer install`",
}


@timer_app.command(name="status")
def timer_status(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the timer state as JSON (for scripting / CI)."),
    ] = False,
) -> None:
    """Show timer presence, enabled state and last trigger.

    Reports the local systemd user manager; inside a distrobox also the
    HOST units (a loadable timer can only live there, #468), a note that
    this machine appears to be a box, and a one-line legend of which leg
    actually fires (cli-audit 2026-10-08 P2).
    """
    status = _timer_status()
    console = Console()
    if json_output:
        if status.get("host_state") is not None:
            status = {**status, "legend": _TIMER_BOX_LEGEND_JSON}
        console.print_json(json.dumps(status))
        return
    table = Table(title=f"Update timer ({TIMER_UNIT})", show_header=True, header_style="bold")
    table.add_column("Field")
    table.add_column("Value")
    for key, value in status.items():
        table.add_row(key, str(value))
    console.print(table)
    if status.get("host_state") is not None:
        console.print()
        console.print(_TIMER_BOX_LEGEND_TEXT)


update_app.add_typer(timer_app, name="timer")


# ── components subcommand (`vesma self-update components`, #W-C) ───────────────────


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
