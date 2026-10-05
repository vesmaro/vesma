"""The W6 integration adapter: Supervisor ↔ control socket ↔ in-process core.

Wires the three surfaces of ``vesma service run`` together without forking
any engine logic:

- :class:`ComponentLogBuffer` — an in-memory per-component ring buffer that
  sits IN FRONT of the real logsink (composite): the supervisor's
  structural lines (identifier ``vesma-supervisor``, component routed from
  the ``component=`` field of the §3.4 line) and child output forwards
  (identifier ``vesma-<component>``) both land here, so the control
  socket's ``logs``/``logs follow`` verbs serve the supervisor's own log
  buffer as control-socket v1 §4.5 intends;
- :class:`SupervisorBackend` — the REAL ``ControlBackend`` over the W2
  ``Supervisor``: status/health from the FSM snapshot, start/stop/restart
  with the §4.5 idempotence markers and the §4.6 lifecycle error mapping
  (100/101/102/103), logs from the buffer;
- :class:`ServiceApp` — the full ``service run`` composition (SL §3.1):
  the supervisor owns the control socket and the component tree; the
  memory-server core (the same ``vesmaro.api.main:app`` the ``serve``
  command runs, via uvicorn — workers pinned to 1) is embedded IN-PROCESS
  as the supervisor's own heart. Death of the core = death of the
  supervisor (fail-fast, exit code 1 — systemd ``Restart=on-failure``
  restarts the unit); there is deliberately NO separate core-restart
  mechanism (SL §3.1, зафиксировано сознательно). The board stays an
  in-process component via its manifest; children come from manifests.

Teardown order (SL §3.5): control socket closes first (no new commands
during teardown), then the supervisor stops every child
reverse-topologically and reaps, then the core drains and the process
exits — 0 on a clean SIGTERM, 1 on core death.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from vesmaro.service.control import (
    MAX_TAIL,
    BindResult,
    ControlServer,
    LogStream,
    StartFailedError,
    StopTimeoutError,
    UnknownComponentError,
)
from vesmaro.service.fsm import ChildState
from vesmaro.service.logsink import Logsink
from vesmaro.service.supervisor import Supervisor

logger = logging.getLogger("vesmaro.service.backend")

#: Ring capacity per component (= MAX_TAIL: the ``logs`` verb can never ask
#: for more than the buffer keeps).
RING_CAPACITY = MAX_TAIL

#: Bounded wait for a start to LEAVE stopped/blocked after ``request_start``
#: before the current FSM value is answered as-is (must stay below the 10 s
#: response timeout of CS §4.7).
START_SETTLE_S = 5.0

#: Bounded wait for a stop to reach ``stopped`` after ``request_stop``
#: returns (the supervisor's stop is synchronous; this is the honesty
#: check, not the grace window).
STOP_SETTLE_S = 2.0

#: States in which the component instance (or its scheduled respawn) is
#: alive or owned by the supervision loop — ``start`` answers
#: ``already-running`` for these (CS §4.5 idempotence).
_LIVE_STATES: frozenset[ChildState] = frozenset(
    {ChildState.STARTING, ChildState.HEALTHY, ChildState.DEGRADED, ChildState.BACKOFF}
)

#: state → CS §4.5 health value (deterministic mapping; the supervisor-level
#: global health lives on the snapshot's ``supervisor.health``).
_HEALTH_BY_STATE: dict[ChildState, str] = {
    ChildState.HEALTHY: "healthy",
    ChildState.DEGRADED: "degraded",
}

_SUPERVISOR_IDENTIFIER = "vesma-supervisor"
_CHILD_IDENTIFIER_PREFIX = "vesma-"
_COMPONENT_FIELD = "component="


def health_of_state(state: ChildState | str) -> str:
    """FSM state → CS §4.5 health value: healthy/degraded keep their name,
    every other state (starting/backoff/stopped/blocked) is ``down``."""
    key = state if isinstance(state, ChildState) else ChildState(state)
    return _HEALTH_BY_STATE.get(key, "down")


class ComponentLogBuffer:
    """In-memory per-component log rings + the ``Logsink`` front half.

    Routing (single writer per line — the supervisor's emit paths):
    the supervisor's own structural lines arrive under identifier
    ``vesma-supervisor`` and are routed by the ``component=<name>`` field
    of the §3.4 grammar; child output arrives as ``vesma-<component>``.
    Everything else is ignored (unknown components, foreign identifiers).
    """

    def __init__(self, component_names: list[str] | tuple[str, ...]) -> None:
        self._rings: dict[str, deque[str]] = {
            name: deque(maxlen=RING_CAPACITY) for name in component_names
        }
        self._cond = threading.Condition()

    # ── Logsink protocol (front half) ──

    def emit(self, identifier: str, line: str, *, severity: str = "INFO") -> None:
        del severity  # the wire format carries the line text only
        name = self._route(identifier, line)
        if name is None:
            return
        with self._cond:
            self._rings[name].append(line)
            self._cond.notify_all()

    def close(self) -> None:
        return None

    # ── read side ──

    def tail(self, component: str, *, tail: int) -> list[str]:
        """Last ``tail`` lines of the component (CS §4.5 logs, follow=false)."""
        with self._cond:
            ring = self._rings[component]
            return list(ring)[-tail:] if tail < len(ring) else list(ring)

    def subscribe(
        self,
        component: str,
        state_of: Any,  # Callable[[], str] — the live FSM state reader
        *,
        tail: int,
    ) -> LogStream:
        """Open a follow subscription (CS §4.5 logs, follow=true): replay up
        to ``tail`` buffered lines first, then stream new ones until the
        source stops (final answer carries the FSM state)."""
        return _BufferedLogStream(self, component, state_of, tail=tail)

    def _route(self, identifier: str, line: str) -> str | None:
        if identifier == _SUPERVISOR_IDENTIFIER:
            # structural line: "vesma.supervisor component=<name> event=…"
            if not line.startswith("vesma.supervisor "):
                return None
            for field in line.split(" ")[2:]:
                if field.startswith(_COMPONENT_FIELD):
                    name = field[len(_COMPONENT_FIELD) :]
                    return name if name in self._rings else None
            return None
        if identifier.startswith(_CHILD_IDENTIFIER_PREFIX):
            name = identifier[len(_CHILD_IDENTIFIER_PREFIX) :]
            if name != "supervisor" and name in self._rings:
                return name
        return None


class _BufferedLogStream:
    """One follow subscription over a component ring (``LogStream``)."""

    def __init__(
        self, buffer: ComponentLogBuffer, component: str, state_of: Any, *, tail: int
    ) -> None:
        self._buffer = buffer
        self._component = component
        self._state_of = state_of
        with buffer._cond:
            ring = buffer._rings[component]
            self._pos = max(0, len(ring) - min(tail, len(ring)))
        self._closed = False

    def poll(self, timeout: float) -> list[str]:
        with self._buffer._cond:
            ring = self._buffer._rings[self._component]
            if self._pos >= len(ring) and not self._closed:
                self._buffer._cond.wait(timeout)
            if self._closed:
                return []
            lines = list(ring)[self._pos :]
            self._pos += len(lines)
            return lines

    def current_state(self) -> str:
        return str(self._state_of())

    @property
    def final_state(self) -> str | None:
        """The §4.5 final answer once the source stopped; ``None`` before."""
        if self._state_of() == ChildState.STOPPED.value:
            return ChildState.STOPPED.value
        return None

    def close(self) -> None:
        self._closed = True
        with self._buffer._cond:
            self._buffer._cond.notify_all()


class CompositeLogsink:
    """Buffer first (never fails), then the real sink (journald/files)."""

    def __init__(self, buffer: ComponentLogBuffer, downstream: Logsink) -> None:
        self._buffer = buffer
        self._downstream = downstream

    def emit(self, identifier: str, line: str, *, severity: str = "INFO") -> None:
        self._buffer.emit(identifier, line, severity=severity)
        self._downstream.emit(identifier, line, severity=severity)

    def close(self) -> None:
        self._downstream.close()


class SupervisorBackend:
    """The real ``ControlBackend`` over the W2 Supervisor (the W3 seam, kept).

    Error mapping (CS §4.6): unknown name → 100, forbidden transition → 101,
    refused/failing start → 102 (``data.reason`` = the §3.4 refusal token),
    stop that never reached ``stopped`` → 103. Error ``data`` stays inside
    the §4.6 hygiene class (names, states, tokens — never paths/env/argv).
    """

    def __init__(
        self,
        supervisor: Supervisor,
        buffer: ComponentLogBuffer,
        *,
        start_settle_s: float = START_SETTLE_S,
        stop_settle_s: float = STOP_SETTLE_S,
    ) -> None:
        self._supervisor = supervisor
        self._buffer = buffer
        self._start_settle_s = start_settle_s
        self._stop_settle_s = stop_settle_s

    # ── status / health (CS §4.5) ──

    def component_status(self, component: str | None) -> dict[str, Any]:
        snapshot: dict[str, Any] = self._supervisor.snapshot()
        components: dict[str, Any] = snapshot.get("components", {})
        if component is None:
            return {
                "supervisor": snapshot["supervisor"],
                "components": {
                    name: self._component_entry(name, entry) for name, entry in components.items()
                },
            }
        entry = components.get(component)
        if entry is None:
            raise UnknownComponentError(component)
        return {"components": {component: self._component_entry(component, entry)}}

    def health(self, component: str | None) -> dict[str, Any]:
        snapshot: dict[str, Any] = self._supervisor.snapshot()
        components: dict[str, Any] = snapshot.get("components", {})
        if component is None:
            return {
                "health": str(snapshot["supervisor"]["health"]),
                "components": {
                    name: health_of_state(str(entry["state"])) for name, entry in components.items()
                },
            }
        entry = components.get(component)
        if entry is None:
            raise UnknownComponentError(component)
        return {"health": health_of_state(str(entry["state"]))}

    def _component_entry(self, name: str, entry: dict[str, Any]) -> dict[str, Any]:
        # Additive fields are allowed (CS §4.5): the snapshot's state/pid/
        # restarts/detail stay, health is derived from the FSM state.
        return {**entry, "health": health_of_state(str(entry["state"]))}

    # ── lifecycle verbs (CS §4.5) ──

    def start_component(self, component: str) -> str:
        self._require_known(component)
        state = self._state(component)
        if state in _LIVE_STATES:
            return "already-running"
        self._supervisor.request_start(component)
        deadline = time.monotonic() + self._start_settle_s
        while time.monotonic() < deadline:
            reason = self._supervisor.refusal_reason(component)
            if reason is not None:
                raise StartFailedError(component, reason)
            state = self._state(component)
            if state in _LIVE_STATES:
                return state.value
            time.sleep(0.05)
        # Still stopped/blocked: blocked is the truthful T2 answer; a bare
        # stopped (thread parked) is answered as-is — never invented states.
        return self._state(component).value

    def stop_component(self, component: str, *, force: bool) -> str:
        self._require_known(component)
        if self._state(component) is ChildState.STOPPED:
            return "already-stopped"
        self._supervisor.request_stop(component, force=force)
        deadline = time.monotonic() + self._stop_settle_s
        while time.monotonic() < deadline:
            if self._state(component) is ChildState.STOPPED and self._process_gone(component):
                return ChildState.STOPPED.value
            time.sleep(0.05)
        raise StopTimeoutError(component)

    def restart_component(self, component: str) -> str:
        # Not idempotent by contract (CS §4.5): stop then start. A stopped
        # source stops as already-stopped and the start leg proceeds.
        self.stop_component(component, force=False)
        return self.start_component(component)

    # ── logs (CS §4.5) ──

    def component_logs(self, component: str, *, tail: int) -> list[str]:
        self._require_known(component)
        return self._buffer.tail(component, tail=tail)

    def stream_logs(self, component: str, *, tail: int) -> LogStream:
        self._require_known(component)
        return self._buffer.subscribe(component, lambda: self._state(component).value, tail=tail)

    # ── internals ──

    def _require_known(self, component: str) -> None:
        if self._supervisor.component_state(component) is None:
            raise UnknownComponentError(component)

    def _state(self, component: str) -> ChildState:
        state = self._supervisor.component_state(component)
        if state is None:
            raise UnknownComponentError(component)
        return state

    def _process_gone(self, component: str) -> bool:
        """True when the child's last pid is absent from /proc or zombied
        (reaped-in-flight); in-process components have no pid — gone."""
        entry = self._supervisor.snapshot()["components"].get(component, {})
        pid = entry.get("pid")
        if pid is None:
            return True
        try:
            with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as fh:
                state_char = fh.read().rpartition(")")[2].split()[0]
        except (OSError, ValueError, IndexError):
            return True
        return state_char == "Z"


class CoreStartupError(Exception):
    """The embedded in-process core failed to start (fail-fast, SL §3.1)."""


class ServiceApp:
    """The ``vesma service run`` composition: supervisor + control socket +
    in-process core, one process (SL §3.1).

    Threading map (main thread returns from :meth:`wait` only at teardown):

    - main caller thread — orchestration; runs the final graceful stop;
    - control accept thread — ``ControlServer.serve_forever``;
    - core thread — uvicorn ``Server.run`` (uvicorn skips its own signal
      handlers off the main thread; the supervisor keeps them);
    - core watcher — core-thread join; an exit that nobody ordered is core
      DEATH: exit code 1 + supervisor shutdown request (fail-fast);
    - shutdown watcher — polls the supervisor's shutdown flag, then closes
      the control socket and asks the core to drain.

    Teardown order: socket close → supervisor.shutdown() (reverse-
    topological child stop + reap) → core drain → exit code
    (0 clean, 1 core death).
    """

    def __init__(
        self,
        manifests: dict[str, Any],
        *,
        socket_path: str | os.PathLike[str] | None = None,
        core_bind: tuple[str, int] | None = None,
        with_core: bool = True,
        log: logging.Logger | None = None,
    ) -> None:
        self._log = log or logger
        self._with_core = with_core
        self._core_bind = core_bind
        self._exit_code = 0
        self._finalized = False
        self._core_server: Any | None = None  # uvicorn.Server (lazy import)
        self._core_thread: threading.Thread | None = None
        self._accept_thread: threading.Thread | None = None

        names = list(manifests)
        self.buffer = ComponentLogBuffer(names)
        from vesmaro.service.logsink import HistoryJournal, make_logsink

        self._sink: Logsink = CompositeLogsink(self.buffer, make_logsink())
        self.supervisor = Supervisor(manifests, logsink=self._sink, journal=HistoryJournal())
        self.backend = SupervisorBackend(self.supervisor, self.buffer)
        self.server = ControlServer(self.backend, log=self._log)
        self._socket_override = str(socket_path) if socket_path is not None else None

    # ── lifecycle ──

    def bind(self) -> BindResult:
        """The CS §4.2 bind (single-instance guard surfaces as
        ``already-running``)."""
        if self._socket_override is not None:
            return self.server.bind_at(Path(self._socket_override))
        return self.server.bind()

    def start(self) -> None:
        """Start the pieces in dependency order: control socket serving
        first (management availability is the §3.2 invariant), then the
        in-process core (children must not race it — it is their API), then
        the supervisor (children spawn topologically), then the watchers."""
        if self._core_bind is None and self._with_core:
            raise ValueError("with_core=True requires core_bind (host, port)")
        self._accept_thread = threading.Thread(
            target=self.server.serve_forever, name="vesma-control-accept", daemon=True
        )
        self._accept_thread.start()
        if self._with_core:
            try:
                self._start_core()
            except CoreStartupError:
                self.server.stop()  # accept loop exits → socket file cleaned
                raise
        self.supervisor.start()
        threading.Thread(target=self._watch_core, name="vesma-core-watcher", daemon=True).start()
        threading.Thread(
            target=self._watch_supervisor, name="vesma-shutdown-watcher", daemon=True
        ).start()

    def wait(self) -> int:
        """Block until the service stopped; finalize and return the exit
        code (0 clean / 1 core death)."""
        thread = self._accept_thread
        if thread is not None:
            thread.join()
        return self._finalize()

    def request_stop(self) -> None:
        """Order the graceful teardown from outside (tests; the SIGTERM path
        goes through the supervisor's own signal handler instead)."""
        self.supervisor.request_shutdown()

    @property
    def core_port(self) -> int | None:
        """The resolved API port when the core serves on port 0, else None."""
        server = self._core_server
        if server is None:
            return None
        for asyncio_server in getattr(server, "servers", None) or []:
            for sock in asyncio_server.sockets or []:
                address = sock.getsockname()
                if isinstance(address, tuple) and len(address) >= 2:
                    return int(address[1])
        return None

    # ── internals ──

    def _start_core(self) -> None:
        """Embed the memory-server core in THIS process (SL §3.1).

        The same import-string app as ``vesma serve`` runs — no engine
        logic forks; workers are PINNED to 1 (workers>1 would make the core
        a child process, contradicting the in-process mandate; a configured
        workers>1 is answered with a loud warning, never a silent pin).
        """
        import uvicorn

        host, port = self._core_bind or ("127.0.0.1", 8000)
        # Same env seeding as the serve command: the app's own settings
        # loader must see the effective bind (auth middleware boundary).
        os.environ["VESMA_API__HOST"] = host
        os.environ["VESMA_API__PORT"] = str(port)
        config = uvicorn.Config("vesmaro.api.main:app", host=host, port=port, workers=1)
        server = uvicorn.Server(config)
        self._core_server = server
        self._core_thread = threading.Thread(target=server.run, name="vesma-core-api", daemon=True)
        self._core_thread.start()
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if getattr(server, "started", False):
                return
            if not self._core_thread.is_alive():
                self._exit_code = 1
                raise CoreStartupError(
                    f"in-process core exited during startup on {host}:{port} — "
                    "see the supervisor log (fail-fast, SL §3.1)"
                )
            time.sleep(0.05)
        self._exit_code = 1
        raise CoreStartupError(f"in-process core did not start within 30s on {host}:{port}")

    def _watch_core(self) -> None:
        thread = self._core_thread
        if thread is None:
            return
        thread.join()
        if self.supervisor.shutdown_requested:
            return  # ordered teardown — the core was ASKED to stop
        # Nobody ordered this: the heart died in its sleep. SL §3.1 —
        # death of the in-process core is death of the supervisor.
        self._log.error(
            "in-process core exited unexpectedly — fail-fast (SL §3.1): "
            "supervisor shuts down, the unit restarts"
        )
        self._exit_code = 1
        self.supervisor.request_shutdown()

    def _watch_supervisor(self) -> None:
        while not self.supervisor.shutdown_requested and not self._finalized:
            time.sleep(0.1)
        self.server.stop()
        server = self._core_server
        if server is not None:
            server.should_exit = True

    def _finalize(self) -> int:
        if self._finalized:
            return self._exit_code
        self._finalized = True
        self.server.stop()
        code = self.supervisor.shutdown()  # reverse-topological stop + reap
        server = self._core_server
        thread = self._core_thread
        if server is not None and thread is not None:
            server.should_exit = True
            thread.join(timeout=15.0)
            if thread.is_alive():
                self._log.error("in-process core did not drain within 15s")
                self._exit_code = max(self._exit_code, 1)
        self._sink.close()
        self._exit_code = max(self._exit_code, code)
        return self._exit_code
