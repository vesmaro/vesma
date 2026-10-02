"""Integration tests for the ``vesma_export`` MCP tool (#84).

Covers the federation export surface exposed through MCP. The tool is a
thin wrapper over :func:`vesmaro.cli.export.run_export`; these tests drive
the real dispatch path (``_dispatch("vesma_export", ...)``) against an
isolated tmp DB so the #86 redaction / no-federate exclusion is verified
end-to-end through the MCP surface.

All secret fixtures are OBVIOUSLY FAKE (per ``sensitive-data.instructions.md``).
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.mcp_server import _dispatch
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus

# ---------------------------------------------------------------------------
# Fixtures — mirror tests/test_no_federate.py conventions (isolated tmp DB).
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp_path / "vault"),
            "data_dir": str(tmp_path / "data"),
            "db_name": "test-mcp-export.db",
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


@pytest.fixture(autouse=True)
def _scrub_export_passphrase() -> Generator[None, None, None]:
    """Ensure VESMA_EXPORT_PASSPHRASE never leaks across tests."""
    saved = os.environ.pop("VESMA_EXPORT_PASSPHRASE", None)
    yield
    if saved is not None:
        os.environ["VESMA_EXPORT_PASSPHRASE"] = saved
    else:
        os.environ.pop("VESMA_EXPORT_PASSPHRASE", None)


@pytest.fixture(autouse=True)
def _patch_manager(mgr: MemoryManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``vesmaro.mcp_server.get_manager`` to return the test's isolated ``mgr``.

    ``_dispatch`` calls the module-level ``get_manager()`` singleton which would
    otherwise resolve to a real MemoryManager backed by ``~/.mnemos``. We point
    it at the per-test ``mgr`` fixture so export/import drives the isolated
    tmp DB.
    """
    import vesmaro.mcp_server as mcp_server

    monkeypatch.setattr(mcp_server, "get_manager", lambda: mgr)


def _add(
    mgr: MemoryManager,
    content: str,
    *,
    tags: list[str] | None = None,
    project: str = "mnemos",
    agent: str = "tech-lead",
) -> str:
    tags = tags or [f"project:{project}", f"agent:{agent}", "mnemos:learning"]
    mem = mgr.add(
        MemoryCreate(
            content=content,
            tags=tags,
            source=MemorySource.CLI,
            status=MemoryStatus.PUBLISHED,
        ),
        project=project,
        agent=agent,
    )
    return str(mem.id)


async def _export(mgr: MemoryManager, **args: Any) -> dict[str, Any]:
    """Invoke the MCP dispatch for vesma_export and parse the JSON dict result.

    ``get_manager`` is patched to return the test's ``mgr`` by the
    ``_patch_manager`` autouse fixture, so ``_dispatch`` drives the test's
    isolated MemoryManager.
    """
    result = await _dispatch("vesma_export", args)
    assert isinstance(result, dict), f"expected dict result, got {type(result)}: {result!r}"
    return result


# ── Happy path ────────────────────────────────────────────────────────────────


