"""vesmaro.service.board — the in-process read-only status panel.

The bundled manifest (``components/board.yaml``) declares this module as
``in_process`` with the entrypoint factory ``create_component`` (CM §3.6:
STRICTLY ``() -> Component``) and the health callback
``vesmaro.service.board:health``.

Supervisor wiring (first-party convention, CM §3.6 factory has no args so
config travels through hooks):

- ``register_config(config)`` — the supervisor calls this BEFORE the
  factory with the validated component config. The factory starts the
  HTTP server ONLY when a config was registered this way; a bare import
  (tests, tooling, doctor) stays dormant and side-effect-free.
- ``register_state_provider(snapshot)`` — called after the factory; the
  panel serves whatever read-only snapshot callable it receives
  (:meth:`vesmaro.service.supervisor.Supervisor.snapshot`).

Surface (READ-ONLY — no control endpoints; control is the W3 socket's
domain, specs/control-socket/v1):

- ``GET /``       — minimal HTML overview of the component states;
- ``GET /status`` — the supervisor snapshot as JSON;
- ``GET /healthz``— 200 + ``{"health": healthy|degraded}`` while the panel
  itself is serving (both values are 200: the panel being reachable IS the
  liveness signal; the supervisor health value travels in the body).

Fail-isolation (W2 brief): if the serving thread dies, ``health()``
reports ``state: failed`` — the supervisor's callback-health path turns
that into T7 degradation while the supervisor itself stays alive.

Stdlib only — the supervisor imports this module in-process.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "BoardComponent",
    "create_component",
    "health",
    "register_config",
    "register_state_provider",
]

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8080

StateProvider = Callable[[], dict[str, Any]]

_config: dict[str, Any] | None = None
_panel: _Panel | None = None
_state_provider: StateProvider | None = None


def register_config(config: dict[str, Any]) -> None:
    """Supervisor hook — the validated component config, before the factory."""
    global _config
    _config = dict(config)


def register_state_provider(provider: StateProvider) -> None:
    """Supervisor hook — the read-only snapshot callable, after the factory."""
    global _state_provider
    _state_provider = provider


def _snapshot() -> dict[str, Any]:
    provider = _state_provider
    if provider is None:
        return {}
    try:
        return provider()
    except Exception:  # a broken provider must not take the panel down
        return {}


def health() -> dict[str, str]:
    """Health callback (CM §3.7: ``() -> {state, detail?}``).

    Dormant (no server started) and serving both read ``healthy`` — with
    NO ``detail`` key so the record is exactly ``{"state": "healthy"}``;
    a dead serving thread reads ``failed`` (the supervisor's callback
    boundary maps it to T7 degraded — the supervisor itself stays alive).
    """
    panel = _panel
    if panel is not None and not panel.serving:
        return {"state": "failed", "detail": "board server thread is not running"}
    return {"state": "healthy"}


class BoardComponent:
    """The Component handle the entrypoint factory returns (CM §3.6)."""

    name = "board"
    kind = "in-process"

    def __init__(self) -> None:
        global _panel
        self._panel: _Panel | None = None
        if _config is not None:
            # Supervisor path only: config registered → serve. A bare
            # factory call (tests/tooling) never binds a port.
            self._panel = _Panel(_config)
            self._panel.start()
        _panel = self._panel

    def shutdown(self) -> None:
        """Graceful stop — the supervisor calls this on component stop."""
        if self._panel is not None:
            self._panel.shutdown()

    @property
    def port(self) -> int | None:
        """The bound port (``None`` while dormant; 0 resolved to the real one)."""
        if self._panel is None:
            return None
        return self._panel.port


def create_component() -> BoardComponent:
    """Entrypoint factory required by the manifest (strictly ``() -> Component``)."""
    return BoardComponent()


class _BoardServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that picks the address family from the host."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        if ":" in address[0]:  # IPv6 literal (schema allows loopback ::1 only)
            self.address_family = socket.AF_INET6
        super().__init__(address, handler)


def _parse_bind(bind: str) -> tuple[str, int]:
    """``bind`` config → (host, port).

    Schema-validated forms (board.yaml pattern — loopback-only v1 posture):
    ``host``, ``host:port``, ``::1``, ``[::1]:port``. A port-less bind uses
    the default port; a bare IPv6 literal is never split mid-address.
    """
    if bind.startswith("["):  # bracketed IPv6 literal
        host_part, _, rest = bind.partition("]")
        host = host_part.removeprefix("[")
        port_s = rest[1:] if rest.startswith(":") else ""
    else:
        host, sep, port_s = bind.rpartition(":")
        # A bare IPv6 literal ("::1") is never split mid-address; a port-less
        # host uses the default port.
        if bind.count(":") > 1 or not sep or not port_s.isdigit():
            host, port_s = bind, ""
    return (host or _DEFAULT_HOST, int(port_s) if port_s else _DEFAULT_PORT)


class _Panel:
    """ThreadingHTTPServer wrapper serving the read-only surface."""

    def __init__(self, config: dict[str, Any]) -> None:
        bind = str(config.get("bind", f"{_DEFAULT_HOST}:{_DEFAULT_PORT}"))
        self._server = _BoardServer(_parse_bind(bind), _BoardHandler)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="vesma-board-http",
            daemon=True,
        )
        self._thread.start()

    @property
    def serving(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def port(self) -> int:
        address: tuple[str, int] = self._server.server_address  # type: ignore[assignment]
        return address[1]

    def shutdown(self) -> None:
        # shutdown() joins the serve_forever loop; never call it from the
        # serving thread itself (the supervisor's stop path does not).
        self._server.shutdown()
        self._server.server_close()


class _BoardHandler(BaseHTTPRequestHandler):
    """Read-only request handler: GET/HEAD only, everything else 405."""

    server_version = "vesma-board"
    sys_version = ""

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._send_html()
        elif path == "/status":
            self._send_json(_snapshot())
        elif path == "/healthz":
            supervisor = _snapshot().get("supervisor")
            value = (
                supervisor.get("health", "unknown") if isinstance(supervisor, dict) else "unknown"
            )
            self._send_json({"health": value})
        else:
            self._send_json({"error": f"no such path: {path}"}, code=404)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        self._method_not_allowed()

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self._send_json({"error": "read-only panel: method not allowed"}, code=405)

    def _send_json(self, payload: dict[str, Any], *, code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_html(self) -> None:
        snapshot = _snapshot()
        rows = "".join(
            f"<tr><td>{name}</td><td>{entry.get('state', '?')}</td>"
            f"<td>{entry.get('pid', 'none')}</td></tr>"
            for name, entry in sorted(snapshot.get("components", {}).items())
            if isinstance(entry, dict)
        )
        supervisor_health = "unknown"
        supervisor = snapshot.get("supervisor")
        if isinstance(supervisor, dict):
            supervisor_health = str(supervisor.get("health", "unknown"))
        body = (
            "<!doctype html><html><head><title>vesma board</title></head><body>"
            f"<h1>vesma board</h1><p>supervisor health: {supervisor_health}</p>"
            "<table border=1><tr><th>component</th><th>state</th><th>pid</th></tr>"
            f"{rows}</table>"
            "<p>read-only surface; see /status and /healthz</p>"
            "</body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Quiet by default: request logging would spam the supervisor's own
        # stream; stderr noise from an in-process module buys nothing.
        return None
