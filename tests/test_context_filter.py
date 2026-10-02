"""Tests for M10 — Context Filter pipeline."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from vesmaro.cli.main import app as cli_app
from vesmaro.config import Settings
from vesmaro.filter.pipeline import (
    _stage_compress,
    _stage_dedup,
    _stage_extract,
    _stage_noise,
    _stage_tokens,
    apply_filter,
    detect_profile,
)
from vesmaro.manager import MemoryManager
from vesmaro.models import MemoryCreate, MemorySource


class TestStageDedup:
    def test_removes_exact_duplicates(self):
        text = "line a\nline b\nline a\nline c"
        result, stats = _stage_dedup(text)
        assert "line a" in result
        assert "line b" in result
        assert "line c" in result
        assert result.count("line a") == 1
        assert stats["exact_dups"] == 1

    def test_removes_near_duplicates(self):
        text = "Error: foo\nError: bar\nError: foo"
        result, stats = _stage_dedup(text)
        assert result.count("Error: foo") == 1
        assert stats["exact_dups"] == 1

    def test_preserves_empty_lines(self):
        text = "line a\n\nline b"
        result, stats = _stage_dedup(text)
        assert "\n\n" in result
        assert stats["exact_dups"] == 0

    def test_empty_input(self):
        result, stats = _stage_dedup("")
        assert result == ""
        assert stats["lines_in"] == 0


class TestStageNoise:
    def test_removes_ansi_codes(self):
        text = "\x1b[31mred text\x1b[0m normal"
        result, stats = _stage_noise(text, "terminal")
        assert "\x1b[" not in result
        assert stats["removed_ansi"] == 2

    def test_removes_progress_bars(self):
        text = "[####      ] 40%\nDone"
        result, stats = _stage_noise(text, "terminal")
        assert "40%" not in result
        assert stats["removed_progress"] == 1

    def test_removes_timestamps(self):
        text = "2024-01-15T10:30:00Z Starting process\nDone"
        result, stats = _stage_noise(text, "log")
        assert "2024-01-15" not in result
        assert stats["removed_timestamps"] == 1

    def test_removes_separators(self):
        text = "Header\n---\nContent"
        result, stats = _stage_noise(text, "log")
        assert "---" not in result
        assert stats["removed_separators"] == 1

    def test_collapses_blank_lines(self):
        text = "a\n\n\n\nb"
        result, _ = _stage_noise(text, "default")
        assert "\n\n\n" not in result
        assert "a\n\nb" in result

    def test_preserves_content_in_code_profile(self):
        text = "Header\n---\nContent"
        result, _stats = _stage_noise(text, "code")
        # Separators are NOT removed in code profile (only log/terminal/default)
        assert "---" in result


class TestStageExtract:
    def test_extracts_error_lines(self):
        text = "line 1\nError: something broke\nline 3"
        result, stats = _stage_extract(text, "log")
        assert "Error: something broke" in result
        assert stats["signal_lines"] > 0

    def test_extracts_warning_lines(self):
        text = "line 1\nWarning: deprecated\nline 3"
        result, stats = _stage_extract(text, "log")
        assert "Warning: deprecated" in result
        assert stats["signal_lines"] > 0

    def test_extracts_exit_codes(self):
        text = "line 1\nExited with code 1\nline 3"
        result, stats = _stage_extract(text, "log")
        assert "Exited with code 1" in result
        assert stats["signal_lines"] > 0

    def test_includes_context_around_signals(self):
        text = "before\nline 1\nError: broke\nline 3\nafter"
        result, _stats = _stage_extract(text, "log")
        assert "before" in result
        assert "Error: broke" in result
        assert "after" in result

    def test_no_signals_returns_original(self):
        text = "Just normal content\nMore content"
        result, stats = _stage_extract(text, "log")
        assert result == text
        assert stats["signal_lines"] == 0

    def test_sampling_for_long_terminal_output(self):
        lines = [f"line {i}" for i in range(100)]
        text = "\n".join(lines)
        result, stats = _stage_extract(text, "terminal")
        assert stats["sampled"] > 0
        assert len(result.splitlines()) < 100


class TestStageCompress:
    def test_compresses_repeated_blocks(self):
        # Need 10+ lines to trigger compression
        text = "\n".join(["  at module func1()"] * 5 + ["  at module func2()"] * 5 + ["Done"])
        result, _stats = _stage_compress(text, "log")
        assert "similar lines" in result or len(result.splitlines()) < 11

    def test_short_text_unchanged(self):
        text = "line 1\nline 2"
        result, stats = _stage_compress(text, "default")
        assert result == text
        assert stats["compressed_blocks"] == 0


class TestStageTokens:
    def test_estimates_tokens(self):
        text = "Hello world this is a test"
        _result, stats = _stage_tokens(text)
        assert stats["estimated_tokens"] > 0

    def test_truncates_to_budget(self):
        text = "word " * 1000
        result, stats = _stage_tokens(text, budget=50)
        assert stats["truncated"] is True
        assert "[...truncated...]" in result
        assert stats["estimated_tokens_after"] <= 55  # some margin

    def test_no_budget_no_truncation(self):
        text = "word " * 100
        result, stats = _stage_tokens(text)
        assert stats["truncated"] is False
        assert result == text


class TestDetectProfile:
    def test_detects_log(self):
        text = "2024-01-15T10:30:00Z [INFO] start\n2024-01-15T10:30:01Z [ERROR] fail"
        assert detect_profile(text) == "log"

    def test_detects_terminal(self):
        text = "\x1b[31mred\x1b[0m"
        assert detect_profile(text) == "terminal"

    def test_detects_code(self):
        text = "def hello():\n    pass\nclass World:\n    pass\nimport os"
        assert detect_profile(text) == "code"

    def test_detects_web(self):
        text = "<html><body>hello</body></html>"
        assert detect_profile(text) == "web"

    def test_detects_docs(self):
        text = "# Title\n## Section\n### Sub\n---\nContent"
        assert detect_profile(text) == "docs"

    def test_defaults_to_default(self):
        text = "Just some plain text."
        assert detect_profile(text) == "default"

    def test_respects_hint(self):
        text = "Just some plain text."
        assert detect_profile(text, hint="log") == "log"


class TestApplyFilter:
    def test_full_pipeline(self):
        text = """2024-01-15T10:30:00Z [INFO] start
