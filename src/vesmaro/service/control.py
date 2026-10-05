"""Supervisor control socket — server side (contract control-socket v1).

Implements ``specs/control-socket/v1`` (1.0.0-draft.2): §3 paths/rights
(user profile), §4.1 socket-directory preflight, §4.2 the bind-side
TOCTOU/SYMLINK procedure, §4.3 JSONL envelope, §4.4 ``hello`` and
versioning, §4.5 methods, §4.6 the error-code registry and error-data
hygiene, §4.7 limits; §5 threat-model mitigations map to conformance
points CS-1..CS-16 (tests: ``tests/test_service_control.py``).

Design: the socket layer is programmed against the :class:`ControlBackend`
PROTOCOL (``typing.Protocol``), NOT against the concrete Supervisor — the
W6 integration wave lands the real adapter. Everything here is testable
against a fake backend.

Transport is ``AF_UNIX``/``SOCK_STREAM`` by construction; there is no code
path that can create a TCP listener (§4.7, CS-15).
"""

from __future__ import annotations

import contextlib
import errno
import itertools
import json
import logging
import os
import re
import select
import socket
import stat
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from vesmaro.service import layout

logger = logging.getLogger("vesmaro.service.control")

# ── Protocol constants (spec §4.3/§4.4/§4.6) ──────────────────────────

PROTOCOL_VERSION = 1
MIN_PROTOCOL = 1
SUPPORTED_MAJORS: list[int] = [PROTOCOL_VERSION]

SOCKET_NAME = "control.sock"
SOCKET_DIR_MODE = 0o700  # spec §3 (user profile)
MODE_SOCKET = 0o600  # spec §3; MUST NOT depend on umask (§3 note B)
LISTEN_BACKLOG = 16

# Error-code registry (spec §4.6). Ranges: <100 protocol, 100-199
# lifecycle, 200 authz, 500 infra.
ERR_BAD_REQUEST = 1
ERR_UNKNOWN_METHOD = 2
ERR_INVALID_PARAMS = 3
ERR_VERSION_UNSUPPORTED = 4
ERR_UNKNOWN_COMPONENT = 100
ERR_INVALID_STATE = 101
ERR_START_FAILED = 102
ERR_STOP_TIMEOUT = 103
ERR_PERMISSION_DENIED = 200
ERR_INTERNAL = 500

# Limits (spec §4.7).
MAX_LINE_BYTES = 1024 * 1024
RESPONSE_TIMEOUT_S = 10.0
DEFAULT_TAIL = 100
MAX_TAIL = 10_000
MAX_FOLLOWS_PER_PEER = 2
MAX_FOLLOWS_TOTAL = 8
FOLLOW_IDLE_TIMEOUT_S = 60.0
FOLLOW_MAX_DURATION_S = 3600.0
BIND_ATTEMPTS = 3

# Full manifest-name template (spec §4.7); regex fail → error 3, unknown
# in the registry → error 100 (name-injection defence, CS-13).
COMPONENT_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")

_SO_PEERCRED: int | None = getattr(socket, "SO_PEERCRED", None)  # Linux-only (§3 note C)
_RECV_CHUNK = 65536
_FOLLOW_POLL_S = 0.2  # follow-loop poll ceiling (idle/hard-cap reactivity)


def default_socket_path() -> tuple[Path, bool]:
    """``(path, used_fallback)`` for ``${XDG_RUNTIME_DIR}/vesma/control.sock``.

    An empty ``XDG_RUNTIME_DIR`` takes the layout §3.6 fallback
    (``~/.local/state/vesma/run/control.sock``) — the WARN line is
    emitted by :func:`vesmaro.service.layout.resolve_runtime_dir`.
    """
    resolution = layout.resolve_runtime_dir()
    return resolution.path / SOCKET_NAME, resolution.used_fallback


# ── Typed errors the server surface carries ───────────────────────────


class SocketDirUnsafeError(Exception):
    """§4.1 lax socket directory: refuse + carry the ready fix command.

    Attributes:
        path: the offending directory.
        problem: one-line human reason.
        fix_command: the ready chmod/chown command with the real path —
            printed for the operator, NEVER auto-executed.
    """

    def __init__(self, path: Path, problem: str, fix_command: str) -> None:
        suffix = f" (fix: {fix_command})" if fix_command else ""
        super().__init__(f"{problem}: {path}{suffix}")
        self.path = path
        self.problem = problem
        self.fix_command = fix_command


class BindRefusedError(Exception):
    """The §4.2 procedure refused the start (diagnostics in ``str``)."""


class BackendError(Exception):
    """Base for typed backend failures the server maps to lifecycle codes.

    Contract §4.6 data hygiene: error ``data`` of the 100-199 range may
    carry ONLY machine-usable fields (component name, FSM state, exit
    code) — never paths, env strings or argv.
    """


class UnknownComponentError(BackendError):
    """Registry has no such manifest → error 100."""

    def __init__(self, component: str, message: str | None = None) -> None:
        super().__init__(message or f"unknown component: {component}")
        self.component = component


