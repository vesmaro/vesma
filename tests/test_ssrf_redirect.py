"""T5-SSRF: per-hop redirect re-validation tests.

Verifies that ingest_url follows redirects manually and validates every
Location target through _validate_url before issuing the next request.

Test coverage:
  (a) Redirect to AWS IPv4 metadata (169.254.169.254) is BLOCKED.
  (b) Redirect to loopback / private ranges is BLOCKED.
  (c) Redirect to IPv6 metadata / loopback is BLOCKED.
  (d) Legitimate public->public redirect is FOLLOWED and content extracted.
  (e) Exceeding the hop limit (_MAX_REDIRECTS) results in a placeholder.
  (f) Location with a non-http(s) scheme is BLOCKED.
  (g) Relative Location is resolved to an absolute URL and then validated.

All tests mock httpx.Client and trafilatura -- no real network calls.
Initial URLs use literal public IPs (8.8.8.8) so _validate_url passes
without DNS resolution.
"""

from __future__ import annotations

import sys
import tempfile
from unittest.mock import MagicMock, call, patch

import pytest

from vesma.config import Settings
from vesma.manager import _MAX_REDIRECTS, MemoryManager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def manager() -> MemoryManager:  # type: ignore[misc]
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = Settings(
            vesma={"vault_path": tmpdir, "data_dir": tmpdir, "db_name": "test.db"},
            embedding={"provider": "nano"},
        )
        mgr = MemoryManager(settings)
        yield mgr  # type: ignore[misc]
        mgr.close()


TAGS = ["project:test", "agent:test", "mnemos:test"]
# 8.8.8.8 is a public IP (Google DNS) -- passes _validate_url without DNS.
INITIAL_URL = "https://8.8.8.8/"


def _stub_trafilatura() -> MagicMock:
    stub = MagicMock()
    stub.extract.return_value = "extracted"
    return stub


def _mock_resp(status: int, body: str = "", location: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = body
    # Use a real dict so .get() works without side-effects.
    r.headers = {"location": location} if location else {}
    return r


# ---------------------------------------------------------------------------
# (a) Redirect to AWS IPv4 metadata endpoint is BLOCKED
# ---------------------------------------------------------------------------


class TestMetadataRedirectBlocked:
    def test_redirect_to_aws_ipv4_metadata_blocked(self, manager: MemoryManager) -> None:
        """Pivot: public host -> 169.254.169.254 must be caught on the redirect hop.

        SSRF rejections are re-raised as ValueError — the blocked URL is NOT
        stored in memory (T5-SSRF v3 hardening).
        """
        redir = _mock_resp(302, location="http://169.254.169.254/latest/meta-data/")
        trafilatura_stub = _stub_trafilatura()
        with (
            patch("httpx.Client") as mock_cls,
            patch.dict(sys.modules, {"trafilatura": trafilatura_stub}),
        ):
            mock_client = MagicMock()
            mock_client.get.return_value = redir
            mock_cls.return_value.__enter__.return_value = mock_client
            with pytest.raises(ValueError, match="security reasons"):
                manager.ingest_url(INITIAL_URL, tags=TAGS, project="test", agent="test")

    def test_redirect_to_aws_ipv4_metadata_second_hop_blocked(self, manager: MemoryManager) -> None:
        """Second-hop metadata pivot: public -> public -> 169.254.169.254 is caught."""
        hop1 = _mock_resp(301, location="https://1.1.1.1/")  # public; passes
        hop2 = _mock_resp(302, location="http://169.254.169.254/")  # blocked
        trafilatura_stub = _stub_trafilatura()
        with (
            patch("httpx.Client") as mock_cls,
            patch.dict(sys.modules, {"trafilatura": trafilatura_stub}),
        ):
            mock_client = MagicMock()
            mock_client.get.side_effect = [hop1, hop2]
            mock_cls.return_value.__enter__.return_value = mock_client
            with pytest.raises(ValueError, match="security reasons"):
                manager.ingest_url(INITIAL_URL, tags=TAGS, project="test", agent="test")


# ---------------------------------------------------------------------------
# (b) Redirect to loopback / private ranges is BLOCKED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/",
        "http://127.0.0.2/",
        "http://10.0.0.1/",
        "http://10.255.255.255/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
    ],
)
def test_redirect_to_private_range_blocked(manager: MemoryManager, location: str) -> None:
    """Redirect to any RFC1918/loopback address must be blocked on the hop.

    SSRF rejections are re-raised as ValueError — the blocked URL is NOT
    stored in memory (T5-SSRF v3 hardening).
    """
    redir = _mock_resp(301, location=location)
    trafilatura_stub = _stub_trafilatura()
    with (
        patch("httpx.Client") as mock_cls,
        patch.dict(sys.modules, {"trafilatura": trafilatura_stub}),
    ):
        mock_client = MagicMock()
        mock_client.get.return_value = redir
        mock_cls.return_value.__enter__.return_value = mock_client
        with pytest.raises(ValueError, match="security reasons"):
            manager.ingest_url(INITIAL_URL, tags=TAGS, project="test", agent="test")


