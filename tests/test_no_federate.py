"""Integration tests for mnemos:no-federate auto-tagging + export redaction +
import validation (#86).

Covers the federation defence-in-depth Layer 1 (write-path scanner) and
the export/import surfaces. Uses real MemoryManager against an isolated
tmp DB (never touches ~/.mnemos).

All secret fixtures are OBVIOUSLY FAKE (per sensitive-data.instructions.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesmaro.cli.export import ExportFilter, ExportFormat, build_json_payload, run_export
from vesmaro.cli.import_ import (
    DEFAULT_MAX_CONTENT_CHARS,
    ImportMode,
    run_import,
    validate_import_payload,
    validate_import_record,
)
from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus, MemoryUpdate

# ---------------------------------------------------------------------------
# Fixtures — mirror tests/test_export_import.py conventions
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp_path / "vault"),
            "data_dir": str(tmp_path / "data"),
            "db_name": "test-no-federate.db",
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


def _add(mgr: MemoryManager, content: str, *, tags: list[str] | None = None) -> str:
    """Add a memory via the manager write-path (Layer 1 scanner runs)."""
    tags = tags or ["project:mnemos", "agent:tech-lead", "mnemos:learning"]
    mem = mgr.add(
        MemoryCreate(
            content=content, tags=tags, source=MemorySource.CLI, status=MemoryStatus.PUBLISHED
        ),
        project="mnemos",
        agent="tech-lead",
    )
    return mem.id


# ── Layer 1: write-path auto-tagging ───────────────────────────────────────────


class TestWritePathAutoTag:
    def test_secret_in_content_auto_adds_no_federate(self, mgr: MemoryManager) -> None:
        # Fake AWS key fixture (AKIA + 16 uppercase alnum).
        content = f"config has key=AKIA{'T' * 16} for aws"
        mem_id = _add(mgr, content)
        mem = mgr.get(mem_id)
        assert mem is not None
        assert "mnemos:no-federate" in mem.tags

    def test_clean_content_no_tag(self, mgr: MemoryManager) -> None:
        content = "just a normal note about the weather"
        mem_id = _add(mgr, content)
        mem = mgr.get(mem_id)
        assert mem is not None
        assert "mnemos:no-federate" not in mem.tags

    def test_idempotent_on_re_add(self, mgr: MemoryManager) -> None:
        # Adding the same secret twice — each record gets exactly one tag.
        content = f"key=AKIA{'T' * 16}"
        id1 = _add(mgr, content)
        id2 = _add(mgr, content)
        mem1 = mgr.get(id1)
        mem2 = mgr.get(id2)
        assert mem1 is not None and mem2 is not None
        assert mem1.tags.count("mnemos:no-federate") == 1
        assert mem2.tags.count("mnemos:no-federate") == 1

    def test_pre_existing_no_federate_not_duplicated(self, mgr: MemoryManager) -> None:
        # Caller already passes the tag — scanner must not add a second one.
        content = f"key=AKIA{'T' * 16}"
        tags = ["project:mnemos", "agent:tech-lead", "mnemos:learning", "mnemos:no-federate"]
        mem = mgr.add(
            MemoryCreate(
                content=content, tags=tags, source=MemorySource.CLI, status=MemoryStatus.PUBLISHED
            ),
            project="mnemos",
            agent="tech-lead",
        )
        assert mem.tags.count("mnemos:no-federate") == 1


# ── Layer 1: update-path auto-tagging (S-2) ─────────────────────────────────────


class TestUpdatePathAutoTag:
    def test_secret_in_updated_content_auto_adds_no_federate(self, mgr: MemoryManager) -> None:
        # S-2: updating content with a secret must auto-add no-federate.
        mem_id = _add(mgr, "clean content no secrets here")
        mem = mgr.get(mem_id)
        assert mem is not None
        assert "mnemos:no-federate" not in mem.tags
        # Update with content containing a fake AWS key.
        mgr.update(mem_id, MemoryUpdate(content=f"now has key=AKIA{'T' * 16}"))
        updated = mgr.get(mem_id)
        assert updated is not None
        assert "mnemos:no-federate" in updated.tags

    def test_update_without_content_does_not_scan(self, mgr: MemoryManager) -> None:
        # S-2: updating only tags/metadata must NOT run the scanner.
        mem_id = _add(mgr, "clean content no secrets here")
        mgr.update(mem_id, MemoryUpdate(tags=["project:mnemos", "agent:tech-lead", "mnemos:rule"]))
        updated = mgr.get(mem_id)
        assert updated is not None
        assert "mnemos:no-federate" not in updated.tags

    def test_update_idempotent_no_duplicate_tag(self, mgr: MemoryManager) -> None:
        # S-2: re-updating with different secret content must not duplicate.
        mem_id = _add(mgr, "clean content no secrets here")
        mgr.update(mem_id, MemoryUpdate(content=f"key=AKIA{'T' * 16}"))
        mgr.update(mem_id, MemoryUpdate(content=f"different key=AKIA{'Z' * 16}"))
        updated = mgr.get(mem_id)
        assert updated is not None
        assert updated.tags.count("mnemos:no-federate") == 1


# ── ADR-0019 Phase A: ingest audit events ─────────────────────────────────────


class TestIngestAudit:
    """One structured verdict per write, correlated by memory id:
    ``ingest scan: id=… scanner=ok|error patterns={…}`` — pattern names
    and counts only, raw values never logged."""

    def _audit_lines(self, caplog):
        return [r for r in caplog.records if "ingest scan" in r.message]

    def test_clean_add_logs_ok_verdict(self, mgr: MemoryManager, caplog) -> None:
        with caplog.at_level("INFO", logger="vesmaro.manager"):
            mem_id = _add(mgr, "just a normal note about the weather")
        lines = self._audit_lines(caplog)
        assert lines, "every write must audit its scan verdict"
        assert "scanner=ok" in lines[0].message
        assert "patterns={}" in lines[0].message
        assert mem_id[:8] in lines[0].message, "audit correlates by memory id"

    def test_secret_add_logs_found_patterns(self, mgr: MemoryManager, caplog) -> None:
        with caplog.at_level("INFO", logger="vesmaro.manager"):
            mem_id = _add(mgr, f"config has key=AKIA{'T' * 16} for aws")
        lines = self._audit_lines(caplog)
        assert lines and "aws-key" in lines[0].message
        assert mem_id[:8] in lines[0].message

    def test_update_with_content_logs_verdict(self, mgr: MemoryManager, caplog) -> None:
        mem_id = _add(mgr, "clean content no secrets here")
        caplog.clear()
        with caplog.at_level("INFO", logger="vesmaro.manager"):
            mgr.update(mem_id, MemoryUpdate(content="still clean content"))
        assert self._audit_lines(caplog), "content updates audit their re-scan"

    def test_update_without_content_does_not_audit_rescan(self, mgr: MemoryManager, caplog) -> None:
        mem_id = _add(mgr, "clean content no secrets here")
        caplog.clear()
        with caplog.at_level("INFO", logger="vesmaro.manager"):
            mgr.update(mem_id, MemoryUpdate(quality_score=0.5))
        assert not self._audit_lines(caplog), "no content → no scan → no verdict line"

    def test_scanner_error_logs_error_verdict_write_survives(
        self, mgr: MemoryManager, caplog, monkeypatch
    ) -> None:
        """Layer 1 stays non-fatal (unchanged): a scanner error never
        blocks the write — but the audit must record scanner=error."""
        import vesmaro.secrets_detector as sd

        def _boom(content: str):
            raise RuntimeError("scanner down")

        monkeypatch.setattr(sd, "detect_secrets", _boom)
        with caplog.at_level("INFO", logger="vesmaro.manager"):
            mem = mgr.add(
                MemoryCreate(
                    content="ordinary content during scanner outage",
                    tags=["project:mnemos", "agent:tech-lead", "mnemos:learning"],
                    source=MemorySource.CLI,
                ),
                project="mnemos",
                agent="tech-lead",
            )
        assert mgr.get(mem.id) is not None, "the write must survive a scanner error"
        lines = self._audit_lines(caplog)
        assert lines and "scanner=error" in lines[0].message


# ── Tag removal with confirmation ──────────────────────────────────────────────


class TestRemoveNoFederate:
    def test_requires_confirmation(self, mgr: MemoryManager) -> None:
        content = f"key=AKIA{'T' * 16}"
        mem_id = _add(mgr, content)
        # Without confirm — must not mutate.
        report = mgr.remove_no_federate(mem_id, confirm=False)
        assert report["requires_confirmation"] is True
        assert report["removed"] is False
        mem = mgr.get(mem_id)
        assert mem is not None
        assert "mnemos:no-federate" in mem.tags

    def test_re_detects_when_secret_still_present(self, mgr: MemoryManager) -> None:
        content = f"key=AKIA{'T' * 16}"
        mem_id = _add(mgr, content)
        # Confirm=True, but secret still in content — tag must be re-added.
        report = mgr.remove_no_federate(mem_id, confirm=True)
        assert report["re_detected"] is True
        assert report["removed"] is False
        mem = mgr.get(mem_id)
        assert mem is not None
        assert "mnemos:no-federate" in mem.tags

    def test_removes_when_content_clean(self, mgr: MemoryManager) -> None:
        # Add a record with the tag manually (no secret in content), then
        # remove with confirm — should succeed because content is clean.
        tags = ["project:mnemos", "agent:tech-lead", "mnemos:learning", "mnemos:no-federate"]
        mem = mgr.add(
            MemoryCreate(
                content="clean content",
                tags=tags,
                source=MemorySource.CLI,
                status=MemoryStatus.PUBLISHED,
            ),
            project="mnemos",
            agent="tech-lead",
        )
        report = mgr.remove_no_federate(mem.id, confirm=True)
        assert report["removed"] is True
        assert report["re_detected"] is False
        updated = mgr.get(mem.id)
        assert updated is not None
        assert "mnemos:no-federate" not in updated.tags

    def test_no_tag_no_op(self, mgr: MemoryManager) -> None:
        mem_id = _add(mgr, "clean content")
        report = mgr.remove_no_federate(mem_id, confirm=True)
        assert report["removed"] is False
        assert "does not carry" in report.get("note", "")

    def test_missing_memory_returns_error(self, mgr: MemoryManager) -> None:
        report = mgr.remove_no_federate("nonexistent-id", confirm=True)
        assert "error" in report


# ── Export redaction + exclusion ───────────────────────────────────────────────


class TestExportRedaction:
    def test_no_federate_excluded_from_export(self, mgr: MemoryManager, tmp_path: Path) -> None:
        # Add a clean record and a secret record.
        _add(mgr, "clean note")
        _add(mgr, f"key=AKIA{'T' * 16}")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)
        payload = json.loads(out.read_text())
        # The secret record should be excluded.
        assert len(payload["memories"]) == 1
        assert "AKIA" not in json.dumps(payload)

    def test_secret_redacted_in_export(self, mgr: MemoryManager, tmp_path: Path) -> None:
        # Add a record with a secret that the write-path scanner tags.
        # The export excludes it (no-federate). To test redaction on a record
        # that passes the filter, we manually add a record where the secret
        # is in a form the write-path scanner catches but we remove the tag
        # first... Actually the simplest way: add a record with content that
        # contains a secret pattern the scanner does NOT auto-tag (so it
        # passes the filter) — but our scanner catches all the patterns.
        # Instead: add a record, manually remove the no-federate tag (with
        # confirm), then export — content is still redacted at export.
        # But remove_no_federate re-detects and re-adds the tag.
        #
        # The cleanest approach: write content with a secret directly to the
        # store (bypassing the scanner) by using sqlite.save, then export.
        # We use the manager's add but with content that the scanner catches,
        # then we patch the record's tags to remove no-federate directly.
        mem_id = _add(mgr, f"key=AKIA{'T' * 16}")
        # Manually strip the no-federate tag via update_fields to simulate an
        # operator override (this bypasses the re-detection guard, which is
        # the operator's explicit choice).
        mem = mgr.get(mem_id)
        assert mem is not None
        new_tags = [t for t in mem.tags if t != "mnemos:no-federate"]
        mgr.sqlite.update_fields(mem_id, tags=new_tags)

        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)
        payload = json.loads(out.read_text())
        # Record passes the filter now (no no-federate tag).
        assert len(payload["memories"]) == 1
        # Content should be redacted.
        content = payload["memories"][0]["content"]
        assert "<REDACTED:aws-key>" in content
        assert "AKIA" + "T" * 16 not in content
        # Summary records the redaction.
        assert payload["redaction_summary"]["redacted_records"] >= 1

    def test_clean_export_no_redaction(self, mgr: MemoryManager, tmp_path: Path) -> None:
        _add(mgr, "just a clean note")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)
        payload = json.loads(out.read_text())
        assert payload["redaction_summary"]["redacted_records"] == 0
        assert payload["redaction_summary"]["excluded_no_federate"] == 0

    def test_build_json_payload_summary(self, mgr: MemoryManager) -> None:
        _add(mgr, "clean")
        _add(mgr, f"key=AKIA{'T' * 16}")
        payload = build_json_payload(mgr, ExportFilter())
        assert "redaction_summary" in payload
        assert payload["redaction_summary"]["excluded_no_federate"] == 1

    def test_secret_in_title_redacted_in_export(self, mgr: MemoryManager, tmp_path: Path) -> None:
        # S-1: a secret in the title must be redacted in the export JSON.
        # We bypass the write-path scanner by writing directly to sqlite so
        # the record passes the no-federate filter, then verify redaction.
        secret_title = f"AKIA{'T' * 16} leaked in title"
        mem = mgr.add(
            MemoryCreate(
                content="clean content here",
                title=secret_title,
                tags=["project:mnemos", "agent:tech-lead", "mnemos:learning"],
                source=MemorySource.CLI,
                status=MemoryStatus.PUBLISHED,
            ),
            project="mnemos",
            agent="tech-lead",
        )
        # Strip the no-federate tag the scanner added so the record passes
        # the export filter and we can assert title-level redaction.
        new_tags = [t for t in mem.tags if t != "mnemos:no-federate"]
        mgr.sqlite.update_fields(mem.id, tags=new_tags)

        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)
        payload = json.loads(out.read_text())
        assert len(payload["memories"]) == 1
        title = payload["memories"][0]["title"]
        assert "<REDACTED:aws-key>" in title
        assert "AKIA" + "T" * 16 not in title
        assert payload["redaction_summary"]["redacted_records"] >= 1


# ── Import validation ──────────────────────────────────────────────────────────


class TestImportValidation:
    def test_valid_record_passes(self) -> None:
        entry = {
            "id": "test-1",
            "content": "normal content",
            "tags": ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
            "title": "A title",
        }
        errors, warnings = validate_import_record(entry)
        assert errors == []
        # No prompt-injection warnings on clean content.
        assert all("prompt-injection" not in w for w in warnings)

    def test_oversized_content_rejected(self) -> None:
        entry = {
            "id": "test-1",
            "content": "x" * (DEFAULT_MAX_CONTENT_CHARS + 1),
            "tags": ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
        }
        errors, _ = validate_import_record(entry, max_content_chars=DEFAULT_MAX_CONTENT_CHARS)
        assert any("exceeds max length" in e for e in errors)

    def test_control_chars_rejected(self) -> None:
        entry = {
            "id": "test-1",
            "content": "has\x00null byte",
            "tags": ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
        }
        errors, _ = validate_import_record(entry)
        assert any("control character" in e for e in errors)

    def test_newline_tab_allowed(self) -> None:
        entry = {
            "id": "test-1",
            "content": "line one\nline two\tindented",
            "tags": ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
        }
        errors, _ = validate_import_record(entry)
        assert errors == []

    def test_schema_drift_rejected(self) -> None:
        entry = {
            "id": "test-1",
            "content": "ok",
            "tags": ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
            "unknown_field": "should be rejected",
        }
        errors, _ = validate_import_record(entry)
        assert any("unknown fields" in e for e in errors)
        assert "unknown_field" in "\n".join(errors)

    def test_tag_contract_violation_rejected(self) -> None:
        entry = {
            "id": "test-1",
            "content": "ok",
            "tags": ["only-one-tag"],
        }
        errors, _ = validate_import_record(entry)
        assert any("tag contract" in e for e in errors)

    def test_too_many_tags_rejected(self) -> None:
        entry = {
            "id": "test-1",
            "content": "ok",
            "tags": [f"tag{i}" for i in range(33)],
        }
        errors, _ = validate_import_record(entry)
        # Will also fail tag contract (no project/agent/mnemos), but the
        # count error should be present.
        assert any("exceeds max count" in e for e in errors)

    def test_oversized_title_rejected(self) -> None:
        entry = {
            "id": "test-1",
            "content": "ok",
            "title": "T" * 257,
            "tags": ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
        }
        errors, _ = validate_import_record(entry)
        assert any("title" in e and "max length" in e for e in errors)

    def test_prompt_injection_warns_not_blocks(self) -> None:
        entry = {
            "id": "test-1",
            "content": (
                "Research note: the 'ignore previous instructions' attack is documented here."
            ),
            "tags": ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
        }
        errors, warnings = validate_import_record(entry)
        assert errors == []  # NOT blocked.
        assert any("prompt-injection" in w for w in warnings)

    def test_missing_content_rejected(self) -> None:
        entry = {
            "id": "test-1",
            "tags": ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
        }
        errors, _ = validate_import_record(entry)
        assert any("missing required field" in e for e in errors)


class TestValidateImportPayload:
    def test_dry_run_report(self) -> None:
        payload = {
            "format_version": "1.0",
            "memories": [
                {
                    "id": "a",
                    "content": "ok",
                    "tags": ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
                },
                {
                    "id": "b",
                    "content": "has\x00null",
                    "tags": ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
                },
            ],
            "projects": [],
        }
        report = validate_import_payload(payload)
        assert report.records_validated == 2
        assert report.valid is False  # second record has control char.
        assert any("b:" in e for e in report.errors)


# ── Import end-to-end (integration with the manager) ──────────────────────────


class TestImportEndToEnd:
    def test_dry_run_validates_without_writing(self, mgr: MemoryManager, tmp_path: Path) -> None:
        # Build a valid export then dry-run import on a fresh manager.
        _add(mgr, "valid note one")
        _add(mgr, "valid note two")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)

        settings2 = Settings(
            vesma={
                "vault_path": str(tmp_path / "vault2"),
                "data_dir": str(tmp_path / "data2"),
                "db_name": "test-no-federate-2.db",
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
            assert result.imported == 2
            assert mgr2.sqlite.count() == before  # nothing written
        finally:
            mgr2.close()

    def test_import_rejects_malicious_content(self, mgr: MemoryManager, tmp_path: Path) -> None:
        # Build a payload with a control char (malicious / corrupted) and
        # verify the import rejects without partial writes.
        _add(mgr, "valid note")
        out = tmp_path / "backup.json"
        run_export(mgr, fmt=ExportFormat.JSON, output=out)
        # Tamper: add a record with a control char.
        payload = json.loads(out.read_text())
        payload["memories"].append(
            {
                "id": "malicious",
                "content": "has\x00null byte",
                "tags": ["project:mnemos", "agent:tech-lead", "mnemos:learning"],
            }
        )
        out.write_text(json.dumps(payload))

        result = run_import(mgr, out, mode=ImportMode.MERGE)
        assert result.errors  # rejected
        assert any("malicious" in e for e in result.errors)
        # The malicious record must NOT have been written.
        assert mgr.get("malicious") is None
