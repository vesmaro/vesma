"""Pin tests for the 6.0 canonical tag prefix flip (task B2b).

The canonical storage prefix for subtype tags is ``vesma:*`` since 6.0
(supersedes the 6.0 store-block "option B" input-alias split). Pins:

  (a) every NEW write mints ``vesma:<subtype>`` — never ``mnemos:<subtype>``;
  (b) filter semantics: canonical ``vesma:`` filters match; the legacy
      ``mnemos:`` spelling is an INPUT alias (same rows); a raw legacy row
      that the 6.0 mover has not re-slugged yet is invisible to tag
      filters (exact-match doctrine — the mover in the same release train
      closes that window);
  (c) the ``mnemos:no-federate`` trust marker is BYTE-STABLE forever
      (ArchCom 2026-10-03 + the mover's security condition): written and
      read only under that spelling, never renamed to ``vesma:no-federate``;
  (d) ``project:``/``agent:`` slugs are DATA — the alias layer never
      rewrites them (``project:mnemos-mesh`` stays ``project:mnemos-mesh``).
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import (
    NO_FEDERATE_TAG,
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    TagContract,
    normalize_tag_alias,
    normalize_tag_aliases,
    validate_tag_contract,
)


@pytest.fixture
def tmp_manager():
    """MemoryManager with isolated storage and a mock embedder."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            vesma={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
        )
        settings.resolve_paths()
        mgr = MemoryManager(settings)
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        mgr._embedder = mock_embedder
        yield mgr
        mgr.close()


def _add(
    mgr: MemoryManager, content: str, tags: list[str], *, project: str = "p", agent: str = "a"
):
    data = MemoryCreate(
        content=content,
        tags=tags,
        source=MemorySource.MANUAL,
        status=MemoryStatus.PUBLISHED,
    )
    return mgr.add(data, project=project, agent=agent)


# ---------------------------------------------------------------------------
# Alias authority — the normalization table
# ---------------------------------------------------------------------------


class TestNormalizeAliasTable:
    def test_legacy_input_rewrites_to_canon(self) -> None:
        assert normalize_tag_alias("mnemos:decision") == "vesma:decision"
        assert normalize_tag_alias("mnemos:checkpoint") == "vesma:checkpoint"

    def test_canonical_input_is_identity(self) -> None:
        assert normalize_tag_alias("vesma:decision") == "vesma:decision"

    def test_legacy_no_federate_stays_byte_stable(self) -> None:
        assert normalize_tag_alias("mnemos:no-federate") == NO_FEDERATE_TAG

    def test_vesma_no_federate_normalizes_to_marker(self) -> None:
        # Double recognition on INPUT is allowed; on write the marker is
        # ALWAYS the legacy spelling.
        assert normalize_tag_alias("vesma:no-federate") == NO_FEDERATE_TAG

    def test_unknown_subtypes_pass_through_for_loud_refusal(self) -> None:
        assert normalize_tag_alias("mnemos:bogus") == "mnemos:bogus"
        assert normalize_tag_alias("vesma:bogus") == "vesma:bogus"

    def test_other_namespaces_untouched(self) -> None:
        assert normalize_tag_alias("project:mnemos-mesh") == "project:mnemos-mesh"
        assert normalize_tag_alias("agent:x") == "agent:x"

    def test_gcw_migrates_to_current_canon(self) -> None:
        result = validate_tag_contract(["project:p", "agent:a", "gcw:decision"])
        assert "vesma:decision" in result
        assert "mnemos:decision" not in result

    def test_gcw_no_federate_migrates_to_marker(self) -> None:
        result = validate_tag_contract(["project:p", "agent:a", "gcw:no-federate"])
        assert NO_FEDERATE_TAG in result
        assert "vesma:no-federate" not in result

    def test_validate_contract_output_is_canonical(self) -> None:
        result = validate_tag_contract(["project:p", "agent:a", "mnemos:decision"])
        assert result == ["project:p", "agent:a", "vesma:decision"]

    def test_validate_refuses_unknown_subtype_either_spelling(self) -> None:
        with pytest.raises(Exception, match="invalid subtype 'bogus'"):
            validate_tag_contract(["project:p", "agent:a", "vesma:bogus"])
        with pytest.raises(Exception, match="invalid subtype 'bogus'"):
            validate_tag_contract(["project:p", "agent:a", "mnemos:bogus"])

    def test_lax_missing_subtype_patches_vesma_legacy(self) -> None:
        result = validate_tag_contract(["project:p", "agent:a"], strict=False)
        assert "vesma:legacy" in result
        assert "mnemos:legacy" not in result

    def test_contract_subtypes_extraction_both_spellings(self) -> None:
        tc = TagContract(tags=["project:p", "agent:a", "vesma:decision", "mnemos:no-federate"])
        assert tc.vesma_subtypes == frozenset({"decision", "no-federate"})

    def test_filter_alias_normalization_list_form(self) -> None:
        assert normalize_tag_aliases(["mnemos:decision", "task:x"]) == [
            "vesma:decision",
            "task:x",
        ]


