"""Tests for the safe bulk tag rename feature (issue #79).

Covers:
  - ``MemoryManager.tags_rename`` — dry-run, real run, subtype filters,
    invalid-subtype handling (skip vs →legacy), idempotency.
  - FTS5 consistency after rename — ``vesma_search`` finds records by the
    NEW tag and NOT the old tag.
  - Denormalised ``project``/``agent`` columns stay in sync.
  - Vector search still returns results after rename (vectors keyed by
    memory_id, not by tags).
  - ``mnemos:synthesized`` is a valid whitelist subtype.
  - ``gcw:synthesized`` migrates to ``mnemos:synthesized`` via
    ``validate_tag_contract``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import (
    VESMARO_TAG_SUBTYPES,
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    validate_tag_contract,
)

# ---------------------------------------------------------------------------
# Fixtures — isolated MemoryManager per test
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings():
    """Yield a Settings object backed by a temporary directory."""
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
        yield settings


@pytest.fixture
def tmp_manager(tmp_settings):
    """Yield a MemoryManager with isolated storage and a mock embedder.

    The mock embedder returns a fixed 384-dim vector so the vector leg
    does not fail (ONNX model is not available in CI).
    """
    mgr = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


def _make_create(
    content: str,
    tags: list[str],
    *,
    status: MemoryStatus = MemoryStatus.PUBLISHED,
) -> MemoryCreate:
    """Build a MemoryCreate with explicit tags and status."""
    return MemoryCreate(
        content=content,
        tags=tags,
        source=MemorySource.MANUAL,
        status=status,
    )


def _add_gcw_memory(mgr: MemoryManager, *, subtype: str = "decision") -> str:
    """Add a published memory with a gcw:<subtype> tag and return its id."""
    tags = ["project:test-proj", "agent:test-agent", f"gcw:{subtype}"]
    data = _make_create(f"gcw memory with {subtype}", tags)
    mem = mgr.add(data, project="test-proj", agent="test-agent")
    return mem.id


# ---------------------------------------------------------------------------
# Unit: mnemos:synthesized whitelist + gcw:synthesized migration
# ---------------------------------------------------------------------------


class TestSynthesizedSubtype:
    def test_synthesized_in_whitelist(self) -> None:
        assert "synthesized" in VESMARO_TAG_SUBTYPES

    def test_validate_accepts_mnemos_synthesized(self) -> None:
        tags = ["project:p", "agent:a", "mnemos:synthesized"]
        out = validate_tag_contract(tags, strict=True)
        assert "mnemos:synthesized" in out

    def test_gcw_synthesized_migrates_to_mnemos(self) -> None:
        """gcw:synthesized is now valid → validate_tag_contract migrates it."""
        tags = ["project:p", "agent:a", "gcw:synthesized"]
        out = validate_tag_contract(tags, strict=True)
        assert "mnemos:synthesized" in out
        assert "gcw:synthesized" not in out

    def test_gcw_unknown_stays_as_is_for_error(self) -> None:
        """Invalid gcw: subtype is preserved (for the error message).

        In strict mode the migration keeps the unknown gcw: tag as-is,
        so the resulting tag set has no valid mnemos: tag and strict
        validation raises. In lax mode the tag is still kept as-is and
        the contract patches in mnemos:legacy. The rename method itself
        re-validates in lax mode, so this is the realistic path.
        """
        tags = ["project:p", "agent:a", "gcw:totally-unknown"]
        out = validate_tag_contract(tags, strict=False)
        # The unknown gcw: tag is preserved (not migrated), and lax mode
        # patches in mnemos:legacy because no valid mnemos: tag exists.
        assert "gcw:totally-unknown" in out
        assert "mnemos:legacy" in out


# ---------------------------------------------------------------------------
# Unit: tags_rename — dry-run, real run, subtype filters
# ---------------------------------------------------------------------------


class TestTagsRenameUnit:
    def test_dry_run_writes_nothing(self, tmp_manager: MemoryManager) -> None:
        mid = _add_gcw_memory(tmp_manager)
        report = tmp_manager.tags_rename(from_prefix="gcw:", to_prefix="mnemos:", dry_run=True)
        assert report["dry_run"] is True
        assert report["renamed"] == 1
        # Nothing written — memory still has gcw:decision
        mem = tmp_manager.sqlite.get(mid)
        assert mem is not None
        assert "gcw:decision" in mem.tags
        assert "mnemos:decision" not in mem.tags

    def test_real_run_writes(self, tmp_manager: MemoryManager) -> None:
        mid = _add_gcw_memory(tmp_manager)
        report = tmp_manager.tags_rename(from_prefix="gcw:", to_prefix="mnemos:", dry_run=False)
        assert report["dry_run"] is False
        assert report["renamed"] == 1
        mem = tmp_manager.sqlite.get(mid)
        assert mem is not None
        assert "mnemos:decision" in mem.tags
        assert "gcw:decision" not in mem.tags

    def test_subtype_filter_renames_only_matching(self, tmp_manager: MemoryManager) -> None:
        _add_gcw_memory(tmp_manager, subtype="decision")
        _add_gcw_memory(tmp_manager, subtype="learning")
        report = tmp_manager.tags_rename(
            from_prefix="gcw:",
            to_prefix="mnemos:",
            subtypes=["decision"],
            dry_run=False,
        )
        assert report["renamed"] == 1
        # The learning one should still be gcw:
        all_mems = tmp_manager.sqlite.list_all(limit=100)
        tags_flat = [t for m in all_mems for t in m.tags]
        assert "mnemos:decision" in tags_flat
        assert "gcw:learning" in tags_flat
        assert "mnemos:learning" not in tags_flat

    def test_idempotent_second_run(self, tmp_manager: MemoryManager) -> None:
        _add_gcw_memory(tmp_manager)
        tmp_manager.tags_rename(from_prefix="gcw:", to_prefix="mnemos:", dry_run=False)
        report2 = tmp_manager.tags_rename(from_prefix="gcw:", to_prefix="mnemos:", dry_run=False)
        assert report2["renamed"] == 0

    def test_invalid_prefix_rejected(self, tmp_manager: MemoryManager) -> None:
        report = tmp_manager.tags_rename(from_prefix="gcw", to_prefix="mnemos:", dry_run=True)
        assert report["renamed"] == 0
        assert len(report["errors"]) == 1
        assert "must end with ':'" in report["errors"][0]


# ---------------------------------------------------------------------------
# Unit: invalid subtypes — skip vs →legacy
# ---------------------------------------------------------------------------


class TestInvalidSubtypes:
    def test_invalid_subtype_skipped_by_default(self, tmp_manager: MemoryManager) -> None:
        # Insert a memory with an invalid gcw: subtype directly via update_fields
        # so it bypasses strict validation (simulating a legacy row).
        mid = _add_gcw_memory(tmp_manager, subtype="decision")
        # Overwrite tags to include an invalid gcw: subtype.
        tmp_manager.sqlite.update_fields(
            mid,
            tags=["project:test-proj", "agent:test-agent", "gcw:totally-unknown"],
            project="test-proj",
            agent="test-agent",
        )
        report = tmp_manager.tags_rename(from_prefix="gcw:", to_prefix="mnemos:", dry_run=False)
        assert report["skipped_invalid"] == 1
        assert report["renamed"] == 0
        mem = tmp_manager.sqlite.get(mid)
        assert mem is not None
        assert "gcw:totally-unknown" in mem.tags

    def test_invalid_subtype_to_legacy(self, tmp_manager: MemoryManager) -> None:
        mid = _add_gcw_memory(tmp_manager, subtype="decision")
        tmp_manager.sqlite.update_fields(
            mid,
            tags=["project:test-proj", "agent:test-agent", "gcw:totally-unknown"],
            project="test-proj",
            agent="test-agent",
        )
        report = tmp_manager.tags_rename(
            from_prefix="gcw:",
            to_prefix="mnemos:",
            dry_run=False,
            invalid_subtypes_to_legacy=True,
        )
        assert report["renamed"] == 1
        assert report["skipped_invalid"] == 0
        mem = tmp_manager.sqlite.get(mid)
        assert mem is not None
        assert "mnemos:legacy" in mem.tags
        assert "gcw:totally-unknown" not in mem.tags


# ---------------------------------------------------------------------------
# Integration: FTS5 consistency + denormalised columns + vector search
# ---------------------------------------------------------------------------


class TestTagsRenameIntegration:
    def test_fts5_finds_new_tag_not_old(self, tmp_manager: MemoryManager) -> None:
        """After rename, vesma_search finds records by mnemos:decision, not gcw:decision."""
        mid = _add_gcw_memory(tmp_manager, subtype="decision")
        # Before rename: search with gcw:decision tag filter finds it.
        hits_before = tmp_manager.search("gcw memory", tags=["gcw:decision"], limit=10)
        assert any(r.memory.id == mid for r in hits_before)

        # Rename.
        tmp_manager.tags_rename(from_prefix="gcw:", to_prefix="mnemos:", dry_run=False)

        # After rename: search with mnemos:decision finds it.
        hits_after_new = tmp_manager.search("gcw memory", tags=["mnemos:decision"], limit=10)
        assert any(r.memory.id == mid for r in hits_after_new)

        # After rename: search with the OLD gcw:decision tag no longer finds it.
        hits_after_old = tmp_manager.search("gcw memory", tags=["gcw:decision"], limit=10)
        assert not any(r.memory.id == mid for r in hits_after_old)

    def test_denormalised_project_agent_unchanged_for_gcw(self, tmp_manager: MemoryManager) -> None:
        """For gcw:→mnemos: the project/agent columns must not drift."""
        mid = _add_gcw_memory(tmp_manager)
        tmp_manager.tags_rename(from_prefix="gcw:", to_prefix="mnemos:", dry_run=False)
        mem = tmp_manager.sqlite.get(mid)
        assert mem is not None
        assert mem.project == "test-proj"
        assert mem.agent == "test-agent"

    def test_vector_search_still_works_after_rename(self, tmp_manager: MemoryManager) -> None:
        """Semantic search returns results after rename — vectors keyed by memory_id."""
        mid = _add_gcw_memory(tmp_manager, subtype="decision")
        tmp_manager.tags_rename(from_prefix="gcw:", to_prefix="mnemos:", dry_run=False)
        # The mock embedder returns a constant vector, so the vector leg
        # returns the memory by id regardless of tag changes. This verifies
        # the vector store is not broken by the rename.
        results = tmp_manager.search("gcw memory", limit=10)
        assert any(r.memory.id == mid for r in results)

    def test_scope_project_filter(self, tmp_manager: MemoryManager) -> None:
        """project= filter scopes the scan to that project only."""
        # Add one memory in test-proj, one in other-proj.
        data_a = _make_create(
            "memory a",
            ["project:test-proj", "agent:a1", "gcw:decision"],
        )
        data_b = _make_create(
            "memory b",
            ["project:other-proj", "agent:a2", "gcw:decision"],
        )
        mgr = tmp_manager
        mem_a = mgr.add(data_a, project="test-proj", agent="a1")
        mem_b = mgr.add(data_b, project="other-proj", agent="a2")

        report = mgr.tags_rename(
            from_prefix="gcw:", to_prefix="mnemos:", dry_run=False, project="test-proj"
        )
        assert report["renamed"] == 1
        # mem_a renamed, mem_b untouched.
        a = mgr.sqlite.get(mem_a.id)
        b = mgr.sqlite.get(mem_b.id)
        assert a is not None and b is not None
        assert "mnemos:decision" in a.tags
        assert "gcw:decision" in b.tags

    def test_trace_recorded(self, tmp_manager: MemoryManager) -> None:
        """A trace row with step='tags_rename' is written after the call."""
        _add_gcw_memory(tmp_manager)
        tmp_manager.tags_rename(from_prefix="gcw:", to_prefix="mnemos:", dry_run=False)
        traces = tmp_manager.sqlite.list_traces(task_label="tags_rename", limit=10)
        assert len(traces) >= 1
        assert traces[0].step == "tags_rename"


# ---------------------------------------------------------------------------
# Integration: MCP dispatch + HTTP endpoint smoke
# ---------------------------------------------------------------------------


class TestMcpAndHttp:
    def test_mcp_dispatch_tags_rename(self, tmp_manager: MemoryManager, monkeypatch) -> None:
        """The MCP _dispatch handles vesma_tags_rename."""
        from mnemos import mcp_server
        from vesma.mcp_server import _dispatch

        _add_gcw_memory(tmp_manager)
        # Patch the MCP server's get_manager to return our isolated manager.
        monkeypatch.setattr(mcp_server, "get_manager", lambda: tmp_manager)

        import asyncio

        result = asyncio.run(
            _dispatch(
                "vesma_tags_rename",
                {"from_prefix": "gcw:", "to_prefix": "mnemos:", "dry_run": False},
            )
        )
        assert isinstance(result, dict)
        assert result["renamed"] == 1
        assert result["dry_run"] is False
