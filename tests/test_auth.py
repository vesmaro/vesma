"""Tests for T-AUTH: token lifecycle, TOTP flow, session management.

Covers the happy-path contract from ADR-0014:
  - Bearer token creation and hash storage
  - Login (no TOTP) → session token
  - Login (TOTP enrolled) → challenge
  - Verify TOTP → session + cookie
  - Logout → session cleared
  - /auth/me → token metadata
  - Loopback bypass (auth_enabled=False, loopback host)
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pyotp  # type: ignore[import-untyped]
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

import vesma.api.main as api_main
from vesma.api.auth import decrypt_totp_secret, encrypt_totp_secret
from vesma.api.auth_store import AuthStore, hash_token
from vesma.api.main import app, lifespan
from vesma.api.middleware import AuthMiddleware
from vesma.config import Settings
from vesma.manager import MemoryManager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def tmp_settings(tmp_dir):
    settings = Settings(
        vesma={
            "vault_path": str(tmp_dir / "vault"),
            "data_dir": str(tmp_dir / "data"),
            "db_name": "test.db",
        },
        embedding={"provider": "onnx"},
        # Scanner is exercised in test_scanner.py — disable it here so
        # the API lifespan does not spawn a daemon thread per test
        # (defence-in-depth against the singleton thread leak).
        scanner={"enabled": False},
    )
    settings.resolve_paths()
    settings.vesma.data_dir.mkdir(parents=True, exist_ok=True)
    return settings


@pytest.fixture
def auth_store(tmp_settings):
    store = AuthStore(tmp_settings.db_path)
    yield store
    store.close()


@pytest.fixture
def client_with_auth(tmp_settings):
    """Full TestClient with AuthMiddleware wired, auth_enabled=False (loopback mode)."""
    mgr = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder

    test_app = FastAPI(title="Mnemos-Auth-Test", version="0.0.1", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)
    test_app.add_middleware(AuthMiddleware)

    api_main._manager = mgr
    with TestClient(test_app) as tc:
        yield tc
    mgr.close()
    api_main._manager = None


@pytest.fixture
def client_auth_enabled(tmp_settings, tmp_dir):
    """TestClient with auth_enabled=True, using a dedicated in-memory AuthStore."""
    from vesma.api.middleware import AuthMiddleware

    mgr = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder

    # Patch settings to enable auth on a "loopback" host so startup guard passes
    tmp_settings.api.auth_enabled = True
    tmp_settings.api.totp_enabled = False
    tmp_settings.api.behind_tls_proxy = False  # loopback, so guard ignores these

    test_app = FastAPI(title="Mnemos-Auth-Enabled-Test", version="0.0.1", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)
    test_app.add_middleware(AuthMiddleware)

    api_main._manager = mgr
    with TestClient(test_app, raise_server_exceptions=True) as tc:
        # Expose auth_store directly so tests can mint tokens
        yield tc
    mgr.close()
    api_main._manager = None


# ---------------------------------------------------------------------------
# AuthStore unit tests
# ---------------------------------------------------------------------------


class TestAuthStore:
    def test_create_token_format(self, auth_store):
        token_id, plaintext = auth_store.create_token(name="test-key")
        assert plaintext.startswith("mnk_")
        assert len(plaintext) > 10
        assert token_id.startswith("tid_")

    def test_token_hash_only_stored(self, auth_store):
        """The plaintext must NOT appear anywhere in the DB row."""
        token_id, plaintext = auth_store.create_token()
        row = auth_store.get_token_by_id(token_id)
        assert row is not None
        for v in row.values():
            if isinstance(v, str):
                assert plaintext not in v

    def test_lookup_by_hash(self, auth_store):
        token_id, plaintext = auth_store.create_token(name="lookup-test")
        sha256 = hash_token(plaintext)
        row = auth_store.get_token_by_hash(sha256)
        assert row is not None
        assert str(row["token_id"]) == token_id

    def test_revoke_permanent(self, auth_store):
        token_id, _ = auth_store.create_token()
        assert auth_store.revoke_token(token_id) is True
        row = auth_store.get_token_by_id(token_id)
        assert row is not None
        assert not auth_store.is_token_active(row)

    def test_login_lockout(self, auth_store):
        token_id, _ = auth_store.create_token()
        for _ in range(10):
            auth_store.increment_login_failure(token_id)
        row = auth_store.get_token_by_id(token_id)
        assert row is not None
        assert not auth_store.is_token_active(row)

    def test_totp_lockout(self, auth_store):
        token_id, _ = auth_store.create_token()
        for _ in range(3):
            auth_store.increment_totp_failure(token_id)
        row = auth_store.get_token_by_id(token_id)
        assert row is not None
        assert not auth_store.is_token_active(row)

    def test_reset_failures(self, auth_store):
        token_id, _ = auth_store.create_token()
        auth_store.increment_login_failure(token_id)
        auth_store.reset_failures(token_id)
        row = auth_store.get_token_by_id(token_id)
        assert row is not None
        assert int(str(row["failure_count"])) == 0

    def test_challenge_lifecycle(self, auth_store):
        token_id, _ = auth_store.create_token()
        cid = auth_store.create_challenge(token_id)
        challenge = auth_store.get_challenge(cid)
        assert challenge is not None
        assert auth_store.is_challenge_valid(challenge)
        auth_store.invalidate_challenge(cid)
        assert auth_store.get_challenge(cid) is None

    def test_session_lifecycle(self, auth_store):
        token_id, _ = auth_store.create_token()
        plaintext, _expires = auth_store.create_session(token_id, ttl_sec=3600)
        sha256 = hash_token(plaintext)
        session = auth_store.get_session_by_hash(sha256)
        assert session is not None
        assert auth_store.is_session_valid(session)
        auth_store.revoke_session(sha256)
        assert auth_store.get_session_by_hash(sha256) is None


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------


class TestCrypto:
    def test_encrypt_decrypt_roundtrip(self):
        secret = pyotp.random_base32(32)
        master_key = "test-master-key-for-unit-tests-only"
        encrypted = encrypt_totp_secret(secret, master_key)
        assert isinstance(encrypted, bytes)
        decrypted = decrypt_totp_secret(encrypted, master_key)
        assert decrypted == secret

    def test_wrong_key_returns_none(self):
        secret = pyotp.random_base32(32)
        encrypted = encrypt_totp_secret(secret, "key-a")
        result = decrypt_totp_secret(encrypted, "key-b")
        assert result is None


# ---------------------------------------------------------------------------
# API endpoint tests (auth_enabled=False, loopback — no auth required)
# ---------------------------------------------------------------------------


class TestAuthEndpoints:
    """Tests for /auth/* endpoints with auth_enabled=False (loopback bypass mode)."""

    def _make_client(self, tmp_settings):
        mgr = MemoryManager(tmp_settings)
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        mgr._embedder = mock_embedder

        test_app = FastAPI(title="T", version="0.0.1", lifespan=lifespan)
        for route in app.routes:
            test_app.routes.append(route)
        test_app.add_middleware(AuthMiddleware)
        api_main._manager = mgr
        return TestClient(test_app), mgr

    def test_login_invalid_token_format(self, tmp_settings):
        tc, mgr = self._make_client(tmp_settings)
        with tc:
            r = tc.post("/auth/login", json={"token": "bad_no_prefix"})
        mgr.close()
        api_main._manager = None
        assert r.status_code == 401

    def test_login_unknown_token(self, tmp_settings):
        tc, mgr = self._make_client(tmp_settings)
        with tc:
            r = tc.post("/auth/login", json={"token": "mnk_" + "a" * 43})
        mgr.close()
        api_main._manager = None
        assert r.status_code == 401

    def test_login_no_totp_returns_session(self, tmp_settings):
        """Login with a valid token (no TOTP enrolled) → session immediately."""
        tc, mgr = self._make_client(tmp_settings)
        with tc:
            store: AuthStore = tc.app.state.auth_store  # type: ignore[attr-defined]
            _token_id, plaintext = store.create_token(name="test")
            r = tc.post("/auth/login", json={"token": plaintext})
            assert r.status_code == 200
            data = r.json()
            assert "session" in data
            assert "expires_at" in data
        mgr.close()
        api_main._manager = None

    def test_login_with_totp_returns_challenge(self, tmp_settings):
        """Login with a TOTP-enrolled token → challenge_id."""
        tc, mgr = self._make_client(tmp_settings)
        with tc:
            store: AuthStore = tc.app.state.auth_store  # type: ignore[attr-defined]
            token_id, plaintext = store.create_token(name="totp-test")

            totp_secret = pyotp.random_base32(32)
            master_key = "test-master-key-must-be-32bytes-or-more"
            encrypted = encrypt_totp_secret(totp_secret, master_key)
            store.set_totp_secret(token_id, encrypted)

            # Patch the load_settings binding inside the auth router module
            # (not vesma.config, because auth.py uses `from ... import load_settings`).
            import vesma.api.auth as auth_mod

            orig_load = auth_mod.load_settings

            from vesma.config import Settings

            def mock_load_settings(_path=None):  # type: ignore[misc]
                s = Settings(
                    vesma={
                        "vault_path": str(tmp_settings.vesma.vault_path),
                        "data_dir": str(tmp_settings.vesma.data_dir),
                        "db_name": tmp_settings.vesma.db_name,
                    },
                    embedding={"provider": "onnx"},
                    api={
                        "host": "127.0.0.1",
                        "port": 8787,
                        "totp_master_key": master_key,
                    },
                )
                s.resolve_paths()
                return s

            auth_mod.load_settings = mock_load_settings  # type: ignore[assignment]
            try:
                r = tc.post("/auth/login", json={"token": plaintext})
                assert r.status_code == 200
                data = r.json()
                assert "challenge_id" in data
                assert data["ttl_sec"] == 120
            finally:
                auth_mod.load_settings = orig_load  # type: ignore[assignment]
        mgr.close()
        api_main._manager = None

    def test_full_totp_flow(self, tmp_settings):
        """Login → verify (correct code) → session issued + cookie set."""
        tc, mgr = self._make_client(tmp_settings)
        with tc:
            store: AuthStore = tc.app.state.auth_store  # type: ignore[attr-defined]
            token_id, plaintext = store.create_token(name="full-totp-flow")

            totp_secret = pyotp.random_base32(32)
            master_key = "integration-test-master-key-xyz"
            encrypted = encrypt_totp_secret(totp_secret, master_key)
            store.set_totp_secret(token_id, encrypted)

            # Patch auth router's own load_settings binding (not vesma.config)
            import vesma.api.auth as auth_mod

            orig_load = auth_mod.load_settings

            def mock_load(path=None):  # type: ignore[misc]
                from vesma.config import Settings

                s = Settings(
                    vesma={
                        "vault_path": str(tmp_settings.vesma.vault_path),
                        "data_dir": str(tmp_settings.vesma.data_dir),
                        "db_name": tmp_settings.vesma.db_name,
                    },
                    embedding={"provider": "onnx"},
                    api={"host": "127.0.0.1", "port": 8787, "totp_master_key": master_key},
                )
                s.resolve_paths()
                return s

            auth_mod.load_settings = mock_load  # type: ignore[assignment]
            try:
                r1 = tc.post("/auth/login", json={"token": plaintext})
                assert r1.status_code == 200
                challenge_id = r1.json()["challenge_id"]

                code = pyotp.TOTP(totp_secret).now()
                r2 = tc.post("/auth/verify", json={"challenge_id": challenge_id, "code": code})
                assert r2.status_code == 200
                data = r2.json()
                assert "session" in data
                assert "mnemos_session" in r2.cookies
            finally:
                auth_mod.load_settings = orig_load  # type: ignore[assignment]
        mgr.close()
        api_main._manager = None

    def test_logout_clears_session(self, tmp_settings):
        tc, mgr = self._make_client(tmp_settings)
        with tc:
            store: AuthStore = tc.app.state.auth_store  # type: ignore[attr-defined]
            _token_id, plaintext = store.create_token()
            r = tc.post("/auth/login", json={"token": plaintext})
            session_token = r.json()["session"]

            r2 = tc.post(
                "/auth/logout",
                headers={"Authorization": f"Bearer {session_token}"},
            )
            assert r2.status_code == 200
            assert r2.json() == {"ok": True}
            sha256 = hash_token(session_token)
            assert store.get_session_by_hash(sha256) is None
        mgr.close()
        api_main._manager = None

    def test_me_endpoint(self, tmp_settings):
        tc, mgr = self._make_client(tmp_settings)
        with tc:
            store: AuthStore = tc.app.state.auth_store  # type: ignore[attr-defined]
            token_id, plaintext = store.create_token(name="me-test")
            r1 = tc.post("/auth/login", json={"token": plaintext})
            session_token = r1.json()["session"]

            r2 = tc.get(
                "/auth/me",
                headers={"Authorization": f"Bearer {session_token}"},
            )
            assert r2.status_code == 200
            data = r2.json()
            assert data["token_id"] == token_id
            assert data["totp"] is False
        mgr.close()
        api_main._manager = None

    def test_verify_wrong_challenge_id(self, tmp_settings):
        tc, mgr = self._make_client(tmp_settings)
        with tc:
            r = tc.post(
                "/auth/verify",
                json={"challenge_id": "chg_nonexistent", "code": "123456"},
            )
            assert r.status_code == 401
        mgr.close()
        api_main._manager = None


# ---------------------------------------------------------------------------
# Docs endpoint auth (finding: /docs bypass on non-loopback binds)
# ---------------------------------------------------------------------------


class TestDocsEndpointAuth:
    """``/docs``, ``/redoc``, ``/openapi.json`` must require auth on
    non-loopback binds to avoid leaking API schema to unauthenticated
    callers. On loopback binds they remain accessible for dev convenience.
    """

    def _make_client_with_host(self, tmp_settings, host: str) -> tuple[TestClient, MemoryManager]:
        """Build a TestClient with the API bound to ``host`` and auth enabled."""
        tmp_settings.api.host = host
        tmp_settings.api.auth_enabled = True
        tmp_settings.api.totp_enabled = False
        tmp_settings.api.behind_tls_proxy = False
        # Non-loopback binds require TOTP + TLS proxy per the startup guard.
        if host not in ("127.0.0.1", "::1", "localhost"):
            tmp_settings.api.totp_enabled = True
            tmp_settings.api.behind_tls_proxy = True
            # TOTP master key is required when TOTP is enabled.
            tmp_settings.api.totp_master_key = SecretStr("test-master-key-must-be-32bytes-or-more")

        mgr = MemoryManager(tmp_settings)
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        mgr._embedder = mock_embedder

        test_app = FastAPI(title="T", version="0.0.1", lifespan=lifespan)
        for route in app.routes:
            test_app.routes.append(route)
        test_app.add_middleware(AuthMiddleware)
        api_main._manager = mgr
        return TestClient(test_app), mgr

    def test_docs_accessible_on_loopback(self, tmp_settings):
        """On loopback bind with auth enabled, /docs is still bypassed."""
        tc, mgr = self._make_client_with_host(tmp_settings, "127.0.0.1")
        with tc:
            r = tc.get("/docs")
            assert r.status_code == 200
        mgr.close()
        api_main._manager = None

    def test_docs_requires_auth_on_non_loopback(self, tmp_settings):
        """On non-loopback bind with auth enabled, /docs returns 401."""
        tc, mgr = self._make_client_with_host(tmp_settings, "0.0.0.0")
        with tc:
            r = tc.get("/docs")
            assert r.status_code == 401
        mgr.close()
        api_main._manager = None

    def test_openapi_json_requires_auth_on_non_loopback(self, tmp_settings):
        """On non-loopback bind with auth enabled, /openapi.json returns 401."""
        tc, mgr = self._make_client_with_host(tmp_settings, "0.0.0.0")
        with tc:
            r = tc.get("/openapi.json")
            assert r.status_code == 401
        mgr.close()
        api_main._manager = None

    def test_redoc_requires_auth_on_non_loopback(self, tmp_settings):
        """On non-loopback bind with auth enabled, /redoc returns 401."""
        tc, mgr = self._make_client_with_host(tmp_settings, "0.0.0.0")
        with tc:
            r = tc.get("/redoc")
            assert r.status_code == 401
        mgr.close()
        api_main._manager = None
