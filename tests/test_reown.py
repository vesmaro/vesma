"""Operator reown (W3 tail, card 87cb4239).

Board rows authored before 6.10.0 carry the legacy identity
(``agent:tech-lead``) and are NOT mutable by a live token —
``vesma_workflow set`` refuses even ``force=True`` (ownership ≠ lock,
ADR-0050 §4; the invariant is correct). The operator path is the root
CLI ``vesma reown <memory-id> --agent <slug> --reason …``: it rewrites
ONLY the ``agent:`` tag of a row, off-plane (``identity=None``), where
the W3 namespace enforcement no-ops by design — the approved
administrative loophole, never a plane bypass.

Covers:
- the tag rewrite: old ``agent:`` gone, new minted; ``project:`` /
  ``vesma:`` / ``specialist:`` tags untouched;
- the W3 gate AFTER reown: ``vesma_workflow set`` under the NEW owner's
  identity passes (the gate reads ownership from tags, so the reown
  unlock is observable end-to-end on the plane);
- the loud refusals: unknown id, tombstoned row;
- the mandatory reason: missing ``--reason`` (and ``--reason`` without a
  value) is a click usage error — exit 2, nothing invoked;
- the audit log line carries old owner, new owner and the reason.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from vesma.cli.main import app, reown
from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.mcp_namespace import (
    AGENT_TAG_PREFIX,
    McpIdentity,
    current_identity,
    reset_request_identity,
    set_request_identity,
)
from vesma.models import MemoryCreate, MemorySource, MemoryStatus

runner = CliRunner()

LEGACY_OWNER = "tech-lead"
NEW_OWNER = "harness-new"


def _base_settings(tmp: Path) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
            "strict_tag_contract": True,
        },
        embedding={"provider": "onnx"},
        scanner={"enabled": False},  # no daemon thread per test
        mcp={
            "namespace": {"cross_read": "published_only"},
            "quota": {"calls_per_minute": 120},
        },
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def store_factory() -> Iterator[Any]:
    """Build isolated MemoryManagers wired into the ``mcp_server`` singleton.

    Same wiring as tests/test_mcp_namespace.py — the workflow-set leg below
    needs the real ``call_tool`` dispatch to observe the ownership flip.
    """
    from vesma import mcp_server

    built: list[MemoryManager] = []

    def _factory(settings: Settings) -> MemoryManager:
        mgr = MemoryManager(settings)
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        mgr._embedder = mock_embedder
        built.append(mgr)
        mcp_server._manager = mgr
        return mgr

    yield _factory
    mcp_server._manager = None
    for mgr in built:
        mgr.close()


@asynccontextmanager
async def mcp_identity(agent: str, jti: str = "jti-reown-1") -> AsyncIterator[McpIdentity]:
    token = set_request_identity(McpIdentity(agent_id=agent, jti=jti))
    try:
        yield current_identity()
    finally:
        reset_request_identity(token)


async def _call_tool_text(name: str, arguments: dict[str, Any]) -> str:
    from vesma import mcp_server

    result = await mcp_server.call_tool(name, arguments)
    return result[0].text


def _seed_legacy_board_row(mgr: MemoryManager) -> Any:
    """The W3-tail shape: a board card under the LEGACY identity, with the
    specialist attribution and the federate exclusion marker in place —
    reown must leave both alone."""
    return mgr.add(
        MemoryCreate(
            content="legacy board card body",
            tags=[
                "project:reown-wave",
                f"{AGENT_TAG_PREFIX}{LEGACY_OWNER}",
                "vesma:decision",
                "specialist:tech-lead",
                "mnemos:no-federate",
            ],
            source=MemorySource.CLI,
            status=MemoryStatus.PUBLISHED,
        ),
        project="reown-wave",
        agent=LEGACY_OWNER,
    )


def _owner_of(mgr: MemoryManager, memory_id: str) -> str:
    mem = mgr.get(memory_id)
    assert mem is not None
    return next(
        (t[len(AGENT_TAG_PREFIX) :] for t in mem.tags if t.startswith(AGENT_TAG_PREFIX)),
        "",
    )


# ── Manager level ─────────────────────────────────────────────────────────────


class TestReownManager:
    def test_reown_rewrites_only_the_agent_tag(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        row = _seed_legacy_board_row(mgr)

        result = mgr.reown(str(row.id), agent=NEW_OWNER, reason="legacy wave hygiene")

        assert result["changed"] is True
        assert result["old_owner"] == LEGACY_OWNER
        assert result["new_owner"] == NEW_OWNER
        mem = mgr.get(str(row.id))
        assert mem is not None
        # the old identity is GONE, the new one minted
        assert f"{AGENT_TAG_PREFIX}{LEGACY_OWNER}" not in mem.tags
        assert f"{AGENT_TAG_PREFIX}{NEW_OWNER}" in mem.tags
        # everything else passes through untouched (card requirement d)
        assert "project:reown-wave" in mem.tags
        assert "vesma:decision" in mem.tags
        assert "specialist:tech-lead" in mem.tags
        assert "mnemos:no-federate" in mem.tags
        # the denormalised column follows the tag (per-agent queries)
        assert mem.agent == NEW_OWNER

    def test_reown_same_owner_is_a_noop(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        row = _seed_legacy_board_row(mgr)

        result = mgr.reown(str(row.id), agent=LEGACY_OWNER, reason="idempotent re-check")

        assert result["changed"] is False
        mem = mgr.get(str(row.id))
        assert mem is not None
        assert f"{AGENT_TAG_PREFIX}{LEGACY_OWNER}" in mem.tags
        assert f"{AGENT_TAG_PREFIX}{NEW_OWNER}" not in mem.tags

    def test_reown_refuses_not_found_loudly(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        with pytest.raises(ValueError, match="not found"):
            mgr.reown("does-not-exist", agent=NEW_OWNER, reason="probe")

    def test_reown_refuses_tombstoned_loudly(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        row = _seed_legacy_board_row(mgr)
        mgr.delete_memory(str(row.id), actor="owner-cli", reason="retire the card")

        with pytest.raises(ValueError, match="tombstoned"):
            mgr.reown(str(row.id), agent=NEW_OWNER, reason="too late")

    def test_reown_requires_reason(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        row = _seed_legacy_board_row(mgr)
        with pytest.raises(ValueError, match="reason is required"):
            mgr.reown(str(row.id), agent=NEW_OWNER, reason="   ")

    def test_reown_requires_agent(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        row = _seed_legacy_board_row(mgr)
        with pytest.raises(ValueError, match="agent is required"):
            mgr.reown(str(row.id), agent="", reason="probe")

    def test_reown_rejects_malformed_slug(self, store_factory: Any) -> None:
        """The strict tag contract judges the slug shape — 'Bad Slug!' is
        not a slug and the write refuses instead of minting garbage."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        row = _seed_legacy_board_row(mgr)
        with pytest.raises(ValueError, match="reown refused"):
            mgr.reown(str(row.id), agent="Bad Slug!", reason="probe")
        assert _owner_of(mgr, str(row.id)) == LEGACY_OWNER  # untouched

    def test_reown_writes_the_audit_line(
        self, store_factory: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        row = _seed_legacy_board_row(mgr)
        with caplog.at_level(logging.WARNING, logger="vesma.manager"):
            mgr.reown(str(row.id), agent=NEW_OWNER, reason="hand over to live team")
        audit = [r for r in caplog.records if "reown:" in r.getMessage()]
        assert audit, "one audit log line expected"
        line = audit[-1].getMessage()
        assert LEGACY_OWNER in line and NEW_OWNER in line and "hand over to live team" in line


# ── The W3 gate AFTER reown (the observable unlock) ──────────────────────────


class TestWorkflowSetAfterReown:
    async def test_set_passes_under_the_new_owner(self, store_factory: Any) -> None:
        """End-to-end: the legacy row a live token could NOT mutate becomes
        mutable by the NEW owner's identity — vesma_workflow set proceeds
        and the transition applies."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        row = _seed_legacy_board_row(mgr)
        memory_id = str(row.id)

        # Pre-state (the W3-tail complaint): the NEW owner's token is refused.
        async with mcp_identity(NEW_OWNER):
            refused = await _call_tool_text(
                "vesma_workflow",
                {
                    "action": "set",
                    "memory_id": memory_id,
                    "to": "in-progress",
                    "actor": NEW_OWNER,
                },
            )
        assert "Namespace violation" in refused
        assert mgr.workflow_get(memory_id)["workflow_status"] == "open"

        mgr.reown(memory_id, agent=NEW_OWNER, reason="legacy board hygiene")

        async with mcp_identity(NEW_OWNER):
            text = await _call_tool_text(
                "vesma_workflow",
                {
                    "action": "set",
                    "memory_id": memory_id,
                    "to": "in-progress",
                    "actor": NEW_OWNER,
                },
            )
        assert "Namespace violation" not in text
        assert "❌" not in text
        assert mgr.workflow_get(memory_id)["workflow_status"] == "in-progress"

    async def test_third_party_still_refused_after_reown(self, store_factory: Any) -> None:
        """Reown moves ownership — it does not open the row to everyone."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        row = _seed_legacy_board_row(mgr)
        mgr.reown(str(row.id), agent=NEW_OWNER, reason="hand over")

        async with mcp_identity("unrelated-agent"):
            text = await _call_tool_text(
                "vesma_workflow",
                {
                    "action": "set",
                    "memory_id": str(row.id),
                    "to": "in-progress",
                    "actor": "unrelated-agent",
                },
            )
        assert "Namespace violation" in text


# ── CLI surface ───────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point VESMA_CONFIG at an empty YAML so the CLI uses tmp_path."""
    from vesma.cli._manager import reset_manager

    reset_manager()
    cfg = tmp_path / "vesma.yaml"
    cfg.write_text(
        f"vesma:\n"
        f"  vault_path: {tmp_path / 'vault'}\n"
        f"  data_dir: {tmp_path / 'data'}\n"
        f"  db_name: cli-reown.db\n"
        f"embedding:\n"
        f"  provider: nano\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VESMA_CONFIG", str(cfg))
    yield cfg
    reset_manager()


class TestReownCli:
    def test_cli_reown_happy_path(self, isolated_config: Path) -> None:
        from vesma.cli._manager import get_manager

        mgr = get_manager("")
        row = _seed_legacy_board_row(mgr)

        result = runner.invoke(
            app,
            ["reown", str(row.id), "--agent", NEW_OWNER, "--reason", "wave hygiene"],
        )
        assert result.exit_code == 0, result.output
        assert _owner_of(mgr, str(row.id)) == NEW_OWNER

    def test_cli_reown_missing_reason_is_a_click_error(self, isolated_config: Path) -> None:
        from vesma.cli._manager import get_manager

        mgr = get_manager("")
        row = _seed_legacy_board_row(mgr)

        result = runner.invoke(app, ["reown", str(row.id), "--agent", NEW_OWNER])
        assert result.exit_code == 2  # click usage error — nothing invoked
        assert _owner_of(mgr, str(row.id)) == LEGACY_OWNER

    def test_cli_reown_reason_without_value_is_a_click_error(self, isolated_config: Path) -> None:
        """``--reason`` flag present but no argument — the same exit-2
        usage error (card requirement: reason is not skippable)."""
        from vesma.cli._manager import get_manager

        mgr = get_manager("")
        row = _seed_legacy_board_row(mgr)

        result = runner.invoke(app, ["reown", str(row.id), "--agent", NEW_OWNER, "--reason"])
        assert result.exit_code == 2
        assert _owner_of(mgr, str(row.id)) == LEGACY_OWNER

    def test_cli_reown_unknown_id_exits_1(self, isolated_config: Path) -> None:
        result = runner.invoke(
            app, ["reown", "no-such-id", "--agent", NEW_OWNER, "--reason", "probe"]
        )
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_cli_reown_tombstoned_exits_1(self, isolated_config: Path) -> None:
        from vesma.cli._manager import get_manager

        mgr = get_manager("")
        row = _seed_legacy_board_row(mgr)
        mgr.delete_memory(str(row.id), actor="owner-cli", reason="retire")

        result = runner.invoke(
            app, ["reown", str(row.id), "--agent", NEW_OWNER, "--reason", "probe"]
        )
        assert result.exit_code == 1
        assert "tombstoned" in result.output

    def test_cli_reown_docstring_warns_reason_is_verbatim_in_audit_log(self) -> None:
        """SEC-E pin: --reason lands in the audit log verbatim — the command
        docstring must warn the operator against secrets in it."""
        assert reown.__doc__ is not None
        assert "verbatim" in reown.__doc__
        assert "audit log" in reown.__doc__
        assert "secrets" in reown.__doc__.lower()