class InvalidStateError(BackendError):
    """FSM forbids the transition → error 101."""

    def __init__(self, component: str, state: str, message: str | None = None) -> None:
        super().__init__(message or f"invalid state transition for {component!r} in {state!r}")
        self.component = component
        self.state = state


class StartFailedError(BackendError):
    """Start failed → error 102; ``reason`` MUST be a short machine token
    (no paths/env/argv — §4.6 data hygiene)."""

    def __init__(self, component: str, reason: str) -> None:
        super().__init__(f"start failed for {component!r}: {reason}")
        self.component = component
        self.reason = reason


class StopTimeoutError(BackendError):
    """Graceful stop budget expired → error 103."""

    def __init__(self, component: str) -> None:
        super().__init__(f"stop timed out for {component!r}")
        self.component = component


# ── Backend protocol (the W6 seam) ────────────────────────────────────


@runtime_checkable
class LogStream(Protocol):
    """One follow subscription as the backend hands it to the server.

    The server owns the lifecycle: it polls, forwards frames, and calls
    :meth:`close` exactly once (client disconnect, final answer or server
    shutdown frees the subscription — CS-14).
    """

    def poll(self, timeout: float) -> list[str]:
        """Block up to ``timeout``; return newly available lines (maybe empty)."""
        ...  # pragma: no cover - protocol definition

    def current_state(self) -> str:
        """Current FSM state of the source component (idle-close final answer)."""
        ...  # pragma: no cover - protocol definition

    @property
    def final_state(self) -> str | None:
        """State after the source stopped, or ``None`` while still running."""
        ...  # pragma: no cover - protocol definition

    def close(self) -> None:
        """Release the subscription (idempotent)."""
        ...  # pragma: no cover - protocol definition


@runtime_checkable
class ControlBackend(Protocol):
    """The supervisor surface the control socket serves (W6 adapter seam).

    Idempotence of ``start``/``stop`` (spec §4.5) is backend duty: an
    already-running start returns ``"already-running"``, an already-stopped
    stop returns ``"already-stopped"``. State strings are opaque FSM
    values owned by specs/service-lifecycle/v1.
    """

    def component_status(self, component: str | None) -> dict[str, Any]:
        """Whole status tree or one component entry (spec §4.5 status)."""
        ...  # pragma: no cover - protocol definition

    def start_component(self, component: str) -> str:
        """Start; returns the new FSM state or ``"already-running"``."""
        ...  # pragma: no cover - protocol definition

    def stop_component(self, component: str, *, force: bool) -> str:
        """Stop (``force`` skips the graceful phase); state or ``"already-stopped"``."""
        ...  # pragma: no cover - protocol definition

    def restart_component(self, component: str) -> str:
        """Restart; returns the new FSM state (not idempotent, spec §4.5)."""
        ...  # pragma: no cover - protocol definition

    def component_logs(self, component: str, *, tail: int) -> list[str]:
        """Last ``tail`` structural lines (spec §4.5 logs, follow=false)."""
        ...  # pragma: no cover - protocol definition

    def stream_logs(self, component: str, *, tail: int) -> LogStream:
        """Open a follow subscription replaying up to ``tail`` history lines
        first (spec §4.5 logs, follow=true)."""
        ...  # pragma: no cover - protocol definition

    def health(self, component: str | None) -> dict[str, Any]:
        """Global + per-component health map, or one component (spec §4.5)."""
        ...  # pragma: no cover - protocol definition


# ── Wire helpers (shared server/client; JSONL framing, spec §4.3) ─────


class LineTooLongError(Exception):
    """Request line exceeded ``MAX_LINE_BYTES`` (spec §4.7 → error 1/close)."""

    def __init__(self, size: int) -> None:
        super().__init__(f"frame of {size} bytes exceeds the {MAX_LINE_BYTES}-byte limit")
        self.size = size


class _LineReader:
    """Bounded JSONL reader over a stream socket (one ``\\n`` per frame)."""

    def __init__(self, sock: socket.socket, max_line: int = MAX_LINE_BYTES) -> None:
        self._sock = sock
        self._max = max_line
        self._buf = bytearray()

    def read_line(self) -> bytes | None:
        """Next line without the ``\\n``; ``None`` on EOF; raises LineTooLongError."""
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0:
                line = bytes(self._buf[:idx])
                del self._buf[: idx + 1]
                if len(line) > self._max:
                    raise LineTooLongError(len(line))
                return line
            if len(self._buf) > self._max:
                raise LineTooLongError(len(self._buf))
            chunk = self._sock.recv(_RECV_CHUNK)
            if not chunk:
                return None
            self._buf.extend(chunk)


def encode_frame(payload: dict[str, Any]) -> bytes:
    """One JSONL frame (UTF-8, compact separators, ``\\n``-terminated)."""
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def send_frame(sock: socket.socket, payload: dict[str, Any]) -> bool:
    """Best-effort frame write; ``False`` when the peer is gone."""
    try:
        sock.sendall(encode_frame(payload))
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class _Request:
    """A validated request envelope (spec §4.3)."""

    id: int
    method: str
    params: dict[str, Any]


