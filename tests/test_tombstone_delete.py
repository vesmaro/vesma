"""Stage 1 delete: tombstone + purge — conformance matrix (ArchCom 2026-10-18).

Surface x tombstone conformance for the soft-delete wave:

* terminal state pins (workflow machine) + the write chokepoint;
* the read checkpoint: default filter on every row-materialization
  surface (no-bypass), parameterized opt-in for admin paths;
* the hash-chained deletion journal + the doctor integrity check;
* owner-only authz (REST operator gate, MCP absence, workflow refusal);
* derived_from lineage quarantine + report;
* no-resurrection suite (DLQ scheduler, pipeline sweeper/swap, import
  merge/restore, federation mapping);
* migration on data copies without loss.

Stage 2 (hard purge) is deliberately NOT covered here.
"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import vesma.api.main as api_main
from vesma.api.auth_store import AuthStore
from vesma.api.main import app, lifespan
from vesma.api.middleware import AuthMiddleware
from vesma.compact import (
    CONTENT_STATE_TOMBSTONED,
    build_metadata_entry,
)
from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import MemoryCreate, PipelineState
from vesma.policy.scheduler import dlq_retry_scheduler
from vesma.storage.sqlite_store import (
    SQLiteStore,
    TombstonedRowFilteredError,
    tombstones_visible,
    verify_deletion_journal_conn,
)
from vesma.workflow import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    WorkflowStatus,
    is_terminal,
    transition_allowed,
    validate_transition,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _settings(tmp: Path) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "tombstone.db",
        },
        embedding={"provider": "onnx"},
        scanner={"enabled": False},
    )
    settings.resolve_paths()
    return settings


def _manager(settings: Settings) -> MemoryManager:
    mgr = MemoryManager(settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    return mgr


@pytest.fixture
def manager() -> Iterator[MemoryManager]:
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = _manager(_settings(Path(tmpdir)))
        yield mgr
        mgr.close()


def _add(mgr: MemoryManager, content: str = "body", title: str = "t") -> Any:
    return mgr.add(
        MemoryCreate(
            content=content,
            title=title,
            # vesma:learning — a STRICT-mode-valid subtype (the JSON import
            # re-validates tags strictly; vesma:note would fail that leg).
            tags=["project:p", "agent:a", "vesma:learning"],
        ),
        project="p",
        agent="a",
        mint_relates_to=False,
    )


def _add_child(mgr: MemoryManager, parent_id: str, content: str = "child") -> Any:
    """Add a memory and wire it as a direct derived_from child of parent_id.

    The ingest path owns lineage minting; tests wire the parent link
    through the update_fields whitelist (the same write path the pipeline
    uses) so no production code is bypassed.
    """
    child = _add(mgr, content=content, title="child")
    assert mgr.sqlite.update_fields(child.id, derived_from=json.dumps([parent_id]))
    return child


def _tombstone(mgr: MemoryManager, memory_id: str) -> dict[str, Any]:
    return mgr.delete_memory(memory_id, actor="test-owner", reason="conformance")


def _snapshot_db(source: Path, target: Path) -> None:
    """Consistent copy of the live DB via the SQLite backup API (the same
    posture as the mover's snapshot gate — WAL-safe, no file-level race)."""
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(target))
    src.backup(dst)
    dst.close()
    src.close()


def _raw_workflow_status(store: SQLiteStore, memory_id: str) -> str | None:
    row = (
        store._get_conn()
        .execute("SELECT workflow_status FROM memories WHERE id = ?", (memory_id,))
        .fetchone()
    )
    return None if row is None else row["workflow_status"]


# ---------------------------------------------------------------------------
# 1. Terminal state pins (the state machine)
# ---------------------------------------------------------------------------


class TestTerminalState:
    def test_tombstoned_is_terminal(self) -> None:
        """tombstoned joins done/withdrawn in the terminal set."""
        assert WorkflowStatus.TOMBSTONED in TERMINAL_STATUSES
        assert is_terminal(WorkflowStatus.TOMBSTONED)

    def test_enterable_from_every_other_state(self) -> None:
        """Deletion knows no workflow state: every state → tombstoned."""
        for state in WorkflowStatus:
            if state is WorkflowStatus.TOMBSTONED:
                continue
            assert transition_allowed(state, WorkflowStatus.TOMBSTONED), state
        # Legacy rows carry NULL — treated as open, deletable.
        assert transition_allowed(None, WorkflowStatus.TOMBSTONED)

    def test_no_edge_out_of_tombstoned(self) -> None:
        """Terminal: tombstoned has no outgoing edges, ever."""
        assert ALLOWED_TRANSITIONS[WorkflowStatus.TOMBSTONED] == frozenset()
        for state in WorkflowStatus:
            if state is WorkflowStatus.TOMBSTONED:
                continue
            with pytest.raises(ValueError):
                validate_transition(WorkflowStatus.TOMBSTONED, state)

    def test_terminal_states_gain_only_tombstoned_edge(self) -> None:
        """The ONLY edge out of done/withdrawn goes to tombstoned —
        the delete path does not weaken terminality for any other target."""
        for terminal in (WorkflowStatus.DONE, WorkflowStatus.WITHDRAWN):
            assert ALLOWED_TRANSITIONS[terminal] == frozenset({WorkflowStatus.TOMBSTONED})


# ---------------------------------------------------------------------------
# 2. Write chokepoint pins
# ---------------------------------------------------------------------------


