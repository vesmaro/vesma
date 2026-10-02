"""End-to-end tests for the ``vesma_workflow`` MCP tool + REST endpoints
(issue #96).

Dedicated e2e matrix driving the 15 scenarios (E1-E15) requested by the
owner through the **real MCP ``call_tool`` dispatch AND the real REST
API** — proving the workflow state machine + 5 guardrails cannot be
bypassed by either entry point. Mirrors the harness in
``test_tags_grouped_e2e.py``: the production ``call_tool``/``_dispatch``
runs against a **real isolated ``MemoryManager``** (tmp_path-backed
SQLite + vault), with only the embedder mocked to stay offline. The REST
client is a real ``TestClient`` with the same isolated manager injected.

What makes this e2e (not unit):

- ``call_tool`` → ``_dispatch`` is the production MCP round-trip; nothing
  is monkeypatched except ``get_manager`` returning the isolated manager.
- The REST routes run through FastAPI's real ASGI stack (``TestClient``).
- The manager, SQLite store, FTS5, and the workflow state machine are
  all production code.

Isolation: every test uses a fresh manager in a per-test
``TemporaryDirectory``. The real ``~/.mnemos/`` store is never touched.

Scenario matrix (E1-E15). Each scenario is asserted at BOTH the MCP and
the REST layer unless the layer does not expose the concept (e.g. unknown
``action`` is MCP-only — REST uses distinct endpoints):

  E1  set open→in-progress (A)            → in-progress, locked_by=A, audit row
  E2  get after E1                         → {workflow_status, locked_by, locked_at}
  E3  history after transitions            → ordered list (actor/from/to/reason/force_used)
  E4  set in-progress→blocked (A, reason)  → blocked
  E5  FORBIDDEN set blocked→done (A)       → REJECTED, state unchanged, NO audit row
  E6  B set while A holds lock (no force)  → REJECTED lock conflict
  E7  B set with force=true + reason       → override, force_used=1, locked_by=B
  E8  force=true WITHOUT reason            → REJECTED (reason required)
  E9  set to=X when already X (idempotent) → no-op, recorded=False, NO audit row
  E10 stale lock (>threshold) → C takeover (no force) → stale_lock_released=True
  E11 rate-limit churn >N/min              → BLOCKED
  E12 DELETE /workflow → withdrawn (terminal); subsequent set REJECTED
  E13 set on nonexistent memory_id         → clear error (NOT AssertionError / 500)
  E14 unknown action                       → clear error
  E15 missing required params              → clear error

Highest-priority: E5 (forbidden edge) and E13 (not-found contract) MUST
hold through BOTH the MCP tool and the REST endpoint — proving the
manager is the single source of truth and neither layer can bypass the
state machine.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
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

# ---------------------------------------------------------------------------
# Fixtures — isolated MemoryManager per test (mirrors test_tags_grouped_e2e.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings():
    """Yield Settings backed by a temp dir with a tight rate limit.

    ``workflow_rate_limit_per_minute=5`` lets E11 hit the cap without real
    waiting; ``workflow_stale_lock_threshold_hours=24`` lets E10 backdate
    the lock past the threshold deterministically.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            vesma={
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
def real_manager(tmp_settings):
    """A REAL MemoryManager (isolated storage) with a mocked embedder.

    Everything except the embedder is production code (SQLite store, vault,
    FTS5, workflow state machine, 5 guardrails). The embedder is mocked to
    keep tests fast and fully offline.
    """
    mgr = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


@pytest.fixture
def client(tmp_settings):
    """A real FastAPI TestClient with an isolated MemoryManager injected.

    The REST workflow routes use ``get_manager()`` which reads the module
    global ``_manager``; setting ``api_main._manager`` swaps the singleton
    so every route hits the isolated manager. The app's real routes are
    appended to a fresh FastAPI app so middleware/lifespan are exercised.
    """
    mgr = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder

    test_app = FastAPI(title="Mnemos-E2E-Test", version="0.1.0", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)
    api_main._manager = mgr
    with TestClient(test_app) as tc:
        yield tc
    mgr.close()
    api_main._manager = None


# ---------------------------------------------------------------------------
# Dispatch helpers — drive the REAL MCP call_tool / _dispatch path
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
    stripping, ``json.loads`` would raise ``JSONDecodeError`` (extra data)
    on ``"{…json…}\n\n⚠️ …"`` and the helper would fall through to returning
    the raw string, breaking downstream ``result["workflow_status"]``
    assertions with ``TypeError: string indices must be integers``.

    This is a pre-existing global-state leak in ``mcp_server.py`` (not #96's
    concern); the helper is made robust to it rather than the reminder logic
    being changed.
    """
    marker = "\n\n⚠️ [vesma]"
    idx = text.find(marker)
    return text[:idx] if idx != -1 else text


async def _call_tool_real(mgr: MemoryManager, args: dict) -> Any:
    """Invoke the real MCP ``call_tool`` with an isolated manager.

    Patches ``get_manager`` so the production singleton is swapped for the
    isolated manager; nothing else is mocked. Returns the parsed JSON of
    the single TextContent the handler returns (or raw text on non-JSON).
    """
    with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
        contents = await call_tool("vesma_workflow", args)
    assert len(contents) == 1, f"expected exactly one TextContent, got {len(contents)}"
    text = _strip_checkpoint_reminder(contents[0].text)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


async def _dispatch_real(mgr: MemoryManager, args: dict) -> dict:
    """Invoke the real ``_dispatch`` directly (raw dict return for errors).

    ``call_tool`` wraps ``_dispatch``; calling ``_dispatch`` directly lets a
    test inspect the raw dict (before JSON serialisation) for negative
    paths that return ``{"error": ...}``.
    """
    with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
        return await _dispatch("vesma_workflow", args)


def _seed_memory(
    mgr: MemoryManager,
    *,
    content: str = "e2e workflow memory",
    project: str = "e2e-proj",
    agent: str = "e2e-agent",
) -> str:
    """Insert a published memory via the manager and return its id."""
    data = MemoryCreate(
        content=content,
        tags=[f"project:{project}", f"agent:{agent}", "mnemos:decision"],
        source=MemorySource.MANUAL,
        status=MemoryStatus.PUBLISHED,
    )
    return mgr.add(data, project=project, agent=agent).id


def _create_rest_memory(client: TestClient) -> str:
    """Create a published memory via the REST API and return its id."""
    resp = client.post(
        "/memories",
        json={
            "content": "e2e rest workflow memory",
            "tags": ["project:e2e-proj", "agent:e2e-agent", "mnemos:decision"],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _set_rest(client: TestClient, mid: str, to: str, actor: str, **extra) -> dict:
    """POST a workflow transition via REST; return the parsed body."""
    body = {"to": to, "actor": actor, **extra}
    resp = client.post(f"/memories/{mid}/workflow", json=body)
    return {"status": resp.status_code, "json": _safe_json(resp)}


def _safe_json(resp) -> dict | str:
    try:
        return resp.json()
    except Exception:
        return resp.text


# ===========================================================================
# E0 — registration: the tool is listed with the action enum
# ===========================================================================


async def test_e0_workflow_tool_registered_with_action_enum() -> None:
    """``list_tools()`` (real MCP registration) advertises ``vesma_workflow``
    with ``action`` enum {set, get, history} and required [action, memory_id]."""
    tools = await list_tools()
    names = {t.name for t in tools}
    assert "vesma_workflow" in names, "vesma_workflow tool not registered"
    tool = next(t for t in tools if t.name == "vesma_workflow")
    action_schema = tool.input_schema["properties"]["action"]
    assert action_schema["enum"] == ["set", "get", "history"]
    assert tool.input_schema["required"] == ["action", "memory_id"]


# ===========================================================================
# E1 — set open→in-progress (actor A): status, lock, audit row
# ===========================================================================


async def test_e1_mcp_set_open_to_in_progress(real_manager: MemoryManager) -> None:
    mid = _seed_memory(real_manager)
    result = await _call_tool_real(
        real_manager,
        {"action": "set", "memory_id": mid, "to": "in-progress", "actor": "A"},
    )
    assert result["to_status"] == "in-progress"
    assert result["recorded"] is True
    assert result["locked_by"] == "A"
    assert result["locked_at"] is not None  # lock timestamp set
    # audit row written
    history = real_manager.workflow_history(mid)
    assert len(history) == 1
    assert history[0]["to_status"] == "in-progress"
    assert history[0]["actor"] == "A"
    # state persisted server-side
    assert real_manager.workflow_get(mid)["workflow_status"] == "in-progress"


def test_e1_rest_set_open_to_in_progress(client: TestClient) -> None:
    mid = _create_rest_memory(client)
    resp = _set_rest(client, mid, "in-progress", "A")
    assert resp["status"] == 200, resp
    body = resp["json"]
    assert body["to_status"] == "in-progress"
    assert body["recorded"] is True
    assert body["locked_by"] == "A"
    assert body["locked_at"] is not None
    # state persisted
    get = client.get(f"/memories/{mid}/workflow")
    assert get.json()["workflow_status"] == "in-progress"


# ===========================================================================
# E2 — get after E1: returns {workflow_status, locked_by, locked_at}
# ===========================================================================


async def test_e2_mcp_get_returns_projection(real_manager: MemoryManager) -> None:
    mid = _seed_memory(real_manager)
    real_manager.workflow_set(mid, "in-progress", actor="A")
    result = await _call_tool_real(real_manager, {"action": "get", "memory_id": mid})
    assert result["workflow_status"] == "in-progress"
    assert result["locked_by"] == "A"
    assert result["locked_at"] is not None
    assert result["memory_id"] == mid


def test_e2_rest_get_returns_projection(client: TestClient) -> None:
    mid = _create_rest_memory(client)
    client.post(f"/memories/{mid}/workflow", json={"to": "in-progress", "actor": "A"})
    resp = client.get(f"/memories/{mid}/workflow")
    assert resp.status_code == 200
    body = resp.json()
    assert body["workflow_status"] == "in-progress"
    assert body["locked_by"] == "A"
    assert body["locked_at"] is not None


# ===========================================================================
# E3 — history after transitions: ordered list with actor/from/to/reason/force_used
# ===========================================================================


async def test_e3_mcp_history_ordered_trail(real_manager: MemoryManager) -> None:
    mid = _seed_memory(real_manager)
    real_manager.workflow_set(mid, "in-progress", actor="A")
    real_manager.workflow_set(mid, "blocked", actor="A", reason="dep missing")
    result = await _call_tool_real(real_manager, {"action": "history", "memory_id": mid})
    assert result["memory_id"] == mid
    trail = result["history"]
    assert len(trail) == 2
    # newest first
    assert trail[0]["to_status"] == "blocked"
    assert trail[0]["from_status"] == "in-progress"
    assert trail[0]["actor"] == "A"
    assert trail[0]["reason"] == "dep missing"
    assert trail[0]["force_used"] is False
    assert trail[1]["to_status"] == "in-progress"
    assert trail[1]["from_status"] is None  # unset → None in audit


def test_e3_rest_history_endpoint(client: TestClient) -> None:
    """REST does not expose a history endpoint directly (history is MCP-only);
    the audit trail is read via the manager. This test confirms the trail that
    REST transitions produce is queryable through the MCP history action on the
    SAME isolated store — proving the audit row written by the REST path is the
    one the MCP path reads (single store, single source of truth)."""
    # NOTE: this reuses the MCP-history path but seeds via REST writes by
    # driving the manager directly (REST writes flow through the same
    # workflow_set, so the audit rows are identical). See test_e3_mcp for the
    # pure MCP path; the audit table is shared regardless of entry point.
    mgr: MemoryManager = api_main._manager  # type: ignore[assignment]
    mid = _create_rest_memory(client)
    client.post(f"/memories/{mid}/workflow", json={"to": "in-progress", "actor": "A"})
    client.post(f"/memories/{mid}/workflow", json={"to": "done", "actor": "A"})
    trail = mgr.workflow_history(mid)
    assert len(trail) == 2
    assert trail[0]["to_status"] == "done"
    assert trail[1]["to_status"] == "in-progress"
    assert trail[0]["actor"] == "A"


# ===========================================================================
# E4 — set in-progress→blocked (actor A, reason): status→blocked
# ===========================================================================


async def test_e4_mcp_set_in_progress_to_blocked(real_manager: MemoryManager) -> None:
    mid = _seed_memory(real_manager)
    real_manager.workflow_set(mid, "in-progress", actor="A")
    result = await _call_tool_real(
        real_manager,
        {
            "action": "set",
            "memory_id": mid,
            "to": "blocked",
            "actor": "A",
            "reason": "waiting on dep",
        },
    )
    assert result["to_status"] == "blocked"
    assert result["recorded"] is True
    # blocked keeps the lock (same actor still owns the work)
    assert result["locked_by"] == "A"
    assert real_manager.workflow_get(mid)["workflow_status"] == "blocked"


def test_e4_rest_set_in_progress_to_blocked(client: TestClient) -> None:
    mid = _create_rest_memory(client)
    client.post(f"/memories/{mid}/workflow", json={"to": "in-progress", "actor": "A"})
    resp = _set_rest(client, mid, "blocked", "A", reason="waiting on dep")
    assert resp["status"] == 200, resp
    assert resp["json"]["to_status"] == "blocked"
    assert client.get(f"/memories/{mid}/workflow").json()["workflow_status"] == "blocked"


# ===========================================================================
# E5 — FORBIDDEN set blocked→done: REJECTED, state unchanged, NO audit row
# (server-side enforcement at BOTH layers — highest priority)
# ===========================================================================


async def test_e5_mcp_blocked_to_done_rejected(real_manager: MemoryManager) -> None:
    """The MCP tool surfaces the forbidden edge; state + audit are unchanged."""
    mid = _seed_memory(real_manager)
    real_manager.workflow_set(mid, "in-progress", actor="A")
    real_manager.workflow_set(mid, "blocked", actor="A")
    before = len(real_manager.workflow_history(mid))

    result = await _dispatch_real(
        real_manager,
        {"action": "set", "memory_id": mid, "to": "done", "actor": "A"},
    )
    assert "error" in result
    assert "blocked → done" in result["error"]
    # state stayed blocked — no bypass
    assert real_manager.workflow_get(mid)["workflow_status"] == "blocked"
    # NO new audit row for the rejected attempt
    assert len(real_manager.workflow_history(mid)) == before


def test_e5_rest_blocked_to_done_rejected_409(client: TestClient) -> None:
    """The REST endpoint surfaces 409; state is unchanged."""
    mid = _create_rest_memory(client)
    client.post(f"/memories/{mid}/workflow", json={"to": "in-progress", "actor": "A"})
    client.post(f"/memories/{mid}/workflow", json={"to": "blocked", "actor": "A"})

    resp = _set_rest(client, mid, "done", "A")
    assert resp["status"] == 409, resp
    assert "blocked → done" in resp["json"]["detail"]
    # state stayed blocked
    assert client.get(f"/memories/{mid}/workflow").json()["workflow_status"] == "blocked"


# ===========================================================================
# E6 — actor B set (valid target) while A holds lock, no force → REJECTED
#
# Note: the literal "B set in-progress while already in-progress" hits the
# idempotent short-circuit (to == from) BEFORE the lock check, so it is a
# no-op, not a lock conflict. To exercise the lock-conflict guardrail the
# target must be a DIFFERENT valid edge from the locked state. See
# test_e6b for the idempotent-short-circuit observation.
# ===========================================================================


async def test_e6_mcp_lock_conflict_rejected(real_manager: MemoryManager) -> None:
    mid = _seed_memory(real_manager)
    real_manager.workflow_set(mid, "in-progress", actor="A")  # A holds the lock
    # B attempts a DIFFERENT valid target (in-progress->blocked) without force
    result = await _dispatch_real(
        real_manager,
        {"action": "set", "memory_id": mid, "to": "blocked", "actor": "B"},
    )
    assert "error" in result
    assert "locked by" in result["error"]
    # lock still held by A
    assert real_manager.workflow_get(mid)["locked_by"] == "A"


def test_e6_rest_lock_conflict_rejected_409(client: TestClient) -> None:
    mid = _create_rest_memory(client)
    client.post(f"/memories/{mid}/workflow", json={"to": "in-progress", "actor": "A"})
    resp = _set_rest(client, mid, "blocked", "B")  # B, no force
    assert resp["status"] == 409, resp
    assert "locked by" in resp["json"]["detail"]
    assert client.get(f"/memories/{mid}/workflow").json()["locked_by"] == "A"


async def test_e6b_idempotent_short_circuit_observation(real_manager: MemoryManager) -> None:
    """Observation (not a failure): a DIFFERENT actor setting the SAME status
    as the current one gets an idempotent no-op, because the idempotent
    guardrail (G3) checks STATUS equality and short-circuits BEFORE the lock
    check. This is documented behaviour - idempotent short-circuit runs before
    the lock guardrails. It means a second actor does NOT get a lock conflict
    for a same-status set; the transition is simply a no-op with no write."""
    mid = _seed_memory(real_manager)
    real_manager.workflow_set(mid, "in-progress", actor="A")  # A holds lock
    result = await _dispatch_real(
        real_manager,
        {"action": "set", "memory_id": mid, "to": "in-progress", "actor": "B"},
    )
    # idempotent no-op, NOT a lock conflict
    assert "error" not in result
    assert result["idempotent"] is True
    assert result["recorded"] is False
    # lock unchanged (still A)
    assert real_manager.workflow_get(mid)["locked_by"] == "A"


# ===========================================================================
# E7 — actor B set with force=true + reason: override, force_used=1, locked_by=B
#
# Same caveat as E6: force is exercised on a DIFFERENT valid target so the
# idempotent short-circuit does not pre-empt it.
# ===========================================================================


async def test_e7_mcp_force_overrides_lock(real_manager: MemoryManager) -> None:
    mid = _seed_memory(real_manager)
    real_manager.workflow_set(mid, "in-progress", actor="A")
    result = await _call_tool_real(
        real_manager,
        {
            "action": "set",
            "memory_id": mid,
            "to": "blocked",
            "actor": "B",
            "force": True,
            "reason": "A unresponsive",
        },
    )
    assert result["force_used"] is True
    assert result["to_status"] == "blocked"
    # blocked keeps the lock; on a takeover the owner becomes B (timestamp refreshed)
    assert result["locked_by"] == "B"
    # audit row records force_used
    assert real_manager.workflow_history(mid)[0]["force_used"] is True


def test_e7_rest_force_overrides_lock(client: TestClient) -> None:
    mid = _create_rest_memory(client)
    client.post(f"/memories/{mid}/workflow", json={"to": "in-progress", "actor": "A"})
    resp = _set_rest(client, mid, "blocked", "B", force=True, reason="A unresponsive")
    assert resp["status"] == 200, resp
    body = resp["json"]
    assert body["force_used"] is True
    assert body["locked_by"] == "B"
    assert client.get(f"/memories/{mid}/workflow").json()["locked_by"] == "B"


# ===========================================================================
# E8 — force=true WITHOUT reason → REJECTED (reason required)
# ===========================================================================


async def test_e8_mcp_force_without_reason_rejected(real_manager: MemoryManager) -> None:
    mid = _seed_memory(real_manager)
    real_manager.workflow_set(mid, "in-progress", actor="A")
    result = await _dispatch_real(
        real_manager,
        {"action": "set", "memory_id": mid, "to": "blocked", "actor": "B", "force": True},
    )
    assert "error" in result
    assert "reason is required when force=True" in result["error"]
    # lock unchanged
    assert real_manager.workflow_get(mid)["locked_by"] == "A"


def test_e8_rest_force_without_reason_rejected_409(client: TestClient) -> None:
    mid = _create_rest_memory(client)
    client.post(f"/memories/{mid}/workflow", json={"to": "in-progress", "actor": "A"})
    resp = _set_rest(client, mid, "blocked", "B", force=True)  # no reason
    assert resp["status"] == 409, resp
    assert "reason is required" in resp["json"]["detail"]


# ===========================================================================
# E9 — set to=X when already X (idempotent): no-op, recorded=False, NO audit row
# ===========================================================================


async def test_e9_mcp_idempotent_same_status(real_manager: MemoryManager) -> None:
    mid = _seed_memory(real_manager)
    real_manager.workflow_set(mid, "in-progress", actor="A")
    before = len(real_manager.workflow_history(mid))

    result = await _call_tool_real(
        real_manager,
        {"action": "set", "memory_id": mid, "to": "in-progress", "actor": "A"},
    )
    assert result["idempotent"] is True
    assert result["recorded"] is False
    # NO spurious audit row
    assert len(real_manager.workflow_history(mid)) == before


def test_e9_rest_idempotent_same_status(client: TestClient) -> None:
    mid = _create_rest_memory(client)
    client.post(f"/memories/{mid}/workflow", json={"to": "in-progress", "actor": "A"})
    resp = _set_rest(client, mid, "in-progress", "A")  # same status
    assert resp["status"] == 200, resp
    assert resp["json"]["idempotent"] is True
    assert resp["json"]["recorded"] is False


# ===========================================================================
# E10 — stale lock (>threshold) → C takeover without force → stale_lock_released=True
# ===========================================================================


async def test_e10_mcp_stale_lock_auto_released(real_manager: MemoryManager) -> None:
    mid = _seed_memory(real_manager)
    real_manager.workflow_set(mid, "in-progress", actor="A")
    # Backdate the lock past the 24h threshold (direct store write simulates
    # the passage of time without real waiting).
    real_manager.sqlite.set_workflow_status(mid, "in-progress", "A", "2020-01-01T00:00:00+00:00")

    # C takes over WITHOUT force - the stale lock auto-releases.
    result = await _call_tool_real(
        real_manager,
        {"action": "set", "memory_id": mid, "to": "blocked", "actor": "C"},
    )
    assert result["stale_lock_released"] is True
    assert result["force_used"] is False
    assert result["to_status"] == "blocked"
    # C now owns the lock (blocked keeps the lock; takeover refreshes owner)
    assert result["locked_by"] == "C"


def test_e10_rest_stale_lock_auto_released(client: TestClient) -> None:
    mid = _create_rest_memory(client)
    client.post(f"/memories/{mid}/workflow", json={"to": "in-progress", "actor": "A"})
    mgr: MemoryManager = api_main._manager  # type: ignore[assignment]
    mgr.sqlite.set_workflow_status(mid, "in-progress", "A", "2020-01-01T00:00:00+00:00")

    resp = _set_rest(client, mid, "blocked", "C")  # no force needed
    assert resp["status"] == 200, resp
    assert resp["json"]["stale_lock_released"] is True
    assert resp["json"]["locked_by"] == "C"


# ===========================================================================
# E11 — rate-limit churn >N/min → BLOCKED
#
# NOTE: the rate-limit guardrail IS enforced at both layers. The HTTP status
# the REST endpoint returns for a rate-limit ValueError is 409 (the REST layer
# maps ALL ValueError from the manager to 409), NOT 429. The MCP layer returns
# a clean error dict. The scenario brief's "429" is the semantically-ideal
# status; the actual implementation uses 409 uniformly — enforcement itself
# holds, the status code is a separate finding for the Tech Lead.
# ===========================================================================


async def test_e11_mcp_rate_limit_blocks_excess(real_manager: MemoryManager) -> None:
    """Fixture caps at 5 transitions/min. Cycle through 5 legal transitions,
    then the 6th must be rejected with a rate-limit error."""
    mid = _seed_memory(real_manager)
    path = ["in-progress", "blocked", "resolved", "in-progress", "blocked"]
    for status in path:
        real_manager.workflow_set(mid, status, actor="A")

    result = await _dispatch_real(
        real_manager,
        {"action": "set", "memory_id": mid, "to": "resolved", "actor": "A"},
    )
    assert "error" in result
    assert "rate limit exceeded" in result["error"]


def test_e11_rest_rate_limit_blocks_excess(client: TestClient) -> None:
    """REST enforces the rate limit; status is 409 (ValueError→409 mapping)."""
    mid = _create_rest_memory(client)
    client.post(f"/memories/{mid}/workflow", json={"to": "in-progress", "actor": "A"})
    client.post(f"/memories/{mid}/workflow", json={"to": "blocked", "actor": "A"})
    client.post(f"/memories/{mid}/workflow", json={"to": "resolved", "actor": "A"})
    client.post(f"/memories/{mid}/workflow", json={"to": "in-progress", "actor": "A"})
    client.post(f"/memories/{mid}/workflow", json={"to": "blocked", "actor": "A"})

    resp = _set_rest(client, mid, "resolved", "A")  # 6th in the minute
    assert resp["status"] == 409, resp  # enforced; 409 not 429 (see docstring)
    assert "rate limit exceeded" in resp["json"]["detail"]


# ===========================================================================
# E12 — DELETE /workflow → withdrawn (terminal); subsequent set REJECTED
# (CR finding #3: DELETE=withdrawn, NOT lock-release-to-resumable)
# ===========================================================================


async def test_e12_mcp_withdrawn_blocks_further_sets(
    real_manager: MemoryManager,
) -> None:
    """The MCP tool has no DELETE action (DELETE is REST-only). This proves the
    equivalent manager-level transition (-> withdrawn) makes the memory terminal:
    a subsequent set is rejected, confirming the irreversibility semantics
    that DELETE relies on. The REST DELETE path is proven in test_e12_rest_*."""
    mid = _seed_memory(real_manager)
    real_manager.workflow_set(mid, "in-progress", actor="A")
    # Equivalent of DELETE: transition to withdrawn (terminal).
    real_manager.workflow_set(mid, "withdrawn", actor="A")
    assert real_manager.workflow_get(mid)["workflow_status"] == "withdrawn"
    assert real_manager.workflow_get(mid)["locked_by"] is None  # lock released

    # Subsequent set to a different state -> REJECTED (terminal).
    result = await _dispatch_real(
        real_manager,
        {"action": "set", "memory_id": mid, "to": "in-progress", "actor": "A"},
    )
    assert "error" in result
    assert "terminal state" in result["error"]


def test_e12_rest_delete_transitions_to_withdrawn(client: TestClient) -> None:
    mid = _create_rest_memory(client)
    client.post(f"/memories/{mid}/workflow", json={"to": "in-progress", "actor": "A"})
    resp = client.delete(f"/memories/{mid}/workflow", params={"actor": "A"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["to_status"] == "withdrawn"
    assert body["terminal"] is True
    assert body["locked_by"] is None  # lock released on terminal
    # persisted
    assert client.get(f"/memories/{mid}/workflow").json()["workflow_status"] == "withdrawn"


def test_e12_rest_set_after_delete_rejected_409(client: TestClient) -> None:
    """After DELETE (withdrawn, terminal) a POST to a different state is 409."""
    mid = _create_rest_memory(client)
    client.post(f"/memories/{mid}/workflow", json={"to": "in-progress", "actor": "A"})
    client.delete(f"/memories/{mid}/workflow", params={"actor": "A"})

    resp = _set_rest(client, mid, "in-progress", "A")
    assert resp["status"] == 409, resp
    assert "terminal state" in resp["json"]["detail"]


def test_e12_rest_delete_idempotent_on_terminal(client: TestClient) -> None:
    """A second DELETE (withdrawn → withdrawn) is an idempotent no-op, not an
    error — the idempotent guardrail (G3) short-circuits same-status sets."""
    mid = _create_rest_memory(client)
    client.delete(f"/memories/{mid}/workflow", params={"actor": "A"})
    resp = client.delete(f"/memories/{mid}/workflow", params={"actor": "A"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["idempotent"] is True
    assert resp.json()["recorded"] is False


# ===========================================================================
# E13 — set on nonexistent memory_id → clear error (NOT AssertionError / 500)
# (server-side enforcement at BOTH layers — highest priority. CR finding #1.)
# ===========================================================================


async def test_e13_mcp_set_nonexistent_memory_clear_error(
    real_manager: MemoryManager,
) -> None:
    """The assert->ValueError fix (CR #1): a nonexistent memory surfaces as a
    clean error dict, NOT an AssertionError (which would be a 500 and would be
    stripped under ``python -O``)."""
    result = await _dispatch_real(
        real_manager,
        {"action": "set", "memory_id": "nonexistent-id", "to": "in-progress", "actor": "A"},
    )
    assert "error" in result
    assert "not found" in result["error"]
    # The contract fix: it is a ValueError message, not an AssertionError trace.
    assert "AssertionError" not in str(result)


async def test_e13_mcp_get_nonexistent_memory_clear_error(
    real_manager: MemoryManager,
) -> None:
    result = await _dispatch_real(real_manager, {"action": "get", "memory_id": "nonexistent-id"})
    assert "error" in result
    assert "not found" in result["error"]


def test_e13_rest_get_nonexistent_memory_404(client: TestClient) -> None:
    resp = client.get("/memories/no-such-id/workflow")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_e13_rest_post_nonexistent_memory_rejected(client: TestClient) -> None:
    """POST on a nonexistent memory is rejected (NOT a 500). The manager raises
    ValueError("not found"); the REST layer maps ALL ValueError → 409, so the
    status is 409 (not the semantically-ideal 404). The CR #1 fix is honored:
    no AssertionError, no 500. The 404-vs-409 status nuance is a separate
    finding for the Tech Lead."""
    resp = client.post(
        "/memories/no-such-id/workflow",
        json={"to": "in-progress", "actor": "A"},
    )
    # No 500 — the contract fix holds.
    assert resp.status_code != 500
    assert resp.status_code == 409  # ValueError→409 (enforced; 404 would be ideal)
    assert "not found" in resp.json()["detail"]


async def test_e13_toctou_concurrent_delete_is_valueerror_not_assertion(
    real_manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TOCTOU window (get() sees the memory, get_workflow_status() does
    not, e.g. a concurrent delete) must surface as ValueError (404/409), not
    AssertionError (500) - the CR #1 fix. Verified at the MCP layer."""
    mid = _seed_memory(real_manager)
    real_manager.workflow_set(mid, "in-progress", actor="A")
    # Simulate the race: existence check passes, current-state read returns None.
    monkeypatch.setattr(real_manager.sqlite, "get_workflow_status", lambda _mid: None)
    result = await _dispatch_real(
        real_manager,
        {"action": "set", "memory_id": mid, "to": "done", "actor": "A"},
    )
    assert "error" in result
    assert "not found" in result["error"]
    assert "AssertionError" not in str(result)


# ===========================================================================
# E14 — unknown action → clear error (MCP-only; REST uses distinct endpoints)
# ===========================================================================


async def test_e14_mcp_unknown_action_error(real_manager: MemoryManager) -> None:
    mid = _seed_memory(real_manager)
    result = await _dispatch_real(real_manager, {"action": "bogus", "memory_id": mid})
    assert "error" in result
    assert "unknown action" in result["error"]


# ===========================================================================
# E15 — missing required params (memory_id / to / actor) → clear error
# ===========================================================================


async def test_e15_mcp_missing_memory_id_error(real_manager: MemoryManager) -> None:
    result = await _dispatch_real(real_manager, {"action": "get"})
    assert "error" in result
    assert "memory_id" in result["error"]


async def test_e15_mcp_set_missing_to_error(real_manager: MemoryManager) -> None:
    mid = _seed_memory(real_manager)
    result = await _dispatch_real(real_manager, {"action": "set", "memory_id": mid, "actor": "A"})
    assert "error" in result
    assert "to" in result["error"]


async def test_e15_mcp_set_missing_actor_error(real_manager: MemoryManager) -> None:
    mid = _seed_memory(real_manager)
    result = await _dispatch_real(
        real_manager, {"action": "set", "memory_id": mid, "to": "in-progress"}
    )
    assert "error" in result
    assert "actor" in result["error"]


def test_e15_rest_set_missing_to_rejected(client: TestClient) -> None:
    """POST without ``to`` → pydantic validation → 422."""
    mid = _create_rest_memory(client)
    resp = client.post(f"/memories/{mid}/workflow", json={"actor": "A"})
    assert resp.status_code == 422


def test_e15_rest_set_missing_actor_rejected(client: TestClient) -> None:
    """POST without ``actor`` → pydantic validation → 422."""
    mid = _create_rest_memory(client)
    resp = client.post(f"/memories/{mid}/workflow", json={"to": "in-progress"})
    assert resp.status_code == 422


def test_e15_rest_delete_missing_actor_rejected(client: TestClient) -> None:
    """DELETE without ``actor`` → 422."""
    mid = _create_rest_memory(client)
    resp = client.delete(f"/memories/{mid}/workflow")
    assert resp.status_code == 422
