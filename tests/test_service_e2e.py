"""End-to-end service tests (wave W6): the REAL composition.

Real Supervisor + real ControlServer + real SupervisorBackend + real child
processes, driven through a LIVE control socket — the integration the unit
suites fake around. Cards: SL-01 socket-leg, SL-16 stop order, CS §4.5
verb semantics over the adapter, SL §3.1 in-process core (embedded API +
fail-fast on core death).

Children come from ``tests/service_children``; every app is registered
with the ``make_app`` fixture so no sleeper leaks into later tests.
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_service_control import JsonlConn, error_of
from vesmaro.service.backend import ServiceApp
from vesmaro.service.logsink import HistoryJournal
from vesmaro.service.manifest import ComponentManifest, load_manifest

CHILDREN = Path(__file__).parent / "service_children"
PY = sys.executable

WAIT_TIMEOUT_S = 20.0


# ── fixtures + helpers ────────────────────────────────────────────────


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate HOME + XDG roots: nothing leaks into (or out of) the real home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    return home


class _NullSink:
    """Test downstream sink: the buffer is the e2e log surface; the real
    §3.7 sinks are unit-covered (tests/test_service_logsink.py)."""

    def emit(self, identifier: str, line: str, *, severity: str = "INFO") -> None:
        del identifier, line, severity

    def close(self) -> None:
        return None


@pytest.fixture
def make_app(tmp_path: Path) -> Iterator[Callable[..., ServiceApp]]:
    """ServiceApp factory with guaranteed teardown (no sleeper leaks)."""
    apps: list[ServiceApp] = []

    def factory(manifests: dict[str, ComponentManifest], **kw: Any) -> ServiceApp:
        kw.setdefault("socket_path", tmp_path / f"control-{len(apps)}.sock")
        kw.setdefault("sink", _NullSink())
        app = ServiceApp(manifests, **kw)
        apps.append(app)
        return app

    yield factory
    for app in apps:
        app.request_stop()
        app.wait()


def wait_until(
    predicate: Callable[[], bool], message: str, timeout: float = WAIT_TIMEOUT_S
) -> None:
    """Poll-until with a generous deadline — observable state only, no
    fixed sleeps (the SL-05 flake lesson, applied everywhere here)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {message}")