# ---------------------------------------------------------------------------
# (a) new writes mint vesma:<subtype>
# ---------------------------------------------------------------------------


class TestNewWritesAreCanonical:
    def test_add_rewrites_legacy_input_to_canon(self, tmp_manager: MemoryManager) -> None:
        mem = _add(tmp_manager, "body", ["project:p", "agent:a", "mnemos:decision"])
        assert "vesma:decision" in mem.tags
        assert "mnemos:decision" not in mem.tags

    def test_add_keeps_canonical_input(self, tmp_manager: MemoryManager) -> None:
        mem = _add(tmp_manager, "body", ["project:p", "agent:a", "vesma:learning"])
        assert mem.tags == ["project:p", "agent:a", "vesma:learning"]

    def test_save_checkpoint_mints_vesma_checkpoint(self, tmp_manager: MemoryManager) -> None:
        mem, _deduped = tmp_manager.save_checkpoint(
            {"goals": "ship the flip", "decisions": "canon is vesma:*"},
            project="p",
            agent="a",
        )
        assert "vesma:checkpoint" in mem.tags
        assert "mnemos:checkpoint" not in mem.tags

    def test_no_stored_row_carries_mnemos_subtype_after_flip(
        self, tmp_manager: MemoryManager
    ) -> None:
        for subtype in ("decision", "rule", "session"):
            _add(tmp_manager, f"body {subtype}", ["project:p", "agent:a", f"mnemos:{subtype}"])
        rows = tmp_manager.sqlite.list_all(limit=100)
        assert rows
        for row in rows:
            assert not any(t.startswith("mnemos:") for t in row.tags)


# ---------------------------------------------------------------------------
# (b) filter semantics — canon matches, legacy input aliases, raw legacy
#     rows await the mover
# ---------------------------------------------------------------------------


class TestFilterSemantics:
    def test_canonical_filter_finds_canonical_row(self, tmp_manager: MemoryManager) -> None:
        _add(tmp_manager, "the decision", ["project:p", "agent:a", "vesma:decision"])
        hits = tmp_manager.search("decision", tags=["vesma:decision"], project="p")
        assert len(hits) == 1

    def test_legacy_filter_alias_finds_same_row(self, tmp_manager: MemoryManager) -> None:
        # Old-style filters keep working: mnemos:decision normalizes to
        # vesma:decision at the search boundary.
        _add(tmp_manager, "the decision", ["project:p", "agent:a", "vesma:decision"])
        hits = tmp_manager.search("decision", tags=["mnemos:decision"], project="p")
        assert len(hits) == 1

    def test_raw_legacy_row_invisible_until_mover(self, tmp_manager: MemoryManager) -> None:
        """Pin of the transition semantics (decision (i), B2b report).

        A row still carrying the RAW legacy spelling (written by a 5.x
        build, not yet re-slugged by the 6.0 mover) is NOT matched by
        tag filters — the mover in the same release train closes this
        window. Exact-match doctrine, pinned so a silent dual-match
        never creeps in.
        """
        mem = _add(tmp_manager, "legacy row", ["project:p", "agent:a", "vesma:decision"])
        conn = sqlite3.connect(str(tmp_manager.settings.db_path))
        try:
            conn.execute(
                "UPDATE memories SET tags = ? WHERE id = ?",
                ('["project:p", "agent:a", "mnemos:decision"]', mem.id),
            )
            conn.commit()
        finally:
            conn.close()
        assert tmp_manager.search("legacy", tags=["vesma:decision"], project="p") == []
        # The row itself is still there (no filter) and the dual-read
        # structural scanners still see its subtype.
        from vesma.compact import derive_record_type

        assert derive_record_type(["project:p", "mnemos:decision"]) == "decision"
        assert derive_record_type(["project:p", "vesma:decision"]) == "decision"


