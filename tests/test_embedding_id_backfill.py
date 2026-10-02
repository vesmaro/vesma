"""Search v2 (issue #313) — ``memories.embedding_id`` write path + backfill.

Live probe against the production DB showed ``embedding_id`` NULL for
1644/1644 rows: the pre-v2 write path never stamped it. The vector leg
always RESOLVED by memory id and worked — the column was a diagnostic
trap (a NULL that looks like "not embedded").

Two fixes under test:

* insert path — ``MemoryManager.upsert_embedding`` (the SINGLE
  embedding write point) stamps ``embedding_id = memory.id`` after the
  vector write succeeds, so every new publication is born stamped;
* backfill — ``MemoryManager.backfill_embedding_ids`` closes the gap for
  existing rows by an id-keyed join against the vector store (idempotent,
  dry-run default; the CLI exposes it as ``mnemos backfill-embedding-ids``).

The VectorStore keys embeddings BY memory id, so the stamp is literally
"this memory has a live vector row".
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus


@pytest.fixture
def tmp_settings():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            vesma={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
            },
            scanner={"enabled": False},
        )
        settings.resolve_paths()
        yield settings


@pytest.fixture
def manager(tmp_settings):
    mgr = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


def _add_published(mgr: MemoryManager, content: str, *, project: str = "proj") -> object:
    data = MemoryCreate(
        content=content,
        tags=[f"project:{project}", "agent:backfill", "mnemos:test"],
        source=MemorySource.MCP,
        status=MemoryStatus.PUBLISHED,
    )
    return mgr.add(data, project=project, agent="backfill")


def _add_raw(mgr: MemoryManager, content: str, *, project: str = "proj") -> object:
    data = MemoryCreate(
        content=content,
        tags=[f"project:{project}", "agent:backfill", "mnemos:test"],
        source=MemorySource.MCP,
        status=MemoryStatus.RAW,
    )
    return mgr.add(data, project=project, agent="backfill")


# ── insert path ──────────────────────────────────────────────────────────────


class TestInsertPathStamps:
    def test_published_add_stamps_embedding_id(self, manager) -> None:
        mem = _add_published(manager, "published content gets stamped")
        reloaded = manager.sqlite.get(mem.id)
        assert reloaded is not None
        # The vector store keys by memory id; the stamp records the live row.
        assert reloaded.embedding_id == mem.id
        assert manager.vectors.has(mem.id)

    def test_raw_add_has_no_vector_and_no_stamp(self, manager) -> None:
        mem = _add_raw(manager, "raw content has no vector")
        reloaded = manager.sqlite.get(mem.id)
        assert reloaded is not None
        assert reloaded.embedding_id is None
        assert not manager.vectors.has(mem.id)

    def test_reindex_repair_after_manual_wipe_of_column(self, manager) -> None:
        """A re-embed through upsert_embedding re-stamps the column."""
        mem = _add_published(manager, "re-embed re-stamps")
        manager.sqlite.update_fields(mem.id, embedding_id=None)
        reloaded = manager.sqlite.get(mem.id)
        assert reloaded is not None and reloaded.embedding_id is None
        manager.rebuild_vector_index(batch_size=10)
        reloaded = manager.sqlite.get(mem.id)
        assert reloaded is not None
        assert reloaded.embedding_id == mem.id


# ── backfill ─────────────────────────────────────────────────────────────────


class TestBackfill:
    def _make_legacy_gap(self, manager: MemoryManager) -> tuple[str, str]:
        """Create two rows with vectors but NULL embedding_id (the live-DB shape)."""
        stamped = _add_published(manager, "stamped already")
        legacy = _add_published(manager, "legacy row whose stamp was never written")
        # Simulate the pre-v2 write path: vector exists, column NULL.
        manager.sqlite.update_fields(legacy.id, embedding_id=None)
        assert manager.vectors.has(legacy.id)
        return stamped.id, legacy.id

    def test_dry_run_reports_without_writing(self, manager) -> None:
        _, legacy_id = self._make_legacy_gap(manager)
        result = manager.backfill_embedding_ids(dry_run=True)
        assert result["missing"] == 1
        assert result["stamped"] == 0
        assert result["skipped_already_set"] == 1
        # Nothing was written.
        reloaded = manager.sqlite.get(legacy_id)
        assert reloaded is not None and reloaded.embedding_id is None

    def test_apply_stamps_from_vector_join(self, manager) -> None:
        _, legacy_id = self._make_legacy_gap(manager)
        result = manager.backfill_embedding_ids(dry_run=False)
        assert result["missing"] == 1
        assert result["stamped"] == 1
        reloaded = manager.sqlite.get(legacy_id)
        assert reloaded is not None
        assert reloaded.embedding_id == legacy_id

    def test_idempotent_second_run_matches_nothing(self, manager) -> None:
        self._make_legacy_gap(manager)
        first = manager.backfill_embedding_ids(dry_run=False)
        assert first["stamped"] == 1
        second = manager.backfill_embedding_ids(dry_run=False)
        assert second["missing"] == 0
        assert second["stamped"] == 0

    def test_row_without_vector_stays_null(self, manager) -> None:
        """A memory whose vector is gone must NOT get a stamp.

        The column records "live vector exists" — stamping a vector-less
        row would make the diagnostic lie.
        """
        mem = _add_published(manager, "vector will be deleted")
        manager.vectors.delete(mem.id)
        manager.sqlite.update_fields(mem.id, embedding_id=None)
        result = manager.backfill_embedding_ids(dry_run=False)
        assert result["missing"] == 0
        reloaded = manager.sqlite.get(mem.id)
        assert reloaded is not None and reloaded.embedding_id is None

    def test_raw_rows_without_vectors_untouched(self, manager) -> None:
        _add_published(manager, "one published row")
        _add_raw(manager, "raw row never embedded")
        result = manager.backfill_embedding_ids(dry_run=False)
        assert result["stamped"] == 0 or result["stamped"] == 1
        # The raw row was never in the vector store, so it never qualifies.
        raw_rows = [m for m in manager.sqlite.list_all(limit=100) if m.status == MemoryStatus.RAW]
        assert all(m.embedding_id is None for m in raw_rows)


# ── vector store helper ──────────────────────────────────────────────────────


class TestAllIds:
    def test_all_ids_lists_every_vector_row(self, manager) -> None:
        mem = _add_published(manager, "all_ids sees this row")
        ids = manager.vectors.all_ids()
        assert mem.id in ids


# ── CLI surface ──────────────────────────────────────────────────────────────


class TestCliCommand:
    def test_dry_run_default_and_apply_flag(self, manager, monkeypatch, capsys) -> None:
        from vesmaro.cli import main as cli_main

        mem = _add_published(manager, "cli backfill target")
        manager.sqlite.update_fields(mem.id, embedding_id=None)
        monkeypatch.setattr(cli_main, "get_manager", lambda _config: manager)

        cli_main.backfill_embedding_ids_cmd(apply=False, config="whatever")
        out = capsys.readouterr().out
        assert "dry run" in out
        assert "missing embedding_id: 1" in out
        reloaded = manager.sqlite.get(mem.id)
        assert reloaded is not None and reloaded.embedding_id is None

        cli_main.backfill_embedding_ids_cmd(apply=True, config="whatever")
        out = capsys.readouterr().out
        assert "stamped: 1" in out
        reloaded = manager.sqlite.get(mem.id)
        assert reloaded is not None
        assert reloaded.embedding_id == mem.id
