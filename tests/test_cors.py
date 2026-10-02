"""Tests for CORS middleware configuration.

Covers:
  - Default: no CORS headers in response (strict default, cors_enabled=False)
  - Configured allow-list: allowed origin gets Access-Control-Allow-Origin
  - Configured allow-list: disallowed origin gets no CORS headers
  - Preflight OPTIONS works for an allowed origin
  - allow_origins=["*"] + allow_credentials=True raises ValueError at setup time
  - allow_origins=["*"] without credentials is accepted (public-read API)
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vesmaro.api import main as api_main
from vesmaro.api.main import _setup_cors, app, lifespan
from vesmaro.config import Settings
from vesmaro.manager import MemoryManager

_ALLOWED = "http://localhost:5173"
_DISALLOWED = "http://evil.example.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(tmp: Path, **api_kwargs: object) -> Settings:
    """Build an isolated Settings object pointing at a temp directory."""
    s = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        embedding={"provider": "onnx"},
        api=api_kwargs,
        # Scanner is exercised in test_scanner.py — disable it here so
        # the API lifespan does not spawn a daemon thread per test
        # (defence-in-depth against the singleton thread leak).
        scanner={"enabled": False},
    )
    s.resolve_paths()
    return s


@contextmanager
def _client(settings: Settings) -> Iterator[TestClient]:
    """Context manager that yields a TestClient backed by an isolated manager.

    CORSMiddleware is added to test_app BEFORE TestClient starts (before
    the first __call__ builds the middleware stack).  Starlette raises
    RuntimeError if add_middleware is called after startup.
    """
    mgr = MemoryManager(settings)
    mock_emb = MagicMock()
    mock_emb.embed.return_value = [0.1] * 384
    mgr._embedder = mock_emb

    test_app = FastAPI(title="Mnemos-CORS-Test", version="0.1.0", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)

    # Configure CORS before the app starts (before first __call__).
    _setup_cors(test_app, settings)

    api_main._manager = mgr
    try:
        with TestClient(test_app) as tc:
            yield tc
    finally:
        mgr.close()
        api_main._manager = None


# ---------------------------------------------------------------------------
# Default behavior - CORS disabled
# ---------------------------------------------------------------------------


class TestCorsDefault:
    def test_no_cors_headers_by_default(self) -> None:
        """Default settings (cors_enabled=False): no CORS header is emitted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _settings(Path(tmpdir))
            with _client(settings) as client:
                resp = client.get("/health", headers={"Origin": _ALLOWED})
                assert resp.status_code == 200
                assert "access-control-allow-origin" not in resp.headers

    def test_preflight_no_cors_header_by_default(self) -> None:
        """Preflight OPTIONS with default settings returns no CORS Allow-Origin."""
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _settings(Path(tmpdir))
            with _client(settings) as client:
                resp = client.options(
                    "/health",
                    headers={
                        "Origin": _ALLOWED,
                        "Access-Control-Request-Method": "GET",
                    },
                )
                assert "access-control-allow-origin" not in resp.headers

    def test_cors_enabled_but_empty_origins_no_header(self) -> None:
        """cors_enabled=True with cors_allow_origins=[] is equivalent to disabled."""
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _settings(
                Path(tmpdir),
                cors_enabled=True,
                cors_allow_origins=[],
            )
            with _client(settings) as client:
                resp = client.get("/health", headers={"Origin": _ALLOWED})
                assert "access-control-allow-origin" not in resp.headers


# ---------------------------------------------------------------------------
# Configured allow-list
# ---------------------------------------------------------------------------


class TestCorsAllowList:
    def test_allowed_origin_gets_header(self) -> None:
        """Origin in the allow-list receives Access-Control-Allow-Origin."""
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _settings(
                Path(tmpdir),
                cors_enabled=True,
                cors_allow_origins=[_ALLOWED],
            )
            with _client(settings) as client:
                resp = client.get("/health", headers={"Origin": _ALLOWED})
                assert resp.status_code == 200
                assert resp.headers["access-control-allow-origin"] == _ALLOWED

    def test_disallowed_origin_no_header(self) -> None:
        """Origin NOT in the allow-list never receives Access-Control-Allow-Origin."""
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _settings(
                Path(tmpdir),
                cors_enabled=True,
                cors_allow_origins=[_ALLOWED],
            )
            with _client(settings) as client:
                resp = client.get("/health", headers={"Origin": _DISALLOWED})
                assert resp.status_code == 200
                assert "access-control-allow-origin" not in resp.headers

    def test_preflight_allowed_origin(self) -> None:
        """Preflight OPTIONS for an allowed origin returns 200 with CORS headers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _settings(
                Path(tmpdir),
                cors_enabled=True,
                cors_allow_origins=[_ALLOWED],
            )
            with _client(settings) as client:
                resp = client.options(
                    "/health",
                    headers={
                        "Origin": _ALLOWED,
                        "Access-Control-Request-Method": "GET",
                    },
                )
                assert resp.status_code == 200
                assert resp.headers["access-control-allow-origin"] == _ALLOWED

    def test_multiple_origins_both_allowed(self) -> None:
        """Multiple origins in the allow-list are each permitted individually."""
        second_origin = "http://app.local:3000"
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _settings(
                Path(tmpdir),
                cors_enabled=True,
                cors_allow_origins=[_ALLOWED, second_origin],
            )
            with _client(settings) as client:
                for origin in (_ALLOWED, second_origin):
                    resp = client.get("/health", headers={"Origin": origin})
                    assert resp.headers["access-control-allow-origin"] == origin


# ---------------------------------------------------------------------------
# Security: wildcard + credentials is forbidden
# ---------------------------------------------------------------------------


class TestCorsWildcardCredentials:
    def test_wildcard_plus_credentials_raises_at_setup(self) -> None:
        """allow_origins=['*'] + allow_credentials=True raises ValueError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _settings(
                Path(tmpdir),
                cors_enabled=True,
                cors_allow_origins=["*"],
                cors_allow_credentials=True,
            )
            test_app = FastAPI(title="T", version="0.1.0")
            with pytest.raises(ValueError, match="allow_credentials"):
                _setup_cors(test_app, settings)

    def test_wildcard_without_credentials_is_accepted(self) -> None:
        """allow_origins=['*'] with allow_credentials=False is valid (public read API)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _settings(
                Path(tmpdir),
                cors_enabled=True,
                cors_allow_origins=["*"],
                cors_allow_credentials=False,
            )
            test_app = FastAPI(title="T", version="0.1.0")
            # Must not raise - wildcard without credentials is a valid CORS pattern
            _setup_cors(test_app, settings)