class TestWriteChokepoint:
    def test_update_fields_cannot_write_workflow_status(self, manager: MemoryManager) -> None:
        """The generic field updater silently drops workflow_status —
        the column's only writer is set_workflow_status."""
        mem = _add(manager)
        # Pure attempt: the key is dropped before SQL, nothing updates.
        assert not manager.sqlite.update_fields(mem.id, workflow_status="tombstoned")
        assert _raw_workflow_status(manager.sqlite, mem.id) is None
        # Mixed with a live field: the live field lands, the workflow key
        # is still dropped (unknown keys never reach the SQL builder).
        assert manager.sqlite.update_fields(mem.id, title="renamed", workflow_status="open")
        assert _raw_workflow_status(manager.sqlite, mem.id) is None
        assert manager.sqlite.get(mem.id).title == "renamed"

    def test_save_preserves_tombstoned(self, manager: MemoryManager) -> None:
        """save() never writes the column: re-saving a tombstoned row
        (the update branch import paths use) keeps it tombstoned."""
        mem = _add(manager)
        _tombstone(manager, mem.id)
        memory = manager.sqlite.get(mem.id, include_tombstoned=True)
        assert memory is not None
        manager.sqlite.save(memory)
        assert _raw_workflow_status(manager.sqlite, mem.id) == "tombstoned"

    def test_workflow_set_refuses_tombstoned(self, manager: MemoryManager) -> None:
        """The generic workflow surface (MCP tool + REST land here) cannot
        soft-delete: tombstoned is reserved for the delete path."""
        mem = _add(manager)
        with pytest.raises(ValueError, match="reserved for the delete path"):
            manager.workflow_set(mem.id, "tombstoned", actor="someone")
        # The row is untouched.
        assert _raw_workflow_status(manager.sqlite, mem.id) is None

    def test_set_workflow_status_is_the_only_writer(self, manager: MemoryManager) -> None:
        """Pin the chokepoint itself: a direct set_workflow_status write
        lands, proving the column is writable only through that method
        (the negative side is pinned by the update_fields test above)."""
        mem = _add(manager)
        assert manager.sqlite.set_workflow_status(mem.id, "tombstoned", None, None)
        assert _raw_workflow_status(manager.sqlite, mem.id) == "tombstoned"


# ---------------------------------------------------------------------------
# 3. Delete happy path + idempotency
# ---------------------------------------------------------------------------


class TestDeletePath:
    def test_delete_tombstones_and_journals(self, manager: MemoryManager) -> None:
        mem = _add(manager)
        result = _tombstone(manager, mem.id)
        assert result["to_status"] == "tombstoned"
        assert result["terminal"] is True
        assert result["idempotent"] is False
        assert result["journal_id"] and result["entry_hash"]
        # State + audit + journal all landed.
        assert _raw_workflow_status(manager.sqlite, mem.id) == "tombstoned"
        history = manager.sqlite.get_workflow_history(mem.id)
        assert history and history[0]["to_status"] == "tombstoned"
        assert history[0]["actor"] == "test-owner"
        entries = manager.sqlite.list_deletion_journal()
        assert len(entries) == 1 and entries[0]["memory_id"] == mem.id

    def test_delete_requires_actor(self, manager: MemoryManager) -> None:
        mem = _add(manager)
        with pytest.raises(ValueError, match="actor is required"):
            manager.delete_memory(mem.id, actor="  ")

    def test_delete_unknown_memory_404_class(self, manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="not found"):
            manager.delete_memory("no-such-id", actor="owner")

    def test_second_delete_is_idempotent_noop(self, manager: MemoryManager) -> None:
        mem = _add(manager)
        _tombstone(manager, mem.id)
        result = manager.delete_memory(mem.id, actor="test-owner")
        assert result["idempotent"] is True
        # No duplicate journal entry for the same deletion.
        assert len(manager.sqlite.list_deletion_journal()) == 1


# ---------------------------------------------------------------------------
# 4. Read checkpoint: no-bypass conformance matrix
# ---------------------------------------------------------------------------


class TestReadCheckpointMatrix:
    def _populate(self, manager: MemoryManager) -> Any:
        mem = _add(manager, content="xylophone quarantine marker", title="matrix")
        # give the row a pipeline_state + cluster so the state/cluster
        # surfaces have something to filter
        manager.sqlite.update_fields(
            mem.id,
            pipeline_state=PipelineState.PENDING,
            cluster_id="cl1",
        )
        return mem

    def test_every_read_surface_excludes_tombstoned(self, manager: MemoryManager) -> None:
        """«Поверхность x tombstone»: the negative test of the ArchCom
        amendment — every UNMARKED read path filters the deleted row."""
        mem = self._populate(manager)
        _tombstone(manager, mem.id)
        store = manager.sqlite
        assert store.get(mem.id) is None
        assert not any(m.id == mem.id for m in store.list_all(limit=100))
        assert not any(m.id == mem.id for m in store.list_recent_for_agent("a", limit=100))
        assert not any(m.id == mem.id for m in store.list_by_cluster("cl1"))
        assert not any(m.id == mem.id for m, _ in store.fts_search("xylophone", limit=100))
        assert not any(m.id == mem.id for m, _ in store.list_all_for_mesh(limit=100))
        assert not any(m.id == mem.id for m in store.list_refine_intake())
        assert not any(m.id == mem.id for m in store.list_by_pipeline_state(PipelineState.PENDING))
        # dedupe lookups answer None (an explicit re-add creates a NEW row,
        # never a resurrect of the deleted one)
        assert store.get_by_file_path("whatever.md") is None
        assert store.get_by_source_url("https://example.test/x") is None

    def test_unmarked_materialization_fails_closed(self, manager: MemoryManager) -> None:
        """The chokepoint: a tombstoned row that reaches _row_to_memory on
        any path WITHOUT opt-in raises — future read paths cannot leak."""
        mem = _add(manager)
        _tombstone(manager, mem.id)
        row = (
            manager.sqlite._get_conn()
            .execute("SELECT * FROM memories WHERE id = ?", (mem.id,))
            .fetchone()
        )
        with pytest.raises(TombstonedRowFilteredError):
            manager.sqlite._row_to_memory(row)

    def test_optin_contextvar_scoped_and_reentrant(self, manager: MemoryManager) -> None:
        mem = _add(manager)
        _tombstone(manager, mem.id)
        row = (
            manager.sqlite._get_conn()
            .execute("SELECT * FROM memories WHERE id = ?", (mem.id,))
            .fetchone()
        )
        with pytest.raises(TombstonedRowFilteredError):
            manager.sqlite._row_to_memory(row)
        with tombstones_visible():
            with tombstones_visible():  # re-entrant
                memory = manager.sqlite._row_to_memory(row)
            assert memory.workflow_status == "tombstoned"
            assert manager.sqlite.get(mem.id, include_tombstoned=True) is not None
        # restored after the block
        with pytest.raises(TombstonedRowFilteredError):
            manager.sqlite._row_to_memory(row)

    def test_list_all_include_tombstoned_keyword(self, manager: MemoryManager) -> None:
        mem = self._populate(manager)
        _tombstone(manager, mem.id)
        with tombstones_visible():
            rows = manager.sqlite.list_all(limit=100, include_tombstoned=True)
        assert any(m.id == mem.id for m in rows)

    def test_search_manager_level_excludes(self, manager: MemoryManager) -> None:
        mem = self._populate(manager)
        _tombstone(manager, mem.id)
        results = manager.search("xylophone", limit=20)
        assert not any(r.memory.id == mem.id for r in results)

    def test_refine_intake_skips_tombstoned(self, manager: MemoryManager) -> None:
        """No-resurrection (swap leg): the B2 daemon must never pick a
        tombstoned row for refinement, claim it, or swap it."""
        mem = self._populate(manager)
        _tombstone(manager, mem.id)
        intake = manager.sqlite.list_refine_intake()
        assert not any(m.id == mem.id for m in intake)
        # The CAS claim also refuses (the shared WHERE carries the filter).
        assert not manager.sqlite.claim_for_refinement(mem.id)


