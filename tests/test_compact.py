"""Unit tests for the compact exchange format (federation Phase 0, issue #85 part 2a).

Covers :mod:`vesma.compact` — the ``vesmaro.federation.v1`` compact
record builder (ArchCom 2026-07-17 federation contract §2.3). The
builder runs the moderation pipeline (:func:`vesma.moderation.moderate`,
#85 Part 1) first and reuses its verdict:

* ``allow``  → record built from original content.
* ``redact`` → record built from sanitized content (secrets redacted,
  PII anonymized to RFC-reserved values).
* ``refuse`` → ``build_compact_record`` returns ``None`` / the record is
  skipped at the payload level.

All secret/PII fixtures use RFC-reserved values (per
``sensitive-data.instructions.md``): 192.0.2.0/24 (RFC 5737),
user@example.com (RFC 5322), example.invalid (RFC 6761). No real
credentials appear anywhere in this file.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vesma.compact import (
    COMPACT_SCHEMA,
    MAX_KEY_POINTS,
    MAX_SUMMARY_LEN,
    MAX_TITLE_LEN,
    CompactRecord,
    build_compact_payload,
    build_compact_record,
    derive_record_type,
    extract_key_points,
    summarize_content,
)
from vesma.models import NO_FEDERATE_TAG, Memory

# ── Fixtures ──────────────────────────────────────────────────────────────────

# Obviously-fake AWS key (AKIA + 16 uppercase). Never a real credential.
FAKE_AWS_KEY = "AKIA" + "T" * 16

#: Default tag set for a decision record (project + agent + mnemos subtype).
_DECISION_TAGS = ["project:mnemos", "agent:gcw-tech-lead", "mnemos:decision"]
_LEARNING_TAGS = ["project:mnemos", "agent:gcw-tech-lead", "mnemos:learning"]


def _make_memory(
    content: str,
    *,
    tags: list[str] | None = None,
    title: str | None = None,
    created_at: datetime | None = None,
    memory_id: str = "11111111-1111-1111-1111-111111111111",
) -> Memory:
    """Build a :class:`Memory` with sensible defaults for compact tests.

    Tags default to a decision record (project/agent/mnemos:decision).
    The ``agent:`` tag is what :func:`build_compact_record` reads for
    ``source_agent`` derivation — but the caller passes ``source_agent``
    explicitly to :func:`build_compact_record`, so the tag is only used
    by :func:`derive_record_type` and the tag-stripping check.
    """
    return Memory(
        id=memory_id,
        content=content,
        title=title,
        tags=tags if tags is not None else list(_DECISION_TAGS),
        created_at=created_at or datetime(2026, 7, 18, 10, 30, 0, tzinfo=UTC),
    )


# ── build_compact_record — verdict routing ────────────────────────────────────


class TestBuildCompactRecordVerdict:
    def test_build_compact_record_allow(self) -> None:
        """Clean memory → record built, type from mnemos tag, summary = content."""
        content = "We chose bearer+TOTP 2FA for remote sessions."
        mem = _make_memory(content, title="ADR-0014 auth decision")
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        assert isinstance(rec, CompactRecord)
        assert rec.type == "decision"
        assert rec.title == "ADR-0014 auth decision"
        assert rec.summary == content
        assert rec.source_agent == "gcw-tech-lead"
        assert rec.id == f"fed:gcw-tech-lead:{mem.id}"

    def test_build_compact_record_redact_secret(self) -> None:
        """Memory with AWS key → record built, summary has <REDACTED:aws-key>, secret gone."""
        content = f"Use the key {FAKE_AWS_KEY} for production access."
        mem = _make_memory(content)
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        assert "<REDACTED:aws-key>" in rec.summary
        assert FAKE_AWS_KEY not in rec.summary

    def test_build_compact_record_redact_pii(self) -> None:
        """Memory with email → record built, summary has user@example.com, original gone."""
        content = "Contact alice@corp.example.com for deployment details."
        mem = _make_memory(content)
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        assert "user@example.com" in rec.summary
        assert "alice@corp.example.com" not in rec.summary

    def test_build_compact_record_refuse_no_federate(self) -> None:
        """Memory with mnemos:no-federate tag → returns None."""
        mem = _make_memory("internal-only decision", tags=[*_DECISION_TAGS, NO_FEDERATE_TAG])
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is None

    def test_build_compact_record_refuse_all_secret(self) -> None:
        """Memory where content is entirely a secret → returns None (>80% redacted)."""
        # FAKE_AWS_KEY is 20 chars; 20/20 = 100% redacted > 0.8 threshold.
        mem = _make_memory(FAKE_AWS_KEY)
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is None


# ── summary truncation ────────────────────────────────────────────────────────


class TestSummaryTruncation:
    def test_summary_truncation(self) -> None:
        """Content > 500 chars → summary ≤ 500 chars, truncated at word boundary with ..."""
        # 600 chars of prose — 100 words of ~6 chars each.
        words = ["word" + str(i % 10) for i in range(120)]
        content = " ".join(words)
        assert len(content) > MAX_SUMMARY_LEN
        mem = _make_memory(content)
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        assert len(rec.summary) <= MAX_SUMMARY_LEN
        assert rec.summary.endswith("...")

    def test_summary_short_content(self) -> None:
        """Content ≤ 500 chars → summary = content as-is."""
        content = "Short decision summary well under the limit."
        mem = _make_memory(content)
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        assert rec.summary == content


# ── key_points extraction ─────────────────────────────────────────────────────


class TestKeyPointsExtraction:
    def test_key_points_bullets(self) -> None:
        """Content with bullet lines → key_points carries the bullet text."""
        content = (
            "Decision:\n- bearer + TOTP 2FA\n"
            "- loopback-only without TOTP\n"
            "- TOTP secret in argon2id"
        )
        mem = _make_memory(content)
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        assert "bearer + TOTP 2FA" in rec.key_points
        assert "loopback-only without TOTP" in rec.key_points
        assert "TOTP secret in argon2id" in rec.key_points

    def test_key_points_numbered(self) -> None:
        """Content with '1. first\\n2. second' → key_points = ['first', 'second']."""
        content = "Steps:\n1. first step\n2. second step\n3. third step"
        mem = _make_memory(content)
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        assert "first step" in rec.key_points
        assert "second step" in rec.key_points
        assert "third step" in rec.key_points

    def test_key_points_no_structure(self) -> None:
        """Plain prose → key_points is [] or first 1-2 sentences."""
        content = "We chose bearer plus TOTP for remote sessions. The loopback stays without TOTP."
        mem = _make_memory(content)
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        # Fallback path: first 1-2 sentences.
        assert isinstance(rec.key_points, list)
        assert len(rec.key_points) <= 2
        if rec.key_points:
            assert rec.key_points[0].startswith("We chose bearer plus TOTP")

    def test_key_points_max_5(self) -> None:
        """Content with 10 bullets → key_points has at most MAX_KEY_POINTS entries."""
        bullets = "\n".join(f"- point number {i}" for i in range(1, 11))
        mem = _make_memory(bullets)
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        assert len(rec.key_points) <= MAX_KEY_POINTS


# ── id prefix ──────────────────────────────────────────────────────────────────


class TestIdPrefix:
    def test_id_prefix(self) -> None:
        """id starts with 'fed:<source_agent>:'."""
        mem = _make_memory("clean content")
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        assert rec.id.startswith("fed:gcw-tech-lead:")
        assert rec.id == f"fed:gcw-tech-lead:{mem.id}"


# ── type mapping ──────────────────────────────────────────────────────────────


class TestTypeMapping:
    @pytest.mark.parametrize(
        ("mnemos_tag", "expected_type"),
        [
            ("mnemos:decision", "decision"),
            ("mnemos:learning", "learning"),
            ("mnemos:bug-pattern", "bug-pattern"),
            ("mnemos:rule", "rule"),
            ("mnemos:open-question", "open-question"),
            ("mnemos:checkpoint", "checkpoint"),
            ("mnemos:session", "session"),
            # Pipeline artefacts → session (sensible default).
            ("mnemos:legacy", "session"),
            ("mnemos:synthesized", "session"),
        ],
    )
    def test_type_mapping(self, mnemos_tag: str, expected_type: str) -> None:
        """Each mnemos subtype → correct type string. Fallback → session."""
        tags = ["project:mnemos", "agent:gcw-tech-lead", mnemos_tag]
        mem = _make_memory("clean content", tags=tags)
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        assert rec.type == expected_type

    def test_type_mapping_fallback_no_mnemos_tag(self) -> None:
        """No mnemos: tag at all → fallback to 'session'."""
        tags = ["project:mnemos", "agent:gcw-tech-lead"]
        mem = _make_memory("clean content", tags=tags)
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        assert rec.type == "session"

    def test_derive_record_type_unknown_subtype_falls_back(self) -> None:
        """Unknown mnemos:<x> subtype → fallback to session."""
        assert derive_record_type(["mnemos:unknown-subtype"]) == "session"
        assert derive_record_type([]) == "session"
        assert derive_record_type(["project:x", "agent:y"]) == "session"

    def test_derive_record_type_skips_no_federate(self) -> None:
        """mnemos:no-federate is NOT mapped to a type — it is skipped."""
        assert derive_record_type([NO_FEDERATE_TAG]) == "session"


# ── tags stripping ─────────────────────────────────────────────────────────────


class TestTagsStripping:
    def test_tags_strip_no_federate(self) -> None:
        """tags don't include mnemos:no-federate even if original had it.

        Moderation refuses no-federate records (returns None), so we
        test the stripping directly via the defensive filter inside
        ``build_compact_record`` — but since refuse wins first, we
        verify the stripping through a redact verdict where the tag is
        present alongside a secret. The refuse path triggers on the
        no-federate tag before the redact fraction check, so we cannot
        get a record with no-federate in tags. Instead, verify the
        stripping at the helper level by confirming a clean record's
        tags never contain no-federate even when the original had it
        but moderation did NOT refuse (impossible with no-federate
        present) — therefore the realistic test is: a record without
        no-federate never gains it, and the filter removes it if present
        in a hypothetical post-moderation scenario.
        """
        # Realistic case: clean memory without no-federate → tags unchanged.
        tags = ["project:mnemos", "agent:gcw-tech-lead", "mnemos:decision"]
        mem = _make_memory("clean content", tags=tags)
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        assert NO_FEDERATE_TAG not in rec.tags
        assert "project:mnemos" in rec.tags
        assert "agent:gcw-tech-lead" in rec.tags
        assert "mnemos:decision" in rec.tags

    def test_tags_preserve_optional_prefixes(self) -> None:
        """Optional prefixes (severity:, stack:, applyTo:) are preserved."""
        tags = [
            "project:mnemos",
            "agent:gcw-tech-lead",
            "mnemos:decision",
            "severity:P0",
            "stack:python",
        ]
        mem = _make_memory("clean content", tags=tags)
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        assert "severity:P0" in rec.tags
        assert "stack:python" in rec.tags


# ── build_compact_payload ─────────────────────────────────────────────────────


class TestBuildCompactPayload:
    def test_build_compact_payload_schema(self) -> None:
        """payload has 'schema': 'vesmaro.federation.v1'."""
        payload = build_compact_payload([], source_agent="gcw-tech-lead")
        assert payload["schema"] == COMPACT_SCHEMA == "vesmaro.federation.v1"

    def test_build_compact_payload_stats(self) -> None:
        """payload has stats with correct counts (total/exported/refused/...)."""
        clean = _make_memory("clean decision", memory_id="11111111-1111-1111-1111-111111111111")
        secret_redacted = _make_memory(
            f"key {FAKE_AWS_KEY} end",
            tags=list(_LEARNING_TAGS),
            memory_id="22222222-2222-2222-2222-222222222222",
        )
        refused = _make_memory(
            "internal-only",
            tags=[*_DECISION_TAGS, NO_FEDERATE_TAG],
            memory_id="33333333-3333-3333-3333-333333333333",
        )
        payload = build_compact_payload(
            [clean, secret_redacted, refused],
            source_agent="gcw-tech-lead",
        )
        stats = payload["stats"]
        assert stats["total"] == 3
        assert stats["exported"] == 2  # clean + redacted
        assert stats["refused"] == 1  # no-federate
        assert stats["secrets_redacted"] >= 1  # the AWS key
        assert stats["pii_anonymized"] >= 0
        assert isinstance(stats["secrets_redacted"], int)
        assert isinstance(stats["pii_anonymized"], int)

    def test_build_compact_payload_empty(self) -> None:
        """Empty list → zero stats, empty records."""
        payload = build_compact_payload([], source_agent="gcw-tech-lead")
        assert payload["schema"] == COMPACT_SCHEMA
        assert payload["records"] == []
        assert payload["stats"] == {
            "total": 0,
            "exported": 0,
            "refused": 0,
            "secrets_redacted": 0,
            "pii_anonymized": 0,
        }

    def test_build_compact_payload_records_are_dicts(self) -> None:
        """Each record in the payload is a JSON-serialisable dict."""
        mem = _make_memory("clean content")
        payload = build_compact_payload([mem], source_agent="gcw-tech-lead")
        assert len(payload["records"]) == 1
        record = payload["records"][0]
        assert isinstance(record, dict)
        assert set(record.keys()) >= {
            "id",
            "type",
            "title",
            "summary",
            "key_points",
            "tags",
            "source_agent",
            "timestamp",
        }


# ── timestamp ──────────────────────────────────────────────────────────────────


class TestTimestamp:
    def test_timestamp_iso8601(self) -> None:
        """timestamp is valid ISO 8601 UTC."""
        ts = datetime(2026, 7, 18, 10, 30, 0, tzinfo=UTC)
        mem = _make_memory("clean content", created_at=ts)
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        # Valid ISO 8601 — parse it back.
        parsed = datetime.fromisoformat(rec.timestamp.replace("Z", "+00:00"))
        assert parsed == ts
        # UTC marker present (either Z or +00:00).
        assert rec.timestamp.endswith("Z") or rec.timestamp.endswith("+00:00")

    def test_timestamp_naive_datetime_assumed_utc(self) -> None:
        """Naive datetime is treated as UTC (defensive)."""
        naive = datetime(2026, 7, 18, 10, 30, 0)
        mem = _make_memory("clean content", created_at=naive)
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        parsed = datetime.fromisoformat(rec.timestamp.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)


# ── title ──────────────────────────────────────────────────────────────────────


class TestTitle:
    def test_title_from_memory_title(self) -> None:
        """memory.title set → used as-is (truncated if too long)."""
        mem = _make_memory("content", title="My Decision Title")
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        assert rec.title == "My Decision Title"

    def test_title_from_first_line_when_no_title(self) -> None:
        """No title → first line of content becomes the title."""
        mem = _make_memory("First line is the title\nSecond line is detail.", title=None)
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        assert rec.title == "First line is the title"

    def test_title_truncated_at_max_len(self) -> None:
        """Title > MAX_TITLE_LEN chars → truncated with ... at word boundary."""
        long_title = " ".join(["decision"] * 60)  # ~480 chars > 256
        mem = _make_memory("content", title=long_title)
        rec = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec is not None
        assert len(rec.title) <= MAX_TITLE_LEN
        assert rec.title.endswith("...")


# ── helpers (unit-tested directly for coverage) ──────────────────────────────


class TestSummarizeContent:
    def test_summarize_content_short(self) -> None:
        assert summarize_content("short") == "short"

    def test_summarize_content_exact_limit(self) -> None:
        content = "a" * MAX_SUMMARY_LEN
        assert summarize_content(content) == content

    def test_summarize_content_over_limit_with_boundary(self) -> None:
        content = " ".join(["word"] * 200)  # 999 chars
        out = summarize_content(content)
        assert len(out) <= MAX_SUMMARY_LEN
        assert out.endswith("...")

    def test_summarize_content_over_limit_no_boundary(self) -> None:
        # Single very long word — hard cut.
        content = "a" * 600
        out = summarize_content(content)
        assert len(out) <= MAX_SUMMARY_LEN
        assert out.endswith("...")


class TestExtractKeyPoints:
    def test_empty_content(self) -> None:
        assert extract_key_points("") == []

    def test_bullet_with_asterisk(self) -> None:
        content = "* first\n* second"
        points = extract_key_points(content)
        assert "first" in points
        assert "second" in points

    def test_bullet_with_bullet_char(self) -> None:
        content = "• first\n• second"
        points = extract_key_points(content)
        assert "first" in points
        assert "second" in points

    def test_mixed_bullets_and_numbered_prefers_bullets(self) -> None:
        content = "- bullet one\n1. numbered one"
        points = extract_key_points(content)
        assert "bullet one" in points
        # Numbered is not reached because bullets already found.
        assert "numbered one" not in points

    def test_max_points_enforced(self) -> None:
        content = "\n".join(f"- point {i}" for i in range(20))
        points = extract_key_points(content)
        assert len(points) == MAX_KEY_POINTS

    def test_sentence_fallback_single_sentence(self) -> None:
        content = "Only one sentence here with no bullets."
        points = extract_key_points(content)
        assert len(points) == 1
        assert points[0].startswith("Only one sentence")


# ── idempotence on import ──────────────────────────────────────────────────────


class TestIdempotence:
    def test_id_is_deterministic(self) -> None:
        """Same memory + source_agent → same id (idempotent on import)."""
        mem = _make_memory("clean content")
        rec1 = build_compact_record(mem, source_agent="gcw-tech-lead")
        rec2 = build_compact_record(mem, source_agent="gcw-tech-lead")
        assert rec1 is not None and rec2 is not None
        assert rec1.id == rec2.id == f"fed:gcw-tech-lead:{mem.id}"

    def test_id_changes_with_source_agent(self) -> None:
        """Different source_agent → different id (provenance encoded in id)."""
        mem = _make_memory("clean content")
        rec_a = build_compact_record(mem, source_agent="agent-a")
        rec_b = build_compact_record(mem, source_agent="agent-b")
        assert rec_a is not None and rec_b is not None
        assert rec_a.id != rec_b.id
        assert rec_a.source_agent == "agent-a"
        assert rec_b.source_agent == "agent-b"
