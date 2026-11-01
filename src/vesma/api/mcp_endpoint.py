"""MCP streamable-HTTP endpoint for the core ``vesma serve`` app (ADR-0050 W1).

Mounts the MCP tool plane (the same ``vesma_*`` tools the stdio
``vesma mcp-server`` serves) at ``/mcp`` on the FastAPI app, behind a
thin ASGI auth gate:

* **Transport** — the SDK 2.x ``StreamableHTTPSessionManager`` (imported
  ONLY through :func:`vesma.mcp_server.streamable_http_session_manager`
  per the ADR-0023 isolation guard — this module never imports the SDK
  itself), JSON responses, stateless mode: a fresh transport per request,
  no server-side session state, so every request re-carries its Bearer
  token and the future Ф2 component front (stateless proxy) inherits the
  exact shape.
* **Authentication (ADR-0050 §3, hard requirement)** — EVERY request to
  ``/mcp`` must carry ``Authorization: Bearer <agent token>``, validated
  fail-closed via :func:`vesma.agent_tokens.validate_agent_token` with
  the serving instance id (``X-Vesma-Instance-Id``) as the node binding
  (``aud`` — issue harness tokens with ``vesma agent-token issue --node
  <instance-id>``, ``default`` for the default instance). Missing,
  malformed or invalid tokens answer 401 JSON; a disabled endpoint
  answers 404; loopback trust of the REST API (``api.auth_enabled=false``
  + loopback bind) does NOT extend here — the AuthMiddleware bypasses
  the path and THIS gate is the only auth, so loopback-without-token is
  a 401 by construction.
* **Token hygiene** — token values are never logged: rejections log the
  verdict's coarse reason / agent_id / jti only (the mesh data-gate
  convention).
* **Per-token quota (ADR-0050 §4, wave W3)** — after a valid verdict the
  gate consults the sliding-window :class:`vesma.mcp_namespace.TokenQuota`
  published on ``app.state.mcp_token_quota`` (key = the token ``jti``);
  an over-limit request answers 429 JSON with ``Retry-After`` BEFORE the
  session manager runs (denied calls never reach a tool).
* **Identity threading (ADR-0050 §4, wave W3)** — the verdict's
  ``(agent_id, jti)`` is published as an :class:`McpIdentity` into the
  module-level request-identity contextvar for the duration of
  ``manager.handle_request`` (set before the await, reset in a
  ``finally``); the MCP tool dispatch reads it for the enforced
  ``agent:<slug>`` namespace and the ``cross_read`` aperture
  (:mod:`vesma.mcp_namespace`). Off-plane callers never see a value —
  the local CLI / stdio / embedded-store behavior is unchanged.

The gate is a raw ASGI app (not BaseHTTPMiddleware) so it adds no
buffering layer between the SDK transport and the server; per-request
state comes from ``scope["app"].state`` which the API lifespan populates
(``mcp_endpoint_enabled`` / ``mcp_session_manager`` / ``mcp_token_deps``
/ ``mcp_token_quota`` / ``instance_id``). Anything missing = fail-closed
(404/503), never an open endpoint.
"""

from __future__ import annotations

import json
import logging
import threading
from types import TracebackType
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.types import Message, Receive, Scope, Send

from vesma.agent_tokens import (
    AgentTokenStore,
    load_or_create_signing_key,
    signing_key_path,
    validate_agent_token,
)
from vesma.config import Settings
from vesma.mcp_namespace import (
    McpIdentity,
    TokenQuota,
    reset_request_identity,
    set_request_identity,
)

logger = logging.getLogger(__name__)

#: The single mount path of the MCP streamable-HTTP plane.
MCP_HTTP_MOUNT_PATH = "/mcp"


def build_mcp_session_manager() -> Any:
    """Build a fresh streamable-HTTP session manager over the shared MCP server.

    Deferred on purpose, twice over: (1) the mcp SDK is imported inside
    :func:`vesma.mcp_server.streamable_http_session_manager` — this module
    stays SDK-free at import time so core paths (CLI import, API import)
    never load it (ADR-0023 guard); (2) a FRESH manager per call is
    required because ``StreamableHTTPSessionManager.run()`` executes at
    most once per instance — each lifespan cycle (every ``serve`` boot,
    every test client) builds its own.
    """
    from vesma import mcp_server

    return mcp_server.streamable_http_session_manager()


