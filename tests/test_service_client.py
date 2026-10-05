"""Control-socket thin-client tests (contract control-socket v1).

Covers the client side of the W3 wave: §4.1 pre-flight refusals (with the
ready fix command, never auto-executed), the hello-first session, the 10 s
response timeout outside follow, range-based handling of unknown error
codes (§4.6/CS-15), follow streaming (§4.5/CS-14), and the ``vesma
service`` CLI wiring (§4.8).
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.control_fakes import FakeBackend
from tests.test_service_control import RunningServer
from vesmaro.service.client import (
    FOLLOW_READ_TIMEOUT_S,
    ControlClient,
    PreflightError,
    ProtocolError,
    ProtocolViolationError,
    ResponseTimeoutError,
    SocketUnavailableError,
    preflight,
)
from vesmaro.service.control import FOLLOW_IDLE_TIMEOUT_S

# ── helpers ───────────────────────────────────────────────────────────


class DeadServer:
    """Accepts connections and never replies (timeout leg)."""

    def __init__(self, path: Path) -> None:
        self.held: list[socket.socket] = []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(4)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        self.listener.settimeout(0.2)
        while not self.stop_event.is_set():
            try:
                conn, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.held.append(conn)  # hold it open, answer nothing

    def stop(self) -> None:
        self.stop_event.set()
        self.listener.close()
        for conn in self.held:
            conn.close()


class ScriptedServer:
    """Hello-aware raw server: answers hello, then one canned reply."""

    def __init__(self, path: Path, reply_after_hello: bytes, *, first_reply: bool = False) -> None:
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(4)
        self.reply = reply_after_hello
        self.first_reply = first_reply  # True: reply with ``reply`` INSTEAD of hello
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        self.listener.settimeout(3.0)
        try:
            conn, _ = self.listener.accept()
            conn.settimeout(3.0)
            self._read_line(conn)
            if not self.first_reply:
                hello_reply = (
                    b'{"id": 1, "result": {"protocol_version": 1, "min_protocol": 1,'
                    b' "server_version": "test", "pid": 1}}\n'
                )
                conn.sendall(hello_reply)
                self._read_line(conn)  # the next request line
            conn.sendall(self.reply)
            time.sleep(0.3)
            conn.close()
        except (OSError, AssertionError):
            return

    @staticmethod
    def _read_line(conn: socket.socket) -> bytes:
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                raise AssertionError("client vanished")
            buf += chunk
        return buf

    def stop(self) -> None:
        self.listener.close()


@pytest.fixture
def sock_path(tmp_path: Path) -> Path:
    return tmp_path / "control.sock"


@pytest.fixture
def server(sock_path: Path) -> Iterator[RunningServer]:
    running = RunningServer(FakeBackend(), sock_path)
    yield running
    running.stop()


# ── §4.1 pre-flight ───────────────────────────────────────────────────


def test_preflight_refuses_group_writable_dir(tmp_path: Path) -> None:
    dir_path = tmp_path / "vesma"
    dir_path.mkdir()
    os.chmod(dir_path, 0o770)  # explicit: umask would strip the group-write bit
    with pytest.raises(PreflightError) as excinfo:
        preflight(dir_path / "control.sock")
    assert "group- or world-writable" in excinfo.value.problem
    assert excinfo.value.fix_command == f"chmod 0700 {dir_path}"


def test_preflight_refuses_world_writable_dir(tmp_path: Path) -> None:
    dir_path = tmp_path / "vesma"
    dir_path.mkdir()
    os.chmod(dir_path, 0o702)
    with pytest.raises(PreflightError) as excinfo:
        preflight(dir_path / "control.sock")
    assert excinfo.value.fix_command.startswith("chmod 0700 ")


def test_preflight_refuses_foreign_owner_dir(tmp_path: Path) -> None:
    dir_path = tmp_path / "vesma"
    dir_path.mkdir()
    with pytest.raises(PreflightError) as excinfo:
        preflight(dir_path / "control.sock", euid=os.geteuid() + 1)
    assert "owned by uid" in excinfo.value.problem
    assert excinfo.value.fix_command.startswith("chown ")


def test_preflight_missing_dir_hints_supervisor(tmp_path: Path) -> None:
    with pytest.raises(PreflightError, match="supervisor running"):
        preflight(tmp_path / "missing" / "control.sock")


def test_client_connect_never_auto_fixes_lax_dir(tmp_path: Path) -> None:
    dir_path = tmp_path / "vesma"
    dir_path.mkdir()
    os.chmod(dir_path, 0o770)
    sock = dir_path / "control.sock"
    client = ControlClient(sock)
    with pytest.raises(PreflightError):
        client.connect()
    assert dir_path.stat().st_mode & 0o022  # mode untouched — no auto-fix
    assert not sock.exists()  # no connection attempt against a lax dir


# ── session: hello-first, verbs, idempotence ─────────────────────────


def test_client_session_all_verbs(server: RunningServer) -> None:
    with ControlClient(server.path, skip_preflight=True) as client:
        assert client.server_info["protocol_version"] == 1
        assert isinstance(client.server_info["pid"], int)
        status = client.status()
        assert set(status["components"]) == set(server.backend.registry)  # type: ignore[arg-type]
        one = client.status("board")
        assert one["components"]["board"]["state"] == "stopped"  # type: ignore[index]
        assert client.start("board") == "starting"
        assert client.start("board") == "already-running"
        assert client.stop("board") == "stopped"
        assert client.stop("board") == "already-stopped"
        assert client.restart("board") == "starting"
        assert client.logs("board", tail=1) == ["board: boot ok"]
        assert client.health()["health"] == "healthy"  # type: ignore[index]


def test_client_maps_protocol_errors(server: RunningServer) -> None:
    with (
        ControlClient(server.path, skip_preflight=True) as client,
        pytest.raises(ProtocolError) as excinfo,
    ):
        client.start("ghost")
    assert excinfo.value.code == 100
    assert excinfo.value.data == {"component": "ghost"}


def test_client_socket_missing(tmp_path: Path) -> None:
    dir_path = tmp_path / "vesma"
    dir_path.mkdir()
    client = ControlClient(dir_path / "control.sock")
    with pytest.raises(SocketUnavailableError):
        client.connect()


def test_client_response_timeout(sock_path: Path) -> None:
    dead = DeadServer(sock_path)
    try:
        client = ControlClient(sock_path, response_timeout=0.3, skip_preflight=True)
        with pytest.raises(ResponseTimeoutError):
            client.connect()
    finally:
        dead.stop()


def test_client_malformed_server_frame_raises_violation(sock_path: Path) -> None:
    scripted = ScriptedServer(sock_path, b"garbage not json\n", first_reply=True)
    try:
        client = ControlClient(sock_path, skip_preflight=True)
        with pytest.raises(ProtocolViolationError):
            client.connect()
    finally:
        scripted.stop()


def test_client_non_int_error_code_raises_violation(sock_path: Path) -> None:
    """A rogue server sending a non-integer error code → typed violation,
    never a raw ValueError from ``int()`` (cascade 2026-10-05, P3a)."""
    scripted = ScriptedServer(
        sock_path,
        b'{"id": 2, "error": {"code": "boom", "message": "broken server"}}\n',
    )
    try:
        client = ControlClient(sock_path, skip_preflight=True)
        client.connect()
        try:
            with pytest.raises(ProtocolViolationError, match="not an integer"):
                client.request("status", {})
        finally:
            client.close()
    finally:
        scripted.stop()


def test_follow_read_timeout_derives_from_server_idle_window() -> None:
    """The client's follow read budget is DERIVED from the server's idle
    window (1.25x margin), not an independent literal (P3b)."""
    assert FOLLOW_READ_TIMEOUT_S == FOLLOW_IDLE_TIMEOUT_S * 1.25
    assert FOLLOW_READ_TIMEOUT_S == 75.0  # the documented budget stays pinned


def test_client_follow_streams_lines_and_final(server: RunningServer) -> None:
    lines: list[str] = []
    result: dict[str, Any] = {}

    def reader() -> None:
        with ControlClient(server.path, skip_preflight=True) as client:
            result["state"] = client.follow_logs("board", lines.append, tail=5)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not server.backend.streams:
        time.sleep(0.02)
    stream = server.backend.streams[-1]
    stream.push("alpha")
    stream.push("beta")
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and len(lines) < 3:
        time.sleep(0.02)
    server.backend.set_state("board", "stopped")
    stream.source_stopped()
    thread.join(timeout=5.0)
    assert lines == ["board: boot ok", "alpha", "beta"]  # replay + pushed
    assert result["state"] == "stopped"
    assert stream.closed  # the client closes after the final answer


# ── CS-15 (client leg): unknown error codes interpreted by range ─────


@pytest.mark.parametrize(
    ("code", "expected_range"),
    [
        (1, "protocol"),
        (4, "protocol"),
        (99, "protocol"),
        (100, "lifecycle"),
        (152, "lifecycle"),
        (199, "lifecycle"),
        (200, "authz"),
        (250, "authz"),
        (500, "infra"),
        (599, "infra"),
        (424, "unknown"),
        (999, "unknown"),
        (-5, "unknown"),
    ],
)
def test_unknown_code_ranges(code: int, expected_range: str) -> None:
    assert ProtocolError(code, "synthetic", None).code_range == expected_range


def test_unknown_code_from_live_server_is_handled(sock_path: Path) -> None:
    scripted = ScriptedServer(
        sock_path,
        b'{"id": 2, "error": {"code": 424, "message": "from the future", "data": {}}}\n',
    )
    try:
        client = ControlClient(sock_path, skip_preflight=True)
        client.connect()
        try:
            with pytest.raises(ProtocolError) as excinfo:
                client.request("holospace", {})
        finally:
            client.close()
        assert excinfo.value.code == 424
        assert excinfo.value.code_range == "unknown"  # degraded, not crashed
    finally:
        scripted.stop()


# ── §4.8 CLI wiring ───────────────────────────────────────────────────


def test_cli_service_group_registers_all_verbs() -> None:
    from vesmaro.cli.main import app

    result = CliRunner().invoke(app, ["service", "--help"])
    assert result.exit_code == 0
    for verb in ("status", "health", "start", "stop", "restart", "logs", "run"):
        assert verb in result.output


def test_cli_service_status_against_fake_server(server: RunningServer) -> None:
    from vesmaro.cli.main import app

    result = CliRunner().invoke(app, ["service", "status", "--socket", str(server.path)])
    assert result.exit_code == 0
    assert "supervisor" in result.output
    assert "board" in result.output


def test_cli_service_start_stop_against_fake_server(server: RunningServer) -> None:
    from vesmaro.cli.main import app

    runner = CliRunner()
    started = runner.invoke(app, ["service", "start", "board", "--socket", str(server.path)])
    assert started.exit_code == 0
    assert "board: starting" in started.output
    stopped = runner.invoke(app, ["service", "stop", "board", "--socket", str(server.path)])
    assert stopped.exit_code == 0
    assert "board: stopped" in stopped.output


def test_cli_service_error_mapping_unknown_component(server: RunningServer) -> None:
    from vesmaro.cli.main import app

    result = CliRunner().invoke(app, ["service", "start", "ghost", "--socket", str(server.path)])
    assert result.exit_code == 1
    assert "unknown component: ghost" in result.output


def test_cli_service_preflight_failure_prints_fix_command(tmp_path: Path) -> None:
    from vesmaro.cli.main import app

    dir_path = tmp_path / "vesma"
    dir_path.mkdir()
    os.chmod(dir_path, 0o770)
    (dir_path / "control.sock").touch()
    result = CliRunner().invoke(
        app, ["service", "status", "--socket", str(dir_path / "control.sock")]
    )
    assert result.exit_code == 1
    flat = " ".join(result.output.split())  # rich may wrap long lines
    assert "pre-flight" in flat
    assert "chmod 0700" in flat

