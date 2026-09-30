# 0009. SSRF guard at the `mnemos_ingest_url` boundary

*Historical artifact — English only.*

- **Status**: Accepted
- **Date**: 2026-06-15
- **Deciders**: abyss, GCW Senior Security Engineer

## Context

`mnemos_ingest_url(url: str)` is a public MCP tool. An agent can hand it any URL.
Without validation, an attacker (or a careless agent) can probe the local network,
read cloud metadata services, or pivot to internal services.

A particularly dangerous target is the **cloud metadata endpoint**
(`169.254.169.254` for AWS, `metadata.google.internal` for GCP, etc.). A successful
SSRF here leaks the host's IAM credentials.

## Decision

`mnemos_ingest_url` validates the URL through `_validate_url()` **before** any
network call. The blocked list:

- **Schemes**: anything other than `http` / `https` is rejected.
- **Loopback**: `localhost`, `127.0.0.0/8`, `::1`.
- **Private IPv4 ranges**: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`.
- **Link-local**: `169.254.0.0/16` (includes the AWS / GCP / Azure metadata
  service at `169.254.169.254`).
- **Unspecified**: `0.0.0.0`, `::`.

The check happens on the **resolved hostname**, not the raw URL. A URL like
`http://[::ffff:169.254.169.254]/` is caught because `urlparse` normalises the
IPv6-mapped form.

(M15 hardening) `tests/test_security.py::test_cloud_metadata_endpoints_blocked`
covers the AWS IPv6-mapped metadata form
(`http://[fd00:ec2::254]/latest/meta-data/`) — see ADR-0012 for the fix.

## Consequences

**Positive**

- A class of escalation attacks (SSRF to cloud metadata) is closed at the boundary.
- The check is **opt-out impossible**: there is no `--allow-private` flag. If a
  legitimate use case arises (e.g. ingesting from a local Gitea), the operator
  must move the server out of the cloud metadata path or proxy through a public
  hostname.
- The 11-test `tests/test_security.py` covers the major cases (blocklist,
  allowlist, scheme, hostname resolution).

**Negative**

- An operator who **needs** to ingest from a private wiki on `192.168.1.10` cannot
  do so without a public proxy. Acceptable; the security benefit outweighs the
  inconvenience.
- The check is a string-level filter, not a network-level sandbox. A DNS rebinding
  attack (where `example.com` resolves to a public IP at check time, then to a
  private IP at request time) is not covered. Mitigated by re-resolving the host
  in `_validate_url` (ADR-0012) and by the per-hop redirect guard (v2, T5-SSRF):
  every `Location` target is validated by `_validate_url` before the next request
  is issued. `httpx.Client(follow_redirects=False)` is retained so the library
  never follows a redirect without the guard running first.

**Neutral**

- The blocklist is hard-coded. A future extension could read it from
  `~/.mnemos/ssrf_allowlist.yaml` (YAML-driven override). Not in v1.

## Amendment: v2 redirect posture (T5-SSRF, 2026-06-17)

Redirects are now **followed with per-hop re-validation** (max 5 hops).
`ingest_url` implements a manual redirect loop; every `Location` header is
resolved to an absolute URL and passed through `_validate_url` before the
next GET. This closes the open-redirect pivot described in the Negative
consequence above. `httpx.Client(follow_redirects=False)` is retained as
the enforcement mechanism - it ensures no hop is skipped by the library.
See `docs/security.md` §2 and `tests/test_ssrf_redirect.py` (23 tests).

## Alternatives considered

- **DNS-level sandboxing (running Vesma in a network namespace with no route to
  private IPs).** Rejected: requires root and a complex systemd setup; too heavy
  for v1.
- **Allow private IPs by default, require opt-out.** Rejected: SSRF risk is too
  high; opt-in is the safer default.
- **Run an outbound HTTP proxy with an allowlist.** Rejected: adds a separate
  process to operate; not justified for v1.

## References

- `tasks/senior-security-engineer/M15.2-bandit-cleanup.md` §"B104 (false positive
  for container networking)"
- `docs/security.md` § SSRF
- `src/vesma/manager.py::_validate_url` — implementation
- `tests/test_security.py` — 11 baseline tests + updated redirect test
- `tests/test_ssrf_redirect.py` — 23 per-hop redirect tests (T5-SSRF)
- ADR-0012 (IPv6 SSRF gap fix, M15)
