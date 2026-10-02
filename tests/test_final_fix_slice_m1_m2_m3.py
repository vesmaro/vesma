"""Final pre-report fix slice — M1 + m2 + m3 acceptance tests.

M1 (MAJOR) — ``vesma_filter`` / ``POST /filter/{id}`` unscanned echo:
the channels now route through ``MemoryManager.issue_context_filter``
(status gate + optional caller-project scope + scan-at-issuance on the
echoed ``clean_content``; refuse mode drops the content). The maintenance
primitive ``apply_context_filter`` itself stays ungated (auto-filter on
ingest, ``filter_all``, CLI) — pinned here by a raw-memory regression
test.

m2 — ``vesma_ingest_url`` title echo: ``auto_title()`` derives from the
fetched page content, so the echoed title is scanned at issuance in BOTH
the MCP dispatch and the Hermes shim; refuse mode drops it.

m3 — rewrite dedupe lookup: ``rewrite_event_key`` is denormalised into an
indexed column (derived in ``save()`` ONLY under
``trusted_rewrite_provenance``, backfilled once from trusted-path rows),
and ``get_memory_id_by_rewrite_event_key`` reads the column — the exact
pattern C10 eliminated for the quota count. Equivalence against the old
``json_extract`` formula is locked by test.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vesmaro.api import main as api_main
from vesmaro.api.main import app, lifespan
from vesmaro.config import Settings
from vesmaro.context_rewrite import context_rewrite
from vesmaro.manager import MemoryManager
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus
from vesmaro.storage.sqlite_store import SQLiteStore

# aws-key pattern: AKIA + 16 chars of [0-9A-Z].
FAKE_AWS_KEY = "AKIAEXAMPLEABCDEFGH1"

_VALID_TAGS = ["project:test", "agent:fix-slice", "mnemos:learning"]
PROJECT = "fix-slice-project"
AGENT = "fix-slice-agent"


def _settings(
    tmp: Path,
    *,
    retrieve_refuse_on_secret: bool = False,
) -> Settings:
    settings = Settings(
        mnemos={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        scanner={"enabled": False},
        ccr={"retrieve_refuse_on_secret": retrieve_refuse_on_secret},
    )
    settings.resolve_paths()
    return settings


def _manager(settings: Settings) -> MemoryManager:
    mgr = MemoryManager(settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    return mgr


def _add(
    mgr: MemoryManager,
    content: str,
    *,
    project: str = PROJECT,
    status: MemoryStatus | None = None,
    metadata: dict[str, object] | None = None,
):
    """Status is EXPLICIT at create time (ADR-0019 B2b): a status-less
    add follows the visibility policy (immediate ⇒ published), while
    these tests pin per-status issuance-gate behavior."""
    return mgr.add(
        MemoryCreate(
            content=content,
            tags=[f"project:{project}", f"agent:{AGENT}", "mnemos:learning"],
            source=MemorySource.MCP,
            metadata=metadata or {},
            **({"status": status} if status is not None else {}),
        ),
        project=project,
        agent=AGENT,
    )


def _legacy_published(mgr: MemoryManager, content: str) -> str:
    """Seed a PUBLISHED row whose content carries a secret.

    Since Phase A no LEGAL path can produce this state (the N1 gate
    refuses dirty publications, zero-loss RAW) — this fixture mimics a
    pre-Phase-A legacy row via the store-level write, the same
    convention as the B1 ``no-op flip`` legacy-row fixture. The point
    under test is the scan-at-issuance behaviour on such a row, not the
    gate that would have prevented it.
    """
    memory = _add(mgr, content)
    assert mgr.sqlite.update_fields(memory.id, status=MemoryStatus.PUBLISHED)
    return memory.id


# ── M1: issuance gate on vesma_filter / POST /filter ────────────────────────


class TestFilterIssuanceGateManager:
    def test_raw_memory_not_filterable_into_context(self, tmp_path: Path) -> None:
        mgr = _manager(_settings(tmp_path))
        memory = _add(mgr, "plain raw content", status=MemoryStatus.RAW)
        assert memory.status == MemoryStatus.RAW
        result = mgr.issue_context_filter(memory.id)
        assert result["status"] == "error"
        assert result["reason"] == "status_gate"
        assert "clean_content" not in result
        mgr.close()

    def test_archived_memory_not_filterable_into_context(self, tmp_path: Path) -> None:
        mgr = _manager(_settings(tmp_path))
        memory = _add(mgr, "archived content", status=MemoryStatus.ARCHIVED)
        result = mgr.issue_context_filter(memory.id)
        assert result["status"] == "error"
        assert result["reason"] == "status_gate"
        mgr.close()

    def test_processing_memory_not_filterable_into_context(self, tmp_path: Path) -> None:
        mgr = _manager(_settings(tmp_path))
        memory = _add(mgr, "in-flight content", status=MemoryStatus.PROCESSING)
        result = mgr.issue_context_filter(memory.id)
        assert result["status"] == "error"
        assert result["reason"] == "status_gate"
        mgr.close()

    def test_missing_memory_not_found(self, tmp_path: Path) -> None:
        mgr = _manager(_settings(tmp_path))
        result = mgr.issue_context_filter("no-such-id")
        assert result["status"] == "error"
        assert result["reason"] == "not_found"
        mgr.close()

    def test_published_secret_redacted_in_echo(self, tmp_path: Path) -> None:
        mgr = _manager(_settings(tmp_path))
        memory_id = _legacy_published(mgr, f"deployment notes token {FAKE_AWS_KEY} end")
        result = mgr.issue_context_filter(memory_id)
        assert result["status"] == "ok"
        assert FAKE_AWS_KEY not in result["clean_content"]
        assert "<REDACTED:aws-key>" in result["clean_content"]
        assert result["redactions"] >= 1
        assert result["redacted_patterns"].get("aws-key") == 1
        # Zero-loss storage: the stored raw content keeps the secret; only
        # the echo is redacted (scan-at-issuance semantics).
        stored = mgr.get(memory_id)
        assert stored is not None
        assert FAKE_AWS_KEY in (stored.raw_content or stored.content)
        mgr.close()

    def test_refuse_mode_drops_content(self, tmp_path: Path) -> None:
        mgr = _manager(_settings(tmp_path, retrieve_refuse_on_secret=True))
        memory_id = _legacy_published(mgr, f"token {FAKE_AWS_KEY} inline")
        result = mgr.issue_context_filter(memory_id)
        assert result["status"] == "error"
        assert result["reason"] == "refused"
        assert "clean_content" not in result
        mgr.close()

    def test_project_scope_fail_closed_on_mismatch(self, tmp_path: Path) -> None:
        mgr = _manager(_settings(tmp_path))
        memory = _add(mgr, "scoped content", status=MemoryStatus.PUBLISHED)
        result = mgr.issue_context_filter(memory.id, project="other-project")
        assert result["status"] == "error"
        assert result["reason"] == "project_scope"
        # The wording does not leak whether the memory exists globally.
        assert "other-project" in result["error"]
        # Matching scope succeeds; absent scope is explicit operator
        # semantics (mirrors GET /memories/{id}).
        ok = mgr.issue_context_filter(memory.id, project=PROJECT)
        assert ok["status"] == "ok"
        unscoped = mgr.issue_context_filter(memory.id)
        assert unscoped["status"] == "ok"
        mgr.close()

    def test_scanner_error_fails_closed(self, tmp_path: Path) -> None:
        mgr = _manager(_settings(tmp_path))
        memory = _add(mgr, "benign content", status=MemoryStatus.PUBLISHED)
        with patch(
            "vesmaro.secrets_detector.detect_secrets",
            side_effect=RuntimeError("scanner boom"),
        ):
            result = mgr.issue_context_filter(memory.id)
        assert result["status"] == "error"
        assert result["reason"] == "refused"
        assert "scanner error" in result["error"]
        assert "clean_content" not in result
        mgr.close()

    def test_maintenance_primitive_still_filters_raw(self, tmp_path: Path) -> None:
        """Regression pin: the ungated primitive keeps serving auto-filter
        / filter_all (raw memories stay re-filterable as maintenance)."""
        mgr = _manager(_settings(tmp_path))
        memory = _add(mgr, "2024-01-15 [ERROR] maintenance material")
        result = mgr.apply_context_filter(memory.id, profile="log")
        assert result["status"] == "ok"
        assert "clean_content" in result
        mgr.close()


class TestFilterIssuanceGateMcp:
    @pytest.mark.asyncio
    async def test_raw_memory_refused(self, tmp_path: Path) -> None:
        from vesmaro.mcp_server import _dispatch

        mgr = _manager(_settings(tmp_path))
        memory = _add(mgr, "raw mcp memory", status=MemoryStatus.RAW)
        with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
            result = await _dispatch("vesma_filter", {"memory_id": memory.id})
        assert result["status"] == "error"
        assert result["reason"] == "status_gate"
        assert "clean_content" not in result
        mgr.close()

    @pytest.mark.asyncio
    async def test_cross_project_fail_closed(self, tmp_path: Path) -> None:
        from vesmaro.mcp_server import _dispatch

        mgr = _manager(_settings(tmp_path))
        memory = _add(mgr, "scoped", status=MemoryStatus.PUBLISHED)
        with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
            result = await _dispatch(
                "vesma_filter",
                {"memory_id": memory.id, "project": "somebody-elses"},
            )
        assert result["status"] == "error"
        assert result["reason"] == "project_scope"
        with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
            ok = await _dispatch(
                "vesma_filter",
                {"memory_id": memory.id, "project": PROJECT},
            )
        assert ok.get("status") != "error"
        assert ok["clean_content"]
        mgr.close()

    @pytest.mark.asyncio
    async def test_secret_redacted_in_tool_response(self, tmp_path: Path) -> None:
        from vesmaro.mcp_server import _dispatch

        mgr = _manager(_settings(tmp_path))
        memory_id = _legacy_published(mgr, f"creds {FAKE_AWS_KEY} leaked")
        with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
            result = await _dispatch("vesma_filter", {"memory_id": memory_id})
        assert result["memory_id"] == memory_id
        assert FAKE_AWS_KEY not in result["clean_content"]
        assert "<REDACTED:aws-key>" in result["clean_content"]
        assert result["redactions"] >= 1
        mgr.close()


class TestFilterIssuanceGateRest:
    @pytest.fixture
    def client(self, tmp_path: Path):
        mgr = _manager(_settings(tmp_path))
        test_app = FastAPI(title="Mnemos-Test", version="0.1.0", lifespan=lifespan)
        for route in app.routes:
            test_app.routes.append(route)
        api_main._manager = mgr
        with TestClient(test_app) as tc:
            yield tc
        mgr.close()
        api_main._manager = None

    @staticmethod
    def _create(client: TestClient, content: str, *, status: str = "raw") -> str:
        resp = client.post(
            "/memories",
            json={
                "content": content,
                "tags": [f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:learning"],
                "source": "mcp",
                "status": status,
            },
        )
        assert resp.status_code == 201
        return resp.json()["id"]

    def test_raw_memory_refused_422(self, client: TestClient) -> None:
        mem_id = self._create(client, "raw rest memory")
        resp = client.post(f"/filter/{mem_id}", json={})
        assert resp.status_code == 422
        assert "filterable into context" in resp.json()["detail"]

    def test_cross_project_refused_403(self, client: TestClient) -> None:
        mem_id = self._create(client, "scoped rest memory")
        client.post(f"/publish/{mem_id}?skip_quality_check=true")
        resp = client.post(f"/filter/{mem_id}", json={"project": "not-my-project"})
        assert resp.status_code == 403

    def test_secret_refused_403_in_refuse_mode(self, tmp_path: Path, client: TestClient) -> None:
        # client fixture already bound to a non-refuse manager; swap the
        # manager for a refuse-mode one on the same routes.
        mgr = _manager(_settings(tmp_path, retrieve_refuse_on_secret=True))
        old = api_main._manager
        api_main._manager = mgr
        try:
            # ADR-0019 Phase A + N1: a secret-carrying row can no longer
            # reach published via the publish endpoint OR a direct-seed
            # create (both gate it), so the published row is planted with
            # a store-level status flip — the pre-gate/legacy state. The
            # filter issuance gate under test stays exactly the same.
            mem_id = self._create(client, f"secret {FAKE_AWS_KEY} rest")
            mgr.sqlite.update_status(mem_id, MemoryStatus.PUBLISHED)
            resp = client.post(f"/filter/{mem_id}", json={})
            assert resp.status_code == 403
            assert "issuance refused" in resp.json()["detail"]
        finally:
            api_main._manager = old
            mgr.close()

    def test_published_filter_ok_with_redactions(self, client: TestClient) -> None:
        # Published row with a planted fake secret — neither the publish
        # endpoint nor a direct-seed create can mint such a row anymore
        # (ADR-0019 Phase A danger gate + N1 direct-seed gate), so the
        # row is planted with a store-level status flip (the legacy
        # pre-gate state); the redaction assertions are unchanged.
        mem_id = self._create(client, f"key {FAKE_AWS_KEY} rest")
        api_main._manager.sqlite.update_status(mem_id, MemoryStatus.PUBLISHED)
        resp = client.post(f"/filter/{mem_id}", json={"project": PROJECT})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert FAKE_AWS_KEY not in data["clean_content"]
        assert data["redactions"] >= 1


# ── m2: ingest_url title echo scanned ────────────────────────────────────────


def _fake_url_memory(mgr: MemoryManager, first_line: str):
    """A Memory-shaped object whose auto_title() is the given first line."""
    return _add(mgr, first_line)


class TestIngestUrlTitleScanMcp:
    @pytest.mark.asyncio
    async def test_secret_in_fetched_title_redacted(self, tmp_path: Path) -> None:
        from vesmaro.mcp_server import _dispatch

        mgr = _manager(_settings(tmp_path))
        fake = _fake_url_memory(mgr, f"page title {FAKE_AWS_KEY} end")
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mgr),
            patch.object(mgr, "ingest_url", return_value=fake) as mock_ing,
        ):
            result = await _dispatch(
                "vesma_ingest_url",
                {
                    "url": "https://example.com/page",
                    "tags": [f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:learning"],
                },
            )
        assert mock_ing.called
        assert FAKE_AWS_KEY not in result["title"]
        assert "<REDACTED:aws-key>" in result["title"]
        assert result["id"] == fake.id
        mgr.close()

    @pytest.mark.asyncio
    async def test_refuse_mode_drops_title(self, tmp_path: Path) -> None:
        from vesmaro.mcp_server import _dispatch

        mgr = _manager(_settings(tmp_path, retrieve_refuse_on_secret=True))
        fake = _fake_url_memory(mgr, f"title {FAKE_AWS_KEY} leak")
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mgr),
            patch.object(mgr, "ingest_url", return_value=fake),
        ):
            result = await _dispatch(
                "vesma_ingest_url",
                {
                    "url": "https://example.com/page",
                    "tags": [f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:learning"],
                },
            )
        assert "error" in result
        assert "issuance refused" in result["error"]
        assert "title" not in result
        mgr.close()

    @pytest.mark.asyncio
    async def test_clean_title_passes_unredacted(self, tmp_path: Path) -> None:
        from vesmaro.mcp_server import _dispatch

        mgr = _manager(_settings(tmp_path))
        fake = _fake_url_memory(mgr, "Deploying Mnemos with podman")
        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mgr),
            patch.object(mgr, "ingest_url", return_value=fake),
        ):
            result = await _dispatch(
                "vesma_ingest_url",
                {
                    "url": "https://example.com/page",
                    "tags": [f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:learning"],
                },
            )
        assert result["title"] == "Deploying Mnemos with podman"
        mgr.close()


def _install_hermes_stubs() -> None:
    """Same minimal stubs as tests/test_hermes_plugin.py (idempotent)."""
    if "agent.memory_provider" not in sys.modules:
        agent_pkg = types.ModuleType("agent")
        agent_pkg.__path__ = []
        mp_mod = types.ModuleType("agent.memory_provider")

        class _StubMemoryProvider:
            pass

        mp_mod.MemoryProvider = _StubMemoryProvider
        sys.modules["agent"] = agent_pkg
        sys.modules["agent.memory_provider"] = mp_mod

    if "tools.registry" not in sys.modules:
        tools_pkg = types.ModuleType("tools")
        tools_pkg.__path__ = []
        reg_mod = types.ModuleType("tools.registry")
        reg_mod.tool_error = lambda msg: json.dumps({"error": msg})
        sys.modules["tools"] = tools_pkg
        sys.modules["tools.registry"] = reg_mod


class TestIngestUrlTitleScanHermes:
    def test_secret_in_fetched_title_redacted(self, tmp_path: Path) -> None:
        _install_hermes_stubs()
        repo_root = str(Path(__file__).resolve().parent.parent)
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from integrations.hermes import VesmaMemoryProvider

        mgr = _manager(_settings(tmp_path))
        fake = _fake_url_memory(mgr, f"hermes page {FAKE_AWS_KEY} end")
        provider = VesmaMemoryProvider({"project": PROJECT, "agent": AGENT})
        adapter = MagicMock()
        adapter.project = PROJECT
        adapter.agent = AGENT
        adapter.session = "sess-1"
        adapter.sdk.manager = mgr
        with (
            patch.object(provider, "_ensure", return_value=adapter),
            patch.object(mgr, "ingest_url", return_value=fake),
        ):
            raw = provider.handle_tool_call(
                "vesma_ingest_url",
                {"url": "https://example.com/", "tags": [f"project:{PROJECT}"]},
            )
        payload = json.loads(raw)
        assert FAKE_AWS_KEY not in payload["title"]
        assert "<REDACTED:aws-key>" in payload["title"]
        mgr.close()

    def test_refuse_mode_drops_title(self, tmp_path: Path) -> None:
        _install_hermes_stubs()
        repo_root = str(Path(__file__).resolve().parent.parent)
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from integrations.hermes import VesmaMemoryProvider

        mgr = _manager(_settings(tmp_path, retrieve_refuse_on_secret=True))
        fake = _fake_url_memory(mgr, f"hermes {FAKE_AWS_KEY} leak")
        provider = VesmaMemoryProvider({"project": PROJECT, "agent": AGENT})
        adapter = MagicMock()
        adapter.project = PROJECT
        adapter.agent = AGENT
        adapter.session = "sess-1"
        adapter.sdk.manager = mgr
        with (
            patch.object(provider, "_ensure", return_value=adapter),
            patch.object(mgr, "ingest_url", return_value=fake),
        ):
            raw = provider.handle_tool_call(
                "vesma_ingest_url",
                {"url": "https://example.com/", "tags": [f"project:{PROJECT}"]},
            )
        payload = json.loads(raw)
        assert "error" in payload
        assert "issuance refused" in payload["error"]
        mgr.close()


# ── m3: indexed rewrite_event_key lookup ─────────────────────────────────────

# Minimal legacy DDL — a pre-m3 memories table WITHOUT rewrite_event_key
# (rewrite_source/rewrite_session present to model the post-C10 window).
_LEGACY_MEMORIES_DDL = """
CREATE TABLE memories (
    id               TEXT PRIMARY KEY,
    content          TEXT NOT NULL,
    title            TEXT,
    tags             TEXT NOT NULL DEFAULT '[]',
    source           TEXT NOT NULL DEFAULT 'manual',
    source_url       TEXT,
    memory_type      TEXT NOT NULL DEFAULT 'note',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    metadata         TEXT NOT NULL DEFAULT '{}',
    file_path        TEXT,
    category         TEXT,
    project          TEXT NOT NULL DEFAULT '',
    agent            TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'raw',
    quality_score    REAL,
    confidence       REAL,
    source_coverage  INTEGER,
    cluster_id       TEXT,
    derived_from     TEXT NOT NULL DEFAULT '[]',
    embedding_id     TEXT,
    raw_content      TEXT,
    clean_content    TEXT,
    filter_profile   TEXT,
    filter_stats     TEXT,
    filter_version   TEXT,
    workflow_status  TEXT,
    locked_by        TEXT,
    locked_at        TEXT,
    rewrite_source   TEXT,
    rewrite_session  TEXT
)
"""


# External-content FTS + triggers created BEFORE row inserts (the same
# healthy-production shape as the schema-batch fixture): the migration
# backfills UPDATE memories, which fires memories_au — the FTS index must
# already know the rows or the trigger's 'delete' command corrupts it.
_LEGACY_MEMORIES_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    id UNINDEXED, title, content, tags, project UNINDEXED, agent UNINDEXED,
    content=memories, content_rowid=rowid, tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, id, title, content, tags, project, agent)
    VALUES (new.rowid, new.id, new.title, new.content, new.tags,
            new.project, new.agent);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, id, title, content, tags,
                             project, agent)
    VALUES ('delete', old.rowid, old.id, old.title, old.content, old.tags,
            old.project, old.agent);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, id, title, content, tags,
                             project, agent)
    VALUES ('delete', old.rowid, old.id, old.title, old.content, old.tags,
            old.project, old.agent);
    INSERT INTO memories_fts(rowid, id, title, content, tags, project, agent)
    VALUES (new.rowid, new.id, new.title, new.content, new.tags,
            new.project, new.agent);
END;
"""


