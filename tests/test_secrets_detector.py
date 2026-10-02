"""Unit tests for the secrets_detector module (#86).

All fixtures use OBVIOUSLY FAKE values (per sensitive-data.instructions.md).
No real credentials appear anywhere in this file.
"""

from __future__ import annotations

import pytest

from vesma.secrets_detector import (
    SecretFinding,
    detect_secrets,
    findings_by_pattern,
    redact_content,
)

# ---------------------------------------------------------------------------
# Mark every test module so the suite is easy to grep for fake fixtures.
# ---------------------------------------------------------------------------
# Fixture convention: every secret-looking string below is a synthetic
# test fixture with no relation to any real credential. Examples:
#   AKIATESTTESTTESTTESTTT  — 20 chars, AKIA + 16 alnum, fake
#   ghp_TESTTESTTESTTESTTESTTESTTESTTESTTT — 40 chars, ghp_ + 36 alnum, fake
#   sk-test...               — short, fake


# ── AWS access keys ───────────────────────────────────────────────────────────


class TestAwsKey:
    def test_positive(self) -> None:
        # AKIA + 16 uppercase alnum = exactly 20 chars.
        # Fake fixture: AKIA + 16 uppercase T's.
        token = "AKIA" + "T" * 16  # 20 chars total
        content = f"key={token} end"
        findings = detect_secrets(content)
        assert len(findings) == 1
        assert findings[0].pattern_name == "aws-key"
        assert findings[0].matched_value == token
        assert findings[0].start == 4
        assert findings[0].end == 24

    def test_negative_lowercase_rejected(self) -> None:
        # Lowercase after AKIA does NOT match (regex requires uppercase).
        content = "AKIAnotmatchingbecauselower end"
        findings = detect_secrets(content)
        # Note: 'AKIAnotmatchingbecauselower' is 29 chars but the regex
        # requires [0-9A-Z]{16} after AKIA — lowercase breaks it.
        aws_findings = [f for f in findings if f.pattern_name == "aws-key"]
        assert aws_findings == []

    def test_negative_too_short(self) -> None:
        # AKIA + only 15 chars — too short, no match.
        content = "AKIA123456789012345 end"  # 15 digits after AKIA
        aws_findings = [f for f in detect_secrets(content) if f.pattern_name == "aws-key"]
        assert aws_findings == []


# ── GitHub tokens ─────────────────────────────────────────────────────────────


class TestGithubToken:
    @pytest.mark.parametrize(
        "prefix",
        ["ghp_", "gho_", "ghu_", "ghs_", "ghr_"],
    )
    def test_positive_all_prefixes(self, prefix: str) -> None:
        # 36 alnum chars after the prefix.
        token = prefix + "T" * 36  # 41 chars total
        content = f"token={token} end"
        findings = detect_secrets(content)
        gh = [f for f in findings if f.pattern_name == "github-token"]
        assert len(gh) == 1
        assert gh[0].matched_value == token

    def test_negative_too_short(self) -> None:
        # Only 35 chars after prefix — too short (need 36).
        content = "ghp_" + "T" * 35
        gh = [f for f in detect_secrets(content) if f.pattern_name == "github-token"]
        assert gh == []


# ── Slack tokens ──────────────────────────────────────────────────────────────


class TestSlackToken:
    @pytest.mark.parametrize("prefix", ["xoxa-", "xoxb-", "xoxp-", "xoxr-", "xoxs-"])
    def test_positive_all_prefixes(self, prefix: str) -> None:
        token = prefix + "TESTTESTTESTTESTTESTTESTTEST"
        content = f"slack={token} end"
        findings = detect_secrets(content)
        sl = [f for f in findings if f.pattern_name == "slack-token"]
        assert len(sl) == 1
        assert sl[0].matched_value == token

    def test_negative_wrong_prefix(self) -> None:
        # xoxc- is not a real Slack prefix and not in our pattern.
        content = "xoxc-TESTTESTTESTTEST end"
        sl = [f for f in detect_secrets(content) if f.pattern_name == "slack-token"]
        assert sl == []


# ── OpenAI / Anthropic keys ────────────────────────────────────────────────────


class TestOpenAIKey:
    def test_positive(self) -> None:
        # sk- + 20+ alnum.
        token = "sk-TESTTESTTESTTESTTESTTEST"  # sk- + 24 chars
        content = f"openai_key={token}"
        findings = detect_secrets(content)
        oai = [f for f in findings if f.pattern_name == "openai-key"]
        assert len(oai) == 1
        assert oai[0].matched_value == token

    def test_negative_too_short(self) -> None:
        # sk- + only 10 chars — too short (need 20+).
        content = "sk-TESTTESTTT"
        oai = [f for f in detect_secrets(content) if f.pattern_name == "openai-key"]
        assert oai == []


