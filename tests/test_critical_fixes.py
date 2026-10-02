"""Regression tests for critical FTS5 + background processor fixes.

Covers:
  - FTS5 rebuild after corruption
  - FTS5 auto-recovery in fts_search()
  - save() uses UPDATE (not INSERT OR REPLACE) for existing rows
  - Background processor start/stop/drain
  - CLI fts rebuild + processor commands
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vesmaro.cli.main import app
from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import Memory, MemorySource, MemoryStatus, MemoryType
from vesmaro.storage.sqlite_store import SQLiteStore


@pytest.fixture(autouse=True)
def _restore_compat_env_aliases() -> Iterator[None]:
    """Restore the #139 compat env aliases after each test.

    ``_make_settings`` writes ``VESMA_DATA_DIR`` / ``VESMA_VAULT__VAULT_PATH``
    directly into ``os.environ``. Since the #139 compat shim made these names
    live, a leak would bleed into later modules (e.g. ``test_config_layout``
    asserts default paths under a fake home). Snapshot and restore around
    every test in this module.
    """
    names = ("VESMA_DATA_DIR", "VESMA_VAULT__VAULT_PATH")
    saved = {name: os.environ.get(name) for name in names}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_memory(mid: str = "m1", content: str = "hello world") -> Memory:
    now = datetime.now(UTC)
    return Memory(
        id=mid,
        content=content,
        title="test",
        tags=["project:test", "agent:test", "mnemos:learning"],
        source=MemorySource.MANUAL,
        source_url=None,
        memory_type=MemoryType.NOTE,
        created_at=now,
        updated_at=now,
        metadata={},
        file_path=None,
        category=None,
        project="test-project",
        agent="test-agent",
        status=MemoryStatus.RAW,
        quality_score=None,
        confidence=None,
        source_coverage=None,
        cluster_id=None,
        derived_from=[],
        embedding_id=None,
        raw_content=None,
        clean_content=None,
        filter_profile=None,
        filter_stats=None,
        filter_version=None,
    )


def _make_settings(tmp_path: Path) -> Settings:
    """Create Settings with isolated tmp paths via env vars."""
    os.environ["VESMA_DATA_DIR"] = str(tmp_path / "data")
    os.environ["VESMA_VAULT__VAULT_PATH"] = str(tmp_path / "vault")
    Path(tmp_path / "data").mkdir(parents=True, exist_ok=True)
    Path(tmp_path / "vault").mkdir(parents=True, exist_ok=True)
    s = Settings()
    s.resolve_paths()
    return s


# ── FTS5 rebuild tests ────────────────────────────────────────────────────────


class TestFTSRebuild:
    def test_rebuild_fixes_corruption(self, tmp_path: Path) -> None:
        """rebuild_fts_index() fixes a corrupted FTS5 index."""
        db = tmp_path / "test.db"
        store = SQLiteStore(db)
        mem = _make_memory(content="unique searchable text")
        store.save(mem)

        # Corrupt: delete from memories without triggering FTS delete
        conn = store._get_conn()
        conn.execute("DELETE FROM memories WHERE id = ?", (mem.id,))
        conn.commit()

        # Rebuild
        count = store.rebuild_fts_index()
        assert count >= 0

        # Re-add and verify search works
        store.save(mem)
        results = store.fts_search("unique searchable text", limit=10)
        assert len(results) == 1
        store.close()

    def test_fts_search_auto_recovers(self, tmp_path: Path) -> None:
        """fts_search() auto-rebuilds on corruption instead of raising."""
        db = tmp_path / "test2.db"
        store = SQLiteStore(db)
        mem = _make_memory(mid="m2", content="auto recovery test phrase")
        store.save(mem)

        # Corrupt FTS by deleting from memories but keeping FTS row
        conn = store._get_conn()
        conn.execute("DELETE FROM memories WHERE id = ?", (mem.id,))
        conn.commit()

        # fts_search should auto-recover, not raise
        results = store.fts_search("auto recovery", limit=10)
        assert isinstance(results, list)  # didn't raise
        store.close()


# ── save() UPDATE vs INSERT tests ─────────────────────────────────────────────


class TestSaveUpdateNotReplace:
    def test_save_update_does_not_corrupt_fts(self, tmp_path: Path) -> None:
        """Saving an existing memory updates FTS correctly (no corruption)."""
        db = tmp_path / "test3.db"
        store = SQLiteStore(db)
        mem = _make_memory(mid="m3", content="original content here")
        store.save(mem)

        # Save again (update) — should use UPDATE, not INSERT OR REPLACE
        mem.content = "updated content here"
        store.save(mem)

        # FTS should still work
        results = store.fts_search("updated content", limit=10)
        assert len(results) == 1
        assert results[0][0].content == "updated content here"
        store.close()

    def test_save_update_preserves_fts_search(self, tmp_path: Path) -> None:
        """Multiple saves don't corrupt FTS — search always works."""
        db = tmp_path / "test4.db"
        store = SQLiteStore(db)
        mem = _make_memory(mid="m4", content="version one")
        store.save(mem)
        store.save(mem)  # second save
        store.save(mem)  # third save

        results = store.fts_search("version one", limit=10)
        assert len(results) == 1
        store.close()


