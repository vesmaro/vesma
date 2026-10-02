"""Tests for the federation client (A-side, Phase 2, contract §3.2 + КП-2).

Covers:

* Happy path — 200 → PullResult with records, fell_back_to_local=False.
* Timeout → OFFLINE_LITE + fallback (КП-2).
* Connection refused → OFFLINE_LITE + fallback.
* 403 → REFUSED + fallback.
* 429 → REFUSED + fallback.
* Malformed 200 body → REFUSED + fallback.
* Unknown peer → REFUSED + fallback.
* Missing bearer token env → REFUSED + fallback.
* Missing base URL env → REFUSED + fallback.

Uses ``httpx.MockTransport`` to mock the peer's HTTP — no live socket.
All fixtures RFC-reserved.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from vesma.config import PeerConfig, Settings
from vesma.federation_client import FEDERATION_PULL_PATH, pull_from_peer
from vesma.trigger_codes import TriggerCode

PEER_A = "mnemos-A"
PEER_B = "mnemos-B"
PROJECT = "project-mnemos"
TOKEN_ENV = "VESMA_FED_PEER_VESMARO_A_TOKEN"
TOKEN_VALUE = "mnk_fed_mnemos-A_exampletoken123"
BASE_URL = "https://example.invalid"


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    os.environ[TOKEN_ENV] = TOKEN_VALUE
    os.environ["VESMA_FED_PEER_VESMARO_A_URL"] = BASE_URL
    settings = Settings(
        vesma={
            "vault_path": str(tmp_path / "vault"),
            "data_dir": str(tmp_path / "data"),
            "db_name": "test.db",
        },
        embedding={"provider": "onnx"},
        scanner={"enabled": False},
        federation={
            "peers": {
                PEER_A: PeerConfig(
                    bearer_token_env=TOKEN_ENV,
                    allowed_projects=[PROJECT],
                    allowed_types=["decision"],
                    rate_limit_per_minute=60,
                ),
            },
        },
    )
    settings.resolve_paths()
    yield settings
    os.environ.pop("VESMA_FED_PEER_VESMARO_A_URL", None)


def _mock(handler, settings: Settings, *, base_url: str = BASE_URL):
    """Build a MockTransport + call pull_from_peer with the override."""
    transport = httpx.MockTransport(handler)
    return pull_from_peer(
        PEER_A,
        "federation threat model",
        PROJECT,
        settings=settings,
        transport=transport,
        base_url_override=base_url,
    )


def _ok_body(*, trigger_code: str = "EXHAUSTIVE", records: list | None = None) -> dict:
    return {
        "trigger_code": trigger_code,
        "records": records or [],
        "ttl_class": "ephemeral",
        "peer_id": PEER_B,
    }


class TestHappyPath:
    def test_200_returns_pull_result_with_records(self, tmp_settings: Settings) -> None:
        record = {
            "id": f"fed:{PEER_B}:11111111-1111-1111-1111-111111111111",
            "type": "decision",
            "title": "ADR-0014 auth decision",
            "summary": "We chose bearer+TOTP 2FA for remote sessions.",
            "key_points": [],
            "tags": ["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
            "source_agent": PEER_B,
            "timestamp": "2026-07-21T12:00:00Z",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == FEDERATION_PULL_PATH
            assert request.headers["Authorization"] == f"Bearer {TOKEN_VALUE}"
            return httpx.Response(200, json=_ok_body(records=[record]))

        result = _mock(handler, tmp_settings)
        assert result.trigger_code == TriggerCode.EXHAUSTIVE
        assert result.fell_back_to_local is False
        assert result.peer_id == PEER_B
        assert len(result.records) == 1
        assert result.records[0].id.startswith(f"fed:{PEER_B}:")


class TestFallback:
    def test_timeout_returns_offline_lite_fallback(self, tmp_settings: Settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("simulated timeout")

        result = _mock(handler, tmp_settings)
        assert result.trigger_code == TriggerCode.OFFLINE_LITE
        assert result.fell_back_to_local is True
        assert result.records == []

    def test_connection_refused_returns_offline_lite_fallback(self, tmp_settings: Settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated connection refused")

        result = _mock(handler, tmp_settings)
        assert result.trigger_code == TriggerCode.OFFLINE_LITE
        assert result.fell_back_to_local is True

    def test_403_returns_refused_fallback(self, tmp_settings: Settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"detail": "forbidden"})

        result = _mock(handler, tmp_settings)
        assert result.trigger_code == TriggerCode.REFUSED
        assert result.fell_back_to_local is True

    def test_429_returns_refused_fallback(self, tmp_settings: Settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"detail": "rate limited"})

        result = _mock(handler, tmp_settings)
        assert result.trigger_code == TriggerCode.REFUSED
        assert result.fell_back_to_local is True

    def test_500_returns_offline_lite_fallback(self, tmp_settings: Settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "server error"})

        result = _mock(handler, tmp_settings)
        assert result.fell_back_to_local is True

    def test_malformed_200_body_returns_refused_fallback(self, tmp_settings: Settings) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json", headers={"content-type": "text/plain"})

        result = _mock(handler, tmp_settings)
        assert result.fell_back_to_local is True


class TestConfigFailClosed:
    def test_unknown_peer_returns_refused_fallback(
        self, tmp_path: Path, tmp_settings: Settings
    ) -> None:
        result = pull_from_peer(
            "mnemos-Z",
            "query",
            PROJECT,
            settings=tmp_settings,
            base_url_override=BASE_URL,
        )
        assert result.trigger_code == TriggerCode.REFUSED
        assert result.fell_back_to_local is True

    def test_missing_token_env_returns_refused_fallback(
        self, tmp_path: Path, tmp_settings: Settings
    ) -> None:
        os.environ.pop(TOKEN_ENV, None)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_body())

        result = _mock(handler, tmp_settings)
        assert result.trigger_code == TriggerCode.REFUSED
        assert result.fell_back_to_local is True

    def test_missing_base_url_returns_refused_fallback(
        self, tmp_path: Path, tmp_settings: Settings
    ) -> None:
        os.environ.pop("VESMA_FED_PEER_VESMARO_A_URL", None)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_body())

        # Pass an explicit empty base_url_override so the env-var path is
        # short-circuited (the handler must NOT be called).
        transport = httpx.MockTransport(handler)
        result = pull_from_peer(
            PEER_A,
            "query",
            PROJECT,
            settings=tmp_settings,
            transport=transport,
            base_url_override="",
        )
        assert result.trigger_code == TriggerCode.REFUSED
        assert result.fell_back_to_local is True


class TestEnvVarResolution:
    def test_base_url_env_name_uppercases_and_replaces_dashes(
        self, tmp_path: Path, tmp_settings: Settings
    ) -> None:
        # mnemos-A → VESMA_FED_PEER_VESMARO_A_URL
        os.environ["VESMA_FED_PEER_VESMARO_A_URL"] = "https://example.invalid"

        def handler(request: httpx.Request) -> httpx.Response:
            # The request URL must be on example.invalid (BASE_URL).
            assert request.url.host == "example.invalid"
            return httpx.Response(200, json=_ok_body())

        result = _mock(handler, tmp_settings)
        assert result.fell_back_to_local is False

    def test_base_url_override_takes_precedence(self, tmp_settings: Settings) -> None:
        os.environ["VESMA_FED_PEER_VESMARO_A_URL"] = "https://wrong.example.invalid"

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "override.example.invalid"
            return httpx.Response(200, json=_ok_body())

        transport = httpx.MockTransport(handler)
        result = pull_from_peer(
            PEER_A,
            "query",
            PROJECT,
            settings=tmp_settings,
            transport=transport,
            base_url_override="https://override.example.invalid",
        )
        assert result.fell_back_to_local is False


class TestTimeout:
    def test_custom_timeout_passed_to_client(self, tmp_settings: Settings) -> None:
        # Use a very short timeout that triggers on the mock — the MockTransport
        # raises TimeoutException immediately, so any positive timeout works.
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("fast")

        result = pull_from_peer(
            PEER_A,
            "query",
            PROJECT,
            settings=tmp_settings,
            transport=httpx.MockTransport(handler),
            timeout_s=0.001,
            base_url_override=BASE_URL,
        )
        assert result.trigger_code == TriggerCode.OFFLINE_LITE
