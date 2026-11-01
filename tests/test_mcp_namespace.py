"""MCP-plane namespace contracts (ADR-0050 §4/§5, wave W3) — the matrix.

Three layers over one contract set:

* **Dispatch-level** (the ``vesma_*`` tool surface itself): the identity
  threading mechanics (``contextvars`` set/reset, ``None`` off-plane),
  the enforced ``agent:<slug>`` reject-loud matrix on EVERY affected
  write surface (vesma_add / vesma_ingest_url / vesma_ingest_document /
  vesma_save_context / vesma_context_rewrite / vesma_compress — the
  mint-protection class extended to the ownership tag, non-agent tags
  untouched), and the ``cross_read`` matrix on every read surface
  (search / agent_recall / recall_context / list_recent / list_tags x
  published_only / own_only x own / foreign-published / foreign-raw /
  unowned rows) with the off-plane (no identity) unchanged-behavior pins.
* **Unit**: the ``TokenQuota`` sliding window — the 120-allowed/121st-
  denied default contract, the injected-clock slide, per-jti windows,
  the None-disabled mode; and the config fail-closed (an unknown
  ``cross_read`` value refuses Settings construction).
* **HTTP** (the real SDK stack, the W1 harness pattern): the gate wiring
  — the token identity threaded from the Bearer verdict into the tool
  dispatch over streamable HTTP, and the per-token 429 with Retry-After
  consumed BEFORE the session manager runs.

The local CLI / stdio plane is pinned by the ``identity=None`` legs: no
test here may observe a behavioral change off the /mcp plane.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.mcp_namespace import (
    AGENT_TAG_PREFIX,
    McpIdentity,
    NamespaceViolationError,
    TokenQuota,
    current_agent,
    current_identity,
    reset_request_identity,
    set_request_identity,
)
from vesma.models import MemoryCreate, MemorySource, MemoryStatus

# ---------------------------------------------------------------------------
# Shared fixtures (dispatch level)
# ---------------------------------------------------------------------------


def _base_settings(
    tmp: Path,
    *,
    cross_read: str = "published_only",
    calls_per_minute: int | None = 120,
    strict_tags: bool = True,
) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
            "strict_tag_contract": strict_tags,
        },
        embedding={"provider": "onnx"},
        scanner={"enabled": False},  # no daemon thread per test
        mcp={
            "namespace": {"cross_read": cross_read},
            "quota": {"calls_per_minute": calls_per_minute},
        },
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def store_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Build isolated MemoryManagers wired into the ``mcp_server`` singleton.

    Yields a factory ``settings -> MemoryManager``; the singleton is
    reset after each test so neighbor modules never see a closed manager.
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
async def mcp_identity(agent: str, jti: str = "jti-test-1") -> AsyncIterator[McpIdentity]:
    """The gate's set/reset discipline, as the tests use it."""
    token = set_request_identity(McpIdentity(agent_id=agent, jti=jti))
    try:
        yield current_identity()
    finally:
        reset_request_identity(token)


def record_owner_tag(tags: list[str]) -> str:
    """Test-local owner reader (beside the seeds, for readability)."""
    return next((t[len("agent:") :] for t in tags if t.startswith("agent:")), "")


async def _dispatch_text(name: str, arguments: dict[str, Any]) -> str:
    """Run one raw dispatch; dicts/strings render as the dispatch returns them."""
    from vesma import mcp_server

    result = await mcp_server._dispatch(name, arguments)
    return result if isinstance(result, str) else json.dumps(result, default=str)


async def _call_tool_text(name: str, arguments: dict[str, Any]) -> str:
    """Full ``call_tool`` path — the loud-violation rendering included."""
    from vesma import mcp_server

    result = await mcp_server.call_tool(name, arguments)
    return result[0].text


def _seed(mgr: MemoryManager, *, owner: str, status: MemoryStatus, marker: str) -> Any:
    """One memory row with a controlled owner tag and status."""
    return mgr.add(
        MemoryCreate(
            content=f"seed {marker} body by {owner}",
            tags=["project:ns-wave", f"{AGENT_TAG_PREFIX}{owner}", "vesma:learning"],
            source=MemorySource.MCP,
            status=status,
        ),
        project="ns-wave",
        agent=owner,
    )


def _seed_matrix(mgr: MemoryManager) -> dict[str, Any]:
    """The four-quadrant store: own/foreign x published/raw, plus unowned."""
    return {
        "own_pub": _seed(mgr, owner="harness-a", status=MemoryStatus.PUBLISHED, marker="own-pub"),
        "own_raw": _seed(mgr, owner="harness-a", status=MemoryStatus.RAW, marker="own-raw"),
        "foreign_pub": _seed(
            mgr, owner="other-agent", status=MemoryStatus.PUBLISHED, marker="foreign-pub"
        ),
        "foreign_raw": _seed(
            mgr, owner="other-agent", status=MemoryStatus.RAW, marker="foreign-raw"
        ),
        "unowned_pub": mgr.add(
            MemoryCreate(
                content="seed unowned-pub body",
                tags=["project:ns-wave", "vesma:learning"],  # no agent tag
                source=MemorySource.MCP,
                status=MemoryStatus.PUBLISHED,
            ),
            project="ns-wave",
            agent="",
        ),
    }


def _marker_ids(seeded: dict[str, Any]) -> dict[str, str]:
    return {key: str(value.id) for key, value in seeded.items()}


async def _save_checkpoint_as(agent: str, project: str, goals: str) -> None:
    """A checkpoint saved under a chosen identity (the gate mechanics)."""
    from vesma import mcp_server

    await mcp_server._dispatch("vesma_save_context", {"project": project, "goals": goals})


# ---------------------------------------------------------------------------
# Identity threading mechanics
# ---------------------------------------------------------------------------


class TestIdentityThreading:
    def test_off_plane_identity_is_none(self) -> None:
        """stdio/CLI/local: no identity var value ever — the W3 null-contract."""
        assert current_identity() is None
        assert current_agent() is None

    async def test_set_and_reset_restores_previous(self) -> None:
        """The gate's set/reset discipline: the var is per-request state."""
        async with mcp_identity("harness-a") as outer:
            assert outer is not None and outer.agent_id == "harness-a"
            assert current_agent() == "harness-a"
            async with mcp_identity("harness-b", jti="jti-2"):
                assert current_agent() == "harness-b"
            assert current_agent() == "harness-a"
        assert current_identity() is None

    async def test_reset_sequence_returns_to_default(self) -> None:
        """Two sequential set/reset pairs unwind to the default — the exact
        shape of the gate's finally across back-to-back requests."""
        first = set_request_identity(McpIdentity(agent_id="a", jti="j1"))
        second = set_request_identity(McpIdentity(agent_id="b", jti="j2"))
        reset_request_identity(second)
        assert current_agent() == "a"
        reset_request_identity(first)
        assert current_identity() is None

    async def test_dispatch_executes_under_identity(
        self, store_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dispatch runs with the gate-published identity in context —
        proven from inside a dispatched handler (the vesma_stats leg, whose
        manager call the test spies on)."""
        from vesma import mcp_server

        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        seen: dict[str, str | None] = {}
        original_stats = mgr.stats

        def spy_stats() -> dict[str, Any]:
            identity = mcp_server.current_identity()
            seen["agent"] = identity.agent_id if identity else None
            return original_stats()

        monkeypatch.setattr(mgr, "stats", spy_stats)
        async with mcp_identity("harness-a", jti="jti-probe"):
            await mcp_server._dispatch("vesma_stats", {})
        assert seen["agent"] == "harness-a"

        # and off-plane the same dispatch sees None (the unchanged contract)
        seen.clear()
        await mcp_server._dispatch("vesma_stats", {})
        assert seen["agent"] is None


class TestIdentityEndToEndDispatch:
    async def test_add_under_identity_matching_tag_stores(self, store_factory: Any) -> None:
        """A matching client tag passes enforcement; the row lands under it."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_add",
                {
                    "content": "matching-owner body",
                    "tags": ["project:ns-wave", "agent:harness-a", "vesma:learning"],
                },
            )
        assert "❌" not in text
        assert any(record_owner_tag(m.tags) == "harness-a" for m in mgr.list_recent(limit=10))


# ---------------------------------------------------------------------------
# Enforced agent:<slug> — reject-loud, per affected surface
# ---------------------------------------------------------------------------


