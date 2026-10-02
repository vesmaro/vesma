"""Regression tests for the remaining code-review findings.

Covers:
  MEDIUM-3 — archived entries surface when include_raw=True
  LOW-1    — search_type indicator based on vector leg output, not contribution
  LOW-2    — invalid status string in MCP tool produces unclear error
  LOW-3    — degraded flag doesn't detect orphaned vectors
  LOW-4    — tag normalization produces leading/trailing hyphens from spaces
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import (
    Memory,
    MemoryCreate,
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
            mnemos={
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_create(
    content: str,
    *,
    agent: str = "test-agent",
    project: str = "test-project",
    status: MemoryStatus = MemoryStatus.RAW,
) -> MemoryCreate:
    """Build a MemoryCreate with valid Mnemos tags and explicit status."""
    return MemoryCreate(
        content=content,
        tags=[f"project:{project}", f"agent:{agent}", "mnemos:learning"],
        status=status,
    )


def _add(mgr: MemoryManager, content: str, **kwargs) -> Memory:
    """Add a memory through the manager and return it."""
    data = _make_create(content, **kwargs)
    project = kwargs.get("project", "test-project")
    agent = kwargs.get("agent", "test-agent")
    return mgr.add(data, project=project, agent=agent)


# ---------------------------------------------------------------------------
# MEDIUM-3 — archived entries surface when include_raw=True
# ---------------------------------------------------------------------------


class TestIncludeRawExcludesArchived:
    def test_include_raw_excludes_archived(self, tmp_manager):
        """include_raw=True returns raw but NOT archived entries."""
        mgr = tmp_manager
        _add(mgr, "raw content about zeta alpha", status=MemoryStatus.RAW)
        _add(mgr, "archived content about zeta alpha", status=MemoryStatus.ARCHIVED)

        results = mgr.search("zeta alpha", include_raw=True)
        statuses = {r.memory.status for r in results}
        assert MemoryStatus.RAW in statuses, "raw should surface with include_raw=True"
        assert MemoryStatus.ARCHIVED not in statuses, (
            "archived must NOT surface with include_raw=True (intentionally hidden)"
        )

    def test_explicit_status_archived_works(self, tmp_manager):
        """Explicit status=ARCHIVED returns archived entries (overrides exclusion)."""
        mgr = tmp_manager
        _add(mgr, "archived content about zeta beta", status=MemoryStatus.ARCHIVED)
        _add(mgr, "published content about zeta beta", status=MemoryStatus.PUBLISHED)

        results = mgr.search("zeta beta", status=MemoryStatus.ARCHIVED)
        assert len(results) == 1, "explicit status=ARCHIVED should return the archived entry"
        assert results[0].memory.status == MemoryStatus.ARCHIVED

    def test_default_search_excludes_archived(self, tmp_manager):
        """Default search (no include_raw, no status) excludes archived."""
        mgr = tmp_manager
        _add(mgr, "published content about zeta gamma", status=MemoryStatus.PUBLISHED)
        _add(mgr, "archived content about zeta gamma", status=MemoryStatus.ARCHIVED)

        results = mgr.search("zeta gamma", include_raw=False)
        statuses = {r.memory.status for r in results}
        assert MemoryStatus.PUBLISHED in statuses
        assert MemoryStatus.ARCHIVED not in statuses


# ---------------------------------------------------------------------------
# LOW-1 — search_type reflects actual vector contribution
# ---------------------------------------------------------------------------


class TestSearchTypeContribution:
    def test_search_type_fts_only_when_vector_filtered_out(self, tmp_manager):
        """Vector leg returns pairs but all filtered out by status → fts_only.

        Scenario: a published memory is in the vector store, but we search
        with an explicit status=ARCHIVED. The vector leg returns the
        published memory's id, but the vector-leg status filter drops it
        (fetched.status != status). The FTS leg also returns nothing for
        archived. The search_type must be "fts_only", not "hybrid", because
        no vector pair contributed to the final result set.
        """
        mgr = tmp_manager
        # Create a published memory and embed it so the vector store has it.
        mem = _add(mgr, "published omega content", status=MemoryStatus.PUBLISHED)
        mgr.vectors.upsert(mem.id, [0.1] * 384)

        # Search with explicit status=ARCHIVED — vector leg returns the
        # published memory's id but it gets filtered out (status mismatch).
        results = mgr.search("omega", status=MemoryStatus.ARCHIVED)
        # No results because no archived entries exist.
        assert results == [], "no archived entries should match"
        # Even with no results, the search_type on the (empty) result list
        # is not directly observable — but we can verify the logic by
        # checking that a search where vector pairs exist but are filtered
        # does not claim "hybrid". We verify via a scenario that DOES
        # return FTS results but filters out the vector leg.

    def test_search_type_fts_only_when_vector_filtered_by_default(self, tmp_manager):
        """Vector leg returns a raw memory but default filter drops it → fts_only.

        Scenario: a raw memory is somehow in the vector store. Default
        search (include_raw=False) filters it out of the vector leg. A
        published memory matches via FTS. search_type must be "fts_only"
        because the vector pair was filtered out and did not contribute.
        """
        mgr = tmp_manager
        # Published memory — surfaces via FTS.
        _add(mgr, "published sigma content", status=MemoryStatus.PUBLISHED)
        # Raw memory — in the vector store but filtered by default gating.
        raw_mem = _add(mgr, "raw sigma content", status=MemoryStatus.RAW)
        mgr.vectors.upsert(raw_mem.id, [0.1] * 384)

        results = mgr.search("sigma", include_raw=False)
        assert len(results) > 0, "published memory should surface via FTS"
        # The raw vector entry was filtered out → search_type is fts_only.
        assert results[0].search_type == "fts_only", (
            "search_type must be 'fts_only' when vector pairs were filtered out "
            "and did not contribute to the final result set"
        )

    def test_search_type_hybrid_when_vector_contributes(self, tmp_manager):
        """Vector leg contributes a NEW result (not in FTS) → search_type='hybrid'.

        Scenario: memory A matches FTS on "tau". Memory B does NOT match
        FTS on "tau" (different text) but is in the vector store. The vector
        leg returns B, which is not in fts_ids → vector_contributed=True.
        """
        mgr = tmp_manager
        # Memory A — matches FTS query "tau".
        _add(mgr, "published tau content", status=MemoryStatus.PUBLISHED)
        # Memory B — does NOT match FTS "tau" but is in the vector store.
        # With a mock embedder returning the same vector, vector search
        # returns all stored vectors regardless of query text.
        mem_b = _add(mgr, "published completely different text", status=MemoryStatus.PUBLISHED)
        mgr.vectors.upsert(mem_b.id, [0.1] * 384)

        results = mgr.search("tau", include_raw=False)
        assert len(results) > 0
        # The vector leg contributed mem_b which is not in fts_ids → hybrid.
        assert results[0].search_type == "hybrid", (
            "search_type must be 'hybrid' when the vector leg contributed a "
            "result that was not already found by FTS"
        )


# ---------------------------------------------------------------------------
# LOW-2 — invalid status string in MCP tool
# ---------------------------------------------------------------------------


class TestMcpSearchInvalidStatus:
    async def test_mcp_search_invalid_status_error(self):
        """vesma_search with status='invalid' → error listing valid values."""
        from vesmaro.mcp_server import _dispatch

        result = await _dispatch(
            "vesma_search",
            {"query": "test", "status": "invalid"},
        )
        assert isinstance(result, str), "error return must be a str"
        assert result.startswith("❌"), "error must start with ❌"
        assert "invalid" in result, "error must mention the invalid value"
        # Must list all valid MemoryStatus values.
        assert "raw" in result
        assert "processing" in result
        assert "processed" in result
        assert "published" in result
        assert "archived" in result

    async def test_mcp_search_valid_status_passes_through(self):
        """vesma_search with a valid status string does not error."""
        from vesmaro.mcp_server import _dispatch

        mock_mgr = MagicMock()
        mock_mgr.search.return_value = []
        mock_mgr.settings.mnemos.strict_tag_contract = False
        with patch("vesmaro.mcp_server.get_manager", return_value=mock_mgr):
            result = await _dispatch(
                "vesma_search",
                {"query": "test", "status": "archived"},
            )
        # Should not be an error string — should be a list (empty results).
        assert not (isinstance(result, str) and result.startswith("❌")), (
            "valid status must not produce an error"
        )
        # search was called with MemoryStatus.ARCHIVED.
        mock_mgr.search.assert_called_once()
        call_kwargs = mock_mgr.search.call_args
        assert call_kwargs.kwargs["status"] == MemoryStatus.ARCHIVED


# ---------------------------------------------------------------------------
# LOW-3 — orphaned vectors in search_health
# ---------------------------------------------------------------------------


class TestStatsOrphanedVectors:
    def test_stats_orphaned_vectors_true(self, tmp_manager):
        """Vectors exist but no published memories → orphaned_vectors=True."""
        mgr = tmp_manager
        # Add a raw memory (not published) and embed it.
        mem = _add(mgr, "raw eta content", status=MemoryStatus.RAW)
        mgr.vectors.upsert(mem.id, [0.1] * 384)

        stats = mgr.stats()
        search_health = stats["search_health"]
        assert search_health["orphaned_vectors"] is True, (
            "vectors exist but published_count==0 → orphaned_vectors must be True"
        )

    def test_stats_orphaned_vectors_false_when_published_exists(self, tmp_manager):
        """Published memories + vectors → orphaned_vectors=False."""
        mgr = tmp_manager
        mem = _add(mgr, "published eta content", status=MemoryStatus.PUBLISHED)
        mgr.vectors.upsert(mem.id, [0.1] * 384)

        stats = mgr.stats()
        assert stats["search_health"]["orphaned_vectors"] is False

    def test_stats_orphaned_vectors_false_when_no_vectors(self, tmp_manager):
        """No vectors → orphaned_vectors=False (nothing to be orphaned)."""
        mgr = tmp_manager
        _add(mgr, "published eta content", status=MemoryStatus.PUBLISHED)

        stats = mgr.stats()
        assert stats["search_health"]["orphaned_vectors"] is False


# ---------------------------------------------------------------------------
# LOW-4 — tag normalization strips spaces
# ---------------------------------------------------------------------------


class TestTagNormalizationStripsSpaces:
    def test_tag_normalization_strips_spaces(self):
        """validate_tag_contract strips leading/trailing spaces before normalization.

        `project: My Project ` → `project:my-project` (no leading/trailing
        hyphens from unstripped spaces).
        """
        result = validate_tag_contract(
            ["project: My Project ", "agent: a", "mnemos:learning"],
            strict=False,
        )
        assert "project:my-project" in result, (
            "leading/trailing spaces must be stripped before space→hyphen; "
            "expected 'project:my-project' in result"
        )
        assert "project:-my-project-" not in result, (
            "unstripped spaces must NOT produce leading/trailing hyphens"
        )

    def test_tag_normalization_strips_spaces_agent(self):
        """agent: tags also get stripped."""
        result = validate_tag_contract(
            ["project:p", "agent: My Agent ", "mnemos:learning"],
            strict=False,
        )
        assert "agent:my-agent" in result
        assert "agent:-my-agent-" not in result


# ---------------------------------------------------------------------------
# LOW-4 (CLI) — tags normalize strips spaces
# ---------------------------------------------------------------------------


class TestTagsNormalizeCliStripsSpaces:
    def test_tags_normalize_cli_strips_spaces(self, tmp_path: Path, monkeypatch):
        """`tags normalize` strips leading/trailing spaces in slugs.

        Adds an entry with `project: My Project ` then runs `tags normalize`
        and verifies the stored tag is `project:my-project` (no
        leading/trailing hyphens).
        """
        from typer.testing import CliRunner

        from vesmaro.cli._manager import reset_manager
        from vesmaro.cli.main import app

        reset_manager()
        cfg = tmp_path / "vesmaro.yaml"
        cfg.write_text(
            f"mnemos:\n"
            f"  vault_path: {tmp_path / 'vault'}\n"
            f"  data_dir: {tmp_path / 'data'}\n"
            f"  db_name: cli-normalize.db\n"
            f"embedding:\n"
            f"  provider: nano\n"
        )
        monkeypatch.setenv("VESMARO_CONFIG", str(cfg))

        runner = CliRunner()

        # Add a memory with a tag that has trailing space.
        add_result = runner.invoke(
            app,
            [
                "add",
                "content for normalize test",
                "--tags",
                "project: My Project ,agent:cli,mnemos:test",
            ],
        )
        assert add_result.exit_code == 0, add_result.output

        # Run tags normalize.
        norm_result = runner.invoke(app, ["tags", "normalize"])
        assert norm_result.exit_code == 0, norm_result.output
        assert "Traceback" not in norm_result.output

        # Verify the stored tag was normalized correctly (no leading/trailing
        # hyphens). Read directly from the manager's SQLite store.
        from vesmaro.cli._manager import get_manager

        mgr = get_manager()
        memories = mgr.sqlite.list_all(limit=100, offset=0)
        normalize_mems = [m for m in memories if "normalize test" in m.content]
        assert len(normalize_mems) == 1, "the added memory must be present"
        tags = normalize_mems[0].tags
        assert "project:my-project" in tags, f"expected 'project:my-project' in {tags}"
        assert "project:-my-project-" not in tags
        assert "project:my-project-" not in tags
        assert "project:-my-project" not in tags

        reset_manager()
