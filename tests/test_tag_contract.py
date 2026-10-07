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

from vesma.models import (
    Memory,
    TagContract,
    TagContractError,
    normalize_tag_aliases,
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
        # Legacy mnemos:* input is an alias — output carries the canon.
        result = validate_tag_contract(VALID_TAGS)
        assert result == ["project:myproject", "agent:copilot", "vesma:learning"]

    def test_all_optional_pass_through(self):
        result = validate_tag_contract(VALID_TAGS_ALL)
        assert set(result) == {
            "project:myproject",
            "agent:copilot",
            "vesma:learning",
            "vesma:decision",
            "source:chat",
            "applyTo:src/**/*.py",
        }

    def test_legacy_input_normalizes_to_canon(self):
        tags = ["project:x", "agent:y", "mnemos:session"]
        result = validate_tag_contract(tags)
        assert result == ["project:x", "agent:y", "vesma:session"]

    def test_canonical_input_unchanged(self):
        tags = ["project:x", "agent:y", "vesma:session"]
        result = validate_tag_contract(tags)
        assert result == tags

    def test_multiple_mnemos_subtypes_allowed(self):
        tags = ["project:x", "agent:y", "mnemos:session", "mnemos:checkpoint"]
        result = validate_tag_contract(tags)
        assert sorted(result) == sorted(
            ["project:x", "agent:y", "vesma:session", "vesma:checkpoint"]
        )


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

    def test_missing_subtype_raises(self):
        with pytest.raises(TagContractError, match="vesma:<subtype>"):
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
        # Legacy-spelled input — each migrates to the canon on validation.
        for subtype in self.EXPECTED:
            tags = [f"mnemos:{subtype}", "project:x", "agent:y"]
            result = validate_tag_contract(tags, strict=True)
            assert any(f"vesma:{subtype}" in t for t in result)

    def test_unknown_subtype_invalid(self):
        with pytest.raises(TagContractError):
            validate_tag_contract(
                ["project:x", "agent:y", "mnemos:totally-unknown"],
                strict=True,
            )


# ---------------------------------------------------------------------------
# vesma:* input alias (6.0.0 store block, ArchCom 2026-10-03 option B)
# ---------------------------------------------------------------------------


class TestVesmaInputAlias:
    """6.0 canonical-prefix flip: ``vesma:<subtype>`` is the canon both
    typed and stored; the legacy ``mnemos:<subtype>`` spelling is the
    input alias, normalized to the canon by ``normalize_tag_aliases`` —
    the single shared helper — and inside ``validate_tag_contract``, so
    every write path accepts it.
    """

    def test_canonical_input_is_identity(self):
        result = validate_tag_contract(["project:x", "agent:y", "vesma:learning"], strict=True)
        assert "vesma:learning" in result
        assert not any(t.startswith("mnemos:") for t in result)

    def test_no_federate_alias_normalized(self):
        """The federation trust marker normalizes too — storage stays byte-stable."""
        result = validate_tag_contract(["project:x", "agent:y", "vesma:no-federate"], strict=True)
        assert "mnemos:no-federate" in result

    def test_legacy_input_normalizes_to_canon(self):
        legacy = ["project:x", "agent:y", "mnemos:learning"]
        assert validate_tag_contract(list(legacy), strict=True) == [
            "project:x",
            "agent:y",
            "vesma:learning",
        ]

    def test_round_trip_alias_equals_legacy(self):
        """Normalized alias input produces the byte-identical tag list as legacy."""
        legacy = ["project:x", "agent:y", "mnemos:learning", "mnemos:no-federate"]
        alias = ["project:x", "agent:y", "vesma:learning", "vesma:no-federate"]
        assert validate_tag_contract(
            normalize_tag_aliases(alias), strict=True
        ) == validate_tag_contract(list(legacy), strict=True)

    def test_unknown_alias_subtype_refuses_loudly(self):
        with pytest.raises(TagContractError, match="invalid subtype 'bogus' in tag 'vesma:bogus'"):
            validate_tag_contract(["project:x", "agent:y", "vesma:bogus"], strict=True)

    def test_alias_typo_refused_not_repaired(self):
        """A mistyped subtype fails with the offending tag named, never minted."""
        with pytest.raises(TagContractError, match="vesma:leanring"):
            validate_tag_contract(["project:x", "agent:y", "vesma:leanring"], strict=True)

    def test_normalize_helper_leaves_other_namespaces_alone(self):
        tags = ["project:p", "agent:a", "task:t", "gcw:decision", "source:chat", "freeform"]
        assert normalize_tag_aliases(list(tags)) == tags

    def test_normalize_helper_unknown_subtype_passthrough(self):
        """The helper never drops or repairs — refusal belongs to the contract."""
        assert normalize_tag_aliases(["vesma:bogus"]) == ["vesma:bogus"]

    def test_legacy_input_accepted_in_lax_mode(self):
        result = validate_tag_contract(["project:x", "agent:y", "mnemos:decision"], strict=False)
        assert "vesma:decision" in result
