"""Regression tests for the AuthMiddleware bypass of the federation pull
endpoint (MNEMOS-AUTH-BYPASS-FIX).

Background
----------
The federation mediated-pull endpoint ``POST /api/v1/federation/pull`` is
authenticated by a **per-peer bearer token** (``mnk_fed_<peer_id>_*``,
ADR-0016) verified inside :func:`vesma.federation_server.handle_pull`.
The global operator session (TOTP/api-key) enforced by
:class:`vesma.api.middleware.AuthMiddleware` is a *different* auth layer
— it guards the operator API surface, not server-to-server federation.

Before this fix, when ``api.auth_enabled=true`` the middleware returned
401 on ``/api/v1/federation/pull`` before ``handle_pull`` ever ran, so
federation pull was broken in any deployment that enables operator auth
(e.g. AgentsNode). The existing test suite in
``tests/test_federation_server.py`` calls ``handle_pull`` directly,
bypassing the middleware — which is why the bug was not caught.

These tests exercise the **full middleware stack** via ``TestClient`` so
the bypass is verified end-to-end:

* AC1 — pull with a valid per-peer bearer, no operator session, returns
  200 (not 401) when ``auth_enabled=true``.
* AC2 — pull with no per-peer bearer returns 401/403 (``handle_pull``
  still rejects — the bypass is fail-closed).
* AC3 — ``access_log_path`` in :class:`FederationConfig` is honoured by
  :func:`vesma.api.federation.get_access_log` (custom path used, not
  the default ``~/.mnemos/logs/federation-access.jsonl``).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vesma.api.main as api_main
from vesma.api.federation import get_access_log
from vesma.api.main import lifespan
from vesma.api.middleware import AuthMiddleware
from vesma.config import FederationConfig, PeerConfig, Settings
from vesma.federation_access_log import FederationAccessLog
from vesma.manager import MemoryManager
from vesma.models import MemoryCreate, MemoryStatus
from vesma.trigger_codes import TriggerCode

# RFC-reserved constants — never real credentials.
PEER_A = "mnemos-A"
PROJECT = "project-mnemos"
TOKEN_ENV = "VESMA_FED_PEER_VESMARO_A_TOKEN"
TOKEN_VALUE = "mnk_fed_mnemos-A_exampletoken123"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def fed_settings(tmp_path: Path) -> Settings:
    """Settings with auth_enabled=True and one configured federation peer."""
    os.environ[TOKEN_ENV] = TOKEN_VALUE
    settings = Settings(
        vesma={
            "vault_path": str(tmp_path / "vault"),
            "data_dir": str(tmp_path / "data"),
            "db_name": "fed-bypass.db",
        },
        embedding={"provider": "onnx"},
        scanner={"enabled": False},
        api={
            "host": "127.0.0.1",
            "auth_enabled": True,
        },
        federation={
            "shared_projects": [PROJECT],
            "peers": {
                PEER_A: PeerConfig(
                    bearer_token_env=TOKEN_ENV,
                    allowed_projects=[PROJECT],
                    allowed_types=["decision", "learning"],
                    rate_limit_per_minute=10,
                ),
            },
        },
    )
    settings.resolve_paths()
    settings.vesma.data_dir.mkdir(parents=True, exist_ok=True)
    return settings


def _make_app(settings: Settings) -> tuple[FastAPI, MemoryManager]:
    """Build a FastAPI app with AuthMiddleware wired, auth_enabled=True.

    Mirrors ``tests/test_auth_security.py::_make_app`` but pins
    ``auth_enabled=True`` and a non-loopback host so the middleware's
    operator-session path is the one under test.
    """
    mgr = MemoryManager(settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder

    test_app = FastAPI(title="FedBypassTest", version="0.0.1", lifespan=lifespan)
    for route in api_main.app.routes:
        test_app.routes.append(route)
    test_app.add_middleware(AuthMiddleware)

    api_main._manager = mgr
    # Wire the federation router's settings + manager + access-log overrides
    # so handle_pull uses the test peer config (not load_settings() defaults).
    # lifespan does not set these, so they persist through startup.
    test_app.state.federation_settings = settings
    test_app.state.federation_manager = mgr
    test_app.state.federation_access_log = FederationAccessLog(
        Path(tempfile.mkdtemp()) / "fed-access.jsonl"
    )
    return test_app, mgr


def _cleanup(mgr: MemoryManager) -> None:
    mgr.close()
    api_main._manager = None


def _add_published_memory(mgr: MemoryManager) -> None:
    """Add a clean decision memory so the pull returns at least one record."""
    mem = mgr.add(
        MemoryCreate(
            content=(
                "Adopted per-peer bearer tokens for federation pull, "
                "verified inside handle_pull. Middleware bypasses the "
                "operator session check on the pull endpoint."
            ),
            title="Federation auth bypass decision",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
        ),
        project=PROJECT,
        agent="gcw-tech-lead",
    )
    mgr.sqlite.update_status(mem.id, MemoryStatus.PUBLISHED)


def _pull_body(
    *,
    peer_id: str = PEER_A,
    query: str = "federation auth bypass",
    project_scope: str = PROJECT,
) -> dict:
    return {"peer_id": peer_id, "query": query, "project_scope": project_scope}


# ── AC1 + AC2: middleware bypass regression ──────────────────────────────────


class TestFederationPullMiddlewareBypass:
    """The pull endpoint must pass AuthMiddleware when auth_enabled=true."""

    def test_pull_with_per_peer_bearer_returns_200(self, fed_settings: Settings) -> None:
        """AC1: valid per-peer bearer, no operator session → 200.

        Before the fix this returned 401 from AuthMiddleware because
        ``/api/v1/federation/pull`` was not in the bypass list and the
        peer bearer is not an operator session token.
        """
        test_app, mgr = _make_app(fed_settings)
        _add_published_memory(mgr)
        try:
            with TestClient(test_app) as tc:
                r = tc.post(
                    "/api/v1/federation/pull",
                    json=_pull_body(),
                    headers={"Authorization": f"Bearer {TOKEN_VALUE}"},
                )
            assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
            body = r.json()
            assert body["trigger_code"] == TriggerCode.EXHAUSTIVE.value
            assert len(body["records"]) >= 1
        finally:
            _cleanup(mgr)

    def test_pull_without_bearer_rejected_by_handle_pull(self, fed_settings: Settings) -> None:
        """AC2: no per-peer bearer → handle_pull rejects (401/403).

        The middleware bypass lets the request reach ``handle_pull``,
        which then fail-closed rejects it because the per-peer bearer is
        missing. This proves the bypass does NOT open an unauthenticated
        hole — ``handle_pull`` is the auth layer for federation.
        """
        test_app, mgr = _make_app(fed_settings)
        _add_published_memory(mgr)
        try:
            with TestClient(test_app) as tc:
                r = tc.post("/api/v1/federation/pull", json=_pull_body())
            # handle_pull returns 403 REFUSED for a missing token; the
            # exact status is owned by the federation server, the key
            # assertion is that the middleware did NOT short-circuit to
            # 401 with '{"detail":"Authentication required"}'.
            assert r.status_code in (401, 403), (
                f"expected 401/403 from handle_pull, got {r.status_code}: {r.text}"
            )
            assert "Authentication required" not in r.text, (
                "middleware short-circuited with the operator-auth 401 — the bypass is not working"
            )
        finally:
            _cleanup(mgr)

    def test_pull_with_wrong_bearer_rejected_by_handle_pull(self, fed_settings: Settings) -> None:
        """AC2 (negative): wrong per-peer bearer → handle_pull rejects.

        Confirms the bypass only skips the operator-session check; the
        per-peer bearer is still verified inside ``handle_pull``.
        """
        test_app, mgr = _make_app(fed_settings)
        _add_published_memory(mgr)
        try:
            with TestClient(test_app) as tc:
                r = tc.post(
                    "/api/v1/federation/pull",
                    json=_pull_body(),
                    headers={"Authorization": "Bearer mnk_fed_mnemos-A_wrong"},
                )
            assert r.status_code in (401, 403), f"expected 401/403, got {r.status_code}: {r.text}"
            body = r.json()
            # Non-200 responses wrap the PullResponse in HTTPException.detail.
            detail = body.get("detail", body)
            assert detail["trigger_code"] == TriggerCode.REFUSED.value
        finally:
            _cleanup(mgr)


# ── AC3: access_log_path honoured ─────────────────────────────────────────────


class TestFederationAccessLogPath:
    """``FederationConfig.access_log_path`` overrides the default log path."""

    def test_custom_access_log_path_used_by_get_access_log(self, tmp_path: Path) -> None:
        """AC3: when ``access_log_path`` is set, ``get_access_log`` uses it.

        We build a minimal FastAPI app (no lifespan DB) and attach a
        Settings object on ``app.state.federation_settings`` — the same
        override hook ``get_access_log`` checks. The module singleton is
        reset so the configured path is re-read.
        """
        import vesma.api.federation as fed_mod

        custom_path = tmp_path / "data" / "federation-access.jsonl"
        settings = Settings(
            vesma={
                "vault_path": str(tmp_path / "vault"),
                "data_dir": str(tmp_path / "data"),
                "db_name": "fed-logpath.db",
            },
            embedding={"provider": "onnx"},
            scanner={"enabled": False},
            federation=FederationConfig(
                shared_projects=[PROJECT],
                access_log_path=str(custom_path),
            ),
        )
        settings.resolve_paths()

        # Reset the module singleton so get_access_log re-resolves.
        fed_mod._access_log = None

        test_app = FastAPI(title="LogPathTest", version="0.0.1")
        test_app.state.federation_settings = settings

        # Build a minimal request-like object — get_access_log only reads
        # app.state.federation_access_log and app.state.federation_settings.
        class _State:
            federation_access_log = None
            federation_settings = settings

        class _App:
            state = _State()

        class _Request:
            app = _App()

        log = get_access_log(cast(Any, _Request()))
        assert log.path == custom_path, f"expected custom path {custom_path}, got {log.path}"

    def test_default_access_log_path_when_unset(self, tmp_path: Path) -> None:
        """AC3 (default): when ``access_log_path`` is None, the default is used."""
        import vesma.api.federation as fed_mod
        from vesma.federation_access_log import DEFAULT_LOG_PATH

        settings = Settings(
            vesma={
                "vault_path": str(tmp_path / "vault"),
                "data_dir": str(tmp_path / "data"),
                "db_name": "fed-logpath-default.db",
            },
            embedding={"provider": "onnx"},
            scanner={"enabled": False},
            federation=FederationConfig(shared_projects=[PROJECT]),
        )
        settings.resolve_paths()

        fed_mod._access_log = None

        class _State:
            federation_access_log = None
            federation_settings = settings

        class _App:
            state = _State()

        class _Request:
            app = _App()

        log = get_access_log(cast(Any, _Request()))
        assert log.path == DEFAULT_LOG_PATH.expanduser(), (
            f"expected default {DEFAULT_LOG_PATH.expanduser()}, got {log.path}"
        )

    def test_custom_path_writes_log_to_configured_location(self, tmp_path: Path) -> None:
        """AC3 (write): an append through the configured-path log lands on disk."""
        from datetime import UTC, datetime

        from vesma.federation_access_log import AccessLogEntry

        custom_path = tmp_path / "data" / "federation-access.jsonl"
        log = FederationAccessLog(custom_path)
        entry = AccessLogEntry(
            peer_id=PEER_A,
            topic_hash="a" * 64,
            timestamp=datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC),
            project_scope=PROJECT,
            trigger_code=TriggerCode.EXHAUSTIVE,
            record_ids_accessed=["rec-1"],
        )
        log.append(entry)
        assert custom_path.exists(), "log file was not created at the custom path"
        text = custom_path.read_text(encoding="utf-8").strip()
        assert "rec-1" in text
        assert TriggerCode.EXHAUSTIVE.value in text