class TestEnforcedAgentTags:
    async def test_add_foreign_agent_tag_is_loud(self, store_factory: Any) -> None:
        """vesma_add: a foreign agent:<slug> tag is a TYPED loud refusal."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        async with mcp_identity("harness-a", jti="jti-add"):
            text = await _call_tool_text(
                "vesma_add",
                {
                    "content": "foreign mint attempt",
                    "tags": ["project:ns-wave", "agent:other-agent", "vesma:learning"],
                },
            )
        assert "Namespace violation" in text
        assert "other-agent" in text
        assert "agent:harness-a" in text  # the sanctioned namespace named in the error
        # nothing written — the mint is refused, not silently re-owned
        assert all(record_owner_tag(m.tags) != "other-agent" for m in mgr.list_recent(limit=50))

    def test_violation_is_typed(self) -> None:
        """The error type is distinct on purpose (not a ValueError) so no
        generic validation path can swallow it."""
        assert not issubclass(NamespaceViolationError, ValueError)

    async def test_add_local_call_foreign_tag_unchanged(self, store_factory: Any) -> None:
        """Off-plane (identity=None): a foreign agent tag behaves pre-W3."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        text = await _call_tool_text(
            "vesma_add",
            {
                "content": "local foreign-owner body",
                "tags": ["project:ns-wave", "agent:local-agent", "vesma:learning"],
            },
        )
        assert "❌" not in text
        assert any(record_owner_tag(m.tags) == "local-agent" for m in mgr.list_recent(limit=10))

    async def test_add_non_agent_tags_not_touched(self, store_factory: Any) -> None:
        """project:*/vesma:* namespaces are NOT enforced — only agent:* is."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_add",
                {
                    "content": "foreign project namespace is fine",
                    "tags": [
                        "project:totally-other-project",
                        "agent:harness-a",
                        "vesma:learning",
                    ],
                },
            )
        assert "❌" not in text
        assert any("project:totally-other-project" in m.tags for m in mgr.list_recent(limit=10))

    async def test_ingest_url_foreign_agent_tag_is_loud(self, store_factory: Any) -> None:
        """vesma_ingest_url: the rejection happens BEFORE any fetch runs."""
        store_factory(_base_settings(Path(tempfile.mkdtemp())))
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_ingest_url",
                {
                    "url": "https://example.invalid/never-fetched",
                    "tags": ["project:ns-wave", "agent:other-agent", "vesma:learning"],
                },
            )
        assert "Namespace violation" in text
        assert "other-agent" in text

    async def test_ingest_document_foreign_agent_tag_is_loud(self, store_factory: Any) -> None:
        """vesma_ingest_document: the chunked writer obeys the same namespace."""
        store_factory(_base_settings(Path(tempfile.mkdtemp())))
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_ingest_document",
                {
                    "text": "document body",
                    "doc_id": "doc-1",
                    "tags": ["project:ns-wave", "agent:other-agent", "vesma:learning"],
                },
            )
        assert "Namespace violation" in text

    async def test_ingest_document_own_agent_tag_stores(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_ingest_document",
                {
                    "text": "document body under own namespace",
                    "doc_id": "doc-2",
                    "tags": ["project:ns-wave", "agent:harness-a", "vesma:learning"],
                },
            )
        assert "❌" not in text
        assert any(record_owner_tag(m.tags) == "harness-a" for m in mgr.list_recent(limit=10))

    async def test_save_context_explicit_foreign_agent_is_loud(self, store_factory: Any) -> None:
        """vesma_save_context: an explicit foreign checkpoint agent is refused."""
        store_factory(_base_settings(Path(tempfile.mkdtemp())))
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_save_context",
                {
                    "project": "ns-wave",
                    "goals": "try to claim a foreign identity",
                    "agent": "other-agent",
                },
            )
        assert "Namespace violation" in text
        assert "other-agent" in text

    async def test_save_context_defaults_to_identity_agent(self, store_factory: Any) -> None:
        """An omitted agent argument mints the checkpoint under the IDENTITY,
        never the legacy ``user`` default (isolation coherence)."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_save_context",
                {"project": "ns-wave", "goals": "checkpoint under identity default"},
            )
        assert "✅" in text
        recalled = mgr.recall_context(project="ns-wave", limit=5)
        assert recalled
        assert all(record_owner_tag(m.tags) == "harness-a" for m in recalled)

    async def test_save_context_local_default_still_user(self, store_factory: Any) -> None:
        """Off-plane the legacy ``user`` default is byte-identical (pin)."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        text = await _dispatch_text(
            "vesma_save_context",
            {"project": "ns-wave", "goals": "local default"},
        )
        assert "✅" in text
        recalled = mgr.recall_context(project="ns-wave", limit=5)
        assert all(record_owner_tag(m.tags) == "user" for m in recalled)

    async def test_context_rewrite_foreign_agent_is_loud(self, store_factory: Any) -> None:
        """vesma_context_rewrite: the rewrite channel's owner is enforced."""
        store_factory(_base_settings(Path(tempfile.mkdtemp())))
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_context_rewrite",
                {
                    "content": "rewrite body",
                    "project": "ns-wave",
                    "agent": "other-agent",
                },
            )
        assert "Namespace violation" in text

    async def test_context_rewrite_own_agent_passes(self, store_factory: Any) -> None:
        store_factory(_base_settings(Path(tempfile.mkdtemp())))
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_context_rewrite",
                {"content": "rewrite body own", "project": "ns-wave", "agent": "harness-a"},
            )
        assert "❌" not in text

    async def test_compress_foreign_agent_is_loud(self, store_factory: Any) -> None:
        """vesma_compress: the CCR issuer ledger refuses a foreign agent mint."""
        store_factory(_base_settings(Path(tempfile.mkdtemp())))
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_compress",
                {"text": "compressible body", "agent": "other-agent"},
            )
        assert "Namespace violation" in text

    async def test_lax_mode_patched_unknown_agent_is_rejected_under_identity(
        self, store_factory: Any
    ) -> None:
        """The contract's lax patch (missing agent tag → ``agent:unknown``) is
        an equally foreign mint under identity — rejected loud, not slipped."""
        store_factory(_base_settings(Path(tempfile.mkdtemp()), strict_tags=False))
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_add",
                {"content": "lax no-agent-tag body", "tags": ["project:ns-wave", "vesma:learning"]},
            )
        assert "Namespace violation" in text
        assert "agent:unknown" in text


# ---------------------------------------------------------------------------
# cross_read matrix (dispatch-level post-filter)
# ---------------------------------------------------------------------------


class TestCrossReadPublishedOnly:
    @pytest.fixture
    def env(self, store_factory: Any) -> Iterator[dict[str, Any]]:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = store_factory(_base_settings(Path(tmpdir), cross_read="published_only"))
            seeded = _seed_matrix(mgr)
            token = set_request_identity(McpIdentity(agent_id="harness-a", jti="jti-cross"))
            try:
                yield {"mgr": mgr, "seeded": seeded}
            finally:
                reset_request_identity(token)

    async def test_search_foreign_raw_hidden_rest_visible(self, env: dict[str, Any]) -> None:
        # include_raw: vesma_search defaults to published-only entries, so the
        # raw legs of the matrix need the opt-in (the search-status contract).
        text = await _dispatch_text(
            "vesma_search", {"query": "seed", "limit": 50, "include_raw": True}
        )
        ids = _marker_ids(env["seeded"])
        assert ids["own_pub"] in text
        assert ids["own_raw"] in text  # own rows are always full
        assert ids["foreign_pub"] in text  # published_only: published foreign visible
        assert ids["foreign_raw"] not in text
        assert ids["unowned_pub"] in text  # unowned + published = visible foreign

    async def test_list_recent_matrix(self, env: dict[str, Any]) -> None:
        text = await _dispatch_text("vesma_list_recent", {"limit": 50})
        ids = _marker_ids(env["seeded"])
        assert ids["own_raw"] in text
        assert ids["foreign_pub"] in text
        assert ids["foreign_raw"] not in text

    async def test_agent_recall_foreign_raw_hidden(self, env: dict[str, Any]) -> None:
        """agent_recall can address any slug — foreign rows obey the aperture."""
        own = await _dispatch_text("vesma_agent_recall", {"agent": "harness-a"})
        foreign = await _dispatch_text("vesma_agent_recall", {"agent": "other-agent"})
        assert "own-pub" in own and "own-raw" in own
        # the foreign agent's RAW rows are hidden even when addressed by slug
        assert "foreign-raw" not in foreign
        assert "foreign-pub" in foreign

    async def test_list_tags_counts_recomputed(self, env: dict[str, Any]) -> None:
        """The global tag counts cannot be post-filtered — under identity they
        are recomputed over the records the aperture serves."""
        text = await _dispatch_text("vesma_list_tags", {})
        counts = json.loads(text)
        assert counts.get("project:ns-wave") == 4  # own_pub/own_raw/foreign_pub/unowned_pub
        assert counts.get("agent:other-agent") == 1  # the published foreign row
        assert counts.get("agent:harness-a") == 2

    async def test_recall_context_checkpoints(self, env: dict[str, Any]) -> None:
        """Checkpoints: own in full; foreign clean checkpoints publish
        immediately (immediate visibility) → visible under published_only."""
        await _save_checkpoint_as("harness-a", "ns-wave", "checkpoint from harness-a")
        async with mcp_identity("other-agent", jti="jti-foreign"):
            await _save_checkpoint_as("other-agent", "ns-wave", "checkpoint from other-agent")
        text = await _dispatch_text("vesma_recall_context", {"project": "ns-wave"})
        assert "checkpoint from harness-a" in text
        assert "checkpoint from other-agent" in text  # published foreign visible