# ── Background processor tests ────────────────────────────────────────────────


class TestBackgroundProcessor:
    def test_starts_and_stops(self, tmp_path: Path) -> None:
        """Processor thread starts and stops cleanly."""
        settings = _make_settings(tmp_path)
        mgr = MemoryManager(settings)
        assert not mgr.processor_running
        mgr.start_background_processor(interval_sec=1)
        assert mgr.processor_running
        mgr.stop_background_processor()
        assert not mgr.processor_running
        mgr.close()

    def test_safe_to_call_twice(self, tmp_path: Path) -> None:
        """Calling start twice doesn't create two threads."""
        settings = _make_settings(tmp_path)
        mgr = MemoryManager(settings)
        mgr.start_background_processor(interval_sec=60)
        mgr.start_background_processor(interval_sec=60)
        assert mgr.processor_running
        mgr.stop_background_processor()
        mgr.close()

    def test_stop_on_close(self, tmp_path: Path) -> None:
        """close() stops the processor."""
        settings = _make_settings(tmp_path)
        mgr = MemoryManager(settings)
        mgr.start_background_processor(interval_sec=60)
        assert mgr.processor_running
        mgr.close()
        assert not mgr.processor_running


# ── CLI tests ─────────────────────────────────────────────────────────────────


runner = CliRunner()


class TestCLI:
    @pytest.fixture()
    def isolated_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Point VESMA_CONFIG at an empty YAML so the CLI uses tmp_path."""
        from vesmaro.cli._manager import reset_manager

        reset_manager()
        cfg = tmp_path / "vesmaro.yaml"
        cfg.write_text(
            f"vesma:\n"
            f"  vault_path: {tmp_path / 'vault'}\n"
            f"  data_dir: {tmp_path / 'data'}\n"
            f"  db_name: cli-critical.db\n"
            f"embedding:\n"
            f"  provider: nano\n"
        )
        monkeypatch.setenv("VESMA_CONFIG", str(cfg))
        yield cfg
        reset_manager()

    def test_fts_rebuild_command(self, isolated_config: Path) -> None:
        """`mnemos fts rebuild` CLI command works."""
        result = runner.invoke(app, ["fts", "rebuild"])
        assert result.exit_code == 0
        assert "rebuilt" in result.stdout.lower()

    def test_processor_status_command(self, isolated_config: Path) -> None:
        """`mnemos processor status` CLI command works."""
        result = runner.invoke(app, ["processor", "status"])
        assert result.exit_code == 0
        assert "queue_depth" in result.stdout
