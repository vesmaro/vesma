"""Federation A2A handler — mediated pull over the A2A protocol (Phase 2).

ArchCom 2026-07-17 federation contract §3.2. The mediated pull can run
over two transports:

1. **HTTP** — :func:`pull_from_peer` calls the peer's
   ``/api/v1/federation/pull`` endpoint directly (see
   :mod:`vesma.federation_client`).
2. **A2A** — when the ``a2a-orchestrator`` MCP server is available,
   A sends ``send_a2a(intent=request-info, query)`` to B, B receives
   it, runs the server flow, responds with
   ``send_a2a(intent=share-finding, ttl_class=ephemeral,
   payload=sanitized)``.

This module is the **A2A layer**: it translates between the A2A
message envelope and the :mod:`vesma.federation_server` /
:mod:`vesma.federation_client` pure functions. It does NOT contain
the server flow itself (that lives in ``federation_server.handle_pull``)
nor the HTTP transport (that lives in ``federation_client.pull_from_peer``).

Graceful degradation (mcp-enhancement.instructions.md): when the
``a2a-orchestrator`` MCP server is NOT available, the handler falls
back to the HTTP federation client directly. The A2A path is an
optimisation (richer envelope, agent-readable intent), not a hard
dependency — absence MUST NOT break federation.

Reference:
    - Contract §3.2: ``.archcom/sessions/2026-07-17-federation-contract.md``
    - §3.3: ephemeral enforcement (policy)
    - §9: trigger codes
    - КП-2: fallback
    - mcp-enhancement.instructions.md: graceful degradation
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from vesma.config import Settings
from vesma.federation_access_log import FederationAccessLog
from vesma.federation_client import pull_from_peer
from vesma.federation_server import PullRequest, PullResponse, PullResult, handle_pull
from vesma.manager import MemoryManager
from vesma.trigger_codes import TriggerCode, should_fallback_to_local

logger = logging.getLogger(__name__)

__all__ = [
    "A2AIntent",
    "A2AMessage",
    "A2AResponse",
    "build_share_finding_payload",
    "handle_request_info",
    "handle_share_finding",
    "mediate_pull_a_side",
]


# ── A2A message envelope (intent + payload) ─────────────────────────────────


class A2AIntent:
    """A2A intent constants used by the federation mediated pull.

    Mirrors the ``intent`` field of the ``a2a-orchestrator`` MCP
    server's ``send_a2a`` tool so this module is usable standalone
    (without importing the MCP server) and testable in isolation.
    """

    REQUEST_INFO = "request-info"
    SHARE_FINDING = "share-finding"


class A2AMessage(Protocol):
    """Minimal A2A message shape the handler consumes/produces.

    A ``Protocol`` so the handler accepts any dict-like or Pydantic
    object with these keys — the real ``a2a-orchestrator`` envelope, a
    plain dict, or a test fixture.
    """

    intent: str
    peer_id: str
    query: str
    project_scope: str
    payload: dict[str, Any]


# ── B-side: receive request-info → run server flow → share-finding ──────────


def handle_request_info(
    message: A2AMessage,
    *,
    settings: Settings,
    manager: MemoryManager,
    access_log: FederationAccessLog,
    presented_token: str | None = None,
    presented_mtls_fingerprint: str | None = None,
) -> A2AResponse:
    """B-side: receive an A2A ``request-info``, run the server flow.

    Returns an :class:`A2AResponse` carrying the ``share-finding``
    intent, the ``trigger_code``, the sanitized records, and the
    ``ttl_class="ephemeral"`` policy marker (contract §3.3).

    The handler is a thin adapter: it extracts the :class:`PullRequest`
    fields from the A2A message, delegates to
    :func:`vesma.federation_server.handle_pull`, and wraps the
    :class:`PullResponse` in the ``share-finding`` A2A envelope.
    """
    request = PullRequest(
        peer_id=message.peer_id,
        query=message.query,
        project_scope=message.project_scope,
    )
    response, http_status = handle_pull(
        request,
        settings=settings,
        manager=manager,
        access_log=access_log,
        presented_token=presented_token,
        presented_mtls_fingerprint=presented_mtls_fingerprint,
    )
    payload = build_share_finding_payload(response)
    return A2AResponse(
        intent=A2AIntent.SHARE_FINDING,
        http_status=http_status,
        payload=payload,
    )


def build_share_finding_payload(response: PullResponse) -> dict[str, Any]:
    """Build the ``share-finding`` A2A payload from a :class:`PullResponse`.

    Contract §3.3: the payload carries ``ttl_class="ephemeral"`` as a
    policy marker. The A-side agent body is responsible for NOT
    persisting these records via ``vesma_add`` — this is a policy,
    not a technical enforcement.
    """
    return {
        "trigger_code": response.trigger_code.value,
        "records": [r.model_dump() for r in response.records],
        "ttl_class": response.ttl_class,
        "peer_id": response.peer_id,
    }


# ── A-side: receive share-finding → dispatch on trigger_code ────────────────


class A2AResponse:
    """The B-side handler's return value — intent + http status + payload.

    A plain class (not Pydantic) so the A2A layer can adapt it to the
    real ``a2a-orchestrator`` envelope, an HTTP response, or a test
    assertion without a serialization round-trip.
    """

    def __init__(self, *, intent: str, http_status: int, payload: dict[str, Any]) -> None:
        self.intent = intent
        self.http_status = http_status
        self.payload = payload


def handle_share_finding(
    response: PullResponse,
    *,
    local_search: Any | None = None,
) -> dict[str, Any]:
    """A-side: dispatch on ``trigger_code`` (contract §3.2, §9, КП-2).

    Args:
        response: The :class:`PullResponse` (or :class:`PullResult`)
            received from B. Both share the ``trigger_code`` +
            ``records`` fields.
        local_search: Optional callable ``(query: str) -> list`` that
            runs the local ``vesma_search`` fallback. Called when the
            trigger code signals fallback (``REFUSED``, ``OFFLINE_LITE``)
            or when the pull fell back to local transport. When
            ``None``, the caller is responsible for running the
            fallback — the handler returns ``{"action": "fallback_local",
            "trigger_code": <code>}`` and the caller acts.

    Returns:
        A dict describing the action A should take:

        * ``{"action": "use", "records": [...]}`` — use the records in
          context (contract §3.3: do NOT call ``vesma_add``).
        * ``{"action": "noop", "trigger_code": "ALREADY_EXHAUSTED"}`` —
          reuse the previous answer (contract §9).
        * ``{"action": "fallback_local", "trigger_code": <code>,
          "local_results": [...]}`` — run local ``vesma_search``
          (КП-2). ``local_results`` is present only when ``local_search``
          was provided.
    """
    code = response.trigger_code
    if code == TriggerCode.ALREADY_EXHAUSTED:
        # Contract §9: reuse the previous answer, do not re-query.
        return {"action": "noop", "trigger_code": code.value}
    if code in (TriggerCode.EXHAUSTIVE, TriggerCode.PARTIAL):
        # Contract §3.3: use records in context; do NOT persist (policy).
        return {
            "action": "use",
            "records": [r.model_dump() for r in response.records],
            "trigger_code": code.value,
        }
    if should_fallback_to_local(code):
        # КП-2: REFUSED / OFFLINE_LITE → local search.
        action: dict[str, Any] = {"action": "fallback_local", "trigger_code": code.value}
        if local_search is not None:
            try:
                action["local_results"] = list(local_search(response))
            except Exception as exc:
                # КП-2 expects a partial result; [] is a valid partial.
                # The exception is logged (not silently swallowed) so the
                # operator can see when the local fallback is degrading.
                # `except Exception` is kept because the search layer has
                # no narrower public exception type — `MemoryManager.search`
                # swallows FTS/vector errors internally and returns [].
                logger.warning(
                    "local_search failed: %s: %s",
                    type(exc).__name__,
                    exc,
                    exc_info=False,
                )
                action["local_results"] = []
        return action
    # Unknown code — fail safe to local fallback.
    return {"action": "fallback_local", "trigger_code": code.value}


# ── A-side orchestration: try A2A, fall back to HTTP transport ────────────────


def mediate_pull_a_side(
    peer_id: str,
    query: str,
    project_scope: str,
    *,
    settings: Settings,
    use_a2a: bool = False,
    timeout_s: float = 2.0,
    include_content: bool = True,
) -> PullResult:
    """A-side entry point — try A2A, fall back to HTTP transport.

    When ``use_a2a=True`` and the ``a2a-orchestrator`` MCP server is
    available, this would call ``send_a2a(intent=request-info, ...)``
    and wait for the ``share-finding`` response. Phase 2 does NOT
    wire the live MCP call here (that requires the MCP runtime); it
    falls back to the HTTP transport (:func:`pull_from_peer`). The
    ``use_a2a`` flag is reserved for the integration session that
    connects the handler to the live ``a2a-orchestrator`` server.

    ``timeout_s`` and ``include_content`` are forwarded to
    :func:`pull_from_peer` on both the A2A and HTTP paths, so callers
    can override the transport timeout and the content-inclusion flag
    regardless of which path is taken.

    Graceful degradation (mcp-enhancement.instructions.md): the
    HTTP fallback is always available; A2A is an optimisation.
    """
    if use_a2a:
        logger.info(
            "federation_a2a: A2A path requested for peer_id=%s — "
            "Phase 2 falls back to HTTP transport (a2a-orchestrator live "
            "wiring is a follow-up integration task)",
            peer_id,
        )
    return pull_from_peer(
        peer_id,
        query,
        project_scope,
        settings=settings,
        timeout_s=timeout_s,
        include_content=include_content,
    )