# ---------------------------------------------------------------------------
# 5. Deletion journal + integrity
# ---------------------------------------------------------------------------


class TestJournal:
    def test_chain_verifies_after_multiple_deletes(self, manager: MemoryManager) -> None:
        ids = [_add(manager, content=f"m{i}").id for i in range(3)]
        for mid in ids:
            _tombstone(manager, mid)
        verdict = manager.sqlite.verify_deletion_journal()
        assert verdict == {
            "ok": True,
            "entries": 3,
            "broken_seq": None,
            "reason": "",
        }

    def test_tamper_detection(self, manager: MemoryManager) -> None:
        mem = _add(manager)
        _tombstone(manager, mem.id)
        conn = manager.sqlite._get_conn()
        # A silent in-place edit of a past deletion must break the chain.
        conn.execute("UPDATE deletion_journal SET reason = 'rewritten' WHERE seq = 1")
        conn.commit()
        verdict = manager.sqlite.verify_deletion_journal()
        assert verdict["ok"] is False
        assert verdict["broken_seq"] == 1

    def test_truncation_detection(self, manager: MemoryManager) -> None:
        """A removed tail entry breaks the NEXT entry's prev_hash binding —
        removal is detectable even though the row itself is gone."""
        first = _add(manager, content="first")
        second = _add(manager, content="second")
        _tombstone(manager, first.id)
        _tombstone(manager, second.id)
        conn = manager.sqlite._get_conn()
        conn.execute("DELETE FROM deletion_journal WHERE seq = 1")
        conn.commit()
        verdict = verify_deletion_journal_conn(conn)
        assert verdict["ok"] is False
        assert verdict["broken_seq"] == 2

    def test_tail_removal_not_detectable_by_chain_alone(self, manager: MemoryManager) -> None:
        """F3 (honest scope, cascade review 2026-10-20): removing the LAST
        entry leaves a chain that still verifies — the walk simply ends one
        entry earlier. This test pins the REAL behavior so the boundary is
        explicit: closing it needs an external head-hash/count anchor
        (Stage-2 candidate, see verify_deletion_journal_conn). The journal
        count visible in `vesma doctor` is the only observable trace."""
        only = _add(manager, content="only")
        _tombstone(manager, only.id)
        assert manager.sqlite.verify_deletion_journal()["entries"] == 1
        conn = manager.sqlite._get_conn()
        conn.execute("DELETE FROM deletion_journal WHERE seq = 1")
        conn.commit()
        verdict = verify_deletion_journal_conn(conn)
        assert verdict["ok"] is True  # honest: not detectable by the chain alone
        assert verdict["entries"] == 0
        # The tombstoned ROW, however, still carries no journal entry —
        # the doctor's F4 cross-check is the compensating control.
        from vesma.cli.doctor import _check_deletion_journal

        result = _check_deletion_journal(manager.settings)
        assert result.status.value == "fail"
        assert "WITHOUT a journal entry" in result.detail

    def test_journal_descendants_recorded(self, manager: MemoryManager) -> None:
        parent = _add(manager)
        child = _add_child(manager, parent.id)
        result = _tombstone(manager, parent.id)
        entries = manager.sqlite.list_deletion_journal()
        assert entries[0]["descendants"] == [child.id]
        assert result["descendants_quarantined"] == [child.id]

    def test_no_removal_path_in_store_api(self) -> None:
        """No-bypass: the store exposes append/count/list/verify ONLY — no
        update/delete/wipe surface exists for the journal."""
        journal_methods = [
            name for name in dir(SQLiteStore) if "journal" in name and not name.startswith("_")
        ]
        assert sorted(journal_methods) == [
            "append_deletion_journal",
            "journal_entry_count",
            "list_deletion_journal",
            "verify_deletion_journal",
        ]

    def test_no_journal_route_in_rest(self) -> None:
        """No REST route exposes journal mutation."""
        paths = [
            getattr(r, "path", "")
            for r in app.routes
            if "journal" in getattr(r, "path", "").lower()
        ]
        assert paths == []

    def test_no_journal_tool_in_mcp(self) -> None:
        """No MCP tool exposes journal mutation."""
        from vesma.mcp_server import list_tools

        tools = asyncio.run(list_tools())
        offenders = [t.name for t in tools if "journal" in t.name.lower()]
        assert offenders == []

    def test_doctor_check_passes_and_detects_tamper(self, manager: MemoryManager) -> None:
        from vesma.cli.doctor import _check_deletion_journal

        settings = manager.settings
        mem = _add(manager)
        _tombstone(manager, mem.id)
        ok = _check_deletion_journal(settings)
        assert ok.status.value == "pass", ok.detail
        conn = manager.sqlite._get_conn()
        conn.execute("UPDATE deletion_journal SET actor = 'someone-else' WHERE seq = 1")
        conn.commit()
        bad = _check_deletion_journal(settings)
        assert bad.status.value == "fail", bad.detail