# ── JWT ───────────────────────────────────────────────────────────────────────


class TestJWT:
    def test_positive(self) -> None:
        # Three base64url segments joined by dots, starting with eyJ.
        # Use only [A-Za-z0-9_-] (no padding) to match the regex.
        token = "eyJTESTTESTTEST.eyJTESTTESTTEST.TESTTESTTEST"
        content = f"Authorization: Bearer {token}"
        findings = detect_secrets(content)
        jwts = [f for f in findings if f.pattern_name == "jwt"]
        assert len(jwts) == 1
        assert jwts[0].matched_value == token

    def test_negative_missing_segment(self) -> None:
        # Only two segments — not a JWT.
        content = "eyJTESTTEST.eyJTESTTEST"
        jwts = [f for f in detect_secrets(content) if f.pattern_name == "jwt"]
        assert jwts == []


# ── PEM private keys ──────────────────────────────────────────────────────────


class TestPemPrivateKey:
    def test_positive_rsa(self) -> None:
        content = "-----BEGIN RSA PRIVATE KEY-----\nMIIEXAMPLE==\n-----END RSA PRIVATE KEY-----"
        findings = detect_secrets(content)
        pems = [f for f in findings if f.pattern_name == "pem-private-key"]
        assert len(pems) == 1
        assert pems[0].matched_value == "-----BEGIN RSA PRIVATE KEY-----"

    def test_positive_ec(self) -> None:
        content = "-----BEGIN EC PRIVATE KEY-----\nEXAMPLE\n-----END EC PRIVATE KEY-----"
        pems = [f for f in detect_secrets(content) if f.pattern_name == "pem-private-key"]
        assert len(pems) == 1

    def test_positive_generic_private(self) -> None:
        content = "-----BEGIN PRIVATE KEY-----\nEXAMPLE\n-----END PRIVATE KEY-----"
        pems = [f for f in detect_secrets(content) if f.pattern_name == "pem-private-key"]
        assert len(pems) == 1

    def test_negative_public_key(self) -> None:
        # Public key headers must NOT match.
        content = "-----BEGIN PUBLIC KEY-----\nEXAMPLE\n-----END PUBLIC KEY-----"
        pems = [f for f in detect_secrets(content) if f.pattern_name == "pem-private-key"]
        assert pems == []


# ── Connection strings ────────────────────────────────────────────────────────


class TestConnectionString:
    @pytest.mark.parametrize(
        "scheme",
        ["postgres", "postgresql", "mysql", "mongodb", "mongodb+srv"],
    )
    def test_positive_all_schemes(self, scheme: str) -> None:
        content = f"url={scheme}://user:password@example.com/db"
        findings = detect_secrets(content)
        cs = [f for f in findings if f.pattern_name == "connection-string"]
        assert len(cs) == 1
        # The matched value should contain the credentials but NOT the host
        # (regex stops at @). Verify the host is not in the match.
        assert "example.com" not in cs[0].matched_value
        assert "user:password" in cs[0].matched_value

    def test_negative_no_password(self) -> None:
        # No credentials embedded — should NOT match.
        content = "postgres://example.com/db"
        cs = [f for f in detect_secrets(content) if f.pattern_name == "connection-string"]
        assert cs == []


# ── High-entropy spans ─────────────────────────────────────────────────────────


class TestHighEntropy:
    def test_positive_base64_key(self) -> None:
        # 48-char high-entropy base64 — well above threshold (4.8 bits/char).
        # Use a clearly fake, random-looking string.
        key = "qT7vW2pR9sZ4xK1nL8mJ3hF6yD5bC0aE9gH2tV4wQ7rS1oP3uI6k"
        content = f"api_key={key}"
        findings = detect_secrets(content)
        he = [f for f in findings if f.pattern_name == "high-entropy"]
        # Should detect at least one high-entropy span (the key itself).
        # Note: the 'api_key=' prefix has low entropy, so the span starts
        # at the key.
        assert len(he) >= 1
        assert key in he[0].matched_value

    def test_negative_normal_english(self) -> None:
        # A 200-word English paragraph must NOT trigger high-entropy.
        # Normal prose has entropy ~4.0-4.3 bits/char in the base64 span.
        content = (
            "The quick brown fox jumps over the lazy dog. "
            "This is a normal English sentence with regular words and "
            "standard punctuation. It should not trigger the high-entropy "
            "detector because the per-character entropy of natural language "
            "is well below the threshold we use for base64-like spans. "
            "Repeated words and common phrases keep the entropy low even "
            "when the text is long enough to meet the minimum length. "
        ) * 3
        he = [f for f in detect_secrets(content) if f.pattern_name == "high-entropy"]
        assert he == [], f"False positive on English prose: {he}"

    def test_negative_short_identifier(self) -> None:
        # A 20-char identifier is below the 32-char minimum.
        content = "someVariableName"
        he = [f for f in detect_secrets(content) if f.pattern_name == "high-entropy"]
        assert he == []


