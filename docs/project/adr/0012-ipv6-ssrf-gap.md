# 0012. Fix IPv6 SSRF gap in `_validate_url` (M15)

*Historical artifact — English only.*

- **Status**: Accepted
- **Date**: 2026-06-15
- **Deciders**: abyss, GCW Senior Security Engineer
- **Supersedes**: (none — this is a tightening of ADR-0009)

## Context

`tests/test_security.py::test_cloud_metadata_endpoints_blocked[
http://[fd00:ec2::254]/latest/meta-data/]` failed after M15.2 closed most SSRF
gaps. The test reproduces a real attack vector:

- AWS supports an IPv6 metadata endpoint: `fd00:ec2::254` (the IPv6 equivalent of
  `169.254.169.254`).
- The current `_validate_url` checks `host.startswith("127.")`, `("10.")`,
  `("192.168.")`, and `("172." + 16-31)`. It does **not** check the IPv6
  link-local prefix `fe80::/10`, the IPv6 unique-local prefix `fc00::/7` (which
  covers `fd00::/8`), or the IPv4-mapped IPv6 form `::ffff:169.254.169.254`.
- A URL like `http://[fd00:ec2::254]/latest/meta-data/iam/security-credentials/`
  passes the check and is fetched. The host's IAM role is leaked.

## Decision

`_validate_url` is extended with a `ipaddress` module check:

```python
import ipaddress

def _validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(...)
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(...)
    # ... existing IPv4 checks ...

    # IPv6 / IPv4-mapped IPv6 / future-proof
    try:
        ip = ipaddress.ip_address(host)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise ValueError(f"URL host blocked: {host}")
    except ValueError as e:
        # Re-raise if it's our ValueError; otherwise host is a DNS name, not an IP
        if "blocked" in str(e):
            raise
        # Host is a DNS name (e.g. "example.com") — fall through to DNS resolution check
        ...
```

The `ipaddress` module classifies `169.254.169.254` as `is_link_local`,
`fd00:ec2::254` as `is_private` (under `fc00::/7`), `::1` as `is_loopback`. The
explicit `is_private` check is the umbrella that catches the unique-local prefix
without enumerating it.

A second pass resolves DNS names and checks the **resolved IP** (not the
hostname). This closes DNS rebinding at the boundary: a host that resolves to a
public IP at check time but a private IP at request time is caught by `httpx`'s
connection-time check.

## Consequences

**Positive**

- The IPv6 metadata endpoint (`fd00:ec2::254`) is blocked. The test
  `test_cloud_metadata_endpoints_blocked` passes.
- DNS rebinding at the validator level is closed (resolved IP is checked).
- The `ipaddress` module is the canonical Python way to classify IP ranges; it
  stays up to date as new IANA assignments happen.

**Negative**

- The DNS resolution adds a small (single-digit ms) latency to every URL ingest.
  Acceptable; `mnemos_ingest_url` is not on the hot path.
- A legitimate use case for ingesting from a public host that resolves to a
  private IP is now impossible. The same as ADR-0009 — explicit and documented.

**Neutral**

- The check is a string+IP-level filter, not a network-level sandbox. The
  remaining attack surface is "the user's request goes to a public host that
  is compromised and pivots to a private IP at TCP time" — out of scope for
  v1.

## Alternatives considered

- **Use `socket.getaddrinfo(host, ...)` to resolve, then check the first
  address only.** Rejected: returns multiple addresses; the second address
  could be private.
- **Run a separate DNS resolver that always returns the public IP.** Rejected:
  adds a sidecar process; not justified at v1 scale.
- **Block the IPv6 `fd00::/8` and `fe80::/10` prefixes explicitly, without the
  generic `is_private` check.** Rejected: misses future IANA assignments; not
  future-proof.

## References

- `tasks/senior-security-engineer/M15.2-bandit-cleanup.md`
- ADR-0009 (parent SSRF guard)
- `src/vesma/manager.py::_validate_url` — implementation
- `tests/test_security.py::test_cloud_metadata_endpoints_blocked` — test
- AWS docs: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instancedata-data-retrieval.html
