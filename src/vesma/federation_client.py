"""Federation client (A-side) — mediated pull transport with fallback.

ArchCom 2026-07-17 federation contract §3.2 + КП-2 (fallback). A thin
HTTP transport that calls the peer's (B's) ``/api/v1/federation/pull``
endpoint and returns a :class:`PullResult` the A-side A2A handler
dispatches on. The client is a **pure transport** — it does NOT run
the local-search fallback itself; it returns the fallback signal and
the caller decides. This keeps the client testable without a live
``MemoryManager`` and avoids a hidden dependency cycle (the client
would need a manager to fall back, the manager is per-project, and
the fallback is a policy decision, not a transport concern).

КП-2 (contract §3.2): A timeout 2s → local ``mnemos_search``, partial
result. The client enforces the timeout via ``httpx`` and signals
fallback through ``PullResult.fell_back_to_local=True``.

Reference:
    - Contract §3.2: ``.archcom/sessions/2026-07-17-federation-contract.md``
    - КП-2: timeout + local fallback
    - ADR-0016: ``docs/project/adr/0016-federation-threat-model.md``
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

import httpx
import pydantic

from vesma.config import PeerConfig, Settings
from vesma.federation_server import PullResult
from vesma.trigger_codes import TriggerCode

# The peer's pull endpoint path — must match the route registered in
# ``vesma.api.federation``. Kept as a module constant so the client
# and the server route agree on the path without an import cycle.
FEDERATION_PULL_PATH = "/api/v1/federation/pull"

logger = logging.getLogger(__name__)

__all__ = [
    "FEDERATION_PULL_PATH",
    "pull_from_peer",
]


def pull_from_peer(*args: Any, **kwargs: Any) -> PullResult:
    """Vitals boundary #8 (A2): time the client pull (standalone — no manager)."""
    import time as _time

    from vesma.metrics.boundary import record_verb_standalone

    t0 = _time.monotonic()
    try:
        result = _pull_from_peer_impl(*args, **kwargs)
    except Exception:
        settings = kwargs.get("settings")
        if settings is not None:
            with contextlib.suppress(Exception):
                record_verb_standalone(
                    settings,
                    surface="background",
                    verb="federation.pull_client",
                    status="error",
                    latency_ms=(_time.monotonic() - t0) * 1000,
                    meta={"peer_id": args[0] if args else kwargs.get("peer_id", "")},
                )
        raise
    settings = kwargs.get("settings")
    if settings is not None:
        with contextlib.suppress(Exception):
            record_verb_standalone(
                settings,
                surface="background",
                verb="federation.pull_client",
                status="ok",
                latency_ms=(_time.monotonic() - t0) * 1000,
                meta={"peer_id": args[0] if args else kwargs.get("peer_id", "")},
            )
    return result