# ── redact_content ───────────────────────────────────────────────────────────


class TestRedactContent:
    def test_replaces_match(self) -> None:
        token = "AKIA" + "T" * 16  # 20 chars
        content = f"key={token} done"
        findings = detect_secrets(content)
        redacted = redact_content(content, findings)
        assert "<REDACTED:aws-key>" in redacted
        assert token not in redacted
        # Surrounding text preserved.
        assert "key=" in redacted
        assert " done" in redacted

    def test_multiple_findings(self) -> None:
        aws = "AKIA" + "T" * 16  # 20 chars
        gh = "ghp_" + "T" * 36  # 40 chars
        content = f"a={aws} b={gh}"
        findings = detect_secrets(content)
        redacted = redact_content(content, findings)
        assert "<REDACTED:aws-key>" in redacted
        assert "<REDACTED:github-token>" in redacted
        assert aws not in redacted
        assert gh not in redacted

    def test_empty_findings_returns_unchanged(self) -> None:
        content = "nothing here"
        assert redact_content(content, []) == content

    def test_overlapping_findings_raise(self) -> None:
        # Build overlapping findings by hand to verify the guard.
        f1 = SecretFinding(pattern_name="a", matched_value="x", start=0, end=5)
        f2 = SecretFinding(pattern_name="b", matched_value="y", start=3, end=8)
        with pytest.raises(ValueError, match="overlapping"):
            redact_content("xxxxxxxxxx", [f1, f2])


# ── Multiple findings in one content ──────────────────────────────────────────


class TestMultipleFindings:
    def test_three_distinct_patterns(self) -> None:
        aws = "AKIA" + "T" * 16  # 20 chars
        gh = "ghp_" + "T" * 36  # 40 chars
        slack = "xoxb-" + "T" * 24
        content = f"aws={aws} gh={gh} slack={slack}"
        findings = detect_secrets(content)
        names = {f.pattern_name for f in findings}
        assert "aws-key" in names
        assert "github-token" in names
        assert "slack-token" in names

    def test_findings_sorted_by_start(self) -> None:
        gh = "ghp_" + "T" * 36  # 40 chars
        aws = "AKIA" + "T" * 16  # 20 chars
        content = f"gh={gh} aws={aws}"
        findings = detect_secrets(content)
        # GitHub token appears first in the text, so it has the lower start.
        assert len(findings) >= 2
        assert findings[0].start < findings[1].start


# ── Empty / None-safe ─────────────────────────────────────────────────────────


class TestEmptyAndNone:
    def test_empty_string(self) -> None:
        assert detect_secrets("") == []

    def test_none_input_returns_empty(self) -> None:
        # detect_secrets handles None defensively (the guard is
        # `if not content` which is True for None).
        assert detect_secrets(None) == []  # type: ignore[arg-type]

    def test_redact_empty_findings_on_empty(self) -> None:
        assert redact_content("", []) == ""


# ── Overlapping patterns ──────────────────────────────────────────────────────


class TestOverlapResolution:
    def test_jwt_containing_aws_like_substring(self) -> None:
        # A JWT starts with eyJ and is accepted. If an AWS-key-like
        # substring falls INSIDE the JWT span, it is dropped (first-match
        # precedence: JWT starts earlier and is accepted; the contained
        # AWS match is dropped).
        jwt = "eyJTESTTESTTEST.eyJAKIATESTTESTTESTTESTTT.TEST"
        content = f"tok={jwt}"
        findings = detect_secrets(content)
        names = [f.pattern_name for f in findings]
        # JWT should win; AWS-key (if any) should be dropped.
        assert "jwt" in names
        # No aws-key finding should survive — it's inside the JWT.
        assert "aws-key" not in names


# ── findings_by_pattern (log-safe counts) ─────────────────────────────────────


class TestFindingsByPattern:
    def test_counts(self) -> None:
        token1 = "AKIA" + "T" * 16
        token2 = "AKIA" + "Z" * 16
        content = f"a={token1} b={token2}"
        findings = detect_secrets(content)
        counts = findings_by_pattern(findings)
        assert counts.get("aws-key", 0) == 2

    def test_empty(self) -> None:
        assert findings_by_pattern([]) == {}