def child_doc(
    name: str,
    script: str,
    args: tuple[str, ...] = (),
    *,
    depends_on: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Optional-tier child (manifest form, CM v1); argv[0] is the test venv
    python, so the children run under the same interpreter as the suite."""
    return {
        "apiVersion": "vesma.component/v1",
        "kind": "child-process",
        "metadata": {
            "name": name,
            "version": "0.1.0",
            "tier": "optional",
            "description": f"w6 e2e child {name}",
            "provenance": {"repo": "https://example.com/x", "license": "MIT"},
        },
        "launch": {"argv": [PY, str(CHILDREN / script), *args]},
        "depends_on": list(depends_on),
        "stop": {"signal": "SIGTERM", "grace_period": "2s"},
    }


def write_manifests(
    tmp_path: Path, docs: dict[str, dict[str, Any]]
) -> dict[str, ComponentManifest]:
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir(exist_ok=True)
    loaded: dict[str, ComponentManifest] = {}
    for name, doc in docs.items():
        path = manifests_dir / f"{name}.yaml"
        path.write_text(yaml.safe_dump(doc), encoding="utf-8")
        loaded[name] = load_manifest(path)
    return loaded


def status_entry(conn: JsonlConn, frame_id: int, name: str) -> dict[str, Any]:
    frame = conn.rt({"id": frame_id, "method": "status"})
    return dict(frame["result"]["components"][name])


def pid_alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rpartition(")")[2].split()[0] != "Z"
    except OSError:
        return False


def hello_ok(conn: JsonlConn) -> dict[str, Any]:
    frame = conn.hello()
    assert "result" in frame, frame
    return dict(frame["result"])


# ── the live system: hello/status with real pids ──────────────────────


class TestLiveSystem:
    def test_hello_status_real_pids_over_live_socket(
        self, isolated_home: Path, tmp_path: Path, make_app: Callable[..., ServiceApp]
    ) -> None:
        manifests = write_manifests(
            tmp_path,
            {
                "alpha": child_doc("alpha", "sleeper.py"),
                "beta": child_doc("beta", "sleeper.py"),
            },
        )
        app = make_app(manifests, with_core=False)
        assert app.bind().status == "bound"
        app.start()
        assert app.socket_path is not None

        conn = JsonlConn(app.socket_path)
        try:
            hello = hello_ok(conn)
            assert hello["protocol_version"] == 1
            assert hello["pid"] == os.getpid()  # the supervisor IS this process

            def both_healthy() -> bool:
                frame = conn.rt({"id": 10, "method": "status"})["result"]
                states = [entry["state"] for entry in frame["components"].values()]
                return states == ["healthy", "healthy"]

            wait_until(both_healthy, "both sleepers healthy via the socket")
            frame = conn.rt({"id": 11, "method": "status"})
            components = frame["result"]["components"]
            for name in ("alpha", "beta"):
                entry = components[name]
                assert entry["state"] == "healthy"
                assert entry["health"] == "healthy"
                pid = entry["pid"]
                assert pid is not None and pid_alive(int(pid))

            health = conn.rt({"id": 12, "method": "health"})["result"]
            assert health["health"] == "healthy"
            assert set(health["components"].values()) == {"healthy"}
        finally:
            conn.close()


# ── idempotence markers over the adapter (CS §4.5) ────────────────────


class TestVerbSemantics:
    def test_start_stop_restart_idempotence_markers(
        self, isolated_home: Path, tmp_path: Path, make_app: Callable[..., ServiceApp]
    ) -> None:
        manifests = write_manifests(tmp_path, {"worker": child_doc("worker", "sleeper.py")})
        app = make_app(manifests, with_core=False)
        app.bind()
        app.start()
        assert app.socket_path is not None
        conn = JsonlConn(app.socket_path)
        try:
            hello_ok(conn)
            wait_until(
                lambda: status_entry(conn, 10, "worker")["state"] == "healthy",
                "worker healthy",
            )
            old_pid = status_entry(conn, 11, "worker")["pid"]

            # stop → stopped; stop again → already-stopped (idempotent, CS-10)
            reply = conn.rt({"id": 12, "method": "stop", "params": {"component": "worker"}})
            assert reply["result"] == {"state": "stopped"}
            reply = conn.rt({"id": 13, "method": "stop", "params": {"component": "worker"}})
            assert reply["result"] == {"state": "already-stopped"}
            wait_until(lambda: not pid_alive(int(old_pid)), "worker gone")

            # manual start after manual stop (the W6 request_start fix):
            # a fresh T1 spawn with a NEW pid
            reply = conn.rt({"id": 14, "method": "start", "params": {"component": "worker"}})
            assert reply["result"]["state"] in ("starting", "healthy")

            def healthy_new_pid() -> bool:
                entry = status_entry(conn, 15, "worker")
                return entry["state"] == "healthy" and entry["pid"] != old_pid

            wait_until(healthy_new_pid, "worker respawned with a new pid")

            # start again → already-running (idempotent, CS-10)
            reply = conn.rt({"id": 16, "method": "start", "params": {"component": "worker"}})
            assert reply["result"] == {"state": "already-running"}

            # restart is NOT idempotent by contract: stop + start, live state
            reply = conn.rt({"id": 17, "method": "restart", "params": {"component": "worker"}})
            assert reply["result"]["state"] in ("starting", "healthy")
            wait_until(
                lambda: status_entry(conn, 18, "worker")["state"] == "healthy",
                "worker healthy after restart",
            )
        finally:
            conn.close()

    def test_unknown_component_maps_to_100(
        self, isolated_home: Path, tmp_path: Path, make_app: Callable[..., ServiceApp]
    ) -> None:
        manifests = write_manifests(tmp_path, {"worker": child_doc("worker", "sleeper.py")})
        app = make_app(manifests, with_core=False)
        app.bind()
        app.start()
        assert app.socket_path is not None
        conn = JsonlConn(app.socket_path)
        try:
            hello_ok(conn)
            for method, params in (
                ("start", {"component": "ghost"}),
                ("stop", {"component": "ghost"}),
                ("restart", {"component": "ghost"}),
                ("logs", {"component": "ghost"}),
            ):
                frame = conn.rt({"id": 20, "method": method, "params": params})
                error = error_of(frame)
                assert error["code"] == 100, (method, error)
                assert error["data"] == {"component": "ghost"}
        finally:
            conn.close()


# ── logs over the buffer: tail + follow until source stop ─────────────


class TestLogsOverBuffer:
    def test_logs_tail_and_follow_until_source_stop(
        self, isolated_home: Path, tmp_path: Path, make_app: Callable[..., ServiceApp]
    ) -> None:
        manifests = write_manifests(
            tmp_path,
            {"logger": child_doc("logger", "line_printer.py", ("--interval", "0.05"))},
        )
        app = make_app(manifests, with_core=False)
        app.bind()
        app.start()
        assert app.socket_path is not None
        follow = JsonlConn(app.socket_path)
        control = JsonlConn(app.socket_path)
        try:
            hello_ok(follow)
            hello_ok(control)

            def lines_flowing() -> bool:
                frame = control.rt(
                    {"id": 30, "method": "logs", "params": {"component": "logger", "tail": 5}}
                )
                lines = frame["result"]["lines"]
                return len(lines) == 5 and all(line.startswith("line-") for line in lines)

            wait_until(lines_flowing, "buffer holds ≥5 printer lines")

            # follow=true: replay (tail=3) then a live stream; the source is
            # stopped via a SECOND connection mid-stream — the follow ends
            # with the final {"state": ...} answer (§4.5)
            follow.send(
                {
                    "id": 31,
                    "method": "logs",
                    "params": {"component": "logger", "tail": 3, "follow": True},
                }
            )
            frames = 0
            streamed = 0
            stop_sent = False
            final: dict[str, Any] | None = None
            deadline = time.monotonic() + WAIT_TIMEOUT_S
            while time.monotonic() < deadline and final is None:
                frame = follow.recv_json()
                if "stream" in frame:
                    frames += 1
                    if str(frame["stream"].get("line", "")).startswith("line-"):
                        streamed += 1
                    if streamed >= 5 and not stop_sent:
                        reply = control.rt(
                            {"id": 32, "method": "stop", "params": {"component": "logger"}}
                        )
                        assert reply["result"] == {"state": "stopped"}
                        stop_sent = True
                elif "result" in frame:
                    final = dict(frame["result"])
            assert frames >= 3, "the follow must replay the tail first"
            assert streamed >= 5, f"too few live frames: {frames}"
            assert final == {"state": "stopped"}, final
        finally:
            follow.close()
            control.close()


# ── SL-16: SIGTERM → reverse-topological stop, zero orphans ───────────


class TestGracefulShutdown:
    def test_sigterm_reverse_topological_stop_no_zombies(
        self, isolated_home: Path, tmp_path: Path, make_app: Callable[..., ServiceApp]
    ) -> None:
        journal = HistoryJournal(directory=tmp_path / "history")
        manifests = write_manifests(
            tmp_path,
            {
                "base": child_doc("base", "sleeper.py"),
                "mid": child_doc("mid", "sleeper.py", depends_on=("base",)),
                "top": child_doc("top", "sleeper.py", depends_on=("mid",)),
            },
        )
        app = make_app(manifests, with_core=False, journal=journal)
        app.bind()
        app.start()
        assert app.socket_path is not None
        conn = JsonlConn(app.socket_path)
        try:
            hello_ok(conn)

            def all_healthy() -> bool:
                frame = conn.rt({"id": 40, "method": "status"})["result"]
                return all(entry["state"] == "healthy" for entry in frame["components"].values())

            wait_until(all_healthy, "chain healthy (topological start)")
            pids: dict[str, int] = {
                name: int(status_entry(conn, frame_id, name)["pid"])
                for frame_id, name in ((41, "base"), (42, "mid"), (43, "top"))
            }
        finally:
            conn.close()

        codes: list[int] = []

        # The CLI pattern: SIGTERM arrives, the handler requests the
        # shutdown, and wait()/shutdown() run on the MAIN thread — the one
        # that installed the signal handlers (signal restoration is a
        # main-thread operation).
        def note_code() -> None:
            codes.append(app.wait())

        os.kill(os.getpid(), signal.SIGTERM)
        wait_until(lambda: app.supervisor.shutdown_requested, "shutdown requested by SIGTERM")
        note_code()
        assert codes == [0], codes

        # reverse-topological stop order from the journal (SL-16): dependents
        # stop before dependencies
        stop_index: dict[str, int] = {}
        for index, record in enumerate(journal.read_records()):
            for name in ("base", "mid", "top"):
                if f"component={name} " in record and "to=stopped" in record:
                    stop_index.setdefault(name, index)
        assert set(stop_index) == {"base", "mid", "top"}, stop_index
        assert stop_index["top"] < stop_index["mid"] < stop_index["base"], stop_index

        # zero children / zero zombies at exit (SL §3.5: exit only after reap)
        wait_until(
            lambda: all(not pid_alive(pid) for pid in pids.values()),
            "every child stopped and reaped",
        )
        # the control socket is gone with the supervisor
        assert app.socket_path is not None and not app.socket_path.exists()


# ── SL-01: kill -9 a child — socket alive, FSM backoff ────────────────


class TestIsolation:
    def test_kill9_child_socket_alive_fsm_backoff(
        self, isolated_home: Path, tmp_path: Path, make_app: Callable[..., ServiceApp]
    ) -> None:
        journal = HistoryJournal(directory=tmp_path / "history")
        manifests = write_manifests(
            tmp_path,
            {
                "victim": child_doc("victim", "sleeper.py"),
                "survivor": child_doc("survivor", "sleeper.py"),
            },
        )
        app = make_app(manifests, with_core=False, journal=journal)
        app.bind()
        app.start()
        assert app.socket_path is not None
        conn = JsonlConn(app.socket_path)
        try:
            hello_ok(conn)

            def both_healthy() -> bool:
                frame = conn.rt({"id": 50, "method": "status"})["result"]
                return all(entry["state"] == "healthy" for entry in frame["components"].values())

            wait_until(both_healthy, "children healthy")
            victim_pid = int(status_entry(conn, 51, "victim")["pid"])
            os.kill(victim_pid, signal.SIGKILL)

            # the socket accepts while the child is dead (SL-01 invariant)
            second = JsonlConn(app.socket_path)
            try:
                hello_ok(second)
            finally:
                second.close()

            # the §3.4 exit line carries the signal (no code, SIGKILL→KILL)
            wait_until(
                lambda: any(
                    f"component=victim event=exit pid={victim_pid} code=none signal=KILL" in record
                    for record in journal.read_records()
                ),
                "exit line signal=KILL in the journal",
            )

            # FSM backoff (T8), then the restart discipline respawns it
            def backoff_then_healthy() -> bool:
                records = journal.read_records()
                had_backoff = any(
                    "component=victim " in record and "to=backoff" in record for record in records
                )
                entry = status_entry(conn, 52, "victim")
                return had_backoff and entry["state"] == "healthy" and entry["pid"] != victim_pid

            wait_until(backoff_then_healthy, "victim backoff + respawn healthy")

            # the supervisor-level health never degraded: optional tier,
            # restart budget alive
            health = conn.rt({"id": 53, "method": "health"})["result"]
            assert health["health"] == "healthy"
        finally:
            conn.close()


# ── SL §3.1: the in-process core (embedded API + fail-fast) ───────────


class TestInProcessCore:
    def test_core_api_answers_while_supervisor_lives_then_death_fails_fast(
        self, isolated_home: Path, tmp_path: Path, make_app: Callable[..., ServiceApp]
    ) -> None:
        manifests = write_manifests(tmp_path, {"worker": child_doc("worker", "sleeper.py")})
        app = make_app(manifests, with_core=True, core_bind=("127.0.0.1", 0))
        app.bind()
        app.start()
        port = app.core_port
        assert port is not None and port > 0

        def api_ok() -> bool:
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                connection.request("GET", "/health")
                response = connection.getresponse()
                body = json.loads(response.read().decode("utf-8"))
                connection.close()
            except OSError:
                return False
            return response.status == 200 and isinstance(body, dict)

        wait_until(api_ok, "embedded API answers /health on its port")

        # the supervisor is alive alongside the core: status over the socket
        assert app.socket_path is not None
        conn = JsonlConn(app.socket_path)
        try:
            hello_ok(conn)
            wait_until(
                lambda: status_entry(conn, 60, "worker")["state"] == "healthy",
                "worker healthy next to the live core",
            )
        finally:
            conn.close()

        # core death simulation: the uvicorn loop ends WITHOUT an ordered
        # teardown. The watcher cannot (and must not) distinguish a graceful
        # self-exit from a crash — the thread exiting un-ordered IS the
        # death signal (SL §3.1: death of the core = death of the
        # supervisor). Expect exit code 1 (fail-fast → unit restart).
        assert app._core_server is not None
        app._core_server.should_exit = True
        wait_until(
            lambda: app.supervisor.shutdown_requested,
            "core thread exit triggered the fail-fast shutdown request",
        )
        assert app.wait() == 1
        # socket cleaned by the accept-loop shutdown
        assert app.socket_path is not None and not app.socket_path.exists()

    def test_core_startup_failure_refuses_the_start(
        self, isolated_home: Path, tmp_path: Path, make_app: Callable[..., ServiceApp]
    ) -> None:
        manifests = write_manifests(tmp_path, {"worker": child_doc("worker", "sleeper.py")})
        # a deterministically impossible bind: the port is held by THIS
        # test's listener → the core cannot start → fail-fast before any
        # child spawns; the control socket is cleaned on the way out
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = int(blocker.getsockname()[1])
        app = make_app(manifests, with_core=True, core_bind=("127.0.0.1", port))
        app.bind()
        try:
            with pytest.raises(Exception, match="core"):  # CoreStartupError
                app.start()
        finally:
            blocker.close()
        assert app.socket_path is not None
        wait_until(lambda: not app.socket_path.exists(), "control socket cleaned up")
