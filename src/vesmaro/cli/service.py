"""``vesma service`` CLI — supervisor control plane (control-socket v1 §4.8).

Thin client over the control socket: status / start / stop / restart /
logs (+ additive health), plus ``run`` — the supervisor entrypoint that
owns the socket and the component tree in one process.

MERGE NOTE (W3/W4 collision): this file is intentionally group-structured,
one command function per verb with no shared mutable state. Wave W4 owns
``install``/``uninstall`` — whoever merges second keeps both function sets
and resolves the one-file conflict by concatenation of the two groups.
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

service_app = typer.Typer(
    name="service",
    help="Supervisor control plane: status, start/stop/restart, logs, run.",
    no_args_is_help=True,
)

console = Console()
logger = logging.getLogger("vesmaro.cli.service")

F = TypeVar("F", bound=Callable[..., Any])


# ── shared plumbing ───────────────────────────────────────────────────


def _open_client(socket: Path | None) -> ControlClient:
    """Connect a thin client (§4.1 pre-flight inside ``connect``)."""
    client = ControlClient(socket or control_socket_path()[0])
    client.connect()
    return client


def _fail(message: str) -> NoReturn:
    console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(code=1) from None


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
