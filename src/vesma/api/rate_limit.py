"""Slowapi rate-limiter singleton for Vesma API (T-AUTH, ADR-0014).

Imported by both ``vesma.api.auth`` (for endpoint decorators) and
``vesma.api.main`` (to set ``app.state.limiter`` and register the
exception handler).  A single object ensures the in-process counter state
is shared.
"""

from __future__ import annotations

from slowapi import Limiter
from starlette.requests import Request

from vesma.api.client_ip import resolve_client_ip


def _rate_key(request: Request) -> str:
    """Key function for slowapi.

    XFF is honoured only when the immediate peer is inside a configured
    ``api.trusted_proxies`` CIDR; otherwise the peer IP itself is used
    (finding auth-4 - untrusted peers must not be able to spoof XFF).
    """
    api_config = getattr(getattr(request.app, "state", None), "api_config", None)
    return resolve_client_ip(request, api_config)


limiter: Limiter = Limiter(key_func=_rate_key)
