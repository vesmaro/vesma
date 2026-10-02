"""Tests for P1-5 — CacheAligner prefix stabilization.

Inspired by headroom's CacheAligner
(https://github.com/headroomlabs-ai/headroom, Apache 2.0). These tests
verify our original implementation: dynamic span extraction, prefix
stability, determinism, profile-aware behaviour, code identifiers not
mangled, and CCR markers staying atomic (mnemos #282 — protected from
extraction in every profile so the ``vesma_retrieve`` round-trip
survives alignment).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vesmaro.cache_aligner import align
from vesmaro.ccr import build_marker, parse_marker
from vesmaro.config import Settings
from vesmaro.manager import MemoryManager

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def manager(tmp_path: Path) -> MemoryManager:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            vesma={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
            },
        )
        settings.resolve_paths()
        mgr = MemoryManager(settings)
        yield mgr
        mgr.close()


# ── Timestamp extraction ──────────────────────────────────────────────────────


class TestTimestampExtraction:
    def test_iso_timestamp_with_z_relocated(self) -> None:
        text = "Session started at 2026-07-17T10:30:00Z and proceeded."
        result = align(text)
        assert result["prefix_stabilized"] is True
        assert result["moved_chars"] > 0
        # The timestamp must not appear in the aligned body (it's in the
        # dynamic block at the end).
        assert (
            "2026-07-17T10:30:00Z" not in result["aligned_text"].split("--- Dynamic context ---")[0]
        )
        # It must appear in the dynamic block.
        assert "2026-07-17T10:30:00Z" in result["aligned_text"]
        assert any(s["kind"] == "timestamp" for s in result["extracted"])

    def test_iso_timestamp_with_offset_relocated(self) -> None:
        text = "Event at 2026-07-17T10:30:00.123+02:00 noted."
        result = align(text)
        assert any(
            s["kind"] == "timestamp" and s["value"] == "2026-07-17T10:30:00.123+02:00"
            for s in result["extracted"]
        )

    def test_space_separated_timestamp_relocated(self) -> None:
        text = "Logged 2026-07-17 10:30:00 done."
        result = align(text)
        assert any(s["kind"] == "timestamp" for s in result["extracted"])


# ── UUID extraction ───────────────────────────────────────────────────────────


class TestUUIDExtraction:
    def test_canonical_uuid_relocated(self) -> None:
        uuid = "550e8400-e29b-41d4-a716-446655440000"
        text = f"Request id {uuid} failed."
        result = align(text)
        assert any(s["kind"] == "uuid" and s["value"] == uuid for s in result["extracted"])
        assert uuid not in result["aligned_text"].split("--- Dynamic context ---")[0]

    def test_uppercase_uuid_relocated(self) -> None:
        uuid = "550E8400-E29B-41D4-A716-446655440000"
        text = f"Request id {uuid} failed."
        result = align(text)
        assert any(s["kind"] == "uuid" for s in result["extracted"])


# ── Session ID extraction ──────────────────────────────────────────────────────


class TestSessionIDExtraction:
    def test_sess_prefix_relocated(self) -> None:
        text = "Running under session sess-abc123def456."
        result = align(text)
        assert any(
            s["kind"] == "session_id" and "sess-abc123def456" in s["value"]
            for s in result["extracted"]
        )

    def test_session_colon_prefix_relocated(self) -> None:
        text = "Context session:abc123def456 active."
        result = align(text)
        assert any(s["kind"] == "session_id" for s in result["extracted"])


# ── No-op on stable text ───────────────────────────────────────────────────────


class TestNoOpOnStableText:
    def test_plain_prose_unchanged(self) -> None:
        text = "The quick brown fox jumps over the lazy dog."
        result = align(text)
        assert result["aligned_text"] == text
        assert result["extracted"] == []
        assert result["prefix_stabilized"] is False
        assert result["moved_chars"] == 0

    def test_empty_text_no_op(self) -> None:
        result = align("")
        assert result == {
            "aligned_text": "",
            "extracted": [],
            "prefix_stabilized": False,
            "moved_chars": 0,
        }


# ── Prefix stability (determinism) ─────────────────────────────────────────────


class TestPrefixStability:
    def test_same_input_identical_output(self) -> None:
        text = "Context at 2026-07-17T10:00:00Z with sess-abc123 done."
        r1 = align(text)
        r2 = align(text)
        assert r1["aligned_text"] == r2["aligned_text"]
        assert r1["extracted"] == r2["extracted"]

    def test_prefix_stable_up_to_first_dynamic_span(self) -> None:
        # Two texts share a stable prefix up to the first dynamic span.
        # After alignment, the prefix (before the first dynamic span) must
        # be identical in both aligned outputs.
        text_a = "System prompt. Session sess-aaa111 at 2026-07-17T10:00:00Z."
        text_b = "System prompt. Session sess-bbb222 at 2026-07-17T11:00:00Z."
        ra = align(text_a)
        rb = align(text_b)
        # The aligned bodies (before the dynamic block) share the prefix
        # "System prompt. Session " — the dynamic span starts right after.
        body_a = ra["aligned_text"].split("--- Dynamic context ---")[0]
        body_b = rb["aligned_text"].split("--- Dynamic context ---")[0]
        # The stable prefix up to "Session " is identical.
        assert body_a.startswith("System prompt. Session ")
        assert body_b.startswith("System prompt. Session ")

    def test_prefix_stabilized_flag_true_when_prefix_exists(self) -> None:
        text = "Stable prefix here. Timestamp 2026-07-17T10:00:00Z."
        result = align(text)
        assert result["prefix_stabilized"] is True

    def test_prefix_stabilized_false_when_dynamic_at_start(self) -> None:
        text = "2026-07-17T10:00:00Z no prefix before timestamp."
        result = align(text)
        assert result["prefix_stabilized"] is False


# ── Code identifiers not mangled ───────────────────────────────────────────────


class TestCodeIdentifiersNotMangled:
    def test_short_hex_not_extracted_as_token(self) -> None:
        # 0xDEADBEEF is 8 chars — below the 20-char token threshold.
        text = "Mask is 0xDEADBEEF and flag is 0x01."
        result = align(text)
        # No token extraction for short hex.
        assert not any(s["kind"] == "token" for s in result["extracted"])

    def test_code_profile_skips_tokens(self) -> None:
        # A 25-char base64-looking string under the "code" profile must
        # NOT be extracted as a token (would mangle code identifiers).
        long_id = "aBcDeFgHiJkLmNoPqRsTuVwXy"
        text = f"const handle = {long_id};"
        result = align(text, profile="code")
        assert not any(s["kind"] == "token" for s in result["extracted"])

    def test_default_profile_extracts_long_token(self) -> None:
        long_id = "aBcDeFgHiJkLmNoPqRsTuVwXy"
        text = f"Token: {long_id} end."
        result = align(text)
        assert any(s["kind"] == "token" and s["value"] == long_id for s in result["extracted"])

    def test_function_name_not_mangled(self) -> None:
        text = "def calculate_total(items: list[int]) -> int: return sum(items)"
        result = align(text)
        # The aligned body must still contain the function signature intact.
        body = result["aligned_text"].split("--- Dynamic context ---")[0]
        assert "def calculate_total(items: list[int]) -> int" in body


# ── Profile-aware behaviour ────────────────────────────────────────────────────


class TestProfileAware:
    def test_code_profile_extracts_timestamps(self) -> None:
        text = "Built at 2026-07-17T10:00:00Z commit abc123."
        result = align(text, profile="code")
        assert any(s["kind"] == "timestamp" for s in result["extracted"])

    def test_docs_profile_extracts_timestamps(self) -> None:
        text = "Updated 2026-07-17T10:00:00Z by author."
        result = align(text, profile="docs")
        assert any(s["kind"] == "timestamp" for s in result["extracted"])

    def test_default_profile_extracts_all(self) -> None:
        text = "At 2026-07-17T10:00:00Z sess-abc123 token aBcDeFgHiJkLmNoPqRsTuVwXy."
        result = align(text)
        kinds = {s["kind"] for s in result["extracted"]}
        assert "timestamp" in kinds
        assert "session_id" in kinds
        assert "token" in kinds


# ── MemoryManager integration ──────────────────────────────────────────────────


class TestMemoryManagerIntegration:
    def test_align_prefix_enabled(self, manager: MemoryManager) -> None:
        text = "Session sess-abc123 at 2026-07-17T10:00:00Z."
        result = manager.align_prefix(text)
        assert result["moved_chars"] > 0
        assert any(s["kind"] == "timestamp" for s in result["extracted"])

    def test_align_prefix_disabled_returns_unchanged(self, manager: MemoryManager) -> None:
        manager.settings.cache_aligner.enabled = False
        text = "Session sess-abc123 at 2026-07-17T10:00:00Z."
        result = manager.align_prefix(text)
        assert result["aligned_text"] == text
        assert result["extracted"] == []
        assert result["prefix_stabilized"] is False
        assert result["moved_chars"] == 0


# ── QA fix #1 — CacheAlignerConfig per-kind toggles wired through align_prefix ─


class TestConfigTogglesWired:
    """The per-kind bool toggles on CacheAlignerConfig must be honoured by
    MemoryManager.align_prefix — a disabled kind stays in-place."""

    def test_config_toggles_respected_single_kind(self, manager: MemoryManager) -> None:
        # A long bare token that would be extracted by default.
        long_token = "aBcDeFgHiJkLmNoPqRsTuVwXy"
        text = f"Stable prefix. Token {long_token} end."
        manager.settings.cache_aligner.extract_tokens = False
        result = manager.align_prefix(text)
        kinds = {s["kind"] for s in result["extracted"]}
        assert "token" not in kinds, (
            f"extract_tokens=False should skip token kind; got kinds={kinds}"
        )
        # The token stays in the aligned body (not relocated).
        assert long_token in result["aligned_text"].split("--- Dynamic context ---")[0]

    def test_config_toggles_all_disabled(self, manager: MemoryManager) -> None:
        text = "At 2026-07-17T10:00:00Z sess-abc123def456 token aBcDeFgHiJkLmNoPqRsTuVwXy."
        cfg = manager.settings.cache_aligner
        cfg.extract_timestamps = False
        cfg.extract_uuids = False
        cfg.extract_session_ids = False
        cfg.extract_dates = False
        cfg.extract_tokens = False
        result = manager.align_prefix(text)
        assert result["extracted"] == []
        # No dynamic block appended → aligned_text equals the rstrip'd body,
        # which is the original text (no spans removed).
        assert "--- Dynamic context ---" not in result["aligned_text"]
        assert result["prefix_stabilized"] is False
        assert result["moved_chars"] == 0

    def test_config_toggles_merge_with_profile(self, manager: MemoryManager) -> None:
        # profile="code" already skips tokens; disabling extract_tokens
        # in config must produce a union (still no tokens), while other
        # kinds that "code" does NOT skip remain extractable.
        long_token = "aBcDeFgHiJkLmNoPqRsTuVwXy"
        text = f"Built at 2026-07-17T10:00:00Z commit {long_token};"
        manager.settings.cache_aligner.extract_tokens = False
        result = manager.align_prefix(text, profile="code")
        kinds = {s["kind"] for s in result["extracted"]}
        assert "token" not in kinds, (
            f"profile=code + extract_tokens=False: union must still skip token; got {kinds}"
        )
        # Timestamps are NOT skipped by "code" profile nor by the toggle →
        # still extracted. This proves the merge is a union, not an either-or
        # that would skip ALL kinds when one toggle is off.
        assert "timestamp" in kinds

    def test_config_toggle_timestamps_respected(self, manager: MemoryManager) -> None:
        text = "Stable prefix. Logged 2026-07-17T10:00:00Z done."
        manager.settings.cache_aligner.extract_timestamps = False
        result = manager.align_prefix(text)
        kinds = {s["kind"] for s in result["extracted"]}
        assert "timestamp" not in kinds
        assert "2026-07-17T10:00:00Z" in result["aligned_text"].split("--- Dynamic context ---")[0]

    def test_config_toggle_dates_respected(self, manager: MemoryManager) -> None:
        text = "Stable prefix. Date 2026-07-17 noted."
        manager.settings.cache_aligner.extract_dates = False
        manager.settings.cache_aligner.extract_timestamps = False
        result = manager.align_prefix(text)
        kinds = {s["kind"] for s in result["extracted"]}
        assert "date" not in kinds

    def test_config_toggle_session_ids_respected(self, manager: MemoryManager) -> None:
        text = "Stable prefix. Session sess-abc123def456 active."
        manager.settings.cache_aligner.extract_session_ids = False
        result = manager.align_prefix(text)
        kinds = {s["kind"] for s in result["extracted"]}
        assert "session_id" not in kinds

    def test_config_toggle_uuids_respected(self, manager: MemoryManager) -> None:
        uuid = "550e8400-e29b-41d4-a716-446655440000"
        text = f"Stable prefix. Request id {uuid} failed."
        manager.settings.cache_aligner.extract_uuids = False
        result = manager.align_prefix(text)
        kinds = {s["kind"] for s in result["extracted"]}
        assert "uuid" not in kinds


# ── QA fix #10 — pure dynamic-only text (no stable prefix) ──────────────────────


class TestOnlyDynamicContent:
    def test_only_dynamic_content_no_stable_prefix(self) -> None:
        text = "2026-07-17T10:00:00Z"
        result = align(text)
        assert result["prefix_stabilized"] is False
        # The timestamp is relocated to the dynamic block; the body is
        # empty or whitespace only (no stable prefix to preserve).
        body = result["aligned_text"].split("--- Dynamic context ---")[0]
        assert body.strip() == ""
        # The timestamp appears in the dynamic block.
        assert "2026-07-17T10:00:00Z" in result["aligned_text"]
        assert any(s["kind"] == "timestamp" for s in result["extracted"])
        assert result["moved_chars"] > 0


# ── QA fix #11 — profile="default" string equivalent to None ───────────────────


class TestProfileDefaultStringEquivalent:
    def test_profile_default_string_equivalent_to_none(self) -> None:
        text = "At 2026-07-17T10:00:00Z sess-abc123 token aBcDeFgHiJkLmNoPqRsTuVwXy."
        r_default = align(text, profile="default")
        r_none = align(text, profile=None)
        assert r_default == r_none
        # Sanity: both actually extracted something (not a no-op).
        assert r_default["extracted"], "expected non-empty extraction for default profile"


# ── QA fix #12 — overlapping timestamp/date span selection ─────────────────────


class TestTimestampNotSplitIntoDate:
    def test_timestamp_not_split_into_date(self) -> None:
        # A full ISO timestamp contains a date substring (2026-07-17).
        # The extractor must match it as ONE timestamp span, not as a
        # separate date span overlapping the timestamp's tail.
        text = "Event at 2026-07-17T10:00:00Z done."
        result = align(text)
        kinds = [s["kind"] for s in result["extracted"]]
        # Exactly one span, and it is a timestamp — no separate date.
        assert len(result["extracted"]) == 1, (
            f"expected exactly 1 span for a full timestamp, got {len(result['extracted'])}: {kinds}"
        )
        assert kinds == ["timestamp"]
        assert "date" not in kinds


# ── mnemos #282 — CCR markers are atomic (protected in ALL profiles) ────────────


class TestCCRMarkerAtomic:
    """#282 Finding 2 — the aligner must never mangle a CCR marker.

    A marker ``[compressed: <hash> | N→M chars | retrieve via
    vesma_retrieve]`` carries a 64-hex hash that the bare-token pattern
    matches in token-extracting profiles (log/terminal/web/default).
    Relocating the hash to the trailing Dynamic-context block destroys
    the marker line and breaks the ``vesma_retrieve`` round-trip. The
    marker span is therefore protected from extraction in EVERY profile
    (code/docs skip tokens, but the protection is generic — any kind
    overlapping a marker region is dropped).
    """

    MARKER = build_marker("f" * 64, 5000, 500)
    # Ordinary dynamic content around the marker: an ISO timestamp and a
    # uuid — these MUST still be extracted (the aligner still works).
    TEXT = (
        "Pipeline report for the batch job.\n"
        "Job 550e8400-e29b-41d4-a716-446655440000 started at "
        "2026-09-14T10:30:00Z and produced:\n"
        f"{MARKER}\n"
        "That is the compressed summary of the run."
    )
    PROFILES = (None, "code", "docs", "default", "log", "terminal", "web")

    def test_marker_byte_identical_in_place_all_profiles(self) -> None:
        for profile in self.PROFILES:
            result = align(self.TEXT, profile=profile)
            context = f"profile={profile!r}"
            # The marker line is byte-identical...
            assert self.MARKER in result["aligned_text"], context
            # ...and stays at its ORIGINAL position: inside the body,
            # not relocated to the trailing Dynamic-context block.
            body = result["aligned_text"].split("--- Dynamic context ---")[0]
            assert self.MARKER in body, f"{context}: marker relocated out of the body"
            # The 64-hex hash is never an extracted span of any kind.
            assert all(s["value"] != "f" * 64 for s in result["extracted"]), (
                f"{context}: marker hash extracted: {[s['value'] for s in result['extracted']]}"
            )
            # The whole marker span is untouched — parseable end-to-end.
            parsed = parse_marker(result["aligned_text"])
            assert parsed is not None, f"{context}: marker no longer parseable"
            assert parsed["hash"] == "f" * 64
            assert parsed["original_chars"] == 5000
            assert parsed["compressed_chars"] == 500

    def test_ordinary_dynamic_content_still_extracted(self) -> None:
        """The protection is marker-specific — the aligner still works."""
        # Profiles that extract timestamps/uuids (all of them do — only
        # tokens are profile-skipped) must still relocate both.
        for profile in self.PROFILES:
            result = align(self.TEXT, profile=profile)
            values = {s["value"] for s in result["extracted"]}
            context = f"profile={profile!r}"
            assert "550e8400-e29b-41d4-a716-446655440000" in values, context
            assert "2026-09-14T10:30:00Z" in values, context
            assert result["moved_chars"] > 0, context

    def test_marker_only_no_extraction_noop(self) -> None:
        """A lone marker line is a complete no-op (nothing to extract)."""
        result = align(self.MARKER)
        assert result["extracted"] == []
        assert result["aligned_text"] == self.MARKER
        assert result["prefix_stabilized"] is False

    def test_marker_hash_never_in_dynamic_block(self) -> None:
        """The hash never leaks into the trailing block listing."""
        result = align(self.TEXT, profile="default")
        tail = result["aligned_text"].split("--- Dynamic context ---")[1]
        assert "f" * 64 not in tail
        assert "token: " not in tail
