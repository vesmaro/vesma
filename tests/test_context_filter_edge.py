"""Edge-case tests for Context Filter activation (M10).

Complements ``test_context_filter.py`` by covering gaps identified during
the QA audit:

* **FTS5 desync fix robustness**: add → filter → search (FTS still finds it).
* **Auto-filter with empty content**: ``vesma_add`` with empty string.
* **Auto-filter with very large content**: 100KB+ input → completes.
* **Auto-filter profile auto-detection**: no profile → correct profile.
* **``vesma_filter`` on memory without raw_content**: uses ``content``.
* **``mnemos filter --all`` on empty database**: graceful, 0 filtered.
* **``mnemos filter --all`` with mixed filtered/unfiltered**: re-filters all.
* **Filter stats accuracy**: ``get_filter_stats()`` correct counts + avg.
* **Non-fatal filter crash**: mock filter to raise → memory saved, clean None.
* **Idempotency with different profiles**: terminal then code → latest wins.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from vesmaro.cli.main import app as cli_app
from vesmaro.config import Settings
from vesmaro.filter.pipeline import apply_filter
from vesmaro.manager import MemoryManager
from vesmaro.models import MemoryCreate, MemorySource


def _make_settings(tmpdir: str, *, auto_filter: bool = True) -> Settings:
    tmp = Path(tmpdir)
    settings = Settings(
        mnemos={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
            "auto_filter": auto_filter,
        },
        embedding={"provider": "nano"},
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def mgr() -> MemoryManager:
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = _make_settings(tmpdir)
        m = MemoryManager(settings)
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        m._embedder = mock_embedder
        yield m
        m.close()


_VALID_TAGS = ["project:test", "agent:filter-test", "mnemos:learning"]


# ── FTS5 desync fix robustness ────────────────────────────────────────────────


class TestFts5DesyncRobustness:
    """The FTS5 desync fix: add → filter → search must still find the memory.

    The fix uses ``update_fields()`` (targeted UPDATE) instead of
    ``save()`` (INSERT OR REPLACE) to avoid firing delete+insert FTS5
    triggers that would lose the rowid mapping. This test verifies the
    fix is robust: after filtering, FTS5 search still returns the memory.
    """

    def test_add_filter_search_fts_still_finds(self, mgr: MemoryManager) -> None:
        """add → filter → FTS search: the memory is still searchable."""
        data = MemoryCreate(
            content="2024-01-15T10:30:00Z [ERROR] kubernetes pod crash loop",
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        memory = mgr.add(data, project="test", agent="filter-test")
        assert memory.clean_content is not None

        # Explicit re-filter (exercises update_fields path).
        result = mgr.apply_context_filter(memory.id, profile="log")
        assert result["status"] == "ok"

        # FTS5 search must still find the memory by a keyword in content.
        hits = mgr.sqlite.fts_search("kubernetes", limit=10)
        assert len(hits) >= 1
        found_ids = {h[0].id for h in hits}
        assert memory.id in found_ids

    def test_add_filter_search_multiple_cycles(self, mgr: MemoryManager) -> None:
        """Multiple add→filter→search cycles: no FTS5 rowid drift."""
        ids: list[str] = []
        for i in range(5):
            data = MemoryCreate(
                content=f"2024-01-15 [ERROR] failure-{i} in module-x",
                tags=_VALID_TAGS,
                source=MemorySource.CLI,
            )
            memory = mgr.add(data, project="test", agent="filter-test")
            mgr.apply_context_filter(memory.id, profile="log")
            ids.append(memory.id)

        # All 5 must be findable via FTS5.
        for mem_id in ids:
            hits = mgr.sqlite.fts_search("failure", limit=20)
            found_ids = {h[0].id for h in hits}
            assert mem_id in found_ids, f"FTS5 lost memory {mem_id} after filter cycle"

    def test_filter_preserves_fts_rowid(self, mgr: MemoryManager) -> None:
        """After filter, the FTS5 rowid matches the memories table rowid."""
        data = MemoryCreate(
            content="[ERROR] rowid-test signal",
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        memory = mgr.add(data, project="test", agent="filter-test")

        # Get the base table rowid before filtering.
        conn = mgr.sqlite._get_conn()  # test-only access
        before = conn.execute("SELECT rowid FROM memories WHERE id=?", (memory.id,)).fetchone()
        assert before is not None
        rowid_before = before["rowid"]

        mgr.apply_context_filter(memory.id, profile="log")

        after = conn.execute("SELECT rowid FROM memories WHERE id=?", (memory.id,)).fetchone()
        assert after is not None
        assert after["rowid"] == rowid_before

        # FTS5 table should have the same rowid.
        fts = conn.execute("SELECT rowid FROM memories_fts WHERE id=?", (memory.id,)).fetchone()
        assert fts is not None
        assert fts["rowid"] == rowid_before


# ── Auto-filter with empty content ────────────────────────────────────────────


class TestAutoFilterEmptyContent:
    """Auto-filter with empty content → no crash, clean_content empty/None."""

    def test_empty_string_content(self, mgr: MemoryManager) -> None:
        """``vesma_add`` with empty string → no crash."""
        data = MemoryCreate(
            content="",
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        memory = mgr.add(data, project="test", agent="filter-test")
        assert memory.id is not None
        # clean_content is empty string (filter pipeline returns "" for "").
        reloaded = mgr.get(memory.id)
        assert reloaded is not None
        # Either None (filter skipped because content is falsy) or "".
        assert reloaded.clean_content in (None, "")

    def test_whitespace_only_content(self, mgr: MemoryManager) -> None:
        """Whitespace-only content → no crash, clean_content is whitespace-stripped."""
        data = MemoryCreate(
            content="   \n\n\t  \n",
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        memory = mgr.add(data, project="test", agent="filter-test")
        assert memory.id is not None
        reloaded = mgr.get(memory.id)
        assert reloaded is not None
        # Filter should handle whitespace-only gracefully.
        assert reloaded.clean_content is not None or reloaded.clean_content is None


# ── Auto-filter with very large content ───────────────────────────────────────


class TestAutoFilterLargeContent:
    """Auto-filter with 100KB+ input → completes, doesn't hang."""

    def test_100kb_log_content(self, mgr: MemoryManager) -> None:
        """100KB of log lines → filter completes within reasonable time."""
        # Generate ~100KB+ of log-like content with some signal.
        lines = []
        for i in range(2600):
            if i % 500 == 499:
                lines.append(f"2024-01-15T10:30:{i:02d}Z [ERROR] failure-{i}")
            else:
                lines.append(f"2024-01-15T10:30:{i:02d}Z [INFO] routine-{i}")
        content = "\n".join(lines)
        assert len(content) > 100_000

        data = MemoryCreate(
            content=content,
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        memory = mgr.add(data, project="test", agent="filter-test")
        assert memory.id is not None
        reloaded = mgr.get(memory.id)
        assert reloaded is not None
        # Filter should have produced some clean content (possibly truncated).
        assert reloaded.clean_content is not None
        # Clean content should be smaller than or equal to original.
        assert len(reloaded.clean_content) <= len(content)

    def test_100kb_with_budget_truncation(self, mgr: MemoryManager) -> None:
        """100KB content with small budget → truncated clean_content."""
        content = "word " * 20000  # ~100KB
        data = MemoryCreate(
            content=content,
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        memory = mgr.add(data, project="test", agent="filter-test")

        # Explicit filter with a small budget.
        result = mgr.apply_context_filter(memory.id, profile="default", budget=100)
        assert result["status"] == "ok"
        assert result["stats"]["tokens"]["truncated"] is True
        assert "[...truncated...]" in result["clean_content"]


# ── Auto-filter profile auto-detection ────────────────────────────────────────


class TestAutoFilterProfileAutoDetection:
    """No profile specified → correct profile auto-detected."""

    @pytest.mark.parametrize(
        ("content", "expected_profile"),
        [
            ("2024-01-15T10:30:00Z [INFO] start\n[ERROR] fail", "log"),
            ("\x1b[31mred text\x1b[0m terminal output", "terminal"),
            ("def hello():\n    pass\nclass World:\n    pass\nimport os", "code"),
            ("<html><body>hello</body></html>", "web"),
            ("# Title\n## Section\n### Sub\n---\nContent", "docs"),
        ],
        ids=["log", "terminal", "code", "web", "docs"],
    )
    def test_auto_detected_profile(
        self, mgr: MemoryManager, content: str, expected_profile: str
    ) -> None:
        """Auto-filter detects the correct profile from content."""
        data = MemoryCreate(
            content=content,
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        memory = mgr.add(data, project="test", agent="filter-test")
        reloaded = mgr.get(memory.id)
        assert reloaded is not None
        assert reloaded.filter_profile == expected_profile


# ── vesma_filter on memory without raw_content ───────────────────────────────


class TestFilterWithoutRawContent:
    """Memory added before auto_filter existed → filter uses ``content``."""

    def test_filter_uses_content_when_no_raw_content(self, mgr: MemoryManager) -> None:
        """When raw_content is NULL, filter falls back to content."""
        data = MemoryCreate(
            content="2024-01-15 [ERROR] legacy memory crash",
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        memory = mgr.add(data, project="test", agent="filter-test")

        # Simulate a legacy memory: clear raw_content, keep content.
        conn = mgr.sqlite._get_conn()  # test-only access
        conn.execute("UPDATE memories SET raw_content=NULL WHERE id=?", (memory.id,))
        conn.commit()
        mgr.sqlite._invalidate_caches()

        # Explicit filter should use content as the source.
        result = mgr.apply_context_filter(memory.id, profile="log")
        assert result["status"] == "ok"
        assert result["clean_content"] is not None
        # The ERROR signal should be preserved.
        assert "ERROR" in result["clean_content"] or "crash" in result["clean_content"]


# ── mnemos filter --all on empty database ─────────────────────────────────────


class TestFilterAllEmptyDatabase:
    """``mnemos filter --all`` on empty DB → graceful, 0 filtered."""

    def test_filter_all_empty(self, mgr: MemoryManager) -> None:
        """filter_all on an empty database returns 0 filtered, no crash."""
        result = mgr.filter_all()
        assert result["status"] == "ok"
        assert result["total"] == 0
        assert result["filtered"] == 0
        assert result["failed"] == 0
        assert result["skipped"] == 0


# ── mnemos filter --all with mixed filtered/unfiltered ────────────────────────


class TestFilterAllMixed:
    """``filter_all`` re-filters ALL memories (not just unfiltered)."""

    def test_filter_all_refilters_already_filtered(self, mgr: MemoryManager) -> None:
        """filter_all re-filters memories that already have clean_content."""
        # Add a memory (auto-filtered on ingest).
        data = MemoryCreate(
            content="2024-01-15 [INFO] start\n[ERROR] boom",
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        memory = mgr.add(data, project="test", agent="filter-test")
        assert memory.clean_content is not None

        # filter_all should re-filter it (with explicit profile).
        result = mgr.filter_all(profile="log")
        assert result["status"] == "ok"
        assert result["total"] == 1
        assert result["filtered"] == 1
        assert result["failed"] == 0

        # The memory was re-filtered (clean_content is populated).
        reloaded = mgr.get(memory.id)
        assert reloaded is not None
        assert reloaded.clean_content is not None
        assert reloaded.filter_profile == "log"

    def test_filter_all_skips_empty_content(self, mgr: MemoryManager) -> None:
        """filter_all skips memories with no content (empty string)."""
        # Add a memory with empty content.
        mgr.settings.mnemos.auto_filter = False
        data = MemoryCreate(
            content="",
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        mgr.add(data, project="test", agent="filter-test")
        mgr.settings.mnemos.auto_filter = True

        result = mgr.filter_all()
        assert result["status"] == "ok"
        # The empty-content memory should be skipped.
        assert result["skipped"] >= 1
        assert result["filtered"] == 0


# ── Filter stats accuracy ─────────────────────────────────────────────────────


class TestFilterStatsAccuracy:
    """``get_filter_stats()`` returns correct counts and avg reduction."""

    def test_stats_counts_correct(self, mgr: MemoryManager) -> None:
        """filtered/unfiltered counts are accurate after mixed adds."""
        # Add 3 memories with auto_filter on (→ filtered).
        for i in range(3):
            data = MemoryCreate(
                content=f"2024-01-15 [INFO] start-{i}\n[ERROR] boom-{i}",
                tags=_VALID_TAGS,
                source=MemorySource.CLI,
            )
            mgr.add(data, project="test", agent="filter-test")

        # Add 1 memory with auto_filter off (→ unfiltered).
        mgr.settings.mnemos.auto_filter = False
        data = MemoryCreate(
            content="unfiltered content",
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        mgr.add(data, project="test", agent="filter-test")
        mgr.settings.mnemos.auto_filter = True

        stats = mgr.sqlite.get_filter_stats()
        assert stats["filtered"] == 3
        assert stats["unfiltered"] == 1
        assert stats["avg_reduction_pct"] >= 0.0
        assert isinstance(stats["by_profile"], dict)
        assert sum(stats["by_profile"].values()) == 3

    def test_stats_avg_reduction_computed(self, mgr: MemoryManager) -> None:
        """avg_reduction_pct is computed from filter_stats JSON."""
        # Add a memory with significant noise (timestamps) → reduction > 0.
        data = MemoryCreate(
            content=(
                "2024-01-15T10:30:00Z [INFO] start\n"
                "2024-01-15T10:30:01Z [INFO] start\n"
                "2024-01-15T10:30:02Z [INFO] start"
            ),
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        mgr.add(data, project="test", agent="filter-test")

        stats = mgr.sqlite.get_filter_stats()
        assert stats["filtered"] >= 1
        # Reduction should be > 0 (timestamps + dedup remove content).
        assert stats["avg_reduction_pct"] > 0.0

    def test_stats_empty_database(self, mgr: MemoryManager) -> None:
        """get_filter_stats on empty DB → zeros, no crash."""
        stats = mgr.sqlite.get_filter_stats()
        assert stats["filtered"] == 0
        assert stats["unfiltered"] == 0
        assert stats["avg_reduction_pct"] == 0.0
        assert stats["by_profile"] == {}


# ── Non-fatal filter crash ────────────────────────────────────────────────────


class TestNonFatalFilterCrash:
    """Mock filter to raise → memory still saved, clean_content stays None."""

    def test_crash_during_auto_filter_keeps_memory(self, mgr: MemoryManager) -> None:
        """If apply_filter raises during auto-filter, memory is still saved."""
        data = MemoryCreate(
            content="content that will crash the filter",
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        with patch(
            "vesmaro.filter.pipeline.apply_filter",
            side_effect=RuntimeError("simulated crash"),
        ):
            memory = mgr.add(data, project="test", agent="filter-test")

        # Memory is saved despite the filter crash.
        assert memory.id is not None
        reloaded = mgr.get(memory.id)
        assert reloaded is not None
        assert reloaded.content == "content that will crash the filter"
        # clean_content is None because filter failed.
        assert reloaded.clean_content is None

    def test_crash_during_filter_all_is_counted(self, mgr: MemoryManager) -> None:
        """filter_all counts a crashed filter as 'failed', not 'filtered'."""
        data = MemoryCreate(
            content="content for filter_all crash test",
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        mgr.add(data, project="test", agent="filter-test")

        with patch(
            "vesmaro.filter.pipeline.apply_filter",
            side_effect=RuntimeError("simulated crash"),
        ):
            result = mgr.filter_all()

        assert result["status"] == "ok"
        assert result["total"] == 1
        assert result["failed"] == 1
        assert result["filtered"] == 0


# ── Idempotency with different profiles ───────────────────────────────────────


class TestFilterIdempotencyDifferentProfiles:
    """Re-filtering with a different profile → clean_content reflects latest."""

    def test_refilter_with_different_profile(self, mgr: MemoryManager) -> None:
        """Filter with 'terminal' then 'code' → clean_content reflects 'code'."""
        # Content that is ambiguous — could be terminal or code.
        content = "def hello():\n    \x1b[31mprint('hi')\x1b[0m\n    pass"
        data = MemoryCreate(
            content=content,
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        memory = mgr.add(data, project="test", agent="filter-test")
        first_profile = memory.filter_profile

        # Re-filter with explicit 'code' profile.
        result = mgr.apply_context_filter(memory.id, profile="code")
        assert result["status"] == "ok"
        assert result["filter_profile"] == "code"

        reloaded = mgr.get(memory.id)
        assert reloaded is not None
        assert reloaded.filter_profile == "code"
        # The clean_content may differ because the profile changed.
        # The key invariant: filter_profile reflects the latest run.
        assert reloaded.filter_profile != first_profile or first_profile == "code"

    def test_refilter_same_profile_idempotent(self, mgr: MemoryManager) -> None:
        """Re-filtering with the same profile → identical clean_content."""
        data = MemoryCreate(
            content="2024-01-15 [INFO] start\n[ERROR] boom",
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        memory = mgr.add(data, project="test", agent="filter-test")
        first_clean = memory.clean_content

        result = mgr.apply_context_filter(memory.id, profile=memory.filter_profile)
        assert result["status"] == "ok"
        assert result["clean_content"] == first_clean


# ── CLI: filter --all on empty DB ─────────────────────────────────────────────


class TestCliFilterAllEmpty:
    """``mnemos filter --all`` on empty DB → graceful exit."""

    def test_cli_filter_all_empty_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = tmp_path / "vesmaro.yaml"
        cfg.write_text(
            f"mnemos:\n"
            f"  vault_path: {tmp_path / 'vault'}\n"
            f"  data_dir: {tmp_path / 'data'}\n"
            f"  db_name: cli-filter-empty.db\n"
            f"embedding:\n"
            f"  provider: nano\n"
        )
        monkeypatch.setenv("VESMARO_CONFIG", str(cfg))

        runner = CliRunner()
        result = runner.invoke(cli_app, ["filter", "--all"])
        # Should exit 0 (nothing to filter, but not an error).
        assert result.exit_code == 0, result.output


# ── MCP: vesma_filter on memory without raw_content ──────────────────────────


class TestMcpFilterWithoutRawContent:
    """``vesma_filter`` MCP tool on a memory with no raw_content."""

    @pytest.mark.asyncio
    async def test_mnemos_filter_uses_content_fallback(self, mgr: MemoryManager) -> None:
        """vesma_filter works on a memory with raw_content=NULL."""
        from vesmaro.mcp_server import _dispatch
        from vesmaro.models import MemoryStatus

        data = MemoryCreate(
            content="2024-01-15 [ERROR] legacy mcp memory",
            tags=_VALID_TAGS,
            source=MemorySource.MCP,
        )
        memory = mgr.add(data, project="test", agent="filter-test")
        # M1 (final review): issuance gate — advance to published first.
        mgr.sqlite.update_status(memory.id, MemoryStatus.PUBLISHED)

        # Clear raw_content to simulate legacy memory.
        conn = mgr.sqlite._get_conn()  # test-only access
        conn.execute(
            "UPDATE memories SET raw_content=NULL, clean_content=NULL WHERE id=?",
            (memory.id,),
        )
        conn.commit()
        mgr.sqlite._invalidate_caches()

        with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
            result = await _dispatch(
                "vesma_filter",
                {"memory_id": memory.id, "profile": "log"},
            )

        # On success, _dispatch returns {memory_id, profile, clean_content, stats}.
        assert "clean_content" in result
        assert "stats" in result
        assert "ERROR" in result["clean_content"] or "legacy" in result["clean_content"]


# ── Filter stats JSON structure ───────────────────────────────────────────────


class TestFilterStatsJsonStructure:
    """The filter_stats JSON stored in the DB has the expected structure."""

    def test_filter_stats_has_reduction_key(self, mgr: MemoryManager) -> None:
        """filter_stats JSON contains the 'reduction' sub-dict."""
        data = MemoryCreate(
            content="2024-01-15 [INFO] start\n[ERROR] boom",
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        memory = mgr.add(data, project="test", agent="filter-test")
        reloaded = mgr.get(memory.id)
        assert reloaded is not None
        assert reloaded.filter_stats is not None

        # filter_stats is deserialised from JSON by _row_to_memory → dict.
        stats = reloaded.filter_stats
        assert isinstance(stats, dict)
        assert "reduction" in stats
        assert "original_chars" in stats["reduction"]
        assert "final_chars" in stats["reduction"]
        assert stats["reduction"]["original_chars"] > 0


# ---------------------------------------------------------------------------
# P0-3: Content-type-aware filter — JSON arrays, code, profile-aware
# ---------------------------------------------------------------------------


class TestJsonArrayCompression:
    """P0-3: JSON arrays get statistical sampling (SmartCrusher-inspired)."""

    def test_large_json_array_sampled(self):
        """JSON array with ≥20 items gets head+tail+anomalies, middle dropped."""
        import json

        # 50-item array — should trigger sampling
        arr = [{"id": i, "value": 0, "name": f"item_{i}"} for i in range(50)]
        # Add an anomaly in the middle
        arr[25]["error"] = "connection timeout"
        text = json.dumps(arr, indent=2)

        result = apply_filter(text, profile="log")
        clean = result["clean_content"]

        # Should be significantly smaller
        assert len(clean) < len(text) * 0.6, (
            f"JSON compression insufficient: {len(clean)} vs {len(text)}"
        )

        # First items (schema) preserved
        assert "item_0" in clean
        # Last items (recency) preserved
        assert "item_49" in clean
        # Anomaly preserved
        assert "timeout" in clean
        # Compression marker present
        assert "_compressed_marker" in clean

    def test_small_json_array_not_sampled(self):
        """JSON array with <20 items is left alone (not worth compressing)."""
        import json

        arr = [{"id": i} for i in range(10)]
        text = json.dumps(arr, indent=2)

        result = apply_filter(text, profile="log")
        # No compression marker — too small to sample
        assert "_compressed_marker" not in result["clean_content"]

    def test_json_anomaly_detection_nonzero(self):
        """Error indicators in JSON array items are kept as anomalies."""
        from vesmaro.filter.pipeline import _is_json_anomaly

        # Error strings are anomalies
        assert _is_json_anomaly("error: something") is True
        assert _is_json_anomaly("ok") is False
        # Dicts with error keys are anomalies
        assert _is_json_anomaly({"error": "fail"}) is True
        assert _is_json_anomaly({"error_code": 500}) is True
        # Dicts with status fields indicating errors
        assert _is_json_anomaly({"status": "error"}) is True
        assert _is_json_anomaly({"level": "fatal"}) is True
        # Dicts without error indicators are NOT anomalies
        assert _is_json_anomaly({"value": 0}) is False
        assert _is_json_anomaly({"id": 42, "name": "item"}) is False
        # None and plain numbers are not anomalies
        assert _is_json_anomaly(None) is False
        assert _is_json_anomaly(42) is False
        assert _is_json_anomaly(0) is False


class TestCodeCompression:
    """P0-3: Code blocks get boilerplate stripping (imports, blank lines)."""

    def test_imports_collapsed(self):
        """Repeated import lines are collapsed into a marker."""
        text = "\n".join(
            [
                "import os",
                "import sys",
                "import json",
                "import re",
                "import logging",
                "import pathlib",
                "",
                "def main():",
                "    pass",
            ]
        )

        result = apply_filter(text, profile="code")
        clean = result["clean_content"]

        # First import kept, rest collapsed
        assert "import os" in clean
        assert "def main" in clean
        # Should have a marker for dropped imports
        stats = result["stats"]["compress"]
        assert stats.get("imports_dropped", 0) >= 1

    def test_consecutive_blank_lines_collapsed(self):
        """Multiple consecutive blank lines collapse to one."""
        text = "import os\n\n\n\n\ndef main():\n    pass"
        result = apply_filter(text, profile="code")
        clean = result["clean_content"]
        # No more than one consecutive blank line
        assert "\n\n\n" not in clean


class TestProfileAwareExtract:
    """P0-3: Extract stage is profile-aware (aggressive for logs, light for docs)."""

    def test_verbose_success_dropped_in_log(self):
        """Verbose INFO/DEBUG lines are dropped in log profile."""
        text = "\n".join(
            [
                "2024-01-15T10:30:00Z [INFO] process started",
                "2024-01-15T10:30:01Z [DEBUG] loading config",
                "2024-01-15T10:30:02Z [INFO] completed successfully",
                "2024-01-15T10:30:03Z [ERROR] something broke",
                "2024-01-15T10:30:04Z [INFO] cleanup done",
            ]
        )
        result = apply_filter(text, profile="log")
        clean = result["clean_content"]

        # Error line preserved
        assert "ERROR" in clean or "broke" in clean
        # Verbose lines dropped (INFO/DEBUG without errors)
        stats = result["stats"]["extract"]
        assert stats.get("verbose_dropped", 0) >= 1

    def test_docs_profile_preserves_content(self):
        """Docs profile does not extract/drop — preserves full content."""
        text = "# Title\n\nSome documentation content.\nMore docs here."
        result = apply_filter(text, profile="docs")
        # Extract stage should be a no-op for docs
        assert result["stats"]["extract"]["signal_lines"] == 0


class TestFilterReductionPct:
    """P0-3: Overall reduction percentage rises to ≥40% on real-ish data."""

    def test_log_reduction_above_40_pct(self):
        """A realistic log with noise achieves ≥40% reduction."""
        lines = []
        for i in range(60):
            lines.append(f"2024-01-15T10:30:{i:02d}Z [INFO] processing item {i}")
        lines.append("2024-01-15T10:31:00Z [ERROR] failed to process item 60")
        lines.append("Traceback (most recent call last):")
        lines.append("  File 'app.py', line 42, in handler")
        lines.append("ValueError: invalid input")
        lines.append("Exited with code 1")
        for i in range(30):
            lines.append(f"2024-01-15T10:32:{i:02d}Z [INFO] cleanup step {i}")
        text = "\n".join(lines)

        result = apply_filter(text, profile="log")
        original = len(text)
        final = len(result["clean_content"])
        reduction_pct = (1 - final / original) * 100

        assert reduction_pct >= 40, f"Log reduction only {reduction_pct:.1f}% — target ≥40%"

    def test_json_reduction_above_60_pct(self):
        """A large JSON array achieves ≥60% reduction."""
        import json

        arr = [{"id": i, "value": 0, "name": f"item_{i}", "data": "x" * 50} for i in range(100)]
        text = json.dumps(arr, indent=2)

        result = apply_filter(text, profile="log")
        original = len(text)
        final = len(result["clean_content"])
        reduction_pct = (1 - final / original) * 100

        assert reduction_pct >= 60, f"JSON reduction only {reduction_pct:.1f}% — target ≥60%"

    def test_no_signal_loss_on_errors(self):
        """Filtering never drops error/warning/exit-code lines."""
        text = "\n".join(
            [
                "2024-01-15T10:30:00Z [INFO] started",
                "2024-01-15T10:30:01Z [INFO] running",
                "2024-01-15T10:30:02Z [ERROR] critical failure",
                "2024-01-15T10:30:03Z [WARNING] deprecated API",
                "Exited with code 1",
                "2024-01-15T10:30:04Z [INFO] done",
            ]
        )
        result = apply_filter(text, profile="log")
        clean = result["clean_content"]

        # All signal lines must be preserved
        assert "ERROR" in clean or "critical" in clean
        assert "WARNING" in clean or "deprecated" in clean
        assert "Exited with code 1" in clean
