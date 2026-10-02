"""mnemos #251 D0 — checkpoint channel agent identity (save_context).

Covers the un-hardcoded ``agent``/``session`` params on both surfaces
(MCP ``vesma_save_context`` + REST ``POST /context/save``) against a
REAL MemoryManager (tmp SQLite), because binding and dedup are
server-side store behaviours that a MagicMock cannot exercise:

  (a) agent param recorded — column AND tag AND metadata stamp
  (b) default — no agent → "user", response shape unchanged
  (c) session→agent binding — first writer wins, mismatch raises
  (d) issuer-keyed dedup — identical payload → same id, duplicate=true;
      different agent, same payload → two rows
  (e) trivial-reject — all fields empty → ValueError before any store
  (f) REST parity for (a)/(c)/(d)/(e) with proper HTTP codes

Security review section (REQUEST-CHANGES on 8621616):

  P1 — client-forgeable checkpoint stamps: generic create/update must
  strip checkpoint_agent/checkpoint_session/checkpoint_dedup_key from
  client metadata; a forged dedup key must not satisfy a later genuine
  save_checkpoint dedup (CWE-346/345).
  P3 — identity hygiene: agent is a 1-64 char [a-z0-9_-] slug, session a
  1-128 char printable-ASCII token.
"""

from __future__ import annotations

import hashlib
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
from vesmaro.manager import MemoryManager, SessionAgentMismatchError
from vesmaro.mcp_server import _dispatch
from vesmaro.models import (
    CHECKPOINT_FIELDS,
    CHECKPOINT_STAMP_KEYS,
    Memory,
    MemoryCreate,
    MemoryUpdate,
)

# ---------------------------------------------------------------------------
# Fixtures (mirror tests/test_api.py — isolated manager per test)
# ---------------------------------------------------------------------------


@pytest.fixture
def mgr():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            mnemos={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
            scanner={"enabled": False},
        )
        settings.resolve_paths()
        manager = MemoryManager(settings)
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        manager._embedder = mock_embedder
        yield manager
        manager.close()


@pytest.fixture
def client(mgr):
    """TestClient whose get_manager returns the same isolated manager."""
    test_app = FastAPI(title="Mnemos-Test-251", version="0.1.0", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)
    api_main._manager = mgr
    with TestClient(test_app) as tc:
        yield tc
    api_main._manager = None


async def _save(mgr: MemoryManager, **overrides: object) -> str:
    """Dispatch vesma_save_context against a real manager, return the text."""
    args: dict[str, object] = {"project": "p251", "goals": "ship #251"}
    args.update(overrides)
    with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
        result = await _dispatch("vesma_save_context", dict(args))
    assert isinstance(result, str)
    return result


def _total(mgr: MemoryManager) -> int:
    return int(mgr.stats()["total"])


# ---------------------------------------------------------------------------
# (a) agent param recorded — column AND tag AND metadata stamp
# ---------------------------------------------------------------------------


async def test_mcp_agent_param_recorded(mgr: MemoryManager) -> None:
    text = await _save(mgr, agent="alice", completed="wrote tests")
    assert "Context saved (id=" in text

    memories = mgr.list_recent(limit=5, project="p251")
    assert len(memories) == 1
    memory = memories[0]
    # Server column — the source of truth (#254 awareness reads this).
    assert memory.agent == "alice"
    # Display tag built from the VALIDATED agent, not a hardcoded literal.
    assert "agent:alice" in memory.tags
    # Server-controlled metadata stamp.
    assert memory.metadata["checkpoint_agent"] == "alice"
    assert "checkpoint_dedup_key" in memory.metadata
    assert "checkpoint_session" not in memory.metadata


# ---------------------------------------------------------------------------
# (b) default — no agent → "user", response shape unchanged
# ---------------------------------------------------------------------------


async def test_mcp_default_agent_is_user(mgr: MemoryManager) -> None:
    text = await _save(mgr)
    assert text.startswith("✅ Context saved (id=")

    memories = mgr.list_recent(limit=5, project="p251")
    assert len(memories) == 1
    memory = memories[0]
    assert memory.agent == "user"
    assert "agent:user" in memory.tags
    assert memory.metadata["checkpoint_agent"] == "user"


