"""Supervisor control socket — thin client (contract control-socket v1).

The CLI-facing side of ``specs/control-socket/v1``: §4.1 pre-flight check
against lax socket directories (refuse + print the ready fix command,
never auto-execute), the §4.3/§4.4 client session (``hello`` first on
every connection, 10 s response timeout outside follow streams), the
§4.5 method wrappers, and range-based handling of unknown error codes
(§4.6). One :class:`ControlClient` serves one command invocation; the CLI
holds no persistent connections except ``logs --follow`` (§4.8).
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vesma.service.control import (
    DEFAULT_TAIL,
    FOLLOW_IDLE_TIMEOUT_S,
    PROTOCOL_VERSION,
    RESPONSE_TIMEOUT_S,
    SocketDirUnsafeError,
    _LineReader,
    check_socket_dir_rights,
    default_socket_path,
    encode_frame,
)

logger = logging.getLogger("vesma.service.client")

# Follow streams are exempt from the 10 s response timeout (§4.7); the
# read budget must exceed the server's idle window with a 1.25x margin
# (currently 60 s -> 75 s) — derived, so retuning the server constant
# retunes the client with it.
FOLLOW_READ_TIMEOUT_S = FOLLOW_IDLE_TIMEOUT_S * 1.25


# ── Client-side errors ────────────────────────────────────────────────


class ControlClientError(Exception):
    """Base for every client-side failure the CLI maps to exit code 1."""


class SocketUnavailableError(ControlClientError):
    """No socket / no listener — the supervisor is probably not running."""


class PreflightError(ControlClientError):
    """§4.1 lax socket directory: refuse + the ready fix command."""

    def __init__(self, path: Path, problem: str, fix_command: str) -> None:
        super().__init__(
            f"{problem}: {path} (fix: {fix_command})" if fix_command else f"{problem}: {path}"
        )
        self.path = path
        self.problem = problem
        self.fix_command = fix_command


class ResponseTimeoutError(ControlClientError):
    """§4.7 response timeout (10 s outside follow) — safe to retry
    idempotent operations."""


class ProtocolError(ControlClientError):
    """The server replied with an error envelope (§4.6).

    Unknown codes are interpreted BY RANGE (§4.6): ``<100`` protocol,
    ``100-199`` lifecycle, ``200-299`` authz, ``500-599`` infra, anything
    else ``unknown``.
    """

    def __init__(self, code: int, message: str, data: dict[str, Any] | None) -> None:
        super().__init__(f"[error {code}] {message}")
        self.code = code
        self.message = message
        self.data = data or {}

    @property
    def code_range(self) -> str:
        if self.code <= 0:
            return "unknown"  # codes are 1-based; non-positive = malformed
        if self.code < 100:
            return "protocol"
        if self.code < 200:
            return "lifecycle"
        if self.code < 300:
            return "authz"
        if 500 <= self.code < 600:
            return "infra"
        return "unknown"


class ProtocolViolationError(ControlClientError):
    """The server sent a malformed frame — the installation is broken."""


# ── Path resolution + §4.1 pre-flight ─────────────────────────────────


def control_socket_path() -> tuple[Path, bool]:
    """``(socket path, used_fallback)`` — the §3/§3.6 user-profile path."""
    return default_socket_path()


def preflight(path: Path, euid: int | None = None) -> None:
    """§4.1: refuse lax socket dirs; raise :class:`PreflightError`.

    The fix command travels on the exception for the CLI to PRINT — the
    client never chmods/chowns anything by itself.
    """
    effective_uid = os.geteuid() if euid is None else euid
    dir_path = path.parent
    if not dir_path.exists():
        raise PreflightError(
            dir_path,
            "socket directory does not exist (is the supervisor running?)",
            "",
        )
    try:
        check_socket_dir_rights(dir_path, effective_uid)
    except SocketDirUnsafeError as exc:
        raise PreflightError(dir_path, exc.problem, exc.fix_command) from exc


# ── Client ────────────────────────────────────────────────────────────


class ControlClient:
    """One command's session: connect → hello → requests → close."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        response_timeout: float = RESPONSE_TIMEOUT_S,
        follow_read_timeout: float = FOLLOW_READ_TIMEOUT_S,
        skip_preflight: bool = False,
    ) -> None:
        self._path = path or control_socket_path()[0]
        self._response_timeout = response_timeout
        self._follow_read_timeout = follow_read_timeout
        self._skip_preflight = skip_preflight
        self._sock: socket.socket | None = None
        self._reader: _LineReader | None = None
        self._ids = itertools.count(1)
        self.server_info: dict[str, Any] = {}

    @property
    def path(self) -> Path:
        return self._path

    def connect(self) -> dict[str, Any]:
        """§4.1 pre-flight, connect, mandatory first ``hello`` (§4.4)."""
        if not self._skip_preflight:
            preflight(self._path)
        if not self._path.exists():
            raise SocketUnavailableError(f"control socket does not exist: {self._path}")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._response_timeout)
        try:
            sock.connect(str(self._path))
        except OSError as exc:
            sock.close()
            raise SocketUnavailableError(f"cannot connect to {self._path}: {exc}") from exc
        self._sock = sock
        self._reader = _LineReader(sock)
        self.server_info = self.request("hello", {"protocol_version": PROTOCOL_VERSION})
        return self.server_info

    def close(self) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
            self._reader = None

    def __enter__(self) -> ControlClient:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── method wrappers (spec §4.5/§4.8) ──

    def status(self, component: str | None = None) -> dict[str, Any]:
        params = {"component": component} if component else {}
        return self.request("status", params)

    def health(self, component: str | None = None) -> dict[str, Any]:
        params = {"component": component} if component else {}
        return self.request("health", params)

    def start(self, component: str) -> str:
        return str(self.request("start", {"component": component})["state"])

    def stop(self, component: str, *, force: bool = False) -> str:
        return str(self.request("stop", {"component": component, "force": force})["state"])

    def restart(self, component: str) -> str:
        return str(self.request("restart", {"component": component})["state"])

    def logs(self, component: str, *, tail: int = DEFAULT_TAIL) -> list[str]:
        result = self.request("logs", {"component": component, "tail": tail})
        return [str(line) for line in result.get("lines", [])]

    def follow_logs(
        self, component: str, on_line: Callable[[str], None], *, tail: int = DEFAULT_TAIL
    ) -> str:
        """``logs follow=true`` (§4.5): stream every frame line to
        ``on_line``; return the final state from the closing result frame.

        Blocks up to the server's idle window between frames; the
        connection must not be reused for other methods afterwards.
        """
        sock = self._sock
        reader = self._reader
        if sock is None or reader is None:
            raise ControlClientError("client is not connected — call connect() first")
        sock.settimeout(self._follow_read_timeout)
        request_id = next(self._ids)
        try:
            sock.sendall(
                encode_frame(
                    {
                        "id": request_id,
                        "method": "logs",
                        "params": {"component": component, "tail": tail, "follow": True},
                    }
                )
            )
            while True:
                line = reader.read_line()
                if line is None:
                    raise ProtocolViolationError(
                        "server closed the follow stream without a final answer"
                    )
                reply = _parse_reply(line)
                if "stream" in reply:
                    stream = reply["stream"]
                    if isinstance(stream, dict) and isinstance(stream.get("line"), str):
                        on_line(stream["line"])
                    continue
                return str(self._result_or_error(reply, request_id).get("state", ""))
        except TimeoutError as exc:
            raise ResponseTimeoutError("follow stream stalled beyond the idle window") from exc
        finally:
            sock.settimeout(self._response_timeout)

    # ── wire ──

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One request frame → result payload; errors raise ProtocolError."""
        sock = self._sock
        reader = self._reader
        if sock is None or reader is None:
            raise ControlClientError("client is not connected — call connect() first")
        frame: dict[str, Any] = {"id": next(self._ids), "method": method}
        if params is not None:
            frame["params"] = params
        try:
            sock.sendall(encode_frame(frame))
            while True:
                line = reader.read_line()
                if line is None:
                    raise ProtocolViolationError("server closed the connection mid-session")
                reply = _parse_reply(line)
                if reply.get("id") != frame["id"]:
                    continue  # a stale frame from a previous pipelined call
                return self._result_or_error(reply, frame["id"])
        except TimeoutError as exc:
            raise ResponseTimeoutError(
                f"no response within {self._response_timeout}s (method: {method})"
            ) from exc
        except OSError as exc:
            raise ControlClientError(f"connection lost during {method}: {exc}") from exc

    @staticmethod
    def _result_or_error(reply: dict[str, Any], frame_id: int) -> dict[str, Any]:
        if "error" in reply:
            error = reply["error"]
            if not isinstance(error, dict):
                raise ProtocolViolationError("error envelope is not an object")
            data = error.get("data")
            raw_code = error.get("code", -1)
            if isinstance(raw_code, bool) or not isinstance(raw_code, int):
                # a broken/rogue server: ``int(<junk>)`` would leak a raw
                # ValueError — a contract violation is the typed answer
                raise ProtocolViolationError(f"error code is not an integer: {raw_code!r}")
            raise ProtocolError(
                raw_code,
                str(error.get("message", "")),
                data if isinstance(data, dict) else None,
            )
        result = reply.get("result")
        if "result" not in reply:
            raise ProtocolViolationError(f"frame {frame_id} carries neither result nor error")
        return result if isinstance(result, dict) else {}


def _parse_reply(line: bytes) -> dict[str, Any]:
    try:
        doc = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolViolationError(f"malformed frame from server: {exc}") from exc
    if not isinstance(doc, dict):
        raise ProtocolViolationError("server frame is not a JSON object")
    return doc