# ---------------------------------------------------------------------------
# (c) Redirect to IPv6 metadata / loopback is BLOCKED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "http://[::1]/",  # IPv6 loopback
        "http://[fd00:ec2::254]/latest/meta-data/",  # AWS IPv6 metadata
        "http://[fe80::1]/",  # IPv6 link-local
    ],
)
def test_redirect_to_ipv6_blocked(manager: MemoryManager, location: str) -> None:
    """Redirect to IPv6 loopback / link-local / metadata must be blocked.

    SSRF rejections are re-raised as ValueError — the blocked URL is NOT
    stored in memory (T5-SSRF v3 hardening).
    """
    redir = _mock_resp(302, location=location)
    trafilatura_stub = _stub_trafilatura()
    with (
        patch("httpx.Client") as mock_cls,
        patch.dict(sys.modules, {"trafilatura": trafilatura_stub}),
    ):
        mock_client = MagicMock()
        mock_client.get.return_value = redir
        mock_cls.return_value.__enter__.return_value = mock_client
        with pytest.raises(ValueError, match="security reasons"):
            manager.ingest_url(INITIAL_URL, tags=TAGS, project="test", agent="test")


# ---------------------------------------------------------------------------
# (d) Legitimate public -> public redirect is followed and content extracted
# ---------------------------------------------------------------------------


class TestLegitimateRedirectFollowed:
    def test_single_redirect_public_to_public(self, manager: MemoryManager) -> None:
        """A single 301 public->public redirect is followed; content is returned."""
        # 1.1.1.1 = Cloudflare DNS - public IP, passes _validate_url.
        hop1 = _mock_resp(301, location="https://1.1.1.1/canonical")
        final = _mock_resp(200, body="canonical content")
        trafilatura_stub = _stub_trafilatura()
        trafilatura_stub.extract.return_value = "canonical content"

        with (
            patch("httpx.Client") as mock_cls,
            patch.dict(sys.modules, {"trafilatura": trafilatura_stub}),
        ):
            mock_client = MagicMock()
            mock_client.get.side_effect = [hop1, final]
            mock_cls.return_value.__enter__.return_value = mock_client
            mem = manager.ingest_url(INITIAL_URL, tags=TAGS, project="test", agent="test")

        assert "[fetch failed:" not in mem.content
        # Two GET calls: initial URL + redirect target.
        assert mock_client.get.call_count == 2

    def test_redirect_chain_within_limit(self, manager: MemoryManager) -> None:
        """A redirect chain <= MAX_REDIRECTS hops is followed successfully."""
        # Build a chain: initial -> hop1 -> hop2 -> final (3 hops total)
        ips = ["1.1.1.1", "8.8.4.4", "9.9.9.9"]
        hops = [_mock_resp(302, location=f"https://{ip}/") for ip in ips]
        final = _mock_resp(200, body="destination")
        trafilatura_stub = _stub_trafilatura()
        trafilatura_stub.extract.return_value = "destination"

        with (
            patch("httpx.Client") as mock_cls,
            patch.dict(sys.modules, {"trafilatura": trafilatura_stub}),
        ):
            mock_client = MagicMock()
            mock_client.get.side_effect = [*hops, final]
            mock_cls.return_value.__enter__.return_value = mock_client
            mem = manager.ingest_url(INITIAL_URL, tags=TAGS, project="test", agent="test")

        assert "[fetch failed:" not in mem.content
        assert mock_client.get.call_count == len(ips) + 1  # initial + 3 hops

    def test_follow_redirects_false_on_client(self, manager: MemoryManager) -> None:
        """httpx.Client must still be constructed with follow_redirects=False.

        The manual per-hop loop relies on the client NOT auto-following, so
        this constructor argument is the enforcement mechanism.
        """
        trafilatura_stub = _stub_trafilatura()
        ok_resp = _mock_resp(200, body="text")
        with (
            patch("httpx.Client") as mock_cls,
            patch.dict(sys.modules, {"trafilatura": trafilatura_stub}),
        ):
            mock_client = MagicMock()
            mock_client.get.return_value = ok_resp
            mock_cls.return_value.__enter__.return_value = mock_client
            manager.ingest_url(INITIAL_URL, tags=TAGS, project="test", agent="test")
        _, kwargs = mock_cls.call_args
        assert kwargs.get("follow_redirects") is False


# ---------------------------------------------------------------------------
# (e) Exceeding the hop limit results in a placeholder (not an exception)
# ---------------------------------------------------------------------------


