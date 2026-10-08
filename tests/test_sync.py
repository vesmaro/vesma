"""Tests for federation Phase 0 batch sync CLI (#85 part 2b).

Covers :mod:`vesma.cli.sync` (``mnemos sync export/import``) and
:mod:`vesma.audit` (sync audit log). Reuses:

* :func:`vesma.compact.build_compact_payload` (#85 Part 2a) — the
  compact format builder. Moderation is invoked inside it.
* :func:`vesma.cli.import_.validate_import_record` (#86) — per-record
  import validation, adapted for the compact record shape.
* :func:`vesma.cli.export._encrypt` / :func:`decrypt` (#84) — AES-256-GCM
  passphrase encryption helpers.

All secret/PII fixtures use RFC-reserved values (per
``sensitive-data.instructions.md``): 192.0.2.0/24 (RFC 5737),
user@example.com (RFC 5322), example.invalid (RFC 6761). The
``AKIA…T*16`` AWS key is obviously fake (AKIA prefix + repeated char).
No real credentials appear anywhere in this file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesma.cli.export import _encrypt, decrypt, is_encrypted
from vesma.cli.sync import (
    run_sync_export,
    run_sync_import,
)
from vesma.compact import COMPACT_SCHEMA, CompactRecord, build_compact_payload
from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import (
    NO_FEDERATE_TAG,
    PIPELINE_RETRY_METADATA_KEYS,
    Memory,
    MemoryCreate,
    MemorySource,
    MemoryStatus,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

#: Obviously-fake AWS key (AKIA + 16 uppercase). Never a real credential.
FAKE_AWS_KEY = "AKIA" + "T" * 16

#: Default passphrase used for encrypted round-trip tests. Not a secret.
_TEST_PASSPHRASE = "test-passphrase-not-a-secret"


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp_path / "vault"),
            "data_dir": str(tmp_path / "data"),
            "db_name": "test-sync.db",
            "auto_filter": False,
        },
        embedding={"provider": "onnx"},
        federation={
            "shared_projects": ["mnemos"],
            "moderation_refuse_threshold": 0.8,
        },
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
def _isolated_audit_log(monkeypatch, tmp_path: Path) -> Path:
    """Redirect the audit log to a tmp path so tests do not pollute the real one.

    Patches ``sync_audit_path`` (read by :func:`log_sync_audit` on every
    call) to return a path under the test's ``tmp_path``. Because
    :func:`log_sync_audit` calls :func:`sync_audit_path` at call time
    (not import time), this redirects every audit write — both the ones
    from :mod:`vesma.cli.sync` and the ones from direct
    :func:`log_sync_audit` calls in :class:`TestAuditModule`.
    """
    audit_path = tmp_path / "audit" / "sync-audit.jsonl"
    import vesma.audit as audit_mod

    monkeypatch.setattr(audit_mod, "sync_audit_path", lambda: audit_path)
    return audit_path


def _add_memory(
    mgr: MemoryManager,
    content: str,
    *,
    project: str = "mnemos",
    agent: str = "tech-lead",
    status: MemoryStatus = MemoryStatus.PUBLISHED,
    tags: list[str] | None = None,
) -> str:
    tags = tags or [f"project:{project}", f"agent:{agent}", "mnemos:decision"]
    mem = mgr.add(
        MemoryCreate(content=content, tags=tags, source=MemorySource.CLI, status=status),
        project=project,
        agent=agent,
    )
    return mem.id


def _read_audit_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── Export ────────────────────────────────────────────────────────────────────


class TestSyncExport:
    def test_sync_export_roundtrip(self, mgr: MemoryManager, tmp_path: Path) -> None:
        """Clean memory → exported in compact format, moderation applied (allow)."""
        _add_memory(mgr, "Clean decision: adopt Pydantic v2 for all models.")
        out = tmp_path / "sync.json"
        result = run_sync_export(mgr, output=out, shared_projects_arg="mnemos")
        assert result.errors == []
        assert result.records_exported == 1
        assert result.records_refused == 0
        assert result.encrypted is False
        assert out.exists()
        payload = json.loads(out.read_text())
        assert payload["schema"] == COMPACT_SCHEMA
        assert len(payload["records"]) == 1
        rec = payload["records"][0]
        assert rec["id"].startswith("fed:")
        assert rec["summary"]
        assert rec["type"] in {"decision", "session"}

    def test_sync_export_excludes_no_federate(self, mgr: MemoryManager, tmp_path: Path) -> None:
        """Memory tagged mnemos:no-federate → excluded from export."""
        _add_memory(
            mgr,
            "internal-only note",
            tags=[
                "project:mnemos",
                "agent:tech-lead",
                "mnemos:decision",
                NO_FEDERATE_TAG,
            ],
        )
        _add_memory(mgr, "public decision")
        out = tmp_path / "sync.json"
        result = run_sync_export(mgr, output=out, shared_projects_arg="mnemos")
        assert result.records_exported == 1
        payload = json.loads(out.read_text())
        assert len(payload["records"]) == 1
        assert "internal-only" not in json.dumps(payload)

    def test_sync_export_excludes_non_shared_project(
        self, mgr: MemoryManager, tmp_path: Path
    ) -> None:
        """Memory whose project is not in shared_projects → excluded."""
        _add_memory(mgr, "shared project memory", project="mnemos")
        _add_memory(mgr, "other project memory", project="other-project")
        out = tmp_path / "sync.json"
        result = run_sync_export(mgr, output=out, shared_projects_arg="mnemos")
        assert result.records_exported == 1
        payload = json.loads(out.read_text())
        assert len(payload["records"]) == 1
        assert "other project memory" not in json.dumps(payload)

    def test_sync_export_refuses_all_secret_content(
        self, mgr: MemoryManager, tmp_path: Path
    ) -> None:
        """Memory whose content is mostly a secret → moderation refuses, counted.

        We bypass ``mgr.add`` (which would auto-tag the record
        ``mnemos:no-federate`` via Layer 1 and exclude it before
        moderation) and save directly to SQLite, so this test exercises
        the *moderation refuse path* — the case where Layer 1 missed the
        secret (e.g. a pattern not in the detector) and Layer 3 catches
        it on export. The refused record is excluded from the payload
        and counted in ``records_refused``.
        """
        from datetime import UTC, datetime

        # 20-char AWS key with tiny prefix → redacted fraction > 0.8 → refuse.
        mem = Memory(
            id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            content=f"x {FAKE_AWS_KEY}",
            tags=["project:mnemos", "agent:tech-lead", "mnemos:decision"],
            source=MemorySource.CLI,
            status=MemoryStatus.PUBLISHED,
            project="mnemos",
            agent="tech-lead",
            created_at=datetime(2026, 7, 19, 10, 0, 0, tzinfo=UTC),
        )
        mgr.sqlite.save(mem)

        out = tmp_path / "sync.json"
        result = run_sync_export(mgr, output=out, shared_projects_arg="mnemos")
        assert result.records_exported == 0
        assert result.records_refused == 1
        payload = json.loads(out.read_text())
        assert payload["records"] == []
        assert FAKE_AWS_KEY not in out.read_text()

    def test_sync_export_dry_run(self, mgr: MemoryManager, tmp_path: Path) -> None:
        """--dry-run builds payload, prints summary, does NOT write file."""
        _add_memory(mgr, "clean memory for dry-run")
        out = tmp_path / "sync.json"
        result = run_sync_export(mgr, output=out, shared_projects_arg="mnemos", dry_run=True)
        assert result.dry_run is True
        assert result.records_exported == 1
        assert not out.exists()

    def test_sync_export_encrypt(self, mgr: MemoryManager, tmp_path: Path, monkeypatch) -> None:
        """--encrypt with VESMA_EXPORT_PASSPHRASE → encrypted file written."""
        _add_memory(mgr, "encryptable clean memory")
        monkeypatch.setenv("VESMA_EXPORT_PASSPHRASE", _TEST_PASSPHRASE)
        out = tmp_path / "sync.enc"
        result = run_sync_export(mgr, output=out, shared_projects_arg="mnemos", encrypt=True)
        assert result.encrypted is True
        assert result.errors == []
        assert out.exists()
        raw = out.read_bytes()
        assert is_encrypted(raw)
        # Round-trip: decrypt and parse.
        payload = json.loads(decrypt(raw, _TEST_PASSPHRASE).decode("utf-8"))
        assert payload["schema"] == COMPACT_SCHEMA
        assert len(payload["records"]) == 1

    def test_sync_export_encrypt_missing_passphrase(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch
    ) -> None:
        """--encrypt without VESMA_EXPORT_PASSPHRASE → error, no file written."""
        _add_memory(mgr, "clean memory")
        monkeypatch.delenv("VESMA_EXPORT_PASSPHRASE", raising=False)
        out = tmp_path / "sync.enc"
        result = run_sync_export(mgr, output=out, shared_projects_arg="mnemos", encrypt=True)
        assert result.encrypted is False
        assert result.errors
        assert not out.exists()

    def test_sync_export_no_shared_projects_error(self, mgr: MemoryManager, tmp_path: Path) -> None:
        """No shared_projects (CLI or config) → ValueError."""
        # Override the fixture's config to an empty list.
        mgr.settings.federation.shared_projects = []
        out = tmp_path / "sync.json"
        with pytest.raises(ValueError, match="no shared_projects configured"):
            run_sync_export(mgr, output=out, shared_projects_arg=None)

    def test_sync_export_cli_overrides_config(self, mgr: MemoryManager, tmp_path: Path) -> None:
        """--shared-projects CLI arg takes precedence over config."""
        _add_memory(mgr, "config project memory", project="mnemos")
        _add_memory(mgr, "cli project memory", project="cli-project")
        out = tmp_path / "sync.json"
        result = run_sync_export(mgr, output=out, shared_projects_arg="cli-project")
        assert result.records_exported == 1
        assert result.shared_projects == ["cli-project"]
        payload = json.loads(out.read_text())
        assert "cli project memory" in json.dumps(payload)
        assert "config project memory" not in json.dumps(payload)


# ── Import ────────────────────────────────────────────────────────────────────


def _write_compact_payload(
    path: Path, memories: list[Memory], *, source_agent: str = "tech-lead"
) -> None:
    """Build and write a compact payload from in-memory Memory objects."""
    payload = build_compact_payload(memories, source_agent=source_agent)
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")


class TestSyncImport:
    def test_sync_import_merge_idempotent(self, mgr: MemoryManager, tmp_path: Path) -> None:
        """Import compact file → merge by id; re-import → no duplicates."""
        src = tmp_path / "sync.json"
        mem = Memory(
            id="11111111-1111-1111-1111-111111111111",
            content="imported decision",
            tags=["project:mnemos", "agent:tech-lead", "mnemos:decision"],
            source=MemorySource.CLI,
            status=MemoryStatus.PUBLISHED,
            created_at=datetime(2026, 7, 19, 10, 0, 0, tzinfo=UTC),
        )
        _write_compact_payload(src, [mem])

        result1 = run_sync_import(mgr, source=src)
        assert result1.errors == []
        assert result1.records_imported == 1
        assert result1.records_skipped == 0

        # Re-import → the record now exists locally → skipped.
        result2 = run_sync_import(mgr, source=src)
        assert result2.errors == []
        assert result2.records_imported == 0
        assert result2.records_skipped == 1

    def test_sync_import_peer_metadata_cannot_mint_retry_keys(
        self, mgr: MemoryManager, tmp_path: Path
    ) -> None:
        """vesma #432 (mint leg, sync leg): the compact contract carries NO
        metadata — ``CompactRecord`` has no such field, pydantic drops
        extraneous keys at validation, and the mapper mints a fresh
        ``federated_origin``-only dict. Pinned here so a future mapper
        change that starts copying peer metadata cannot silently reopen
        the mint hole: a peer-supplied ``pipeline_retry_count`` on a
        fresh row must never land as if the local server had minted it
        (CWE-346 — server-minted per store, like the manager and JSON-
        import strips)."""
        src = tmp_path / "sync-forged-retry.json"
        mem = Memory(
            id="22222222-2222-2222-2222-222222222222",
            content="peer row with a forged retry counter",
            tags=["project:mnemos", "agent:tech-lead", "mnemos:decision"],
            source=MemorySource.CLI,
            status=MemoryStatus.PUBLISHED,
            created_at=datetime(2026, 7, 19, 10, 0, 0, tzinfo=UTC),
        )
        payload = build_compact_payload([mem], source_agent="tech-lead")
        # The mint attempt: a hostile (or naive) peer ships server-minted
        # keys alongside the contract fields.
        payload["records"][0]["metadata"] = {
            "pipeline_retry_count": 99,
            "pipeline_retry_at": "2099-01-01T00:00:00+00:00",
        }
        src.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")

        result = run_sync_import(mgr, source=src)
        assert result.errors == []
        assert result.records_imported == 1

        stored = mgr.sqlite.get("fed:tech-lead:22222222-2222-2222-2222-222222222222")
        assert stored is not None
        assert not PIPELINE_RETRY_METADATA_KEYS & set(stored.metadata), (
            "a peer-supplied retry counter must not survive sync-accept"
        )
        assert stored.metadata == {"federated_origin": "tech-lead"}, (
            "the mapper mints exactly the federated_origin stamp — nothing else lands"
        )

    def test_sync_import_skip_existing(self, mgr: MemoryManager, tmp_path: Path) -> None:
        """Record whose id already exists locally → skipped, not overwritten."""
        # Pre-create the memory with the FEDERATED id (fed:<agent>:<uuid>)
        # so the import's idempotency check (mgr.sqlite.get(record.id)) finds it.
        fed_id = "fed:tech-lead:22222222-2222-2222-2222-222222222222"
        mem = Memory(
            id=fed_id,
            content="local original content",
            tags=["project:mnemos", "agent:tech-lead", "mnemos:decision"],
            source=MemorySource.CLI,
            status=MemoryStatus.PUBLISHED,
            project="mnemos",
            agent="tech-lead",
            created_at=datetime(2026, 7, 19, 10, 0, 0, tzinfo=UTC),
        )
        mgr.sqlite.save(mem)

        # Build a compact payload with the SAME id but different content.
        src = tmp_path / "sync.json"
        remote_mem = Memory(
            id="22222222-2222-2222-2222-222222222222",
            content="remote changed content",
            tags=["project:mnemos", "agent:tech-lead", "mnemos:decision"],
            source=MemorySource.CLI,
            status=MemoryStatus.PUBLISHED,
            created_at=datetime(2026, 7, 19, 10, 0, 0, tzinfo=UTC),
        )
        _write_compact_payload(src, [remote_mem], source_agent="tech-lead")

        result = run_sync_import(mgr, source=src)
        assert result.records_imported == 0
        assert result.records_skipped == 1
        # Local content preserved (not overwritten).
        local = mgr.sqlite.get(fed_id)
        assert local is not None
        assert "local original content" in local.content

    def test_sync_import_dry_run(self, mgr: MemoryManager, tmp_path: Path) -> None:
        """--dry-run validates and reports; no writes."""
        src = tmp_path / "sync.json"
        mem = Memory(
            id="33333333-3333-3333-3333-333333333333",
            content="dry-run decision",
            tags=["project:mnemos", "agent:tech-lead", "mnemos:decision"],
            source=MemorySource.CLI,
            status=MemoryStatus.PUBLISHED,
            created_at=datetime(2026, 7, 19, 10, 0, 0, tzinfo=UTC),
        )
        _write_compact_payload(src, [mem])

        result = run_sync_import(mgr, source=src, dry_run=True)
        assert result.dry_run is True
        assert result.errors == []
        assert result.records_imported == 1  # validated count
        # Nothing was actually written.
        assert mgr.sqlite.get("fed:tech-lead:33333333-3333-3333-3333-333333333333") is None

    def test_sync_import_encrypted(self, mgr: MemoryManager, tmp_path: Path, monkeypatch) -> None:
        """Import an encrypted compact file with --passphrase-env → decrypted + imported."""
        mem = Memory(
            id="44444444-4444-4444-4444-444444444444",
            content="encrypted decision",
            tags=["project:mnemos", "agent:tech-lead", "mnemos:decision"],
            source=MemorySource.CLI,
            status=MemoryStatus.PUBLISHED,
            created_at=datetime(2026, 7, 19, 10, 0, 0, tzinfo=UTC),
        )
        payload = build_compact_payload([mem], source_agent="tech-lead")
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        encrypted = _encrypt(raw, _TEST_PASSPHRASE)
        src = tmp_path / "sync.enc"
        src.write_bytes(encrypted)

        # Use a custom env var name (not the default).
        monkeypatch.setenv("MY_SYNC_PASS", _TEST_PASSPHRASE)
        result = run_sync_import(mgr, source=src, passphrase_env="MY_SYNC_PASS")
        assert result.errors == []
        assert result.records_imported == 1
        assert mgr.sqlite.get("fed:tech-lead:44444444-4444-4444-4444-444444444444") is not None

    def test_sync_import_encrypted_default_env(
        self, mgr: MemoryManager, tmp_path: Path, monkeypatch
    ) -> None:
        """Encrypted import without --passphrase-env falls back to VESMA_EXPORT_PASSPHRASE."""
        mem = Memory(
            id="55555555-5555-5555-5555-555555555555",
            content="default-env decision",
            tags=["project:mnemos", "agent:tech-lead", "mnemos:decision"],
            source=MemorySource.CLI,
            status=MemoryStatus.PUBLISHED,
            created_at=datetime(2026, 7, 19, 10, 0, 0, tzinfo=UTC),
        )
        payload = build_compact_payload([mem], source_agent="tech-lead")
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        src = tmp_path / "sync.enc"
        src.write_bytes(_encrypt(raw, _TEST_PASSPHRASE))

        monkeypatch.setenv("VESMA_EXPORT_PASSPHRASE", _TEST_PASSPHRASE)
        result = run_sync_import(mgr, source=src)
        assert result.errors == []
        assert result.records_imported == 1

    def test_sync_import_validation_rejects_malicious(
        self, mgr: MemoryManager, tmp_path: Path
    ) -> None:
        """Schema drift / oversized / prompt-injection → rejected (no partial writes)."""
        # Schema drift: wrong schema string.
        bad = tmp_path / "bad.json"
        bad.write_text(
            json.dumps({"schema": "vesma.federation.evil", "records": [], "stats": {}}),
            encoding="utf-8",
        )
        result = run_sync_import(mgr, source=bad)
        assert result.errors
        assert any("schema" in e for e in result.errors)

        # Oversized content: summary > 1 MiB.
        big = tmp_path / "big.json"
        oversized = CompactRecord(
            id="fed:tech-lead:66666666-6666-6666-6666-666666666666",
            type="decision",
            title="big",
            summary="x" * (1_048_577),
            key_points=[],
            tags=["project:mnemos", "agent:tech-lead", "mnemos:decision"],
            source_agent="tech-lead",
            timestamp="2026-07-19T10:00:00Z",
        )
        payload = {
            "schema": COMPACT_SCHEMA,
            "records": [oversized.model_dump()],
            "stats": {
                "total": 1,
                "exported": 1,
                "refused": 0,
                "secrets_redacted": 0,
                "pii_anonymized": 0,
            },
        }
        big.write_text(json.dumps(payload), encoding="utf-8")
        result_big = run_sync_import(mgr, source=big)
        assert result_big.errors
        assert any("max length" in e or "exceeds" in e for e in result_big.errors)
        # Nothing was written (oversized record rejected, no partial writes).
        assert mgr.sqlite.get("fed:tech-lead:66666666-6666-6666-6666-666666666666") is None

    def test_sync_import_rejects_prompt_injection_warning(
        self, mgr: MemoryManager, tmp_path: Path
    ) -> None:
        """Prompt-injection content → warning (not error); record is still imported."""
        src = tmp_path / "sync.json"
        mem = Memory(
            id="77777777-7777-7777-7777-777777777777",
            content="Research note: the pattern [INST] appears in this discussion of injection.",
            tags=["project:mnemos", "agent:tech-lead", "mnemos:learning"],
            source=MemorySource.CLI,
            status=MemoryStatus.PUBLISHED,
            created_at=datetime(2026, 7, 19, 10, 0, 0, tzinfo=UTC),
        )
        _write_compact_payload(src, [mem])
        result = run_sync_import(mgr, source=src)
        # Prompt-injection is a warning, not an error — import succeeds.
        assert result.records_imported == 1
        assert any("prompt-injection" in w.lower() or "inst" in w.lower() for w in result.warnings)

    def test_sync_import_missing_file(self, mgr: MemoryManager, tmp_path: Path) -> None:
        """Non-existent source → error, no crash."""
        result = run_sync_import(mgr, source=tmp_path / "nope.json")
        assert result.errors
        assert any("not found" in e for e in result.errors)

    def test_sync_import_invalid_json(self, mgr: MemoryManager, tmp_path: Path) -> None:
        """Garbage file → error."""
        src = tmp_path / "garbage.json"
        src.write_text("{not valid json", encoding="utf-8")
        result = run_sync_import(mgr, source=src)
        assert result.errors
        assert any("JSON" in e or "json" in e.lower() for e in result.errors)


# ── Audit log ─────────────────────────────────────────────────────────────────


class TestSyncAuditLog:
    def test_audit_log_export(
        self, mgr: MemoryManager, tmp_path: Path, _isolated_audit_log: Path
    ) -> None:
        """After export, sync-audit.jsonl has an entry with correct counts."""
        _add_memory(mgr, "audited clean memory")
        out = tmp_path / "sync.json"
        result = run_sync_export(mgr, output=out, shared_projects_arg="mnemos")
        assert result.errors == []

        entries = _read_audit_entries(_isolated_audit_log)
        # The autouse fixture patches log_sync_audit; one entry should be present.
        export_entries = [e for e in entries if e.get("action") == "sync-export"]
        assert export_entries, "expected at least one sync-export audit entry"
        entry = export_entries[-1]
        assert entry["records_exported"] == 1
        assert "encrypted" in entry
        assert "shared_projects" in entry
        assert entry["shared_projects"] == ["mnemos"]

    def test_audit_log_import(
        self, mgr: MemoryManager, tmp_path: Path, _isolated_audit_log: Path
    ) -> None:
        """After import, audit log has a sync-import entry."""
        src = tmp_path / "sync.json"
        mem = Memory(
            id="88888888-8888-8888-8888-888888888888",
            content="audit-tracked decision",
            tags=["project:mnemos", "agent:tech-lead", "mnemos:decision"],
            source=MemorySource.CLI,
            status=MemoryStatus.PUBLISHED,
            created_at=datetime(2026, 7, 19, 10, 0, 0, tzinfo=UTC),
        )
        _write_compact_payload(src, [mem])

        run_sync_import(mgr, source=src)
        entries = _read_audit_entries(_isolated_audit_log)
        import_entries = [e for e in entries if e.get("action") == "sync-import"]
        assert import_entries
        entry = import_entries[-1]
        assert entry["records_imported"] == 1
        assert "errors" in entry
        assert "warnings" in entry

    def test_audit_log_no_raw_values(
        self, mgr: MemoryManager, tmp_path: Path, _isolated_audit_log: Path
    ) -> None:
        """Audit log entries contain NO raw content / secrets / PII — counters only."""
        # Add a memory with content that would be redacted.
        _add_memory(mgr, f"secret prefix {FAKE_AWS_KEY} trailing")
        out = tmp_path / "sync.json"
        run_sync_export(mgr, output=out, shared_projects_arg="mnemos")

        blob = _isolated_audit_log.read_text() if _isolated_audit_log.exists() else ""
        # No raw secret.
        assert FAKE_AWS_KEY not in blob
        # No content-like strings (the audit entry keys are counters/paths only).
        entries = _read_audit_entries(_isolated_audit_log)
        for entry in entries:
            # Allowed keys: counters, paths, flags, project lists.
            serialized = json.dumps(entry)
            assert "secret prefix" not in serialized
            assert "trailing" not in serialized


# ── Audit module unit tests ───────────────────────────────────────────────────


class TestAuditModule:
    def test_sync_audit_path_constant(self) -> None:
        """The audit log filename is the documented relative path."""
        from vesma.audit import SYNC_AUDIT_FILENAME

        assert SYNC_AUDIT_FILENAME == ".vesma/logs/sync-audit.jsonl"

    def test_log_sync_audit_appends_jsonl(self, tmp_path: Path, monkeypatch) -> None:
        """log_sync_audit writes one JSON object per line, adds timestamp."""
        import vesma.audit as audit_mod

        # Use a private log path distinct from the autouse fixture's path.
        log_path = tmp_path / "audit-module.jsonl"
        monkeypatch.setattr(audit_mod, "sync_audit_path", lambda: log_path)

        audit_mod.log_sync_audit({"action": "sync-export", "records_exported": 5})
        audit_mod.log_sync_audit({"action": "sync-import", "records_imported": 3})

        lines = log_path.read_text().splitlines()
        assert len(lines) == 2
        e1 = json.loads(lines[0])
        e2 = json.loads(lines[1])
        assert e1["action"] == "sync-export"
        assert e1["records_exported"] == 5
        assert "timestamp" in e1
        assert e2["action"] == "sync-import"
        assert e2["records_imported"] == 3