class TestCrossReadOwnOnly:
    @pytest.fixture
    def env(self, store_factory: Any) -> Iterator[dict[str, Any]]:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = store_factory(_base_settings(Path(tmpdir), cross_read="own_only"))
            seeded = _seed_matrix(mgr)
            token = set_request_identity(McpIdentity(agent_id="harness-a", jti="jti-cross2"))
            try:
                yield {"mgr": mgr, "seeded": seeded}
            finally:
                reset_request_identity(token)

    async def test_search_only_own_rows(self, env: dict[str, Any]) -> None:
        text = await _dispatch_text(
            "vesma_search", {"query": "seed", "limit": 50, "include_raw": True}
        )
        ids = _marker_ids(env["seeded"])
        assert ids["own_pub"] in text
        assert ids["own_raw"] in text
        assert ids["foreign_pub"] not in text
        assert ids["foreign_raw"] not in text
        assert ids["unowned_pub"] not in text

    async def test_list_recent_only_own_rows(self, env: dict[str, Any]) -> None:
        text = await _dispatch_text("vesma_list_recent", {"limit": 50})
        ids = _marker_ids(env["seeded"])
        assert ids["own_raw"] in text
        assert ids["foreign_pub"] not in text

    async def test_list_tags_only_own_counts(self, env: dict[str, Any]) -> None:
        text = await _dispatch_text("vesma_list_tags", {})
        counts = json.loads(text)
        assert counts.get("agent:other-agent") is None
        assert counts.get("agent:harness-a") == 2
        assert counts.get("project:ns-wave") == 2

    async def test_recall_context_foreign_checkpoint_hidden(self, env: dict[str, Any]) -> None:
        await _save_checkpoint_as("harness-a", "ns-wave", "own checkpoint")
        async with mcp_identity("other-agent", jti="jti-foreign2"):
            await _save_checkpoint_as("other-agent", "ns-wave", "foreign checkpoint")
        text = await _dispatch_text("vesma_recall_context", {"project": "ns-wave"})
        assert "own checkpoint" in text
        assert "foreign checkpoint" not in text


class TestCrossReadOffPlaneUnchanged:
    @pytest.fixture
    def env(self, store_factory: Any) -> Iterator[dict[str, Any]]:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = store_factory(_base_settings(Path(tmpdir), cross_read="own_only"))
            seeded = _seed_matrix(mgr)
            assert current_identity() is None
            yield {"mgr": mgr, "seeded": seeded}

    async def test_no_identity_no_filter_even_under_own_only(self, env: dict[str, Any]) -> None:
        """The strictest config changes NOTHING off-plane (local contract)."""
        text = await _dispatch_text(
            "vesma_search", {"query": "seed", "limit": 50, "include_raw": True}
        )
        ids = _marker_ids(env["seeded"])
        assert ids["own_raw"] in text
        assert ids["foreign_raw"] in text
        list_text = await _dispatch_text("vesma_list_recent", {"limit": 50})
        assert ids["foreign_pub"] in list_text

    async def test_local_list_tags_full_counts(self, env: dict[str, Any]) -> None:
        text = await _dispatch_text("vesma_list_tags", {})
        counts = json.loads(text)
        assert counts.get("project:ns-wave") == 5  # every seeded row


# ---------------------------------------------------------------------------
# Row-ownership gates on the MUTATING surfaces (TL spot-check slice):
# vesma_tags (bulk add/remove/rename), vesma_workflow:set, vesma_import —
# plus the cross_read aperture on the export READ surface.
# ---------------------------------------------------------------------------


def _row_with_tags(mgr: MemoryManager, *, owner: str, extra_tags: list[str], marker: str) -> Any:
    """A row with full control over the tag list (bulk-gate seeds)."""
    tags = ["project:ns-wave", "vesma:learning"]
    if owner:
        tags.insert(1, f"agent:{owner}")
    tags.extend(extra_tags)
    return mgr.add(
        MemoryCreate(
            content=f"bulk-gate {marker}",
            tags=tags,
            source=MemorySource.MCP,
            status=MemoryStatus.PUBLISHED,
        ),
        project="ns-wave",
        agent=owner,
    )


class TestBulkTagsOwnership:
    """vesma_tags add/remove/rename: a row the identity does not own is
    refused; own rows keep full freedom (incl. repairing their namespace
    tags). Refusals hold for dry_run previews too — a forbidden mutation
    is not even previewable."""

    async def test_add_foreign_row_in_scope_is_loud(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        foreign = _row_with_tags(mgr, owner="other-agent", extra_tags=[], marker="add-f")
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_tags",
                {
                    "action": "add",
                    "tags": ["severity:high"],
                    "dry_run": False,
                },
            )
        assert "Namespace violation" in text
        assert "other-agent" in text
        # nothing written — the foreign row stayed untouched
        assert "severity:high" not in mgr.get(str(foreign.id)).tags  # type: ignore[union-attr]

    async def test_add_foreign_agent_tag_mint_is_loud_even_on_own_rows(
        self, store_factory: Any
    ) -> None:
        """The mint gate runs FIRST: introducing a NEW foreign agent:* tag is
        refused even when every row in scope is the identity's own."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        own = _row_with_tags(mgr, owner="harness-a", extra_tags=[], marker="add-mint")
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_tags",
                {"action": "add", "tags": ["agent:other-agent"], "dry_run": False},
            )
        assert "Namespace violation" in text
        assert "agent:other-agent" not in mgr.get(str(own.id)).tags  # type: ignore[union-attr]

    async def test_add_own_rows_applies(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        own = _row_with_tags(mgr, owner="harness-a", extra_tags=[], marker="add-ok")
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_tags",
                {"action": "add", "tags": ["severity:high"], "dry_run": False},
            )
        assert "❌" not in text
        assert "severity:high" in mgr.get(str(own.id)).tags  # type: ignore[union-attr]

    async def test_add_local_unscoped_unchanged(self, store_factory: Any) -> None:
        """Off-plane: a foreign-owned row in scope is happily mutated (pin)."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        foreign = _row_with_tags(mgr, owner="other-agent", extra_tags=[], marker="add-local")
        text = await _dispatch_text(
            "vesma_tags", {"action": "add", "tags": ["severity:low"], "dry_run": False}
        )
        assert "❌" not in text
        assert "severity:low" in mgr.get(str(foreign.id)).tags  # type: ignore[union-attr]

    async def test_remove_foreign_row_is_loud(self, store_factory: Any) -> None:
        """Ripping a tag off a FOREIGN row (ownership tags included) is refused."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        foreign = _row_with_tags(
            mgr, owner="other-agent", extra_tags=["severity:high"], marker="rm-f"
        )
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_tags",
                {"action": "remove", "tags": ["severity:high"], "dry_run": False},
            )
        assert "Namespace violation" in text
        assert "severity:high" in mgr.get(str(foreign.id)).tags  # type: ignore[union-attr]

    async def test_remove_own_row_incl_own_agent_tag_ok(self, store_factory: Any) -> None:
        """Own row: full freedom — the namespace gate does not fire on your
        own rows, even when the removal list carries your own agent: tag.
        The manager's own strict CONTRACT gate remains the last line: a
        removal that would strip the last agent: tag is refused per-row by
        the manager (the report carries the contract error, nothing
        written) — the namespace layer never pre-empts that."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        own = _row_with_tags(mgr, owner="harness-a", extra_tags=["severity:high"], marker="rm-ok")
        async with mcp_identity("harness-a"):
            applied = await _dispatch_text(
                "vesma_tags",
                {"action": "remove", "tags": ["severity:high"], "dry_run": False},
            )
            own_tag = await _dispatch_text(
                "vesma_tags",
                {"action": "remove", "tags": ["agent:harness-a"], "dry_run": True},
            )
        assert "❌" not in applied and "❌" not in own_tag
        row = mgr.get(str(own.id))
        assert row is not None
        assert "severity:high" not in row.tags  # the legal removal applied
        # the own-agent-tag removal passed the namespace gate; the manager's
        # contract gate refused the contract-breaking write instead
        assert "changed" in own_tag
        row2 = mgr.get(str(own.id))
        assert row2 is not None
        assert "agent:harness-a" in row2.tags

    async def test_rename_agent_prefix_refused_entirely(self, store_factory: Any) -> None:
        """FROM or TO the agent: namespace — refused WHATEVER the rows own
        (a bulk op spans foreign rows by construction)."""
        store_factory(_base_settings(Path(tempfile.mkdtemp())))
        async with mcp_identity("harness-a"):
            to_agent = await _call_tool_text(
                "vesma_tags",
                {"action": "rename", "from_prefix": "gcw:", "to_prefix": "agent:"},
            )
            from_agent = await _call_tool_text(
                "vesma_tags",
                {"action": "rename", "from_prefix": "agent:", "to_prefix": "newns:"},
            )
        assert "Namespace violation" in to_agent
        assert "Namespace violation" in from_agent

    async def test_rename_foreign_row_is_loud(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        foreign = _row_with_tags(mgr, owner="other-agent", extra_tags=["gcw:rule"], marker="rn-f")
        own = _row_with_tags(mgr, owner="harness-a", extra_tags=["gcw:rule"], marker="rn-o")
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_tags",
                {
                    "action": "rename",
                    "from_prefix": "gcw:",
                    "to_prefix": "vesma:",
                    "dry_run": False,
                },
            )
        assert "Namespace violation" in text
        # neither row rewritten — the gate runs BEFORE the manager
        assert "gcw:rule" in mgr.get(str(foreign.id)).tags  # type: ignore[union-attr]
        assert "gcw:rule" in mgr.get(str(own.id)).tags  # type: ignore[union-attr]

    async def test_rename_own_rows_applies(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        own = _row_with_tags(mgr, owner="harness-a", extra_tags=["gcw:rule"], marker="rn-ok")
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_tags",
                {
                    "action": "rename",
                    "from_prefix": "gcw:",
                    "to_prefix": "vesma:",
                    "dry_run": False,
                },
            )
        assert "❌" not in text
        assert "vesma:rule" in mgr.get(str(own.id)).tags  # type: ignore[union-attr]

    async def test_rename_nofederate_marker_on_foreign_row_skipped_not_gated(
        self, store_factory: Any
    ) -> None:
        """The byte-stable mnemos:no-federate marker is SKIPPED by the rename
        (never renamed) — a foreign row carrying ONLY the marker is not an
        affected row and the operation passes (exact-predicate parity pin)."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        foreign = _row_with_tags(
            mgr, owner="other-agent", extra_tags=["mnemos:no-federate"], marker="nf"
        )
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_tags",
                {
                    "action": "rename",
                    "from_prefix": "mnemos:",
                    "to_prefix": "vesma:",
                    "dry_run": False,
                },
            )
        assert "❌" not in text
        # the marker survived untouched on the foreign row
        assert "mnemos:no-federate" in mgr.get(str(foreign.id)).tags  # type: ignore[union-attr]

    async def test_rename_local_agent_prefix_unchanged(self, store_factory: Any) -> None:
        """Off-plane: the agent: prefix rename behaves exactly as pre-W3."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        _row_with_tags(mgr, owner="legacy-agent", extra_tags=["gcw:rule"], marker="loc")
        text = await _dispatch_text(
            "vesma_tags",
            {"action": "rename", "from_prefix": "gcw:", "to_prefix": "agent:", "dry_run": False},
        )
        assert "❌" not in text


