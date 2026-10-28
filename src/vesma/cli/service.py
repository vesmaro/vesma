"""``vesma service`` CLI — install/uninstall (W4) + supervisor control plane (W3).

Two command groups share this sub-app:

* ``install`` / ``uninstall`` — the install flow (pack manifests, data
  dirs, venvs, the generated systemd user unit; wave W4).
* ``status`` / ``health`` / ``start`` / ``stop`` / ``restart`` / ``logs``
  — the thin client over the control socket (control-socket v1 §4.8),
  plus ``run`` — the supervisor entrypoint that owns the socket and the
  component tree in one process (wave W3).

CLI topology (variant B, owner-ratified 2026-10-23): the SYSTEM profile
moved to the ``vesma system`` domain (:mod:`vesma.cli.system_cmd`). The
former ``--system`` flag forms here stay as hidden deprecated aliases —
identical behavior + a one-line stderr hint (removal not before 9.0);
``update`` without ``--system`` refuses, pointing at the canonical
spelling.

MERGE NOTE (W3/W4 collision): both waves created this file; the resolved
shape is the concatenation of the two groups — one command function per
verb, no shared mutable state.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypeVar

import typer
from rich.console import Console
from rich.text import Text

from vesma.service import layout
from vesma.service.client import (
    ControlClient,
    ControlClientError,
    PreflightError,
    ProtocolError,
    ResponseTimeoutError,
    SocketUnavailableError,
    control_socket_path,
)
from vesma.service.control import MAX_TAIL
from vesma.service.errors import ManifestError
from vesma.service.home_lock import WriterLockBusyError, writer_lock
from vesma.service.install import InstallError, UninstallResult, install, uninstall
from vesma.service.system_install import (
    SystemInstallError,
    SystemUninstallResult,
    install_system,
    uninstall_system,
)
from vesma.service.system_update import SystemUpdateResult, update_system

service_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="service",
    help=(
        "Install/uninstall the vesma service (USER profile) and drive the "
        "supervisor control plane: status, start/stop/restart, logs, run.\n\n"
        "The client verbs (status/health/start/stop/restart/logs) talk to the "
        "running supervisor over its control socket; `install` sets up the "
        "systemd user unit and per-component venvs, and `run` starts the "
        "supervisor itself in the foreground. Every client verb accepts "
        "`--socket` to point at a non-default control socket (tests, "
        "multi-instance machines). The component registry verbs "
        "(install/update/configs/status of component MANIFESTS) live on "
        "`vesma service components`; `start/stop/restart` take one NAME or "
        "`--all` (topological order — wave B of CLI topology v2).\n\n"
        "The SYSTEM profile (FHS, root) is the `vesma system` domain: "
        "`vesma system install` / `uninstall` / `update`. The former "
        "`--system` flag forms here remain as hidden deprecated aliases."
    ),
    no_args_is_help=True,
)

console = Console()
logger = logging.getLogger("vesma.cli.service")

#: The log source named by the explicit empty-output message (the P2
#: cli-audit 2026-10-08 finding: `service logs` printed NOTHING on an
#: empty tail — silence is never an answer). Wave B of CLI topology v2
#: carried it from the former top-level `vesma logs` verb into `service
#: logs` (its canonical home since the wave).
LOG_SOURCE_NOTE = (
    "source: supervisor ring buffer via the control socket — lines emitted "
    "since the supervisor started; the component may not have run yet"
)

F = TypeVar("F", bound=Callable[..., Any])


# ── install/uninstall (wave W4) ───────────────────────────────────────


def _fail_install(exc: Exception) -> NoReturn:
    # Text(exc): report DATA must never pass through rich markup — an
    # exception message containing '[' is operator text, not formatting.
    console.print("[red]✗[/red]", Text(str(exc)))
    raise typer.Exit(1) from exc


def _deprecated_flag_hint(old_form: str, new_form: str) -> None:
    """One-line deprecation hint for a hidden legacy flag form (stderr).

    Standing design rule: flags do not replace subcommands. The
    ``--system`` flag forms moved to the ``vesma system`` domain (variant
    B, owner-ratified 2026-10-23); they keep working here — identical
    behavior, scripts must not be able to tell the difference — but point
    at the canonical spelling. stderr only — stdout stays clean for pipes
    and JSON consumers. Removal not before 9.0 (MAJOR).
    """
    typer.echo(f"[deprecated] `{old_form}` is deprecated — use: {new_form}", err=True)


@service_app.command(name="install")
def service_install(
    system: Annotated[
        bool,
        typer.Option(
            "--system",
            help="Deprecated flag form — use: `vesma system install`.",
            hidden=True,
        ),
    ] = False,
) -> None:
    """Install the service (the USER profile): pack manifests, dirs, venvs, unit.

    Idempotent — re-running regenerates every artifact (hand edits to the
    unit are overwritten by design, threat model "ручная правка юнита").
    Inside a container the filesystem hardening directives are loudly
    downgraded (marker in the unit + report lines).

    The SYSTEM profile has its own domain: `vesma system install` (FHS:
    /etc/vesma, /var/lib/vesma, /usr/lib/vesma; requires root). The former
    `--system` flag form remains here as a hidden deprecated alias —
    identical behavior, removal not before 9.0.
    """
    if system:
        _deprecated_flag_hint("vesma service install --system", "vesma system install")
    try:
        result = install_system() if system else install()
    except (InstallError, ManifestError, SystemInstallError) as exc:
        _fail_install(exc)
    for line in result.lines:
        downgraded_line = line.startswith("CONTAINER DOWNGRADE")
        warn_line = line.startswith(("WARN:", "note:", "preserved"))
        style = "[yellow]⚠[/yellow]" if (downgraded_line or warn_line) else "[green]✓[/green]"
        # Text(line): report lines carry operator data (paths, names) —
        # rendered without markup, only the prefix is styling.
        console.print(style, Text(line))
    console.print(f"[green]Service installed ({result.unit_path}).[/green]")


@service_app.command(name="uninstall")
def service_uninstall(
    name: Annotated[
        str | None,
        typer.Argument(help="Component name (e.g. board, metrics).", show_default=False),
    ] = None,
    remove_all: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Uninstall everything: stop/disable + remove the unit and all components.",
        ),
    ] = False,
    system: Annotated[
        bool,
        typer.Option(
            "--system",
            help="Deprecated flag form — use: `vesma system uninstall`.",
            hidden=True,
        ),
    ] = False,
) -> None:
    """Uninstall one component, or the whole USER installation with --all.

    Only files the install flow owns are removed (manifest, env file,
    venv). Component data dirs are operator data and are preserved.

    The SYSTEM profile has its own domain: `vesma system uninstall` (the
    exact inverse of the install-state; requires root). The former
    `--system` flag form remains here as a hidden deprecated alias —
    identical behavior, removal not before 9.0.
    """
    if system:
        _deprecated_flag_hint("vesma service uninstall --system", "vesma system uninstall")
    try:
        result: UninstallResult | SystemUninstallResult
        if system:
            if name is not None or remove_all:
                raise SystemInstallError(
                    "--system uninstalls the whole system profile — do not "
                    "combine it with a component name or --all"
                )
            result = uninstall_system()
        else:
            result = uninstall(name, remove_all=remove_all)
    except (InstallError, ManifestError, SystemInstallError) as exc:
        _fail_install(exc)
    for line in result.lines:
        warn_line = line.startswith(("WARN:", "preserved"))
        style = "[yellow]⚠[/yellow]" if warn_line else "[green]✓[/green]"
        console.print(style, Text(line))
    console.print("[green]Uninstall complete.[/green]")


@service_app.command(name="update")
def service_update(
    system: Annotated[
        bool,
        typer.Option(
            "--system",
            help="Deprecated flag form — use: `vesma system update`.",
            hidden=True,
        ),
    ] = False,
) -> None:
    """Deprecated alias surface: the SYSTEM profile lives on `vesma system update`.

    The user profile updates through `vesma self-update` (the pip channel
    owns user installs); the SYSTEM profile updates through
    `vesma system update` (specs/layout/v2 §3.13 — drift refuses loudly
    BEFORE any action, a failed health gate rolls back to the previous
    revision, re-runs are idempotent). The former `--system` flag form
    remains here as a hidden deprecated alias — identical behavior,
    removal not before 9.0; the bare form refuses.
    """
    if not system:
        _fail_install(
            SystemInstallError(
                "`vesma service update` targets the SYSTEM profile only —"
                " use: `vesma system update`; the user profile updates via"
                " `vesma self-update`"
            )
        )
    _deprecated_flag_hint("vesma service update --system", "vesma system update")
    try:
        result: SystemUpdateResult = update_system()
    except (InstallError, ManifestError, SystemInstallError) as exc:
        _fail_install(exc)
    for line in result.lines:
        warn_line = line.startswith(("WARN:", "note:", "preserved", "rollback:", "conffile kept"))
        style = "[yellow]⚠[/yellow]" if warn_line else "[green]✓[/green]"
        # Text(line): report lines carry operator data (paths, names) —
        # rendered without markup, only the prefix is styling.
        console.print(style, Text(line))
    console.print(f"[green]System profile updated ({result.engine_venv}).[/green]")


# ── control plane: shared plumbing (wave W3) ──────────────────────────


def _fail(message: str) -> NoReturn:
    console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(code=1) from None


def _open_client(socket: Path | None) -> ControlClient:
    """Connect a thin client (§4.1 pre-flight inside ``connect``)."""
    client = ControlClient(socket or control_socket_path()[0])
    client.connect()
    return client


def _with_client(socket: Path | None, action: Callable[[ControlClient], Any]) -> Any:
    """Run one CLI verb against a fresh client; map errors to exit 1."""
    try:
        client = _open_client(socket)
    except PreflightError as exc:
        console.print(
            f"[red]refusing to connect ([§4.1] pre-flight)[/red]: {exc.problem}: {exc.path}"
        )
        if exc.fix_command:
            console.print(f"fix manually, then retry:\n  [bold]{exc.fix_command}[/bold]")
        raise typer.Exit(code=1) from None
    except SocketUnavailableError as exc:
        _fail(f"{exc} — is the supervisor running? (start it with `vesma service run`)")
    except ControlClientError as exc:
        _fail(str(exc))
    try:
        return action(client)
    except ProtocolError as exc:
        hint = (
            "" if exc.code_range != "unknown" else f" (unrecognized code, range: {exc.code_range})"
        )
        _fail(f"supervisor rejected the request: {exc.message}{hint}")
    except ResponseTimeoutError as exc:
        _fail(
            f"{exc} — the operation may or may not have taken effect; retry idempotent verbs safely"
        )
    except ControlClientError as exc:
        _fail(str(exc))
    finally:
        client.close()


# ── verbs: client side (§4.8 mapping is 1:1) ──────────────────────────


@service_app.command("status")
def status(
    component: Annotated[str | None, typer.Argument(help="Optional single component.")] = None,
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Control socket override (tests).")
    ] = None,
) -> None:
    """Show the component state tree (or one component).

    Prints the live state (running/stopped/failed plus PID and uptime where
    available) as JSON for every installed component, or only the one named
    as the argument. Requires the supervisor to be up (`vesma service run`);
    exits 1 with a hint when the control socket does not answer.
    """
    result = _with_client(socket, lambda client: client.status(component))
    console.print_json(json.dumps(result))


@service_app.command("health")
def health(
    component: Annotated[str | None, typer.Argument(help="Optional single component.")] = None,
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Control socket override (tests).")
    ] = None,
) -> None:
    """Show global and per-component health (or one component).

    Health is the supervisor's own verdict (healthz semantics): OK when the
    component process answers its readiness probe, DEGRADED/FAIL with the
    reason otherwise. Use it after `start`/`restart` to confirm the component
    actually came up, or pass a component name to check just that one.
    """
    result = _with_client(socket, lambda client: client.health(component))
    console.print_json(json.dumps(result))


@service_app.command("start")
def start(
    component: Annotated[
        str | None,
        typer.Argument(help="Component name from the manifest registry.", show_default=False),
    ] = None,
    all_components: Annotated[
        bool,
        typer.Option("--all", help="Start every installed component (dependency order)."),
    ] = False,
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Control socket override (tests).")
    ] = None,
) -> None:
    """Start a component — or every installed one with --all (topo order).

    The single-component path is the same control-plane dispatch as
    always (idempotent: an already-running component is reported as-is,
    so start scripts can run this unconditionally) behind the wave-61
    target resolution: the NAME is checked against the registry first,
    bare invocation refuses (never a bulk mutation). `--all` starts in
    dependency order (`depends_on` before dependents) — lifted here from
    the former top-level `vesma start --all` (CLI topology v2 wave B).
    The hidden top-level `vesma start` alias calls the SAME function.
    Check `vesma service health` afterwards to confirm readiness.
    """
    from vesma.cli.components import start_command

    start_command(
        component,
        all_components=all_components,
        socket=socket,
        invoked_as="vesma service start",
        install_hint="vesma service components install --all",
        registry_hint="vesma service components status",
    )


@service_app.command("stop")
def stop(
    component: Annotated[
        str | None,
        typer.Argument(help="Component name from the manifest registry.", show_default=False),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Skip the graceful phase (SIGKILL after a short delay).")
    ] = False,
    all_components: Annotated[
        bool,
        typer.Option("--all", help="Stop every installed component (reverse dependency order)."),
    ] = False,
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Control socket override (tests).")
    ] = None,
) -> None:
    """Stop a component — or every installed one with --all (reverse topo).

    Graceful first (SIGTERM, short grace period); `--force` skips the
    graceful phase and SIGKILLs after a short delay — for a wedged
    process only, since in-flight work is lost. Idempotent per
    component: an already-stopped component is reported as-is. `--all`
    stops in REVERSE dependency order (dependents torn down before what
    they depend on) — lifted here from the former top-level `vesma stop
    --all` (CLI topology v2 wave B). The hidden top-level `vesma stop`
    alias calls the SAME function.
    """
    from vesma.cli.components import stop_command

    stop_command(
        component,
        all_components=all_components,
        socket=socket,
        force=force,
        invoked_as="vesma service stop",
        install_hint="vesma service components install --all",
        registry_hint="vesma service components status",
    )


@service_app.command("restart")
def restart(
    component: Annotated[
        str | None,
        typer.Argument(help="Component name from the manifest registry.", show_default=False),
    ] = None,
    all_components: Annotated[
        bool,
        typer.Option("--all", help="Restart every installed component (dependency order)."),
    ] = False,
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Control socket override (tests).")
    ] = None,
) -> None:
    """Restart a component — or every installed one with --all (topo order).

    A restart is a real stop-then-start cycle, not a no-op replay: unlike
    start/stop it is NOT idempotent — a retry after a lost response
    performs a second restart. `--all` restarts in dependency order —
    lifted here from the former top-level `vesma restart --all` (CLI
    topology v2 wave B). The hidden top-level `vesma restart` alias
    calls the SAME function. Confirm readiness with `vesma service
    health`.
    """
    from vesma.cli.components import restart_command

    restart_command(
        component,
        all_components=all_components,
        socket=socket,
        invoked_as="vesma service restart",
        install_hint="vesma service components install --all",
        registry_hint="vesma service components status",
    )


@service_app.command("logs")
def logs(
    component: Annotated[str, typer.Argument(help="Component name from the manifest registry.")],
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Stream new lines until the component stops.")
    ] = False,
    tail: Annotated[
        int,
        typer.Option("--tail", "-n", min=1, max=MAX_TAIL, help=f"Last N lines (<= {MAX_TAIL})."),
    ] = 100,
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Control socket override (tests).")
    ] = None,
) -> None:
    """Show recent component logs; --follow streams until the source stops.

    Prints the last `--tail` lines (default 100, capped at 10000 — the
    same cap the `--tail` option enforces) from the component's log
    source, or keeps streaming new lines with `--follow`/`-f` until the
    component stops. The canonical home of component logs since CLI
    topology v2 (wave B): the former top-level `vesma logs NAME` verb
    moved here; log lines are OPERATOR DATA rendered through Text() —
    never through rich markup (a child emitting "[red]…" must not style
    the trusted CLI) — and an EMPTY result says so explicitly with the
    source named: silence is never an answer.
    """
    if follow:
        _with_client(
            socket,
            lambda client: client.follow_logs(
                component, lambda line: console.print(Text(line)), tail=tail
            ),
        )
        return
    lines = _with_client(socket, lambda client: client.logs(component, tail=tail))
    if not lines:
        console.print(f"no log lines available for {component} ({LOG_SOURCE_NOTE})", style="yellow")
        return
    for line in lines:
        console.print(Text(line))


# ── verb: run — the supervisor entrypoint (server side) ───────────────


def _load_manifests() -> dict[str, Any]:
    """Installation manifests; the bundled pack when nothing is installed."""
    from vesma.service import load_bundled_manifest, load_installation

    components_dir = layout.components_dir()
    if components_dir.is_dir():
        return dict(load_installation(components_dir))
    bundled = ("board", "metrics")
    return {name: load_bundled_manifest(name, set(bundled)) for name in bundled}


@service_app.command("run")
def run(
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Control socket override (tests).")
    ] = None,
    config: Annotated[
        str | None,
        typer.Option("--config", "-c", help="Path to config.yaml (bind/port of the core API)."),
    ] = None,
) -> None:
    """Run the vesma service: supervisor + control socket + in-process core.

    The memory-server core (the same app ``vesma serve`` runs) is embedded
    IN THIS PROCESS as the supervisor's own heart (service-lifecycle v1
    §3.1): death of the core = death of the supervisor (fail-fast, exit 1,
    the systemd unit restarts — there is deliberately no core-restart
    mechanism). Board and children come from their manifests.

    Single-instance: when a live supervisor already answers on the socket,
    exits 0 with a message (control-socket v1 §4.2 step 2).
    """
    from vesma.cli._manager import load_settings_or_exit
    from vesma.logging_setup import setup_logging
    from vesma.service.backend import CoreStartupError, ServiceApp

    try:
        manifests = _load_manifests()
    except Exception as exc:
        _fail(f"cannot load the installation manifests: {exc}")
    if not manifests:
        _fail("no component manifests found — install components first (wave W4)")

    settings = load_settings_or_exit(config)
    setup_logging(settings)
    host, port = settings.api.host, settings.api.port
    if settings.runtime.uvicorn_workers != 1:
        # The in-process core is ONE process by contract (SL §3.1); the
        # multiprocess serve profile does not apply here — say so loudly.
        console.print(
            "[yellow]warning[/yellow] runtime.uvicorn_workers="
            f"{settings.runtime.uvicorn_workers} ignored by `service run`: "
            "the in-process core is always single-process (workers=1)"
        )

    app = ServiceApp(
        manifests,
        socket_path=socket,
        core_bind=(host, port),
        log=logger,
        config=config,
        component_configs=dict(settings.components),
    )
    try:
        outcome = app.bind()
    except Exception as exc:
        _fail(f"control socket bind failed: {exc}")
    if outcome.status == "already-running":
        console.print(f"vesma service is already running (socket: {outcome.path})")
        raise typer.Exit(code=0)
    # ADR-0049 Lock 1 (wave 0): the supervisor is THE writer of its store
    # home — the per-home exclusive flock is held for the whole process
    # lifetime, AFTER the endpoint guard (the same-endpoint case answers
    # «already running» above; a DIFFERENT-endpoint supervisor over the
    # SAME home refuses here with the holder's name).
    lock_endpoint = f"service control={outcome.path} core=http://{host}:{port}"
    try:
        with writer_lock(settings.vesma.data_dir, endpoint=lock_endpoint):
            try:
                app.start()
            except CoreStartupError as exc:
                _fail(str(exc))
            core_part = f", core: http://{host}:{port}"
            if app.core_port is not None:  # port 0 resolved to the ephemeral port
                core_part = f", core: http://{host}:{app.core_port}"
            console.print(
                f"vesma service running (socket: {outcome.path}, pid: {os.getpid()}{core_part})"
            )
            code = app.wait()
            console.print(f"vesma service stopped (exit code: {code})")
    except WriterLockBusyError as exc:
        _fail(str(exc))
    raise typer.Exit(code=code)


if __name__ == "__main__":  # pragma: no cover — manual invocation
    service_app()
