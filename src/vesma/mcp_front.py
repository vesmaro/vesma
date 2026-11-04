"""MCP component front (ADR-0050 W2, Ф2) — a stateless streamable-HTTP proxy.

A THIN transport gateway that gives harnesses ONE entry point to the
core ``/mcp`` plane: it accepts any MCP request on its own port and
transparently forwards it to the core, streaming in both directions.
It is a platform COMPONENT (``contrib/mcp-front/`` — manifest, env
template, README): the component supervisor owns the lifecycle, this
module only defines the child's ASGI app and its child-process
entrypoint (``python -m vesma.mcp_front``).

Contract (ADR-0050 §3, C2) — the front is an IDENTITY CARRIER, never an
authorizer:

* **No authentication of its own.** The core's W1 gate
  (``api/mcp_endpoint.py``) is the ONLY authority on the plane; the
  front forwards ``Authorization`` (and every other header) untouched
  and unread — there is deliberately no second path that could widen or
  narrow what a harness token may do. The core scopes every
  front-originated request by the token's harness identity (W3).
* **No body mutation.** Request and response bodies are streamed as raw
  bytes (``aiter_raw`` — content-encoding stays untouched), never
  parsed, never re-encoded. The single exception is the SIZE GUARD
  below, which refuses rather than mutates.
* **Body size guard** — the SDK's streamable-HTTP ceiling is inherited
  (4 MiB, ``mcp.server.streamable_http`` ``MAXIMUM_MESSAGE_SIZE``): a
  declared ``Content-Length`` above the ceiling is refused 413 BEFORE
  anything is forwarded; a chunked body is counted while streaming and
  refused 413 the moment it crosses. The ceiling is the same one the
  core would enforce, applied a hop earlier.
* **Core unreachable → a FIXED 503 JSON.** The reason goes to the log
  only (the mesh data-gate discipline) — the wire answer never carries
  exception text, paths or URLs.
* **Single-plane mapping.** The front is a ONE-URL gateway: whatever
  path the harness dials, the request lands on the configured target
  URL verbatim (default ``http://127.0.0.1:8787/mcp/`` — the core's
  streamable plane). Streamable-HTTP clients POST/GET the exact URL
  they are configured with, so a harness pointed at the front address
  reaches the plane regardless of the local path; ``/healthz`` is the
  one local exception (the component supervisor's probe).
* **Loopback-only target (C2 guard).** The target is where harness
  Authorization headers GO — pointing it off-box would ship harness
  tokens to a foreign host. ``validate_target`` refuses any non-loopback
  http(s) target at construction, at CLI parse time and at Settings
  construction (``mcp.front.target``). Same-host is the ratified Ф2
  topology (UDS, the preferred future transport, is same-host by
  definition); a remote-core deployment is a deliberate future opt-out,
  not a silent possibility.

Isolation guards: the mcp SDK is NEVER imported here (ADR-0023 — the
SDK lives only in ``vesma.mcp_server``); the module imports stdlib +
``httpx`` (+ ``starlette.types`` type aliases only) and the CLI is pure
``argparse`` (a child-process entrypoint must not drag the click CLI
stack into the component venv path).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from urllib.parse import urlsplit

import httpx
from starlette.types import Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

#: The core MCP plane the front forwards to (the ``vesma serve`` app's
#: streamable-HTTP mount; ``api/mcp_endpoint.MCP_HTTP_MOUNT_PATH``).
DEFAULT_TARGET = "http://127.0.0.1:8787/mcp/"

#: Default listen port of the component front (the port the prod
#: ``mcp`` config section reserves for the MCP plane).
DEFAULT_PORT = 8790

#: Default upstream timeout, seconds. Applied to connect/read/write/pool
#: alike (``httpx.Timeout``): MCP tool calls can legitimately run long
#: against a big store, so the default is generous — a stateless JSON
#: response either arrives or the request fails as 503.
DEFAULT_TIMEOUT_SEC = 60.0

#: Request-body ceiling — the SDK streamable-HTTP default
#: (``mcp.server.streamable_http`` MAXIMUM_MESSAGE_SIZE, 4 MiB). The
#: front inherits it so an oversized request dies at the front instead
#: of consuming a hop first.
MAX_BODY_BYTES = 4 * 1024 * 1024

#: The one local (never-forwarded) path: the component supervisor's
#: health probe. Always enabled — there is deliberately no flag to turn
#: it off (a supervised child without a probe is an unmonitorable child).
HEALTHZ_PATH = "/healthz"

#: The FIXED wire answer for an unreachable core — no exception text on
#: the wire (the cause is logged, never echoed; the mesh data-gate rule).
_UNAVAILABLE_DETAIL = "vesma core MCP plane is unreachable (mcp.front.target)"

#: The FIXED wire answer for an oversized body.
_OVERSIZE_DETAIL = "request body exceeds the maximum MCP message size"

#: env var carrying the target when ``--target`` is not given (the
#: component manifest's env.d slot sets exactly this key).
TARGET_ENV_VAR = "VESMA_FRONT_TARGET"

#: Hop-by-hop headers (RFC 9110 §7.6.1) — proxy semantics, never
#: forwarded in either direction. ASGI header names arrive lowercase;
#: the sets hold BYTES so the membership check type-checks against the
#: raw header pairs.
_HOP_BY_HOP = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)

#: Request headers the front does NOT forward: hop-by-hop plus the
#: framing headers the ASGI/httpx legs own themselves (``host`` is the
#: TARGET's host, ``content-length`` is recomputed by httpx from the
#: streamed body, ``expect`` has no ASGI meaning).
_REQUEST_DROP = _HOP_BY_HOP | {b"host", b"content-length", b"expect"}

#: Response headers the front does NOT forward: hop-by-hop only — the
#: body is forwarded RAW (``aiter_raw``), so ``content-length`` stays
#: exact and content-encoding survives untouched.
_RESPONSE_DROP = _HOP_BY_HOP


def validate_target(url: str) -> str:
    """Return ``url`` unchanged if it is a legal front target, else raise.

    Legal = http(s) URL whose host is loopback (``localhost``, the
    127.0.0.0/8 literals, ``::1``). The C2 guard: the front is an
    identity carrier — every harness ``Authorization`` header it
    forwards lands on this URL, so a non-loopback target is a
    token-exfiltration footgun and is refused fail-closed (a remote-core
    deployment needs an explicit future opt-out, never a silent default).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(
            f"front target must be an http(s) URL, got scheme {parts.scheme!r} ({url!r})"
        )
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"front target has no host part: {url!r}")
    if host != "localhost" and not host.startswith("127.") and host != "::1":
        raise ValueError(
            f"front target must be loopback (the front carries harness "
            f"Authorization headers — ADR-0050 C2), got host {host!r} ({url!r})"
        )
    return url


