"""Shared helper for resolving the trusted client IP from a request.

Used by both ``rate_limit`` (finding auth-4) and ``middleware`` / ``auth``
session pinning (finding auth-6). The same trust-policy must apply
everywhere we make a security decision on the client IP:

- If the immediate peer (``request.client.host``) is inside one of the
  configured ``api.trusted_proxies`` CIDRs, take the left-most entry of
  ``X-Forwarded-For`` as the real client IP.
- Otherwise the peer IS the client; ignore any X-Forwarded-For (an
  untrusted peer can spoof it freely).
"""

from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.requests import Request

    from vesma.config import ApiConfig

logger = logging.getLogger(__name__)


def _peer_in_trusted_proxies(peer: str, trusted_proxies: list[str]) -> bool:
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for cidr in trusted_proxies:
        try:
            if peer_ip in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            logger.warning("api.trusted_proxies: invalid CIDR %r - ignored", cidr)
    return False


def resolve_client_ip(request: Request, api_config: ApiConfig | None) -> str:
    """Return the trusted client IP for a request.

    Falls back to ``"unknown"`` only when neither the ASGI peer nor a
    validated XFF header is available.
    """
    client = request.client
    peer = client.host if client else None

    if (
        api_config is not None
        and api_config.trusted_proxies
        and peer is not None
        and _peer_in_trusted_proxies(peer, api_config.trusted_proxies)
    ):
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            forwarded = xff.split(",")[0].strip()
            if forwarded:
                return forwarded

    return peer if peer else "unknown"
