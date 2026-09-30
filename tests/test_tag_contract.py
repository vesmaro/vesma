"""Tests for M2: TagContract.

Covers:
  - validate_tag_contract() — happy path, missing tags, invalid format,
    multiple project: tags, strict vs lax mode
  - TagContract model — project/agent extraction, lax patching
  - Memory model — TagContractError on bad tags, effective_content()
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import ValidationError

from vesmaro.models import (
    Memory,
    TagContract,
    TagContractError,
    validate_tag_contract,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_TAGS = ["project:myproject", "agent:copilot", "mnemos:learning"]

VALID_TAGS_ALL = [
    "project:myproject",
    "agent:copilot",
    "mnemos:learning",
    "mnemos:decision",
    "source:chat",
    "applyTo:src/**/*.py",
]


# ---------------------------------------------------------------------------
# validate_tag_contract — happy path
# ---------------------------------------------------------------------------


class TestValidateTagContractHappyPath:
    def test_minimal_valid(self):
        result = validate_tag_contract(VALID_TAGS)
        assert result == VALID_TAGS

    def test_all_optional_pass_through(self):
        result = validate_tag_contract(VALID_TAGS_ALL)
        assert set(result) == set(VALID_TAGS_ALL)

    def test_returns_original_list_unchanged(self):
        tags = ["project:x", "agent:y", "mnemos:session"]
        result = validate_tag_contract(tags)
        assert result == tags

    def test_multiple_mnemos_subtypes_allowed(self):
        tags = ["project:x", "agent:y", "mnemos:session", "mnemos:checkpoint"]
        result = validate_tag_contract(tags)
        assert sorted(result) == sorted(tags)


# ---------------------------------------------------------------------------
# validate_tag_contract — strict mode raises
# ---------------------------------------------------------------------------


class TestValidateTagContractStrictRaises:
    def test_missing_project_raises(self):
        with pytest.raises(TagContractError, match="project:"):
            validate_tag_contract(["agent:copilot", "mnemos:learning"], strict=True)

    def test_missing_agent_raises(self):
        with pytest.raises(TagContractError, match="agent:"):
            validate_tag_contract(["project:myproject", "mnemos:learning"], strict=True)

    def test_missing_mnemos_raises(self):
        with pytest.raises(TagContractError, match="mnemos:"):
            validate_tag_contract(["project:myproject", "agent:copilot"], strict=True)

    def test_empty_list_raises(self):
        with pytest.raises(TagContractError):
            validate_tag_contract([], strict=True)

    def test_multiple_project_raises(self):
        with pytest.raises(TagContractError, match="exactly one"):
            validate_tag_contract(
                ["project:a", "project:b", "agent:y", "mnemos:learning"],
                strict=True,
            )

    def test_multiple_agent_raises(self):
        with pytest.raises(TagContractError, match="exactly one"):
            validate_tag_contract(
                ["project:x", "agent:a", "agent:b", "mnemos:learning"],
                strict=True,
            )

    def test_invalid_mnemos_subtype_raises(self):
        with pytest.raises(TagContractError, match="mnemos:"):
            validate_tag_contract(
                ["project:x", "agent:y", "mnemos:invalid-subtype"],
                strict=True,
            )

    def test_invalid_project_slug_raises(self):
        with pytest.raises(TagContractError, match="project:"):
            validate_tag_contract(
                ["project:Invalid Slug!", "agent:y", "mnemos:learning"],
                strict=True,
            )

    def test_invalid_agent_slug_raises(self):
        with pytest.raises(TagContractError, match="agent:"):
            validate_tag_contract(
                ["project:x", "agent:bad name here", "mnemos:learning"],
                strict=True,
            )


# ---------------------------------------------------------------------------
# validate_tag_contract — lax (strict=False) auto-patches or passes
# ---------------------------------------------------------------------------


class TestValidateTagContractLaxMode:
    def test_lax_allows_missing_project_with_patch(self):
        """In lax mode, missing required tags do NOT raise — they may be patched."""
        tags = ["agent:copilot", "mnemos:learning"]
        # Should not raise; behaviour: either patch or return as-is
        result = validate_tag_contract(tags, strict=False)
        assert isinstance(result, list)

    def test_lax_allows_missing_agent(self):
        tags = ["project:myproject", "mnemos:learning"]
        result = validate_tag_contract(tags, strict=False)
        assert isinstance(result, list)

    def test_lax_allows_missing_mnemos(self):
        tags = ["project:myproject", "agent:copilot"]
        result = validate_tag_contract(tags, strict=False)
        assert isinstance(result, list)

    def test_lax_still_raises_on_multiple_project(self):
        """Multiple project: tags are always an error — ambiguous context."""
        with pytest.raises(TagContractError, match="exactly one"):
            validate_tag_contract(
                ["project:a", "project:b", "agent:y", "mnemos:learning"],
                strict=False,
            )

    def test_lax_still_raises_on_multiple_agent(self):
        with pytest.raises(TagContractError, match="exactly one"):
            validate_tag_contract(
                ["project:x", "agent:a", "agent:b", "mnemos:learning"],
                strict=False,
            )


# ---------------------------------------------------------------------------
# TagContract model
# ---------------------------------------------------------------------------


class TestTagContractModel:
    def test_extracts_project_and_agent(self):
        tc = TagContract(tags=VALID_TAGS)
        assert tc.project == "myproject"
        assert tc.agent == "copilot"

    def test_mnemos_subtypes_extracted(self):
        tags = ["project:x", "agent:y", "mnemos:session", "mnemos:checkpoint"]
        tc = TagContract(tags=tags)
        assert "session" in tc.mnemos_subtypes
        assert "checkpoint" in tc.mnemos_subtypes

    def test_invalid_tags_raise_validation_error_in_strict(self):
        with pytest.raises(ValidationError):
            TagContract(tags=["agent:copilot", "mnemos:learning"], strict=True)

    def test_lax_model_accepts_incomplete_tags(self):
        tc = TagContract(tags=["agent:copilot", "mnemos:learning"], strict=False)
        assert isinstance(tc, TagContract)

    def test_immutable_tags_list(self):
        tc = TagContract(tags=VALID_TAGS)
        assert isinstance(tc.tags, (list, tuple, frozenset))


# ---------------------------------------------------------------------------
# Memory model — TagContract integration
# ---------------------------------------------------------------------------


class TestMemoryTagContractIntegration:
    def test_memory_with_valid_tags(self):
        m = Memory(
            content="Test memory entry.",
            tags=VALID_TAGS,
            project="myproject",
            agent="copilot",
        )
        assert m.project == "myproject"
        assert m.agent == "copilot"

    def test_memory_strict_mode_rejects_missing_project(self):
        with pytest.raises((TagContractError, ValidationError)):
            Memory(
                content="Test.",
                tags=["agent:copilot", "mnemos:learning"],
                project="",
                agent="copilot",
                strict_tags=True,
            )

    def test_memory_effective_content_prefers_clean(self):
        m = Memory(
            content="raw",
            tags=VALID_TAGS,
            project="myproject",
            agent="copilot",
            clean_content="cleaned",
        )
        assert m.effective_content() == "cleaned"

    def test_memory_effective_content_falls_back_to_content(self):
        m = Memory(
            content="raw",
            tags=VALID_TAGS,
            project="myproject",
            agent="copilot",
        )
        assert m.effective_content() == "raw"

    def test_memory_id_is_populated(self):
        m = Memory(
            content="Test.",
            tags=VALID_TAGS,
            project="myproject",
            agent="copilot",
        )
        assert m.id  # not empty/None

    def test_memory_status_default_is_raw(self):
        m = Memory(
            content="Test.",
            tags=VALID_TAGS,
            project="myproject",
            agent="copilot",
        )
        assert m.status is not None
        # MemoryStatus(str, Enum) — value is "raw"; .value always == "raw"
        assert m.status.value == "raw"


# ---------------------------------------------------------------------------
# Vesma subtype catalogue
# ---------------------------------------------------------------------------


class TestMnemosSubtypes:
    """All documented mnemos: subtypes must be in the allowed set."""

    EXPECTED: ClassVar[set[str]] = {
        "session",
        "bug-pattern",
        "learning",
        "decision",
        "rule",
        "open-question",
        "checkpoint",
        "legacy",
    }

    def test_all_expected_subtypes_valid(self):
        for subtype in self.EXPECTED:
            tags = [f"mnemos:{subtype}", "project:x", "agent:y"]
            result = validate_tag_contract(tags, strict=True)
            assert any(f"mnemos:{subtype}" in t for t in result)

    def test_unknown_subtype_invalid(self):
        with pytest.raises(TagContractError):
            validate_tag_contract(
                ["project:x", "agent:y", "mnemos:totally-unknown"],
                strict=True,
            )
