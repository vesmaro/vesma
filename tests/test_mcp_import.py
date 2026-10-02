"""Integration tests for the ``vesma_import`` MCP tool (#84).

Covers the federation import surface exposed through MCP. The tool is a
thin wrapper over :func:`vesma.cli.import_.run_import`; these tests drive
the real dispatch path (``_dispatch("vesma_import", ...)``) against an
isolated tmp DB so the #86 import validation (schema drift, oversized
content, prompt-injection logging) is verified end-to-end through the MCP
surface.

All passphrases are OBVIOUSLY FAKE (per ``sensitive-data.instructions.md``).
"""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from vesma.cli.export import ExportFormat, run_export
from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.mcp_server import _dispatch
from vesma.models import MemoryCreate, MemorySource, MemoryStatus

# ---------------------------------------------------------------------------
# Fixtures — mirror tests/test_no_federate.py conventions (isolated tmp DB).
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp_path / "vault"),
            "data_dir": str(tmp_path / "data"),
            "db_name": "test-mcp-import.db",
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
def _scrub_passphrase_env() -> Generator[None, None, None]:
    """Ensure no passphrase env vars leak across tests."""
    saved: dict[str, str] = {}
    for key in list(os.environ):
        if "PASSPHRASE" in key:
            saved[key] = os.environ.pop(key)
    yield
    for key, val in saved.items():
        os.environ[key] = val


