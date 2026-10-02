"""Unit tests for the moderation pipeline (federation Phase 0, issue #85).

Covers the moderation pipeline (ArchCom 2026-07-17 federation contract
§2.2): secrets detector reuse, PII scrubber, neutral-value replacement,
mapping table TTL, verdict logic (allow / redact / refuse).

All secret/PII fixtures use RFC-reserved values (per
``sensitive-data.instructions.md``): 192.0.2.0/24 (RFC 5737),
user@example.com (RFC 5322), example.invalid (RFC 6761). No real
credentials appear anywhere in this file.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vesma.models import NO_FEDERATE_TAG
from vesma.moderation import (
    DOC_IPV4_BLOCK,
    PII_TYPES,
    MappingTable,
    ModerationVerdict,
    anonymize_pii,
    moderate,
    neutral_value_for,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

# Obviously-fake AWS key (AKIA + 16 uppercase). Never a real credential.
FAKE_AWS_KEY = "AKIA" + "T" * 16
# Obviously-fake GitHub token (ghp_ + 36 alnum).
FAKE_GITHUB_TOKEN = "ghp_" + "a" * 36


# ── allow: clean content ───────────────────────────────────────────────────────


class TestModerationAllow:
    def test_allow_clean_content(self) -> None:
        """No secrets, no PII → verdict=allow, content unchanged."""
        content = "This is a clean note about the project architecture."
        result = moderate(content)
        assert result.verdict == ModerationVerdict.ALLOW
        assert result.sanitized_content == content
        assert result.stats["secrets_redacted"] == 0
        assert result.stats["pii_anonymized"] == 0

    def test_empty_content(self) -> None:
        """Empty string → verdict=allow, empty result."""
        result = moderate("")
        assert result.verdict == ModerationVerdict.ALLOW
        assert result.sanitized_content == ""
        assert result.stats["secrets_redacted"] == 0
        assert result.stats["pii_anonymized"] == 0

    def test_none_tags_treated_as_empty(self) -> None:
        """tags=None → treated as empty list, no refuse."""
        result = moderate("clean content", tags=None)
        assert result.verdict == ModerationVerdict.ALLOW


# ── redact: secrets ───────────────────────────────────────────────────────────


class TestModerationRedactSecrets:
    def test_redact_secret(self) -> None:
        """Content with AWS key → verdict=redact, <REDACTED:aws-key> in result."""
        content = f"Use the key {FAKE_AWS_KEY} for production access."
        result = moderate(content)
        assert result.verdict == ModerationVerdict.REDACT
        assert "<REDACTED:aws-key>" in result.sanitized_content
        assert FAKE_AWS_KEY not in result.sanitized_content
        assert result.stats["secrets_redacted"] >= 1
        assert result.stats["pii_anonymized"] == 0

    def test_redact_github_token(self) -> None:
        """Content with GitHub token → verdict=redact."""
        content = f"token: {FAKE_GITHUB_TOKEN} end"
        result = moderate(content)
        assert result.verdict == ModerationVerdict.REDACT
        assert "<REDACTED:github-token>" in result.sanitized_content
        assert FAKE_GITHUB_TOKEN not in result.sanitized_content


# ── redact: PII ───────────────────────────────────────────────────────────────


class TestModerationRedactPII:
    def test_redact_pii_email(self) -> None:
        """Content with email → verdict=redact, user@example.com replaces original."""
        content = "Contact alice@corp.example.com for details."
        result = moderate(content)
        assert result.verdict == ModerationVerdict.REDACT
        assert "user@example.com" in result.sanitized_content
        assert "alice@corp.example.com" not in result.sanitized_content
        assert result.stats["pii_anonymized"] >= 1
        assert result.stats["secrets_redacted"] == 0

    def test_redact_pii_ipv4(self) -> None:
        """Content with 10.0.0.5 → 192.0.2.1."""
        content = "The server is at 10.0.0.5 on the internal network."
        result = moderate(content)
        assert result.verdict == ModerationVerdict.REDACT
        assert "192.0.2.1" in result.sanitized_content
        assert "10.0.0.5" not in result.sanitized_content
        assert result.stats["pii_anonymized"] >= 1

    def test_redact_pii_ipv6(self) -> None:
        """Content with IPv6 → 2001:db8::1."""
        content = "Node address fe80::1abc is reachable via mesh."
        result = moderate(content)
        assert result.verdict == ModerationVerdict.REDACT
        assert "2001:db8::1" in result.sanitized_content
        assert "fe80::1abc" not in result.sanitized_content

    def test_redact_pii_hostname(self) -> None:
        """Content with internal.corp.local → example.invalid."""
        content = "Deploy target is internal.corp.local in region eu-west-2."
        result = moderate(content)
        assert result.verdict == ModerationVerdict.REDACT
        assert "example.invalid" in result.sanitized_content
        assert "internal.corp.local" not in result.sanitized_content

    def test_redact_pii_filepath(self) -> None:
        """Content with /home/user/secret.txt → /example/path/to/file."""
        content = "Read the config from /home/user/secret.txt before deploy."
        result = moderate(content)
        assert result.verdict == ModerationVerdict.REDACT
        assert "/example/path/to/file" in result.sanitized_content
        assert "/home/user/secret.txt" not in result.sanitized_content

    def test_redact_pii_windows_path(self) -> None:
        """Windows drive-letter path C:\\Users\\admin\\secret.txt → neutral.

        Enterprise content with Windows paths must be redacted — without
        this pattern they leak unredacted (P1-1 fix).
        """
        content = r"Open C:\Users\admin\secret.txt to read the credentials."
        result = moderate(content)
        assert result.verdict == ModerationVerdict.REDACT
        assert r"C:\example\path\to\file" in result.sanitized_content
        assert r"C:\Users\admin\secret.txt" not in result.sanitized_content
        assert result.stats["pii_anonymized"] >= 1

    def test_redact_pii_unc_path(self) -> None:
        """UNC path \\\\server\\share\\file → neutral UNC.

        UNC/SMB share paths common in enterprise Windows environments
        must be redacted (P1-1 fix).
        """
        content = r"Mount \\server\share\file to access the dataset."
        result = moderate(content)
        assert result.verdict == ModerationVerdict.REDACT
        assert r"\\example.invalid\share\file" in result.sanitized_content
        assert r"\\server\share\file" not in result.sanitized_content
        assert result.stats["pii_anonymized"] >= 1


# ── refuse ─────────────────────────────────────────────────────────────────────


class TestModerationRefuse:
    def test_refuse_no_federate_tag(self) -> None:
        """tags include mnemos:no-federate → verdict=refuse."""
        content = "This is a clean note but the owner opted out."
        result = moderate(content, tags=[NO_FEDERATE_TAG])
        assert result.verdict == ModerationVerdict.REFUSE
        assert result.sanitized_content == ""
        assert result.stats["secrets_redacted"] == 0
        assert result.stats["pii_anonymized"] == 0

    def test_refuse_all_secret(self) -> None:
        """Content is entirely a secret (just AKIATEST...) → verdict=refuse (>80% redacted)."""
        content = FAKE_AWS_KEY
        result = moderate(content)
        assert result.verdict == ModerationVerdict.REFUSE
        assert result.sanitized_content == ""
        assert result.stats["secrets_redacted"] >= 1

    def test_refuse_high_redaction_ratio(self) -> None:
        """Content where >80% gets redacted → refuse."""
        # FAKE_AWS_KEY is 20 chars. With 3 chars of prose ("x " prefix),
        # the redacted fraction is 20/22 ≈ 91% > 80% → refuse.
        content = "x " + FAKE_AWS_KEY
        result = moderate(content)
        assert result.verdict == ModerationVerdict.REFUSE
        assert result.sanitized_content == ""

    def test_refuse_threshold_respected(self) -> None:
        """Custom refuse_threshold=0.5 → borderline content refuses earlier."""
        # 20-char secret + 30 chars prose = 40% redaction. With threshold 0.5,
        # this should still REDACT (40% < 50%). With threshold 0.3, REFUSE.
        content = f"Use key {FAKE_AWS_KEY} for the prod deploy now please."
        result_default = moderate(content)
        assert result_default.verdict == ModerationVerdict.REDACT
        result_strict = moderate(content, refuse_threshold=0.3)
        assert result_strict.verdict == ModerationVerdict.REFUSE


# ── stats ─────────────────────────────────────────────────────────────────────


class TestModerationStats:
    def test_stats_counters(self) -> None:
        """stats has secrets_redacted, pii_anonymized counts, no raw values."""
        content = f"Key {FAKE_AWS_KEY} and email alice@corp.example.com in one record."
        result = moderate(content)
        assert result.verdict == ModerationVerdict.REDACT
        assert isinstance(result.stats["secrets_redacted"], int)
        assert isinstance(result.stats["pii_anonymized"], int)
        assert result.stats["secrets_redacted"] >= 1
        assert result.stats["pii_anonymized"] >= 1
        # No raw secret/PII values leak into stats.
        stats_str = str(result.stats)
        assert FAKE_AWS_KEY not in stats_str
        assert "alice@corp.example.com" not in stats_str

    def test_stats_verdict_code(self) -> None:
        """stats has verdict_code (int) for log/metric pipelines."""
        result_allow = moderate("clean content")
        assert result_allow.stats["verdict_code"] == 0
        result_redact = moderate(f"key {FAKE_AWS_KEY} end")
        assert result_redact.stats["verdict_code"] == 1
        result_refuse = moderate("clean", tags=[NO_FEDERATE_TAG])
        assert result_refuse.stats["verdict_code"] == 2

    def test_result_no_mapping_field(self) -> None:
        """ModerationResult must NOT carry the mapping table (leak surface)."""
        result = moderate(f"email alice@corp.example.com and key {FAKE_AWS_KEY}")
        # The dataclass should not have a `mapping` attribute.
        assert not hasattr(result, "mapping") or result.__class__.__name__ == "ModerationResult"
        # Verify via fields: slots dataclass — mapping is not a declared field.
        from dataclasses import fields

        field_names = {f.name for f in fields(result)}
        assert "mapping" not in field_names


# ── combined ──────────────────────────────────────────────────────────────────


class TestModerationCombined:
    def test_combined_secret_and_pii(self) -> None:
        """Content with both secret and email → both redacted, verdict=redact."""
        content = f"Admin key {FAKE_AWS_KEY} contacted at alice@corp.example.com."
        result = moderate(content)
        assert result.verdict == ModerationVerdict.REDACT
        assert "<REDACTED:aws-key>" in result.sanitized_content
        assert "user@example.com" in result.sanitized_content
        assert FAKE_AWS_KEY not in result.sanitized_content
        assert "alice@corp.example.com" not in result.sanitized_content
        assert result.stats["secrets_redacted"] >= 1
        assert result.stats["pii_anonymized"] >= 1

    def test_multiple_pii_types(self) -> None:
        """Multiple PII types in one record → all anonymized."""
        content = "Server 10.0.0.5 hosts db.internal.corp.local and user alice@corp.example.com."
        result = moderate(content)
        assert result.verdict == ModerationVerdict.REDACT
        assert "192.0.2.1" in result.sanitized_content
        assert "example.invalid" in result.sanitized_content
        assert "user@example.com" in result.sanitized_content
        assert result.stats["pii_anonymized"] >= 3


# ── idempotency: RFC-reserved values not re-redacted ──────────────────────────


class TestModerationIdempotency:
    def test_rfc_reserved_not_redacted(self) -> None:
        """Neutral values (incl. Windows/UNC paths) → NOT re-redacted."""
        content = (
            "Use 192.0.2.1 as the doc IP, email user@example.com for help, "
            "hostname example.invalid for tests, unix path /example/path/to/file, "
            r"windows path C:\example\path\to\file, "
            r"unc path \\example.invalid\share\file."
        )
        result = moderate(content)
        # These are already neutral — should pass through unchanged.
        assert result.verdict == ModerationVerdict.ALLOW
        assert "192.0.2.1" in result.sanitized_content
        assert "user@example.com" in result.sanitized_content
        assert "example.invalid" in result.sanitized_content
        assert "/example/path/to/file" in result.sanitized_content
        assert r"C:\example\path\to\file" in result.sanitized_content
        assert r"\\example.invalid\share\file" in result.sanitized_content
        assert result.stats["pii_anonymized"] == 0

    def test_already_sanitized_idempotent(self) -> None:
        """Running moderate() on already-sanitized content → allow, unchanged."""
        original = "Contact alice@corp.example.com for access."
        first = moderate(original)
        assert first.verdict == ModerationVerdict.REDACT
        second = moderate(first.sanitized_content)
        assert second.verdict == ModerationVerdict.ALLOW
        assert second.sanitized_content == first.sanitized_content


# ── PII scrubber unit tests ────────────────────────────────────────────────────


class TestAnonymizePii:
    def test_no_pii_returns_unchanged(self) -> None:
        content = "no pii here at all"
        mapping = MappingTable()
        result, count, per_type, chars = anonymize_pii(content, mapping=mapping)
        assert result == content
        assert count == 0
        assert per_type == {}
        assert chars == 0

    def test_email_anonymized(self) -> None:
        content = "reach bob@shop.example.org"
        mapping = MappingTable()
        result, count, per_type, chars = anonymize_pii(content, mapping=mapping)
        assert "user@example.com" in result
        assert "bob@shop.example.org" not in result
        assert count == 1
        assert per_type.get("email") == 1
        assert chars == len("bob@shop.example.org")


# ── MappingTable TTL ──────────────────────────────────────────────────────────


class TestMappingTable:
    def test_get_or_create_returns_neutral_value(self) -> None:
        mapping = MappingTable(ttl_hours=24)
        repl = mapping.get_or_create("email", "alice@corp.example.com")
        assert repl == "user@example.com"

    def test_same_original_returns_same_replacement(self) -> None:
        mapping = MappingTable()
        r1 = mapping.get_or_create("ipv4", "10.0.0.5")
        r2 = mapping.get_or_create("ipv4", "10.0.0.5")
        assert r1 == r2 == "192.0.2.1"

    def test_expired_entry_evicted(self) -> None:
        """Expired entries are evicted by evict_expired(); live ones survive.

        P1-2 fix: the previous test created two separate MappingTable
        instances and asserted evicted == 0 both times — it never
        advanced time on the same table, so TTL eviction was never
        tested. This rewrite uses ONE table, advances time past TTL,
        and asserts the expired entry is actually evicted.
        """
        start = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
        mapping = MappingTable(ttl_hours=24, now=start)
        mapping.get_or_create("email", "alice@corp.example.com")
        assert len(mapping) == 1
        # Within TTL → entry survives, no eviction.
        mapping._now = start + timedelta(hours=12)
        assert mapping.evict_expired() == 0
        assert len(mapping) == 1
        # Advance past TTL (24h) → entry is expired and evicted.
        mapping._now = start + timedelta(hours=25)
        evicted = mapping.evict_expired()
        assert evicted == 1
        assert len(mapping) == 0

    def test_unknown_type_raises(self) -> None:
        mapping = MappingTable()
        with pytest.raises(KeyError):
            mapping.get_or_create("unknown-type", "value")


# ── neutral_value_for ─────────────────────────────────────────────────────────


class TestNeutralValueFor:
    def test_returns_rfc_reserved_values(self) -> None:
        assert neutral_value_for("email") == "user@example.com"
        assert neutral_value_for("ipv4") == "192.0.2.1"
        assert neutral_value_for("ipv6") == "2001:db8::1"
        assert neutral_value_for("hostname") == "example.invalid"
        assert neutral_value_for("unix-path") == "/example/path/to/file"
        assert neutral_value_for("win-path") == r"C:\example\path\to\file"
        assert neutral_value_for("unc-path") == r"\\example.invalid\share\file"

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(KeyError):
            neutral_value_for("unknown")


# ── PII_TYPES introspection ───────────────────────────────────────────────────


class TestPIITypes:
    def test_pii_types_complete(self) -> None:
        assert set(PII_TYPES) == {
            "email",
            "ipv4",
            "ipv6",
            "hostname",
            "unix-path",
            "win-path",
            "unc-path",
        }

    def test_doc_ipv4_block_constant(self) -> None:
        assert DOC_IPV4_BLOCK == "192.0.2.0/24"