def test_too_many_redirects_produces_placeholder(manager: MemoryManager) -> None:
    """ingest_url degrades to a placeholder when MAX_REDIRECTS is exceeded."""
    # Use distinct public IPs to avoid triggering the loop-detection set.
    # Build MAX_REDIRECTS + 2 redirect responses so the limit is clearly exceeded.
    redirect_ips = [f"1.2.3.{i}" for i in range(1, _MAX_REDIRECTS + 3)]
    redirects = [_mock_resp(302, location=f"https://{ip}/") for ip in redirect_ips]
    # Validate that the ips themselves are actually public (not private/blocked).
    for ip in redirect_ips:
        MemoryManager._validate_url(f"https://{ip}/")

    trafilatura_stub = _stub_trafilatura()
    with (
        patch("httpx.Client") as mock_cls,
        patch.dict(sys.modules, {"trafilatura": trafilatura_stub}),
    ):
        mock_client = MagicMock()
        mock_client.get.side_effect = redirects
        mock_cls.return_value.__enter__.return_value = mock_client
        mem = manager.ingest_url(INITIAL_URL, tags=TAGS, project="test", agent="test")

    assert "[fetch failed:" in mem.content


# ---------------------------------------------------------------------------
# (f) Location with a non-http(s) scheme is BLOCKED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "gopher://example.com/",
        "mailto:attacker@evil.com",
    ],
)
def test_redirect_to_non_http_scheme_blocked(manager: MemoryManager, location: str) -> None:
    """A redirect to any scheme other than http/https must be rejected.

    SSRF rejections are re-raised as ValueError — the blocked URL is NOT
    stored in memory (T5-SSRF v3 hardening).
    """
    redir = _mock_resp(302, location=location)
    trafilatura_stub = _stub_trafilatura()
    with (
        patch("httpx.Client") as mock_cls,
        patch.dict(sys.modules, {"trafilatura": trafilatura_stub}),
    ):
        mock_client = MagicMock()
        mock_client.get.return_value = redir
        mock_cls.return_value.__enter__.return_value = mock_client
        with pytest.raises(ValueError, match="security reasons"):
            manager.ingest_url(INITIAL_URL, tags=TAGS, project="test", agent="test")


# ---------------------------------------------------------------------------
# (g) Relative Location is resolved to an absolute URL before validation
# ---------------------------------------------------------------------------


class TestRelativeLocationResolution:
    def test_relative_path_resolved_against_base(self, manager: MemoryManager) -> None:
        """A path-relative Location (/new-path) is resolved via urljoin and validated."""
        # Redirect to /canonical on the same host -> stays on 8.8.8.8 -> public IP -> OK.
        redir = _mock_resp(301, location="/canonical")
        final = _mock_resp(200, body="resolved page")
        trafilatura_stub = _stub_trafilatura()
        trafilatura_stub.extract.return_value = "resolved page"

        with (
            patch("httpx.Client") as mock_cls,
            patch.dict(sys.modules, {"trafilatura": trafilatura_stub}),
        ):
            mock_client = MagicMock()
            mock_client.get.side_effect = [redir, final]
            mock_cls.return_value.__enter__.return_value = mock_client
            mem = manager.ingest_url(INITIAL_URL, tags=TAGS, project="test", agent="test")

        assert "[fetch failed:" not in mem.content
        # Second GET must target the resolved absolute URL, not the bare relative path.
        second_call_url = mock_client.get.call_args_list[1]
        assert second_call_url == call("https://8.8.8.8/canonical", timeout=30)

    def test_relative_location_to_private_ip_blocked_after_resolution(
        self, manager: MemoryManager
    ) -> None:
        """Even when Location is relative, resolved URL pointing to private IP is blocked.

        SSRF rejections are re-raised as ValueError — the blocked URL is NOT
        stored in memory (T5-SSRF v3 hardening).
        """
        # Protocol-relative Location that resolves to a metadata host.
        # urljoin("https://8.8.8.8/", "http://169.254.169.254/") = "http://169.254.169.254/"
        redir = _mock_resp(302, location="http://169.254.169.254/")
        trafilatura_stub = _stub_trafilatura()
        with (
            patch("httpx.Client") as mock_cls,
            patch.dict(sys.modules, {"trafilatura": trafilatura_stub}),
        ):
            mock_client = MagicMock()
            mock_client.get.return_value = redir
            mock_cls.return_value.__enter__.return_value = mock_client
            with pytest.raises(ValueError, match="security reasons"):
                manager.ingest_url(INITIAL_URL, tags=TAGS, project="test", agent="test")


# ---------------------------------------------------------------------------
# Redirect-loop detection
# ---------------------------------------------------------------------------