class TestWorkflowSetOwnership:
    async def test_set_foreign_row_is_loud(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        foreign = _seed(mgr, owner="other-agent", status=MemoryStatus.PUBLISHED, marker="wf-f")
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_workflow",
                {
                    "action": "set",
                    "memory_id": str(foreign.id),
                    "to": "in-progress",
                    "actor": "harness-a",
                },
            )
        assert "Namespace violation" in text
        status = mgr.workflow_get(str(foreign.id))
        assert status is not None and status["workflow_status"] == "open"  # untouched

    async def test_set_unowned_row_is_foreign(self, store_factory: Any) -> None:
        """An unowned row ("") is NOT a wildcard — same convention as cross_read."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        unowned = mgr.add(
            MemoryCreate(
                content="unowned workflow target",
                tags=["project:ns-wave", "vesma:learning"],
                source=MemorySource.MCP,
                status=MemoryStatus.PUBLISHED,
            ),
            project="ns-wave",
            agent="",
        )
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_workflow",
                {
                    "action": "set",
                    "memory_id": str(unowned.id),
                    "to": "in-progress",
                    "actor": "harness-a",
                },
            )
        assert "Namespace violation" in text

    async def test_set_own_row_applies(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        own = _seed(mgr, owner="harness-a", status=MemoryStatus.PUBLISHED, marker="wf-o")
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_workflow",
                {
                    "action": "set",
                    "memory_id": str(own.id),
                    "to": "in-progress",
                    "actor": "harness-a",
                },
            )
        assert "❌" not in text
        status = mgr.workflow_get(str(own.id))
        assert status is not None and status["workflow_status"] == "in-progress"

    async def test_set_local_foreign_row_unchanged(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        foreign = _seed(mgr, owner="other-agent", status=MemoryStatus.PUBLISHED, marker="wf-l")
        text = await _dispatch_text(
            "vesma_workflow",
            {
                "action": "set",
                "memory_id": str(foreign.id),
                "to": "in-progress",
                "actor": "whoever",
            },
        )
        assert "❌" not in text


class TestImportNamespacePrescan:
    async def test_import_foreign_agent_tag_row_is_loud(self, store_factory: Any) -> None:
        """A row minting a foreign agent:<slug> through an export file is
        refused BEFORE any write; nothing lands in the store."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        payload = {
            "format_version": "1",
            "memories": [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "content": "imported foreign-mint row",
                    "tags": ["project:ns-wave", "agent:other-agent", "vesma:learning"],
                    "status": "published",
                }
            ],
        }
        source = Path(tempfile.mkdtemp()) / "foreign.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        before = mgr.stats()["total"]
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_import",
                {"source_path": str(source), "mode": "merge"},
            )
        assert "Namespace violation" in text
        assert "row 0" in text
        assert mgr.stats()["total"] == before  # nothing written

    async def test_import_own_tag_row_imports(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        payload = {
            "format_version": "1",
            "memories": [
                {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "content": "imported own-namespace row",
                    "tags": ["project:ns-wave", "agent:harness-a", "vesma:learning"],
                    "status": "published",
                }
            ],
        }
        source = Path(tempfile.mkdtemp()) / "own.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_import", {"source_path": str(source), "mode": "merge"}
            )
        assert "❌" not in text
        assert mgr.get("22222222-2222-4222-8222-222222222222") is not None

    async def test_import_local_foreign_tag_unchanged(self, store_factory: Any) -> None:
        """Off-plane: a foreign agent: tag imports exactly as pre-W3 (pin)."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        payload = {
            "format_version": "1",
            "memories": [
                {
                    "id": "33333333-3333-4333-8333-333333333333",
                    "content": "local import with foreign tag",
                    "tags": ["project:ns-wave", "agent:other-agent", "vesma:learning"],
                    "status": "published",
                }
            ],
        }
        source = Path(tempfile.mkdtemp()) / "local.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        text = await _dispatch_text("vesma_import", {"source_path": str(source), "mode": "merge"})
        assert "❌" not in text
        assert mgr.get("33333333-3333-4333-8333-333333333333") is not None


class TestExportCrossRead:
    """The export READ surface obeys the same aperture as search/recall —
    otherwise a harness downloads the whole store around cross_read."""

    async def _export_payload(self, mgr: MemoryManager, identity: str | None, tmp: Path) -> Any:
        from vesma.cli.export import parse_json_export

        output = tmp / "export.json"
        if identity is not None:
            async with mcp_identity(identity):
                text = await _dispatch_text("vesma_export", {"output_path": str(output)})
        else:
            text = await _dispatch_text("vesma_export", {"output_path": str(output)})
        assert "❌" not in text
        return parse_json_export(output.read_bytes())

    async def test_export_published_only_filters_foreign_rows(self, store_factory: Any) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = store_factory(_base_settings(Path(tmpdir), cross_read="published_only"))
            seeded = _seed_matrix(mgr)
            ids = _marker_ids(seeded)
            payload = await self._export_payload(mgr, "harness-a", Path(tmpdir))
            exported_ids = {row["id"] for row in payload["memories"]}
            assert ids["own_pub"] in exported_ids
            assert ids["own_raw"] in exported_ids
            assert ids["foreign_pub"] in exported_ids
            assert ids["foreign_raw"] not in exported_ids
            assert ids["unowned_pub"] in exported_ids
            summary = payload["redaction_summary"]
            assert summary.get("excluded_cross_read") == 1  # exactly the foreign raw row

    async def test_export_own_only_filters_all_foreign(self, store_factory: Any) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = store_factory(_base_settings(Path(tmpdir), cross_read="own_only"))
            seeded = _seed_matrix(mgr)
            ids = _marker_ids(seeded)
            payload = await self._export_payload(mgr, "harness-a", Path(tmpdir))
            exported_ids = {row["id"] for row in payload["memories"]}
            assert exported_ids == {ids["own_pub"], ids["own_raw"]}

    async def test_export_off_plane_full_store(self, store_factory: Any) -> None:
        """Off-plane: no row filter — the export stays byte-identical (pin)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = store_factory(_base_settings(Path(tmpdir), cross_read="own_only"))
            seeded = _seed_matrix(mgr)
            ids = _marker_ids(seeded)
            payload = await self._export_payload(mgr, None, Path(tmpdir))
            exported_ids = {row["id"] for row in payload["memories"]}
            assert len(exported_ids) == 5
            assert ids["foreign_raw"] in exported_ids
            assert "excluded_cross_read" not in payload["redaction_summary"]


