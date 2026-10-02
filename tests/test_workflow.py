"""Tests for the workflow lifecycle (mnemos #96).

Covers four layers, mirroring ``test_tags_grouped.py`` +
``test_tags_grouped_e2e.py`` (same fixtures / conftest patterns):

  1. **State machine** (``vesmaro.workflow``) — valid paths, forbidden edges
     (blocked → done; transitions out of terminal states).
  2. **The 5 guardrails** via ``MemoryManager.workflow_set``:
       G1 audit log        — every recorded transition writes a
                             ``memory_workflow_history`` row.
       G2 stale-lock       — a lock older than the threshold is auto-
                             releasable by a different actor (no force).
       G3 idempotent       — setting ``to=X`` when already ``X`` is a
                             no-op (no write, no audit row).
       G4 force-unlock     — ``force=True`` overrides a foreign lock;
                             ``force_used=1`` in the audit; reason required.
       G5 rate limit       — >N transitions/min on one memory is blocked.
  3. **MCP dispatch** (real ``call_tool`` round-trip) — action=set/get/
     history; missing-args; unknown-action; guardrail surfacing.
  4. **REST** — nested ``GET/POST/DELETE /memories/{id}/workflow``.
  5. **Server-side enforcement** — the tool / REST layer cannot bypass the
     state machine (the manager is the single source of truth).

The ``workflow_status`` entity is intentionally distinct from
``MemoryStatus`` (raw/processing/processed/published/archived): that one
tracks the *knowledge-pipeline* stage, this one tracks the *work
lifecycle*.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vesmaro.api import main as api_main
from vesmaro.api.main import app, lifespan
from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.mcp_server import _dispatch, call_tool, list_tools
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus
from vesmaro.workflow import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    WorkflowStatus,
    WorkflowTransitionError,
    is_terminal,
    transition_allowed,
    validate_transition,
)

# ---------------------------------------------------------------------------
# Fixtures — isolated MemoryManager per test (mirrors test_tags_grouped.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings():
    """Yield a Settings object backed by a temporary directory.

    Uses a tight rate limit + short stale threshold so the guardrail tests
    can exercise the limits without waiting real time.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            mnemos={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
                "workflow_rate_limit_per_minute": 5,
                "workflow_stale_lock_threshold_hours": 24,
            },
            embedding={"provider": "onnx"},
        )
        settings.resolve_paths()
        yield settings


@pytest.fixture
def tmp_manager(tmp_settings):
    """Yield a MemoryManager with isolated storage and a mock embedder."""
    mgr = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


def _add_memory(
    mgr: MemoryManager,
    *,
    content: str = "workflow memory",
    project: str = "wf-proj",
    agent: str = "wf-agent",
) -> str:
    """Add a published memory and return its id."""
    data = MemoryCreate(
        content=content,
        tags=[f"project:{project}", f"agent:{agent}", "mnemos:decision"],
        source=MemorySource.MANUAL,
        status=MemoryStatus.PUBLISHED,
    )
    mem = mgr.add(data, project=project, agent=agent)
    return mem.id


# ---------------------------------------------------------------------------
# Layer 1 — State machine (pure functions in vesmaro.workflow)
# ---------------------------------------------------------------------------