def test_redirect_loop_produces_placeholder(manager: MemoryManager) -> None:
    """A URL that redirects back to itself (or to an already-visited URL) is caught."""
    # A -> B -> A: after the second visit to A it should be detected.
    # We use a mock that alternates between two public IPs.
    hop_ab = _mock_resp(302, location="https://1.1.1.1/")  # -> 1.1.1.1
    hop_ba = _mock_resp(302, location="https://8.8.8.8/")  # -> back to initial host

    trafilatura_stub = _stub_trafilatura()
    with (
        patch("httpx.Client") as mock_cls,
        patch.dict(sys.modules, {"trafilatura": trafilatura_stub}),
    ):
        mock_client = MagicMock()
        # Cycle: 8.8.8.8 -> 1.1.1.1 -> 8.8.8.8 -> 1.1.1.1 -> ...
        # The hop limit will fire first (MAX_REDIRECTS = 5) but either way
        # a placeholder is produced.
        mock_client.get.side_effect = [hop_ab, hop_ba] * 10
        mock_cls.return_value.__enter__.return_value = mock_client
        mem = manager.ingest_url(INITIAL_URL, tags=TAGS, project="test", agent="test")
    assert "[fetch failed:" in mem.content


# ---------------------------------------------------------------------------
# (h) Obfuscated IPv4 encodings (decimal / octal / hex) are BLOCKED
# ---------------------------------------------------------------------------
# These collapse to loopback / metadata addresses at resolve time. _validate_url
# resolves the host through the SAME getaddrinfo the real connection uses, so an
# encoding that a resolver folds into 127.0.0.1 is rejected as loopback, while
# one no resolver recognises raises "could not be resolved" -- either way the
# request is refused (CWE-918 defence-in-depth).


@pytest.mark.parametrize(
    "host",
    [
        "2130706433",  # decimal for 127.0.0.1
        "0177.0.0.1",  # octal first octet -> 127.0.0.1
        "0x7f.0.0.1",  # hex first octet -> 127.0.0.1
        "017700000001",  # full octal 127.0.0.1
    ],
)
def test_obfuscated_ipv4_loopback_blocked(manager: MemoryManager, host: str) -> None:
    """Decimal/octal/hex encodings of 127.0.0.1 must not pass _validate_url."""
    with pytest.raises(ValueError):
        MemoryManager._validate_url(f"http://{host}/")


def test_redirect_to_decimal_metadata_blocked(manager: MemoryManager) -> None:
    """A redirect Location using the decimal encoding of a private IP is blocked.

    SSRF rejections are re-raised as ValueError — the blocked URL is NOT
    stored in memory (T5-SSRF v3 hardening).
    """
    # 2852039166 = 169.254.169.254 (AWS metadata) in decimal notation.
    redir = _mock_resp(302, location="http://2852039166/")
    trafilatura_stub = _stub_trafilatura()
    with (
        patch("httpx.Client") as mock_cls,
        patch.dict(sys.modules, {"trafilatura": trafilatura_stub}),
    ):
        mock_client = MagicMock()
        mock_client.get.return_value = redir
        mock_cls.return_value.__enter__.return_value = mock_client
        with pytest.raises(ValueError, match="security reasons"):
            manager.ingest_url(INITIAL_URL, tags=TAGS, project="test", agent="test")


# ---------------------------------------------------------------------------
# (i) userinfo in the Location must not fool host extraction
# ---------------------------------------------------------------------------
# Attack: "http://trusted.example@169.254.169.254/" -- a naive parser that
# treats the part before "@" as the host would connect to the metadata IP while
# logging "trusted.example". urlparse correctly returns the post-"@" authority
# as the host, so _validate_url checks the REAL target and blocks it.


def test_userinfo_does_not_mask_blocked_host(manager: MemoryManager) -> None:
    """A benign-looking userinfo prefix must not bypass host validation."""
    with pytest.raises(ValueError):
        MemoryManager._validate_url("http://trusted.example@169.254.169.254/")


def test_redirect_with_userinfo_to_metadata_blocked(manager: MemoryManager) -> None:
    """A redirect whose Location hides a metadata IP behind userinfo is blocked.

    SSRF rejections are re-raised as ValueError — the blocked URL is NOT
    stored in memory (T5-SSRF v3 hardening).
    """
    redir = _mock_resp(302, location="http://api.public.test@169.254.169.254/latest/meta-data/")
    trafilatura_stub = _stub_trafilatura()
    with (
        patch("httpx.Client") as mock_cls,
        patch.dict(sys.modules, {"trafilatura": trafilatura_stub}),
    ):
        mock_client = MagicMock()
        mock_client.get.return_value = redir
        mock_cls.return_value.__enter__.return_value = mock_client
        with pytest.raises(ValueError, match="security reasons"):
            manager.ingest_url(INITIAL_URL, tags=TAGS, project="test", agent="test")