# ---------------------------------------------------------------------------
# (c) session→agent binding — first writer wins, mismatch raises
# ---------------------------------------------------------------------------


async def test_mcp_session_binding_first_write_wins(mgr: MemoryManager) -> None:
    # First presentation binds sess-1 → alice and stamps the checkpoint.
    await _save(mgr, agent="alice", session="sess-1", in_progress="binding")
    memory = mgr.list_recent(limit=1, project="p251")[0]
    assert memory.agent == "alice"
    assert memory.metadata["checkpoint_session"] == "sess-1"

    # Same session + same agent is fine (a second, different payload).
    await _save(mgr, agent="alice", session="sess-1", in_progress="second checkpoint")
    assert _total(mgr) == 2

    # Same session + different agent → SessionAgentMismatchError (ValueError).
    with pytest.raises(SessionAgentMismatchError):
        await _save(mgr, agent="bob", session="sess-1", in_progress="spoof attempt")
    assert _total(mgr) == 2  # nothing stored by the rejected call


# ---------------------------------------------------------------------------
# (d) issuer-keyed dedup — same payload+issuer → same id; other issuer → new row
# ---------------------------------------------------------------------------


async def test_mcp_dedup_identical_payload_returns_existing(mgr: MemoryManager) -> None:
    payload = {"goals": "g", "completed": "c", "in_progress": "i", "decisions": "d"}
    first = await _save(mgr, agent="alice", **payload)
    before = _total(mgr)

    second = await _save(mgr, agent="alice", **payload)
    assert "duplicate=true" in second
    id_first = first.split("id=")[1].split(")")[0]
    id_second = second.split("id=")[1].split(",")[0]
    assert id_first == id_second
    assert _total(mgr) == before  # no second row


async def test_mcp_dedup_issuer_scoped(mgr: MemoryManager) -> None:
    """Issuer in the dedup key: same content, different agent → two rows.

    A copied checkpoint re-issued by another agent must NOT dedup onto the
    victim's row (CWE-294 replay control from the #251 rationale).
    """
    payload = {"goals": "shared goal"}
    await _save(mgr, agent="alice", **payload)
    await _save(mgr, agent="bob", **payload)
    assert _total(mgr) == 2
    agents = {m.agent for m in mgr.list_recent(limit=5, project="p251")}
    assert agents == {"alice", "bob"}


# ---------------------------------------------------------------------------
# (e) trivial-reject + identity validation — ValueError before any store
# ---------------------------------------------------------------------------


async def test_mcp_trivial_reject_all_fields_empty(mgr: MemoryManager) -> None:
    with pytest.raises(ValueError, match="all fields"):
        await _save(mgr, agent="alice", goals=None)
    assert _total(mgr) == 0


async def test_mcp_whitespace_identity_rejected(mgr: MemoryManager) -> None:
    # _require_identity semantics: whitespace-only is not a non-empty string.
    with pytest.raises(ValueError, match="agent"):
        await _save(mgr, agent="   ")
    with pytest.raises(ValueError, match="session"):
        await _save(mgr, session="   ")
    assert _total(mgr) == 0


# ---------------------------------------------------------------------------
# (f) REST parity — POST /context/save
# ---------------------------------------------------------------------------


