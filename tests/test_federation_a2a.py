"""Tests for the federation A2A handler (Phase 2, contract §3.2).

Covers:

* B-side: ``request-info`` → server flow → ``share-finding`` envelope
  with the right trigger code + records + ttl_class.
* A-side: dispatch on trigger code —
  - EXHAUSTIVE / PARTIAL → use records.
  - ALREADY_EXHAUSTED → noop.
  - REFUSED / OFFLINE_LITE → fallback_local (with and without a
    local_search callable).
* A2A envelope payload shape — ``trigger_code`` is the enum value,
  ``ttl_class="ephemeral"``, ``records`` is the list of compact-record
  dicts.
* ``mediate_pull_a_side`` falls back to HTTP transport when A2A is
  requested but the live MCP server is not wired (graceful
  degradation).

All fixtures RFC-reserved.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesmaro.config import PeerConfig, Settings
from vesmaro.federation_a2a import (
    A2AIntent,
    A2AResponse,
    build_share_finding_payload,
    handle_request_info,
    handle_share_finding,
    mediate_pull_a_side,
)
from vesmaro.federation_access_log import FederationAccessLog
from vesmaro.federation_server import PullResponse
from vesmaro.manager import MemoryManager
from vesmaro.models import MemoryCreate, MemoryStatus
from vesmaro.trigger_codes import TriggerCode

PEER_A = "mnemos-A"
PEER_B = "mnemos-B"
PROJECT = "project-mnemos"
TOKEN_ENV = "VESMA_FED_PEER_VESMARO_A_TOKEN"
TOKEN_VALUE = "mnk_fed_mnemos-A_exampletoken123"
FAKE_AWS_KEY = "AKIA" + "T" * 16


class _Message:
    """Minimal A2A message fixture — implements the A2AMessage Protocol."""

    def __init__(self, *, intent: str, peer_id: str, query: str, project_scope: str) -> None:
        self.intent = intent
        self.peer_id = peer_id
        self.query = query
        self.project_scope = project_scope
        self.payload = {}


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    os.environ[TOKEN_ENV] = TOKEN_VALUE
    settings = Settings(
        mnemos={
            "vault_path": str(tmp_path / "vault"),
            "data_dir": str(tmp_path / "data"),
            "db_name": "test.db",
        },
        embedding={"provider": "onnx"},
        scanner={"enabled": False},
        federation={
            "shared_projects": [PROJECT],
            "peers": {
                PEER_A: PeerConfig(
                    bearer_token_env=TOKEN_ENV,
                    allowed_projects=[PROJECT],
                    allowed_types=["decision", "learning"],
                    rate_limit_per_minute=60,
                ),
            },
        },
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def manager(tmp_settings: Settings) -> MemoryManager:
    mgr = MemoryManager(tmp_settings)
    mock = MagicMock()
    mock.embed.return_value = [0.1] * 384
    mgr._embedder = mock
    yield mgr
    mgr.close()


@pytest.fixture
def access_log(tmp_path: Path) -> FederationAccessLog:
    return FederationAccessLog(tmp_path / "a2a-access.jsonl")


def _add_memory(
    manager: MemoryManager,
    content: str,
    *,
    tags: list[str],
    title: str | None = None,
    project: str = PROJECT,
    agent: str = "gcw-tech-lead",
) -> None:
    mem = manager.add(
        MemoryCreate(content=content, title=title, tags=list(tags)),
        project=project,
        agent=agent,
    )
    manager.sqlite.update_status(mem.id, MemoryStatus.PUBLISHED)


# ── B-side ────────────────────────────────────────────────────────────────────


class TestBSideRequestInfo:
    def test_request_info_returns_share_finding_intent(
        self, manager, access_log, tmp_settings: Settings
    ) -> None:
        _add_memory(
            manager,
            "federation threat model: decision content",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
        )
        msg = _Message(
            intent=A2AIntent.REQUEST_INFO,
            peer_id=PEER_A,
            query="federation threat model",
            project_scope=PROJECT,
        )
        resp = handle_request_info(
            msg,
            settings=tmp_settings,
            manager=manager,
            access_log=access_log,
            presented_token=TOKEN_VALUE,
        )
        assert isinstance(resp, A2AResponse)
        assert resp.intent == A2AIntent.SHARE_FINDING
        assert resp.http_status == 200

    def test_share_finding_payload_carries_trigger_code_and_ephemeral(
        self, manager, access_log, tmp_settings: Settings
    ) -> None:
        _add_memory(
            manager,
            "federation threat model: decision content",
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
        )
        msg = _Message(
            intent=A2AIntent.REQUEST_INFO,
            peer_id=PEER_A,
            query="federation threat model",
            project_scope=PROJECT,
        )
        resp = handle_request_info(
            msg,
            settings=tmp_settings,
            manager=manager,
            access_log=access_log,
            presented_token=TOKEN_VALUE,
        )
        payload = resp.payload
        assert payload["trigger_code"] == TriggerCode.EXHAUSTIVE.value
        assert payload["ttl_class"] == "ephemeral"
        assert payload["peer_id"] == PEER_B
        assert isinstance(payload["records"], list)
        assert len(payload["records"]) == 1

    def test_auth_failure_returns_refused(
        self, manager, access_log, tmp_settings: Settings
    ) -> None:
        msg = _Message(
            intent=A2AIntent.REQUEST_INFO,
            peer_id=PEER_A,
            query="topic",
            project_scope=PROJECT,
        )
        resp = handle_request_info(
            msg,
            settings=tmp_settings,
            manager=manager,
            access_log=access_log,
            presented_token="wrong-token",
        )
        assert resp.http_status == 403
        assert resp.payload["trigger_code"] == TriggerCode.REFUSED.value


# ── A-side ────────────────────────────────────────────────────────────────────


def _resp(*, trigger_code: TriggerCode, records: list | None = None) -> PullResponse:
    return PullResponse(
        trigger_code=trigger_code,
        records=records or [],
        ttl_class="ephemeral",
        peer_id=PEER_B,
    )


class TestASideShareFinding:
    def test_exhaustive_returns_use_action(self) -> None:
        resp = _resp(trigger_code=TriggerCode.EXHAUSTIVE, records=[])
        action = handle_share_finding(resp)
        assert action["action"] == "use"
        assert action["trigger_code"] == TriggerCode.EXHAUSTIVE.value

    def test_partial_returns_use_action(self) -> None:
        resp = _resp(trigger_code=TriggerCode.PARTIAL)
        action = handle_share_finding(resp)
        assert action["action"] == "use"

    def test_already_exhausted_returns_noop(self) -> None:
        resp = _resp(trigger_code=TriggerCode.ALREADY_EXHAUSTED)
        action = handle_share_finding(resp)
        assert action["action"] == "noop"
        assert action["trigger_code"] == TriggerCode.ALREADY_EXHAUSTED.value

    def test_refused_returns_fallback_local_without_search(self) -> None:
        resp = _resp(trigger_code=TriggerCode.REFUSED)
        action = handle_share_finding(resp)
        assert action["action"] == "fallback_local"
        assert "local_results" not in action

    def test_refused_runs_local_search_when_provided(self) -> None:
        resp = _resp(trigger_code=TriggerCode.REFUSED)
        called = []

        def local_search(_resp: PullResponse) -> list:
            called.append(_resp)
            return ["local-result-1", "local-result-2"]

        action = handle_share_finding(resp, local_search=local_search)
        assert action["action"] == "fallback_local"
        assert action["local_results"] == ["local-result-1", "local-result-2"]
        assert len(called) == 1

    def test_offline_lite_returns_fallback_local(self) -> None:
        resp = _resp(trigger_code=TriggerCode.OFFLINE_LITE)
        action = handle_share_finding(resp)
        assert action["action"] == "fallback_local"

    def test_local_search_exception_does_not_crash(self) -> None:
        resp = _resp(trigger_code=TriggerCode.REFUSED)

        def local_search(_resp: PullResponse) -> list:
            raise RuntimeError("boom")

        action = handle_share_finding(resp, local_search=local_search)
        assert action["action"] == "fallback_local"
        assert action["local_results"] == []

    def test_handle_share_finding_local_search_exception_logged_and_fallback_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """CR#3: a local_search exception MUST be logged, not silently swallowed.

        ``[]`` is still the partial result (КП-2 expects partial), but the
        operator must see a warning so the silent-suppression anti-pattern
        (lint-and-validate) is avoided.
        """
        resp = _resp(trigger_code=TriggerCode.REFUSED)

        def local_search(_resp: PullResponse) -> list:
            raise RuntimeError("boom")

        with caplog.at_level(logging.WARNING, logger="vesmaro.federation_a2a"):
            action = handle_share_finding(resp, local_search=local_search)

        assert action["action"] == "fallback_local"
        assert action["local_results"] == []
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "local_search failed" in warnings[0].getMessage()
        assert "RuntimeError" in warnings[0].getMessage()
        assert "boom" in warnings[0].getMessage()


# ── mediate_pull_a_side — graceful degradation ────────────────────────────────


class TestMediatePullASide:
    def test_use_a2a_falls_back_to_http_transport(
        self,
        manager,
        access_log,
        tmp_settings: Settings,
        monkeypatch,
    ) -> None:
        # mediate_pull_a_side with use_a2a=True should fall back to HTTP
        # transport (Phase 2 does not wire the live MCP server).
        os.environ["VESMA_FED_PEER_VESMARO_A_URL"] = "https://example.invalid"

        from vesmaro.federation_client import pull_from_peer as real_pull

        captured: dict = {}

        def fake_pull(*args, **kwargs):
            captured["called"] = True
            return real_pull(*args, **kwargs)

        monkeypatch.setattr("vesmaro.federation_a2a.pull_from_peer", fake_pull)
        result = mediate_pull_a_side(
            PEER_A,
            "query",
            PROJECT,
            settings=tmp_settings,
            use_a2a=True,
        )
        assert captured["called"] is True
        # The HTTP transport itself will fail (no live peer) → fallback.
        assert result.fell_back_to_local is True


# ── build_share_finding_payload ──────────────────────────────────────────────


class TestBuildPayload:
    def test_payload_records_are_compact_dict(self) -> None:
        from vesmaro.compact import CompactRecord

        rec = CompactRecord(
            id="fed:mnemos-B:11111111-1111-1111-1111-111111111111",
            type="decision",
            title="ADR-0014 auth decision",
            summary="We chose bearer+TOTP 2FA for remote sessions.",
            key_points=[],
            tags=["project:project-mnemos", "agent:gcw-tech-lead", "mnemos:decision"],
            source_agent=PEER_B,
            timestamp="2026-07-21T12:00:00Z",
        )
        resp = PullResponse(
            trigger_code=TriggerCode.EXHAUSTIVE,
            records=[rec],
            ttl_class="ephemeral",
            peer_id=PEER_B,
        )
        payload = build_share_finding_payload(resp)
        assert payload["trigger_code"] == "EXHAUSTIVE"
        assert payload["ttl_class"] == "ephemeral"
        assert isinstance(payload["records"][0], dict)
        assert payload["records"][0]["id"].startswith("fed:mnemos-B:")