class TestStateMachine:
    def test_valid_normal_path(self) -> None:
        """open → in-progress → done is the happy path."""
        assert transition_allowed(None, WorkflowStatus.IN_PROGRESS)
        assert transition_allowed(WorkflowStatus.OPEN, WorkflowStatus.IN_PROGRESS)
        assert transition_allowed(WorkflowStatus.IN_PROGRESS, WorkflowStatus.DONE)

    def test_valid_blocked_resolved_path(self) -> None:
        """in-progress → blocked → resolved → done is the recovery path."""
        assert transition_allowed(WorkflowStatus.IN_PROGRESS, WorkflowStatus.BLOCKED)
        assert transition_allowed(WorkflowStatus.BLOCKED, WorkflowStatus.RESOLVED)
        assert transition_allowed(WorkflowStatus.RESOLVED, WorkflowStatus.DONE)

    def test_forbidden_blocked_to_done(self) -> None:
        """blocked → done is FORBIDDEN — the headline edge an agent trips
        when it tries to skip a stuck dependency."""
        assert not transition_allowed(WorkflowStatus.BLOCKED, WorkflowStatus.DONE)
        with pytest.raises(WorkflowTransitionError, match="blocked → done"):
            validate_transition(WorkflowStatus.BLOCKED, WorkflowStatus.DONE)

    def test_forbidden_transitions_out_of_terminal(self) -> None:
        """done / withdrawn are terminal — no further transitions."""
        for terminal in TERMINAL_STATUSES:
            assert is_terminal(terminal)
            assert transition_allowed(terminal, WorkflowStatus.OPEN) is False
            with pytest.raises(WorkflowTransitionError, match="terminal state"):
                validate_transition(terminal, WorkflowStatus.IN_PROGRESS)

    def test_none_from_status_treated_as_open(self) -> None:
        """A never-set memory (None) follows the ``open`` edges."""
        # open edges: in-progress, withdrawn
        assert transition_allowed(None, WorkflowStatus.IN_PROGRESS)
        assert transition_allowed(None, WorkflowStatus.WITHDRAWN)
        # not a direct jump to done/blocked (open has no such edge)
        assert not transition_allowed(None, WorkflowStatus.DONE)
        assert not transition_allowed(None, WorkflowStatus.BLOCKED)

    def test_every_declared_edge_is_allowed(self) -> None:
        """Every edge in ALLOWED_TRANSITIONS must pass transition_allowed.

        A guard against accidental drift between the table and the
        predicate: if someone adds an edge to the dict but the predicate
        disagrees, this fails.
        """
        for src, targets in ALLOWED_TRANSITIONS.items():
            for tgt in targets:
                assert transition_allowed(src, tgt), f"{src}→{tgt} should be allowed"

    def test_withdrawn_reachable_from_every_nonterminal(self) -> None:
        """The release/cancel path (→ withdrawn) exists from every active
        state, so an operator can always end a workflow."""
        nonterminal = set(WorkflowStatus) - TERMINAL_STATUSES
        for src in nonterminal:
            assert transition_allowed(src, WorkflowStatus.WITHDRAWN), (
                f"{src}→withdrawn must be allowed (release path)"
            )

    def test_validate_transition_allows_valid_no_raise(self) -> None:
        """validate_transition returns None (no raise) for a valid edge."""
        assert validate_transition(None, WorkflowStatus.IN_PROGRESS) is None


# ---------------------------------------------------------------------------
# Layer 2 — The 5 guardrails via MemoryManager
# ---------------------------------------------------------------------------