@dataclass(frozen=True)
class _FrameError:
    """Envelope-level rejection: the reply frame to send (then close)."""

    id: int
    code: int
    message: str

    def payload(self) -> dict[str, Any]:
        return {"id": self.id, "error": {"code": self.code, "message": self.message}}


def _parse_request(line: bytes) -> _Request | _FrameError:
    """Validate one request frame against the §4.3 envelope.

    Form violations → ``_FrameError`` (error 1 + close). Absent
    ``params`` is legal (treated as ``{}``); a non-dict ``params`` is a
    params-level problem, rejected by the dispatcher as error 3.
    """
    try:
        doc = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _FrameError(0, ERR_BAD_REQUEST, "malformed JSON frame")
    if not isinstance(doc, dict):
        return _FrameError(0, ERR_BAD_REQUEST, "frame must be a JSON object")
    frame_id = doc.get("id")
    if isinstance(frame_id, bool) or not isinstance(frame_id, int):
        return _FrameError(0, ERR_BAD_REQUEST, "integer 'id' is required")
    method = doc.get("method")
    if not isinstance(method, str):
        return _FrameError(frame_id, ERR_BAD_REQUEST, "string 'method' is required")
    params = doc.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _FrameError(frame_id, ERR_BAD_REQUEST, "'params' must be an object")
    return _Request(id=frame_id, method=method, params=params)


def _is_response_frame(line: bytes) -> bool:
    """True for a well-formed RESPONSE envelope (probe validation, §4.2-2)."""
    try:
        doc = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if (
        not isinstance(doc, dict)
        or isinstance(doc.get("id"), bool)
        or not isinstance(doc.get("id"), int)
    ):
        return False
    return ("result" in doc and "error" not in doc) or ("error" in doc and "result" not in doc)


def hello_result() -> dict[str, Any]:
    """The §4.4 hello success payload (engine version, not contract version)."""
    try:
        from vesmaro import __version__

        engine_version: str = __version__
    except Exception:  # pragma: no cover - metadata always present in installs
        engine_version = "0.0.0"
    return {
        "protocol_version": PROTOCOL_VERSION,
        "min_protocol": MIN_PROTOCOL,
        "server_version": engine_version,
        "pid": os.getpid(),
    }


# ── §4.1 directory preflight (shared server/client) ───────────────────


def check_socket_dir_rights(dir_path: Path, euid: int) -> None:
    """Refuse lax directories: group/world-writable or foreign owner (§4.1).

    Raises :class:`SocketDirUnsafeError` with the ready fix command; callers
    print it but MUST NOT auto-execute it.
    """
    try:
        info = os.stat(dir_path)
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise SocketDirUnsafeError(
            dir_path, f"socket directory unavailable ({reason})", ""
        ) from exc
    if info.st_uid != euid:
        raise SocketDirUnsafeError(
            dir_path,
            f"socket directory is owned by uid {info.st_uid}, not {euid}",
            f"chown {euid} {dir_path}",
        )
    if info.st_mode & 0o022:
        raise SocketDirUnsafeError(
            dir_path,
            "socket directory is group- or world-writable",
            f"chmod 0700 {dir_path}",
        )


# ── §4.2 bind-side TOCTOU/SYMLINK procedure ───────────────────────────


@dataclass(frozen=True)
class _Target:
    """``lstat`` classification of an occupied socket path (step 3)."""

    kind: Literal["own-socket", "symlink", "not-socket", "foreign-socket", "gone"]
    uid: int | None = None