@pytest.fixture(autouse=True)
def _patch_manager(mgr: MemoryManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``vesma.mcp_server.get_manager`` to return the test's isolated ``mgr``.

    ``_dispatch`` calls the module-level ``get_manager()`` singleton which would
    otherwise resolve to a real MemoryManager backed by ``~/.mnemos``. We point
    it at the per-test ``mgr`` fixture so export/import drives the isolated
    tmp DB.
    """
    import vesma.mcp_server as mcp_server

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


async def _import(mgr: MemoryManager, **args: Any) -> dict[str, Any]:
    """Invoke the MCP dispatch for vesma_import and parse the JSON dict result.

    ``get_manager`` is patched to return the test's ``mgr`` by the
    ``_patch_manager`` autouse fixture, so ``_dispatch`` drives the test's
    isolated MemoryManager.
    """
    result = await _dispatch("vesma_import", args)
    assert isinstance(result, dict), f"expected dict result, got {type(result)}: {result!r}"
    return result


def _make_export_file(mgr: MemoryManager, out: Path) -> Path:
    """Seed one memory and write a JSON export the tests can import."""
    _add(mgr, "seed memory for export")
    run_export(mgr, fmt=ExportFormat.JSON, output=out)
    # Drop the seed memory so the import target starts empty (or near-empty).
    return out


# ── Happy path ────────────────────────────────────────────────────────────────


class TestImportHappyPath:
    async def test_merge_inserts_memories(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        export_file = _make_export_file(mgr, tmp_path / "source.json")
        # Wipe the live DB so the import has something to insert.
        mgr.sqlite.wipe_all()
        mgr.vectors.wipe()
        assert mgr.sqlite.count() == 0

        result = await _import(mgr, source_path=str(export_file), mode="merge")

        assert "error" not in result, f"unexpected error: {result}"
        assert result["mode"] == "merge"
        assert result["dry_run"] is False
        assert result["imported"] == 1
        assert result["errors"] == []
        assert result["format_version"] == "1.0"
        assert mgr.sqlite.count() == 1

    async def test_merge_overwrite_updates_existing(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        export_file = _make_export_file(mgr, tmp_path / "source.json")
        # The seed memory already exists in the live DB (same id).

        result = await _import(mgr, source_path=str(export_file), mode="merge", overwrite=True)

        assert "error" not in result
        # overwrite=True → existing record updated, not skipped.
        assert result["updated"] == 1
        assert result["skipped"] == 0
        assert result["imported"] == 0

    async def test_merge_skips_existing_without_overwrite(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        export_file = _make_export_file(mgr, tmp_path / "source.json")

        result = await _import(mgr, source_path=str(export_file), mode="merge", overwrite=False)

        assert "error" not in result
        assert result["skipped"] == 1
        assert result["updated"] == 0


# ── Restore mode + confirm gate ───────────────────────────────────────────────


class TestImportRestore:
    async def test_restore_without_confirm_errors(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        export_file = _make_export_file(mgr, tmp_path / "source.json")

        result = await _import(mgr, source_path=str(export_file), mode="restore")

        assert "error" in result
        assert "confirm" in result["error"].lower()
        # Nothing was wiped.
        assert mgr.sqlite.count() == 1

    async def test_restore_with_confirm_wipes_and_imports(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        export_file = _make_export_file(mgr, tmp_path / "source.json")
        # Add an extra memory that restore mode must wipe.
        _add(mgr, "extra that should be wiped")
        assert mgr.sqlite.count() == 2

        result = await _import(mgr, source_path=str(export_file), mode="restore", confirm=True)

        assert "error" not in result
        assert result["imported"] == 1
        # Only the imported record remains (the extra was wiped).
        assert mgr.sqlite.count() == 1


# ── Dry-run ──────────────────────────────────────────────────────────────────


class TestImportDryRun:
    async def test_dry_run_validates_without_writing(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        export_file = _make_export_file(mgr, tmp_path / "source.json")
        # Wipe so a real import would insert; dry-run must NOT insert.
        mgr.sqlite.wipe_all()
        mgr.vectors.wipe()
        assert mgr.sqlite.count() == 0

        result = await _import(mgr, source_path=str(export_file), mode="merge", dry_run=True)

        assert "error" not in result
        assert result["dry_run"] is True
        # Validation reports the record count but nothing is written.
        assert result["imported"] == 1
        assert mgr.sqlite.count() == 0, "dry-run must not write to the store"


# ── Passphrase via env-var name ───────────────────────────────────────────────


class TestImportPassphraseEnv:
    async def test_passphrase_env_name_reads_value(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Build an encrypted export, then import it back via env-var name.
        _add(mgr, "encrypted seed")
        encrypted_out = tmp_path / "encrypted.bin"
        # OBVIOUSLY FAKE passphrase per sensitive-data.instructions.md.
        passphrase = "test-passphrase-EXAMPLE"
        run_export(
            mgr, fmt=ExportFormat.JSON, output=encrypted_out, encrypt=True, passphrase=passphrase
        )
        # Wipe the live DB so the import inserts the record.
        mgr.sqlite.wipe_all()
        mgr.vectors.wipe()
        monkeypatch.setenv("MCP_IMPORT_TEST_PASSPHRASE", passphrase)

        result = await _import(
            mgr,
            source_path=str(encrypted_out),
            mode="merge",
            passphrase_env="MCP_IMPORT_TEST_PASSPHRASE",
        )

        assert "error" not in result, f"unexpected error: {result}"
        assert result["imported"] == 1
        assert mgr.sqlite.count() == 1

    async def test_passphrase_env_not_set_errors(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        export_file = _make_export_file(mgr, tmp_path / "source.json")
        monkeypatch.delenv("MISSING_PASS_ENV", raising=False)

        result = await _import(
            mgr, source_path=str(export_file), mode="merge", passphrase_env="MISSING_PASS_ENV"
        )

        assert "error" in result
        assert "MISSING_PASS_ENV" in result["error"]

    async def test_passphrase_env_invalid_name_errors(
        self, mgr: MemoryManager, tmp_path: Path
    ) -> None:
        export_file = _make_export_file(mgr, tmp_path / "source.json")

        result = await _import(
            mgr, source_path=str(export_file), mode="merge", passphrase_env="not a valid name!"
        )

        assert "error" in result
        assert "passphrase_env" in result["error"]


# ── #86 inheritance: import validation ─────────────────────────────────────────


class TestImportFederationInheritance:
    async def test_schema_drift_rejected(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Build an export, then inject an unknown field into a memory record.
        export_file = _make_export_file(mgr, tmp_path / "source.json")
        import json

        payload = json.loads(export_file.read_text())
        payload["memories"][0]["this_field_does_not_exist"] = "drift"
        bad_file = tmp_path / "drift.json"
        bad_file.write_text(json.dumps(payload))

        result = await _import(mgr, source_path=str(bad_file), mode="merge")

        assert "error" not in result, (
            "errors are reported in result['errors'], not the top-level error key"
        )
        # Schema drift produces field-level errors recorded in the result.
        assert any("schema" in e or "unknown fields" in e for e in result["errors"]), (
            f"expected schema-drift error, got: {result['errors']}"
        )
        # Nothing was written (validation rejects the whole batch).
        # The seed memory is still the only one present.
        assert mgr.sqlite.count() == 1

    async def test_oversized_content_rejected(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        export_file = _make_export_file(mgr, tmp_path / "source.json")
        import json

        payload = json.loads(export_file.read_text())
        # Inject a content blob exceeding the 1 MiB limit.
        payload["memories"][0]["content"] = "x" * (2 * 1024 * 1024)
        bad_file = tmp_path / "oversized.json"
        bad_file.write_text(json.dumps(payload))

        result = await _import(mgr, source_path=str(bad_file), mode="merge")

        assert any("exceeds max length" in e or "content" in e for e in result["errors"]), (
            f"expected oversized-content error, got: {result['errors']}"
        )

    async def test_prompt_injection_logged_as_warning(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        export_file = _make_export_file(mgr, tmp_path / "source.json")
        import json

        payload = json.loads(export_file.read_text())
        # Legitimate content that discusses injection — must be a WARNING,
        # not a blocking error (per #86 validation design).
        payload["memories"][0]["content"] = (
            "Security research: the <|im_start|> token starts a ChatML turn."
        )
        bad_file = tmp_path / "injection.json"
        bad_file.write_text(json.dumps(payload))

        result = await _import(mgr, source_path=str(bad_file), mode="merge", dry_run=True)

        # Prompt-injection patterns are warnings, not errors.
        assert any("prompt-injection" in w.lower() for w in result["warnings"]), (
            f"expected prompt-injection warning, got: {result['warnings']}"
        )
        # And the validation still passes (valid=True → no top-level error).
        # Dry-run reports the validated count.
        assert result["imported"] == 1


# ── Argument validation ──────────────────────────────────────────────────────


class TestImportValidation:
    async def test_missing_source_path_errors(
        self, mgr: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = await _import(mgr)
        assert "error" in result
        assert "source_path" in result["error"]

    async def test_relative_source_path_errors(
        self, mgr: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = await _import(mgr, source_path="relative/source.json")
        assert "error" in result
        assert "absolute" in result["error"]

    async def test_invalid_mode_errors(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = await _import(mgr, source_path=str(tmp_path / "x.json"), mode="bogus")
        assert "error" in result
        assert "mode" in result["error"].lower()
