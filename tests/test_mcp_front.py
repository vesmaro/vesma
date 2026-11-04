"""ADR-0050 W2 (Ф2) — the MCP component front's proxy contract.

The front (``vesma.mcp_front``) is a stateless streamable-HTTP proxy and
an IDENTITY CARRIER, never an authorizer (ADR-0050 §3, C2): the core's
W1 gate stays the only authority. These tests pin that contract against
an in-process fake core (a Starlette app reached through
``httpx.ASGITransport`` — no sockets, no SDK):

* header passthrough — ``Authorization`` reaches the core BYTE-identical,
  the front reads nothing and strips nothing but hop-by-hop framing;
* body passthrough both directions, streamed (multi-chunk request in,
  multi-chunk response out), never buffered whole, never mutated;
* core unreachable → a FIXED 503 JSON (the cause goes to the log only);
* the SDK-inherited 4 MiB ceiling → 413 at the front, both for a declared
  ``Content-Length`` (refused BEFORE any hop) and for a lying/chunked
  body (counted while streaming);
* ``/healthz`` is local-only (the supervisor's probe never reaches the
  core);
* the C2 loopback-target guard (the target is where harness tokens GO —
  a non-loopback URL is refused at construction, at CLI parse time and
  at Settings construction via ``mcp.front.*``);
* the child entrypoint is pure argparse, imports no mcp SDK (ADR-0023).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.types import Message

from vesma.config import Settings
from vesma.mcp_front import (
    DEFAULT_TARGET,
    HEALTHZ_PATH,
    MAX_BODY_BYTES,
    TARGET_ENV_VAR,
    McpFront,
    build_arg_parser,
    resolve_target,
    validate_target,
)

SRC = Path(__file__).resolve().parent.parent / "src"

#: The harness credential used in the passthrough pins. A test fixture
#: value, not a real token (secrets-hygiene: examples carry no secrets).
_TEST_BEARER = "Bearer vesma-agent-test-token-42"


# ---------------------------------------------------------------------------
# Fake core + drivers (in-process, no sockets)
# ---------------------------------------------------------------------------


class _FakeCore:
    """A Starlette app that records what actually arrived from the front."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.app = Starlette(
            routes=[
                Route(
                    "/{path:path}",
                    self._handle,
                    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
                )
            ]
        )

    async def _handle(self, request: Request) -> Any:
        body = await request.body()
        self.seen.append(
            {
                "method": request.method,
                "headers": dict(request.headers),
                "body": body,
            }
        )
        if body == b"boom":
            # A non-200 upstream answer must reach the caller untouched.
            return PlainTextResponse("kaboom", status_code=500)
        return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": "ok"})


class _DeadCore(httpx.AsyncBaseTransport):
    """A transport that always fails like an unreachable socket."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)


class _ChunkedCore(httpx.AsyncBaseTransport):
    """A transport answering with a streamed multi-chunk body + hop-by-hop noise."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async def stream() -> AsyncIterator[bytes]:
            yield b'event: {"jsonrpc": "2.0"'
            yield b', "id": 1}\n\n'

        return httpx.Response(
            200,
            headers=[
                ("content-type", "text/event-stream"),
                ("connection", "keep-alive"),
                ("transfer-encoding", "chunked"),
            ],
            content=stream(),
        )


def _front(core_app: Any, **kwargs: Any) -> McpFront:
    """A front wired to an in-process core through ASGITransport."""
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=core_app))
    return McpFront(client=client, **kwargs)


async def _call(front: McpFront, **kwargs: Any) -> httpx.Response:
    """One harness-side request into the front (ASGI, no sockets)."""
    transport = httpx.ASGITransport(app=front)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as caller:
        return await caller.request("POST", "/mcp", **kwargs)


def _scope(method: str = "POST", path: str = "/mcp") -> dict[str, Any]:
    """A minimal http ASGI scope for the raw (manual) driver."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "query_string": b"",
        "headers": [
            (b"host", b"127.0.0.1:8790"),
            (b"content-type", b"application/json"),
            (b"authorization", _TEST_BEARER.encode()),
        ],
    }


def _receive(chunks: list[bytes]):  # type: ignore[no-untyped-def]
    async def receive() -> Message:
        if chunks:
            body = chunks.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


async def _drive(front: McpFront, scope: dict[str, Any], chunks: list[bytes]) -> list[Message]:
    """Drive the front's ASGI callable directly, capturing sent messages."""
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await front(scope, _receive(list(chunks)), send)
    return sent


