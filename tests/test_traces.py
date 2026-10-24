"""Tests for the ``vesma traces`` CLI command (M17 — trace viewer).

CLI topology FINAL (owner doctrine 2026-10-24, 7.0.0): the trace viewer
owns ``vesma traces``; the former ``vesma logs`` / ``vesma task-logs``
spellings are REMOVED (the word "logs" belongs to the daemon world —
``vesma service logs NAME``). ``traces`` takes NO positional argument.

Uses ``tmp_path`` for the database — never touches the real store.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from vesma.cli.main import app
from vesma.models import Trace
from vesma.storage.sqlite_store import SQLiteStore

runner = CliRunner()


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "vesma.yaml"
    cfg.write_text(
        f"vesma:\n"
        f"  vault_path: {tmp_path / 'vault'}\n"
        f"  data_dir: {tmp_path / 'data'}\n"
        f"  db_name: traces-test.db\n"
        "embedding:\n"
        f"  provider: nano\n"
    )
    monkeypatch.setenv("VESMA_CONFIG", str(cfg))
    return cfg


@pytest.fixture
def traces_db(isolated_config: Path) -> SQLiteStore:
    """Seed the isolated DB with a few traces."""
    from vesma.config import load_settings

    settings = load_settings(str(isolated_config))
    settings.resolve_paths()
    store = SQLiteStore(settings.db_path)
    store.save_trace(
        Trace(
            task_label="cluster",
            project="vesma",
            step="embed",
            item_id="mem-1",
            latency_ms=120,
            cache_hit=False,
        )
    )
    store.save_trace(
        Trace(
            task_label="synthesize",
            project="vesma",
            step="llm-call",
            item_id="mem-2",
            latency_ms=2500,
            llm_called=True,
            fallback_used=False,
        )
    )
    store.save_trace(
        Trace(
            task_label="publish",
            project="other-project",
            step="vector-index",
            item_id="mem-3",
            latency_ms=5,
        )
    )
    # Checkpoint WAL so the CLI process sees the traces on disk.
    store._get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store._get_conn().commit()
    yield store
    store.close()


class TestTracesCommand:
    def test_traces_shows_traces(self, isolated_config, traces_db):
        result = runner.invoke(app, ["traces", "--limit", "10"])
        assert result.exit_code == 0, result.output
        assert "cluster" in result.output
        assert "synthesize" in result.output

    def test_traces_filter_by_task(self, isolated_config, traces_db):
        result = runner.invoke(app, ["traces", "--task", "cluster", "--limit", "10"])
        assert result.exit_code == 0, result.output
        assert "cluster" in result.output
        # synthesize should be filtered out
        assert "synthesize" not in result.output

    def test_traces_filter_by_project(self, isolated_config, traces_db):
        result = runner.invoke(app, ["traces", "--project", "other-project", "--limit", "10"])
        assert result.exit_code == 0, result.output
        assert "other-project" in result.output
        assert "vesma" not in result.output or "other-project" in result.output

    def test_traces_empty_db_prints_no_traces(self, isolated_config):
        """Empty trace store says so."""
        result = runner.invoke(app, ["traces"])
        assert result.exit_code == 0, result.output
        assert "no" in result.output.lower() or "trace" in result.output.lower()

    def test_traces_invalid_since_date_exits_nonzero(self, isolated_config, traces_db):
        result = runner.invoke(app, ["traces", "--since", "not-a-date"])
        assert result.exit_code != 0


class TestTracesTopology:
    """7.0.0 pins: `traces` is the ONE trace-viewer spelling; the former
    spellings answer with the standard typer error — no stubs, no
    redirects (every removed spelling returns exit 2)."""

    def test_traces_group_registered(self):
        registered_groups = {getattr(g, "name", None) for g in app.registered_groups}
        assert "traces" in registered_groups

    def test_logs_and_task_logs_spellings_are_gone(self):
        """The former spellings are NOT registered — 'logs' belongs to the
        daemon world (`vesma service logs`) and 'task-logs' died with it."""
        click_root = get_command(app)
        assert "logs" not in click_root.commands
        assert "task-logs" not in click_root.commands
        for argv in (["logs"], ["task-logs"]):
            result = runner.invoke(app, argv)
            assert result.exit_code == 2, (argv, result.output)

    def test_positional_argument_is_a_usage_error(self, isolated_config):
        """Components are not trace subjects: ANY positional is the
        parser's usage error (exit 2, "No such command") — the former
        `logs NAME` redirect special case is gone (no orphan hint paths)."""
        result = runner.invoke(app, ["traces", "board"])
        assert result.exit_code == 2, result.output
        assert "No such command" in result.output
