"""Lifecycle hooks (mnemos #125, Wave 3) — module, MCP surface, REST surface.

Pinned here:

* ``pre_llm_call`` — thin wrapper over ``assemble_context`` with the
  caller identity threaded and ``context_hint`` as the EXPLICIT recall
  query (``stats.recall.query_source == "explicit"``); the ADR-0018
  entry invariant runs inside the assemble pipeline, not in the hook.
* ``on_session_start`` — thin wrapper over ``recall_context``; this
  channel owns the issuance scan of the echoed checkpoints (mirroring
  ``vesma_recall_context``); refuse mode drops the checkpoint.
* ``post_tool_call`` — the autocompression entry point: default OFF,
  per-call ``auto_compress`` and the ``hooks.auto_compress`` knob both
  enable it; N2 MANDATE — the compress call ALWAYS threads
  ``(agent, session)`` onto the cache row (issuer ledger verified).
* The grouped ``vesma_hooks`` MCP tool (action:enum, #97 pattern) and
  the parametric REST ``POST /hooks/{action}`` route — both over the
  shared ``dispatch_hook``.

All secrets below are obviously fake EXAMPLE-style values built from
the detector's own pattern catalogue; real credentials never appear.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import vesma.mcp_server as mcp_mod
from vesma.api import main as api_main
from vesma.api.main import app, lifespan
from vesma.config import Settings
from vesma.hooks import dispatch_hook
from vesma.manager import MemoryManager
from vesma.mcp_server import _dispatch
from vesma.models import (
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    MemoryType,
    MemoryUpdate,
)

FAKE_AWS_KEY = "AKIAEXAMPLEABCDEFGH1"

PROJECT = "hooks-proj"
AGENT = "hooks-agent"
SESSION = "hooks-session"


def _settings(
    tmp: Path,
    *,
    hooks_auto_compress: bool = False,
    hooks_max_output_chars: int | None = None,
    **ccr: Any,
) -> Settings:
    hooks: dict[str, Any] = {"auto_compress": hooks_auto_compress}
    if hooks_max_output_chars is not None:
        hooks["max_output_chars"] = hooks_max_output_chars
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        ccr={"min_size_chars": 100, **ccr},  # type: ignore[arg-type]
        hooks=hooks,  # type: ignore[arg-type]
        # PG-0.5: the hook now emits an auto-index hint; the hooks suite
        # tests hook contracts, not the graph — and the hint would otherwise
        # auto-register the TEST RUNNER's cwd (the mnemos repo itself, a
        # marker repo) and background-index it into the throwaway data dir.
        code_graph={"enabled": True, "auto_index": False},
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def manager() -> Iterator[MemoryManager]:
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = MemoryManager(_settings(Path(tmpdir)))
        yield mgr
        mgr.close()


@pytest.fixture
def auto_manager() -> Iterator[MemoryManager]:
    """Manager with the ``hooks.auto_compress`` knob ON (automation deployment).

    The compression-automation pair is mandatory since nhi-3 secure
    defaults: the knob requires ``ccr.validate_markers=true`` in the SAME
    config (pinned by tests/test_config.py), so the fixture carries the
    strict gate too.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = MemoryManager(
            _settings(Path(tmpdir), hooks_auto_compress=True, validate_markers=True)
        )
        yield mgr
        mgr.close()


