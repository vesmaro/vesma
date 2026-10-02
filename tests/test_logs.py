"""Tests for ``mnemos logs`` CLI subcommand (M17 — trace viewer).

Uses ``tmp_path`` for the database — never touches the real
``~/.mnemos/mnemos.db``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
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
        f"  db_name: logs-test.db\n"
        f"embedding:\n"
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
            project="mnemos",
            step="embed",
            item_id="mem-1",
            latency_ms=120,
            cache_hit=False,
        )
    )
    store.save_trace(
        Trace(
            task_label="synthesize",
            project="mnemos",
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


class TestLogsCommand:
    def test_logs_shows_traces(self, isolated_config, traces_db):
        result = runner.invoke(app, ["logs", "--limit", "10"])
        assert result.exit_code == 0, result.output
        assert "cluster" in result.output
        assert "synthesize" in result.output

    def test_logs_filter_by_task(self, isolated_config, traces_db):
        result = runner.invoke(app, ["logs", "--task", "cluster", "--limit", "10"])
        assert result.exit_code == 0, result.output
        assert "cluster" in result.output
        # synthesize should be filtered out
        assert "synthesize" not in result.output

    def test_logs_filter_by_project(self, isolated_config, traces_db):
        result = runner.invoke(app, ["logs", "--project", "other-project", "--limit", "10"])
        assert result.exit_code == 0, result.output
        assert "other-project" in result.output
        assert "mnemos" not in result.output or "other-project" in result.output

    def test_logs_empty_db_prints_no_traces(self, isolated_config):
        result = runner.invoke(app, ["logs"])
        assert result.exit_code == 0, result.output
        assert "no" in result.output.lower() or "trace" in result.output.lower()

    def test_logs_invalid_since_date_exits_nonzero(self, isolated_config, traces_db):
        result = runner.invoke(app, ["logs", "--since", "not-a-date"])
        assert result.exit_code != 0

    def test_logs_command_registered(self):
        """`mnemos logs` is registered on the Typer app."""
        registered_groups = {getattr(g, "name", None) for g in app.registered_groups}
        assert "logs" in registered_groups