class McpTokenDeps:
    """Lazily built ``(AgentTokenStore, Ed25519PrivateKey)`` pair for the /mcp gate.

    Mirrors the mesh server's ``_token_validation_deps`` lazy pattern: a
    Vesma host that never receives a ``/mcp`` request never opens the
    token registry and never mints the signing key ("first use"). The
    pair is cached for the lifetime of the app (one SQLite connection,
    one key) and closed by the API lifespan's finally block. Double-checked
    under a lock so concurrent first requests build the pair exactly once;
    failures propagate to the caller (mapped to a fixed 503), no
    half-initialised state is retained.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._store: AgentTokenStore | None = None
        self._key: Ed25519PrivateKey | None = None

    def resolve(self) -> tuple[AgentTokenStore, Ed25519PrivateKey]:
        """Return the cached (store, key) pair, building both on first use."""
        if self._store is None or self._key is None:
            with self._lock:
                if self._store is None:
                    self._store = AgentTokenStore(self._settings.db_path)
                if self._key is None:
                    self._key = load_or_create_signing_key(
                        signing_key_path(
                            self._settings.vesma.data_dir,
                            override=self._settings.federation.agent_token_key_path,
                        )
                    )
        return self._store, self._key

    def close(self) -> None:
        """Close the cached store (idempotent; the key object needs no close)."""
        with self._lock:
            if self._store is not None:
                self._store.close()
                self._store = None
            self._key = None

    def __enter__(self) -> McpTokenDeps:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _bearer_token(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Extract the Bearer credential from raw ASGI headers (None when absent).

    Only a well-formed ``Authorization: Bearer <value>`` (first header
    wins, per ASGI multi-header convention) yields the value; any other
    scheme — and the loopback caller in particular — yields None, which
    the gate turns into 401.
    """
    for name, value in headers:
        if name == b"authorization":
            text = value.decode("latin-1")
            if text.startswith("Bearer "):
                token = text[len("Bearer ") :].strip()
                return token or None
            return None
    return None


async def _send_json(
    send: Send,
    *,
    status: int,
    body: dict[str, str],
    retry_after: int | None = None,
) -> None:
    """Send a bare JSON ASGI response (401s carry ``WWW-Authenticate: Bearer``,
    a quota 429 carries ``Retry-After`` seconds)."""
    payload = json.dumps(body).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode("ascii")),
    ]
    if status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    if retry_after is not None:
        headers.append((b"retry-after", str(max(1, int(retry_after))).encode("ascii")))
    message: Message = {"type": "http.response.start", "status": status, "headers": headers}
    await send(message)
    await send({"type": "http.response.body", "body": payload})


