"""Deprecation-alias middleware tests (card vesma-rest-deprecation-middleware).

Verifies the migration wiring: legacy root API paths carry
``Deprecation`` + ``Link`` headers pointing at the canonical ``/api/v1``
location, canonical / canonical-service paths stay clean, and the light
per-template hit counter grows on legacy hits only.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vesma.api import main as api_main
from vesma.api.main import app, lifespan
from vesma.api.middleware import (
    _LEGACY_ROOT_PATHS,
    DeprecationMiddleware,
    get_deprecation_hits,
    reset_deprecation_hits,
)
from vesma.config import Settings
from vesma.manager import MemoryManager


def _settings(tmp: Path) -> Settings:
    settings = Settings(
        mnemos={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        # Scanner disabled — the API lifespan must not spawn a daemon
        # scanner thread per test (defence-in-depth, see test_api.py).
        scanner={"enabled": False},
    )
    settings.resolve_paths()
    return settings


@pytest.fixture()
def client(tmp_path: Path):
    reset_deprecation_hits()
    mgr = MemoryManager(_settings(tmp_path))
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    # Fresh app per test: copy the real routes, register ONLY the
    # deprecation boundary (no auth/vitals under test here).
    test_app = FastAPI(title="Deprecation-Test", version="0.0.0", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)
    test_app.add_middleware(DeprecationMiddleware)
    api_main._manager = mgr
    with TestClient(test_app) as tc:
        yield tc
    mgr.close()
    api_main._manager = None
    reset_deprecation_hits()


class TestLegacyAliasHeaders:
    def test_legacy_root_path_gets_deprecation_and_link(self, client: TestClient) -> None:
        resp = client.get("/memories")
        assert resp.status_code == 200
        assert resp.headers["Deprecation"] == "true"
        assert resp.headers["Link"] == '</api/v1/memories>; rel="suggested-version"'

    def test_legacy_parameterised_route_matched_by_template(self, client: TestClient) -> None:
        resp = client.get(f"/memories/{uuid.uuid4()}")
        assert resp.status_code == 404
        assert resp.headers["Deprecation"] == "true"
        assert resp.headers["Link"] == '</api/v1/memories/{memory_id}>; rel="suggested-version"'


class TestCleanPaths:
    def test_canonical_api_v1_path_stays_clean(self, client: TestClient) -> None:
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        assert "Deprecation" not in resp.headers
        assert "Link" not in resp.headers

    def test_health_stays_clean(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert "Deprecation" not in resp.headers
        assert "Link" not in resp.headers

    def test_unmatched_404_stays_clean(self, client: TestClient) -> None:
        # No route match → no ``scope["route"]`` → no decoration (the raw
        # concrete path must never be used as a fallback template).
        resp = client.get("/definitely-not-a-vesma-route")
        assert resp.status_code == 404
        assert "Deprecation" not in resp.headers
        assert "Link" not in resp.headers
        assert get_deprecation_hits() == {}

    def test_method_not_allowed_on_legacy_path_is_decorated(self, client: TestClient) -> None:
        # A 405 still resolves to the matched route template — the response
        # was served at the legacy location, so it carries the hint.
        resp = client.put("/memories")
        assert resp.status_code == 405
        assert resp.headers["Deprecation"] == "true"
        assert resp.headers["Link"] == '</api/v1/memories>; rel="suggested-version"'


class TestHitCounter:
    def test_hit_rate_grows_per_template(self, client: TestClient) -> None:
        before = get_deprecation_hits()
        assert client.get("/memories").status_code == 200
        assert client.get("/tags").status_code == 200
        after = get_deprecation_hits()
        assert after["/memories"] == before.get("/memories", 0) + 1
        assert after["/tags"] == before.get("/tags", 0) + 1

    def test_clean_paths_are_not_counted(self, client: TestClient) -> None:
        assert client.get("/api/v1/stats").status_code == 200
        assert client.get("/health").status_code == 200
        assert get_deprecation_hits() == {}


class TestLegacyPathSet:
    def test_health_is_never_deprecated(self) -> None:
        assert "/health" not in _LEGACY_ROOT_PATHS

    def test_no_canonical_or_separate_surface_members(self) -> None:
        for member in _LEGACY_ROOT_PATHS:
            assert not member.startswith("/api/v1")
            # /v1 = A2A Sessions API (M16) — separate versioned surface.
            assert not member.startswith("/v1")
            # /auth = T-AUTH surface (ADR-0014) — no /api/v1 counterpart.
            assert not member.startswith("/auth")

    def test_representative_members_present(self) -> None:
        for member in (
            "/metrics",
            "/memories",
            "/search",
            "/context/recall",
            "/graph/projects",
        ):
            assert member in _LEGACY_ROOT_PATHS
