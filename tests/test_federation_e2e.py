"""End-to-end federation roundtrip tests (QA#1 coverage blocker).

Exercises the full federation export→import→search pipeline on two
isolated MemoryManager instances backed by separate tmp directories:

    mgrA (home A)
      └─ add memory (decision about 2FA remote sessions)
      └─ list memories via ``MemoryManager.list_recent``
      └─ ``build_compact_payload(memories, source_agent='test-a')``
      └─ json.dumps(payload) → tmp file
                                              │
                                              ▼
    mgrB (home B)
      └─ ``run_sync_import(mgrB, source=tmpfile)``
      └─ assert records_imported >= 1
      └─ ``mgrB.search('2FA remote sessions')`` → assert hits >= 1
      └─ re-import same payload → idempotent (0 imported, >=1 skipped)

Fixtures mirror the pattern in ``tests/test_sync.py`` (``tmp_settings``
+ ``mgr``). Each test builds its own pair of managers so they never
share a SQLite DB or vector store.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesmaro.cli.sync import run_sync_import
from vesmaro.compact import build_compact_payload
from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus


def _make_settings(home: Path) -> Settings:
    """Build an isolated Settings rooted at ``home``."""
    settings = Settings(
        vesma={
            "vault_path": str(home / "vault"),
            "data_dir": str(home / "data"),
            "db_name": "federation-e2e.db",
            "auto_filter": False,
        },
        embedding={"provider": "onnx"},
        federation={
            "shared_projects": ["smoke"],
            "moderation_refuse_threshold": 0.8,
        },
    )
    settings.resolve_paths()
    return settings


def _make_manager(home: Path) -> MemoryManager:
    """Create a MemoryManager backed by ``home`` with a stub embedder."""
    mgr = MemoryManager(_make_settings(home))
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    return mgr


def _add_decision(mgr: MemoryManager) -> str:
    """Add a clean, federation-safe decision memory; return its id."""
    mem = mgr.add(
        MemoryCreate(
            content=(
                "Adopted bearer+TOTP 2FA for all remote sessions. "
                "Token TTL is 15 minutes; refresh via /auth/refresh. "
                "TOTP seed is provisioned through the secrets manager."
            ),
            title="Auth decision",
            tags=["project:smoke", "agent:test-a", "mnemos:decision"],
            source=MemorySource.CLI,
            status=MemoryStatus.PUBLISHED,
        ),
        project="smoke",
        agent="test-a",
    )
    return mem.id


def _write_payload(path: Path, memories: list, *, source_agent: str) -> dict:
    """Build a compact payload and write it as JSON to ``path``."""
    payload = build_compact_payload(memories, source_agent=source_agent)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return payload


@pytest.fixture(autouse=True)
def _isolated_audit_log(monkeypatch, tmp_path: Path) -> Path:
    """Redirect sync audit log writes to tmp_path (mirrors test_sync.py)."""
    audit_path = tmp_path / "audit" / "sync-audit.jsonl"
    import vesmaro.audit as audit_mod

    monkeypatch.setattr(audit_mod, "sync_audit_path", lambda: audit_path)
    return audit_path


class TestFederationE2E:
    """E2E: compact export from A → JSON file → import into B → search B."""

    def test_federation_roundtrip_a_to_b_searchable(self, tmp_path: Path) -> None:
        home_a = tmp_path / "homeA"
        home_b = tmp_path / "homeB"
        home_a.mkdir()
        home_b.mkdir()

        mgr_a = _make_manager(home_a)
        mgr_b = _make_manager(home_b)
        try:
            # 1. Add a clean decision on A.
            _add_decision(mgr_a)

            # 2. List memories from A via MemoryManager.list_recent.
            memories = mgr_a.list_recent(limit=100, project="smoke")
            assert len(memories) >= 1

            # 3. Build the compact payload and dump to a tmp file.
            payload_file = tmp_path / "compact.json"
            payload = _write_payload(payload_file, memories, source_agent="test-a")
            assert payload["schema"] == "vesmaro.federation.v1"
            assert len(payload["records"]) >= 1

            # 4. Import into B.
            result = run_sync_import(mgr_b, source=payload_file)
            assert result.errors == []
            assert result.records_imported >= 1
            assert result.records_skipped == 0

            # 5. B can find the imported memory by content search.
            hits = mgr_b.search("2FA remote sessions", limit=5, project="smoke")
            assert len(hits) >= 1
            assert any("2FA" in h.memory.content for h in hits)
        finally:
            mgr_a.close()
            mgr_b.close()

    def test_federation_roundtrip_idempotent_reimport(self, tmp_path: Path) -> None:
        home_a = tmp_path / "homeA"
        home_b = tmp_path / "homeB"
        home_a.mkdir()
        home_b.mkdir()

        mgr_a = _make_manager(home_a)
        mgr_b = _make_manager(home_b)
        try:
            _add_decision(mgr_a)
            memories = mgr_a.list_recent(limit=100, project="smoke")
            assert len(memories) >= 1

            payload_file = tmp_path / "compact.json"
            _write_payload(payload_file, memories, source_agent="test-a")

            # First import — record lands.
            first = run_sync_import(mgr_b, source=payload_file)
            assert first.errors == []
            assert first.records_imported >= 1

            # Re-import the same payload — idempotent: nothing new imported.
            second = run_sync_import(mgr_b, source=payload_file)
            assert second.errors == []
            assert second.records_imported == 0
            assert second.records_skipped >= 1
        finally:
            mgr_a.close()
            mgr_b.close()