class McpHttpEndpoint:
    """Auth gate in front of the SDK streamable-HTTP session manager.

    Resolution order (fail-closed at every step, cheapest check first):

    1. endpoint disabled (``mcp.endpoint_enabled=false``, or the flag was
       never set by a lifespan) → 404 — the surface does not exist and
       its auth state is not revealed;
    2. no ``Authorization: Bearer`` header → 401 — BEFORE any lifecycle
       state is revealed (an unauthenticated caller cannot probe whether
       the endpoint is initialized);
    3. no session manager on the app (enabled but the lifespan never
       built one — only possible on a bare/misconfigured app) → 503;
    4. token store/key unavailable → 503 with a FIXED detail (the cause
       goes to the log only — no store paths on the wire, the mesh
       data-gate discipline);
    5. :func:`validate_agent_token` against the serving instance id →
       any rejection is a 401 with the coarse reason category;
    6. per-token quota (``app.state.mcp_token_quota``, keyed by the
       verdict's ``jti``) → over-limit is a 429 JSON with ``Retry-After``
       (the resource axis of ADR-0050 §4; W3);
    7. only then the request enters the SDK session manager — with the
       verdict identity published into the request-identity contextvar
       for the dispatch (W3), reset in a ``finally``.

    Non-http scopes (ASGI lifespan frames never reach a Mount, but be
    explicit) are dropped silently.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - defensive
            return
        state: Any = scope["app"].state

        # 1. Disabled (or never configured) → the plane does not exist.
        if not getattr(state, "mcp_endpoint_enabled", False):
            await _send_json(
                send,
                status=404,
                body={"detail": "MCP HTTP endpoint is disabled (mcp.endpoint_enabled)"},
            )
            return

        # 2. Bearer presence — BEFORE any lifecycle state is revealed
        #    (cascade hardening, LOW #2: an unauthenticated caller must not
        #    be able to probe whether the endpoint is initialized).
        token = _bearer_token(scope.get("headers", []))
        if token is None:
            logger.info("mcp_http: rejected request reason=no_bearer")
            await _send_json(
                send,
                status=401,
                body={
                    "detail": "Authentication required: Bearer agent token "
                    "(vesma agent-token issue)"
                },
            )
            return

        # 3. Enabled but no manager → lifecycle misconfiguration, fail closed.
        manager = getattr(state, "mcp_session_manager", None)
        if manager is None:  # pragma: no cover - defensive
            await _send_json(
                send,
                status=503,
                body={"detail": "MCP HTTP endpoint is not initialized"},
            )
            return

        # 4. Validation deps (lazy, cached on the app state).
        deps: McpTokenDeps | None = getattr(state, "mcp_token_deps", None)
        instance_id = getattr(state, "instance_id", None)
        if deps is None or not instance_id:  # pragma: no cover - defensive
            await _send_json(
                send,
                status=503,
                body={"detail": "MCP HTTP endpoint is not initialized"},
            )
            return
        try:
            store, key = deps.resolve()
        except Exception as exc:  # surfaced as 503, logged with cause
            logger.error("mcp_http: token validation deps unavailable (%s)", exc)
            await _send_json(
                send,
                status=503,
                body={"detail": "agent token validation backend unavailable"},
            )
            return

        # 5. Fail-closed token validation; aud = the serving instance id.
        try:
            verdict = validate_agent_token(token, str(instance_id), store=store, key=key)
        except Exception as exc:  # mid-validation crash → fixed detail
            logger.error("mcp_http: agent token validation failed (%s)", exc)
            await _send_json(
                send,
                status=503,
                body={"detail": "agent token validation failed"},
            )
            return
        if not verdict.valid:
            # T7 hygiene: coarse reason / agent_id / jti only — never the token.
            logger.info(
                "mcp_http: rejected request agent_id=%s jti=%s reason=%s",
                verdict.agent_id,
                verdict.jti,
                verdict.reason,
            )
            await _send_json(
                send,
                status=401,
                body={"detail": f"agent token rejected: {verdict.reason}"},
            )
            return

        # 6. Per-token quota (ADR-0050 §4, W3) — BEFORE the session manager
        #    runs: denied calls never reach a tool. Key = the verdict's jti
        #    (a rotated token starts a fresh window). The verdict is valid
        #    here, so agent_id/jti are both minted by the validator; the
        #    identity-shaped defensive fallback keeps the gate typed even
        #    if a future validator change ever loosened that invariant
        #    (fail-closed to "" rather than crashing the plane).
        quota: TokenQuota | None = getattr(state, "mcp_token_quota", None)
        if quota is None:  # pragma: no cover - defensive (lifespan always publishes)
            await _send_json(
                send,
                status=503,
                body={"detail": "MCP HTTP endpoint is not initialized"},
            )
            return
        identity = McpIdentity(
            agent_id=verdict.agent_id or "",
            jti=verdict.jti or "",
        )
        decision = quota.check(identity.jti)
        if not decision.allowed:
            logger.info(
                "mcp_http: quota exceeded agent_id=%s jti=%s retry_after=%ss",
                identity.agent_id,
                identity.jti,
                decision.retry_after,
            )
            await _send_json(
                send,
                status=429,
                body={
                    "detail": (
                        "MCP per-token quota exceeded (mcp.quota.calls_per_minute) — retry later"
                    )
                },
                retry_after=decision.retry_after,
            )
            return

        # 7. Authenticated and inside the quota — publish the request
        #    identity for the dispatch (enforced agent:<slug> + cross_read,
        #    vesma.mcp_namespace) strictly for the duration of the SDK
        #    session manager call, reset in a finally: the var is
        #    per-request state, never a process default.
        ctx_token = set_request_identity(identity)
        try:
            await manager.handle_request(scope, receive, send)
        finally:
            reset_request_identity(ctx_token)


#: The module-level ASGI app mounted on the API app (see ``api.main``).
#: Stateless per-request resolution from ``scope["app"].state`` keeps the
#: instance shareable across app copies (test apps included).
mcp_http_endpoint = McpHttpEndpoint()
