"""Tests for export/import functionality (M17 — backup/restore).

CRITICAL: every test uses ``tmp_path`` for the database — the real
``~/.mnemos/mnemos.db`` is never touched. Import restore-mode tests wipe
only the isolated tmp DB.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesma.cli.export import (
    CompressMode,
    ExportFilter,
    ExportFormat,
    decrypt,
    run_export,
)
from vesma.cli.import_ import ImportMode, run_import
from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import (
    CHECKPOINT_STAMP_KEYS,
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    Project,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp_path / "vault"),
            "data_dir": str(tmp_path / "data"),
            "db_name": "test-export.db",
            "auto_filter": False,
        },
        embedding={"provider": "onnx"},
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def mgr(tmp_settings: Settings) -> MemoryManager:
    m = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    m._embedder = mock_embedder
    yield m
    m.close()


def _add_memory(
    mgr: MemoryManager,
    content: str,
    *,
    project: str = "mnemos",
    agent: str = "tech-lead",
    status: MemoryStatus = MemoryStatus.PUBLISHED,
    tags: list[str] | None = None,
) -> str:
    tags = tags or [f"project:{project}", f"agent:{agent}", "mnemos:learning"]
    mem = mgr.add(
        MemoryCreate(content=content, tags=tags, source=MemorySource.CLI, status=status),
        project=project,
        agent=agent,
    )
    return mem.id


# ---------------------------------------------------------------------------
# Export — JSON format
# ---------------------------------------------------------------------------


class TestExportJSON:
    def test_json_export_has_format_version_and_mnemos_version(self, mgr, tmp_path):
        _add_memory(mgr, "hello world")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)
        payload = json.loads(out.read_text())
        assert payload["format_version"] == "1.0"
        assert payload["mnemos_version"]  # present and non-empty
        assert "exported_at" in payload
        assert isinstance(payload["memories"], list)
        assert len(payload["memories"]) == 1

    def test_json_export_includes_all_memories(self, mgr, tmp_path):
        _add_memory(mgr, "one")
        _add_memory(mgr, "two")
        _add_memory(mgr, "three")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)
        payload = json.loads(out.read_text())
        assert len(payload["memories"]) == 3

    def test_json_export_includes_projects(self, mgr, tmp_path):
        mgr.sqlite.save_project(Project(name="mnemos", paths=["/tmp/mnemos"], description="test"))
        _add_memory(mgr, "x")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)
        payload = json.loads(out.read_text())
        assert any(p["name"] == "mnemos" for p in payload["projects"])

    def test_json_export_no_traces(self, mgr, tmp_path):
        """Traces are NEVER included in export (owner decision)."""
        from vesma.models import Trace

        mgr.sqlite.save_trace(Trace(task_label="cluster", project="mnemos", step="embed"))
        _add_memory(mgr, "x")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)
        payload = json.loads(out.read_text())
        assert "traces" not in payload

    def test_json_export_filter_section_records_filters(self, mgr, tmp_path):
        _add_memory(mgr, "x", project="mnemos")
        out = tmp_path / "backup.json"
        run_export(
            mgr,
            fmt=ExportFormat.JSON,
            output=out,
            filt=ExportFilter(project="mnemos"),
        )
        payload = json.loads(out.read_text())
        assert payload["filter"]["project"] == "mnemos"


# ---------------------------------------------------------------------------
# Export — SQLite format
# ---------------------------------------------------------------------------


class TestExportSQLite:
    def test_sqlite_export_is_valid_tar_gz(self, mgr, tmp_path):
        _add_memory(mgr, "snapshot me")
        out = tmp_path / "backup.tar.gz"
        run_export(mgr, fmt=ExportFormat.SQLITE, output=out)
        assert out.exists()
        with tarfile.open(out, "r:gz") as tar:
            names = tar.getnames()
            assert "mnemos.db" in names

    def test_sqlite_export_contains_memories(self, mgr, tmp_path):
        _add_memory(mgr, "snapshot me")
        out = tmp_path / "backup.tar.gz"
        run_export(mgr, fmt=ExportFormat.SQLITE, output=out)
        with tarfile.open(out, "r:gz") as tar:
            db_bytes = tar.extractfile("mnemos.db").read()
        # Write to a temp file and query it.
        tmp_db = tmp_path / "restored.db"
        tmp_db.write_bytes(db_bytes)
        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        conn.close()
        assert row[0] == 1

    def test_sqlite_export_ignores_filters(self, mgr, tmp_path):
        """SQLite format is always a full snapshot — filters are ignored."""
        _add_memory(mgr, "a", project="mnemos")
        _add_memory(mgr, "b", project="other")
        out = tmp_path / "backup.tar.gz"
        result = run_export(
            mgr,
            fmt=ExportFormat.SQLITE,
            output=out,
            filt=ExportFilter(project="mnemos"),
        )
        assert any("Filters are ignored" in w for w in result.warnings)
        # Snapshot still contains both memories.
        with tarfile.open(out, "r:gz") as tar:
            db_bytes = tar.extractfile("mnemos.db").read()
        tmp_db = tmp_path / "restored.db"
        tmp_db.write_bytes(db_bytes)
        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        conn.close()
        assert row[0] == 2


# ---------------------------------------------------------------------------
# Export — partial filters
# ---------------------------------------------------------------------------


class TestExportFilters:
    def test_filter_by_project(self, mgr, tmp_path):
        _add_memory(mgr, "a", project="mnemos")
        _add_memory(mgr, "b", project="other")
        out = tmp_path / "subset.json"
        run_export(
            mgr,
            fmt=ExportFormat.JSON,
            output=out,
            filt=ExportFilter(project="mnemos"),
        )
        payload = json.loads(out.read_text())
        assert len(payload["memories"]) == 1
        assert payload["memories"][0]["project"] == "mnemos"

    def test_filter_by_agent(self, mgr, tmp_path):
        _add_memory(mgr, "a", agent="tech-lead")
        _add_memory(mgr, "b", agent="reviewer")
        out = tmp_path / "subset.json"
        run_export(
            mgr,
            fmt=ExportFormat.JSON,
            output=out,
            filt=ExportFilter(agent="tech-lead"),
        )
        payload = json.loads(out.read_text())
        assert len(payload["memories"]) == 1
        assert payload["memories"][0]["agent"] == "tech-lead"

    def test_filter_by_status(self, mgr, tmp_path):
        _add_memory(mgr, "pub", status=MemoryStatus.PUBLISHED)
        _add_memory(mgr, "raw", status=MemoryStatus.RAW)
        out = tmp_path / "subset.json"
        run_export(
            mgr,
            fmt=ExportFormat.JSON,
            output=out,
            filt=ExportFilter(status=MemoryStatus.PUBLISHED),
        )
        payload = json.loads(out.read_text())
        assert len(payload["memories"]) == 1
        assert payload["memories"][0]["status"] == "published"

    def test_filter_by_tags(self, mgr, tmp_path):
        _add_memory(mgr, "a", tags=["project:mnemos", "agent:x", "mnemos:learning"])
        _add_memory(mgr, "b", tags=["project:mnemos", "agent:x", "mnemos:decision"])
        out = tmp_path / "subset.json"
        run_export(
            mgr,
            fmt=ExportFormat.JSON,
            output=out,
            filt=ExportFilter(tags=["mnemos:decision"]),
        )
        payload = json.loads(out.read_text())
        assert len(payload["memories"]) == 1

    def test_filter_by_since(self, mgr, tmp_path):
        old_id = _add_memory(mgr, "old")
        # Manually backdate the first memory.
        old_mem = mgr.sqlite.get(old_id)
        assert old_mem is not None
        old_mem.created_at = datetime.now(UTC) - timedelta(days=10)
        old_mem.updated_at = old_mem.created_at
        mgr.sqlite.save(old_mem)

        _add_memory(mgr, "new")
        out = tmp_path / "incremental.json"
        run_export(
            mgr,
            fmt=ExportFormat.JSON,
            output=out,
            filt=ExportFilter(since=datetime.now(UTC) - timedelta(days=1)),
        )
        payload = json.loads(out.read_text())
        assert len(payload["memories"]) == 1
        assert payload["memories"][0]["content"] == "new"


# ---------------------------------------------------------------------------
# Export — compression
# ---------------------------------------------------------------------------


class TestExportCompression:
    def test_gzip_compression_produces_valid_gzip(self, mgr, tmp_path):
        _add_memory(mgr, "compress me")
        out = tmp_path / "backup.json.gz"
        run_export(
            mgr,
            fmt=ExportFormat.JSON,
            output=out,
            compress=CompressMode.GZIP,
        )
        raw = gzip.decompress(out.read_bytes())
        payload = json.loads(raw.decode("utf-8"))
        assert len(payload["memories"]) == 1

    def test_zstd_falls_back_to_gzip_when_unavailable(self, mgr, tmp_path):
        """zstd requested but not installed → falls back to gzip with a warning."""
        _add_memory(mgr, "x")
        out = tmp_path / "backup.json.zst"
        result = run_export(
            mgr,
            fmt=ExportFormat.JSON,
            output=out,
            compress=CompressMode.ZSTD,
        )
        # The fallback warning is present OR zstd succeeded if installed.
        if any("zstd" in w.lower() for w in result.warnings):
            raw = gzip.decompress(out.read_bytes())
            payload = json.loads(raw.decode("utf-8"))
            assert len(payload["memories"]) == 1


# ---------------------------------------------------------------------------
# Export — encryption
# ---------------------------------------------------------------------------


class TestExportEncryption:
    def test_encrypted_export_can_be_decrypted(self, mgr, tmp_path):
        _add_memory(mgr, "secret")
        out = tmp_path / "backup.json.enc"
        run_export(
            mgr,
            fmt=ExportFormat.JSON,
            output=out,
            encrypt=True,
            passphrase="correct-horse-battery-staple",
        )
        raw = out.read_bytes()
        assert raw.startswith(b"MNEMOS1")
        decrypted = decrypt(raw, "correct-horse-battery-staple")
        payload = json.loads(decrypted.decode("utf-8"))
        assert len(payload["memories"]) == 1

    def test_wrong_passphrase_fails(self, mgr, tmp_path):
        _add_memory(mgr, "secret")
        out = tmp_path / "backup.json.enc"
        run_export(
            mgr,
            fmt=ExportFormat.JSON,
            output=out,
            encrypt=True,
            passphrase="correct",
        )
        with pytest.raises(ValueError, match="Wrong passphrase"):
            decrypt(out.read_bytes(), "wrong")

    def test_encrypted_compressed_export_roundtrip(self, mgr, tmp_path):
        _add_memory(mgr, "secret compressed")
        out = tmp_path / "backup.json.gz.enc"
        run_export(
            mgr,
            fmt=ExportFormat.JSON,
            output=out,
            compress=CompressMode.GZIP,
            encrypt=True,
            passphrase="pw123",
        )
        raw = out.read_bytes()
        decrypted = decrypt(raw, "pw123")
        decompressed = gzip.decompress(decrypted)
        payload = json.loads(decompressed.decode("utf-8"))
        assert len(payload["memories"]) == 1


# ---------------------------------------------------------------------------
# Import — merge mode
# ---------------------------------------------------------------------------


class TestImportMerge:
    def test_merge_inserts_new_memories(self, mgr, tmp_path):
        _add_memory(mgr, "existing")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)

        # New isolated manager with a fresh DB in a different data dir.
        settings2 = Settings(
            vesma={
                "vault_path": str(tmp_path / "vault2"),
                "data_dir": str(tmp_path / "data2"),
                "db_name": "test-export-2.db",
                "auto_filter": False,
            },
            embedding={"provider": "onnx"},
        )
        settings2.resolve_paths()
        mgr2 = MemoryManager(settings2)
        mock = MagicMock()
        mock.embed.return_value = [0.1] * 384
        mgr2._embedder = mock
        try:
            result = run_import(mgr2, out, mode=ImportMode.MERGE)
            assert result.imported == 1
            assert result.errors == []
            assert mgr2.sqlite.count() == 1
        finally:
            mgr2.close()

    def test_merge_skips_existing_ids(self, mgr, tmp_path):
        _add_memory(mgr, "original")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)

        # Re-import into the same DB — should skip.
        result = run_import(mgr, out, mode=ImportMode.MERGE)
        assert result.imported == 0
        assert result.skipped == 1
        assert mgr.sqlite.count() == 1

    def test_merge_is_idempotent(self, mgr, tmp_path):
        _add_memory(mgr, "x")
        _add_memory(mgr, "y")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)

        r1 = run_import(mgr, out, mode=ImportMode.MERGE)
        r2 = run_import(mgr, out, mode=ImportMode.MERGE)
        assert r1.imported == 0 and r1.skipped == 2
        assert r2.imported == 0 and r2.skipped == 2
        assert mgr.sqlite.count() == 2

    def test_merge_overwrite_updates_existing(self, mgr, tmp_path):
        _add_memory(mgr, "original content")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)

        # Mutate the export to have different content, then import with --overwrite.
        payload = json.loads(out.read_text())
        payload["memories"][0]["content"] = "updated content"
        out.write_text(json.dumps(payload))

        result = run_import(mgr, out, mode=ImportMode.MERGE, overwrite=True)
        assert result.updated == 1
        mem = mgr.sqlite.get(payload["memories"][0]["id"])
        assert mem is not None
        assert mem.content == "updated content"


# ---------------------------------------------------------------------------
# Import — canon-line server-minted keys (cascade review SEC P3-5)
# ---------------------------------------------------------------------------


class TestImportCanonLineStrip:
    """The JSON import path strips canon-line SERVER-MINTED keys from row
    metadata by default — a crafted export must not land checkpoint
    stamps, a checkpoint-type canon envelope or gate-owned
    ``canon_warnings`` as if the server had minted them. A client
    task/decision/report envelope (canon §2) is peer data and persists.
    ``--trusted-restore`` (run_import trusted_restore=True) keeps the
    keys verbatim: the operator asserts a trusted self-backup."""

    @staticmethod
    def _crafted_export(tmp_path: Path) -> Path:
        """An export with forged canon-line metadata on two rows."""
        forged_stamps = {
            "checkpoint_agent": "alice",
            "checkpoint_session": "forged-sess",
            "checkpoint_dedup_key": "deadbeef",
            "canon": {
                "schema_version": "1",
                "type": "checkpoint",
                "status": "active",
                "language": "en",
                "session_ref": None,
            },
            "canon_warnings": [{"code": "CANON-E-TITLE", "rule": "canon §3", "detail": "forged"}],
            "harmless": 1,
        }
        task_env = {
            "schema_version": "1",
            "type": "task",
            "status": "active",
            "language": "en",
            "owner_slug": "tech-lead",
            "priority": "P1",
            "size": "M",
        }
        payload = {
            "format_version": "1.0",
            "mnemos_version": "test",
            "memories": [
                {
                    "id": "forged-canon-row-0001",
                    "content": "exported row with forged canon-line metadata",
                    "tags": ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
                    "source": "cli",
                    "status": "published",
                    "metadata": dict(forged_stamps),
                },
                {
                    "id": "task-envelope-row-0002",
                    "content": "exported row with a client task envelope",
                    "tags": ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
                    "source": "cli",
                    "status": "published",
                    "metadata": {"canon": dict(task_env)},
                },
            ],
            "projects": [],
        }
        out = tmp_path / "crafted.json"
        out.write_text(json.dumps(payload), encoding="utf-8")
        return out

    def test_import_strips_forged_canon_line_keys(self, mgr, tmp_path):
        out = self._crafted_export(tmp_path)
        result = run_import(mgr, out, mode=ImportMode.MERGE)
        assert result.imported == 2
        assert not result.errors

        forged = mgr.sqlite.get("forged-canon-row-0001")
        assert forged is not None
        assert not CHECKPOINT_STAMP_KEYS & set(forged.metadata), (
            "stamps + checkpoint-type canon must not survive an untrusted import"
        )
        assert "canon_warnings" not in forged.metadata
        assert forged.metadata["harmless"] == 1  # innocent keys survive
        assert any("stripped canon-line" in w for w in result.warnings), (
            "the strip is reported as a warning line"
        )

        task_row = mgr.sqlite.get("task-envelope-row-0002")
        assert task_row is not None
        assert task_row.metadata["canon"]["type"] == "task", (
            "client task envelopes are peer data (canon §2) and persist"
        )

    def test_trusted_restore_keeps_canon_line_keys(self, mgr, tmp_path):
        out = self._crafted_export(tmp_path)
        result = run_import(mgr, out, mode=ImportMode.MERGE, trusted_restore=True)
        assert result.imported == 2
        assert not result.errors
        assert not any("stripped canon-line" in w for w in result.warnings)

        forged = mgr.sqlite.get("forged-canon-row-0001")
        assert forged is not None
        assert forged.metadata["checkpoint_agent"] == "alice"
        assert forged.metadata["checkpoint_dedup_key"] == "deadbeef"
        assert forged.metadata["canon"]["type"] == "checkpoint"
        assert forged.metadata["canon_warnings"][0]["code"] == "CANON-E-TITLE"


# ---------------------------------------------------------------------------
# Import — restore mode
# ---------------------------------------------------------------------------


class TestImportRestore:
    def test_restore_without_confirm_refuses(self, mgr, tmp_path):
        _add_memory(mgr, "x")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)
        result = run_import(mgr, out, mode=ImportMode.RESTORE, confirm=False)
        assert result.errors
        assert any("confirm" in e for e in result.errors)

    def test_restore_wipes_and_imports(self, mgr, tmp_path):
        """Restore mode wipes the DB then imports the snapshot."""
        _add_memory(mgr, "keep me")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)

        # Add a memory that should be wiped by restore.
        _add_memory(mgr, "should be wiped")
        assert mgr.sqlite.count() == 2

        result = run_import(mgr, out, mode=ImportMode.RESTORE, confirm=True)
        assert result.imported == 1
        assert mgr.sqlite.count() == 1
        mem = mgr.sqlite.list_all(limit=10)[0]
        assert mem.content == "keep me"

    def test_restore_dry_run_does_not_wipe(self, mgr, tmp_path):
        _add_memory(mgr, "a")
        _add_memory(mgr, "b")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)
        before = mgr.sqlite.count()
        result = run_import(mgr, out, mode=ImportMode.RESTORE, dry_run=True)
        assert result.dry_run is True
        assert mgr.sqlite.count() == before  # nothing wiped


# ---------------------------------------------------------------------------
# Import — dry-run
# ---------------------------------------------------------------------------


class TestImportDryRun:
    def test_dry_run_validates_without_writing(self, mgr, tmp_path):
        _add_memory(mgr, "x")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)

        # Use a fresh empty manager with an isolated DB.
        settings2 = Settings(
            vesma={
                "vault_path": str(tmp_path / "vault2"),
                "data_dir": str(tmp_path / "data2"),
                "db_name": "test-export-2.db",
                "auto_filter": False,
            },
            embedding={"provider": "onnx"},
        )
        settings2.resolve_paths()
        mgr2 = MemoryManager(settings2)
        mock = MagicMock()
        mock.embed.return_value = [0.1] * 384
        mgr2._embedder = mock
        try:
            before = mgr2.sqlite.count()
            result = run_import(mgr2, out, mode=ImportMode.MERGE, dry_run=True)
            assert result.dry_run is True
            assert result.imported == 1
            assert mgr2.sqlite.count() == before  # nothing written
        finally:
            mgr2.close()

    def test_dry_run_reports_format_version(self, mgr, tmp_path):
        _add_memory(mgr, "x")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)
        result = run_import(mgr, out, mode=ImportMode.MERGE, dry_run=True)
        assert result.format_version == "1.0"

    def test_dry_run_on_missing_file_reports_error(self, mgr, tmp_path):
        result = run_import(mgr, tmp_path / "nope.json", mode=ImportMode.MERGE, dry_run=True)
        assert result.errors
        assert any("not found" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Import — format version mismatch
# ---------------------------------------------------------------------------


class TestImportFormatVersion:
    def test_unknown_format_version_still_imports_with_warning(self, mgr, tmp_path):
        """A future format_version is accepted (forward-compat) — the caller
        decides whether to trust it. We do not hard-fail on a higher version."""
        _add_memory(mgr, "x")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)
        payload = json.loads(out.read_text())
        payload["format_version"] = "99.0"
        out.write_text(json.dumps(payload))

        settings2 = Settings(
            vesma={
                "vault_path": str(tmp_path / "vault2"),
                "data_dir": str(tmp_path / "data2"),
                "db_name": "test-export-2.db",
                "auto_filter": False,
            },
            embedding={"provider": "onnx"},
        )
        settings2.resolve_paths()
        mgr2 = MemoryManager(settings2)
        mock = MagicMock()
        mock.embed.return_value = [0.1] * 384
        mgr2._embedder = mock
        try:
            result = run_import(mgr2, out, mode=ImportMode.MERGE)
            assert result.format_version == "99.0"
            assert result.imported == 1
        finally:
            mgr2.close()


# ---------------------------------------------------------------------------
# Import — encrypted file
# ---------------------------------------------------------------------------


class TestImportEncrypted:
    def test_import_encrypted_with_passphrase(self, mgr, tmp_path):
        _add_memory(mgr, "secret")
        out = tmp_path / "backup.json.enc"
        run_export(
            mgr,
            fmt=ExportFormat.JSON,
            output=out,
            encrypt=True,
            passphrase="pw",
        )
        settings2 = Settings(
            vesma={
                "vault_path": str(tmp_path / "vault2"),
                "data_dir": str(tmp_path / "data2"),
                "db_name": "test-export-2.db",
                "auto_filter": False,
            },
            embedding={"provider": "onnx"},
        )
        settings2.resolve_paths()
        mgr2 = MemoryManager(settings2)
        mock = MagicMock()
        mock.embed.return_value = [0.1] * 384
        mgr2._embedder = mock
        try:
            result = run_import(mgr2, out, mode=ImportMode.MERGE, passphrase="pw")
            assert result.imported == 1
            assert result.errors == []
        finally:
            mgr2.close()

    def test_import_encrypted_without_passphrase_errors(self, mgr, tmp_path):
        _add_memory(mgr, "secret")
        out = tmp_path / "backup.json.enc"
        run_export(
            mgr,
            fmt=ExportFormat.JSON,
            output=out,
            encrypt=True,
            passphrase="pw",
        )
        result = run_import(mgr, out, mode=ImportMode.MERGE)
        assert result.errors
        assert any("passphrase" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Import — SQLite snapshot restore
# ---------------------------------------------------------------------------


class TestImportSQLite:
    def test_sqlite_restore_replaces_db(self, mgr, tmp_path):
        _add_memory(mgr, "snapshot content")
        out = tmp_path / "backup.tar.gz"
        run_export(mgr, fmt=ExportFormat.SQLITE, output=out)

        # Add a memory after the snapshot — it should be gone after restore.
        _add_memory(mgr, "post-snapshot")
        assert mgr.sqlite.count() == 2

        backup_dir = tmp_path / "backup"
        result = run_import(
            mgr,
            out,
            mode=ImportMode.RESTORE,
            confirm=True,
            backup_dir=backup_dir,
        )
        assert result.imported == 1
        assert mgr.sqlite.count() == 1
        # The backup dir should contain the pre-restore DB (named after db_name).
        assert (backup_dir / mgr.settings.vesma.db_name).exists()

    def test_sqlite_dry_run_validates_without_replacing(self, mgr, tmp_path):
        _add_memory(mgr, "x")
        out = tmp_path / "backup.tar.gz"
        run_export(mgr, fmt=ExportFormat.SQLITE, output=out)
        before = mgr.sqlite.count()
        result = run_import(mgr, out, mode=ImportMode.RESTORE, dry_run=True)
        assert result.dry_run is True
        assert result.imported == 1
        assert mgr.sqlite.count() == before
