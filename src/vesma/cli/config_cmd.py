"""``vesma config`` + ``vesma system config`` — the TWO config domains.

CLI topology v2 wave C (ArchCom-ratified 2026-10-23): config gets TWO
EXPLICIT verbs, never an euid/env auto-detect — a config command that
silently switches its write target by privilege is the exact failure
class the committee rejected (2:1). ``vesma config`` owns the USER
profile only (the ``find_config_file`` search order: explicit →
``$VESMA_CONFIG`` → ``./config.yaml`` → ``~/.vesma/config.yaml``);
``vesma system config`` owns the SYSTEM profile conffile only
(``/etc/vesma/config.yaml`` via the FHS manifest resolver). Each help
names the sibling domain — discoverability without ambiguity.

Reuse contract (one implementation, no forks):

* validation goes through :func:`vesma.config.load_settings` — the
  SAME machinery ``vesma doctor`` runs;
* the user paths table renders through the doctor's own
  ``_collect_paths``/``_render_paths`` (imported as a module attribute
  so the seam is monkeypatchable — tests pin the shared function);
* system paths come from the FHS manifest resolvers only;
* ``set --file`` delegates to
  :func:`vesma.service.system_install.install_system_conffile`
  (validate-first, same-dir backup, atomic replace, 0640 root:vesma).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console
from rich.text import Text

from vesma.config import find_config_file, load_settings
from vesma.service import fhs
from vesma.service.system_install import (
    SystemInstallError,
    install_system_conffile,
)

config_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="config",
    help=(
        "Inspect and validate the USER profile's config (read-only).\n\n"
        "The resolved config file follows the doctor's search order: "
        "$VESMA_CONFIG → ./config.yaml → ~/.vesma/config.yaml. `show` "
        "prints the resolved path plus a compact effective-view of the "
        "settings (env overrides included); `validate` parses and "
        "schema-checks the file through the SAME load_settings the "
        "doctor runs (exit 0/1, one-line verdict); `paths` renders the "
        "SAME paths table the doctor renders. No write verb here by "
        "design: edit the file directly with your editor of choice. "
        "This domain NEVER switches to the system profile — the system "
        "conffile (/etc/vesma/config.yaml, root) lives on "
        "`vesma system config`."
    ),
    no_args_is_help=True,
)

system_config_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="config",
    help=(
        "Inspect and manage the SYSTEM profile's conffile (root).\n\n"
        "Target is /etc/vesma/config.yaml resolved through the FHS "
        "manifest — never a user path. `show`/`validate`/`paths` are "
        "read-only and work unprivileged EXCEPT where the 0640 "
        "root:vesma mode blocks reading: then the command says so "
        "loudly with the sudo hint (it never escalates itself). "
        "`set --file` is the ONLY write: the candidate is validated "
        "FIRST (same load_settings), the current conffile is backed up "
        "same-dir (.bak-<ts>), the install is atomic (tmp + rename) "
        "with mode/owner per the manifest (0640 root:vesma). No editor "
        "is ever spawned from root (G3) — the manual path is "
        "`sudoedit /etc/vesma/config.yaml`. The USER config lives on "
        "`vesma config` (it never switches targets by euid/env)."
    ),
    no_args_is_help=True,
)

console = Console()

#: The doctor-identical config search order, printed wherever the file
#: turns out to be absent (the operator must see WHERE it would load
#: from, not just "not found").
_SEARCH_ORDER_HINT = "search order: $VESMA_CONFIG → ./config.yaml → ~/.vesma/config.yaml"

_JSONOption = Annotated[
    bool,
    typer.Option("--json", help="Emit machine-readable JSON instead of the table."),
]


# ── shared rendering (the compact effective-view) ──────────────────────


def _effective_view(settings: Any) -> dict[str, Any]:
    """The compact effective-view of the loaded settings (show output).

    Deliberately NOT a raw dump of every subsection: the operational
    core a config question is really about (where the store lives, what
    binds, what embeds, what logs) — the same selection philosophy the
    doctor's paths table applies to locations.
    """
    return {
        "store": {
            "data_dir": str(settings.vesma.data_dir),
            "vault_path": str(settings.vesma.vault_path),
            "db_name": settings.vesma.db_name,
        },
        "api": {
            "host": settings.api.host,
            "port": settings.api.port,
            "auth_enabled": settings.api.auth_enabled,
        },
        "embedding": {
            "provider": settings.embedding.provider,
            "model": settings.embedding.model,
        },
        "logging": {
            "level": settings.logging.level,
            "log_file": str(settings.logging.log_file) if settings.logging.log_file else "",
        },
        "mesh": {
            "enabled": settings.mesh.enabled,
        },
    }


def _render_effective(view: dict[str, Any]) -> None:
    """Print the effective-view as labeled sections (mirrors the doctor's
    paths-table style: label column, values through Text — paths are
    operator data, never rich markup)."""
    for section, values in view.items():
        console.print(f"[bold cyan]── {section} ──[/bold cyan]")
        for key, value in values.items():
            rendered = value if value != "" else "(default)"
            console.print(f"  {key:<13}", Text(str(rendered)))


def _print_json(payload: dict[str, Any]) -> None:
    console.print_json(json.dumps(payload, ensure_ascii=False))


def _load_fail(exc: Exception, *, label: str) -> NoReturn:
    """The shared load-failure renderer: typed code passthrough (legacy
    guards carry a stable diagnostic code), data through Text."""
    code = getattr(exc, "code", None)
    detail = f"{code}: {exc}" if code else str(exc)
    console.print("[red]✗[/red]", Text(f"{label}: {detail}"))
    raise typer.Exit(1) from exc


# ── the USER domain: vesma config ──────────────────────────────────────


@config_app.command(name="show")
def config_show(json_output: _JSONOption = False) -> None:
    """Show the resolved USER config path + the effective settings.

    The path is the file load_settings would ACTUALLY load ($VESMA_CONFIG
    → ./config.yaml → ~/.vesma/config.yaml) — with no file anywhere the
    zero-config profile prints explicitly (built-in defaults apply). The
    effective view is the settings as the engine sees them (env overrides
    included), not a raw YAML echo. System-profile config is a different
    domain: `vesma system config show`.
    """
    path = find_config_file()
    try:
        settings = load_settings()
    except Exception as exc:
        _load_fail(exc, label="config load failed")
    if json_output:
        _print_json(
            {
                "config_file": str(path) if path is not None else None,
                "profile": "user",
                "effective": _effective_view(settings),
            }
        )
        return
    if path is None:
        console.print("[cyan]zero-config[/cyan] — no config file found; built-in defaults apply")
        console.print(f"[dim]{_SEARCH_ORDER_HINT}[/dim]")
    else:
        console.print("  [bold]config file[/bold]:", Text(str(path)))
    _render_effective(_effective_view(settings))


@config_app.command(name="validate")
def config_validate() -> None:
    """Parse + schema-validate the USER config (the doctor's check, one line).

    Runs the SAME load_settings validation the doctor's Config check runs
    (one implementation: YAML parse, schema, typed legacy-era guards).
    Verdict is one line, exit code 0 valid / 1 invalid — scriptable. The
    zero-config profile (no file on the search path) is valid by design:
    the built-in defaults are the sanctioned baseline. The group/other-
    readable mode warning mirrors the doctor (exit stays 0 — a warning,
    not a failure). Read-only, needs no privileges.
    """
    path = find_config_file()
    try:
        load_settings()
    except Exception as exc:
        _load_fail(exc, label=str(path) if path is not None else "config validation failed")
    if path is None:
        console.print(
            "[green]✓[/green] zero-config — no config file on the search path;"
            " the built-in defaults are valid"
        )
        console.print(f"[dim]{_SEARCH_ORDER_HINT}[/dim]")
        raise typer.Exit(0)
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = None
    if mode is not None and mode & 0o077:
        console.print(
            "[yellow]⚠[/yellow]",
            Text(
                f"{path} (valid, but group/other-readable — mode {mode:04o});"
                f" the file can carry API keys — tighten: chmod 600 {path}"
            ),
        )
        raise typer.Exit(0)
    console.print("[green]✓[/green]", Text(f"{path} (valid)"))
    raise typer.Exit(0)


@config_app.command(name="paths")
def config_paths(json_output: _JSONOption = False) -> None:
    """Render the USER profile's paths (the SAME table the doctor renders).

    Delegates to the doctor's own collector — root/config/data/db/vault/
    logs/cache/completion rows are produced by the ONE `_collect_paths`
    `vesma doctor paths` uses, so the two surfaces can never disagree.
    System-profile paths (/etc/vesma tree) are a different domain:
    `vesma system config paths`.
    """
    from vesma.cli import doctor as doctor_mod

    try:
        settings = load_settings()
    except Exception as exc:
        _load_fail(exc, label="cannot load settings")
    paths = doctor_mod._collect_paths(settings)
    if json_output:
        _print_json({"profile": "user", "paths": paths})
        return
    doctor_mod._render_paths(paths)
    console.print("[dim]system-profile paths → `vesma system config paths`[/dim]")


# ── the SYSTEM domain: vesma system config ─────────────────────────────


def _system_target() -> Path:
    """The system conffile — FHS manifest resolver only, never a literal."""
    return fhs.system_config_path()


def _probe_system_conffile(target: Path) -> str:
    """Distinguish absent from unreadable WITHOUT guessing.

    Returns ``"absent"``, ``"unreadable"``, or ``"present"``. A non-root
    stat on the 0750 root:vesma tree raises PermissionError — reporting
    that as "absent" would be a silent lie; the sudo hint is the honest
    answer (the command itself NEVER escalates — G1/G2).
    """
    try:
        target.stat()
    except FileNotFoundError:
        return "absent"
    except PermissionError:
        return "unreadable"
    return "present"


def _print_unreadable(target: Path, verb: str) -> None:
    console.print(
        "[red]✗[/red]",
        Text(
            f"cannot access {target} as this user — the system config tree"
            " is root:vesma (0750/0640); try: "
            f"sudo vesma system config {verb}"
        ),
    )


def _load_system_settings(target: Path) -> Any:
    """Load the SYSTEM conffile through the ONE machinery (explicit path
    — the user search order is never consulted for the system domain)."""
    return load_settings(target)


@system_config_app.command(name="show")
def system_config_show(json_output: _JSONOption = False) -> None:
    """Show the SYSTEM conffile path + its effective settings (root tree).

    Target is /etc/vesma/config.yaml through the FHS manifest resolver.
    Absent conffile = the system profile runs zero-config (honest note,
    exit 0). When the 0640 root:vesma mode blocks reading as non-root the
    command says so loudly with the sudo hint and exits 1 — it never
    escalates itself and never falls back to user paths. The USER config
    is a different domain: `vesma config show`.
    """
    target = _system_target()
    state = _probe_system_conffile(target)
    if state == "absent":
        if json_output:
            _print_json({"profile": "system", "config_file": str(target), "present": False})
            return
        console.print("  [bold]config file[/bold]:", Text(str(target)))
        console.print(
            "[cyan]absent[/cyan] — the system profile runs zero-config"
            " (built-in defaults; the system unit needs no conffile)"
        )
        return
    if state == "unreadable":
        _print_unreadable(target, "show")
        raise typer.Exit(1)
    try:
        settings = _load_system_settings(target)
    except PermissionError as exc:
        _print_unreadable(target, "show")
        raise typer.Exit(1) from exc
    except Exception as exc:
        _load_fail(exc, label=str(target))
    if json_output:
        _print_json(
            {
                "profile": "system",
                "config_file": str(target),
                "present": True,
                "effective": _effective_view(settings),
            }
        )
        return
    console.print("  [bold]config file[/bold]:", Text(str(target)))
    _render_effective(_effective_view(settings))


@system_config_app.command(name="validate")
def system_config_validate() -> None:
    """Parse + schema-validate the SYSTEM conffile (one-line verdict).

    The SAME load_settings validation the doctor runs, pointed at the
    FHS-resolved system path only. Absent conffile is valid (zero-config
    baseline, exit 0). Unreadable as non-root (0640 root:vesma) → loud
    sudo hint, exit 1 — the command never escalates itself. Invalid →
    the parse/schema error and exit 1. Read-only; runs as root it needs
    no extra privileges beyond the file's own mode.
    """
    target = _system_target()
    state = _probe_system_conffile(target)
    if state == "absent":
        console.print(
            "[green]✓[/green]",
            Text(f"no system conffile at {target} — zero-config defaults apply"),
        )
        raise typer.Exit(0)
    if state == "unreadable":
        _print_unreadable(target, "validate")
        raise typer.Exit(1)
    try:
        _load_system_settings(target)
    except PermissionError as exc:
        _print_unreadable(target, "validate")
        raise typer.Exit(1) from exc
    except Exception as exc:
        _load_fail(exc, label=str(target))
    console.print("[green]✓[/green]", Text(f"{target} (valid)"))
    raise typer.Exit(0)


#: The system paths table rows — (label, FHS manifest resolver). Path
#: math only (no I/O, no privileges): an unprivileged operator can see
#: where the system profile WOULD live.
_SYSTEM_PATH_ROWS: tuple[tuple[str, Callable[..., Path]], ...] = (
    ("Config root", fhs.system_config_root),
    ("Config file", fhs.system_config_path),
    ("Components.d", fhs.system_components_dir),
    ("Env dir", fhs.system_env_dir),
    ("Data root", fhs.system_data_root),
    ("Install state", fhs.system_install_state_path),
    ("System unit", fhs.system_unit_path),
    ("Cache root", fhs.system_cache_root),
    ("Static root", fhs.system_static_root),
)


def _system_paths() -> dict[str, str]:
    return {label: str(resolver()) for label, resolver in _SYSTEM_PATH_ROWS}


@system_config_app.command(name="paths")
def system_config_paths(json_output: _JSONOption = False) -> None:
    """Render the SYSTEM profile's paths (FHS manifest, no user paths).

    Every row comes from a FHS manifest resolver (config root, conffile,
    components.d, env.d, data root, install state, unit, cache, static) —
    path math only, readable without root: seeing where the system
    profile lives must not require the privileges of changing it. The
    USER profile's paths are a different domain: `vesma config paths`.
    """
    paths = _system_paths()
    if json_output:
        _print_json({"profile": "system", "paths": paths})
        return
    console.print("[bold cyan]── System paths (FHS manifest) ──────────────[/bold cyan]")
    for label, value in paths.items():
        console.print(f"  {label:<13}", Text(value))
    console.print("[dim]user-profile paths → `vesma config paths`[/dim]")


@system_config_app.command(name="set")
def system_config_set(
    file: Annotated[
        Path,
        typer.Option(
            "--file",
            help="Path to the candidate config file (validated BEFORE any mutation).",
        ),
    ],
) -> None:
    """Install a validated config as the SYSTEM conffile (the only write).

    Validate-first: the candidate goes through the SAME load_settings
    validation, and an invalid candidate is refused with the tree
    untouched (no backup, no replace). On success the current conffile
    is backed up same-dir (.bak-<ts>), the new bytes land ATOMICALLY
    (same-dir tmp + rename) with mode/owner per the FHS manifest
    (0640 root:vesma), and the report names from/to/backup plus the
    restart note (the daemon reads the config once at startup). Requires
    root; refuses when the system profile is not installed. No editor
    is ever spawned from root (G3) — the manual path is
    `sudoedit /etc/vesma/config.yaml`.
    """
    try:
        result = install_system_conffile(file)
    except SystemInstallError as exc:
        console.print("[red]✗[/red]", Text(str(exc)))
        raise typer.Exit(1) from exc
    owner_group = f"{result.owner or '-'}:{result.group or '-'}"
    console.print(
        "[green]✓[/green] installed",
        Text(f"{result.target} (mode {result.mode:04o} {owner_group} per the FHS manifest)"),
    )
    if result.backup is not None:
        console.print("  [bold]backup[/bold]:", Text(str(result.backup)))
    else:
        console.print("  first conffile — no previous file to back up")
    console.print(
        "[yellow]note[/yellow] the new config takes effect on the NEXT service"
        " start (the daemon reads the config once at startup)"
    )
    console.print("  [dim]restart with: sudo systemctl restart vesma[/dim]")
    console.print(f"  [dim]manual edits: sudoedit {result.target}[/dim]")


if __name__ == "__main__":  # pragma: no cover — manual invocation
    config_app()
