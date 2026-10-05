"""``vesma service`` CLI — install/uninstall (W4) + supervisor control plane (W3).

Two command groups share this sub-app:

* ``install`` / ``uninstall`` — the install flow (pack manifests, data
  dirs, venvs, the generated systemd user unit; wave W4).
* ``status`` / ``health`` / ``start`` / ``stop`` / ``restart`` / ``logs``
  — the thin client over the control socket (control-socket v1 §4.8),
  plus ``run`` — the supervisor entrypoint that owns the socket and the
  component tree in one process (wave W3).

MERGE NOTE (W3/W4 collision): both waves created this file; the resolved
shape is the concatenation of the two groups — one command function per
verb, no shared mutable state.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypeVar

import typer
from rich.console import Console
from rich.text import Text

from vesmaro.service import layout
from vesmaro.service.client import (
    ControlClient,
    ControlClientError,
    PreflightError,
    ProtocolError,
    ResponseTimeoutError,
    SocketUnavailableError,
    control_socket_path,
)
from vesmaro.service.control import (
    MAX_TAIL,
    ControlBackend,
    ControlServer,
    UnknownComponentError,
)
from vesmaro.service.errors import ManifestError
from vesmaro.service.install import InstallError, install, uninstall

service_app = typer.Typer(
    name="service",
    help=(
        "Install/uninstall the vesma service and drive the supervisor "
        "control plane: status, start/stop/restart, logs, run."
    ),
    no_args_is_help=True,
)

console = Console()
logger = logging.getLogger("vesmaro.cli.service")

F = TypeVar("F", bound=Callable[..., Any])


# ── install/uninstall (wave W4) ───────────────────────────────────────


def _fail_install(exc: Exception) -> NoReturn:
    # Text(exc): report DATA must never pass through rich markup — an
    # exception message containing '[' is operator text, not formatting.
    console.print("[red]✗[/red]", Text(str(exc)))
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
        _fail_install(exc)
    for line in result.lines:
        downgraded_line = line.startswith("CONTAINER DOWNGRADE")
        style = "[yellow]⚠[/yellow]" if downgraded_line else "[green]✓[/green]"
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
) -> None:
    """Uninstall one component, or the whole installation with --all.

    Only files the install flow owns are removed (manifest, env file,
    venv). Component data dirs are operator data and are preserved.
    """
    try:
        result = uninstall(name, remove_all=remove_all)
    except (InstallError, ManifestError) as exc:
        _fail_install(exc)
    for line in result.lines:
        warn_line = line.startswith("WARN:")
        style = "[yellow]⚠[/yellow]" if warn_line else "[green]✓[/green]"
        console.print(style, Text(line))
    console.print("[green]Uninstall complete.[/green]")


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
    """Show the component state tree (or one component)."""
    result = _with_client(socket, lambda client: client.status(component))
    console.print_json(json.dumps(result))


@service_app.command("health")
def health(
    component: Annotated[str | None, typer.Argument(help="Optional single component.")] = None,
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Control socket override (tests).")
    ] = None,
) -> None:
    """Show global and per-component health (or one component)."""
    result = _with_client(socket, lambda client: client.health(component))
    console.print_json(json.dumps(result))


@service_app.command("start")
def start(
    component: Annotated[str, typer.Argument(help="Component name from the manifest registry.")],
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Control socket override (tests).")
    ] = None,
) -> None:
    """Start a component (idempotent: already-running is not an error)."""
    state = _with_client(socket, lambda client: client.start(component))
    console.print(f"{component}: {state}")


@service_app.command("stop")
def stop(
    component: Annotated[str, typer.Argument(help="Component name from the manifest registry.")],
    force: Annotated[
        bool, typer.Option("--force", help="Skip the graceful phase (SIGKILL after a short delay).")
    ] = False,
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Control socket override (tests).")
    ] = None,
) -> None:
    """Stop a component (idempotent: already-stopped is not an error)."""
    state = _with_client(socket, lambda client: client.stop(component, force=force))
    console.print(f"{component}: {state}")


@service_app.command("restart")
def restart(
    component: Annotated[str, typer.Argument(help="Component name from the manifest registry.")],
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Control socket override (tests).")
    ] = None,
) -> None:
    """Restart a component (not idempotent by contract)."""
    state = _with_client(socket, lambda client: client.restart(component))
    console.print(f"{component}: {state}")


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
    """Show recent component logs; --follow streams until the source stops."""
    if follow:
        _with_client(
            socket,
            lambda client: client.follow_logs(
                component, lambda line: console.print(line), tail=tail
            ),
        )
        return
    lines = _with_client(socket, lambda client: client.logs(component, tail=tail))
    for line in lines:
        console.print(line)


# ── verb: run — the supervisor entrypoint (server side) ───────────────


class _SupervisorRunBackend:
    """W3 seam backend over the W2 Supervisor (replaced by the W6 adapter).

    Today's Supervisor exposes the whole-installation surface
    (``snapshot()``/``global_health``) plus installation-wide start; the
    per-component verbs and log streaming need the W6 adapter. Missing
    mappings raise, which the server maps to error 500 with the reason in
    the supervisor log (never in client-visible data — §4.6 hygiene).
    """

    def __init__(self, supervisor: Any) -> None:
        self._supervisor = supervisor

    def component_status(self, component: str | None) -> dict[str, Any]:
        snapshot: dict[str, Any] = dict(self._supervisor.snapshot())
        if component is None:
            return snapshot
        components = snapshot.get("components", {})
        if component not in components:
            raise UnknownComponentError(component)
        return {"components": {component: components[component]}}

    def health(self, component: str | None) -> dict[str, Any]:
        raise RuntimeError("health aggregation requires the W6 supervisor adapter")

    def start_component(self, component: str) -> str:
        raise RuntimeError("per-component start requires the W6 supervisor adapter")

    def stop_component(self, component: str, *, force: bool) -> str:
        raise RuntimeError("per-component stop requires the W6 supervisor adapter")

    def restart_component(self, component: str) -> str:
        raise RuntimeError("per-component restart requires the W6 supervisor adapter")

    def component_logs(self, component: str, *, tail: int) -> list[str]:
        raise RuntimeError("log serving requires the W6 supervisor adapter")

    def stream_logs(self, component: str, *, tail: int) -> Any:
        raise RuntimeError("log streaming requires the W6 supervisor adapter")


def _load_supervisor() -> type[Any] | None:
    """The documented ``vesmaro.service.Supervisor`` export (W2 branch).

    Dynamic lookup on purpose: this branch predates the W2 merge, and the
    import surface stays stable across the merge.
    """
    import vesmaro.service as service_pkg

    return getattr(service_pkg, "Supervisor", None)


def _load_manifests() -> dict[str, Any]:
    """Installation manifests; the bundled pack when nothing is installed."""
    from vesmaro.service import load_bundled_manifest, load_installation

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
) -> None:
    """Run the supervisor: own the control socket and the component tree.

    Single-instance: when a live supervisor already answers on the socket,
    exits 0 with a message (control-socket v1 §4.2 step 2).
    """
    supervisor_cls = _load_supervisor()
    if supervisor_cls is None:
        _fail(
            "the supervisor module is not part of this build yet (wave W2 not merged); "
            "`vesma service run` becomes available once vesmaro.service exports Supervisor"
        )
    try:
        manifests = _load_manifests()
    except Exception as exc:
        _fail(f"cannot load the installation manifests: {exc}")
    if not manifests:
        _fail("no component manifests found — install components first (wave W4)")

    supervisor = supervisor_cls(manifests)
    backend: ControlBackend = _SupervisorRunBackend(supervisor)
    server = ControlServer(backend, log=logger)
    try:
        outcome = server.bind() if socket is None else server.bind_at(socket)
    except Exception as exc:
        _fail(f"control socket bind failed: {exc}")
    if outcome.status == "already-running":
        console.print(f"vesma service is already running (socket: {outcome.path})")
        raise typer.Exit(code=0)
    supervisor.start()
    console.print(f"vesma service running (socket: {outcome.path}, pid: {os.getpid()})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("shutdown requested (SIGINT)")
    finally:
        server.stop()
        supervisor.request_shutdown()
        time.sleep(0.2)  # grace for the reaper thread; W6 refines the join


if __name__ == "__main__":  # pragma: no cover — manual invocation
    service_app()