class _RequestBodyTooLargeError(Exception):
    """A streamed request body crossed :data:`MAX_BODY_BYTES` mid-stream."""


class _ClientDisconnectedError(Exception):
    """The harness went away mid-request (ASGI ``http.disconnect``)."""


async def _request_body(receive: Receive, limit: int) -> AsyncIterator[bytes]:
    """Stream the ASGI request body into the upstream request.

    Counting guard: bodies WITHOUT a declared ``Content-Length``
    (chunked uploads) are counted here and refuse the moment they cross
    ``limit`` — a lying or absent header cannot sneak a giant body past
    the pre-check. ``http.disconnect`` raises :class:`_ClientDisconnected`
    (the caller closes quietly — there is nobody left to answer).
    """
    total = 0
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunk: bytes = message.get("body", b"") or b""
            if chunk:
                total += len(chunk)
                if total > limit:
                    raise _RequestBodyTooLargeError
                yield chunk
            if not message.get("more_body", False):
                return
        elif message["type"] == "http.disconnect":
            raise _ClientDisconnectedError


async def _send_json(send: Send, *, status: int, body: dict[str, str]) -> None:
    """Send a bare JSON ASGI response (the core gate's helper shape)."""
    payload = json.dumps(body).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode("ascii")),
    ]
    message: Message = {"type": "http.response.start", "status": status, "headers": headers}
    await send(message)
    await send({"type": "http.response.body", "body": payload})