# ---------------------------------------------------------------------------
# Cascade-review fix slice (F-1…F-8): import overwrite ownership, CCR
# redeem gate, hooks/assemble identity args, filter/workflow-read
# semantics, config mis-level keys, DR-25 signage window, bulk-gate
# set-equality mirror.
# ---------------------------------------------------------------------------


def _export_payload_file(rows: list[dict[str, Any]]) -> Path:
    """A minimal valid JSON export file carrying the given rows."""
    source = Path(tempfile.mkdtemp()) / "payload.json"
    source.write_text(json.dumps({"format_version": "1", "memories": rows}), encoding="utf-8")
    return source


class TestImportOverwriteOwnership:
    """F-1: merge+overwrite replaces a row by id WHOLESALE — every id the
    import would replace must belong to the request identity."""

    async def test_overwrite_foreign_row_is_loud(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        foreign = _seed(mgr, owner="other-agent", status=MemoryStatus.PUBLISHED, marker="ov-f")
        source = _export_payload_file(
            [
                {
                    "id": str(foreign.id),
                    "content": "hostile overwrite body",
                    "tags": ["project:ns-wave", "agent:harness-a", "vesma:learning"],
                    "status": "published",
                }
            ]
        )
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_import", {"source_path": str(source), "mode": "merge", "overwrite": True}
            )
        assert "Namespace violation" in text
        assert "other-agent" in text
        row = mgr.get(str(foreign.id))
        assert row is not None
        assert row.content == "seed ov-f body by other-agent"  # intact — no silent wipe

    async def test_overwrite_false_skips_foreign_row_unchanged(self, store_factory: Any) -> None:
        """Without overwrite the existing id is SKIPPED (no mutation) — the
        ownership gate does not fire, the row stays byte-identical."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        foreign = _seed(mgr, owner="other-agent", status=MemoryStatus.PUBLISHED, marker="ov-s")
        source = _export_payload_file(
            [
                {
                    "id": str(foreign.id),
                    "content": "would-be overwrite body",
                    "tags": ["project:ns-wave", "agent:harness-a", "vesma:learning"],
                    "status": "published",
                }
            ]
        )
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_import",
                {"source_path": str(source), "mode": "merge", "overwrite": False},
            )
        assert "❌" not in text
        row = mgr.get(str(foreign.id))
        assert row is not None
        assert row.content == "seed ov-s body by other-agent"

    async def test_overwrite_own_row_applies(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        own = _seed(mgr, owner="harness-a", status=MemoryStatus.PUBLISHED, marker="ov-o")
        source = _export_payload_file(
            [
                {
                    "id": str(own.id),
                    "content": "legitimate self-overwrite body",
                    "tags": ["project:ns-wave", "agent:harness-a", "vesma:learning"],
                    "status": "published",
                }
            ]
        )
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_import", {"source_path": str(source), "mode": "merge", "overwrite": True}
            )
        assert "❌" not in text
        row = mgr.get(str(own.id))
        assert row is not None
        assert row.content == "legitimate self-overwrite body"

    async def test_overwrite_local_foreign_row_unchanged(self, store_factory: Any) -> None:
        """Off-plane: merge+overwrite of a foreign row behaves pre-W3 (pin)."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        foreign = _seed(mgr, owner="other-agent", status=MemoryStatus.PUBLISHED, marker="ov-l")
        source = _export_payload_file(
            [
                {
                    "id": str(foreign.id),
                    "content": "local overwrite body",
                    "tags": ["project:ns-wave", "agent:other-agent", "vesma:learning"],
                    "status": "published",
                }
            ]
        )
        text = await _dispatch_text(
            "vesma_import", {"source_path": str(source), "mode": "merge", "overwrite": True}
        )
        assert "❌" not in text
        row = mgr.get(str(foreign.id))
        assert row is not None
        assert row.content == "local overwrite body"


class TestRetrieveRedeemGate:
    """F-2: CCR markers ride published content by design — a bare hash must
    not bypass cross_read. Under identity the cache row's issuer agent must
    BE the request identity (violation, consistent with the workflow gate);
    an unknown hash keeps retrieve_content's found=False contract."""

    async def _cached_entry(self, mgr: MemoryManager, agent: str | None) -> str:
        """Cache one >min_size entry under the given issuer; return the hash."""
        text = "redeem-gate original payload. " * 30  # > 500 chars → cached
        result = mgr.compress_content(text, agent=agent)
        h = result["hash"]
        assert h
        return h

    async def test_redeem_foreign_cache_entry_is_loud(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        h = await self._cached_entry(mgr, agent="other-agent")
        async with mcp_identity("harness-a"):
            text = await _call_tool_text("vesma_retrieve", {"hash": h})
        assert "Namespace violation" in text
        assert "other-agent" in text

    async def test_redeem_own_cache_entry_ok(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        h = await self._cached_entry(mgr, agent="harness-a")
        async with mcp_identity("harness-a"):
            text = await _dispatch_text("vesma_retrieve", {"hash": h})
        assert "❌" not in text
        assert "found" in text  # the issued payload (found=true)

    async def test_redeem_local_foreign_hash_unchanged(self, store_factory: Any) -> None:
        """Off-plane: strict markers are OFF by default → a hash-only redeem
        of an issuer-stamped row behaves exactly as pre-W3 (pin)."""
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        h = await self._cached_entry(mgr, agent="other-agent")
        text = await _dispatch_text("vesma_retrieve", {"hash": h})
        assert "❌" not in text

    async def test_redeem_unknown_hash_not_found(self, store_factory: Any) -> None:
        """An unknown hash falls through to the manager's found=False
        contract — the gate never invents content or an oracle."""
        store_factory(_base_settings(Path(tempfile.mkdtemp())))
        async with mcp_identity("harness-a"):
            text = await _dispatch_text("vesma_retrieve", {"hash": "ab" * 32})
        assert "❌" not in text
        assert "found" in text


class TestIdentityArgSurfaces:
    """F-3: the hooks/assemble agent arguments carry the same identity gate
    as every other agent-argument surface (compress had it; these two were
    the inconsistency). post_tool_call with auto_compress=false is the
    LLM-free happy path; assemble pins the foreign rejection only."""

    async def test_hooks_foreign_agent_is_loud(self, store_factory: Any) -> None:
        store_factory(_base_settings(Path(tempfile.mkdtemp())))
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_hooks",
                {
                    "action": "post_tool_call",
                    "session": "s-1",
                    "project": "ns-wave",
                    "agent": "other-agent",
                    "tool_name": "some_tool",
                    "output_text": "tool output",
                    "auto_compress": False,
                },
            )
        assert "Namespace violation" in text

    async def test_hooks_own_agent_clean_and_local_foreign_passthrough(
        self, store_factory: Any
    ) -> None:
        store_factory(_base_settings(Path(tempfile.mkdtemp())))
        async with mcp_identity("harness-a"):
            own = await _dispatch_text(
                "vesma_hooks",
                {
                    "action": "post_tool_call",
                    "session": "s-1",
                    "project": "ns-wave",
                    "agent": "harness-a",
                    "tool_name": "some_tool",
                    "output_text": "tool output",
                    "auto_compress": False,
                },
            )
        assert "❌" not in own
        assert '"auto_compress": false' in own
        # off-plane: a foreign agent argument passes through untouched (pin)
        local = await _dispatch_text(
            "vesma_hooks",
            {
                "action": "post_tool_call",
                "session": "s-1",
                "project": "ns-wave",
                "agent": "other-agent",
                "tool_name": "some_tool",
                "output_text": "tool output",
                "auto_compress": False,
            },
        )
        assert "❌" not in local

    async def test_assemble_foreign_agent_is_loud(self, store_factory: Any) -> None:
        store_factory(_base_settings(Path(tempfile.mkdtemp())))
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_assemble_context",
                {"session": "s-1", "project": "ns-wave", "agent": "other-agent"},
            )
        assert "Namespace violation" in text