def _classify_path_target(path: Path, euid: int) -> _Target:
    """Classify WITHOUT following symlinks (``lstat``, §4.2 step 3)."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return _Target("gone")
    if stat.S_ISLNK(info.st_mode):
        return _Target("symlink", info.st_uid)
    if not stat.S_ISSOCK(info.st_mode):
        return _Target("not-socket", info.st_uid)
    if info.st_uid != euid:
        return _Target("foreign-socket", info.st_uid)
    return _Target("own-socket", info.st_uid)


def _probe_live_instance(path: Path, timeout: float) -> Literal["live", "stale", "refused"]:
    """``connect()``-probe of an EADDRINUSE path (§4.2 step 2).

    ``live`` — a peer answered with a well-formed response envelope
    within the timeout (any valid hello reply counts — including a major
    mismatch, which is still a running vesma). ``stale`` — the kernel
    positively reports no listener (ECONNREFUSED / ENOENT). ``refused`` —
    anything else: refuse the start with diagnostics and DELETE NOTHING.
    """
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect(str(path))
        hello = {"id": 0, "method": "hello", "params": {"protocol_version": PROTOCOL_VERSION}}
        probe.sendall(encode_frame(hello))
        reader = _LineReader(probe, MAX_LINE_BYTES)
        line = reader.read_line()
        if line is not None and _is_response_frame(line):
            return "live"
        return "refused"
    except (ConnectionRefusedError, FileNotFoundError):
        return "stale"
    except (OSError, LineTooLongError):
        return "refused"
    finally:
        probe.close()


def _apply_socket_mode(path: Path, mode: int) -> None:
    """Explicit chmod so socket rights never depend on umask (§3 note B)."""
    os.chmod(path, mode)


def _verify_bound_rights(bound: socket.socket, path: Path, euid: int) -> None:
    """Post-bind rights verification (§4.2 step 5): fatal on any mismatch.

    Linux note: ``fstat`` on a bound AF_UNIX fd reads the kernel sockfs
    pseudo-inode (mode always 0777, a different st_dev/st_ino), NOT the
    filesystem node DAC is enforced on. The verification therefore reads:
    - the bound FD via ``fstat`` — socket type + owner;
    - the bound PATH via ``stat`` — the on-disk node peers actually hit
      (mode 0600, own uid);
    - the fd↔path tie via ``getsockname``.
    Together they verify exactly what the spec asks («режим и владелец»,
    umask-independent per §3 note B).
    """
    fd_info = os.fstat(bound.fileno())
    if not stat.S_ISSOCK(fd_info.st_mode):
        raise BindRefusedError(f"bound fd is not a socket: {path}")
    if fd_info.st_uid != euid:
        raise BindRefusedError(
            f"bound socket fd owner is uid {fd_info.st_uid}, expected {euid}: {path}"
        )
    if bound.getsockname() != str(path):
        raise BindRefusedError(
            f"bound fd answers on {bound.getsockname()!r}, expected {str(path)!r}"
        )
    node_info = os.stat(path)
    if not stat.S_ISSOCK(node_info.st_mode):
        raise BindRefusedError(f"socket path is not a socket node: {path}")
    if node_info.st_uid != euid:
        raise BindRefusedError(
            f"socket node owner is uid {node_info.st_uid}, expected {euid}: {path}"
        )
    mode = stat.S_IMODE(node_info.st_mode)
    if mode != MODE_SOCKET:
        raise BindRefusedError(f"socket mode is {oct(mode)}, expected {oct(MODE_SOCKET)}: {path}")


def bind_control_socket(
    path: Path,
    *,
    euid: int | None = None,
    probe_timeout_s: float = RESPONSE_TIMEOUT_S,
    stale_settle_s: float = 0.1,
    attempts: int = BIND_ATTEMPTS,
    log: logging.Logger | None = None,
) -> socket.socket | None:
    """The §4.2 procedure, in order; returns the LISTENING socket.

    Returns ``None`` when a live instance answered the probe (the caller
    exits «already running» — single-instance guard). Raises
    :class:`SocketDirUnsafeError` (lax dir) and :class:`BindRefusedError` (every
    refusal branch: symlink / non-socket / foreign owner / non-answering
    occupant / fstat mismatch / attempts exhausted). A blind unlink of a
    live socket is impossible by construction: unlink happens only after
    a positive stale probe AND an own-uid socket ``lstat``.

    Race strengthening (still inside the §4.2 loop): a stale verdict is
    re-probed once after ``stale_settle_s`` — a concurrent winner that is
    inside its bind→serve window starts answering within the settle time,
    which turns the verdict into «already running» instead of unlinking
    its fresh socket (CS-7). Genuinely dead sockets stay stale across the
    settle re-probe.
    """
    log = log or logger
    euid = os.geteuid() if euid is None else euid
    if _SO_PEERCRED is None:
        raise BindRefusedError(
            "SO_PEERCRED is unavailable on this platform — control socket v1 is "
            "Linux-only (spec §3 note C); refusing to bind without the peer check"
        )
    dir_path = path.parent if str(path.parent) else Path(".")
    if dir_path.exists():
        check_socket_dir_rights(dir_path, euid)  # §4.1 symmetric hardening
    layout.ensure_dir(dir_path, SOCKET_DIR_MODE)

    last_error: Exception | None = None
    for _ in range(attempts):
        bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            bound.bind(str(path))
        except OSError as exc:
            last_error = exc
            if exc.errno != errno.EADDRINUSE:
                bound.close()
                raise BindRefusedError(f"bind {path} failed: {exc}") from exc
            verdict = _probe_live_instance(path, probe_timeout_s)
            if verdict == "stale" and stale_settle_s > 0:
                # settle: a concurrent winner inside its bind→serve window
                # starts answering here; a truly dead socket stays stale
                time.sleep(stale_settle_s)
                verdict = _probe_live_instance(path, probe_timeout_s)
            if verdict == "live":
                bound.close()
                return None  # single-instance guard: caller exits «already running»
            if verdict == "refused":
                bound.close()
                raise BindRefusedError(
                    f"{path} is occupied by a process that does not answer the "
                    "control protocol — refusing to touch it (spec §4.2 step 2)"
                ) from exc
            target = _classify_path_target(path, euid)
            if target.kind == "gone":  # vanished between probe and lstat — retry
                bound.close()
                continue
            if target.kind == "own-socket":
                os.unlink(str(path))
                log.warning("control: removed stale own-uid socket %s (no live listener)", path)
                bound.close()
                continue
            bound.close()
            if target.kind == "symlink":
                log.error("control: REFUSING %s — path is a symlink, never followed (§4.2)", path)
                raise BindRefusedError(
                    f"{path} is a symlink — refusing to start, never following it"
                ) from exc
            if target.kind == "foreign-socket":
                log.error(
                    "control: REFUSING %s — foreign-uid socket file (uid %s), not deleted",
                    path,
                    target.uid,
                )
                raise BindRefusedError(
                    f"{path} is a socket owned by uid {target.uid} — refusing to touch it"
                ) from exc
            log.error("control: REFUSING %s — path exists and is not a socket (§4.2)", path)
            raise BindRefusedError(
                f"{path} exists and is not a socket — refusing to delete it"
            ) from exc
        # bind succeeded → listen FIRST (any EADDRINUSE racer now gets a
        # connectable socket instead of a false-stale one), then explicit
        # mode, then the rights verification (§4.2 steps 4-5)
        bound.listen(LISTEN_BACKLOG)
        _apply_socket_mode(path, MODE_SOCKET)
        _verify_bound_rights(bound, path, euid)
        log.info(
            "control: listening on %s (dir %0o, socket %0o)", path, SOCKET_DIR_MODE, MODE_SOCKET
        )
        return bound
    raise BindRefusedError(f"bind {path}: still contested after {attempts} attempts ({last_error})")


# ── Server ────────────────────────────────────────────────────────────


def _peer_allowed(cred: tuple[int, int, int] | None, euid: int) -> bool:
    """§3 SO_PEERCRED defense-in-depth verdict (CS-3): the peer uid MUST
    equal the socket owner (the supervisor's effective uid). ``None``
    credentials (getsockopt failure) fail CLOSED."""
    return cred is not None and cred[1] == euid


@dataclass(frozen=True)
class BindResult:
    """Outcome of a control-socket bind."""

    status: Literal["bound", "already-running"]
    path: Path
    used_fallback: bool


@dataclass
class _FollowCounters:
    """§4.7 follow limits: per-peer and installation-wide."""

    per_peer: dict[str, int] = field(default_factory=dict)
    total: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def try_acquire(self, peer_key: str, max_peer: int, max_total: int) -> bool:
        with self.lock:
            if self.per_peer.get(peer_key, 0) >= max_peer or self.total >= max_total:
                return False
            self.per_peer[peer_key] = self.per_peer.get(peer_key, 0) + 1
            self.total += 1
            return True

    def release(self, peer_key: str) -> None:
        with self.lock:
            left = self.per_peer.get(peer_key, 0) - 1
            if left <= 0:
                self.per_peer.pop(peer_key, None)
            else:
                self.per_peer[peer_key] = left
            self.total = max(0, self.total - 1)


# §4.6 error-data hygiene for ``StartFailedError.reason``: backend code
# may attach arbitrary text; only a short lowercase token may reach
# client-visible data (names/tokens only — never paths/env/argv).
START_REASON_RE = re.compile(r"[a-z0-9_.-]{0,64}")


def client_safe_start_reason(reason: str) -> str:
    """Collapse an adversarial ``StartFailedError.reason`` to a safe token.

    Anything outside ``[a-z0-9_.-]{0,64}`` (uppercase, spaces, control
    chars, free text) is replaced by the neutral ``"start-failed"``.
    """
    return reason if START_REASON_RE.fullmatch(reason) else "start-failed"


class ControlServer:
    """JSONL request server over one bound AF_UNIX control socket."""

    def __init__(
        self,
        backend: ControlBackend,
        *,
        probe_timeout_s: float = RESPONSE_TIMEOUT_S,
        follow_idle_timeout_s: float = FOLLOW_IDLE_TIMEOUT_S,
        follow_max_duration_s: float = FOLLOW_MAX_DURATION_S,
        max_follows_per_peer: int = MAX_FOLLOWS_PER_PEER,
        max_follows_total: int = MAX_FOLLOWS_TOTAL,
        max_line_bytes: int = MAX_LINE_BYTES,
        log: logging.Logger | None = None,
    ) -> None:
        self._backend = backend
        self._log = log or logger
        self._probe_timeout_s = probe_timeout_s
        self._follow_idle_s = follow_idle_timeout_s
        self._follow_max_s = follow_max_duration_s
        self._max_follows_peer = max_follows_per_peer
        self._max_follows_total = max_follows_total
        self._max_line = max_line_bytes
        self._follows = _FollowCounters()
        self._listener: socket.socket | None = None
        self._sock_path: Path | None = None
        self._used_fallback = False
        self._stop = threading.Event()
        self._conns: list[threading.Thread] = []
        self._conns_lock = threading.Lock()
        self._conn_ids = itertools.count(1)

    # ── lifecycle ──

    @property
    def path(self) -> Path | None:
        return self._sock_path

    @property
    def bound(self) -> bool:
        return self._listener is not None

    def bind(self) -> BindResult:
        """Run the §4.2 procedure at the canonical path; keep the listener."""
        resolution = layout.resolve_runtime_dir()
        self._used_fallback = resolution.used_fallback
        return self.bind_at(resolution.path / SOCKET_NAME)

    def bind_at(self, path: Path) -> BindResult:
        """Bind an explicit path (same §4.2 procedure)."""
        bound = bind_control_socket(path, probe_timeout_s=self._probe_timeout_s, log=self._log)
        self._sock_path = path
        if bound is None:
            return BindResult("already-running", path, self._used_fallback)
        self._listener = bound
        return BindResult("bound", path, self._used_fallback)

    def serve_forever(self) -> None:
        """Accept loop until :meth:`stop`; one thread per connection.

        SO_PEERCRED runs on EVERY accept BEFORE the first byte is read
        (spec §3; CS-3): a uid mismatch is closed immediately.
        """
        listener = self._listener
        if listener is None:
            raise RuntimeError("serve_forever() before a successful bind()")
        listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                continue
            cred = self._peer_cred(conn)
            if cred is None or not _peer_allowed(cred, os.geteuid()):
                self._log.warning("control: peer uid check failed (cred=%s) — closing unread", cred)
                conn.close()
                continue
            thread = threading.Thread(
                target=self._serve_connection,
                args=(conn, cred[0]),
                name=f"vesma-control-{next(self._conn_ids)}",
                daemon=True,
            )
            with self._conns_lock:
                # Prune finished connection threads on every accept — the
                # accept loop is the list's only writer. Without this the
                # list grows without bound under steady polling (~100 MB
                # /day at 1 Hz; cascade 2026-10-05, P2-1).
                self._conns[:] = [t for t in self._conns if t.is_alive()]
                self._conns.append(thread)
            thread.start()
        self._cleanup()

    def stop(self) -> None:
        """Signal the accept loop to finish and release the socket path."""
        self._stop.set()
        listener = self._listener
        if listener is not None:
            with contextlib.suppress(OSError):
                listener.close()

    def active_follows(self) -> int:
        """Installation-wide live follow count (observability/tests)."""
        with self._follows.lock:
            return self._follows.total

    # ── internals ──

    def _cleanup(self) -> None:
        with self._conns_lock:
            threads = list(self._conns)
            self._conns.clear()
        for thread in threads:
            thread.join(timeout=2.0)
        if self._listener is not None:
            with contextlib.suppress(OSError):
                self._listener.close()
            self._listener = None
        self._unlink_own_socket()

    def _unlink_own_socket(self) -> None:
        """Remove the socket file ONLY if it is still our own socket."""
        path = self._sock_path
        if path is None:
            return
        if _classify_path_target(path, os.geteuid()).kind == "own-socket":
            with contextlib.suppress(OSError):
                os.unlink(str(path))

    def _peer_cred(self, conn: socket.socket) -> tuple[int, int, int] | None:
        """``(pid, uid, gid)`` via SO_PEERCRED; ``None`` when unavailable."""
        if _SO_PEERCRED is None:  # pragma: no cover - bind() already refused
            return None
        try:
            raw = conn.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", raw)
        except OSError:
            return None

    def _serve_connection(self, conn: socket.socket, peer_pid: int) -> None:
        with conn:
            peer_key = f"uid:{os.geteuid()} pid:{peer_pid}"
            reader = _LineReader(conn, self._max_line)
            hello_done = False
            while not self._stop.is_set():
                try:
                    line = reader.read_line()
                except LineTooLongError as exc:
                    reply: dict[str, Any] = {
                        "id": 0,
                        "error": {"code": ERR_BAD_REQUEST, "message": str(exc)},
                    }
                    send_frame(conn, reply)
                    break  # framing trust lost → close (spec §4.7 allows close)
                except OSError:
                    break  # client went away
                if line is None:
                    break
                if not line.strip():
                    continue
                request = _parse_request(line)
                if isinstance(request, _FrameError):
                    send_frame(conn, request.payload())
                    break  # envelope-form violation → error 1 + close
                if request.method == "hello":
                    outcome = self._handle_hello(request.params)
                    hello_done = not isinstance(outcome, _ProtoErrorReply)
                    self._log.debug("control: hello peer=%s ok=%s", peer_key, hello_done)
                    if not self._reply(conn, request.id, outcome):
                        break
                    continue
                if not hello_done:
                    reply_err = _ProtoErrorReply(
                        ERR_VERSION_UNSUPPORTED,
                        "protocol version is not negotiated — send hello first",
                        {"reason": "hello_required", "supported": SUPPORTED_MAJORS},
                    )
                    if not self._reply(conn, request.id, reply_err):
                        break
                    continue
                if not self._dispatch(conn, peer_key, request):
                    break

    def _reply(
        self,
        conn: socket.socket,
        frame_id: int,
        outcome: dict[str, Any] | _ProtoErrorReply,
    ) -> bool:
        payload: dict[str, Any]
        if isinstance(outcome, _ProtoErrorReply):
            error: dict[str, Any] = {"code": outcome.code, "message": outcome.message}
            if outcome.data is not None:
                error["data"] = outcome.data
            payload = {"id": frame_id, "error": error}
        else:
            payload = {"id": frame_id, "result": outcome}
        return send_frame(conn, payload)

    def _dispatch(self, conn: socket.socket, peer_key: str, request: _Request) -> bool:
        """Route one post-hello request; ``False`` ends the connection."""
        self._log.info("control: method=%s peer=%s", request.method, peer_key)
        if request.method == "logs" and request.params.get("follow") is True:
            return self._run_follow(conn, peer_key, request)
        outcome = self._call_method(request.method, request.params)
        return self._reply(conn, request.id, outcome)

    def _call_method(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any] | _ProtoErrorReply:
        """Params validation + backend call + honest error mapping (§4.6)."""
        try:
            if method == "hello":  # re-hello is legal and re-negotiates
                return self._handle_hello(params)
            if method == "status":
                component, err = _optional_component(params)
                if err:
                    return err
                return self._backend.component_status(component)
            if method == "start":
                component, err = _required_component(params)
                if err:
                    return err
                return {"state": self._backend.start_component(component)}
            if method == "stop":
                component, err = _required_component(params)
                if err:
                    return err
                force, err = _optional_bool(params, "force")
                if err:
                    return err
                return {"state": self._backend.stop_component(component, force=force)}
            if method == "restart":
                component, err = _required_component(params)
                if err:
                    return err
                return {"state": self._backend.restart_component(component)}
            if method == "logs":
                component, err = _required_component(params)
                if err:
                    return err
                tail, err = _optional_tail(params)
                if err:
                    return err
                return {"lines": self._backend.component_logs(component, tail=tail)}
            if method == "health":
                component, err = _optional_component(params)
                if err:
                    return err
                return self._backend.health(component)
            return _ProtoErrorReply(ERR_UNKNOWN_METHOD, f"unknown method: {method}")
        except UnknownComponentError as exc:
            return _ProtoErrorReply(ERR_UNKNOWN_COMPONENT, str(exc), {"component": exc.component})
        except InvalidStateError as exc:
            return _ProtoErrorReply(
                ERR_INVALID_STATE, str(exc), {"component": exc.component, "state": exc.state}
            )
        except StartFailedError as exc:
            return _ProtoErrorReply(
                ERR_START_FAILED,
                str(exc),
                {"component": exc.component, "reason": client_safe_start_reason(exc.reason)},
            )
        except StopTimeoutError as exc:
            return _ProtoErrorReply(ERR_STOP_TIMEOUT, str(exc), {"component": exc.component})
        except Exception:
            # §4.6: 500 details go to the supervisor log, never to the client.
            self._log.exception("control: internal error in %s", method)
            return _ProtoErrorReply(ERR_INTERNAL, "internal error")

    def _handle_hello(self, params: dict[str, Any]) -> dict[str, Any] | _ProtoErrorReply:
        """§4.4: major-version negotiation."""
        version = params.get("protocol_version")
        if isinstance(version, bool) or not isinstance(version, int):
            return _ProtoErrorReply(ERR_INVALID_PARAMS, "integer 'protocol_version' is required")
        if version != PROTOCOL_VERSION:
            return _ProtoErrorReply(
                ERR_VERSION_UNSUPPORTED,
                f"unsupported protocol major {version}",
                {"supported": SUPPORTED_MAJORS},
            )
        return hello_result()

    def _run_follow(self, conn: socket.socket, peer_key: str, request: _Request) -> bool:
        """``logs follow=true``: stream frames until a final answer (§4.5/§4.7)."""
        component, err = _required_component(request.params)
        if err:
            return self._reply(conn, request.id, err)
        tail, err = _optional_tail(request.params)
        if err:
            return self._reply(conn, request.id, err)
        if not self._follows.try_acquire(peer_key, self._max_follows_peer, self._max_follows_total):
            return self._reply(
                conn,
                request.id,
                _ProtoErrorReply(
                    ERR_INVALID_PARAMS,
                    "too many concurrent follow subscriptions "
                    f"(limits: {self._max_follows_peer} per peer, "
                    f"{self._max_follows_total} per installation)",
                ),
            )
        # One try/finally from acquire onward: a failed stream OPEN
        # (unknown component, backend crash) must release the slot too —
        # otherwise ghost follows accumulate and a legitimate follow hits
        # the per-peer limit (cascade 2026-10-05, P1).
        stream: LogStream | None = None
        try:
            try:
                stream = self._backend.stream_logs(component, tail=tail)
            except UnknownComponentError as exc:
                return self._reply(
                    conn,
                    request.id,
                    _ProtoErrorReply(ERR_UNKNOWN_COMPONENT, str(exc), {"component": component}),
                )
            except Exception:
                self._log.exception("control: internal error opening follow for %s", component)
                return self._reply(
                    conn, request.id, _ProtoErrorReply(ERR_INTERNAL, "internal error")
                )
            self._log.info("control: follow open component=%s peer=%s", component, peer_key)
            return self._pump_follow(conn, request.id, component, stream)
        finally:
            if stream is not None:
                stream.close()
                self._log.info("control: follow closed component=%s peer=%s", component, peer_key)
            self._follows.release(peer_key)

    def _pump_follow(
        self, conn: socket.socket, frame_id: int, component: str, stream: LogStream
    ) -> bool:
        """Frame pump: stream lines, enforce idle/hard-cap, final answer."""
        started = time.monotonic()
        idle_deadline = started + self._follow_idle_s
        hard_deadline = started + self._follow_max_s
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= hard_deadline:
                return self._finish_follow(conn, frame_id, stream.current_state())
            idle_left = idle_deadline - now
            if idle_left <= 0:
                return self._finish_follow(conn, frame_id, stream.current_state())
            try:
                lines = stream.poll(min(idle_left, _FOLLOW_POLL_S))
            except Exception:
                self._log.exception("control: follow source failed for %s", component)
                return self._reply(conn, frame_id, _ProtoErrorReply(ERR_INTERNAL, "internal error"))
            if lines:
                idle_deadline = time.monotonic() + self._follow_idle_s
                for text in lines:
                    frame = {"id": frame_id, "stream": {"component": component, "line": text}}
                    if not send_frame(conn, frame):
                        return False  # client gone → subscription freed by caller
            if stream.final_state is not None:
                return self._finish_follow(conn, frame_id, stream.final_state)
            if self._client_gone(conn):
                return False  # client disconnected → free the subscription (CS-14)
        return self._finish_follow(conn, frame_id, stream.current_state())

    def _finish_follow(self, conn: socket.socket, frame_id: int, state: str) -> bool:
        return send_frame(conn, {"id": frame_id, "result": {"state": state}})

    @staticmethod
    def _client_gone(conn: socket.socket) -> bool:
        """True when the peer closed (EOF) or the socket is broken.

        Readable PENDING bytes are deliberately NOT treated as «gone»:
        ``MSG_PEEK`` answering non-empty keeps the pump running — the
        follow still ends via its idle/hard-cap deadlines. Active
        mid-follow rogue-frame rejection is a W6 hardening item.
        """
        try:
            readable, _, _ = select.select([conn], [], [], 0)
        except (OSError, ValueError):
            return True
        if not readable:
            return False
        try:
            return conn.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True


# ── params validators (error 3 vs 100 split, spec §4.6/§4.7) ──────────


@dataclass(frozen=True)
class _ProtoErrorReply:
    """A dispatcher-level error reply (any registry code, §4.6)."""

    code: int
    message: str
    data: dict[str, Any] | None = None


_MISSING = object()


def _optional_component(params: dict[str, Any]) -> tuple[str | None, _ProtoErrorReply | None]:
    value = params.get("component", _MISSING)
    if value is _MISSING:
        return None, None
    return _validated_component(value)


def _required_component(params: dict[str, Any]) -> tuple[str, _ProtoErrorReply | None]:
    value = params.get("component", _MISSING)
    if value is _MISSING:
        return "", _ProtoErrorReply(ERR_INVALID_PARAMS, "'component' is required")
    name, err = _validated_component(value)
    return name or "", err


def _validated_component(value: Any) -> tuple[str | None, _ProtoErrorReply | None]:
    if not isinstance(value, str):
        return None, _ProtoErrorReply(ERR_INVALID_PARAMS, "'component' must be a string")
    if not COMPONENT_NAME_RE.fullmatch(value):
        return None, _ProtoErrorReply(ERR_INVALID_PARAMS, f"invalid component name: {value}")
    return value, None


def _optional_bool(params: dict[str, Any], key: str) -> tuple[bool, _ProtoErrorReply | None]:
    value = params.get(key, False)
    if not isinstance(value, bool):
        return False, _ProtoErrorReply(ERR_INVALID_PARAMS, f"'{key}' must be a boolean")
    return value, None


def _optional_tail(params: dict[str, Any]) -> tuple[int, _ProtoErrorReply | None]:
    value = params.get("tail", DEFAULT_TAIL)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0, _ProtoErrorReply(ERR_INVALID_PARAMS, "'tail' must be an integer")
    if not 1 <= value <= MAX_TAIL:
        return 0, _ProtoErrorReply(ERR_INVALID_PARAMS, f"'tail' must be within 1..{MAX_TAIL}")
    return value, None


__all__ = [
    "BIND_ATTEMPTS",
    "DEFAULT_TAIL",
    "ERR_BAD_REQUEST",
    "ERR_INTERNAL",
    "ERR_INVALID_PARAMS",
    "ERR_INVALID_STATE",
    "ERR_PERMISSION_DENIED",
    "ERR_START_FAILED",
    "ERR_STOP_TIMEOUT",
    "ERR_UNKNOWN_COMPONENT",
    "ERR_UNKNOWN_METHOD",
    "ERR_VERSION_UNSUPPORTED",
    "FOLLOW_IDLE_TIMEOUT_S",
    "FOLLOW_MAX_DURATION_S",
    "MAX_FOLLOWS_PER_PEER",
    "MAX_FOLLOWS_TOTAL",
    "MAX_LINE_BYTES",
    "MAX_TAIL",
    "MIN_PROTOCOL",
    "MODE_SOCKET",
    "PROTOCOL_VERSION",
    "RESPONSE_TIMEOUT_S",
    "SOCKET_DIR_MODE",
    "SOCKET_NAME",
    "SUPPORTED_MAJORS",
    "BackendError",
    "BindRefusedError",
    "BindResult",
    "ControlBackend",
    "ControlServer",
    "InvalidStateError",
    "LineTooLongError",
    "LogStream",
    "SocketDirUnsafeError",
    "StartFailedError",
    "StopTimeoutError",
    "UnknownComponentError",
    "bind_control_socket",
    "check_socket_dir_rights",
    "default_socket_path",
    "encode_frame",
    "hello_result",
    "send_frame",
]