class TestGuardrail1AuditLog:
    """G1: every recorded transition writes an immutable audit row."""

    def test_each_transition_records_history_row(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        tmp_manager.workflow_set(mid, "in-progress", actor="alice")
        tmp_manager.workflow_set(mid, "done", actor="alice")

        history = tmp_manager.workflow_history(mid)
        assert len(history) == 2
        # newest first
        assert history[0]["to_status"] == "done"
        assert history[0]["from_status"] == "in-progress"
        assert history[0]["actor"] == "alice"
        assert history[1]["to_status"] == "in-progress"
        assert history[1]["from_status"] is None  # legacy/unset → None in audit

    def test_audit_row_captures_force_and_reason(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        tmp_manager.workflow_set(mid, "in-progress", actor="alice")
        # bob force-takes the lock
        tmp_manager.workflow_set(mid, "blocked", actor="bob", force=True, reason="alice afk")

        history = tmp_manager.workflow_history(mid)
        force_row = history[0]
        assert force_row["actor"] == "bob"
        assert force_row["reason"] == "alice afk"
        assert force_row["force_used"] is True
        assert force_row["to_status"] == "blocked"


class TestGuardrail2StaleLock:
    """G2: a lock older than the threshold is auto-releasable without force."""

    def test_stale_lock_auto_released_by_other_actor(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        tmp_manager.workflow_set(mid, "in-progress", actor="alice")
        # Backdate the lock past the 24h threshold so it counts as stale.
        tmp_manager.sqlite.set_workflow_status(
            mid, "in-progress", "alice", "2020-01-01T00:00:00+00:00"
        )

        # carol takes over WITHOUT force — the stale lock auto-releases.
        result = tmp_manager.workflow_set(mid, "blocked", actor="carol")
        assert result["stale_lock_released"] is True
        assert result["force_used"] is False
        assert result["locked_by"] == "carol"  # lock transferred

    def test_fresh_lock_not_stale_blocks_other_actor(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        tmp_manager.workflow_set(mid, "in-progress", actor="alice")

        with pytest.raises(ValueError, match="locked by"):
            tmp_manager.workflow_set(mid, "blocked", actor="bob")


class TestGuardrail3Idempotent:
    """G3: setting ``to=X`` when already ``X`` is a no-op."""

    def test_idempotent_transition_writes_nothing(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        tmp_manager.workflow_set(mid, "in-progress", actor="alice")

        # Re-set the same status — no-op.
        result = tmp_manager.workflow_set(mid, "in-progress", actor="alice")
        assert result["idempotent"] is True
        assert result["recorded"] is False

        # No spurious audit row was written.
        history = tmp_manager.workflow_history(mid)
        assert len(history) == 1  # only the original in-progress transition


class TestGuardrail4ForceUnlock:
    """G4: force=True overrides a foreign lock; reason required."""

    def test_force_overrides_foreign_lock(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        tmp_manager.workflow_set(mid, "in-progress", actor="alice")

        result = tmp_manager.workflow_set(
            mid, "blocked", actor="bob", force=True, reason="alice unresponsive"
        )
        assert result["force_used"] is True
        assert result["locked_by"] == "bob"
        # audit row records force_used
        assert tmp_manager.workflow_history(mid)[0]["force_used"] is True

    def test_force_without_reason_rejected(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        tmp_manager.workflow_set(mid, "in-progress", actor="alice")

        with pytest.raises(ValueError, match="reason is required when force=True"):
            tmp_manager.workflow_set(mid, "blocked", actor="bob", force=True)

    def test_force_blank_reason_rejected(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        tmp_manager.workflow_set(mid, "in-progress", actor="alice")

        with pytest.raises(ValueError, match="reason is required when force=True"):
            tmp_manager.workflow_set(mid, "blocked", actor="bob", force=True, reason="   ")


class TestGuardrail5RateLimit:
    """G5: >N transitions/min on one memory is blocked."""

    def test_rate_limit_blocks_excess_transitions(self, tmp_manager: MemoryManager) -> None:
        # Fixture sets workflow_rate_limit_per_minute=5. Cycle a memory
        # through 5 legal transitions, then the 6th must be rejected.
        mid = _add_memory(tmp_manager)
        # 1: open→in-progress, 2: →blocked, 3: →resolved, 4: →in-progress,
        # 5: →blocked  (5 recorded; at the limit)
        path = ["in-progress", "blocked", "resolved", "in-progress", "blocked"]
        for status in path:
            tmp_manager.workflow_set(mid, status, actor="alice")

        # The 6th transition in the same minute exceeds the limit.
        with pytest.raises(ValueError, match="rate limit exceeded"):
            tmp_manager.workflow_set(mid, "resolved", actor="alice")


# ---------------------------------------------------------------------------
# Layer 2b — Input validation & memory-existence guardrails
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_unknown_status_rejected(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        with pytest.raises(ValueError, match="invalid workflow status"):
            tmp_manager.workflow_set(mid, "not-a-status", actor="alice")

    def test_missing_actor_rejected(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        with pytest.raises(ValueError, match="actor is required"):
            tmp_manager.workflow_set(mid, "in-progress", actor="")

    def test_blank_actor_rejected(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        with pytest.raises(ValueError, match="actor is required"):
            tmp_manager.workflow_set(mid, "in-progress", actor="   ")

    def test_missing_memory_rejected(self, tmp_manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="not found"):
            tmp_manager.workflow_set("nonexistent-id", "in-progress", actor="alice")

    def test_concurrent_delete_raises_valueerror_not_assertion(
        self, tmp_manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A delete racing between the existence check and the status read
        must surface as ValueError (404/409), not AssertionError (500) or —
        under ``python -O`` — a TypeError. Closes the TOCTOU contract gap
        (Code Review #96 finding #1)."""
        mid = _add_memory(tmp_manager)
        # Simulate the race: get() sees the memory, get_workflow_status() does not.
        monkeypatch.setattr(tmp_manager.sqlite, "get_workflow_status", lambda _mid: None)
        with pytest.raises(ValueError, match="not found"):
            tmp_manager.workflow_set(mid, "in-progress", actor="alice")


class TestWorkflowGet:
    def test_get_normalises_unset_status_to_open(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        result = tmp_manager.workflow_get(mid)
        assert result is not None
        assert result["workflow_status"] == "open"  # never-set → open
        assert result["locked_by"] is None

    def test_get_missing_memory_returns_none(self, tmp_manager: MemoryManager) -> None:
        assert tmp_manager.workflow_get("no-such-id") is None

    def test_get_returns_current_projection(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        tmp_manager.workflow_set(mid, "in-progress", actor="alice")
        result = tmp_manager.workflow_get(mid)
        assert result["workflow_status"] == "in-progress"
        assert result["locked_by"] == "alice"

    def test_history_empty_for_memory_with_no_transitions(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        assert tmp_manager.workflow_history(mid) == []


# ---------------------------------------------------------------------------
# Layer 3 — MCP dispatch (real call_tool round-trip)
# ---------------------------------------------------------------------------


def _strip_checkpoint_reminder(text: str) -> str:
    """Strip a checkpoint reminder appended after the JSON payload.

    ``mcp_server._checkpoint_reminder()`` may append a nudge like
    ``\n\n⚠️ [vesma] N tool calls since last checkpoint … Consider
    calling vesma_save_context …`` after the tool's JSON response. It is
    informational metadata for MCP clients, NOT part of the tool's return
    value — so a correct client must ignore it before parsing.

    The module-global ``_checkpoint_tracker`` accumulates
    ``calls_since_save`` across the whole test session, so the reminder can
    fire mid-suite even when this module's own call count is low. Without
    stripping, ``json.loads`` raises ``JSONDecodeError`` (extra data) on
    ``"{…json…}\n\n⚠️ …"`` and the helper falls through to returning the
    raw string, breaking downstream ``result["memory_id"]`` /
    ``result["history"]`` assertions with ``TypeError: string indices
    must be integers``.

    Mirrors the same fix in ``test_workflow_e2e.py`` and
    ``test_tags_grouped_e2e.py`` — one theme: dispatch helpers must ignore
    the checkpoint reminder appended by ``mcp_server``.
    """
    marker = "\n\n⚠️ [vesma]"
    idx = text.find(marker)
    return text[:idx] if idx != -1 else text


async def _call_tool_real(real_manager: MemoryManager, name: str, args: dict):
    """Invoke the real MCP ``call_tool`` handler with an isolated manager."""
    with patch("vesmaro.mcp_server.get_manager", return_value=real_manager):
        contents = await call_tool(name, args)
    assert len(contents) == 1, f"expected exactly one TextContent, got {len(contents)}"
    text = _strip_checkpoint_reminder(contents[0].text)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


async def _dispatch_real(real_manager: MemoryManager, name: str, args: dict):
    """Invoke the real ``_dispatch`` directly (raw dict return for errors)."""
    with patch("vesmaro.mcp_server.get_manager", return_value=real_manager):
        return await _dispatch(name, args)


class TestMcpRegistration:
    async def test_workflow_tool_registered(self) -> None:
        """list_tools() advertises vesma_workflow with the action enum."""
        tools = await list_tools()
        names = {t.name for t in tools}
        assert "vesma_workflow" in names
        tool = next(t for t in tools if t.name == "vesma_workflow")
        action_schema = tool.input_schema["properties"]["action"]
        assert action_schema["enum"] == ["set", "get", "history"]
        assert tool.input_schema["required"] == ["action", "memory_id"]


class TestMcpDispatch:
    async def test_action_set_transitions_and_records(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        result = await _call_tool_real(
            tmp_manager,
            "vesma_workflow",
            {"action": "set", "memory_id": mid, "to": "in-progress", "actor": "alice"},
        )
        assert result["to_status"] == "in-progress"
        assert result["recorded"] is True
        # state changed server-side
        assert tmp_manager.workflow_get(mid)["workflow_status"] == "in-progress"

    async def test_action_get_returns_status(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        result = await _call_tool_real(
            tmp_manager, "vesma_workflow", {"action": "get", "memory_id": mid}
        )
        assert result["workflow_status"] == "open"

    async def test_action_get_missing_memory_error(self, tmp_manager: MemoryManager) -> None:
        result = await _dispatch_real(
            tmp_manager,
            "vesma_workflow",
            {"action": "get", "memory_id": "no-such-id"},
        )
        assert "error" in result
        assert "not found" in result["error"]

    async def test_action_history_returns_trail(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        tmp_manager.workflow_set(mid, "in-progress", actor="alice")
        result = await _call_tool_real(
            tmp_manager, "vesma_workflow", {"action": "history", "memory_id": mid}
        )
        assert result["memory_id"] == mid
        assert len(result["history"]) == 1
        assert result["history"][0]["to_status"] == "in-progress"

    async def test_set_missing_to_error(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        result = await _dispatch_real(
            tmp_manager,
            "vesma_workflow",
            {"action": "set", "memory_id": mid, "actor": "alice"},
        )
        assert "error" in result
        assert "to" in result["error"]

    async def test_set_missing_actor_error(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        result = await _dispatch_real(
            tmp_manager,
            "vesma_workflow",
            {"action": "set", "memory_id": mid, "to": "in-progress"},
        )
        assert "error" in result
        assert "actor" in result["error"]

    async def test_missing_memory_id_error(self, tmp_manager: MemoryManager) -> None:
        result = await _dispatch_real(tmp_manager, "vesma_workflow", {"action": "get"})
        assert "error" in result
        assert "memory_id" in result["error"]

    async def test_unknown_action_error(self, tmp_manager: MemoryManager) -> None:
        mid = _add_memory(tmp_manager)
        result = await _dispatch_real(
            tmp_manager,
            "vesma_workflow",
            {"action": "bogus", "memory_id": mid},
        )
        assert "error" in result
        assert "unknown action" in result["error"]

    async def test_guardrail_violation_surfaced_as_error(self, tmp_manager: MemoryManager) -> None:
        """A forbidden transition surfaces as a clean error dict, not a crash."""
        mid = _add_memory(tmp_manager)
        # Reach 'blocked' via the valid path (open → in-progress → blocked);
        # open → blocked is itself forbidden, so the in-progress step is required.
        tmp_manager.workflow_set(mid, "in-progress", actor="alice")
        tmp_manager.workflow_set(mid, "blocked", actor="alice")
        result = await _dispatch_real(
            tmp_manager,
            "vesma_workflow",
            {"action": "set", "memory_id": mid, "to": "done", "actor": "alice"},
        )
        assert "error" in result
        assert "blocked → done" in result["error"]


# ---------------------------------------------------------------------------
# Layer 4 — REST (nested /memories/{id}/workflow)
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_settings):
    """Yield a TestClient with an isolated MemoryManager (mirrors test_api.py)."""
    mgr = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder

    test_app = FastAPI(title="Mnemos-Test", version="0.1.0", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)
    api_main._manager = mgr
    with TestClient(test_app) as tc:
        yield tc
    mgr.close()
    api_main._manager = None


def _create_memory(client: TestClient) -> str:
    """Create a published memory via the REST API and return its id."""
    resp = client.post(
        "/memories",
        json={
            "content": "rest workflow memory",
            "tags": ["project:wf", "agent:wf-agent", "mnemos:decision"],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestRestWorkflow:
    def test_get_workflow_defaults_to_open(self, client: TestClient) -> None:
        mid = _create_memory(client)
        resp = client.get(f"/memories/{mid}/workflow")
        assert resp.status_code == 200
        data = resp.json()
        assert data["workflow_status"] == "open"
        assert data["locked_by"] is None

    def test_get_workflow_404_for_missing_memory(self, client: TestClient) -> None:
        resp = client.get("/memories/no-such-id/workflow")
        assert resp.status_code == 404

    def test_post_workflow_transitions(self, client: TestClient) -> None:
        mid = _create_memory(client)
        resp = client.post(
            f"/memories/{mid}/workflow",
            json={"to": "in-progress", "actor": "alice"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["to_status"] == "in-progress"
        assert data["recorded"] is True
        assert data["locked_by"] == "alice"

        # state persisted
        get_resp = client.get(f"/memories/{mid}/workflow")
        assert get_resp.json()["workflow_status"] == "in-progress"

    def test_post_workflow_forbidden_transition_409(self, client: TestClient) -> None:
        mid = _create_memory(client)
        # Reach 'blocked' via the valid path (open → in-progress → blocked).
        client.post(
            f"/memories/{mid}/workflow",
            json={"to": "in-progress", "actor": "alice"},
        )
        client.post(
            f"/memories/{mid}/workflow",
            json={"to": "blocked", "actor": "alice"},
        )
        # blocked → done is forbidden → 409 conflict
        resp = client.post(
            f"/memories/{mid}/workflow",
            json={"to": "done", "actor": "alice"},
        )
        assert resp.status_code == 409
        assert "blocked → done" in resp.json()["detail"]

    def test_post_workflow_force_requires_reason_409(self, client: TestClient) -> None:
        mid = _create_memory(client)
        client.post(
            f"/memories/{mid}/workflow",
            json={"to": "in-progress", "actor": "alice"},
        )
        resp = client.post(
            f"/memories/{mid}/workflow",
            json={"to": "blocked", "actor": "bob", "force": True},
        )
        assert resp.status_code == 409
        assert "reason is required" in resp.json()["detail"]

    def test_delete_workflow_releases_lock_to_withdrawn(self, client: TestClient) -> None:
        mid = _create_memory(client)
        client.post(
            f"/memories/{mid}/workflow",
            json={"to": "in-progress", "actor": "alice"},
        )
        # DELETE releases the lock by transitioning to withdrawn (terminal).
        resp = client.delete(f"/memories/{mid}/workflow", params={"actor": "alice"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["to_status"] == "withdrawn"
        assert data["terminal"] is True
        assert data["locked_by"] is None  # lock released on terminal

    def test_delete_requires_actor_422(self, client: TestClient) -> None:
        mid = _create_memory(client)
        resp = client.delete(f"/memories/{mid}/workflow")
        assert resp.status_code == 422

    def test_delete_force_overrides_foreign_lock(self, client: TestClient) -> None:
        mid = _create_memory(client)
        client.post(
            f"/memories/{mid}/workflow",
            json={"to": "in-progress", "actor": "alice"},
        )
        # bob force-releases alice's lock via DELETE.
        resp = client.delete(
            f"/memories/{mid}/workflow",
            params={"actor": "bob", "force": "true", "reason": "alice afk"},
        )
        assert resp.status_code == 200
        assert resp.json()["force_used"] is True

    def test_transition_out_of_terminal_state_409(self, client: TestClient) -> None:
        """A POST transitioning OUT of a terminal state is forbidden → 409.

        Once a memory reaches ``withdrawn`` (e.g. via DELETE), no further
        transition is permitted. Note: a second DELETE is idempotent
        (withdrawn → withdrawn is a no-op → 200), tested below; the 409 path
        is an attempt to move to a DIFFERENT state.
        """
        mid = _create_memory(client)
        client.delete(f"/memories/{mid}/workflow", params={"actor": "alice"})
        # POST to a different status from a terminal state → 409.
        resp = client.post(
            f"/memories/{mid}/workflow",
            json={"to": "in-progress", "actor": "alice"},
        )
        assert resp.status_code == 409
        assert "terminal state" in resp.json()["detail"]

    def test_delete_on_terminal_state_is_idempotent_200(self, client: TestClient) -> None:
        """A second DELETE (withdrawn → withdrawn) is an idempotent no-op, not
        an error. Guardrail 3 applies: same-status transition is a no-op."""
        mid = _create_memory(client)
        client.delete(f"/memories/{mid}/workflow", params={"actor": "alice"})
        resp = client.delete(f"/memories/{mid}/workflow", params={"actor": "alice"})
        assert resp.status_code == 200
        assert resp.json()["idempotent"] is True
        assert resp.json()["recorded"] is False


# ---------------------------------------------------------------------------
# Layer 5 — Server-side enforcement (manager is the single source of truth)
# ---------------------------------------------------------------------------


class TestServerSideEnforcement:
    """The state machine cannot be bypassed via the tool / REST layer.

    ``workflow_set`` is the ONLY writer of ``workflow_status`` (and the
    audit log). Neither the MCP tool nor the REST endpoint re-implements
    the state machine — they are thin wrappers — so a forbidden edge is
    rejected regardless of the entry point. This is the test the user
    explicitly asked for: proving the manager is the single source of truth.
    """

    def test_blocked_to_done_rejected_at_mcp_layer(self, tmp_manager: MemoryManager) -> None:
        """The MCP tool surfaces the forbidden edge; the state is unchanged."""
        mid = _add_memory(tmp_manager)
        tmp_manager.workflow_set(mid, "in-progress", actor="alice")
        tmp_manager.workflow_set(mid, "blocked", actor="alice")

        result = asyncio.run(
            _dispatch_real(
                tmp_manager,
                "vesma_workflow",
                {"action": "set", "memory_id": mid, "to": "done", "actor": "alice"},
            )
        )
        assert "error" in result
        # state stayed blocked — no bypass
        assert tmp_manager.workflow_get(mid)["workflow_status"] == "blocked"
        # no audit row written for the rejected attempt
        assert len(tmp_manager.workflow_history(mid)) == 2

    def test_blocked_to_done_rejected_at_rest_layer(self, client: TestClient) -> None:
        """The REST endpoint surfaces 409; the state is unchanged."""
        mid = _create_memory(client)
        # Reach 'blocked' via the valid path (open → in-progress → blocked);
        # open → blocked is itself forbidden, so the in-progress step is required.
        client.post(f"/memories/{mid}/workflow", json={"to": "in-progress", "actor": "alice"})
        client.post(f"/memories/{mid}/workflow", json={"to": "blocked", "actor": "alice"})
        resp = client.post(f"/memories/{mid}/workflow", json={"to": "done", "actor": "alice"})
        assert resp.status_code == 409
        # state stayed blocked
        get = client.get(f"/memories/{mid}/workflow")
        assert get.json()["workflow_status"] == "blocked"

    def test_generic_field_update_cannot_touch_workflow_status(
        self, tmp_manager: MemoryManager
    ) -> None:
        """update_fields / save are NOT wired to workflow_status — the column
        is managed exclusively by set_workflow_status. A direct column write
        would require going through the manager's guarded path."""
        mid = _add_memory(tmp_manager)
        # The public update_fields path does not list workflow_status as an
        # updatable field, so the only way to change it is workflow_set.
        # Verify the column is absent from the field-updater vocabulary:
        from vesmaro.storage.sqlite_store import SQLiteStore

        updatable = getattr(SQLiteStore, "_FIELD_UPDATERS", {})
        assert "workflow_status" not in updatable, (
            "workflow_status must NOT be in _FIELD_UPDATERS — that would let a "
            "generic field update bypass the state machine (#96 invariant)."
        )
        # And the memory still reads as open (no backdoor write happened).
        assert tmp_manager.workflow_get(mid)["workflow_status"] == "open"