# ---------------------------------------------------------------------------
# (c) no-federate — byte-stable trust marker, double pin (write + read)
# ---------------------------------------------------------------------------


class TestNoFederateByteStability:
    def test_marker_constant_is_legacy_spelled(self) -> None:
        assert NO_FEDERATE_TAG == "mnemos:no-federate"

    def test_write_keeps_marker_spelling(self, tmp_manager: MemoryManager) -> None:
        mem = _add(
            tmp_manager,
            "secret-bearing row",
            ["project:p", "agent:a", "vesma:decision", NO_FEDERATE_TAG],
        )
        assert NO_FEDERATE_TAG in mem.tags

    def test_rename_to_vesma_never_touches_marker(self, tmp_manager: MemoryManager) -> None:
        """The mover's exact call shape must leave the marker alone."""
        _add(
            tmp_manager,
            "excluded row",
            ["project:p", "agent:a", "vesma:decision", NO_FEDERATE_TAG],
        )
        report = tmp_manager.tags_rename(from_prefix="mnemos:", to_prefix="vesma:", dry_run=False)
        rows = tmp_manager.sqlite.list_all(limit=100)
        for row in rows:
            assert NO_FEDERATE_TAG in row.tags or not any(t == NO_FEDERATE_TAG for t in row.tags)
            assert "vesma:no-federate" not in row.tags
        assert report["errors"] == []

    def test_rename_from_vesma_cannot_mint_vesma_marker(self, tmp_manager: MemoryManager) -> None:
        _add(tmp_manager, "plain row", ["project:p", "agent:a", "vesma:decision"])
        tmp_manager.tags_rename(from_prefix="vesma:", to_prefix="mnemos:", dry_run=False)
        rows = tmp_manager.sqlite.list_all(limit=100)
        for row in rows:
            assert "vesma:no-federate" not in row.tags

    def test_federation_type_scanner_skips_marker_both_spellings(self) -> None:
        from vesma.federation_server import _memory_type_for_filter

        assert _memory_type_for_filter([NO_FEDERATE_TAG]) == ""
        assert _memory_type_for_filter(["vesma:no-federate"]) == ""
        assert _memory_type_for_filter(["vesma:no-federate", "vesma:rule"]) == "rule"
        assert _memory_type_for_filter(["mnemos:decision"]) == "decision"


# ---------------------------------------------------------------------------
# (d) project/agent slugs are data — never rewritten by the alias layer
# ---------------------------------------------------------------------------


class TestProjectSlugsUntouched:
    def test_foreign_mnemos_slug_survives_write(self, tmp_manager: MemoryManager) -> None:
        mem = _add(
            tmp_manager,
            "cross-project row",
            ["project:mnemos-mesh", "agent:a", "mnemos:decision"],
            project="mnemos-mesh",
        )
        assert "project:mnemos-mesh" in mem.tags

    def test_alias_normalizer_never_touches_project_agent(self) -> None:
        tags = ["project:mnemos-mesh", "agent:mnemos-agent", "vesma:decision"]
        assert normalize_tag_aliases(tags) == tags

    def test_contract_extracts_foreign_slug_verbatim(self) -> None:
        tc = TagContract(tags=["project:mnemos-mesh", "agent:a", "vesma:decision"])
        assert tc.project == "mnemos-mesh"