# ---------------------------------------------------------------------------
# C2: identity passthrough
# ---------------------------------------------------------------------------


class TestIdentityPassthrough:
    async def test_authorization_reaches_core_byte_identical(self) -> None:
        core = _FakeCore()
        response = await _call(
            _front(core.app),
            headers={"Authorization": _TEST_BEARER, "Content-Type": "application/json"},
            content=b'{"jsonrpc": "2.0", "method": "tools/list"}',
        )
        assert response.status_code == 200
        assert len(core.seen) == 1
        seen = core.seen[0]
        assert seen["headers"]["authorization"] == _TEST_BEARER, (
            "the front is an identity CARRIER — the credential must arrive "
            "byte-identical, neither stripped nor rewritten (C2)"
        )
        assert seen["headers"]["content-type"] == "application/json"

    async def test_authorization_never_interpreted_no_auth_of_its_own(self) -> None:
        """Garbage credentials pass through untouched — auth is the core's job."""
        core = _FakeCore()
        response = await _call(
            _front(core.app),
            headers={"Authorization": "Basic not-even-a-token"},
            content=b"x",
        )
        assert response.status_code == 200
        assert core.seen[0]["headers"]["authorization"] == "Basic not-even-a-token"

    async def test_hop_by_hop_headers_not_forwarded(self) -> None:
        """The HARNESS's hop-by-hop values never reach the core; the front
        manages its own upstream connection (httpx re-adds Connection on
        its own — the front forces `close`, so the harness's `keep-alive`
        cannot ride through)."""
        core = _FakeCore()
        await _call(
            _front(core.app),
            headers={"Connection": "keep-alive", "Te": "trailers"},
            content=b"x",
        )
        seen_headers = core.seen[0]["headers"]
        assert seen_headers.get("connection") != "keep-alive"
        assert seen_headers.get("connection") == "close"
        assert "te" not in seen_headers

    async def test_upstream_status_and_body_pass_untouched(self) -> None:
        core = _FakeCore()
        response = await _call(_front(core.app), content=b"boom")
        assert response.status_code == 500
        assert response.text == "kaboom"


# ---------------------------------------------------------------------------
# Streaming (bodies cross in chunks, never buffered whole)
# ---------------------------------------------------------------------------


class TestStreaming:
    async def test_multi_chunk_request_body_reaches_core_intact(self) -> None:
        core = _FakeCore()
        front = _front(core.app)
        sent = await _drive(
            front,
            _scope(),
            [b'{"jsonrpc": "2.0",', b' "method": "tools/call"}'],
        )
        assert core.seen[0]["body"] == b'{"jsonrpc": "2.0", "method": "tools/call"}'
        start = next(m for m in sent if m["type"] == "http.response.start")
        assert start["status"] == 200
        terminator = sent[-1]
        assert terminator["type"] == "http.response.body"
        assert terminator.get("more_body") is False

    async def test_multi_chunk_response_forwards_in_chunks_without_hop_by_hop(
        self,
    ) -> None:
        client = httpx.AsyncClient(transport=_ChunkedCore())
        front = McpFront(client=client)
        sent = await _drive(front, _scope(), [b"x"])
        body_messages = [m for m in sent if m["type"] == "http.response.body"]
        # The two upstream chunks crossed to the harness separately —
        # the response leg streamed instead of buffering whole.
        assert len(body_messages) >= 2
        assembled = b"".join(m.get("body", b"") for m in body_messages)
        assert assembled == b'event: {"jsonrpc": "2.0", "id": 1}\n\n'
        start = next(m for m in sent if m["type"] == "http.response.start")
        names = {name.decode("latin-1").lower() for name, _ in start["headers"]}
        assert "content-type" in names
        assert "connection" not in names, "hop-by-hop framing must not cross the front"
        assert "transfer-encoding" not in names
        assert start["status"] == 200

    async def test_client_disconnect_closes_quietly(self) -> None:
        core = _FakeCore()
        front = _front(core.app)
        scope = _scope()

        async def receive() -> Message:
            return {"type": "http.disconnect"}

        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        await front(scope, receive, send)
        assert sent == [], "a gone caller gets no fabricated answer"
        assert core.seen == [], "a dead request never reaches the core"


