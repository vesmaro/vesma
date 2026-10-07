"""ASGI auth middleware for Vesma API (T-AUTH, ADR-0014).

Sits after CORS, before routes.  Logic:

- Bypass list: ``/health``, ``/auth/login``, ``/auth/verify`` are always
  bypassed. ``/docs``, ``/redoc``, ``/openapi.json`` and the metrics
  endpoints (``/metrics``, ``/api/v1/metrics``) are bypassed ONLY on
  loopback binds — on non-loopback binds they require a valid session
  (issue #249: the metrics exposition exports
  ``mnemos_memories_by_project{project=...}`` and
  ``mnemos_memories_by_agent{agent=...}`` gauges, so unauthenticated
  exposure is a reconnaissance-grade surface, CWE-200). All other paths
  require a valid session.
- Trust-zone resolution: if ``api.auth_enabled`` is ``False`` AND
  ``api.host`` is a loopback address (``127.0.0.1`` / ``::1`` /
  ``localhost``), the middleware is a no-op (local-desktop scenario, ADR §
  Trust zones).  If ``auth_enabled`` is ``False`` but the configured bind is
  non-loopback, requests are rejected with 401 as defence-in-depth (the
  startup guard in ``lifespan`` normally prevents reaching this branch).
- Session extraction: ``Authorization: Bearer <session>`` takes precedence;
  ``mnemos_session`` cookie is the fallback.
- CSRF: state-changing methods (POST / PUT / DELETE / PATCH) additionally
  require the ``Authorization: Bearer`` header — a cross-origin attacker
  cannot read or set that header on a cookie-only request (ADR §CSRF T3).
- On every authenticated request the session TTL is slid forward and the
  validated session dict is stored on ``request.state.auth_session`` for
  downstream use.

Deviation from ADR letter: the loopback check uses ``api.host`` (the
configured bind address) rather than ``request.client.host``.  Rationale:
Starlette's ``TestClient`` sets ``client.host = "testclient"`` which is not
in ``{"127.0.0.1", "::1"}``; checking the bind address gives the correct
semantics (if the server is configured for loopback, it *is* loopback) and
preserves backward-compatibility for the existing test suite.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import threading
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from vesma.api.auth_store import AuthStore
from vesma.api.client_ip import resolve_client_ip

logger = logging.getLogger(__name__)

# Paths that always bypass auth. Health/login/verify are public by design;
# federation pull is public to the *middleware* because it authenticates
# itself per-peer (see below).
#
# Docs endpoints (/docs, /redoc, /openapi.json) AND metrics endpoints
# (/metrics, /api/v1/metrics) are only bypassed on loopback binds — see
# ``_get_bypass_paths``. On non-loopback binds they require auth:
# docs leak the API schema, and metrics (issue #249) leak operational
# intelligence — the exposition exports per-project and per-agent gauges
# (``mnemos_memories_by_project``, ``mnemos_memories_by_agent``), which is
# reconnaissance-grade information for an unauthenticated remote caller
# (CWE-200). Loopback binds keep both open: local scrapers (e.g. a
# Prometheus agent scraping localhost) and local dev tooling rely on
# unauthenticated access there.
#
# ``/api/v1/federation/pull`` is bypassed unconditionally because federation
# peers authenticate with a per-peer bearer token (``mnk_fed_<peer_id>_*``,
# ADR-0016) that is verified inside ``handle_pull`` — the federation server
# is its own auth layer for server-to-server pull. The global operator
# session (TOTP/api-key) is for the operator API surface, not for peer
# federation. Without this bypass the middleware returns 401 before
# ``handle_pull`` ever runs, so federation pull is broken whenever
# ``api.auth_enabled=true``. The bypass is fail-closed: ``handle_pull``
# still rejects requests with no/incorrect per-peer bearer (403 REFUSED).
_ALWAYS_BYPASS = frozenset(
    {
        "/health",
        "/auth/login",
        "/auth/verify",
        "/api/v1/federation/pull",
    }
)
_DOCS_BYPASS = frozenset({"/docs", "/redoc", "/openapi.json"})
_METRICS_BYPASS = frozenset({"/metrics", "/api/v1/metrics"})
_MUTATION_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


def _get_bypass_paths(host: str) -> frozenset[str]:
    """Return the set of paths that bypass auth for the given bind host.

    On loopback binds, docs and metrics endpoints are exposed without auth
    for dev convenience and for local scrapers (a Prometheus agent scraping
    localhost must not need operator credentials). On non-loopback binds
    both require auth: docs leak the API schema, and metrics (issue #249)
    export ``mnemos_memories_by_project`` / ``mnemos_memories_by_agent``
    gauges — operational intelligence an unauthenticated remote caller
    must not see (CWE-200).
    """
    paths: set[str] = set(_ALWAYS_BYPASS)
    if _is_loopback_host(host):
        paths |= _METRICS_BYPASS
        paths |= _DOCS_BYPASS
    return frozenset(paths)


def _is_loopback_host(host: str) -> bool:
    if host in {"localhost", "ip6-localhost"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _json_response(body: str, status: int) -> Response:
    return Response(body, status_code=status, media_type="application/json")


class AuthMiddleware(BaseHTTPMiddleware):
    """HTTP middleware enforcing bearer-token / session authentication."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: object) -> Response:

        _call_next: Callable[[Request], Awaitable[Response]] = call_next  # type: ignore[assignment]

        path = request.url.path

        # 1. Load auth config from app state (set in lifespan) — needed for
        #    both the bypass-path computation (host-dependent) and the
        #    trust-zone resolution below.
        api_config = getattr(request.app.state, "api_config", None)
        if api_config is None:
            # Fail closed - finding auth-2. Tests that build a bare app
            # MUST attach an api_config to app.state (loopback ApiConfig is
            # fine) so the middleware can make a trust-zone decision.
            logger.error("auth: request rejected - app.state.api_config not initialised")
            return _json_response('{"detail":"Auth not initialised"}', 503)

        # 2. Bypass list — docs endpoints are only bypassed on loopback binds.
        bypass_paths = _get_bypass_paths(api_config.host)
        if path in bypass_paths:
            return await _call_next(request)

        # 3. Trust-zone resolution
        if not api_config.auth_enabled:
            if _is_loopback_host(api_config.host):
                # Loopback bind + auth off → local-desktop mode, allow all.
                return await _call_next(request)
            # Non-loopback + auth off → defense-in-depth rejection.
            # The startup guard in lifespan normally prevents this state.
            logger.warning(
                "auth: non-loopback bind (%r) with auth_enabled=False — rejecting request",
                api_config.host,
            )
            return _json_response('{"detail":"Unauthorized"}', 401)

        # 5. Extract session token (Bearer > cookie)
        auth_header = request.headers.get("Authorization", "")
        bearer: str | None = None
        if auth_header.startswith("Bearer "):
            bearer = auth_header[7:]

        session_token = bearer or request.cookies.get("mnemos_session")

        if not session_token:
            return _json_response('{"detail":"Authentication required"}', 401)

        # 6. Validate session
        session_hash = hashlib.sha256(session_token.encode()).hexdigest()
        auth_store: AuthStore | None = getattr(request.app.state, "auth_store", None)
        if auth_store is None:
            return _json_response('{"detail":"Auth service unavailable"}', 503)

        # 6b. Direct-bearer fast path: if the bearer has the API-token prefix
        # (``mnk_``), look up the token row and, when it is active and
        # ``totp_required`` is false, admit the request directly without a
        # session. This allows tokens created with ``--no-totp`` to be used
        # as plain bearer credentials (no login/verify/session flow).
        direct_bearer_admitted = False
        if bearer is not None and bearer.startswith("mnk_"):
            from vesma.api.auth_store import hash_token as _hash_token

            token_row = auth_store.get_token_by_hash(_hash_token(bearer))
            if (
                token_row is not None
                and auth_store.is_token_active(token_row)
                and not int(str(token_row.get("totp_required", 1) or 0))
            ):
                # TOTP not required — admit directly.
                request.state.auth_token = token_row
                request.state.auth_bearer = True
                direct_bearer_admitted = True
                logger.debug("auth: direct-bearer admit for token_id=[REDACTED] (totp_required=0)")

        if direct_bearer_admitted:
            return await _call_next(request)

        if api_config.behind_tls_proxy:
            client_ip: str | None = resolve_client_ip(request, api_config)
        else:
            client = request.client
            client_ip = client.host if client else None

        session = auth_store.get_session_by_hash(session_hash)
        # Finding auth-6: when behind a TLS proxy AND the peer is a trusted
        # proxy, derive client IP from the validated X-Forwarded-For header
        # rather than the proxy IP itself. Otherwise the pinned IP would be
        # the proxy IP and pinning would be a no-op against an attacker
        # behind the same proxy.

        if session is None or not auth_store.is_session_valid(
            session,
            client_ip=client_ip,
            session_pin_ip=api_config.session_pin_ip,
        ):
            return _json_response('{"detail":"Invalid or expired session"}', 401)

        # 7. CSRF: mutations require the Authorization: Bearer header
        if request.method in _MUTATION_METHODS and not bearer:
            return _json_response(
                '{"detail":"CSRF protection: Authorization header required for mutations"}',
                403,
            )

        # 8. Slide session TTL and expose session to downstream handlers
        auth_store.touch_session(session_hash, api_config.session_ttl_sec)
        request.state.auth_session = session
        request.state.auth_session_hash = session_hash

        return await _call_next(request)


class VitalsVerbMiddleware(BaseHTTPMiddleware):
    """Phase A2 boundary #2 — one verb row per REST request.

    Verb is the ROUTE TEMPLATE (``rest:POST:/search``), never the raw
    path — concrete paths are banned from the plane (RL-S2). Service
    endpoints (health/metrics/stats/docs) are excluded. Unmatched 404s
    are counted under the fixed verb ``unmatched`` — a counter only, no
    caller-controlled material enters the ledger.
    """

    _EXCLUDED = frozenset(
        {"/health", "/metrics", "/api/v1/metrics", "/docs", "/redoc", "/openapi.json"}
    )

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        import time as _time

        path = request.scope.get("path", "")
        if path in self._EXCLUDED or path.startswith("/api/v1/stats"):
            return await call_next(request)
        t0 = _time.monotonic()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            verb = f"rest:{request.method}:{template}"
            try:
                from vesma.api.main import get_manager  # deferred: no import cycle

                get_manager().record_verb_vitals(
                    surface="rest",
                    verb=verb,
                    status="ok" if status_code < 400 else "error",
                    latency_ms=(_time.monotonic() - t0) * 1000,
                    status_code=status_code,
                )
            except Exception:
                pass


# ── REST deprecation aliases (card vesma-rest-deprecation-middleware) ────────
#
# Canon is ``/api/v1``; the root-level routes below are legacy aliases. The
# frozenset holds route TEMPLATES (Starlette ``route.path``), not concrete
# paths — parameterised routes (``/memories/{memory_id}``) must match too.
# The physical route move happens in 6.0; until then the aliases keep
# serving and are only decorated with deprecation headers.
#
# Deliberately NOT in this set (and orthogonal to AuthMiddleware's
# ``_ALWAYS_BYPASS``/``_METRICS_BYPASS`` and VitalsVerbMiddleware's
# ``_EXCLUDED`` — those lists gate auth/vitals semantics and are untouched
# here; deprecation headers neither widen nor narrow them):
#   ``/health``                 — liveness convention, never deprecated;
#   ``/v1/...``                 — A2A Sessions API (M16): a separate, already
#                                 versioned surface mounted at ``/v1`` by
#                                 design, not an alias of ``/api/v1``;
#   ``/auth/...``               — T-AUTH surface (ADR-0014): no ``/api/v1``
#                                 counterpart exists, so it is not a legacy
#                                 alias;
#   ``/docs``, ``/redoc``, ``/openapi.json`` — schema UI, not API endpoints.
#
# ``/metrics`` IS an alias: the canonical Prometheus exposition is
# ``/api/v1/metrics``; the root path serves a legacy JSON body.
_LEGACY_ROOT_PATHS = frozenset(
    {
        "/metrics",
        "/memories",
        "/memories/{memory_id}",
        "/memories/{memory_id}/workflow",
        "/memories/{memory_id}/quarantine/release",
        "/search",
        "/recall/agent/{name}",
        "/process",
        "/reindex",
        "/synthesize",
        "/publish/{memory_id}",
        "/dlq",
        "/dlq/{dlq_id}/retry",
        "/dlq/{dlq_id}",
        "/filter/{memory_id}",
        "/tags",
        "/tags/rename",
        "/traces",
        "/rules/ingest",
        "/context/save",
        "/context/recall",
        "/context/assemble",
        "/context/rewrite",
        "/hooks/{action}",
        "/compress",
        "/retrieve",
        "/auto-collect",
        "/ingest-url",
        "/ingest-document",
        "/watch/start",
        "/watch/stop",
        "/watch/status",
        "/graph/index",
        "/graph/status/{project_id}",
        "/graph/search",
        "/graph/trace",
        "/graph/outline",
        "/graph/snippet",
        "/graph/coverage",
        "/graph/schema",
        "/graph/projects",
        "/graph/projects/{project_id}",
    }
)

# Per-template hit counter — light, in-process (single event loop per
# worker), no new telemetry backend. Surfaced via logs (INFO on the first
# hit of a template, DEBUG afterwards) and :func:`get_deprecation_hits`.
# Wired into the vitals exposition only if the migration dashboard needs
# it (6.0 follow-up decision).
_DEPRECATION_HITS_LOCK = threading.Lock()
_DEPRECATION_HITS: dict[str, int] = {}


def get_deprecation_hits() -> dict[str, int]:
    """Return a copy of per-legacy-template deprecation hit counts."""
    with _DEPRECATION_HITS_LOCK:
        return dict(_DEPRECATION_HITS)


def reset_deprecation_hits() -> None:
    """Clear the hit counter (test hook)."""
    with _DEPRECATION_HITS_LOCK:
        _DEPRECATION_HITS.clear()


class DeprecationMiddleware(BaseHTTPMiddleware):
    """Decorate legacy root API paths with deprecation headers.

    Why: the API versioning canon is ``/api/v1``; the ~40 root-level routes
    (``/memories``, ``/search``, ``/context/*``, ``/graph/*``, ...) remain
    as backward-compatibility aliases until Vesma 6.0 physically moves them
    (card ``vesma-rest-deprecation-middleware``; alias removal lands in the
    6.0 pocket together with the route move). This middleware marks every
    response served via a legacy alias so clients can migrate early.

    Headers on a legacy-alias hit:

    - ``Deprecation: true`` — the location is deprecated.
    - ``Link: </api/v1<template>>; rel="suggested-version"`` — the canonical
      location to migrate to (matched by route TEMPLATE, so parameterised
      aliases resolve to their ``/api/v1`` counterpart).

    Sunset (RFC 8594) is deliberately NOT sent: it requires an absolute
    HTTP-date and the 6.0 release date is not fixed. Fabricating one would
    violate the RFC and mislead clients. ``Sunset`` is added in the 6.0
    line as soon as the date exists.

    Path resolution follows the ``VitalsVerbMiddleware`` pattern: the route
    template is read from ``request.scope["route"]`` after ``call_next``,
    so only responses produced by an actual route match are decorated
    (auth-rejected 401s happen before routing and are not decorated).
    Canonical ``/api/v1/*``, ``/health`` and the ``/v1`` sessions surface
    never match the template set and stay clean.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        # Route template only — never fall back to the raw concrete path:
        # an unmatched 404 or an auth-rejected 401 (rejected before routing)
        # has no ``scope["route"]`` and must stay clean. A 405 (path matched,
        # method not allowed) still carries the matched template and IS
        # decorated — the response was served at the legacy location.
        route = request.scope.get("route")
        template = str(getattr(route, "path", "")) if route is not None else ""
        if template not in _LEGACY_ROOT_PATHS:
            return response
        response.headers["Deprecation"] = "true"
        response.headers["Link"] = f'</api/v1{template}>; rel="suggested-version"'
        with _DEPRECATION_HITS_LOCK:
            count = _DEPRECATION_HITS.get(template, 0) + 1
            _DEPRECATION_HITS[template] = count
        if count == 1:
            logger.info("deprecation: legacy REST alias hit template=%s (first)", template)
        else:
            logger.debug("deprecation: legacy REST alias hit template=%s count=%d", template, count)
        return response
