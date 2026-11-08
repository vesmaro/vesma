"""Phase 1 pytest packaging of the MCP token injector (ArchCom 2026-11-06).

Port of the merged Phase 0 smoke checklist (protocol P0/L/U legs, T1-T9 +
T10 + T5') against the repo artifact ``vesma.mcp_injector``, run against a
dummy upstream on an ephemeral port (the staging-verify practice). Leg
names reference the ArchCom contract pins:

- P0 fail-closed: startup 0600/owner/loopback refusals, runtime unreadable
  credential files -> 503;
- L listener: peer-check (T6), bare 403 without oracle (T10), strip-then-
  inject (T5/T5'), path allowlist, method gates;
- U upstream: 401-retry-once-on-byte-change, 413+drain, 411 chunked,
  GET-with-body (SEC-03 documented behavior), SSE flush-per-chunk, rate and
  connection caps, --no-sse-get, healthz shape.
"""

from __future__ import annotations

import dataclasses
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

TOKEN_A = b"core-token-aaaa1111"
TOKEN_B = b"core-token-bbbb2222"
SECRET = b"listener-secret-3333"
AUTH = {"Authorization": "Bearer " + SECRET.decode()}


# ── dummy upstream (the staging practice: programmable, ephemeral port) ──


@dataclasses.dataclass
class _Action:
    """One scripted upstream response; ``rotate`` drives the 401-retry path."""

    status: int = 200
    body: bytes = b'{"jsonrpc":"2.0","id":1}'
    rotate: tuple[str, bytes] | None = None  # atomic token-file rewrite on hit


@dataclasses.dataclass
class _Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class _UpstreamState:
    def __init__(self) -> None:
        self.port = 0
        self.requests: list[_Request] = []
        self.script: list[_Action] = []
        self.default = _Action()
        self.hold: threading.Event | None = None
        self.chunk_pieces: list[bytes] = []
        self.chunk_delay = 0.0
        self.lock = threading.Lock()


class _UpstreamServer(ThreadingHTTPServer):
    state: _UpstreamState


