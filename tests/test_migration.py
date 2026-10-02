"""M13 — Migration tests: ai-brain → Vesma.

Uses a temporary SQLite DB that mimics ai-brain schema.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from vesma.cli.migrate import migrate_from_ai_brain
from vesma.config import load_settings
from vesma.manager import MemoryManager
from vesma.models import MemoryStatus


@pytest.fixture
def ai_brain_db(tmp_path: Path) -> Path:
    """Create a temporary ai-brain-like SQLite DB."""
    db_path = tmp_path / "ai_brain.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            content TEXT,
            title TEXT,
            tags TEXT,
            source TEXT,
            source_url TEXT,
            memory_type TEXT,
            created_at TEXT,
            updated_at TEXT,
            metadata TEXT,
            file_path TEXT,
            status TEXT,
            parent_ids TEXT,
            content_ru TEXT,
            content_en TEXT,
            category TEXT
        )
        """
    )
    # Insert legacy records
    rows = [
        {
            "id": "legacy-1",
            "content": "Hello world",
            "title": "Greeting",
            "tags": json.dumps(["old-tag"]),
            "source": "telegram",
            "source_url": "https://t.me/c/123/456",
            "memory_type": "note",
            "status": "published",
            "parent_ids": json.dumps([]),
            "content_ru": "Привет мир",
            "content_en": None,
            "category": "Технологии/ИИ и ML",
        },
        {
            "id": "legacy-2",
            "content": "",
            "title": "Empty content",
            "tags": json.dumps([]),
            "source": "manual",
            "source_url": None,
            "memory_type": "fact",
            "status": "raw",
            "parent_ids": json.dumps(["legacy-1"]),
            "content_ru": "",
            "content_en": "English only",
            "category": None,
        },
    ]
    for row in rows:
        conn.execute(
            "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"],
                row["content"],
                row["title"],
                row["tags"],
                row["source"],
                row["source_url"],
                row["memory_type"],
                "2024-01-01T00:00:00+00:00",
                "2024-01-01T00:00:00+00:00",
                json.dumps({}),
                None,
                row["status"],
                row["parent_ids"],
                row["content_ru"],
                row["content_en"],
                row["category"],
            ),
        )
    conn.commit()
    conn.close()
    return db_path


class TestMigrationDryRun:
    def test_dry_run_reports_counts(self, ai_brain_db, tmp_path) -> None:
        summary = migrate_from_ai_brain(
            ai_brain_db,
            dry_run=True,
            settings=None,
        )
        assert summary["dry_run"] is True
        assert summary["memories_migrated"] == 2
        assert summary["vault_files_migrated"] == 0
        assert summary["errors"] == []


class TestMigrationWrite:
    def test_migrates_with_contract_tags(self, ai_brain_db, tmp_path) -> None:
        # Use a fresh data dir so we don't collide with other tests
        settings = load_settings()
        settings.vesma.data_dir = tmp_path / ".mnemos"
        settings.vesma.vault_path = tmp_path / "mnemos-vault"
        settings.resolve_paths()

        summary = migrate_from_ai_brain(
            ai_brain_db,
            dry_run=False,
            settings=settings,
        )
        assert summary["memories_migrated"] == 2
        assert summary["errors"] == []

        # Verify migrated data
        mgr = MemoryManager(settings)
        all_mem = mgr.list_recent(limit=10)
        assert len(all_mem) == 2

        # Check tags have Vesma contract
        for mem in all_mem:
            assert any(t.startswith("project:") for t in mem.tags)
            assert any(t.startswith("agent:") for t in mem.tags)
            assert any(t.startswith("mnemos:") for t in mem.tags)

    def test_source_mapping(self, ai_brain_db, tmp_path) -> None:
        settings = load_settings()
        settings.vesma.data_dir = tmp_path / ".mnemos"
        settings.vesma.vault_path = tmp_path / "mnemos-vault"
        settings.resolve_paths()

        migrate_from_ai_brain(ai_brain_db, dry_run=False, settings=settings)
        mgr = MemoryManager(settings)
        mems = mgr.list_recent(limit=10)
        sources = {m.source.value for m in mems}
        assert "mcp" in sources  # telegram mapped to mcp
        assert "manual" in sources

    def test_status_preserved(self, ai_brain_db, tmp_path) -> None:
        settings = load_settings()
        settings.vesma.data_dir = tmp_path / ".mnemos"
        settings.vesma.vault_path = tmp_path / "mnemos-vault"
        settings.resolve_paths()

        migrate_from_ai_brain(ai_brain_db, dry_run=False, settings=settings)
        mgr = MemoryManager(settings)
        mems = mgr.list_recent(limit=10)
        statuses = {m.status for m in mems}
        assert MemoryStatus.PUBLISHED in statuses
        assert MemoryStatus.RAW in statuses

    def test_backup_created(self, ai_brain_db, tmp_path) -> None:
        settings = load_settings()
        settings.vesma.data_dir = tmp_path / ".mnemos"
        settings.vesma.vault_path = tmp_path / "mnemos-vault"
        settings.resolve_paths()

        # Pre-create a valid SQLite DB so backup triggers
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(settings.db_path))
        conn.execute("CREATE TABLE test (id TEXT)")
        conn.commit()
        conn.close()

        migrate_from_ai_brain(ai_brain_db, dry_run=False, settings=settings)
        backups = list(settings.vesma.data_dir.glob("*.backup-*"))
        assert len(backups) >= 1

    def test_vault_migration(self, ai_brain_db, tmp_path) -> None:
        vault = tmp_path / "brain-vault"
        vault.mkdir()
        (vault / "note.md").write_text("# Hello")
        sub = vault / "sub"
        sub.mkdir()
        (sub / "deep.md").write_text("## Deep")

        settings = load_settings()
        settings.vesma.data_dir = tmp_path / ".mnemos"
        settings.vesma.vault_path = tmp_path / "mnemos-vault"
        settings.resolve_paths()

        summary = migrate_from_ai_brain(
            ai_brain_db,
            vault,
            dry_run=False,
            settings=settings,
        )
        assert summary["vault_files_migrated"] == 2
        assert (settings.vesma.vault_path / "note.md").exists()
        assert (settings.vesma.vault_path / "sub" / "deep.md").exists()