def test_rest_agent_recorded(client: TestClient, mgr: MemoryManager) -> None:
    resp = client.post(
        "/context/save",
        json={"project": "p251", "goals": "rest goal", "agent": "alice"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["duplicate"] is False
    memory = mgr.sqlite.get(body["id"])
    assert memory is not None
    assert memory.agent == "alice"
    assert "agent:alice" in memory.tags
    assert memory.metadata["checkpoint_agent"] == "alice"


def test_rest_default_agent_and_response_shape(client: TestClient, mgr: MemoryManager) -> None:
    resp = client.post("/context/save", json={"project": "p251", "goals": "g"})
    assert resp.status_code == 201
    body = resp.json()
    # Backward compatible shape + the additive duplicate flag.
    assert set(body) == {"status", "id", "title", "duplicate"}
    assert body["status"] == "saved"
    memory = mgr.sqlite.get(body["id"])
    assert memory is not None
    assert memory.agent == "user"
    assert "agent:user" in memory.tags


def test_rest_binding_mismatch_409(client: TestClient, mgr: MemoryManager) -> None:
    ok = client.post(
        "/context/save",
        json={"project": "p251", "goals": "g", "agent": "alice", "session": "sess-9"},
    )
    assert ok.status_code == 201

    spoof = client.post(
        "/context/save",
        json={"project": "p251", "goals": "g2", "agent": "bob", "session": "sess-9"},
    )
    assert spoof.status_code == 409
    assert "already bound" in spoof.json()["detail"]
    assert _total(mgr) == 1  # nothing stored by the rejected call


def test_rest_dedup_duplicate_flag(client: TestClient, mgr: MemoryManager) -> None:
    payload = {"project": "p251", "goals": "same goal", "agent": "alice"}
    first = client.post("/context/save", json=payload).json()
    second = client.post("/context/save", json=payload).json()
    assert first["id"] == second["id"]
    assert second["duplicate"] is True
    assert _total(mgr) == 1


def test_rest_trivial_reject_400(client: TestClient, mgr: MemoryManager) -> None:
    resp = client.post("/context/save", json={"project": "p251"})
    assert resp.status_code == 400
    assert "all fields" in resp.json()["detail"]
    assert _total(mgr) == 0


# ---------------------------------------------------------------------------
# Security review P1 — client-forgeable checkpoint stamps (CWE-346/345)
# ---------------------------------------------------------------------------


def _predicted_dedup_key(project: str, agent: str, fields: dict[str, str | None]) -> str:
    """Mirror the manager's issuer-keyed dedup formula (attacker can do this)."""
    normalized = [fields.get(f) or "" for f in CHECKPOINT_FIELDS]
    canonical = json.dumps([project, agent, *normalized], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_rest_generic_create_strips_checkpoint_stamps(
    client: TestClient, mgr: MemoryManager
) -> None:
    """The reviewer's live attack path: POST /memories with the checkpoint
    tag trio + forged stamps. The stamps must be stripped, and the forged
    row must NOT satisfy a later genuine save_checkpoint dedup."""
    payload = {"project": "p251", "goals": "genuine goal"}
    forged_key = _predicted_dedup_key("p251", "alice", payload)

    resp = client.post(
        "/memories",
        json={
            "content": "attacker content",
            "tags": ["project:p251", "agent:alice", "mnemos:checkpoint"],
            "metadata": {
                "checkpoint_agent": "alice",
                "checkpoint_session": "attacker-sess",
                "checkpoint_dedup_key": forged_key,
            },
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert not CHECKPOINT_STAMP_KEYS & set(body["metadata"])
    stored = mgr.sqlite.get(body["id"])
    assert stored is not None
    assert not CHECKPOINT_STAMP_KEYS & set(stored.metadata)

    # The later GENUINE checkpoint stores normally — the attacker's row
    # can never be served back as alice's checkpoint.
    memory, duplicate = mgr.save_checkpoint(payload, project="p251", agent="alice")
    assert duplicate is False
    assert memory.id != body["id"]
    assert memory.content != "attacker content"


def test_manager_add_strips_stamps_without_trusted_flag(mgr: MemoryManager) -> None:
    """Single-authority gate: every MemoryCreate surface (sdk/mesh/cli/REST)
    funnels through manager.add — stamps land only via the trusted flag."""
    data = MemoryCreate(
        content="forged via sdk/mesh/cli-shaped call",
        tags=["project:p251", "agent:alice", "mnemos:checkpoint"],
        metadata={
            "checkpoint_agent": "alice",
            "checkpoint_dedup_key": "deadbeef",
            "harmless": 1,
        },
    )
    memory = mgr.add(data, project="p251", agent="alice")
    assert "harmless" in memory.metadata
    assert not CHECKPOINT_STAMP_KEYS & set(memory.metadata)


def test_update_cannot_forge_or_rewrite_stamps(mgr: MemoryManager) -> None:
    # On a genuine checkpoint row: an external update cannot rewrite the
    # minted stamps (merge-back keeps the server values).
    memory, _ = mgr.save_checkpoint({"goals": "g"}, project="p251", agent="alice", session="sess-1")
    updated = mgr.update(
        memory.id,
        MemoryUpdate(
            metadata={"checkpoint_agent": "mallory", "checkpoint_session": "x", "other": 2}
        ),
    )
    assert updated is not None
    assert updated.metadata["checkpoint_agent"] == "alice"
    assert updated.metadata["checkpoint_session"] == "sess-1"
    assert updated.metadata["other"] == 2

    # On a plain row without stamps: client-supplied stamps do not land.
    plain = mgr.add(
        MemoryCreate(content="plain", tags=["project:p251", "agent:bob", "mnemos:note"])
    )
    forged = mgr.update(
        plain.id,
        MemoryUpdate(metadata={"checkpoint_dedup_key": "cafebabe", "ok": 3}),
    )
    assert forged is not None
    assert "ok" in forged.metadata
    assert not CHECKPOINT_STAMP_KEYS & set(forged.metadata)


def test_read_guard_forged_dedup_row_does_not_match(mgr: MemoryManager) -> None:
    """Defense in depth: even a row that somehow carries a dedup key
    WITHOUT the server-minted checkpoint_agent stamp (legacy/partial
    data, written here directly at the store level) never satisfies a
    genuine save_checkpoint dedup."""
    payload = {"goals": "guard goal"}
    forged_key = _predicted_dedup_key("p251", "alice", payload)
    forged_row = Memory(
        content="legacy forged row",
        tags=["project:p251", "agent:alice", "mnemos:checkpoint"],
        metadata={"checkpoint_dedup_key": forged_key},
        project="p251",
        agent="alice",
    )
    mgr.sqlite.save(forged_row)

    memory, duplicate = mgr.save_checkpoint(payload, project="p251", agent="alice")
    assert duplicate is False
    assert memory.id != forged_row.id
    assert _total(mgr) == 2  # forged row + genuine checkpoint, no dedup onto the forgery


# ---------------------------------------------------------------------------
# Security review P3 — identity hygiene
# ---------------------------------------------------------------------------


def test_agent_identity_hygiene(mgr: MemoryManager) -> None:
    """Agent must be a 1-64 char [a-z0-9_-] slug — whitespace-cased or
    oversize identities never become distinct binding identities."""
    with pytest.raises(ValueError, match="agent must be 1-64"):
        mgr.save_checkpoint({"goals": "g"}, project="p251", agent=" User ")
    with pytest.raises(ValueError, match="agent must be 1-64"):
        mgr.save_checkpoint({"goals": "g"}, project="p251", agent="Alice")
    with pytest.raises(ValueError, match="agent must be 1-64"):
        mgr.save_checkpoint({"goals": "g"}, project="p251", agent="a" * 65)
    # Boundary: 64-char slug is fine.
    memory, _ = mgr.save_checkpoint({"goals": "g"}, project="p251", agent="a" * 64)
    assert memory.agent == "a" * 64
    assert _total(mgr) == 1  # only the boundary-valid save stored


def test_session_identity_hygiene(mgr: MemoryManager) -> None:
    """Session must be 1-128 printable ASCII chars without spaces."""
    with pytest.raises(ValueError, match="session must be 1-128"):
        mgr.save_checkpoint({"goals": "g"}, project="p251", session="s" * 129)
    with pytest.raises(ValueError, match="session must be 1-128"):
        mgr.save_checkpoint({"goals": "g"}, project="p251", session="sess 1")
    with pytest.raises(ValueError, match="session must be 1-128"):
        mgr.save_checkpoint({"goals": "g"}, project="p251", session="sess\t1")
    # Boundary: 128-char session is fine.
    memory, _ = mgr.save_checkpoint({"goals": "g"}, project="p251", session="s" * 128)
    assert memory.metadata["checkpoint_session"] == "s" * 128