class McpFront:
    """The stateless proxy ASGI app (one instance per front process).

    ``client`` is a test seam (the ``mesh_bin`` pattern): tests inject an
    ``httpx.AsyncClient`` whose transport reaches an in-process fake
    core; the production path builds its own pooled client on first use
    (lifespan startup when run under uvicorn, first request otherwise).
    A client built by the app itself is closed on shutdown/aclose; an
    injected client stays owned by the injector.
    """

    def __init__(
        self,
        target: str = DEFAULT_TARGET,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        max_body_bytes: int = MAX_BODY_BYTES,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._target = validate_target(target)
        self._timeout = timeout
        self._max_body_bytes = max_body_bytes
        self._client = client
        self._injected = client is not None

    @property
    def target(self) -> str:
        """The validated upstream URL (for startup logs — never secret)."""
        return self._target

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))
        return self._client

    async def aclose(self) -> None:
        """Close the owned client (idempotent; injected clients untouched)."""
        if self._client is not None and not self._injected:
            await self._client.aclose()
        if not self._injected:
            self._client = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope_type != "http":  # websocket etc. — not a front surface
            return
        await self._proxy(scope, receive, send)

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        """Build the pooled client at startup, close it at shutdown."""
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                self._ensure_client()
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _proxy(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("path") == HEALTHZ_PATH:
            # Local liveness only: the supervisor's probe answers here and
            # never reaches the core (a dead core is a 503 on real requests,
            # not a red front — the front itself is stateless and alive).
            await _send_json(send, status=200, body={"status": "ok", "service": "vesma-mcp-front"})
            return

        method: str = scope["method"].upper()
        raw_headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        forwarded = [(n, v) for (n, v) in raw_headers if n.lower() not in _REQUEST_DROP]
        # The front owns upstream connection management (hop-by-hop was
        # dropped above, and httpx would otherwise re-add its own
        # keep-alive): one deterministic Connection: close per request —
        # the stateless MCP plane is single-roundtrip anyway.
        forwarded.append((b"connection", b"close"))

        # Fast-fail size guard on the DECLARED length — nothing is read,
        # nothing is forwarded. The streamed counter below is the
        # authoritative guard for absent/lying declarations.
        for name, value in raw_headers:
            if name == b"content-length":
                try:
                    declared = int(value.decode("latin-1"))
                except ValueError:
                    break  # malformed framing — let the counter decide
                if declared > self._max_body_bytes:
                    logger.warning(
                        "mcp_front: refused oversized body (%d > %d bytes)",
                        declared,
                        self._max_body_bytes,
                    )
                    await _send_json(send, status=413, body={"detail": _OVERSIZE_DETAIL})
                    return
                break

        client = self._ensure_client()
        request = client.build_request(
            method,
            self._target,
            headers=forwarded,
            content=_request_body(receive, self._max_body_bytes),
        )
        try:
            response = await client.send(request, stream=True)
        except _RequestBodyTooLargeError:
            logger.warning(
                "mcp_front: refused oversized chunked body (limit %d bytes)",
                self._max_body_bytes,
            )
            await _send_json(send, status=413, body={"detail": _OVERSIZE_DETAIL})
            return
        except _ClientDisconnectedError:
            return  # nobody to answer — close quietly
        except httpx.HTTPError as exc:
            logger.warning("mcp_front: core unreachable (%s: %s)", type(exc).__name__, exc)
            await _send_json(send, status=503, body={"detail": _UNAVAILABLE_DETAIL})
            return

        response_headers = [
            (n, v) for (n, v) in response.headers.raw if n.lower() not in _RESPONSE_DROP
        ]
        started = False
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": response.status_code,
                    "headers": response_headers,
                }
            )
            started = True
            async for chunk in response.aiter_raw():
                if chunk:
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except httpx.HTTPError as exc:
            logger.warning("mcp_front: core died mid-response (%s: %s)", type(exc).__name__, exc)
            if not started:  # pragma: no cover - defensive (headers already read)
                await _send_json(send, status=503, body={"detail": _UNAVAILABLE_DETAIL})
            else:
                # The honest outcome of a transparent proxy: the harness sees a
                # truncated body and a closed connection, never fabricated data.
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            await response.aclose()


def resolve_target(explicit: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """Resolve the front target: ``--target`` > ``VESMA_FRONT_TARGET`` > default.

    ``env`` is a test seam; ``None`` reads the process environment. The
    result always passes :func:`validate_target` (fail-closed C2 guard).
    """
    source = os.environ if env is None else env
    value = explicit or source.get(TARGET_ENV_VAR) or DEFAULT_TARGET
    return validate_target(value)


def build_arg_parser() -> argparse.ArgumentParser:
    """The child-process CLI (pure argparse — no click in the child path)."""
    parser = argparse.ArgumentParser(
        prog="python -m vesma.mcp_front",
        description=(
            "vesma MCP component front (ADR-0050 Ф2): stateless "
            "streamable-HTTP proxy to the core /mcp plane. Carries the "
            "harness Authorization header untouched — it authenticates "
            "nothing itself (C2)."
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (loopback-only is the v1 platform posture)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"listen port (default {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--target",
        default=None,
        help=(
            f"core MCP plane URL (loopback only; default: ${TARGET_ENV_VAR} "
            f"env var, then {DEFAULT_TARGET})"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SEC,
        help=f"upstream timeout in seconds (default {DEFAULT_TIMEOUT_SEC:g})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Child-process entrypoint: parse, validate, serve under uvicorn."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        target = resolve_target(args.target)
    except ValueError as exc:
        parser.error(str(exc))  # exits 2 — a misconfigured child must not boot

    import uvicorn  # the front's only runtime server dependency

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    front = McpFront(target=target, timeout=args.timeout)
    logger.info(
        "mcp_front: %s:%s -> %s (timeout %gs, body limit %d bytes, "
        "healthz %s; identity passthrough — no local auth, ADR-0050 C2)",
        args.host,
        args.port,
        front.target,
        args.timeout,
        MAX_BODY_BYTES,
        HEALTHZ_PATH,
    )
    uvicorn.run(front, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover - child-process entrypoint
    raise SystemExit(main())