class TestFilterOwnership:
    """F-4 (mutation half): re-filtering WRITES the filter projection on
    the row — a mutating surface; foreign/unowned rows are refused under
    identity, own rows run the (heuristic, LLM-free) pipeline."""

    async def test_filter_foreign_row_is_loud(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        foreign = _seed(mgr, owner="other-agent", status=MemoryStatus.PUBLISHED, marker="flt-f")
        async with mcp_identity("harness-a"):
            text = await _call_tool_text("vesma_filter", {"memory_id": str(foreign.id)})
        assert "Namespace violation" in text  # the gate refused BEFORE any re-filter

    async def test_filter_own_row_applies(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        own = _seed(mgr, owner="harness-a", status=MemoryStatus.PUBLISHED, marker="flt-o")
        async with mcp_identity("harness-a"):
            text = await _dispatch_text("vesma_filter", {"memory_id": str(own.id)})
        assert "❌" not in text
        row = mgr.get(str(own.id))
        assert row is not None
        assert row.filter_profile is not None  # the projection landed

    async def test_filter_local_foreign_row_unchanged(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        foreign = _seed(mgr, owner="other-agent", status=MemoryStatus.PUBLISHED, marker="flt-l")
        text = await _dispatch_text("vesma_filter", {"memory_id": str(foreign.id)})
        assert "❌" not in text


class TestWorkflowReadAperture:
    """F-4 (read half): read-BY-ID obeys the SAME dispatch aperture as the
    list surfaces — foreign published ids already surface in search under
    published_only, so hiding them by id would be inconsistent; an
    INVISIBLE row answers the surface's existing not-found / empty-history
    shape (the no-oracle contract the tombstone leg already holds)."""

    async def test_get_foreign_published_visible_under_published_only(
        self, store_factory: Any
    ) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        foreign = _seed(mgr, owner="other-agent", status=MemoryStatus.PUBLISHED, marker="wg-p")
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_workflow", {"action": "get", "memory_id": str(foreign.id)}
            )
        assert "❌" not in text
        assert "not found" not in text

    async def test_get_foreign_raw_hidden_not_found(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        foreign = _seed(mgr, owner="other-agent", status=MemoryStatus.RAW, marker="wg-r")
        async with mcp_identity("harness-a"):
            get_text = await _dispatch_text(
                "vesma_workflow", {"action": "get", "memory_id": str(foreign.id)}
            )
            hist_text = await _dispatch_text(
                "vesma_workflow", {"action": "history", "memory_id": str(foreign.id)}
            )
        assert "not found" in get_text
        assert '"history": []' in hist_text  # the empty-history shape, no oracle

    async def test_get_foreign_published_hidden_under_own_only(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp()), cross_read="own_only"))
        foreign = _seed(mgr, owner="other-agent", status=MemoryStatus.PUBLISHED, marker="wg-o")
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_workflow", {"action": "get", "memory_id": str(foreign.id)}
            )
        assert "not found" in text

    async def test_get_local_foreign_row_unchanged(self, store_factory: Any) -> None:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        foreign = _seed(mgr, owner="other-agent", status=MemoryStatus.RAW, marker="wg-l")
        text = await _dispatch_text(
            "vesma_workflow", {"action": "get", "memory_id": str(foreign.id)}
        )
        assert "not found" not in text


class TestConfigMisLevelKeys:
    """F-6: McpConfig carries fail-closed SECURITY knobs — a key parked at
    the wrong nesting level (or a typo'd knob) must refuse the boot, never
    silently fall back to the default aperture."""

    def test_mis_level_cross_read_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, pytest.raises(ValidationError):
            Settings(
                vesma={
                    "vault_path": str(Path(tmpdir) / "v"),
                    "data_dir": str(Path(tmpdir) / "d"),
                },
                embedding={"provider": "onnx"},
                mcp={"cross_read": "own_only"},  # belongs under namespace:
            )

    def test_typo_knob_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, pytest.raises(ValidationError):
            Settings(
                vesma={
                    "vault_path": str(Path(tmpdir) / "v"),
                    "data_dir": str(Path(tmpdir) / "d"),
                },
                embedding={"provider": "onnx"},
                mcp={"endpoint_enabled": True, "quota": {"calls_permin": 10}},
            )

    def test_valid_shape_still_constructs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = Settings(
                vesma={
                    "vault_path": str(Path(tmpdir) / "v"),
                    "data_dir": str(Path(tmpdir) / "d"),
                },
                embedding={"provider": "onnx"},
                mcp={"endpoint_enabled": False, "namespace": {"cross_read": "own_only"}},
            )
            assert settings.mcp.endpoint_enabled is False
            assert settings.mcp.namespace.cross_read == "own_only"


class TestBulkGateMirrorDryRun:
    """F-5: the gate's affected-row mirror is SET-EQUAL (count-exact) with
    the manager's own dry-run report across the edge-subtype matrix — the
    single-source refactor in manager.py is tracked separately (post-merge
    card); until then this pin catches drift in BOTH directions."""

    async def _mirror_matrix(self, store_factory: Any) -> MemoryManager:
        mgr = store_factory(_base_settings(Path(tempfile.mkdtemp())))
        tags_common = ["project:ns-wave", "agent:harness-a", "vesma:learning"]
        mgr.add(
            MemoryCreate(content="m1", tags=[*tags_common, "gcw:rule"], source=MemorySource.MCP),
            project="ns-wave",
            agent="harness-a",
        )
        mgr.add(
            MemoryCreate(content="m2", tags=[*tags_common, "gcw:bogus"], source=MemorySource.MCP),
            project="ns-wave",
            agent="harness-a",
        )
        mgr.add(
            MemoryCreate(
                content="m3", tags=[*tags_common, "mnemos:no-federate"], source=MemorySource.MCP
            ),
            project="ns-wave",
            agent="harness-a",
        )
        return mgr

    async def _assert_mirror(
        self,
        mgr: MemoryManager,
        action: str,
        args: dict[str, Any],
        manager_call: Any,
    ) -> None:
        from vesma.mcp_server import _bulk_tag_affected_rows

        affected = _bulk_tag_affected_rows(mgr, action, args)
        report = manager_call()
        assert report.get("dry_run") is True
        assert len(affected) == report["changed"], (
            f"{action} mirror drift: gate={len(affected)} manager={report['changed']}"
        )

    async def test_matrix_add_remove_rename_mirror(self, store_factory: Any) -> None:
        mgr = await self._mirror_matrix(store_factory)
        cases: list[tuple[str, dict[str, Any], Any]] = [
            (
                "add",
                {"tags": ["severity:high"]},
                lambda: mgr.tags_add(["severity:high"], dry_run=True),
            ),
            (
                "add",
                {"tags": ["vesma:learning"]},
                lambda: mgr.tags_add(["vesma:learning"], dry_run=True),
            ),
            (
                "remove",
                {"tags": ["gcw:rule"]},
                lambda: mgr.tags_remove(["gcw:rule"], dry_run=True),
            ),
            (
                "remove",
                {"tags": ["gcw:"], "wildcard": True},
                lambda: mgr.tags_remove(["gcw:"], wildcard=True, dry_run=True),
            ),
            (
                "remove",
                {"tags": []},
                lambda: mgr.tags_remove([], dry_run=True),
            ),
            (
                "rename",
                {"from_prefix": "gcw:", "to_prefix": "vesma:"},
                lambda: mgr.tags_rename("gcw:", "vesma:", dry_run=True),
            ),
            (
                "rename",
                {
                    "from_prefix": "gcw:",
                    "to_prefix": "vesma:",
                    "invalid_subtypes_to_legacy": True,
                },
                lambda: mgr.tags_rename(
                    "gcw:", "vesma:", dry_run=True, invalid_subtypes_to_legacy=True
                ),
            ),
            (
                "rename",
                {"from_prefix": "gcw:", "to_prefix": "vesma:", "subtypes": ["rule"]},
                lambda: mgr.tags_rename("gcw:", "vesma:", subtypes=["rule"], dry_run=True),
            ),
            (
                "rename",
                {"from_prefix": "mnemos:", "to_prefix": "vesma:"},
                lambda: mgr.tags_rename("mnemos:", "vesma:", dry_run=True),
            ),
        ]
        for action, args, manager_call in cases:
            await self._assert_mirror(mgr, action, args, manager_call)

    async def test_mirror_concrete_counts(self, store_factory: Any) -> None:
        """The matrix above is non-trivial: pin the exact numbers so a
        both-sides-drift cannot pass the mirror silently."""
        from vesma.mcp_server import _bulk_tag_affected_rows

        mgr = await self._mirror_matrix(store_factory)
        assert len(_bulk_tag_affected_rows(mgr, "add", {"tags": ["severity:high"]})) == 3
        assert len(_bulk_tag_affected_rows(mgr, "add", {"tags": ["vesma:learning"]})) == 0
        assert len(_bulk_tag_affected_rows(mgr, "remove", {"tags": ["gcw:rule"]})) == 1
        assert (
            len(_bulk_tag_affected_rows(mgr, "remove", {"tags": ["gcw:"], "wildcard": True})) == 2
        )
        assert len(_bulk_tag_affected_rows(mgr, "remove", {"tags": []})) == 0
        assert (
            len(
                _bulk_tag_affected_rows(
                    mgr, "rename", {"from_prefix": "gcw:", "to_prefix": "vesma:"}
                )
            )
            == 1
        )
        report = mgr.tags_rename("gcw:", "vesma:", dry_run=True)
        assert report["changed"] == 1 and report["skipped_invalid"] == 1