2024-01-15T10:30:00Z [INFO] start
\x1b[31mError: something broke\x1b[0m
2024-01-15T10:30:02Z [INFO] done
"""
        result = apply_filter(text, profile="log", budget=100)

        assert result["clean_content"] is not None
        assert result["profile"] == "log"
        assert "stats" in result
        assert result["version"] == "v1"

        # Verify ANSI removed
        assert "\x1b[" not in result["clean_content"]

        # Verify dedup happened (duplicate "start" line)
        assert result["stats"]["dedup"]["exact_dups"] >= 1

        # Verify noise removed
        assert result["stats"]["noise"]["removed_ansi"] > 0

        # Verify signal extracted
        assert result["stats"]["extract"]["signal_lines"] > 0

    def test_auto_detects_profile(self):
        text = "def hello():\n    pass\nclass World:\n    pass\nimport os"
        result = apply_filter(text)
        assert result["profile"] == "code"

    def test_budget_truncation(self):
        text = "word " * 1000
        result = apply_filter(text, budget=50)
        assert result["stats"]["tokens"]["truncated"] is True
        assert "[...truncated...]" in result["clean_content"]

    def test_empty_input(self):
        result = apply_filter("")
        assert result["clean_content"] == ""
        assert result["profile"] == "default"


# ---------------------------------------------------------------------------
# Auto-filter on ingest, MCP tool, CLI command, stats (M10 activation)
# ---------------------------------------------------------------------------


def _make_settings(tmpdir: str) -> Settings:
    tmp = Path(tmpdir)
    settings = Settings(
        mnemos={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
            "auto_filter": True,
        },
        embedding={"provider": "nano"},
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def mgr():
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = _make_settings(tmpdir)
        m = MemoryManager(settings)
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        m._embedder = mock_embedder
        yield m
        m.close()


_VALID_TAGS = ["project:test", "agent:filter-test", "mnemos:learning"]


class TestAutoFilterOnAdd:
    """Auto-filter activation via MemoryManager.add()."""

    def test_auto_filter_populates_clean_content(self, mgr: MemoryManager) -> None:
        """auto_filter=True → add() populates clean_content."""
        data = MemoryCreate(
            content="2024-01-15T10:30:00Z [INFO] start\n[INFO] start\nError: boom",
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        memory = mgr.add(data, project="test", agent="filter-test")
        assert memory.clean_content is not None
        assert memory.filter_profile is not None
        assert memory.filter_stats is not None

    def test_auto_filter_disabled_keeps_clean_content_none(self, mgr: MemoryManager) -> None:
        """auto_filter=False → clean_content stays None."""
        mgr.settings.mnemos.auto_filter = False
        data = MemoryCreate(
            content="some plain content",
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        memory = mgr.add(data, project="test", agent="filter-test")
        assert memory.clean_content is None

    def test_auto_filter_non_fatal_on_crash(self, mgr: MemoryManager) -> None:
        """If the filter crashes, the memory is still saved with raw content."""
        from unittest.mock import patch

        data = MemoryCreate(
            content="content that will trigger a filter crash",
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        # apply_filter is imported lazily inside apply_context_filter;
        # patch it at its source module.
        with patch(
            "vesmaro.filter.pipeline.apply_filter",
            side_effect=RuntimeError("simulated filter crash"),
        ):
            # Should not raise — auto-filter is non-fatal
            memory = mgr.add(data, project="test", agent="filter-test")

        assert memory.id is not None
        # Memory is saved; clean_content may be None because filter failed
        reloaded = mgr.get(memory.id)
        assert reloaded is not None
        assert reloaded.content == "content that will trigger a filter crash"

    def test_auto_filter_preserves_raw_content(self, mgr: MemoryManager) -> None:
        """raw_content invariant: never mutated after first write."""
        raw_text = "\x1b[31mError: boom\x1b[0m\n2024-01-15 [INFO] start"
        data = MemoryCreate(
            content=raw_text,
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        memory = mgr.add(data, project="test", agent="filter-test")
        # content (the original) is preserved
        assert memory.content == raw_text
        # clean_content is the filtered projection
        assert memory.clean_content is not None
        assert "\x1b[" not in (memory.clean_content or "")


class TestMcpFilterTool:
    """vesma_filter MCP tool — explicit filter/refresh."""

    @pytest.mark.asyncio
    async def test_mnemos_filter_tool_registered(self) -> None:
        from vesmaro.mcp_server import list_tools

        tools = await list_tools()
        names = [t.name for t in tools]
        assert "vesma_filter" in names

    @pytest.mark.asyncio
    async def test_mnemos_filter_dispatch(self, mgr: MemoryManager) -> None:
        from unittest.mock import patch

        from vesmaro.mcp_server import _dispatch
        from vesmaro.models import MemoryStatus

        data = MemoryCreate(
            content="line 1\nline 1\nline 2",
            tags=_VALID_TAGS,
            source=MemorySource.MCP,
        )
        memory = mgr.add(data, project="test", agent="filter-test")
        # M1 (final review): vesma_filter is issuance-gated — raw memories
        # are not filterable into context, so advance to published first.
        mgr.sqlite.update_status(memory.id, MemoryStatus.PUBLISHED)
        # Reset clean_content to simulate unfiltered memory
        mgr.sqlite.update_fields(memory.id, clean_content=None)
        memory = mgr.get(memory.id)
        assert memory is not None and memory.clean_content is None

        with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
            result = await _dispatch(
                "vesma_filter",
                {"memory_id": memory.id, "profile": "default"},
            )

        assert result["memory_id"] == memory.id
        assert result["profile"] == "default"
        assert "clean_content" in result
        assert "stats" in result

    @pytest.mark.asyncio
    async def test_mnemos_filter_with_budget(self, mgr: MemoryManager) -> None:
        from unittest.mock import patch

        from vesmaro.mcp_server import _dispatch
        from vesmaro.models import MemoryStatus

        data = MemoryCreate(
            content="word " * 500,
            tags=_VALID_TAGS,
            source=MemorySource.MCP,
        )
        memory = mgr.add(data, project="test", agent="filter-test")
        mgr.sqlite.update_status(memory.id, MemoryStatus.PUBLISHED)

        with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
            result = await _dispatch(
                "vesma_filter",
                {"memory_id": memory.id, "budget": 50},
            )

        assert result["stats"]["tokens"]["truncated"] is True

    @pytest.mark.asyncio
    async def test_mnemos_filter_missing_memory(self, mgr: MemoryManager) -> None:
        from unittest.mock import patch

        from vesmaro.mcp_server import _dispatch

        with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
            result = await _dispatch(
                "vesma_filter",
                {"memory_id": "nonexistent-id"},
            )

        assert result["status"] == "error"


class TestMcpAddAutoFilter:
    """vesma_add auto-filters and returns filter metadata."""

    @pytest.mark.asyncio
    async def test_mnemos_add_returns_filtered_flag(self, mgr: MemoryManager) -> None:
        from unittest.mock import patch

        from vesmaro.mcp_server import _dispatch

        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mgr),
            patch(
                "vesmaro.mcp_server.validate_tag_contract",
                side_effect=lambda tags, **_kw: tags,
            ),
        ):
            result = await _dispatch(
                "vesma_add",
                {
                    "content": "2024-01-15 [INFO] start\n[INFO] start\nError: boom",
                    "tags": _VALID_TAGS,
                },
            )

        assert result["filtered"] is True
        assert result["filter_profile"] is not None


class TestSearchReturnsCleanContent:
    """vesma_search / vesma_recall_context return clean_content after auto-filter."""

    @pytest.mark.asyncio
    async def test_search_returns_clean_content(self, mgr: MemoryManager) -> None:
        from unittest.mock import patch

        from vesmaro.mcp_server import _dispatch

        # Add a memory with ANSI codes — auto-filter strips them
        raw = "\x1b[31mError: kubernetes boom\x1b[0m"
        data = MemoryCreate(
            content=raw,
            tags=_VALID_TAGS,
            source=MemorySource.MCP,
        )
        memory = mgr.add(data, project="test", agent="filter-test")
        assert memory.clean_content is not None
        assert "\x1b[" not in memory.clean_content

        with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
            results = await _dispatch(
                "vesma_search",
                {"query": "kubernetes", "limit": 10, "include_raw": True},
            )

        assert len(results) >= 1
        # The returned content should be the clean version (no ANSI)
        assert any("\x1b[" not in r["content"] for r in results)

    @pytest.mark.asyncio
    async def test_recall_returns_clean_content(self, mgr: MemoryManager) -> None:
        from unittest.mock import patch

        from vesmaro.mcp_server import _dispatch

        raw = "\x1b[31mcheckpoint content\x1b[0m"
        data = MemoryCreate(
            content=raw,
            tags=["project:recall-test", "agent:user", "mnemos:checkpoint"],
            source=MemorySource.MCP,
        )
        memory = mgr.add(data, project="recall-test", agent="user")
        assert memory.clean_content is not None

        with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
            result = await _dispatch(
                "vesma_recall_context",
                {"project": "recall-test"},
            )

        # result is a string; clean content should not contain ANSI
        assert isinstance(result, str)
        assert "\x1b[" not in result


class TestStatsFilterSection:
    """mnemos stats includes filter statistics."""

    def test_stats_includes_filter_section(self, mgr: MemoryManager) -> None:
        data = MemoryCreate(
            content="2024-01-15 [INFO] start\n[INFO] start\nError: boom",
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        mgr.add(data, project="test", agent="filter-test")

        stats = mgr.stats()
        assert "filter" in stats
        filter_stats = stats["filter"]
        assert filter_stats["auto_filter"] is True
        assert filter_stats["filtered_count"] >= 1
        assert "unfiltered_count" in filter_stats
        assert "avg_reduction_pct" in filter_stats
        assert "by_profile" in filter_stats


class TestFilterIdempotent:
    """Re-filtering the same memory with the same profile yields the same result."""

    def test_filter_idempotent(self, mgr: MemoryManager) -> None:
        data = MemoryCreate(
            content="2024-01-15 [INFO] start\n[INFO] start\nError: boom",
            tags=_VALID_TAGS,
            source=MemorySource.CLI,
        )
        memory = mgr.add(data, project="test", agent="filter-test")
        first_clean = memory.clean_content

        # Re-filter explicitly
        result = mgr.apply_context_filter(memory.id, profile=memory.filter_profile)
        assert result["status"] == "ok"
        assert result["clean_content"] == first_clean


class TestCliFilterCommand:
    """mnemos filter <id> and mnemos filter --all CLI commands."""

    def test_cli_filter_single(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = tmp_path / "vesmaro.yaml"
        cfg.write_text(
            f"mnemos:\n"
            f"  vault_path: {tmp_path / 'vault'}\n"
            f"  data_dir: {tmp_path / 'data'}\n"
            f"  db_name: cli-filter.db\n"
            f"embedding:\n"
            f"  provider: nano\n"
        )
        monkeypatch.setenv("VESMARO_CONFIG", str(cfg))

        runner = CliRunner()
        # First add a memory
        result = runner.invoke(
            cli_app,
            ["add", "2024-01-15 [INFO] start", "--tags", ",".join(_VALID_TAGS)],
        )
        assert result.exit_code == 0, result.output

        # Extract the memory id from output
        # Output format: "✓ Saved: ... (uuid)"
        output = result.output
        # Find the UUID in parentheses
        import re

        match = re.search(r"\(([0-9a-f-]{36})\)", output)
        assert match is not None, f"Could not find memory id in output: {output}"
        mem_id = match.group(1)

        # Run filter command
        result = runner.invoke(cli_app, ["filter", mem_id, "--profile", "default"])
        assert result.exit_code == 0, result.output
        assert "Filtered" in result.output or "✓" in result.output

    def test_cli_filter_all(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = tmp_path / "vesmaro.yaml"
        cfg.write_text(
            f"mnemos:\n"
            f"  vault_path: {tmp_path / 'vault'}\n"
            f"  data_dir: {tmp_path / 'data'}\n"
            f"  db_name: cli-filter-all.db\n"
            f"embedding:\n"
            f"  provider: nano\n"
        )
        monkeypatch.setenv("VESMARO_CONFIG", str(cfg))

        runner = CliRunner()
        # Add a couple of memories
        for i in range(3):
            result = runner.invoke(
                cli_app,
                ["add", f"line {i} content", "--tags", ",".join(_VALID_TAGS)],
            )
            assert result.exit_code == 0, result.output

        # Run filter --all
        result = runner.invoke(cli_app, ["filter", "--all"])
        assert result.exit_code == 0, result.output
        assert "Filtered" in result.output or "✓" in result.output

    def test_cli_filter_no_id_no_all_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "vesmaro.yaml"
        cfg.write_text(
            f"mnemos:\n"
            f"  vault_path: {tmp_path / 'vault'}\n"
            f"  data_dir: {tmp_path / 'data'}\n"
            f"  db_name: cli-filter-err.db\n"
            f"embedding:\n"
            f"  provider: nano\n"
        )
        monkeypatch.setenv("VESMARO_CONFIG", str(cfg))

        runner = CliRunner()
        result = runner.invoke(cli_app, ["filter"])
        assert result.exit_code != 0