# ---------------------------------------------------------------------------
# Failure modes: 503 fixed detail, 413 size guard
# ---------------------------------------------------------------------------


class TestCoreUnreachable:
    async def test_fixed_503_when_core_unreachable(self) -> None:
        front = McpFront(client=httpx.AsyncClient(transport=_DeadCore()))
        response = await _call(front, content=b"x")
        assert response.status_code == 503
        payload = response.json()
        assert payload == {"detail": "vesma core MCP plane is unreachable (mcp.front.target)"}, (
            "the wire answer is a FIXED string — no exception text on the wire"
        )


class TestBodySizeGuard:
    async def test_declared_oversize_refused_before_any_hop(self) -> None:
        core = _FakeCore()
        response = await _call(
            _front(core.app),
            headers={"Content-Length": str(MAX_BODY_BYTES + 1)},
            content=b"tiny",
        )
        assert response.status_code == 413
        assert response.json() == {"detail": "request body exceeds the maximum MCP message size"}
        assert core.seen == [], "an oversized request must not reach the core"

    async def test_chunked_oversize_refused_midstream(self) -> None:
        core = _FakeCore()
        front = _front(core.app, max_body_bytes=16)
        sent = await _drive(front, _scope(), [b"y" * 10, b"y" * 10])  # 20 > 16
        start = next(m for m in sent if m["type"] == "http.response.start")
        assert start["status"] == 413
        assert core.seen == [], "the crossing chunk never leaves the front"

    async def test_chunked_body_at_limit_passes(self) -> None:
        core = _FakeCore()
        front = _front(core.app, max_body_bytes=16)
        await _drive(front, _scope(), [b"y" * 8, b"y" * 8])  # exactly 16
        assert core.seen[0]["body"] == b"y" * 16


# ---------------------------------------------------------------------------
# /healthz — local only
# ---------------------------------------------------------------------------


class TestHealthz:
    async def test_healthz_is_local_and_never_forwarded(self) -> None:
        core = _FakeCore()
        await _call(_front(core.app), content=b"")
        # sanity: real requests DO forward
        assert len(core.seen) == 1

        core2 = _FakeCore()
        transport = httpx.ASGITransport(app=_front(core2.app))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as caller:
            health = await caller.get(HEALTHZ_PATH)
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "service": "vesma-mcp-front"}
        assert core2.seen == [], "the supervisor's probe must never reach the core"

    async def test_lifespan_builds_and_closes_the_owned_client(self) -> None:
        front = McpFront()  # no injected client — the app owns one
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        # FINITE receive sequence: a fake receive that always answers
        # "lifespan.startup" turns the ASGI contract loop into an
        # unbounded startup-complete append loop (the 66G-swap OOM
        # detonator of the first gate run). uvicorn sends startup once,
        # then shutdown — mirror exactly that.
        messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

        async def receive() -> Message:
            return next(messages)

        await front({"type": "lifespan"}, receive, send)
        kinds = [m["type"] for m in sent]
        assert "lifespan.startup.complete" in kinds
        assert "lifespan.shutdown.complete" in kinds
        await front.aclose()


# ---------------------------------------------------------------------------
# C2 guard: loopback-only target
# ---------------------------------------------------------------------------


class TestLoopbackTargetGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8787/mcp/",
            "http://127.5.5.5:8787/mcp/",
            "http://localhost:8787/mcp/",
            "http://[::1]:8787/mcp/",
            "https://localhost/mcp/",
        ],
    )
    def test_loopback_forms_accepted(self, url: str) -> None:
        assert validate_target(url) == url
        McpFront(target=url)  # construction passes

    @pytest.mark.parametrize(
        "url",
        [
            "http://10.0.0.1:8787/mcp/",
            "https://example.com/mcp/",
            "http://192.168.1.10/mcp/",
            "ftp://127.0.0.1/mcp/",
            "not-a-url",
            "http:///mcp/",
        ],
    )
    def test_non_loopback_refused(self, url: str) -> None:
        with pytest.raises(ValueError):
            validate_target(url)
        with pytest.raises(ValueError):
            McpFront(target=url)

    def test_default_target_is_loopback(self) -> None:
        assert validate_target(DEFAULT_TARGET) == DEFAULT_TARGET


# ---------------------------------------------------------------------------
# Config keys: mcp.front.* (McpFrontConfig)
# ---------------------------------------------------------------------------