def _pull_from_peer_impl(
    peer_id: str,
    query: str,
    project_scope: str,
    *,
    settings: Settings,
    timeout_s: float = 2.0,
    transport: httpx.BaseTransport | None = None,
    include_content: bool = True,
    base_url_override: str | None = None,
) -> PullResult:
    """Pull from a federated peer with КП-2 fallback.

    Reads :class:`PeerConfig` from ``settings.federation.peers[peer_id]``
    for the per-peer bearer-token env name. The peer's base URL is
    read from the ``VESMA_FED_PEER_<peer_id_upper>_URL`` env var (the
    operator configures the peer's HTTP base URL out-of-band, like the
    bearer token; we never bake URLs into config files). The
    ``base_url_override`` is for tests.

    Args:
        peer_id: A2A id of the peer to pull from.
        query: The query topic.
        project_scope: Which project A wants to pull.
        settings: The Vesma :class:`Settings` (provides ``federation.peers``).
        timeout_s: HTTP timeout in seconds (КП-2 default 2.0s).
        transport: Optional ``httpx`` transport for tests (e.g.
            ``httpx.MockTransport``). When ``None``, the default
            ``httpx`` transport is used.
        include_content: Forwarded to the server (Phase 2.5 hook).
        base_url_override: Optional override for the peer's HTTP base
            URL. When ``None``, the client reads the URL from the env
            var ``VESMA_FED_PEER_<peer_id_upper>_URL``.

    Returns:
        :class:`PullResult` — ``trigger_code``, ``records``,
        ``fell_back_to_local``, ``peer_id``.

    Behaviour:

    * 200 → parse :class:`PullResponse`, return
      ``PullResult(trigger_code, records, fell_back_to_local=False)``.
    * Timeout / connection refused → КП-2 fallback:
      ``PullResult(trigger_code=OFFLINE_LITE, records=[], fell_back_to_local=True)``.
      The caller runs local ``mnemos_search``.
    * 403 → ``PullResult(trigger_code=REFUSED, records=[], fell_back_to_local=True)``.
    * 429 → ``PullResult(trigger_code=REFUSED, records=[], fell_back_to_local=True)``.
    * Unknown peer → fail-closed:
      ``PullResult(trigger_code=REFUSED, records=[], fell_back_to_local=True)``.
    * Missing bearer token env → fail-closed (REFUSED + fallback).
    """
    peer = settings.federation.peers.get(peer_id)
    if peer is None:
        logger.info("federation_client: unknown peer_id=%s — fail-closed (REFUSED)", peer_id)
        # Unknown peer is a config failure, not a transport failure —
        # return REFUSED (fail-closed), not OFFLINE_LITE.
        return PullResult(
            trigger_code=TriggerCode.REFUSED,
            records=[],
            fell_back_to_local=True,
            peer_id=peer_id,
        )

    token = _resolve_token(peer)
    if not token:
        logger.warning(
            "federation_client: bearer token env %s unset for peer_id=%s — fail-closed",
            peer.bearer_token_env,
            peer_id,
        )
        return _fallback(peer_id, trigger_code_refused=True)

    base_url = base_url_override if base_url_override is not None else _resolve_base_url(peer_id)
    if not base_url:
        logger.warning(
            "federation_client: base URL env unset for peer_id=%s — fail-closed",
            peer_id,
        )
        return _fallback(peer_id, trigger_code_refused=True)

    payload: dict[str, Any] = {
        "peer_id": peer_id,
        "query": query,
        "project_scope": project_scope,
        "include_content": include_content,
    }
    headers = {"Authorization": f"Bearer {token}"}

    try:
        with httpx.Client(transport=transport, timeout=timeout_s, base_url=base_url) as client:
            resp = client.post(FEDERATION_PULL_PATH, json=payload, headers=headers)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
        # КП-2: timeout / connection refused → fallback to local search.
        logger.info(
            "federation_client: transport error for peer_id=%s (%s) — КП-2 fallback",
            peer_id,
            type(exc).__name__,
        )
        return _fallback(peer_id)
    except httpx.HTTPError as exc:
        logger.warning(
            "federation_client: HTTP error for peer_id=%s: %s — fallback",
            peer_id,
            exc,
        )
        return _fallback(peer_id)

    if resp.status_code == 200:
        return _parse_ok(resp, peer_id)
    if resp.status_code in (403, 429):
        logger.info(
            "federation_client: peer_id=%s returned %d — REFUSED fallback",
            peer_id,
            resp.status_code,
        )
        return _fallback(peer_id, trigger_code_refused=True)
    logger.warning(
        "federation_client: unexpected status %d from peer_id=%s — fallback",
        resp.status_code,
        peer_id,
    )
    return _fallback(peer_id)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _resolve_token(peer: PeerConfig) -> str | None:
    """Read the per-peer bearer token from the env var named in config."""
    raw = os.environ.get(peer.bearer_token_env, "")
    return raw or None


def _resolve_base_url(peer_id: str) -> str | None:
    """Read the peer's HTTP base URL from ``VESMA_FED_PEER_<ID>_URL``.

    The env var name uppercases the peer id and replaces ``-`` with
    ``_`` so ``vesma-A`` → ``VESMA_FED_PEER_VESMA_A_URL``. Trailing
    slashes are stripped so ``base_url + path`` joins cleanly.
    """
    env_name = f"VESMA_FED_PEER_{peer_id.upper().replace('-', '_')}_URL"
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return None
    return raw.rstrip("/")


def _parse_ok(resp: httpx.Response, peer_id: str) -> PullResult:
    """Parse a 200 response into a :class:`PullResult`.

    On a malformed body the client fails closed (REFUSED + fallback) —
    a peer that returns 200 with a non-PullResponse body is either
    misconfigured or malicious, and КП-2 says "fallback on anything
    that is not a clean 200 PullResponse".
    """
    try:
        data = resp.json()
        return PullResult.model_validate(
            {
                "trigger_code": data["trigger_code"],
                "records": data.get("records", []),
                "fell_back_to_local": False,
                "peer_id": data.get("peer_id", peer_id),
            }
        )
    except (ValueError, KeyError, TypeError, pydantic.ValidationError) as exc:
        logger.warning(
            "federation_client: malformed 200 body from peer_id=%s: %s — fallback",
            peer_id,
            exc,
        )
        return _fallback(peer_id, trigger_code_refused=True)


def _fallback(peer_id: str, *, trigger_code_refused: bool = False) -> PullResult:
    """Build the canonical КП-2 fallback result.

    ``trigger_code_refused=True`` is used for auth/transport-refusal
    cases (403, 429, missing token, missing URL) where the contract
    says A should fall back to local search but the trigger is
    ``REFUSED`` (content cannot be shared), not ``OFFLINE_LITE`` (B
    is unreachable). The default is ``OFFLINE_LITE`` for the genuine
    transport-failure case (timeout / connection refused).
    """
    code = TriggerCode.REFUSED if trigger_code_refused else TriggerCode.OFFLINE_LITE
    return PullResult(
        trigger_code=code,
        records=[],
        fell_back_to_local=True,
        peer_id=peer_id,
    )