# ---------------------------------------------------------------------------
# 6. Owner-only authz
# ---------------------------------------------------------------------------


class TestAuthz:
    def test_mcp_has_no_delete_tool(self) -> None:
        """MCP tokens get NO memory-delete surface: the tool registry
        carries no tool that deletes a MEMORY (OWASP LLM01 / ASVS V7 — an
        agent cannot be granted what does not exist). The one 'delete'
        tool, ``vesma_delete_graph_project``, purges the code-graph INDEX
        projection — it never touches the memories table."""
        from vesma.mcp_server import list_tools

        tools = asyncio.run(list_tools())
        offenders = [
            t.name
            for t in tools
            if "delete" in t.name.lower() and t.name != "vesma_delete_graph_project"
        ]
        assert offenders == []

    def test_rest_delete_loopback_owner_allowed(self, manager: MemoryManager) -> None:
        """auth disabled (loopback single-operator deploy): the caller IS
        the owner — delete answers 200 with the tombstone result."""
        test_app = _test_app(manager, auth_enabled=False)
        mem = _add(manager)
        with TestClient(test_app) as tc:
            resp = tc.request(
                "DELETE", f"/memories/{mem.id}", json={"actor": "owner", "reason": "cleanup"}
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["to_status"] == "tombstoned"

    def test_rest_delete_without_operator_refused(self, manager: MemoryManager) -> None:
        """auth enabled + no admitted session/token: the DELETE surface is
        closed (middleware 401; the endpoint's own 403 is the in-depth
        layer for any request that reaches it without operator context)."""
        test_app = _test_app(manager, auth_enabled=True)
        mem = _add(manager)
        with TestClient(test_app) as tc:
            resp = tc.request("DELETE", f"/memories/{mem.id}", json={"actor": "agent"})
        assert resp.status_code == 401, resp.text
        assert _raw_workflow_status(manager.sqlite, mem.id) != "tombstoned"

    def test_rest_delete_with_operator_token_allowed(
        self, manager: MemoryManager, tmp_path: Path
    ) -> None:
        """auth enabled + admitted bearer token (owner identity): delete
        succeeds — the owner-auth level is the gate, not the transport."""
        settings = manager.settings
        settings.vesma.data_dir.mkdir(parents=True, exist_ok=True)
        auth_store = AuthStore(settings.db_path)
        _token_id, plaintext = auth_store.create_token(name="owner", totp_required=False)
        auth_store.close()
        test_app = _test_app(manager, auth_enabled=True)
        mem = _add(manager)
        with TestClient(test_app) as tc:
            resp = tc.request(
                "DELETE",
                f"/memories/{mem.id}",
                json={"actor": "owner", "reason": "operator delete"},
                headers={"Authorization": f"Bearer {plaintext}"},
            )
        assert resp.status_code == 200, resp.text
        assert _raw_workflow_status(manager.sqlite, mem.id) == "tombstoned"

    def test_rest_delete_unknown_memory_404(self, manager: MemoryManager) -> None:
        test_app = _test_app(manager, auth_enabled=False)
        with TestClient(test_app) as tc:
            resp = tc.request("DELETE", "/memories/no-such-id", json={"actor": "owner"})
        assert resp.status_code == 404

    def test_rest_delete_missing_actor_422(self, manager: MemoryManager) -> None:
        test_app = _test_app(manager, auth_enabled=False)
        mem = _add(manager)
        with TestClient(test_app) as tc:
            resp = tc.request("DELETE", f"/memories/{mem.id}", json={"actor": "   "})
        assert resp.status_code == 422


def _test_app(manager: MemoryManager, *, auth_enabled: bool) -> FastAPI:
    """Fresh FastAPI app with the real routes + auth middleware wired."""
    manager.settings.api.auth_enabled = auth_enabled
    test_app = FastAPI(title="Tombstone-Test", version="0.0.1", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)
    test_app.exception_handlers.update(app.exception_handlers)
    if auth_enabled:
        test_app.add_middleware(AuthMiddleware)
    api_main._manager = manager
    return test_app


# ---------------------------------------------------------------------------
# 7. derived_from lineage quarantine
# ---------------------------------------------------------------------------


class TestLineage:
    def test_children_quarantined_lineage_broken(self, manager: MemoryManager) -> None:
        parent = _add(manager)
        child = _add_child(manager, parent.id)
        result = _tombstone(manager, parent.id)
        assert result["descendants_quarantined"] == [child.id]
        stored = manager.sqlite.get(child.id)
        assert stored is not None
        assert stored.pipeline_state == PipelineState.QUARANTINED
        assert stored.quarantine_reason is not None
        assert "lineage-broken" in stored.quarantine_reason
        assert parent.id in stored.quarantine_reason

    def test_no_transitive_cascade(self, manager: MemoryManager) -> None:
        """One hop only: a grandchild keeps its state (explicit cascade
        command is a later stage — Stage 1 reports, it does not cascade)."""
        parent = _add(manager)
        child = _add_child(manager, parent.id)
        grandchild = _add_child(manager, child.id, content="grandchild")
        result = _tombstone(manager, parent.id)
        assert result["descendants_quarantined"] == [child.id]
        grand = manager.sqlite.get(grandchild.id)
        assert grand is not None
        assert grand.pipeline_state != PipelineState.QUARANTINED

    def test_already_hidden_child_skipped(self, manager: MemoryManager) -> None:
        """A child that is already tombstoned/quarantined is not
        double-marked; it lands in descendants_skipped."""
        parent = _add(manager)
        child = _add_child(manager, parent.id)
        _tombstone(manager, child.id)
        result = _tombstone(manager, parent.id)
        assert result["descendants_quarantined"] == []
        assert result["descendants_skipped"] == [child.id]


# ---------------------------------------------------------------------------
# 8. No-resurrection suite
# ---------------------------------------------------------------------------


class TestNoResurrection:
    def test_dlq_scheduler_does_not_raise(self, manager: MemoryManager) -> None:
        """A tombstoned memory sitting in the DLQ stays deleted when the
        retry scheduler ticks."""
        mem = _add(manager)
        manager.sqlite.dlq_add(mem.id, task_label="synthesize", error_message="boom")
        _tombstone(manager, mem.id)
        dlq_retry_scheduler(manager)  # must not raise the row
        assert manager.sqlite.get(mem.id) is None
        assert _raw_workflow_status(manager.sqlite, mem.id) == "tombstoned"

    def test_pipeline_sweeper_skips_tombstoned(self, manager: MemoryManager) -> None:
        mem = _add(manager)
        manager.sqlite.update_fields(mem.id, pipeline_state=PipelineState.FAILED)
        _tombstone(manager, mem.id)
        # The sweeper's listing (list_by_pipeline_state) and the intake
        # predicate both refuse the row — pinned in the matrix above; here
        # we pin the swap-side projection: the row is not served, whatever
        # its pipeline bookkeeping says.
        assert manager.sqlite.list_by_pipeline_state(PipelineState.FAILED) == []

    def test_import_merge_skips_tombstoned_json(self, manager: MemoryManager) -> None:
        """A JSON export carrying a tombstoned row cannot resurrect it:
        the import filter skips the entry entirely."""
        mem = _add(manager)
        _tombstone(manager, mem.id)
        export = manager.sqlite.get(mem.id, include_tombstoned=True)
        assert export is not None
        payload = {
            "format_version": 3,
            "memories": [json.loads(export.model_dump_json())],
            "projects": [],
        }
        from vesma.cli.import_ import _import_json

        result = _import_json(manager, payload, mode="merge", overwrite=False, dry_run=False)
        assert result.errors == []
        assert result.imported == 0
        assert _raw_workflow_status(manager.sqlite, mem.id) == "tombstoned"
        assert manager.sqlite.get(mem.id) is None

    def test_import_merge_skips_tombstoned_sqlite_snapshot(
        self, manager: MemoryManager, tmp_path: Path
    ) -> None:
        """Same filter on the SQLite merge leg (raw snapshot rows)."""
        mem = _add(manager)
        _tombstone(manager, mem.id)
        # Build a snapshot DB containing the tombstoned row.
        snap = tmp_path / "snap.db"
        _snapshot_db(manager.settings.db_path, snap)
        from vesma.cli.import_ import _import_sqlite

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(snap, arcname="mnemos.db")
        _import_sqlite(manager, buf.getvalue(), mode="merge", dry_run=False, backup_dir=None)
        # The tombstoned row is skipped (not imported, not resurrected).
        assert _raw_workflow_status(manager.sqlite, mem.id) == "tombstoned"
        assert manager.sqlite.get(mem.id) is None

    def test_import_restore_keeps_tombstoned_hidden(
        self, manager: MemoryManager, tmp_path: Path
    ) -> None:
        """SQLite-snapshot RESTORE (raw file swap): the tombstoned row
        survives byte-for-byte and stays invisible after the sweep."""
        mem = _add(manager)
        _tombstone(manager, mem.id)
        snap = tmp_path / "snap.db"
        _snapshot_db(manager.settings.db_path, snap)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(snap, arcname="mnemos.db")
        from vesma.cli.import_ import _import_sqlite

        result = _import_sqlite(
            manager, buf.getvalue(), mode="restore", dry_run=False, backup_dir=None
        )
        assert result.errors == []
        assert _raw_workflow_status(manager.sqlite, mem.id) == "tombstoned"
        assert manager.sqlite.get(mem.id) is None
        assert any("tombstoned" in w for w in result.warnings), result.warnings

    def test_federation_metadata_entry_maps_tombstoned(self, manager: MemoryManager) -> None:
        """The mesh metadata leg maps a tombstoned memory to
        content_state=tombstoned (the peer import leg skips it)."""
        mem = _add(manager, content="federate me", title="fed")
        entry = build_metadata_entry(mem, origin_peer="self")
        assert entry is not None
        assert entry.content_state == "available"
        _tombstone(manager, mem.id)
        tombstoned = manager.sqlite.get(mem.id, include_tombstoned=True)
        entry2 = build_metadata_entry(tombstoned, origin_peer="self")
        assert entry2 is not None
        assert entry2.content_state == CONTENT_STATE_TOMBSTONED

    def test_mesh_body_export_excludes_tombstoned(self, manager: MemoryManager) -> None:
        """A deleted memory is never served as available mesh content."""
        mem = _add(manager, content="mesh body", title="mesh")
        _tombstone(manager, mem.id)
        pairs = manager.sqlite.list_all_for_mesh(limit=100)
        assert not any(m.id == mem.id for m, _ in pairs)


# ---------------------------------------------------------------------------
# 9. Export / sync admin surfaces
# ---------------------------------------------------------------------------


class TestAdminSurfaces:
    def test_export_default_excludes_optin_sees(self, manager: MemoryManager) -> None:
        """F2 (cascade review): list_for_export EXCLUDES tombstoned by
        default (it feeds agent-facing surfaces — MCP export, scanner);
        the explicit operator opt-in keeps round-trip fidelity."""
        mem = _add(manager)
        _tombstone(manager, mem.id)
        assert not any(m.id == mem.id for m in manager.sqlite.list_for_export())
        rows = manager.sqlite.list_for_export(include_tombstoned=True)
        assert any(m.id == mem.id and m.workflow_status == "tombstoned" for m in rows)

    def test_build_json_payload_default_excludes_tombstoned(self, manager: MemoryManager) -> None:
        """F2: the shared payload builder filters tombstoned rows for
        agent-facing filters (belt over the store flag)."""
        from vesma.cli.export import ExportFilter, build_json_payload

        mem = _add(manager, content="secret deleted body", title="gone")
        _tombstone(manager, mem.id)
        payload = build_json_payload(manager, ExportFilter())
        assert not any(m["id"] == mem.id for m in payload["memories"])
        # Operator opt-in keeps round-trip fidelity.
        payload = build_json_payload(manager, ExportFilter(include_tombstoned=True))
        assert any(m["id"] == mem.id for m in payload["memories"])

    def test_mcp_export_json_excludes_tombstoned(
        self, manager: MemoryManager, tmp_path: Path
    ) -> None:
        """F2 pin: the MCP vesma_export tool NEVER ships deleted content
        to the agent-facing file."""
        from vesma.mcp_server import _handle_export

        mem = _add(manager, content="agent must not read this", title="mcp-leak")
        _tombstone(manager, mem.id)
        out = tmp_path / "mcp-export.json"
        result = _handle_export(manager, {"output_path": str(out), "format": "json"})
        assert "error" not in result, result
        payload = json.loads(out.read_text())
        assert not any(m["id"] == mem.id for m in payload["memories"])
        assert "agent must not read this" not in out.read_text()

    def test_mcp_export_sqlite_refused(self, manager: MemoryManager, tmp_path: Path) -> None:
        """F2 pin: format=sqlite from the MCP surface is refused — the
        full snapshot carries tombstoned rows + the journal."""
        from vesma.mcp_server import _handle_export

        out = tmp_path / "mcp-export.tar.gz"
        result = _handle_export(manager, {"output_path": str(out), "format": "sqlite"})
        assert "error" in result
        assert "operator-only" in result["error"]
        assert not out.exists()

    def test_rest_export_sqlite_requires_operator(
        self, manager: MemoryManager, tmp_path: Path
    ) -> None:
        """F2 pin: the REST /api/v1/export sqlite leg (whole DB + journal)
        answers 403 without operator context, 200 with one."""
        settings = manager.settings
        settings.vesma.data_dir.mkdir(parents=True, exist_ok=True)
        auth_store = AuthStore(settings.db_path)
        _token_id, plaintext = auth_store.create_token(name="owner", totp_required=False)
        auth_store.close()
        test_app = _test_app(manager, auth_enabled=True)
        with TestClient(test_app) as tc:
            refused = tc.request(
                "POST",
                "/api/v1/export",
                json={"format": "sqlite"},
            )
            assert refused.status_code == 401, refused.text  # middleware closes first
            ok = tc.request(
                "POST",
                "/api/v1/export",
                json={"format": "sqlite"},
                headers={"Authorization": f"Bearer {plaintext}"},
            )
            assert ok.status_code == 200, ok.text

    def test_rest_export_json_operator_includes_tombstoned(
        self, manager: MemoryManager, tmp_path: Path
    ) -> None:
        """F2 pin (REST semantics): under the T-AUTH trust model an
        ADMITTED REST token IS an operator (no token roles exist), so the
        json leg keeps round-trip fidelity and includes the tombstoned
        row. The agent-facing exclusion lives on the MCP leg (no operator
        concept: tombstoned always excluded, sqlite refused) and in the
        default list_for_export filter (scanner, sync)."""
        settings = manager.settings
        settings.vesma.data_dir.mkdir(parents=True, exist_ok=True)
        auth_store = AuthStore(settings.db_path)
        _token_id, plaintext = auth_store.create_token(name="owner", totp_required=False)
        auth_store.close()
        test_app = _test_app(manager, auth_enabled=True)
        mem = _add(manager, content="operator round-trip keeps me", title="rest-op")
        _tombstone(manager, mem.id)
        with TestClient(test_app) as tc:
            resp = tc.post(
                "/api/v1/export",
                json={"format": "json"},
                headers={"Authorization": f"Bearer {plaintext}"},
            )
            assert resp.status_code == 200, resp.text
            assert "operator round-trip keeps me" in resp.text

    def test_scanner_walk_excludes_tombstoned(self, manager: MemoryManager) -> None:
        """F2: list_for_export's default exclusion also covers the pipeline
        scanner's corpus walk — the sweeper never reprocesses a deleted
        row's content."""
        mem = _add(manager, content="scanner bait")
        _tombstone(manager, mem.id)
        rows = manager.sqlite.list_for_export(since=None, limit=None)
        assert not any(m.id == mem.id for m in rows)

    def test_sync_export_excludes_tombstoned_content(self, manager: MemoryManager) -> None:
        """Guard-refusal posture (Stage 1): the compact sync payload never
        ships a tombstoned row — a peer cannot receive deleted content at
        all (marker propagation rides the mesh metadata leg)."""
        from vesma.cli.sync import _query_memories_for_sync

        mem = _add(manager)
        _tombstone(manager, mem.id)
        eligible = _query_memories_for_sync(manager, ["p"])
        assert not any(m.id == mem.id for m in eligible)


# ---------------------------------------------------------------------------
# 10b. CLI surface (interactive gate)
# ---------------------------------------------------------------------------


class TestCliDelete:
    @pytest.fixture
    def isolated_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        from vesma.cli._manager import reset_manager

        reset_manager()
        cfg = tmp_path / "vesma.yaml"
        cfg.write_text(
            f"vesma:\n"
            f"  vault_path: {tmp_path / 'vault'}\n"
            f"  data_dir: {tmp_path / 'data'}\n"
            f"  db_name: tombstone-cli.db\n"
            f"embedding:\n  provider: nano\n"
        )
        monkeypatch.setenv("VESMA_CONFIG", str(cfg))
        yield cfg
        reset_manager()

    def _manager_via_cli(self) -> MemoryManager:
        from vesma.cli._manager import get_manager

        return get_manager()

    def test_cli_delete_requires_confirmation_in_tty(
        self, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-interactive shell without --yes: refuse (the human gate is
        the owner-only enforcement at this surface)."""
        from vesma.cli.main import app

        mgr = self._manager_via_cli()
        mem = _add(mgr)
        monkeypatch.setattr("sys.stdin", None)  # forces the non-tty branch
        result = runner.invoke(app, ["delete", mem.id])
        assert result.exit_code == 1
        assert _raw_workflow_status(mgr.sqlite, mem.id) != "tombstoned"

    def test_cli_delete_with_yes_tombstones(
        self, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from vesma.cli.main import app

        mgr = self._manager_via_cli()
        mem = _add(mgr)
        monkeypatch.setattr("sys.stdin", None)
        result = runner.invoke(app, ["delete", mem.id, "--yes", "--reason", "cli wave"])
        assert result.exit_code == 0, result.output
        assert _raw_workflow_status(mgr.sqlite, mem.id) == "tombstoned"
        assert len(mgr.sqlite.list_deletion_journal()) == 1

    def test_cli_delete_wrong_typed_id_aborts(self, isolated_config: Path) -> None:
        """Typed confirmation must match the id exactly — a mismatch aborts
        with nothing deleted."""
        from vesma.cli.main import app

        mgr = self._manager_via_cli()
        mem = _add(mgr)
        result = runner.invoke(app, ["delete", mem.id], input="not-the-id\n")
        assert result.exit_code == 1
        assert _raw_workflow_status(mgr.sqlite, mem.id) != "tombstoned"

    def test_cli_delete_typed_id_confirms(self, isolated_config: Path) -> None:
        from vesma.cli.main import app

        mgr = self._manager_via_cli()
        mem = _add(mgr)
        result = runner.invoke(app, ["delete", mem.id], input=f"{mem.id}\n")
        assert result.exit_code == 0, result.output
        assert _raw_workflow_status(mgr.sqlite, mem.id) == "tombstoned"

    def test_cli_delete_unknown_id_exits_1(self, isolated_config: Path) -> None:
        from vesma.cli.main import app

        result = runner.invoke(app, ["delete", "no-such-id", "--yes"])
        assert result.exit_code == 1

    def test_cli_delete_already_tombstoned_reports_noop(
        self, isolated_config: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from vesma.cli.main import app

        mgr = self._manager_via_cli()
        mem = _add(mgr)
        mgr.delete_memory(mem.id, actor="owner")
        monkeypatch.setattr("sys.stdin", None)
        result = runner.invoke(app, ["delete", mem.id, "--yes"])
        assert result.exit_code == 0, result.output
        assert "already tombstoned" in result.output


# ---------------------------------------------------------------------------
# 10c. Cascade-review fixes (F1/F4/F5)
# ---------------------------------------------------------------------------


class TestWorkflowOracleClosed:
    """F1: the workflow projection is not a deletion oracle."""

    def test_workflow_get_tombstoned_answers_not_found(self, manager: MemoryManager) -> None:
        mem = _add(manager)
        _tombstone(manager, mem.id)
        assert manager.workflow_get(mem.id) is None

    def test_workflow_history_tombstoned_answers_empty(self, manager: MemoryManager) -> None:
        """The history rows carry operator actor/reason strings — they have
        no agent-reachable read path once the memory is deleted."""
        mem = _add(manager)
        _tombstone(manager, mem.id)
        assert manager.workflow_history(mem.id) == []

    def test_rest_workflow_get_tombstoned_404(self, manager: MemoryManager) -> None:
        test_app = _test_app(manager, auth_enabled=False)
        mem = _add(manager)
        _tombstone(manager, mem.id)
        with TestClient(test_app) as tc:
            resp = tc.get(f"/memories/{mem.id}/workflow")
        assert resp.status_code == 404, resp.text

    def test_rest_memory_get_tombstoned_404(self, manager: MemoryManager) -> None:
        test_app = _test_app(manager, auth_enabled=False)
        mem = _add(manager)
        _tombstone(manager, mem.id)
        with TestClient(test_app) as tc:
            resp = tc.get(f"/memories/{mem.id}")
        assert resp.status_code == 404, resp.text

    def test_workflow_set_on_tombstoned_refused(self, manager: MemoryManager) -> None:
        """Both layers refuse: the existence pre-check answers not-found
        (F1 — no oracle), and even a direct status read hits the terminal
        edge refusal. Either way the transition never lands."""
        mem = _add(manager)
        _tombstone(manager, mem.id)
        with pytest.raises(ValueError):
            manager.workflow_set(mem.id, "open", actor="someone")
        assert _raw_workflow_status(manager.sqlite, mem.id) == "tombstoned"


class TestAtomicTombstone:
    """F4: the three Stage-1 writes are one transaction; the idempotent
    branch repairs a pre-atomic gap; the doctor cross-checks."""

    def test_delete_writes_all_three_records(self, manager: MemoryManager) -> None:
        mem = _add(manager)
        result = _tombstone(manager, mem.id)
        assert _raw_workflow_status(manager.sqlite, mem.id) == "tombstoned"
        assert manager.sqlite.get_workflow_history(mem.id)
        assert manager.sqlite.journal_entry_count(mem.id) == 1
        assert result["journal_repaired"] is False

    def test_idempotent_redelete_rebuilds_missing_journal(self, manager: MemoryManager) -> None:
        """A pre-atomic tombstone (journal row lost to a crash) is REPAIRED
        on re-delete: the entry is rebuilt and the chain verifies."""
        mem = _add(manager)
        _tombstone(manager, mem.id)
        conn = manager.sqlite._get_conn()
        conn.execute("DELETE FROM deletion_journal WHERE memory_id = ?", (mem.id,))
        conn.commit()
        result = manager.delete_memory(mem.id, actor="owner")
        assert result["idempotent"] is True
        assert result["journal_repaired"] is True
        assert result["journal_id"] is not None
        assert manager.sqlite.journal_entry_count(mem.id) == 1
        assert manager.sqlite.verify_deletion_journal()["ok"] is True

    def test_idempotent_redelete_without_gap_stays_noop(self, manager: MemoryManager) -> None:
        mem = _add(manager)
        _tombstone(manager, mem.id)
        result = manager.delete_memory(mem.id, actor="owner")
        assert result["journal_repaired"] is False
        assert result["journal_id"] is None
        assert manager.sqlite.journal_entry_count(mem.id) == 1

    def test_doctor_crosscheck_unjournaled_fails(self, manager: MemoryManager) -> None:
        from vesma.cli.doctor import _check_deletion_journal

        mem = _add(manager)
        _tombstone(manager, mem.id)
        conn = manager.sqlite._get_conn()
        conn.execute("DELETE FROM deletion_journal WHERE memory_id = ?", (mem.id,))
        conn.commit()
        result = _check_deletion_journal(manager.settings)
        assert result.status.value == "fail"
        assert "WITHOUT a journal entry" in result.detail

    def test_doctor_crosscheck_purged_predecessor_informational(
        self, manager: MemoryManager
    ) -> None:
        """A journal entry whose memory row was hard-purged (doc re-ingest
        / restore classes) is NOT a failure — informational only."""
        from vesma.cli.doctor import _check_deletion_journal

        mem = _add(manager)
        _tombstone(manager, mem.id)
        conn = manager.sqlite._get_conn()
        conn.execute("DELETE FROM memories WHERE id = ?", (mem.id,))
        conn.commit()
        result = _check_deletion_journal(manager.settings)
        assert result.status.value == "pass", result.detail
        assert "purged rows" in result.detail


class TestImportCollision:
    """F5: a locally tombstoned id wins over any import."""

    def _export_entry(self, manager: MemoryManager, mem_id: str) -> dict[str, Any]:
        mem = manager.sqlite.get(mem_id, include_tombstoned=True)
        assert mem is not None
        return json.loads(mem.model_dump_json())

    def test_json_merge_collision_skips_and_keeps_deleted(self, manager: MemoryManager) -> None:
        from vesma.cli.import_ import _import_json

        mem = _add(manager, content="original deleted body")
        _tombstone(manager, mem.id)
        entry = self._export_entry(manager, mem.id)
        entry["content"] = "resurrected body from export"
        entry["workflow_status"] = None  # a non-tombstoned export row
        payload = {"format_version": 3, "memories": [entry], "projects": []}
        result = _import_json(manager, payload, mode="merge", overwrite=True, dry_run=False)
        assert result.errors == []
        assert result.imported == 0 and result.updated == 0
        assert result.skipped == 1
        assert any("locally tombstoned" in w for w in result.warnings)
        stored = manager.sqlite.get(mem.id, include_tombstoned=True)
        assert stored is not None
        assert stored.content == "original deleted body"  # NOT mutated
        assert stored.workflow_status == "tombstoned"

    def test_sqlite_merge_collision_skips_and_keeps_deleted(
        self, manager: MemoryManager, tmp_path: Path
    ) -> None:
        """The SQLite merge leg: a snapshot row colliding with a locally
        tombstoned id is skipped — the deleted row is not mutated."""
        from vesma.cli.import_ import _import_sqlite

        mem = _add(manager, content="original deleted body")
        _tombstone(manager, mem.id)
        # A snapshot from a store where the same id is alive, with new content.
        snap = tmp_path / "snap.db"
        _snapshot_db(manager.settings.db_path, snap)
        conn = sqlite3.connect(str(snap))
        conn.execute(
            "UPDATE memories SET workflow_status = NULL, content = 'snapshot body' WHERE id = ?",
            (mem.id,),
        )
        conn.commit()
        conn.close()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(snap, arcname="mnemos.db")
        result = _import_sqlite(
            manager, buf.getvalue(), mode="merge", dry_run=False, backup_dir=None
        )
        assert result.skipped >= 1
        assert any("locally tombstoned" in w for w in result.warnings)
        stored = manager.sqlite.get(mem.id, include_tombstoned=True)
        assert stored is not None
        assert stored.content == "original deleted body"
        assert stored.workflow_status == "tombstoned"


# ---------------------------------------------------------------------------
# 10. Migration on data copies without loss
# ---------------------------------------------------------------------------


class TestMigrationOnCopies:
    def test_copy_preserves_tombstoned_and_journal(
        self, manager: MemoryManager, tmp_path: Path
    ) -> None:
        """A store copy (the mover's snapshot-gate copies whole DB files)
        carries the tombstoned row and the intact journal chain."""
        mem = _add(manager)
        _tombstone(manager, mem.id)
        copy_path = tmp_path / "copy.db"
        src = sqlite3.connect(str(manager.settings.db_path))
        dst = sqlite3.connect(str(copy_path))
        src.backup(dst)
        dst.close()
        src.close()
        # Verify over the copy with a bare connection (mover posture: no
        # store instantiation, no migration side effects).
        conn = sqlite3.connect(str(copy_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT workflow_status FROM memories WHERE id = ?", (mem.id,)
        ).fetchone()
        assert row["workflow_status"] == "tombstoned"
        verdict = verify_deletion_journal_conn(conn)
        assert verdict["ok"] is True and verdict["entries"] == 1
        conn.close()

    def test_legacy_db_gains_journal_table(self, manager: MemoryManager, tmp_path: Path) -> None:
        """A pre-tombstone DB copy gains the journal table on next connect
        (the _DB_SCHEMA connect-time path) — and existing rows survive the
        schema gain untouched."""
        mem = _add(manager)
        legacy = tmp_path / "legacy.db"
        _snapshot_db(manager.settings.db_path, legacy)
        # Simulate a pre-Stage-1 database: drop the new surface.
        conn = sqlite3.connect(str(legacy))
        conn.execute("DROP TABLE deletion_journal")
        conn.commit()
        conn.close()
        # Reopen through the store: the schema script re-creates the table.
        store = SQLiteStore(legacy)
        try:
            assert store.verify_deletion_journal()["ok"] is True
            # No data loss: the pre-existing row is intact and visible.
            fetched = store.get(mem.id)
            assert fetched is not None and fetched.id == mem.id
        finally:
            store.close()