class _DummyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 3  # body-honoring reads must not wedge the dummy upstream

    def _record(self) -> _Action:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        req = _Request(
            self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body
        )
        with self.server.state.lock:
            self.server.state.requests.append(req)
            action = (
                self.server.state.script.pop(0)
                if self.server.state.script
                else self.server.state.default
            )
        if self.server.state.hold is not None:
            self.server.state.hold.wait(timeout=10)
        return action

    def _respond(self, action: _Action) -> None:
        state = self.server.state
        if state.chunk_pieces:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for piece in state.chunk_pieces:
                self.wfile.write(b"%X\r\n" % len(piece) + piece + b"\r\n")
                self.wfile.flush()
                time.sleep(state.chunk_delay)  # delay AFTER each piece
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        self.send_response(action.status)
        self.send_header("Content-Length", str(len(action.body)))
        self.end_headers()
        self.wfile.write(action.body)

    def _handle(self) -> None:
        action = self._record()
        if action.rotate is not None:
            path, data = action.rotate
            tmp = Path(f"{path}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        self._respond(action)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def log_message(self, fmt: str, *args: object) -> None:
        pass


@pytest.fixture()
def upstream() -> Iterator[_UpstreamState]:
    server = _UpstreamServer(("127.0.0.1", 0), _DummyHandler)
    server.state = _UpstreamState()
    server.state.port = server.server_address[1]
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.state
    server.shutdown()
    server.server_close()


# ── injector process fixture ──────────────────────────────────────────────


@dataclasses.dataclass
class _Injector:
    port: int
    proc: subprocess.Popen[bytes]
    _conn: http.client.HTTPConnection | None = None

    def request(
        self, method: str, path: str, *, headers: dict[str, str] | None = None, body: bytes = b""
    ) -> http.client.HTTPResponse:
        # one connection per request; the PREVIOUS one stays open until now
        # so the caller can still read the returned response body
        if self._conn is not None:
            self._conn.close()
        self._conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self._conn.request(method, path, body=body or None, headers=headers or {})
        return self._conn.getresponse()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _launch(upstream_port: int, token: Path, secret: Path, *extra: str) -> _Injector:
    port = _free_port()
    proc = subprocess.Popen(  # nosec B603 - fixed argv, repo venv interpreter
        [
            sys.executable,
            "-m",
            "vesma.mcp_injector",
            "--port",
            str(port),
            "--upstream-port",
            str(upstream_port),
            "--token-file",
            str(token),
            "--secret-file",
            str(secret),
            *extra,
        ],
        stderr=subprocess.PIPE,
    )
    injector = _Injector(port, proc)
    last: Exception | None = None
    for _ in range(100):
        if proc.poll() is not None:
            break  # startup refusal: the caller inspects the exit code
        try:
            if injector.request("GET", "/healthz").status == 200:
                return injector
        except OSError as exc:
            last = exc
        time.sleep(0.05)
    proc.terminate()
    stderr = (proc.stderr.read() if proc.stderr else b"").decode()
    raise AssertionError(f"injector never became ready ({last=}): {stderr}")


@pytest.fixture()
def cred_files(tmp_path: Path) -> tuple[Path, Path]:
    token, secret = tmp_path / "token", tmp_path / "secret"
    token.write_bytes(TOKEN_A)
    secret.write_bytes(SECRET)
    token.chmod(0o600)
    secret.chmod(0o600)
    return token, secret


@pytest.fixture()
def injector(upstream: _UpstreamState, cred_files: tuple[Path, Path]) -> Iterator[_Injector]:
    token, secret = cred_files
    started = _launch(upstream.port, token, secret)
    yield started
    started.proc.terminate()
    started.proc.wait(timeout=10)


# ── P0: fail-closed ───────────────────────────────────────────────────────


@pytest.mark.usefixtures("upstream")
@pytest.mark.parametrize(
    "leg", ["token-0644", "secret-0640", "missing-token", "empty-token", "bind-0.0.0.0"]
)
def test_startup_fail_closed_refusals(
    upstream: _UpstreamState, cred_files: tuple[Path, Path], leg: str
) -> None:
    """Startup refuses any credential-policy or bind violation (exit 2)."""
    token, secret = cred_files
    expect = "must be 0600"
    if leg == "token-0644":
        token.chmod(0o644)
    elif leg == "secret-0640":
        secret.chmod(0o640)
    elif leg == "missing-token":
        token = token.with_name("absent")
        expect = "No such file"
    elif leg == "empty-token":
        token = token.with_name("empty")
        token.write_bytes(b"")
        token.chmod(0o600)
        expect = "outside (0, 4096)"
    else:
        expect = "refusing to start"
    extra = ("--listen-host", "0.0.0.0") if leg == "bind-0.0.0.0" else ()
    proc = subprocess.Popen(  # nosec B603
        [
            sys.executable,
            "-m",
            "vesma.mcp_injector",
            "--port",
            str(_free_port()),
            "--upstream-port",
            str(upstream.port),
            "--token-file",
            str(token),
            "--secret-file",
            str(secret),
            *extra,
        ],
        stderr=subprocess.PIPE,
    )
    _, stderr = proc.communicate(timeout=15)
    assert proc.returncode == 2, stderr.decode()
    assert expect in stderr.decode()


@pytest.mark.parametrize("leg", ["token", "secret"])
def test_runtime_unreadable_credential_fail_closed_503(
    injector: _Injector, cred_files: tuple[Path, Path], leg: str
) -> None:
    """Runtime loss of a credential file -> fail-closed 503, core sees nothing."""
    target = cred_files[0] if leg == "token" else cred_files[1]
    target.chmod(0o000)
    try:
        resp = injector.request("POST", "/mcp", headers=AUTH)
        assert resp.status == 503
        assert json.loads(resp.read())["detail"] == "auth source unavailable"
    finally:
        target.chmod(0o600)


# ── L: listener surface ───────────────────────────────────────────────────


def test_peer_check_rejects_non_loopback() -> None:
    """T6: a non-loopback peer gets a raw 403 before request parsing."""
    from vesma.mcp_injector import InjectorHandler

    handler = InjectorHandler.__new__(InjectorHandler)
    handler.client_address = ("192.0.2.1", 55555)  # type: ignore[attr-defined]
    handler.wfile = io.BytesIO()  # type: ignore[attr-defined]
    handler.rfile = io.BytesIO(b"")  # type: ignore[attr-defined]
    handler.close_connection = False  # type: ignore[attr-defined]
    handler.handle()
    assert handler.wfile.getvalue().startswith(b"HTTP/1.1 403 Forbidden")  # type: ignore[attr-defined]
    assert handler.close_connection  # type: ignore[attr-defined]


def test_wrong_listener_secret_bare_403_t10(injector: _Injector, upstream: _UpstreamState) -> None:
    resp = injector.request("POST", "/mcp", headers={"Authorization": "Bearer wrong-secret"})
    assert resp.status == 403
    assert json.loads(resp.read()) == {"detail": "denied"}
    assert upstream.requests == []  # never reached the core


def test_missing_authorization_403(injector: _Injector, upstream: _UpstreamState) -> None:
    assert injector.request("POST", "/mcp").status == 403
    assert upstream.requests == []


def test_strip_then_inject_t5_t5prime(injector: _Injector, upstream: _UpstreamState) -> None:
    headers = {**AUTH, "X-Trace": "trace-1", "Accept": "text/event-stream"}
    resp = injector.request("POST", "/mcp", headers=headers, body=b'{"id":1}')
    assert resp.status == 200
    (req,) = upstream.requests
    # T5: exactly one fresh Bearer to the core; T5': the listener secret
    # NEVER reaches the core (no forwarded header value carries it).
    assert req.headers["authorization"] == f"Bearer {TOKEN_A.decode()}"
    assert all(SECRET not in value.encode() for value in req.headers.values())
    assert req.headers["host"] == f"127.0.0.1:{upstream.port}"
    assert req.headers["x-trace"] == "trace-1"
    assert req.body == b'{"id":1}'


def test_path_allowlist_404(injector: _Injector, upstream: _UpstreamState) -> None:
    resp = injector.request("POST", "/forbidden", headers=AUTH)
    assert resp.status == 404
    assert upstream.requests == []


def test_options_405_allow(injector: _Injector) -> None:
    resp = injector.request("OPTIONS", "/mcp", headers=AUTH)
    assert resp.status == 405
    assert resp.getheader("Allow") == "GET, POST, DELETE"


# ── U: upstream mechanics ─────────────────────────────────────────────────


def test_401_retry_once_on_byte_change(
    injector: _Injector, upstream: _UpstreamState, cred_files: tuple[Path, Path]
) -> None:
    """Rotation invariant (code layer): on a 401, retry EXACTLY once, only
    if the token file changed byte-wise between the attempts."""
    token, _ = cred_files
    upstream.script = [
        _Action(status=401, rotate=(str(token), TOKEN_B)),
        _Action(status=200),
    ]
    resp = injector.request("POST", "/mcp", headers=AUTH)
    assert resp.status == 200
    first, second = upstream.requests
    assert first.headers["authorization"] == f"Bearer {TOKEN_A.decode()}"
    assert second.headers["authorization"] == f"Bearer {TOKEN_B.decode()}"
    assert len(upstream.requests) == 2  # one retry, not more


def test_401_stable_token_no_retry(injector: _Injector, upstream: _UpstreamState) -> None:
    """A stable invalid token produces no retry: genuine auth failures
    stay visible to the client."""
    upstream.default = _Action(status=401)
    assert injector.request("POST", "/mcp", headers=AUTH).status == 401
    assert len(upstream.requests) == 1


def test_413_over_limit_body(upstream: _UpstreamState, cred_files: tuple[Path, Path]) -> None:
    started = _launch(upstream.port, *cred_files, "--max-body-bytes", "64")
    try:
        resp = started.request("POST", "/mcp", headers=AUTH, body=b"x" * 100)
        assert resp.status == 413
        # a follow-up request on a fresh connection succeeds: the drained
        # 413 path never poisoned the server
        assert started.request("POST", "/mcp", headers=AUTH).status == 200
    finally:
        started.proc.terminate()
        started.proc.wait(timeout=10)


def test_411_chunked_request_rejected(injector: _Injector) -> None:
    sock = socket.create_connection(("127.0.0.1", injector.port), timeout=10)
    try:
        sock.sendall(
            b"POST /mcp HTTP/1.1\r\nHost: x\r\n"
            b"Authorization: Bearer " + SECRET + b"\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        data = sock.recv(4096)
    finally:
        sock.close()
    assert data.startswith(b"HTTP/1.1 411")


def test_get_with_body_forwarded_bodyless_sec03(
    injector: _Injector, upstream: _UpstreamState
) -> None:
    """SEC-03 documented CURRENT behavior: a GET that carries a body is
    forwarded bodyless (GET bodies are never read or drained) with the
    client's Content-Length header intact - so a body-honoring upstream
    waits for bytes that never arrive, times out, and the client surfaces
    a 502. MCP streamable HTTP never sends GET bodies; the payload half is
    pinned here (the core never processed the request), the keep-alive
    desync hazard is documented in the module docstring.
    """
    conn = http.client.HTTPConnection("127.0.0.1", injector.port, timeout=15)
    try:
        conn.request("GET", "/mcp", body=b"AAAAA", headers={**AUTH, "Content-Length": "5"})
        resp = conn.getresponse()
        body = resp.read()
        assert resp.status == 502
        assert json.loads(body)["detail"] == "upstream error"
    finally:
        conn.close()
    assert upstream.requests == []  # the core never saw a valid request


def test_sse_passthrough_flush_per_chunk(injector: _Injector, upstream: _UpstreamState) -> None:
    """SSE is the functional gate: each upstream chunk must reach the
    client the moment it arrives (buffered SSE = hung long tool calls)."""
    upstream.chunk_pieces = [b"data: one\n\n", b"data: two\n\n"]
    upstream.chunk_delay = 0.6
    sock = socket.create_connection(("127.0.0.1", injector.port), timeout=10)
    t0 = time.monotonic()
    try:
        sock.sendall(
            b"GET /mcp HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + SECRET + b"\r\n\r\n"
        )
        received: bytes = b""
        while b"data: one" not in received and time.monotonic() - t0 < 5:
            received += sock.recv(4096)
    finally:
        sock.close()
    assert b"data: one" in received
    # the first event arrived BEFORE the upstream's post-piece delay elapsed
    # (a buffering proxy would deliver nothing until the stream ended ~1.2s)
    assert time.monotonic() - t0 < 0.55


def test_rate_limit_429(upstream: _UpstreamState, cred_files: tuple[Path, Path]) -> None:
    started = _launch(upstream.port, *cred_files, "--rate-max", "2", "--rate-window-sec", "60")
    try:
        assert started.request("POST", "/mcp", headers=AUTH).status == 200
        assert started.request("POST", "/mcp", headers=AUTH).status == 200
        limited = started.request("POST", "/mcp", headers=AUTH)
        assert limited.status == 429
        assert json.loads(limited.read())["detail"] == "rate limited"
    finally:
        started.proc.terminate()
        started.proc.wait(timeout=10)


def test_conn_cap_503(upstream: _UpstreamState, cred_files: tuple[Path, Path]) -> None:
    started = _launch(upstream.port, *cred_files, "--max-conns", "1")
    hold = threading.Event()
    upstream.hold = hold
    try:
        result: list[int] = []

        def occupant() -> None:
            result.append(started.request("POST", "/mcp", headers=AUTH).status)

        thread = threading.Thread(target=occupant, daemon=True)
        thread.start()
        time.sleep(0.3)  # let the occupant take the only slot
        assert started.request("POST", "/mcp", headers=AUTH).status == 503
        hold.set()
        thread.join(timeout=10)
        assert result == [200]
    finally:
        upstream.hold = None
        hold.set()
        started.proc.terminate()
        started.proc.wait(timeout=10)


def test_no_sse_get_flag(upstream: _UpstreamState, cred_files: tuple[Path, Path]) -> None:
    started = _launch(upstream.port, *cred_files, "--no-sse-get")
    try:
        resp = started.request("GET", "/mcp", headers=AUTH)
        assert resp.status == 405
        assert resp.getheader("Allow") == "POST, DELETE"
    finally:
        started.proc.terminate()
        started.proc.wait(timeout=10)


def test_healthz_shape(injector: _Injector) -> None:
    """healthz needs no secret and carries the minimal contract shape:
    {ok, token_file_readable, upstream_tcp_ok} - no paths/versions/pids."""
    resp = injector.request("GET", "/healthz")
    assert resp.status == 200
    assert json.loads(resp.read()) == {
        "ok": True,
        "token_file_readable": True,
        "upstream_tcp_ok": True,
    }
