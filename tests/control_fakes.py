"""Shared test fakes for the control-socket wave (W3).

``FakeBackend`` implements the ``ControlBackend`` protocol of
``vesmaro.service.control`` against in-memory state, including the
backend-side duties the contract assigns to the supervisor: start/stop
idempotence markers, typed lifecycle errors, and push-based log streams
whose sources can be driven (and stopped) by the tests.
"""

from __future__ import annotations

import os
import queue
import threading

from vesmaro.service.control import (
    LogStream,
    StartFailedError,
    StopTimeoutError,
    UnknownComponentError,
)

DEFAULT_COMPONENTS = ("board", "metrics")


class FakeLogStream:
    """Queue-backed LogStream the test drives via push()/source_stopped()."""

    def __init__(self, backend: FakeBackend, component: str, replay: list[str]) -> None:
        self._backend = backend
        self.component = component
        self._q: queue.Queue[str] = queue.Queue()
        for line in replay:
            self._q.put(line)
        self._final: str | None = None
        self.closed = False
        self.close_count = 0

    def push(self, line: str) -> None:
        self._q.put(line)

    def source_stopped(self) -> None:
        """Simulate the source reaching its stop state (final answer)."""
        self._final = self._backend.states[self.component]

    def poll(self, timeout: float) -> list[str]:
        try:
            first = self._q.get(timeout=timeout)
        except queue.Empty:
            return []
        lines = [first]
        while True:
            try:
                lines.append(self._q.get_nowait())
            except queue.Empty:
                return lines

    def current_state(self) -> str:
        return self._backend.states[self.component]

    @property
    def final_state(self) -> str | None:
        return self._final

    def close(self) -> None:
        self.closed = True
        self.close_count += 1


class FakeBackend:
    """In-memory ControlBackend: registry, FSM-ish states, controllable
    failures and live streams."""

    def __init__(self, components: tuple[str, ...] = DEFAULT_COMPONENTS) -> None:
        self.registry = set(components)
        self.states: dict[str, str] = {c: "stopped" for c in components}
        self.health_map: dict[str, str] = {c: "down" for c in components}
        self.lines: dict[str, list[str]] = {c: [f"{c}: boot ok"] for c in components}
        self.calls: list[tuple[str, ...]] = []
        self.streams: list[FakeLogStream] = []
        self.fail_start: dict[str, str] = {}
        self.stop_timeout_for: set[str] = set()
        self._lock = threading.Lock()

    # ── test drivers ──

    def set_state(self, component: str, state: str) -> None:
        with self._lock:
            self.states[component] = state

    def push_line(self, component: str, line: str) -> None:
        for stream in list(self.streams):
            if stream.component == component and not stream.closed:
                stream.push(line)

    # ── ControlBackend protocol ──

    def _require(self, component: str) -> None:
        if component not in self.registry:
            raise UnknownComponentError(component)

    def component_status(self, component: str | None) -> dict[str, object]:
        if component is None:
            return {
                "supervisor": {"pid": os.getpid(), "health": "healthy"},
                "components": {
                    name: {"state": state, "health": self.health_map[name]}
                    for name, state in sorted(self.states.items())
                },
            }
        self._require(component)
        return {
            "components": {
                component: {"state": self.states[component], "health": self.health_map[component]}
            }
        }

    def start_component(self, component: str) -> str:
        self._require(component)
        self.calls.append(("start", component))
        if component in self.fail_start:
            raise StartFailedError(component, self.fail_start[component])
        state = self.states[component]
        if state in ("starting", "healthy"):
            return "already-running"
        self.states[component] = "starting"
        self.health_map[component] = "degraded"
        return "starting"

    def stop_component(self, component: str, *, force: bool) -> str:
        self._require(component)
        self.calls.append(("stop", component, force))
        if component in self.stop_timeout_for:
            raise StopTimeoutError(component)
        state = self.states[component]
        if state == "stopped":
            return "already-stopped"
        self.states[component] = "stopped"
        self.health_map[component] = "down"
        for stream in list(self.streams):
            if stream.component == component:
                stream.source_stopped()
        return "stopped"

    def restart_component(self, component: str) -> str:
        self._require(component)
        self.calls.append(("restart", component))
        self.states[component] = "starting"
        return "starting"

    def component_logs(self, component: str, *, tail: int) -> list[str]:
        self._require(component)
        self.calls.append(("logs", component, tail))
        return list(self.lines[component])[-tail:]

    def stream_logs(self, component: str, *, tail: int) -> LogStream:
        self._require(component)
        stream = FakeLogStream(self, component, replay=list(self.lines[component])[-tail:])
        self.streams.append(stream)
        return stream

    def health(self, component: str | None) -> dict[str, object]:
        if component is None:
            return {"health": "healthy", "components": dict(self.health_map)}
        self._require(component)
        return {"health": self.health_map[component]}
