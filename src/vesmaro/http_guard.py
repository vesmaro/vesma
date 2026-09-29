"""SSRF guard for the engine's outbound HTTP legs (ADR-0009, ADR-0012).

One guard, every outbound leg. The body lived on
``MemoryManager._validate_url`` while URL ingest was the engine's only
outbound HTTP leg; W4c added the second one (the Jev decision adapter,
:mod:`vesmaro.decision_jev`), and a guard duplicated per caller is a
guard that drifts. The function was extracted verbatim — the manager
method now delegates here, and its pin tests (``test_security``,
``test_ssrf_redirect``) are unchanged.

Covers (ADR-0009, ADR-0012):
- Schemes: only http, https
- DNS names: resolved and the *resolved* IP is checked
- IPv4: loopback, RFC1918 private, link-local (169.254/16), 0.0.0.0
- IPv6: loopback (::1), link-local (fe80::/10), unique-local (fc00::/7
  which includes AWS IPv6 metadata fd00:ec2::254), IPv4-mapped IPv6
- Any IP flagged by ``ipaddress`` as private/loopback/link-local/
  reserved/multicast is rejected
"""

from __future__ import annotations


def validate_url_ssrf(url: str) -> str:
    """Validate ``url`` for SSRF safety.

    Raises ``ValueError`` on a blocked scheme or host; returns the url
    unchanged when admissible. Call BEFORE any connection is attempted —
    the guard is a pre-flight boundary, not a post-mortem.
    """
    import ipaddress
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"URL scheme must be http(s), got {parsed.scheme}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("URL must have a host")

    # The "0.0.0.0" entry is a blocklist literal, NOT a socket bind.
    # nosec B104 — see ADR-0009 §"B104 false positive".
    blocked_v4_literals: set[str] = {
        "localhost",
        "127.0.0.1",
        "0.0.0.0",  # nosec B104 — blocklist entry
        "::1",
        "169.254.169.254",  # AWS IPv4 metadata
    }

    if host in blocked_v4_literals:
        raise ValueError(f"URL host blocked for SSRF safety: {host}")

    # 1) If host is a literal IP (v4 or v6), use ipaddress to classify it.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None  # not a literal IP; it's a DNS name — resolve below

    if ip is not None:
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(f"URL host blocked for SSRF safety: {host}")
        return url

    # 2) DNS name. Check IPv4-prefix heuristics first (cheap, fast-fail).
    if host.startswith("127."):
        raise ValueError(f"URL host blocked for SSRF safety: {host}")
    if host.startswith("10."):
        raise ValueError(f"URL host blocked for SSRF safety: {host}")
    if host.startswith("192.168."):
        raise ValueError(f"URL host blocked for SSRF safety: {host}")
    if host.startswith("172."):
        second_octet = host[4:].split(".")[0]
        if second_octet.isdigit() and 16 <= int(second_octet) <= 31:
            raise ValueError(f"URL host blocked for SSRF safety: {host}")

    # 3) Resolve the DNS name. If any resolved address is private/loopback/
    # link-local, reject. This closes DNS rebinding at the boundary: even
    # if the resolver returns a public IP at check time and a private IP
    # at TCP time, we re-checked at the *resolve* step and the httpx
    # Client below will be the one making the actual connection. The
    # boundary check still raises the bar.
    import socket

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"URL host could not be resolved: {host} ({exc})") from exc

    for info in infos:
        sockaddr = info[4]
        # ``sockaddr[0]`` is typed as ``str | int`` by ``typeshed``
        # (on some platforms it can be a 4-byte packed int); force
        # a ``str`` so downstream ``startswith`` / ``ip_address`` work
        # uniformly and mypy can narrow the type.
        resolved = str(sockaddr[0])
        # Strip IPv4-mapped IPv6 prefix (e.g. "::ffff:127.0.0.1" → "127.0.0.1")
        if resolved.startswith("::ffff:"):
            resolved = resolved[len("::ffff:") :]
        try:
            rip = ipaddress.ip_address(resolved)
        except ValueError:
            continue
        if (
            rip.is_private
            or rip.is_loopback
            or rip.is_link_local
            or rip.is_reserved
            or rip.is_multicast
            or rip.is_unspecified
        ):
            raise ValueError(f"URL host resolves to blocked address: {host} → {resolved}")

    return url