# ---------------------------------------------------------------------------
# TokenQuota (unit) + config fail-closed
# ---------------------------------------------------------------------------


class TestTokenQuota:
    def test_default_120_calls_then_121st_denied(self) -> None:
        """The ratified default: 120 calls per rolling minute, the 121st
        within the same window answers denied with Retry-After >= 1."""
        quota = TokenQuota()
        assert quota.calls_per_minute == 120
        now = 10_000.0
        for _ in range(120):
            decision = quota.check("jti-a", now=now)
            assert decision.allowed
        denied = quota.check("jti-a", now=now)
        assert not denied.allowed
        assert denied.retry_after is not None and denied.retry_after >= 1

    def test_window_slides_with_time(self) -> None:
        quota = TokenQuota(calls_per_minute=2)
        quota.check("jti-b", now=0.0)
        quota.check("jti-b", now=0.0)
        denied = quota.check("jti-b", now=30.0)
        assert not denied.allowed
        assert denied.retry_after is not None and denied.retry_after <= 60
        # past the window the oldest timestamps prune and calls admit again
        assert quota.check("jti-b", now=61.0).allowed

    def test_denied_call_does_not_extend_window(self) -> None:
        """A 429 hammer must not push its own recovery further away."""
        quota = TokenQuota(calls_per_minute=1)
        quota.check("jti-c", now=0.0)
        first = quota.check("jti-c", now=1.0)
        second = quota.check("jti-c", now=10.0)
        third = quota.check("jti-c", now=20.0)
        assert not first.allowed and not second.allowed and not third.allowed
        assert quota.check("jti-c", now=60.5).allowed

    def test_windows_are_per_jti(self) -> None:
        """The quota key is the token jti: a rotated token starts fresh;
        two tokens of the SAME agent do not share a window."""
        quota = TokenQuota(calls_per_minute=1)
        assert quota.check("jti-old", now=0.0).allowed
        assert not quota.check("jti-old", now=1.0).allowed
        assert quota.check("jti-new", now=1.0).allowed  # rotated token: own window

    def test_none_disables(self) -> None:
        quota = TokenQuota(None)
        for i in range(500):
            assert quota.check("jti-d", now=float(i)).allowed

    def test_zero_or_negative_refused(self) -> None:
        with pytest.raises(ValueError):
            TokenQuota(0)


class TestConfigFailClosed:
    def test_unknown_cross_read_value_refuses_settings(self) -> None:
        """ADR-0050 §5: an unknown mode fails closed AT CONFIG CONSTRUCTION."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(ValidationError) as excinfo:
                Settings(
                    vesma={
                        "vault_path": str(Path(tmpdir) / "v"),
                        "data_dir": str(Path(tmpdir) / "d"),
                    },
                    embedding={"provider": "onnx"},
                    mcp={"namespace": {"cross_read": "everything"}},
                )
            assert "cross_read" in str(excinfo.value)

    def test_unknown_namespace_key_refuses_settings(self) -> None:
        """extra=forbid on the new config groups: a typo'd key never boots."""
        with tempfile.TemporaryDirectory() as tmpdir, pytest.raises(ValidationError):
            Settings(
                vesma={
                    "vault_path": str(Path(tmpdir) / "v"),
                    "data_dir": str(Path(tmpdir) / "d"),
                },
                embedding={"provider": "onnx"},
                mcp={"namespace": {"crossread": "own_only"}},
            )

    def test_valid_modes_construct(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            for mode in ("published_only", "own_only"):
                settings = Settings(
                    vesma={
                        "vault_path": str(Path(tmpdir) / "v"),
                        "data_dir": str(Path(tmpdir) / "d"),
                    },
                    embedding={"provider": "onnx"},
                    mcp={"namespace": {"cross_read": mode}},
                )
                assert settings.mcp.namespace.cross_read == mode

    def test_quota_zero_refuses_none_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            kwargs: dict[str, Any] = {
                "vesma": {
                    "vault_path": str(Path(tmpdir) / "v"),
                    "data_dir": str(Path(tmpdir) / "d"),
                },
                "embedding": {"provider": "onnx"},
            }
            with pytest.raises(ValidationError):
                Settings(**kwargs, mcp={"quota": {"calls_per_minute": 0}})
            settings = Settings(**kwargs, mcp={"quota": {"calls_per_minute": None}})
            assert settings.mcp.quota.calls_per_minute is None


# ---------------------------------------------------------------------------
# DR-25 (doctor): MCP plane namespace health
# ---------------------------------------------------------------------------


class TestDoctorDr25:
    def _settings(self, tmp: Path) -> Settings:
        settings = Settings(
            vesma={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
            scanner={"enabled": False},
        )
        settings.resolve_paths()
        return settings

    def test_registry_pin_dr25_registered(self) -> None:
        from vesma.service import doctor_checks

        findings = doctor_checks.run_service_checks.__doc__ or ""
        assert "DR-25" in findings
        # and the check itself is callable with no args (the registry shape)
        assert callable(doctor_checks._dr25)

    def test_cross_read_invalid_mode_fails(self, tmp_path: Path) -> None:
        from vesma.service import doctor_checks
        from vesma.service.doctor_checks import Severity

        settings = self._settings(tmp_path)
        object.__setattr__(settings.mcp.namespace, "cross_read", "everything")
        finding = doctor_checks._dr25(settings_loader=lambda: settings)
        assert finding.check_id == "DR-25"
        assert finding.severity is Severity.FAIL
        assert "everything" in (finding.detail or "")

    def test_config_load_failure_fails_closed(self, tmp_path: Path) -> None:
        from vesma.service import doctor_checks
        from vesma.service.doctor_checks import Severity

        def _boom() -> Settings:
            raise RuntimeError("broken config")

        finding = doctor_checks._dr25(settings_loader=_boom)
        assert finding.severity is Severity.FAIL
        assert "fails closed" in (finding.detail or "")

    def test_expiring_token_warns_with_agent_ids(self, tmp_path: Path) -> None:
        from vesma.agent_tokens import (
            AgentTokenStore,
            issue_agent_token,
            load_or_create_signing_key,
        )
        from vesma.service import doctor_checks
        from vesma.service.doctor_checks import Severity

        settings = self._settings(tmp_path)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)  # the store home
        key = load_or_create_signing_key(tmp_path / "signing.key")
        store = AgentTokenStore(settings.db_path)
        try:
            issue_agent_token(
                agent_id="harness-x",
                node_id="default",
                scope_spec="rw",
                ttl_hours=1.0,  # inside the final 6h → the URGENT rotation WARN
                store=store,
                key=key,
            )
        finally:
            store.close()
        finding = doctor_checks._dr25(settings_loader=lambda: settings)
        assert finding.severity is Severity.WARN
        assert "harness-x" in (finding.detail or "")
        assert "within 6h" in (finding.detail or "")

    def test_aging_band_is_info_ok_not_warn(self, tmp_path: Path) -> None:
        """F-8 (cascade review): the TTL cap IS 24h — a token in its final
        6-24h band is an INFO line at OK severity, never a permanent WARN
        (signage fatigue)."""
        from vesma.agent_tokens import (
            AgentTokenStore,
            issue_agent_token,
            load_or_create_signing_key,
        )
        from vesma.service import doctor_checks
        from vesma.service.doctor_checks import Severity

        settings = self._settings(tmp_path)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)  # the store home
        key = load_or_create_signing_key(tmp_path / "signing.key")
        store = AgentTokenStore(settings.db_path)
        try:
            issue_agent_token(
                agent_id="harness-aging",
                node_id="default",
                scope_spec="rw",
                ttl_hours=20.0,  # in the 6-24h band → info, not WARN
                store=store,
                key=key,
            )
        finally:
            store.close()
        finding = doctor_checks._dr25(settings_loader=lambda: settings)
        assert finding.severity is Severity.OK
        assert "harness-aging" in (finding.detail or "")
        assert "final 24h" in (finding.detail or "")
        assert "within 6h" not in (finding.detail or "")

    def test_retired_rows_reported_info_ok(self, tmp_path: Path) -> None:
        from vesma.agent_tokens import (
            AgentTokenStore,
            issue_agent_token,
            load_or_create_signing_key,
        )
        from vesma.service import doctor_checks
        from vesma.service.doctor_checks import Severity

        settings = self._settings(tmp_path)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)  # the store home
        key = load_or_create_signing_key(tmp_path / "signing.key")
        store = AgentTokenStore(settings.db_path)
        try:
            _token, claims = issue_agent_token(
                agent_id="harness-y",
                node_id="default",
                scope_spec="rw",
                store=store,
                key=key,
            )
            assert store.revoke_jti(claims.jti, now=0)
        finally:
            store.close()
        finding = doctor_checks._dr25(settings_loader=lambda: settings)
        assert finding.severity is Severity.OK  # INFO rides OK severity, W4 owns purging
        assert "info:" in (finding.detail or "")
        assert "harness-y" in (finding.detail or "")

    def test_no_store_home_is_ok_status(self, tmp_path: Path) -> None:
        from vesma.service import doctor_checks
        from vesma.service.doctor_checks import Severity

        settings = self._settings(tmp_path)
        # no tokens ever issued → the registry exists but is empty
        finding = doctor_checks._dr25(settings_loader=lambda: settings)
        assert finding.severity is Severity.OK
        assert "no store home yet" in (finding.detail or "")
        assert "nothing to audit" in (finding.detail or "")

    def test_cross_read_valid_mode_reported_ok(self, tmp_path: Path) -> None:
        from vesma.service import doctor_checks
        from vesma.service.doctor_checks import Severity

        settings = self._settings(tmp_path)
        finding = doctor_checks._dr25(settings_loader=lambda: settings)
        assert finding.severity is Severity.OK
        assert "cross_read=published_only" in (finding.detail or "")


