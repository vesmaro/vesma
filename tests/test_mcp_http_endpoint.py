"""Contract tests for the in-core MCP streamable-HTTP endpoint (ADR-0050 W1).

Pins the ``/mcp`` plane on the ``vesma serve`` app:

* **Transport contracts** — the MCP initialize handshake over HTTP,
  ``tools/list`` returning the canonical tool set, and ``tools/call`` on
  light read-only tools (``vesma_stats`` / ``vesma_list_tags``) against a
  REAL test store — the same handler wiring the stdio leg serves, so a
  transport must not fork the tool surface.
* **Auth negatives (ADR-0050 §3, hard requirement)** — no Bearer → 401,
  garbage Bearer → 401, a valid-envelope token bound to a foreign node
  (aud mismatch) → 401, and a disabled ``mcp.endpoint_enabled`` → 404.
  The loopback-trust invariant IS the no-token case: the fixture's app
  runs auth-free on loopback (every REST route would admit the request),
  yet ``/mcp`` still demands the token.
* **Instance identity** — the ``X-Vesma-Instance-Id`` middleware stamps
  ``/mcp`` responses too (the middleware chain wraps mounts), which is
  the discoverable ``aud`` a harness needs to get a token issued.

The fixture mirrors ``tests/test_api.py`` (fresh app + lifespan per test,
isolated MemoryManager on both manager singletons — REST resolves through
``api.main``, MCP tool dispatch through ``vesma.mcp_server``) plus the
agent-token issuance pattern of ``tests/test_agent_tokens.py``: the
signing key is minted at the settings path the endpoint gate reads, so
the gate and these tests verify against the SAME key material.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_mcp_sdk2_port import _real_mcp_modules
from vesma.agent_tokens import (
    AgentTokenStore,
    issue_agent_token,
    load_or_create_signing_key,
    signing_key_path,
)
from vesma.api import main as api_main
from vesma.api.main import app, lifespan
from vesma.api.middleware import (
    AuthMiddleware,
    DeprecationMiddleware,
    InstanceIdHeaderMiddleware,
    VitalsVerbMiddleware,
)
from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import MemoryCreate
from vesma.service.instance import current_instance_id

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _real_mcp_sdk_stack() -> Iterator[None]:
    """Run this module against the REAL mcp SDK, not the conftest stubs.

    The streamable-HTTP transport under test exists only in the real
    distribution; the conftest stubs (installed whenever nothing imported
    ``mcp`` before conftest runs) cannot drive an HTTP session. Evict and
    restore the stub modules — the established ``test_mcp_sdk2_port``
    pattern — for the whole module.

    The extra re-import HERE is load-bearing: pytest collection imports
    sibling test modules (``test_mcp_server.py``) BEFORE any test runs, so
    ``vesma.mcp_server`` is already the stub-bound copy AND the ``vesma``
    package attribute points at it — the eviction helper clears
    ``sys.modules`` but only re-syncs the attribute on EXIT. Forcing the
    fresh import inside the window rebinds both to the real-SDK module for
    the duration; the helper's restore puts the stub copy back after.
    """
    with _real_mcp_modules():
        import importlib

        importlib.import_module("vesma.mcp_server")
        yield


def _base_settings(tmp: Path, *, endpoint_enabled: bool = True) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        embedding={"provider": "onnx"},
        # Scanner off: no daemon thread per test (same rationale as
        # test_api.py's fixture).
        scanner={"enabled": False},
        mcp={"endpoint_enabled": endpoint_enabled},
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def mcp_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Settings, Path]]:
    """Deterministic instance id + an isolated tmp home, yielded with its tmpdir."""
    monkeypatch.delenv("VESMA_INSTANCE_ID", raising=False)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        yield _base_settings(tmp), tmp


@dataclass
class McpHarness:
    """One test app instance: the client, its settings/manager and its token."""

    client: TestClient
    settings: Settings
    manager: MemoryManager

    def issue_token(self, *, agent: str = "test-harness", node: str | None = None) -> str:
        """Mint the signing key at the settings path; issue one rw token.

        ``node`` defaults to the instance id the endpoint gate validates
        against (resolved by the SAME read-only resolver the lifespan
        uses, so the aud binding matches whatever the home resolves to).
        """
        key = load_or_create_signing_key(signing_key_path(self.settings.vesma.data_dir))
        aud = node if node is not None else current_instance_id(self.settings.vesma.data_dir)
        store = AgentTokenStore(self.settings.db_path)
        try:
            token, _claims = issue_agent_token(
                agent_id=agent,
                node_id=aud,
                scope_spec="rw",
                store=store,
                key=key,
            )
        finally:
            store.close()
        return token


def _build_harness(settings: Settings) -> McpHarness:
    """Fresh FastAPI app + TestClient wired to an isolated MemoryManager."""
    mgr = MemoryManager(settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder

    test_app = FastAPI(title="Mnemos-Test", version="0.1.0", lifespan=lifespan)
    # Copy all routes from the real app (the /mcp Mount included) and the
    # exception handlers registered on it (rate limiting, TagContract → 422).
    for route in app.routes:
        test_app.routes.append(route)
    test_app.exception_handlers.update(app.exception_handlers)
    # Route copies do NOT carry the middleware stack (add_middleware lives
    # on the instance) — mirror the production chain explicitly so the
    # /mcp tests exercise the REAL ordering: AuthMiddleware bypasses /mcp
    # (the mounted gate is the only auth there) and the instance-id
    # header middleware stamps mounted responses too.
    test_app.add_middleware(AuthMiddleware)
    test_app.add_middleware(VitalsVerbMiddleware)
    test_app.add_middleware(DeprecationMiddleware)
    test_app.add_middleware(InstanceIdHeaderMiddleware)

    # Both manager singletons must see the isolated store: the REST
    # lifespan resolves through api.main.get_manager(), the MCP tool
    # dispatch through vesma.mcp_server.get_manager().
    api_main._manager = mgr
    from vesma import mcp_server as mcp_server_module

    mcp_server_module._manager = mgr
    return McpHarness(client=TestClient(test_app), settings=settings, manager=mgr)


@pytest.fixture
def client(mcp_env: tuple[Settings, Path]) -> Iterator[McpHarness]:
    """Enabled MCP plane; restores both manager singletons after the test."""
    settings, _tmp = mcp_env
    harness = _build_harness(settings)
    with harness.client:
        yield harness
    _teardown_managers()


@pytest.fixture
def disabled_client(mcp_env: tuple[Settings, Path]) -> Iterator[McpHarness]:
    """mcp.endpoint_enabled=false — the 404 posture (same tmp home)."""
    _settings, tmp = mcp_env
    harness = _build_harness(_base_settings(tmp, endpoint_enabled=False))
    with harness.client:
        yield harness
    _teardown_managers()


def _teardown_managers() -> None:
    from vesma import mcp_server as mcp_server_module

    mcp_server_module._manager = None
    api_main._manager = None


# ---------------------------------------------------------------------------
# Request helpers (MCP JSON-RPC over HTTP, JSON response mode)
# ---------------------------------------------------------------------------

_MCP_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "mcp-protocol-version": "2025-06-18",
}

_HANDSHAKE_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}


def _rpc(method: str, params: dict[str, Any] | None = None, id_: int = 1) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _post(client: McpHarness, token: str | None, body: dict[str, Any]) -> Any:
    headers = dict(_MCP_HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.client.post("/mcp", headers=headers, json=body)


# ---------------------------------------------------------------------------
# Transport contracts
# ---------------------------------------------------------------------------


class TestMcpHttpContracts:
    def test_initialize_handshake_over_http(self, client: McpHarness) -> None:
        """initialize completes over HTTP and answers the vesma serverInfo."""
        resp = _post(
            client,
            client.issue_token(),
            _rpc(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "contract-test", "version": "0.0.1"},
                },
            ),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["jsonrpc"] == "2.0" and data["id"] == 1
        result = data["result"]
        assert result["serverInfo"]["name"] == "vesma"
        assert result["serverInfo"]["version"]
        assert result["protocolVersion"] in _HANDSHAKE_PROTOCOL_VERSIONS

    def test_tools_list_returns_canonical_set(self, client: McpHarness) -> None:
        resp = _post(client, client.issue_token(), _rpc("tools/list", {}))
        assert resp.status_code == 200
        tools = resp.json()["result"]["tools"]
        names = {t["name"] for t in tools}
        # Canonical tool surface — the same registry the stdio leg serves.
        for expected in (
            "vesma_add",
            "vesma_search",
            "vesma_recall_context",
            "vesma_save_context",
            "vesma_list_recent",
            "vesma_list_tags",
            "vesma_stats",
            "vesma_assemble_context",
        ):
            assert expected in names, f"{expected} missing from tools/list over HTTP"
        for tool in tools:
            assert tool["description"]
            assert "inputSchema" in tool

    def test_tools_call_stats_json_parses(self, client: McpHarness) -> None:
        resp = _post(
            client,
            client.issue_token(),
            _rpc("tools/call", {"name": "vesma_stats", "arguments": {}}),
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert "error" not in payload
        text = payload["result"]["content"][0]["text"]
        stats = json.loads(text)
        assert isinstance(stats, dict)
        assert "total" in stats

    def test_tools_call_list_tags_on_seeded_store(self, client: McpHarness) -> None:
        client.manager.add(
            MemoryCreate(
                content="http-leg smoke memory",
                tags=["project:mcp-w1", "agent:qa", "vesma:note"],
            ),
            project="mcp-w1",
            agent="qa",
        )
        resp = _post(
            client,
            client.issue_token(),
            _rpc("tools/call", {"name": "vesma_list_tags", "arguments": {}}),
        )
        assert resp.status_code == 200
        text = resp.json()["result"]["content"][0]["text"]
        tags = json.loads(text)
        # vesma_list_tags mirrors mgr.list_tags(): a {tag: count} mapping.
        assert tags.get("project:mcp-w1") == 1

    def test_instance_id_header_stamps_mcp_responses(self, client: McpHarness) -> None:
        """The ADR-0049 instance header middleware covers the mounted /mcp too."""
        resp = _post(client, client.issue_token(), _rpc("tools/list", {}))
        assert resp.status_code == 200
        instance_id = resp.headers.get("X-Vesma-Instance-Id")
        assert instance_id
        # The same id the gate validates tokens against (aud) — a harness
        # discovers it from the header, then issues a token bound to it.
        assert instance_id == current_instance_id(client.settings.vesma.data_dir)


# ---------------------------------------------------------------------------
# Auth negatives (ADR-0050 §3)
# ---------------------------------------------------------------------------


class TestMcpHttpAuthNegatives:
    def test_missing_token_is_401_on_loopback_trust_api(self, client: McpHarness) -> None:
        """Loopback trust of the REST API does NOT extend to /mcp.

        The fixture's app runs api.auth_enabled=False on a loopback bind
        (every REST route would admit this request) — /mcp still refuses
        a tokenless caller with 401.
        """
        resp = _post(client, None, _rpc("tools/list", {}))
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"
        assert "detail" in resp.json()

    def test_garbage_token_is_401(self, client: McpHarness) -> None:
        resp = _post(client, "not-a-valid-agent-token", _rpc("tools/list", {}))
        assert resp.status_code == 401
        assert "rejected" in resp.json()["detail"]

    def test_foreign_node_binding_is_401(self, client: McpHarness) -> None:
        """A validly signed token bound to another node (aud) is refused."""
        resp = _post(
            client,
            client.issue_token(node="some-other-gateway"),
            _rpc("tools/list", {}),
        )
        assert resp.status_code == 401
        assert "aud_mismatch" in resp.json()["detail"]

    def test_non_bearer_scheme_is_401(self, client: McpHarness) -> None:
        resp = client.client.post(
            "/mcp",
            headers={**_MCP_HEADERS, "Authorization": "Basic dXNlcjpwYXNz"},
            json=_rpc("tools/list", {}),
        )
        assert resp.status_code == 401


class TestMcpHttpDisabled:
    def test_disabled_endpoint_answers_404_even_with_valid_token(
        self, disabled_client: McpHarness
    ) -> None:
        resp = _post(disabled_client, disabled_client.issue_token(), _rpc("tools/list", {}))
        assert resp.status_code == 404
        assert "disabled" in resp.json()["detail"]

    def test_disabled_endpoint_hides_auth_state(self, disabled_client: McpHarness) -> None:
        """404 for a tokenless caller too — a disabled surface leaks nothing."""
        resp = _post(disabled_client, None, _rpc("tools/list", {}))
        assert resp.status_code == 404
