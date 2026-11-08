"""vesma-mcp-injector: loopback auth-injecting reverse proxy for the vesma MCP core.

ArchCom 2026-11-06 (protocol + contract
``2026-11-06-mcp-token-seamless-injector``, memory card dbf0e063): seamless
MCP access for ageless harness sessions. Per request the proxy re-reads the
agent token file and injects exactly one fresh Bearer to the core; the
client authenticates to the proxy with a static listener secret. MCP-blind:
JSON-RPC bodies and Mcp-Session-Id pass through untouched; only the
Authorization header is rewritten.

This module is the CANONICAL SOURCE of the injector (Phase 1 product wave,
2026-11-08): the host-local deployment at ``~/.local/bin/vesma-mcp-injector``
is a byte-compatible copy of this artifact ported at SHA db209fc4... — the
live unit keeps running until its next update absorbs the repo form. Run as
``python -m vesma.mcp_injector --port 8789 --token-file <f> --secret-file <f>``
(same flags as the deployed script); stdlib only, no third-party imports.

ROTATION INVARIANT (contract section 5): rotation period (12h, 2x/day) must
stay strictly below the token TTL (24h). The one 401 retry on file change
closes the millisecond boundary race around the atomic token-file replace.
Both layers are mandatory; do not revert to a daily cadence.

Security posture:
  - binds 127.0.0.1 only (IPv4 literal; never 0.0.0.0, never "localhost");
  - peer-check on accept: any non-127.0.0.1 peer is dropped with a WARN
    (contract T6 — loopback TCP has no uid boundary, defense-in-depth on
    top of the bind);
  - upstream connections: 2s CONNECT timeout, NO read timeout (long SSE /
    tool-call streams must never be cut by an idle-read timer);
  - rate limiter: global sliding window, default 100 requests / 10s
    (--rate-max / --rate-window-sec). A bursty harness may raise it; a 429
    here is load-shedding, NOT an auth failure;
  - listener secret compared via hmac.compare_digest; mismatch -> bare 403;
  - client Authorization is always stripped, never forwarded upstream;
  - token/secret file unreadable -> fail-closed 503, core sees no garbage;
  - startup refuses to run unless both files are 0600 and owned by euid;
  - logs carry statuses and timings only, never headers, bodies or secrets.

Fix wave 2026-11-06 (ArchCom contract T6 + review): peer-check on accept,
2s upstream connect timeout, minimal healthz body, no upstream Server/Date
duplication, peer in the structured request line, 413 drains then closes
the connection, DELETE forwards its body with a consistent Content-Length.

Known SEC-03 behavior (documented, deliberate): a GET that carries a request
body is forwarded bodyless — GET bodies are never read or drained. An HTTP
client MUST NOT send a body with GET; the MCP streamable-HTTP transport never
does. A GET-with-body therefore desyncs the keep-alive framing of that
particular client connection (the unread bytes are misparsed as the next
request line); every other connection and all MCP traffic is unaffected.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import http.client
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("vesma-mcp-injector")

# Hop-by-hop headers (RFC 9110 7.6.1) - never forwarded in either direction.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

# Response headers the proxy restates itself (send_response emits its own
# Server/Date) - forwarding the upstream pair would duplicate them.
RESTATE_HEADERS = {"server", "date"}

CONNECT_TIMEOUT_S = 2.0  # upstream CONNECT only; reads stay unbounded (SSE)


class _UpstreamConnection(http.client.HTTPConnection):
    """Upstream socket with a 2s CONNECT timeout and NO read timeout.

    http.client's ``timeout`` applies to every socket op, so it cannot be
    used as-is: an idle-read timer would cut long SSE/tool-call streams.
    Instead: connect under the timeout, then unarm it for the data phase.
    """

    def connect(self) -> None:
        super().connect()
        self.sock.settimeout(None)


class _Cfg:
    """Runtime configuration; populated exactly once in :func:`main`."""

    listen_host: str
    listen_port: int
    up_host: str
    up_port: int
    token_file: str
    secret_file: str
    allowed_paths: tuple[str, ...]
    no_sse_get: bool
    max_body: int
    rate_max: int
    rate_window: float
    rate_events: deque[float]
    rate_lock: threading.Lock
    conn_sem: threading.BoundedSemaphore
    started: float


CFG = _Cfg()


def _strict_file_check(path: str, what: str) -> str | None:
    """Return an error string if the credential file violates policy, else None."""
    try:
        st = os.stat(path)
    except OSError as exc:
        return f"{what}: {path}: {exc.strerror}"
    if not os.path.isfile(path):
        return f"{what}: {path}: not a regular file"
    if st.st_mode & 0o777 != 0o600:
        return f"{what}: {path}: mode is {oct(st.st_mode & 0o777)}, must be 0600"
    if st.st_uid != os.geteuid():
        return f"{what}: {path}: owner uid {st.st_uid} != euid {os.geteuid()}"
    if not (0 < st.st_size < 4096):
        return f"{what}: {path}: size {st.st_size} outside (0, 4096)"
    return None


def _read_credential(path: str) -> bytes | None:
    """Read a credential file; on any error log strerror only and return None."""
    try:
        with open(path, "rb") as fh:
            data = fh.read().strip()
        return data or None
    except OSError as exc:
        LOG.error("credential file unreadable: %s: %s", path, exc.strerror)
        return None


def _rate_allow() -> bool:
    """Global sliding-window rate limit (protects the core from local runaways)."""
    now = time.monotonic()
    with CFG.rate_lock:
        while CFG.rate_events and now - CFG.rate_events[0] > CFG.rate_window:
            CFG.rate_events.popleft()
        if len(CFG.rate_events) >= CFG.rate_max:
            return False
        CFG.rate_events.append(now)
        return True


class InjectorHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "vesma-mcp-injector/1.0"
    sys_version = ""
    timeout = 120  # request-line/body stall guard; SSE waits happen upstream

    # -- plumbing ---------------------------------------------------------

    def handle(self) -> None:
        # T6 peer-check on accept: a non-loopback peer never reaches request
        # parsing. The 127.0.0.1 bind is the primary gate; this is the
        # defense-in-depth layer the contract mandates for loopback TCP
        # (no uid boundary at TCP level).
        if self.client_address[0] != "127.0.0.1":
            LOG.warning("peer rejected (non-loopback): %s", self.client_address[0])
            with contextlib.suppress(OSError):
                self.wfile.write(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            self.close_connection = True
            return
        super().handle()

    def log_message(self, fmt: str, *args: object) -> None:
        LOG.info("peer=127.0.0.1:%d %s", self.client_address[1], fmt % args)

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bare_deny(self, status: int) -> None:
        # Deliberately reason-free body: no oracle for wrong-listener probes.
        self._send_json(status, {"detail": "denied"} if status == 403 else {"detail": "rejected"})

    # -- health ------------------------------------------------------------

    def _healthz(self) -> None:
        token_ok = _read_credential(CFG.token_file) is not None
        try:
            with socket.create_connection((CFG.up_host, CFG.up_port), timeout=1.0):
                upstream_ok = True
        except OSError:
            upstream_ok = False
        # Contract shape: {ok, token_file_readable, upstream_tcp_ok} - no
        # versions, no pids/uptime, no file paths (error strings leak them).
        self._send_json(
            200,
            {
                "ok": True,
                "token_file_readable": token_ok,
                "upstream_tcp_ok": upstream_ok,
            },
        )

    # -- request entry points ----------------------------------------------

    def do_GET(self) -> None:
        self._proxy("GET")

    def do_POST(self) -> None:
        self._proxy("POST")

    def do_DELETE(self) -> None:
        self._proxy("DELETE")

    def do_OPTIONS(self) -> None:
        self._proxy("OPTIONS")

    # -- core --------------------------------------------------------------

    def _proxy(self, method: str) -> None:
        t0 = time.monotonic()
        path = self.path

        norm = path.split("?", 1)[0]
        if norm != "/":
            norm = norm.rstrip("/")
        if norm == "/healthz":
            self._healthz()
            return

        if norm not in CFG.allowed_paths:
            LOG.info("%s %s -> 404 (path not allowed)", method, path)
            self._send_json(404, {"detail": "not found"})
            self.close_connection = True  # request body never read
            return

        # 1. Listener secret: check, and treat as the ONLY client credential.
        secret = _read_credential(CFG.secret_file)
        if secret is None:
            # fail-closed before any body handling - close the connection
            # (same desync class as the 413 path)
            self._send_json(503, {"detail": "auth source unavailable"})
            self.close_connection = True
            return
        presented = self.headers.get("Authorization", "")
        if not (
            presented.startswith("Bearer ")
            and hmac.compare_digest(presented[7:].strip().encode("utf-8", "replace"), secret)
        ):
            LOG.warning("listener auth mismatch peer=127.0.0.1:%d", self.client_address[1])
            self._bare_deny(403)
            self.close_connection = True  # request body never read
            return

        # 2. Method gates.
        if method == "OPTIONS":
            self.send_response(405)
            self.send_header("Allow", "GET, POST, DELETE")
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.close_connection = True  # request body never read
            return
        if method == "GET" and CFG.no_sse_get:
            LOG.info("GET %s -> 405 (sse disabled by policy)", path)
            self.send_response(405)
            self.send_header("Allow", "POST, DELETE")
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.close_connection = True  # request body never read
            return
        if method not in ("GET", "POST", "DELETE"):
            self._bare_deny(405)
            self.close_connection = True  # request body never read
            return

        # 3. Load shedding before the core sees anything.
        if not _rate_allow():
            self._send_json(429, {"detail": "rate limited"})
            self.close_connection = True  # request body never read
            return
        if not CFG.conn_sem.acquire(blocking=False):
            self._send_json(503, {"detail": "overloaded"})
            self.close_connection = True  # request body never read
            return

        try:
            status, retried, changed = self._forward(method, path)
        finally:
            CFG.conn_sem.release()
        LOG.info(
            "peer=127.0.0.1:%d %s %s -> %s dur_ms=%d retried=%s token_changed=%s",
            self.client_address[1],
            method,
            path,
            status,
            int((time.monotonic() - t0) * 1000),
            retried,
            changed,
        )

    def _forward(self, method: str, path: str) -> tuple[int, bool, bool]:
        token = _read_credential(CFG.token_file)
        if token is None:
            # fail-closed 503 BEFORE the request body is read - the unread
            # body would desync a keep-alive connection, so it must close
            self._send_json(503, {"detail": "auth source unavailable"})
            self.close_connection = True
            return 503, False, False

        body = b""
        if method in ("POST", "DELETE"):
            te = self.headers.get("Transfer-Encoding", "").lower()
            if "chunked" in te:
                self._send_json(411, {"detail": "length required"})
                # chunked body bytes (if any) were never read - the connection
                # must not be reused in that state
                self.close_connection = True
                return 411, False, False
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except ValueError:
                self._send_json(400, {"detail": "bad content-length"})
                self.close_connection = True  # request body never read
                return 400, False, False
            if length > CFG.max_body:
                # Answer first (the client may stall mid-body), then drain
                # best-effort with a stall guard; the connection closes either
                # way - unread bytes must never desync a keep-alive.
                self._send_json(413, {"detail": "request body too large"})
                self._drain_request_body(length)
                self.close_connection = True
                return 413, False, False
            if length:
                try:
                    body = self.rfile.read(length)
                except OSError:
                    self._send_json(400, {"detail": "bad request body"})
                    self.close_connection = True  # body partially read
                    return 400, False, False
                if len(body) != length:
                    self._send_json(400, {"detail": "short request body"})
                    self.close_connection = True  # body partially read
                    return 400, False, False

        resp = self._upstream(method, path, body, token)
        retried = changed = False
        if resp is not None and resp.status == 401:
            # One retry, ONLY if the token file changed between attempts.
            # 401 means the core rejected the request pre-dispatch (no side
            # effects), so the retry is application-safe. A stable invalid
            # token produces no retry: genuine auth failures stay visible.
            fresh = _read_credential(CFG.token_file)
            if fresh is not None and fresh != token:
                changed = True
                retried = True
                with contextlib.suppress(Exception):  # best-effort close
                    resp.close()
                LOG.warning("401 with changed token file, retrying once")
                resp = self._upstream(method, path, body, fresh)
        if resp is not None:
            self._stream_response(resp, method)
        return (resp.status if resp is not None else 502), retried, changed

    def _drain_request_body(self, length: int) -> None:
        """Best-effort drain of an unread request body with a short stall
        guard - bounded work; the caller closes the connection afterwards."""
        try:
            self.connection.settimeout(1.0)
            remaining = min(length, 4 * 1024 * 1024)
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                self.connection.settimeout(self.timeout)

    def _upstream(
        self, method: str, path: str, body: bytes, token: bytes
    ) -> http.client.HTTPResponse | None:
        """One upstream connection per request (loopback connect is ~free)."""
        conn = _UpstreamConnection(CFG.up_host, CFG.up_port, timeout=CONNECT_TIMEOUT_S)
        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            lk = key.lower()
            if lk in HOP_BY_HOP or lk in ("authorization", "host"):
                continue  # strip client auth (T5') and re-derive Host
            headers[key] = value
        headers["Host"] = f"{CFG.up_host}:{CFG.up_port}"
        headers["Authorization"] = "Bearer " + token.decode("utf-8", "replace")
        try:
            conn.request(method, path, body=body if body else None, headers=headers)
            return conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            LOG.error("upstream %s:%d failed: %r", CFG.up_host, CFG.up_port, exc)
            with contextlib.suppress(OSError):
                self._send_json(502, {"detail": "upstream error"})
            return None

    def _stream_response(self, resp: http.client.HTTPResponse, method: str) -> None:
        """Chunk-by-chunk passthrough with flush. Buffering SSE is a functional
        defect by contract: http.client's resp.read(n) accumulates across
        chunk boundaries, so chunked upstreams are parsed manually below."""
        try:
            self.send_response(resp.status)
            for key, value in resp.getheaders():
                lk = key.lower()
                if (
                    lk in HOP_BY_HOP
                    or lk in ("content-length", "transfer-encoding")
                    or lk in RESTATE_HEADERS
                ):
                    continue
                self.send_header(key, value)
            if resp.status in (204, 304) or method == "HEAD":
                self.end_headers()
                return
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if resp.headers.get("Transfer-Encoding", "").lower() == "chunked":
                self._passthrough_chunked(resp)
            else:
                self._passthrough_plain(resp)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            LOG.info("client disconnected mid-stream")
        except OSError as exc:
            LOG.error("response stream failed: %s", exc.strerror or repr(exc))
        finally:
            with contextlib.suppress(Exception):  # best-effort close
                resp.close()

    def _emit(self, data: bytes) -> None:
        self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _passthrough_chunked(self, resp: http.client.HTTPResponse) -> None:
        """Manual chunked decoding: emit each upstream chunk the moment it
        arrives (an SSE event is one flush = one chunk = one client frame)."""
        fp = resp.fp
        while True:
            size_line = fp.readline(65552)
            if not size_line:
                return
            try:
                size = int(size_line.strip().split(b";", 1)[0], 16)
            except ValueError:
                LOG.error("upstream chunk framing broken, terminating stream")
                return
            if size == 0:
                for _ in range(8192):  # drain optional trailers
                    line = fp.readline(65552)
                    if line in (b"\r\n", b"\n", b""):
                        break
                return
            remaining = size
            while remaining > 0:
                data = fp.read(min(remaining, 65536))
                if not data:
                    return
                remaining -= len(data)
                self._emit(data)
            fp.read(2)  # CRLF terminating the chunk data

    def _passthrough_plain(self, resp: http.client.HTTPResponse) -> None:
        """Content-Length bodies: read EXACTLY length bytes per-arrival
        (never wait for EOF - keep-alive upstreams never send one).
        No Content-Length: read until EOF (legacy close-delimited)."""
        fp = resp.fp
        cl_header = resp.headers.get("Content-Length")
        if cl_header is None:
            while True:
                data = fp.read1(65536)
                if not data:
                    return
                self._emit(data)
            return
        try:
            remaining = int(cl_header)
        except ValueError:
            return
        while remaining > 0:
            data = fp.read1(min(remaining, 65536))
            if not data:
                return
            remaining -= len(data)
            self._emit(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8789)
    parser.add_argument("--upstream-host", default="127.0.0.1")
    parser.add_argument("--upstream-port", type=int, default=8787)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--secret-file", required=True)
    parser.add_argument(
        "--allow-path",
        action="append",
        default=[],
        help="allowed upstream path (default /mcp, repeatable)",
    )
    parser.add_argument(
        "--no-sse-get",
        action="store_true",
        help="answer 405 to GET on MCP path (POST-only fallback)",
    )
    parser.add_argument("--max-body-bytes", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--max-conns", type=int, default=64)
    parser.add_argument("--rate-max", type=int, default=100)
    parser.add_argument("--rate-window-sec", type=float, default=10.0)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    CFG.listen_host, CFG.listen_port = args.listen_host, args.port
    CFG.up_host, CFG.up_port = args.upstream_host, args.upstream_port
    CFG.token_file, CFG.secret_file = args.token_file, args.secret_file
    CFG.allowed_paths = tuple(
        (p if p == "/" else p.rstrip("/")) for p in (args.allow_path or ["/mcp"])
    )
    CFG.no_sse_get = args.no_sse_get
    CFG.max_body = args.max_body_bytes
    CFG.rate_max, CFG.rate_window = args.rate_max, args.rate_window_sec
    CFG.rate_events = deque()
    CFG.rate_lock = threading.Lock()
    CFG.conn_sem = threading.BoundedSemaphore(args.max_conns)
    CFG.started = time.monotonic()

    if CFG.listen_host != "127.0.0.1":
        LOG.error("refusing to start: listen host must be the IPv4 literal 127.0.0.1")
        return 2

    for path, what in ((CFG.token_file, "token"), (CFG.secret_file, "secret")):
        err = _strict_file_check(path, what)
        if err:
            LOG.error("refusing to start: %s", err)
            return 2

    server = ThreadingHTTPServer((CFG.listen_host, CFG.listen_port), InjectorHandler)
    server.daemon_threads = True

    def _graceful(signum: int, _frame: object) -> None:
        LOG.info("signal %d, shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    LOG.info(
        "listening %s:%d -> upstream %s:%d paths=%s token=%s (0600 ok) sse_get=%s",
        CFG.listen_host,
        CFG.listen_port,
        CFG.up_host,
        CFG.up_port,
        ",".join(CFG.allowed_paths),
        CFG.token_file,
        "enabled" if not CFG.no_sse_get else "disabled",
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