# ---------------------------------------------------------------------------
# HTTP gate wiring (real SDK stack — the W1 harness pattern)
# ---------------------------------------------------------------------------


try:  # the HTTP legs need the real fastapi/httpx stack
    from fastapi.testclient import TestClient as _TestClient  # noqa: F401

    _HTTP_STACK_AVAILABLE = True
except Exception:  # pragma: no cover — CI always has it
    _HTTP_STACK_AVAILABLE = False

if _HTTP_STACK_AVAILABLE:
    from tests.test_mcp_http_endpoint import (  # noqa: F401 — imported autouse fixture
        _build_harness,
        _real_mcp_sdk_stack,
        _teardown_managers,
    )

    _MCP_HEADERS = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "mcp-protocol-version": "2025-06-18",
    }

    class TestHttpGateWiring:
        """The gate-level contracts over streamable HTTP (real transport)."""

        @pytest.fixture
        def http_env(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
            monkeypatch.delenv("VESMA_INSTANCE_ID", raising=False)
            # The shared-suite guard (conftest no_mcp_http_leg_in_shared_env)
            # refuses the leg for every module but the W1 contract module;
            # this module owns the real-SDK fixture too (imported autouse
            # above), so it re-points the lifespan at the REAL builder for
            # its own lifetime — the same opt-in, one layer down.
            from vesma.api import main as api_main
            from vesma.api import mcp_endpoint as api_mcp_endpoint

            monkeypatch.setattr(
                api_main,
                "build_mcp_session_manager",
                api_mcp_endpoint.build_mcp_session_manager,
            )
            with tempfile.TemporaryDirectory() as tmpdir:
                settings = _base_settings(
                    Path(tmpdir), cross_read="published_only", calls_per_minute=2
                )
                harness = _build_harness(settings)
                with harness.client:
                    yield harness
                _teardown_managers()

        def _issue_token(self, settings: Settings, agent: str) -> str:
            from vesma.agent_tokens import (
                AgentTokenStore,
                issue_agent_token,
                load_or_create_signing_key,
                signing_key_path,
            )
            from vesma.service.instance import current_instance_id

            key = load_or_create_signing_key(signing_key_path(settings.vesma.data_dir))
            store = AgentTokenStore(settings.db_path)
            try:
                token, _claims = issue_agent_token(
                    agent_id=agent,
                    node_id=current_instance_id(settings.vesma.data_dir),
                    scope_spec="rw",
                    store=store,
                    key=key,
                )
            finally:
                store.close()
            return token

        def _post(self, harness: Any, token: str, body: dict[str, Any]) -> Any:
            return harness.client.post(
                "/mcp",
                headers={**_MCP_HEADERS, "Authorization": f"Bearer {token}"},
                json=body,
            )

        def test_per_token_quota_429_with_retry_after(self, http_env: Any) -> None:
            """The resource axis over the wire: calls_per_minute=2 → the 3rd
            call of the SAME token answers 429 + Retry-After before any tool."""
            harness = http_env
            token = self._issue_token(harness.settings, "quota-agent")
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            assert self._post(harness, token, body).status_code == 200
            assert self._post(harness, token, body).status_code == 200
            third = self._post(harness, token, body)
            assert third.status_code == 429
            retry_after = third.headers.get("retry-after")
            assert retry_after is not None and int(retry_after) >= 1
            assert "quota" in third.json()["detail"].lower()
            # a different token (fresh jti) is NOT throttled by the first's window
            fresh = self._issue_token(harness.settings, "quota-agent")
            assert self._post(harness, fresh, body).status_code == 200

        def test_identity_threaded_over_http(self, http_env: Any) -> None:
            """The W3 residual closed on the wire: the Bearer verdict's agent_id
            reaches the tool dispatch — a foreign client tag is refused loudly
            over HTTP, the matching tag stores under the identity."""
            harness = http_env
            token = self._issue_token(harness.settings, "wire-agent")
            foreign = self._post(
                harness,
                token,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "vesma_add",
                        "arguments": {
                            "content": "foreign mint over the wire",
                            "tags": ["project:ns-wire", "agent:other-agent", "vesma:learning"],
                        },
                    },
                },
            )
            assert foreign.status_code == 200  # the refusal rides the TOOL result
            assert "Namespace violation" in foreign.json()["result"]["content"][0]["text"]
            matching = self._post(
                harness,
                token,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "vesma_add",
                        "arguments": {
                            "content": "own namespace over the wire",
                            "tags": ["project:ns-wire", "agent:wire-agent", "vesma:learning"],
                        },
                    },
                },
            )
            assert matching.status_code == 200
            assert "❌" not in matching.json()["result"]["content"][0]["text"]
            assert any(
                record_owner_tag(m.tags) == "wire-agent"
                for m in harness.manager.list_recent(limit=10)
            )

        def test_cross_read_published_only_over_http(self, http_env: Any) -> None:
            """A foreign RAW row is invisible over HTTP (published_only); the
            identity's own row is served in full."""
            harness = http_env
            _seed(
                harness.manager,
                owner="other-agent",
                status=MemoryStatus.RAW,
                marker="wire-foreign-raw",
            )
            _seed(
                harness.manager,
                owner="wire-agent",
                status=MemoryStatus.RAW,
                marker="wire-own-raw",
            )
            token = self._issue_token(harness.settings, "wire-agent")
            resp = self._post(
                harness,
                token,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "vesma_search",
                        "arguments": {"query": "seed", "limit": 50, "include_raw": True},
                    },
                },
            )
            assert resp.status_code == 200
            text = resp.json()["result"]["content"][0]["text"]
            assert "wire-own-raw" in text  # own rows always full
            assert "wire-foreign-raw" not in text  # foreign raw hidden

        def test_concurrent_identities_no_cross_leak(self, http_env: Any) -> None:
            """F-7 (cascade review): two tokens interleaved CONCURRENTLY over
            the REAL streamable-HTTP transport — the per-request identity
            contextvar must never bleed across requests. Every add carries
            its OWN matching agent tag; one deliberate mismatch (token A
            claiming B's namespace) must answer the loud refusal."""
            import concurrent.futures

            harness = http_env
            # the http_env fixture carries calls_per_minute=2 (the quota
            # test's posture) — this test needs the quota OUT of the way,
            # so swap in a disabled limiter for the process under test
            harness.client.app.state.mcp_token_quota = TokenQuota(None)
            token_a = self._issue_token(harness.settings, "conc-a")
            token_b = self._issue_token(harness.settings, "conc-b")

            def _add(tok: str, agent: str, marker: str) -> tuple[int, str]:
                resp = self._post(
                    harness,
                    tok,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "vesma_add",
                            "arguments": {
                                "content": f"concurrent body {agent} {marker}",
                                "tags": ["project:ns-conc", f"agent:{agent}", "vesma:learning"],
                            },
                        },
                    },
                )
                return resp.status_code, resp.json()["result"]["content"][0]["text"]

            def _call(spec: tuple[str, str, str]) -> tuple[int, str]:
                return _add(*spec)

            specs = [
                *[(token_a, "conc-a", f"m{i}") for i in range(6)],
                *[(token_b, "conc-b", f"n{i}") for i in range(6)],
                (token_a, "conc-b", "x1"),  # the deliberate cross-claim
            ]
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                outcomes = list(pool.map(_call, specs))

            codes = [code for code, _ in outcomes]
            # EVERY call answers 200 — the refusal rides the TOOL result
            # (the transport never errors), so all 13 statuses are success.
            assert codes.count(200) == 13
            assert sum(1 for _, t in outcomes if "Namespace violation" in t) == 1
            # the refusal is on the cross-claim, and names the right identity
            cross_text = outcomes[-1][1]
            assert "conc-b" in cross_text and "conc-a" in cross_text

            rows = [r for r in harness.manager.list_recent(limit=50) if "project:ns-conc" in r.tags]
            assert len(rows) == 12
            for r in rows:
                owner = record_owner_tag(r.tags)
                assert owner in ("conc-a", "conc-b")
                assert f"concurrent body {owner}" in r.content  # owner == its own call