def _build_legacy_db(path: Path) -> None:
    """Three rows: trusted rewrite event, planted client metadata, plain."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(_LEGACY_MEMORIES_DDL)
        conn.executescript(_LEGACY_MEMORIES_FTS)
        now = "2026-08-27T10:00:00+00:00"
        rows = [
            (
                "r1",
                "trusted rewrite event",
                json.dumps(
                    {
                        "source": "context-rewrite",
                        "rewrite_session": "s1",
                        "rewrite_event_key": "k1",
                    }
                ),
            ),
            (
                "r2",
                "client-planted key",
                json.dumps({"source": "manual", "rewrite_event_key": "planted"}),
            ),
            ("r3", "plain row", json.dumps({})),
        ]
        for mid, content, metadata in rows:
            conn.execute(
                "INSERT INTO memories (id, content, project, agent, created_at,"
                " updated_at, metadata) VALUES (?,?,?,?,?,?,?)",
                (mid, content, PROJECT, AGENT, now, now, metadata),
            )
        conn.commit()
    finally:
        conn.close()


class TestRewriteEventKeyMigration:
    def test_column_added_and_backfill_gated_on_provenance(self, tmp_path: Path) -> None:
        db = tmp_path / "legacy.db"
        _build_legacy_db(db)
        store = SQLiteStore(db)
        conn = store._get_conn()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
        assert "rewrite_event_key" in cols
        keys = {
            r["id"]: r["rewrite_event_key"]
            for r in conn.execute("SELECT id, rewrite_event_key FROM memories")
        }
        # Trusted rewrite row promoted; planted client metadata NOT
        # promoted; plain row NULL.
        assert keys["r1"] == "k1"
        assert keys["r2"] is None
        assert keys["r3"] is None
        # Backfill flag set — the UPDATE does not re-run on reconnect.
        assert conn.execute(
            "SELECT 1 FROM meta WHERE key='schema_backfill_rewrite_event_key_v1'"
        ).fetchone()
        store.close()

    def test_index_exists(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "fresh.db")
        conn = store._get_conn()
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_memories_rewrite_event_key_created'"
        ).fetchone()
        assert row is not None
        store.close()

    def test_backfill_idempotent_on_reopen(self, tmp_path: Path) -> None:
        db = tmp_path / "legacy.db"
        _build_legacy_db(db)
        store = SQLiteStore(db)
        first = (
            store._get_conn()
            .execute("SELECT rewrite_event_key FROM memories WHERE id='r1'")
            .fetchone()[0]
        )
        store.close()
        store = SQLiteStore(db)
        second = (
            store._get_conn()
            .execute("SELECT rewrite_event_key FROM memories WHERE id='r1'")
            .fetchone()[0]
        )
        assert first == second == "k1"
        store.close()


class TestRewriteEventKeyLookup:
    def test_trusted_path_populates_column_and_dedupes(self, tmp_path: Path) -> None:
        mgr = _manager(_settings(tmp_path))
        first = context_rewrite(
            mgr,
            content="replaced block with unique body 0001",
            project=PROJECT,
            agent=AGENT,
            session="sess-1",
        )
        assert first["status"] == "stored"
        conn = mgr.sqlite._get_conn()
        key = conn.execute(
            "SELECT rewrite_event_key FROM memories WHERE id=?",
            (first["memory_id"],),
        ).fetchone()[0]
        assert key == first["event_key"]
        # Redelivery deduplicates via the indexed column.
        second = context_rewrite(
            mgr,
            content="replaced block with unique body 0001",
            project=PROJECT,
            agent=AGENT,
            session="sess-1",
        )
        assert second["status"] == "deduplicated"
        assert second["memory_id"] == first["memory_id"]
        mgr.close()

    def test_untrusted_planted_metadata_never_derives_column(self, tmp_path: Path) -> None:
        mgr = _manager(_settings(tmp_path))
        planted = _add(
            mgr,
            "forged rewrite event",
            metadata={
                "source": "context-rewrite",
                "rewrite_session": "sess-evil",
                "rewrite_event_key": "evil-key",
            },
        )
        conn = mgr.sqlite._get_conn()
        row = conn.execute(
            "SELECT rewrite_source, rewrite_session, rewrite_event_key FROM memories WHERE id=?",
            (planted.id,),
        ).fetchone()
        assert tuple(row) == (None, None, None)
        assert mgr.sqlite.get_memory_id_by_rewrite_event_key("evil-key") is None
        mgr.close()

    def test_lookup_equivalence_locked_against_json_extract(self, tmp_path: Path) -> None:
        """The pre-m3 implementation's formula, verbatim, on the same data."""
        mgr = _manager(_settings(tmp_path))
        for n in range(3):
            context_rewrite(
                mgr,
                content=f"event body {n:04d} unique",
                project=PROJECT,
                agent=AGENT,
                session=f"s{n}",
            )
        _add(
            mgr,
            "planted via generic surface",
            metadata={"source": "context-rewrite", "rewrite_event_key": "planted"},
        )
        conn = mgr.sqlite._get_conn()
        keys = [
            r["key"]
            for r in conn.execute(
                "SELECT DISTINCT json_extract(metadata, '$.rewrite_event_key') "
                "AS key FROM memories "
                "WHERE json_extract(metadata, '$.rewrite_event_key') IS NOT NULL"
            )
        ]
        assert len(keys) == 4  # three trusted + one planted
        for key in keys:
            legacy = conn.execute(
                "SELECT id FROM memories "
                "WHERE json_extract(metadata, '$.rewrite_event_key') = ? "
                "ORDER BY created_at ASC LIMIT 1",
                (key,),
            ).fetchone()
            via_column = mgr.sqlite.get_memory_id_by_rewrite_event_key(key)
            if key == "planted":
                # The planted key is metadata-only: the legacy formula
                # finds it (the pre-m3 vulnerability), the column lookup
                # (the production path) correctly does not.
                assert legacy is not None and via_column is None
                continue
            if legacy is None:
                assert via_column is None
            else:
                assert via_column == str(legacy["id"]), key
        assert mgr.sqlite.get_memory_id_by_rewrite_event_key("planted") is None
        mgr.close()