def _settings_kwargs(**mcp: Any) -> dict[str, Any]:
    return {
        "vesma": {"vault_path": "/tmp/fp-v", "data_dir": "/tmp/fp-d"},
        "embedding": {"provider": "onnx"},
        "scanner": {"enabled": False},
        "mcp": mcp,
    }


class TestMcpFrontConfig:
    def test_defaults_parse(self) -> None:
        settings = Settings(**_settings_kwargs())
        front = settings.mcp.front
        assert front.port == 8790
        assert front.target == "http://127.0.0.1:8787/mcp/"
        assert front.timeout == 60.0

    def test_keys_parse_through_settings(self) -> None:
        settings = Settings(
            **_settings_kwargs(
                front={"port": 9000, "target": "http://localhost:8787/mcp/", "timeout": 30}
            )
        )
        front = settings.mcp.front
        assert front.port == 9000
        assert front.target == "http://localhost:8787/mcp/"
        assert front.timeout == 30

    def test_extra_forbid_refuses_unknown_key(self) -> None:
        with pytest.raises(ValidationError):
            Settings(**_settings_kwargs(front={"porrt": 9000}))

    def test_non_loopback_target_refuses_the_boot(self) -> None:
        with pytest.raises(ValidationError, match=r"mcp.front.target"):
            Settings(**_settings_kwargs(front={"target": "http://10.0.0.1:8787/mcp/"}))


# ---------------------------------------------------------------------------
# Child-process entrypoint (argparse, no click, no SDK)
# ---------------------------------------------------------------------------


class TestEntrypoint:
    def test_parser_defaults(self) -> None:
        args = build_arg_parser().parse_args([])
        assert args.host == "127.0.0.1"
        assert args.port == 8790
        assert args.target is None
        assert args.timeout == 60.0

    def test_parser_accepts_the_documented_flags(self) -> None:
        args = build_arg_parser().parse_args(
            ["--port", "9001", "--target", "http://127.0.0.1:8787/mcp/", "--timeout", "120"]
        )
        assert args.port == 9001
        assert args.timeout == 120.0

    def test_resolve_target_precedence_flag_over_env_over_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TARGET_ENV_VAR, "http://127.0.0.1:9999/mcp/")
        assert resolve_target() == "http://127.0.0.1:9999/mcp/"
        assert resolve_target("http://localhost:8787/mcp/") == "http://localhost:8787/mcp/"
        monkeypatch.delenv(TARGET_ENV_VAR)
        assert resolve_target() == DEFAULT_TARGET

    def test_main_refuses_non_loopback_target_with_exit_2(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            from vesma.mcp_front import main

            main(["--target", "http://10.0.0.1:8787/mcp/"])
        assert excinfo.value.code == 2

    def test_front_imports_without_the_mcp_sdk(self) -> None:
        """ADR-0023: the front never pulls the SDK (source stays lazy in core)."""
        code = (
            "import sys, vesma.mcp_front\n"
            "loaded = [m for m in sys.modules if m == 'mcp' or m.startswith('mcp.')]\n"
            "assert not loaded, f'mcp SDK loaded by the front: {loaded}'\n"
            "print(':ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(SRC.parent.parent),
        )
        assert result.returncode == 0, f"front import broken:\n{result.stderr}"
        assert ":ok" in result.stdout

    def test_front_never_imports_click(self) -> None:
        """The child-process entrypoint is argparse-only by contract.

        Precise form: OUR module source references no CLI framework and
        the import pulls NONE of vesma's own CLI stack (typer). Bare
        `click` in sys.modules is httpx's OWN transitive CLI module
        (`httpx._main` imports it unconditionally by upstream design) —
        asserting its absence would make the contract unachievable with
        httpx as the proxy client, which is not the contract's intent.
        """
        source = (SRC / "vesma" / "mcp_front.py").read_text()
        for forbidden in ("import click", "from click", "import typer", "from typer"):
            assert forbidden not in source, f"front source references {forbidden!r}"
        code = (
            "import sys, vesma.mcp_front\n"
            "assert 'typer' not in sys.modules, 'vesma CLI stack leaked into the child path'\n"
            "print(':ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(SRC.parent.parent),
        )
        assert result.returncode == 0, f"front import broken:\n{result.stderr}"
        assert ":ok" in result.stdout