def _add_published(mgr: MemoryManager, content: str) -> str:
    data = MemoryCreate(
        content=content,
        tags=[f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:learning"],
        source=MemorySource.MCP,
        status=MemoryStatus.PUBLISHED,
    )
    return str(mgr.add(data, project=PROJECT, agent=AGENT).id)


# ── pre_llm_call ──────────────────────────────────────────────────────────────


class TestPreLlmCall:
    def test_returns_assemble_result_with_hook_envelope(self, manager: MemoryManager) -> None:
        _add_published(manager, "pre call body mentioning quokka-hook token")
        result = dispatch_hook(
            manager,
            action="pre_llm_call",
            session=SESSION,
            project=PROJECT,
            agent=AGENT,
        )

        assert result["hook"] == "pre_llm_call"
        assert result["session"] == SESSION
        assert result["mode"] == "sync", "hook delivery is pinned to sync"
        assert "text" in result and "blocks" in result
        assert "injection" in result
        # Entry invariant inside the pipeline: every injected block is
        # provenance-wrapped.
        for block in result["blocks"]:
            assert block["provenance"].startswith("[mnemos:")

    def test_context_hint_is_the_explicit_recall_query(self, manager: MemoryManager) -> None:
        _add_published(manager, "deployment note about quokka-hint rotation")
        result = dispatch_hook(
            manager,
            action="pre_llm_call",
            session=SESSION,
            project=PROJECT,
            agent=AGENT,
            context_hint="quokka-hint",
        )

        assert result["stats"]["recall"]["query"] == "quokka-hint"
        assert result["stats"]["recall"]["query_source"] == "explicit"

    def test_identity_required(self, manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="agent is required"):
            dispatch_hook(
                manager,
                action="pre_llm_call",
                session=SESSION,
                project=PROJECT,
                agent="  ",
            )

    def test_blank_context_hint_rejected(self, manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="context_hint"):
            dispatch_hook(
                manager,
                action="pre_llm_call",
                session=SESSION,
                project=PROJECT,
                agent=AGENT,
                context_hint="   ",
            )


# ── on_session_start ─────────────────────────────────────────────────────────


class TestOnSessionStart:
    def _checkpoint(self, mgr: MemoryManager, content: str) -> str:
        data = MemoryCreate(
            content=content,
            tags=[f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:checkpoint"],
            source=MemorySource.MCP,
            memory_type=MemoryType.SESSION_CONTEXT,
            status=MemoryStatus.PUBLISHED,
        )
        return str(mgr.add(data, project=PROJECT, agent=AGENT).id)

    def test_recalls_and_scans_checkpoints(self, manager: MemoryManager) -> None:
        self._checkpoint(manager, "# checkpoint goals quokka-boot start")
        result = dispatch_hook(
            manager,
            action="on_session_start",
            session=SESSION,
            project=PROJECT,
            agent=AGENT,
        )

        assert result["hook"] == "on_session_start"
        assert result["checkpoints"], "checkpoint must be recalled"
        item = result["checkpoints"][0]
        assert "quokka-boot" in item["content"]
        assert item["redactions"] == 0
        assert result["redactions"] == 0
        # #456 shape policy: the pattern-name field is ABSENT on a clean
        # issuance (never an empty dict), same as every other surface.
        assert "redacted_patterns" not in result
        assert "redacted_patterns" not in item

    def test_secret_in_checkpoint_redacted_on_this_channel(self, manager: MemoryManager) -> None:
        self._checkpoint(manager, f"# checkpoint\napi key {FAKE_AWS_KEY} for the deploy")
        result = dispatch_hook(
            manager,
            action="on_session_start",
            session=SESSION,
            project=PROJECT,
            agent=AGENT,
        )

        assert result["checkpoints"], "redact-and-issue keeps the checkpoint"
        content = result["checkpoints"][0]["content"]
        assert FAKE_AWS_KEY not in content
        assert "<REDACTED:aws-key>" in content
        assert result["redactions"] >= 1
        assert result["checkpoints"][0]["redacted_patterns"] == {"aws-key": 1}
        # #456: the TOP-LEVEL total carries the merged pattern names too.
        assert result["redacted_patterns"] == {"aws-key": 1}

    def test_no_checkpoints_is_empty_list(self, manager: MemoryManager) -> None:
        result = dispatch_hook(
            manager,
            action="on_session_start",
            session=SESSION,
            project=PROJECT,
            agent=AGENT,
        )
        assert result["checkpoints"] == []

    def test_identity_required(self, manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="session is required"):
            dispatch_hook(
                manager,
                action="on_session_start",
                session="",
                project=PROJECT,
                agent=AGENT,
            )


# ── recall_context — archived checkpoint regression ──────────────────────────


class TestRecallContextArchivedFilter:
    """Archived checkpoints must not surface as fresh context.

    ``on_session_start`` is a thin wrapper over ``recall_context``, so this
    pins the manager-level invariant directly: the no-query freshness leg
    filters ARCHIVED after ``list_all`` and before the limit trim.
    """

    def _checkpoint(self, mgr: MemoryManager, content: str) -> str:
        data = MemoryCreate(
            content=content,
            tags=[f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:checkpoint"],
            source=MemorySource.MCP,
            memory_type=MemoryType.SESSION_CONTEXT,
            status=MemoryStatus.PUBLISHED,
        )
        return str(mgr.add(data, project=PROJECT, agent=AGENT).id)

    def test_freshness_leg_skips_archived_checkpoint(self, manager: MemoryManager) -> None:
        active_a = self._checkpoint(manager, "# checkpoint active quokka-live goals")
        active_b = self._checkpoint(manager, "# checkpoint active quokka-second goals")
        archived = self._checkpoint(manager, "# checkpoint archived quokka-stale goals")
        manager.update(archived, MemoryUpdate(status=MemoryStatus.ARCHIVED))

        # No-query leg: freshness recall returns active checkpoints only.
        recalled_ids = [str(m.id) for m in manager.recall_context(project=PROJECT, limit=10)]
        assert active_a in recalled_ids
        assert active_b in recalled_ids
        assert archived not in recalled_ids

        # Search over the same project keeps the archived checkpoint hidden too.
        found_ids = [str(r.memory.id) for r in manager.search("quokka", project=PROJECT, limit=10)]
        assert archived not in found_ids


# ── post_tool_call ───────────────────────────────────────────────────────────


TOOL_OUTPUT = "build log quokka-tool start\n" + "\n".join(
    f"step {i} compiled module {i} ok" for i in range(60)
)


class TestPostToolCall:
    def _issuer_row(self, mgr: MemoryManager, marker_hash: str) -> dict[str, Any]:
        row = (
            mgr.sqlite._get_conn()
            .execute(
                "SELECT issuer_agent, issuer_session FROM ccr_cache WHERE hash=?",
                (marker_hash,),
            )
            .fetchone()
        )
        return dict(row) if row else {}

    def test_default_off_returns_noop_envelope(self, manager: MemoryManager) -> None:
        result = dispatch_hook(
            manager,
            action="post_tool_call",
            session=SESSION,
            project=PROJECT,
            agent=AGENT,
            tool_name="bash",
            output_text=TOOL_OUTPUT,
        )

        assert result["auto_compress"] is False
        assert result["compressed"] is False
        assert "compressed_text" not in result
        cached = (
            manager.sqlite._get_conn()
            .execute("SELECT COUNT(*) AS n FROM ccr_cache")
            .fetchone()["n"]
        )
        assert cached == 0, "off hook must not write the cache"

    def test_per_call_auto_compress_threads_identity_n2(self, manager: MemoryManager) -> None:
        result = dispatch_hook(
            manager,
            action="post_tool_call",
            session=SESSION,
            project=PROJECT,
            agent=AGENT,
            tool_name="bash",
            output_text=TOOL_OUTPUT,
            auto_compress=True,
        )

        assert result["auto_compress"] is True
        assert result["compressed"] is True
        assert result["marker"].startswith("[compressed: ")
        assert result["compressed_text"].startswith(result["marker"])
        assert "substitute" in result["action"]
        # N2 MANDATE: the cache row carries the caller's issuer identity —
        # identity-less compress would mint an unverifiable NULL-issuer row.
        row = self._issuer_row(manager, result["ccr"]["hash"])
        assert row["issuer_agent"] == AGENT
        assert row["issuer_session"] == SESSION

    def test_config_knob_enables_without_per_call_arg(self, auto_manager: MemoryManager) -> None:
        result = dispatch_hook(
            auto_manager,
            action="post_tool_call",
            session=SESSION,
            project=PROJECT,
            agent=AGENT,
            tool_name="bash",
            output_text=TOOL_OUTPUT,
        )

        assert result["auto_compress"] is True
        assert result["compressed"] is True

    def test_identity_required(self, manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="agent is required"):
            dispatch_hook(
                manager,
                action="post_tool_call",
                session=SESSION,
                project=PROJECT,
                agent=None,  # type: ignore[arg-type]
                tool_name="bash",
                output_text=TOOL_OUTPUT,
                auto_compress=True,
            )

    def test_tool_name_required(self, manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="tool_name"):
            dispatch_hook(
                manager,
                action="post_tool_call",
                session=SESSION,
                project=PROJECT,
                agent=AGENT,
                tool_name=" ",
                output_text=TOOL_OUTPUT,
                auto_compress=True,
            )

    def test_over_cap_output_rejected_no_write(self, manager: MemoryManager) -> None:
        """F3 (W3 review): hooks.max_output_chars rejects an oversized
        output_text at the boundary BEFORE any write — even on an
        auto_compress=off call (the harness learns the contract early)."""
        manager.settings.hooks.max_output_chars = 500
        oversized = TOOL_OUTPUT + "x" * 5_000

        with pytest.raises(ValueError, match=r"hooks\.max_output_chars"):
            dispatch_hook(
                manager,
                action="post_tool_call",
                session=SESSION,
                project=PROJECT,
                agent=AGENT,
                tool_name="bash",
                output_text=oversized,
                auto_compress=True,
            )
        # The same payload is rejected with autocompression OFF too.
        with pytest.raises(ValueError, match=r"hooks\.max_output_chars"):
            dispatch_hook(
                manager,
                action="post_tool_call",
                session=SESSION,
                project=PROJECT,
                agent=AGENT,
                tool_name="bash",
                output_text=oversized,
            )
        cached = (
            manager.sqlite._get_conn()
            .execute("SELECT COUNT(*) AS n FROM ccr_cache")
            .fetchone()["n"]
        )
        assert cached == 0, "over-cap output must never reach the cache/FTS"

    def test_default_cap_allows_normal_output(self, manager: MemoryManager) -> None:
        """The default cap (1,048,576) does not trip on realistic output."""
        assert manager.settings.hooks.max_output_chars == 1_048_576
        result = dispatch_hook(
            manager,
            action="post_tool_call",
            session=SESSION,
            project=PROJECT,
            agent=AGENT,
            tool_name="bash",
            output_text=TOOL_OUTPUT,
            auto_compress=True,
        )
        assert result["compressed"] is True


# ── dispatch ─────────────────────────────────────────────────────────────────


class TestDispatch:
    def test_unknown_action(self, manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="unknown hook action"):
            dispatch_hook(
                manager,
                action="bogus",
                session=SESSION,
                project=PROJECT,
                agent=AGENT,
            )

    def test_post_tool_call_requires_payload_args(self, manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="tool_name' and 'output_text'"):
            dispatch_hook(
                manager,
                action="post_tool_call",
                session=SESSION,
                project=PROJECT,
                agent=AGENT,
            )


# ── MCP surface: vesma_hooks (grouped action:enum) ──────────────────────────


class TestMcpHooksTool:
    def _call(self, mgr: MemoryManager, args: dict[str, Any]) -> Any:
        monkeypatch_manager = mgr
        mcp_mod._manager = monkeypatch_manager
        try:
            return asyncio.new_event_loop().run_until_complete(_dispatch("vesma_hooks", args))
        finally:
            mcp_mod._manager = None

    def test_pre_llm_call(self, manager: MemoryManager) -> None:
        _add_published(manager, "mcp hook body quokka-mcp token")
        result = self._call(
            manager,
            {
                "action": "pre_llm_call",
                "session": SESSION,
                "project": PROJECT,
                "agent": AGENT,
                "context_hint": "quokka-mcp",
            },
        )
        assert result["hook"] == "pre_llm_call"
        assert result["stats"]["recall"]["query"] == "quokka-mcp"

    def test_unknown_action_is_clean_error(self, manager: MemoryManager) -> None:
        result = self._call(
            manager,
            {"action": "nope", "session": SESSION, "project": PROJECT, "agent": AGENT},
        )
        assert "error" in result
        assert result["error"] == (
            "action must be one of: pre_llm_call, on_session_start, post_tool_call"
        )

    def test_missing_identity_is_clean_error(self, manager: MemoryManager) -> None:
        result = self._call(
            manager, {"action": "pre_llm_call", "session": SESSION, "project": PROJECT}
        )
        assert result == {"error": "agent is required and must be a non-empty string"}

    def test_auto_compress_string_not_coerced(self, manager: MemoryManager) -> None:
        result = self._call(
            manager,
            {
                "action": "post_tool_call",
                "session": SESSION,
                "project": PROJECT,
                "agent": AGENT,
                "tool_name": "bash",
                "output_text": TOOL_OUTPUT,
                "auto_compress": "true",
            },
        )
        assert result == {"error": "auto_compress must be a boolean when provided"}

    def test_post_tool_call_roundtrip(self, manager: MemoryManager) -> None:
        result = self._call(
            manager,
            {
                "action": "post_tool_call",
                "session": SESSION,
                "project": PROJECT,
                "agent": AGENT,
                "tool_name": "bash",
                "output_text": TOOL_OUTPUT,
                "auto_compress": True,
            },
        )
        assert result["compressed"] is True


# ── REST surface: POST /hooks/{action} ───────────────────────────────────────


class TestRestHooksRoute:
    @pytest.fixture
    def rest_client(self, manager: MemoryManager) -> Iterator[TestClient]:
        api_main._manager = manager
        test_app = FastAPI(title="Mnemos-Hooks-Test", version="0.1.0", lifespan=lifespan)
        for route in app.routes:
            test_app.routes.append(route)
        with TestClient(test_app) as tc:
            yield tc
        api_main._manager = None

    def test_on_session_start(self, rest_client: TestClient) -> None:
        resp = rest_client.post(
            "/hooks/on_session_start",
            json={"session": SESSION, "project": PROJECT, "agent": AGENT},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["hook"] == "on_session_start"
        assert body["checkpoints"] == []

    def test_unknown_action_is_404(self, rest_client: TestClient) -> None:
        resp = rest_client.post(
            "/hooks/bogus",
            json={"session": SESSION, "project": PROJECT, "agent": AGENT},
        )
        assert resp.status_code == 404
        assert "unknown hook action" in resp.json()["detail"]

    def test_missing_payload_args_is_422_hook_boundary(self, rest_client: TestClient) -> None:
        resp = rest_client.post(
            "/hooks/post_tool_call",
            json={"session": SESSION, "project": PROJECT, "agent": AGENT},
        )
        assert resp.status_code == 422
        assert "tool_name" in resp.json()["detail"]

    def test_over_cap_output_is_422_no_write(
        self, rest_client: TestClient, manager: MemoryManager
    ) -> None:
        """F3: the cap maps to 422 at the REST surface (context_rewrite
        caps convention) and nothing is written."""
        manager.settings.hooks.max_output_chars = 500
        resp = rest_client.post(
            "/hooks/post_tool_call",
            json={
                "session": SESSION,
                "project": PROJECT,
                "agent": AGENT,
                "tool_name": "bash",
                "output_text": TOOL_OUTPUT + "x" * 5_000,
                "auto_compress": True,
            },
        )
        assert resp.status_code == 422
        assert "hooks.max_output_chars" in resp.json()["detail"]
        cached = (
            manager.sqlite._get_conn()
            .execute("SELECT COUNT(*) AS n FROM ccr_cache")
            .fetchone()["n"]
        )
        assert cached == 0

    def test_pre_llm_call(self, rest_client: TestClient, manager: MemoryManager) -> None:
        _add_published(manager, "rest hook body quokka-rest token")
        resp = rest_client.post(
            "/hooks/pre_llm_call",
            json={
                "session": SESSION,
                "project": PROJECT,
                "agent": AGENT,
                "context_hint": "quokka-rest",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["hook"] == "pre_llm_call"
        assert resp.json()["stats"]["recall"]["query"] == "quokka-rest"
