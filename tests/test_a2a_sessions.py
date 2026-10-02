"""Tests for the A2A Sessions API (M16).

Covers all 5 endpoints + idempotency + validation:

  * ``test_create_session``     — POST /v1/sessions
  * ``test_get_session``        — GET /v1/sessions/{id}
  * ``test_404_unknown_session`` — error path
  * ``test_write_turn``         — POST /v1/sessions/{id}/turns (atomic)
  * ``test_idempotency``        — same ``message_id`` returns same turn
  * ``test_load_turn_summary``  — mode=summary (no content in body)
  * ``test_load_turn_full``     — mode=full (content in body)
  * ``test_range_summary``      — POST .../turns/range
  * ``test_validation_error``   — invalid role/outcome → 422
  * ``test_outcome_requires_a2a`` — outcome guard

The fixture mirrors ``tests/test_api.py`` (isolated MemoryManager +
copied routes) so each test runs against its own database file.  This
matters: the A2A tables live in the same SQLite file, and a stale row
from one test would otherwise leak into the next.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vesma.api import main as api_main
from vesma.api.main import app, lifespan
from vesma.config import Settings
from vesma.manager import MemoryManager

# Pattern for ``conv-YYYY-MM-DD-<short-uuid>`` — checked in
# ``test_create_session`` to make sure the contract is held.
_SESSION_ID_RE = re.compile(r"^conv-\d{4}-\d{2}-\d{2}-[0-9a-f]{8}$")
_TURN_ID_RE = re.compile(r"^turn-\d+$")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            vesma={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test-a2a.db",
            },
            embedding={"provider": "onnx"},
            # Scanner is exercised in test_scanner.py — disable it here so
            # the API lifespan does not spawn a daemon thread per test
            # (defence-in-depth against the singleton thread leak).
            scanner={"enabled": False},
        )
        settings.resolve_paths()
        yield settings


@pytest.fixture
def client(tmp_settings):
    """Yield a TestClient backed by a fresh MemoryManager per test."""
    mgr = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder

    test_app = FastAPI(
        title="Mnemos-A2A-Test",
        version="0.1.0",
        lifespan=lifespan,
    )
    for route in app.routes:
        test_app.routes.append(route)

    api_main._manager = mgr
    with TestClient(test_app) as tc:
        yield tc
    mgr.close()
    api_main._manager = None


@pytest.fixture
def session(client: TestClient) -> dict:
    """Create one session and return its body as a dict."""
    resp = client.post(
        "/v1/sessions",
        json={"user_id": "abyss", "metadata": {"workspace": "mnemos"}},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 1. POST /v1/sessions
# ---------------------------------------------------------------------------


class TestCreateSession:
    def test_create_session(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/sessions",
            json={"user_id": "abyss", "metadata": {"started_by": "vscode"}},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert _SESSION_ID_RE.match(body["session_id"]), body["session_id"]
        assert body["user_id"] == "abyss"
        assert body["turns_count"] == 0
        assert body["metadata"] == {"started_by": "vscode"}
        assert "created_at" in body
        assert body["created_at"] == body["updated_at"]

    def test_create_session_minimal_payload(self, client: TestClient) -> None:
        """No body at all should still work (defaults)."""
        resp = client.post("/v1/sessions", json={})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert _SESSION_ID_RE.match(body["session_id"])
        assert body["user_id"] == ""
        assert body["turns_count"] == 0

    def test_create_two_sessions_have_distinct_ids(self, client: TestClient) -> None:
        a = client.post("/v1/sessions", json={"user_id": "u"}).json()["session_id"]
        b = client.post("/v1/sessions", json={"user_id": "u"}).json()["session_id"]
        assert a != b


# ---------------------------------------------------------------------------
# 2. GET /v1/sessions/{id}
# ---------------------------------------------------------------------------


class TestGetSession:
    def test_get_session(self, client: TestClient, session: dict) -> None:
        resp = client.get(f"/v1/sessions/{session['session_id']}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["session_id"] == session["session_id"]
        assert body["user_id"] == "abyss"
        assert body["turns_count"] == 0

    def test_404_unknown_session(self, client: TestClient) -> None:
        resp = client.get("/v1/sessions/conv-2099-01-01-deadbeef")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 3. POST /v1/sessions/{id}/turns
# ---------------------------------------------------------------------------


def _write_turn(
    client: TestClient,
    session_id: str,
    *,
    role: str = "a2a_message",
    content: str = "Need a migration for orders.archived_at column.",
    message_id: str | None = "msg-x7y8z9",
    from_: str = "mnemos-senior-system-engineer",
    to: str = "mnemos-senior-dba",
    outcome: str | None = "delivered",
    tags: list[str] | None = None,
) -> dict:
    payload: dict = {
        "role": role,
        "content": content,
        "from": from_,
        "to": to,
    }
    if message_id is not None:
        payload["message_id"] = message_id
    if outcome is not None:
        payload["outcome"] = outcome
    payload["tags"] = tags or ["migration", "schema", "orders"]
    return client.post(f"/v1/sessions/{session_id}/turns", json=payload).json()


class TestWriteTurn:
    def test_write_turn(self, client: TestClient, session: dict) -> None:
        body = _write_turn(client, session["session_id"])
        assert _TURN_ID_RE.match(body["turn_id"]), body["turn_id"]
        assert body["step_number"] == 1
        assert body["context_pointer"] == f"memory://{session['session_id']}#step-1"
        assert "created_at" in body
        # GET session should now report turns_count=1
        g = client.get(f"/v1/sessions/{session['session_id']}").json()
        assert g["turns_count"] == 1
        # updated_at should have advanced
        assert g["updated_at"] >= session["updated_at"]

    def test_write_turn_increments_step_number(self, client: TestClient, session: dict) -> None:
        first = _write_turn(client, session["session_id"], message_id="msg-1", content="first")
        second = _write_turn(client, session["session_id"], message_id="msg-2", content="second")
        third = _write_turn(client, session["session_id"], message_id="msg-3", content="third")
        assert first["step_number"] == 1
        assert second["step_number"] == 2
        assert third["step_number"] == 3
        assert first["turn_id"] == "turn-1"
        assert second["turn_id"] == "turn-2"
        assert third["turn_id"] == "turn-3"

    def test_write_turn_to_unknown_session_is_404(self, client: TestClient) -> None:
        body = client.post(
            "/v1/sessions/conv-2099-01-01-deadbeef/turns",
            json={"role": "user", "content": "hi"},
        )
        assert body.status_code == 404


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_idempotency(self, client: TestClient, session: dict) -> None:
        """Repeat POST with the same message_id returns the SAME turn."""
        first = _write_turn(client, session["session_id"], message_id="msg-idem-1")
        second = _write_turn(
            client, session["session_id"], message_id="msg-idem-1", content="DIFFERENT"
        )
        assert first["turn_id"] == second["turn_id"]
        assert first["step_number"] == second["step_number"]
        # No duplicate — turns_count must still be 1
        g = client.get(f"/v1/sessions/{session['session_id']}").json()
        assert g["turns_count"] == 1

    def test_idempotency_different_message_ids_create_two_turns(
        self, client: TestClient, session: dict
    ) -> None:
        a = _write_turn(client, session["session_id"], message_id="msg-A")
        b = _write_turn(client, session["session_id"], message_id="msg-B")
        assert a["turn_id"] != b["turn_id"]
        assert a["step_number"] == 1
        assert b["step_number"] == 2

    def test_no_message_id_is_not_idempotent(self, client: TestClient, session: dict) -> None:
        """Two POSTs with no message_id create two distinct turns."""
        a = _write_turn(client, session["session_id"], message_id=None)
        b = _write_turn(client, session["session_id"], message_id=None)
        assert a["step_number"] == 1
        assert b["step_number"] == 2


# ---------------------------------------------------------------------------
# 4. GET /v1/sessions/{id}/turns/{turn_id}  (mode=summary | full)
# ---------------------------------------------------------------------------


class TestLoadTurn:
    def test_load_turn_summary(self, client: TestClient, session: dict) -> None:
        _write_turn(
            client,
            session["session_id"],
            content=(
                "Need a migration for orders.archived_at column. "
                "DECISION: add column, not table. "
                "- [x] write migration plan"
            ),
            message_id="msg-summary-1",
        )
        # default mode = summary
        resp = client.get(f"/v1/sessions/{session['session_id']}/turns/turn-1")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["turn_id"] == "turn-1"
        assert body["role"] == "a2a_message"
        assert body["from"] == "mnemos-senior-system-engineer"
        assert body["to"] == "mnemos-senior-dba"
        assert body["context_pointer"] == f"memory://{session['session_id']}#step-1"
        # In summary mode, content must NOT be present
        assert body.get("content") is None
        # Summary must be present and bounded
        assert body["summary"] is not None
        assert len(body["summary"]) <= 500  # spec: ≤ 500 bytes
        # key_decisions were extracted
        assert any("DECISION:" in d for d in body["key_decisions"])
        assert any("- [x]" in d for d in body["key_decisions"])

    def test_load_turn_full(self, client: TestClient, session: dict) -> None:
        full_content = (
            "Long form A2A message that should be returned verbatim "
            "when mode=full is requested. " * 10
        )
        _write_turn(
            client,
            session["session_id"],
            content=full_content,
            message_id="msg-full-1",
        )
        resp = client.get(f"/v1/sessions/{session['session_id']}/turns/turn-1?mode=full")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["content"] is not None
        assert body["content"] == full_content

    def test_load_unknown_turn_is_404(self, client: TestClient, session: dict) -> None:
        resp = client.get(f"/v1/sessions/{session['session_id']}/turns/turn-9999")
        assert resp.status_code == 404

    def test_load_turn_unknown_session_is_404(self, client: TestClient) -> None:
        resp = client.get("/v1/sessions/conv-2099-01-01-deadbeef/turns/turn-1")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 5. POST /v1/sessions/{id}/turns/range
# ---------------------------------------------------------------------------


class TestRange:
    def test_range_summary(self, client: TestClient, session: dict) -> None:
        # Write 5 turns with no idempotency key, so each gets its own step
        for i in range(5):
            _write_turn(
                client,
                session["session_id"],
                message_id=None,
                content=f"step {i + 1}: some content {i + 1}",
            )
        resp = client.post(
            f"/v1/sessions/{session['session_id']}/turns/range",
            json={"from_step": 1, "to_step": 3, "mode": "summary"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 3
        assert body["mode"] == "summary"
        assert [t["turn_id"] for t in body["turns"]] == ["turn-1", "turn-2", "turn-3"]
        for t in body["turns"]:
            assert t.get("content") is None
            assert t["summary"] is not None

    def test_range_full(self, client: TestClient, session: dict) -> None:
        for i in range(3):
            _write_turn(
                client,
                session["session_id"],
                message_id=None,
                content=f"step {i + 1} content",
            )
        resp = client.post(
            f"/v1/sessions/{session['session_id']}/turns/range",
            json={"from_step": 1, "to_step": 5, "mode": "full"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Only 3 turns exist, so total=3
        assert body["total"] == 3
        assert body["mode"] == "full"
        for t in body["turns"]:
            assert t["content"] is not None

    def test_range_unknown_session_is_404(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/sessions/conv-2099-01-01-deadbeef/turns/range",
            json={"from_step": 1, "to_step": 5},
        )
        assert resp.status_code == 404

    def test_range_inverted_steps_is_422(self, client: TestClient, session: dict) -> None:
        resp = client.post(
            f"/v1/sessions/{session['session_id']}/turns/range",
            json={"from_step": 5, "to_step": 1},
        )
        assert resp.status_code == 422

    def test_range_out_of_bounds_is_empty(self, client: TestClient, session: dict) -> None:
        _write_turn(client, session["session_id"], message_id="only-1")
        resp = client.post(
            f"/v1/sessions/{session['session_id']}/turns/range",
            json={"from_step": 100, "to_step": 200},
        )
        assert resp.status_code == 200
        assert resp.json() == {"turns": [], "total": 0, "mode": "summary"}


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


class TestValidation:
    def test_validation_error_invalid_role(self, client: TestClient, session: dict) -> None:
        resp = client.post(
            f"/v1/sessions/{session['session_id']}/turns",
            json={"role": "robot_overlord", "content": "x"},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        # FastAPI returns a list of error objects
        assert any("role" in (e.get("loc") or []) for e in detail)

    def test_validation_error_empty_content(self, client: TestClient, session: dict) -> None:
        resp = client.post(
            f"/v1/sessions/{session['session_id']}/turns",
            json={"role": "user", "content": ""},
        )
        assert resp.status_code == 422

    def test_outcome_rejected_for_non_a2a_role(self, client: TestClient, session: dict) -> None:
        resp = client.post(
            f"/v1/sessions/{session['session_id']}/turns",
            json={
                "role": "user",
                "content": "hi",
                "outcome": "delivered",  # invalid for non-a2a_message
            },
        )
        assert resp.status_code == 422

    def test_a2a_message_defaults_to_delivered_outcome(
        self, client: TestClient, session: dict
    ) -> None:
        """a2a_message turns without an explicit outcome get DELIVERED."""
        resp = client.post(
            f"/v1/sessions/{session['session_id']}/turns",
            json={
                "role": "a2a_message",
                "content": "hello from agent",
                "message_id": "msg-default-outcome",
            },
        )
        assert resp.status_code == 201, resp.text
        # Default outcome was applied (DELIVERED), should be visible in
        # the full-mode GET
        g = client.get(f"/v1/sessions/{session['session_id']}/turns/turn-1?mode=full").json()
        assert g["outcome"] == "delivered"


# ---------------------------------------------------------------------------
# OpenAPI / docs surface
# ---------------------------------------------------------------------------


class TestOpenAPI:
    def test_openapi_includes_v1_sessions(self, client: TestClient) -> None:
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        v1_paths = [p for p in schema["paths"] if p.startswith("/v1/")]
        assert "/v1/sessions" in v1_paths
        assert "/v1/sessions/{session_id}" in v1_paths
        assert "/v1/sessions/{session_id}/turns" in v1_paths
        assert "/v1/sessions/{session_id}/turns/{turn_id}" in v1_paths
        assert "/v1/sessions/{session_id}/turns/range" in v1_paths

    def test_existing_routes_still_present(self, client: TestClient) -> None:
        """Backwards-compat guard — DO NOT touch the original routes."""
        resp = client.get("/openapi.json")
        paths = resp.json()["paths"]
        assert "/memories" in paths
        assert "/search" in paths
        assert "/recall/agent/{name}" in paths