class TestExportHappyPath:
    async def test_json_export_writes_file_and_metadata(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _add(mgr, "first memory")
        _add(mgr, "second memory")
        out = tmp_path / "backup.json"

        result = await _export(mgr, output_path=str(out))

        assert out.exists(), "export file must be written to disk"
        assert "error" not in result
        assert result["path"] == str(out)
        assert result["format"] == "json"
        assert result["compress"] == "none"
        assert result["encrypted"] is False
        assert result["memory_count"] == 2
        assert result["bytes"] == out.stat().st_size
        payload = json.loads(out.read_text())
        assert len(payload["memories"]) == 2

    async def test_export_with_project_filter(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _add(mgr, "in scope", project="mnemos")
        _add(mgr, "out of scope", project="other")
        out = tmp_path / "filtered.json"

        result = await _export(mgr, output_path=str(out), project="mnemos")

        assert result["memory_count"] == 1
        payload = json.loads(out.read_text())
        assert all(m["project"] == "mnemos" for m in payload["memories"])

    async def test_export_with_status_filter(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _add(mgr, "published one", project="mnemos")
        out = tmp_path / "status.json"

        result = await _export(mgr, output_path=str(out), status="published")

        assert result["memory_count"] == 1
        payload = json.loads(out.read_text())
        assert payload["memories"][0]["status"] == "published"

    async def test_export_with_tags_filter(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _add(
            mgr,
            "tagged",
            tags=["project:mnemos", "agent:tech-lead", "mnemos:learning", "severity:high"],
        )
        _add(mgr, "untagged")
        out = tmp_path / "tags.json"

        result = await _export(mgr, output_path=str(out), tags=["severity:high"])

        assert result["memory_count"] == 1
        payload = json.loads(out.read_text())
        assert "severity:high" in payload["memories"][0]["tags"]

    async def test_export_with_since_until_filters(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _add(mgr, "old memory")
        out = tmp_path / "time.json"

        # since = far past, until = far future → both should match.
        result = await _export(
            mgr,
            output_path=str(out),
            since="2000-01-01T00:00:00",
            until="2099-12-31T23:59:59",
        )

        assert result["memory_count"] == 1


# ── SQLite format ─────────────────────────────────────────────────────────────


class TestExportSqlite:
    async def test_sqlite_snapshot_writes_tar_gz(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _add(mgr, "snapshot me")
        out = tmp_path / "backup.tar.gz"

        result = await _export(mgr, output_path=str(out), format="sqlite")

        assert result["format"] == "sqlite"
        assert out.exists()
        with tarfile.open(fileobj=io.BytesIO(out.read_bytes()), mode="r:gz") as tar:
            assert "mnemos.db" in tar.getnames()

    async def test_sqlite_ignores_filters_with_warning(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _add(mgr, "a", project="mnemos")
        _add(mgr, "b", project="other")
        out = tmp_path / "snap.tar.gz"

        # A filter that would match only 1 record in JSON mode.
        result = await _export(mgr, output_path=str(out), format="sqlite", project="mnemos")

        assert result["format"] == "sqlite"
        # SQLite is a full snapshot → memory_count reflects all records.
        assert result["memory_count"] == 2
        assert any("ignored for sqlite" in w.lower() for w in result.get("warnings", []))


# ── Compression ──────────────────────────────────────────────────────────────


class TestExportCompress:
    async def test_gzip_compression(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import gzip

        _add(mgr, "compress me")
        out = tmp_path / "backup.json.gz"

        result = await _export(mgr, output_path=str(out), compress="gzip")

        assert result["compress"] == "gzip"
        assert out.exists()
        raw = gzip.decompress(out.read_bytes())
        payload = json.loads(raw)
        assert len(payload["memories"]) == 1


# ── Encryption ───────────────────────────────────────────────────────────────


class TestExportEncrypt:
    async def test_encrypt_reads_passphrase_from_env(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _add(mgr, "secret memory")
        out = tmp_path / "encrypted.bin"
        # OBVIOUSLY FAKE passphrase per sensitive-data.instructions.md.
        monkeypatch.setenv("VESMA_EXPORT_PASSPHRASE", "test-passphrase-EXAMPLE")

        result = await _export(mgr, output_path=str(out), encrypt=True)

        assert result["encrypted"] is True
        assert out.exists()
        assert out.read_bytes().startswith(b"MNEMOS1"), (
            "encrypted export must carry the magic header"
        )

    async def test_encrypt_without_env_var_errors(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _add(mgr, "secret memory")
        out = tmp_path / "encrypted.bin"
        monkeypatch.delenv("VESMA_EXPORT_PASSPHRASE", raising=False)

        result = await _export(mgr, output_path=str(out), encrypt=True)

        assert "error" in result
        assert "VESMA_EXPORT_PASSPHRASE" in result["error"]
        assert not out.exists(), "no file should be written when passphrase is missing"


# ── #86 inheritance: no-federate exclusion + secret redaction ─────────────────


class TestExportFederationInheritance:
    async def test_no_federate_excluded_from_export(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Clean record passes; secret record gets auto-tagged mnemos:no-federate
        # by the write-path scanner (#86 Layer 1) and is excluded from export.
        _add(mgr, "clean note")
        _add(mgr, f"key=AKIA{'T' * 16}")  # fake AWS key → auto no-federate
        out = tmp_path / "backup.json"

        result = await _export(mgr, output_path=str(out))

        assert result["memory_count"] == 1
        payload = json.loads(out.read_text())
        assert len(payload["memories"]) == 1
        assert "AKIA" not in json.dumps(payload), "no-federate record must be fully excluded"
        assert payload["redaction_summary"]["excluded_no_federate"] == 1

    async def test_secret_redacted_in_passing_record(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bypass the write-path scanner by writing directly to the store so the
        # record passes the no-federate filter, then verify export-time redaction.
        mem_id = _add(mgr, f"key=AKIA{'T' * 16}")
        mem = mgr.get(mem_id)
        assert mem is not None
        new_tags = [t for t in mem.tags if t != "mnemos:no-federate"]
        mgr.sqlite.update_fields(mem_id, tags=new_tags)
        out = tmp_path / "backup.json"

        result = await _export(mgr, output_path=str(out))

        assert result["memory_count"] == 1
        payload = json.loads(out.read_text())
        content = payload["memories"][0]["content"]
        assert "<REDACTED:aws-key>" in content
        assert "AKIA" + "T" * 16 not in content
        assert payload["redaction_summary"]["redacted_records"] >= 1


# ── Argument validation ──────────────────────────────────────────────────────


class TestExportValidation:
    async def test_missing_output_path_errors(
        self, mgr: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = await _export(mgr)
        assert "error" in result
        assert "output_path" in result["error"]

    async def test_relative_output_path_errors(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = await _export(mgr, output_path="relative/backup.json")
        assert "error" in result
        assert "absolute" in result["error"]

    async def test_invalid_format_errors(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = await _export(mgr, output_path=str(tmp_path / "x.json"), format="xml")
        assert "error" in result
        assert "format" in result["error"].lower()

    async def test_invalid_compress_errors(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = await _export(mgr, output_path=str(tmp_path / "x.json"), compress="bzip2")
        assert "error" in result
        assert "compress" in result["error"].lower()

    async def test_invalid_status_errors(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = await _export(mgr, output_path=str(tmp_path / "x.json"), status="bogus")
        assert "error" in result
        assert "status" in result["error"].lower()

    async def test_invalid_since_timestamp_errors(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = await _export(mgr, output_path=str(tmp_path / "x.json"), since="not-a-date")
        assert "error" in result
        assert "since" in result["error"].lower()
