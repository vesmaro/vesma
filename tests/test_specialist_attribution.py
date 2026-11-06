"""Specialist attribution layer (ADR-0050 addendum) — the matrix.

Layer 2 of the two-layer identity contract: the OPTIONAL
``specialist:<slug>`` tag names the executor ROLE behind a write
("who did this work": tech-lead, senior-system-engineer, …) in addition
to the enforced harness identity (``agent:<slug>`` — W3 layer 1, which
these tests pin UNTOUCHED). The server validates the bare slug and
mints the tag itself on the write surfaces (vesma_add /
vesma_save_context / vesma_ingest_url / vesma_ingest_document + the
CLI ``vesma add --specialist`` parity).

Four layers over one contract set:

* **Unit**: ``mint_specialist_tag`` — the one mint authority. Bare
  slug alphabet (``[a-z0-9_-]{1,64}``, the agent-slug class), lowercase
  normalization, reserved-namespace rejection (``project:`` …
  ``committee``) BEFORE the generic colon-injection check, fail-loud
  ValueError; the minted tag flows through :func:`validate_tag_contract
  <vesma.models.validate_tag_contract>` untouched.
* **Dispatch**: all four MCP write surfaces mint the tag server-side —
  including off-plane (``identity=None``, the local CLI/stdio contract).
* **The attribution invariant (the pin this wave exists for)**:
  specialist NEVER participates in ownership — ``record_owner`` reads
  ``agent:`` only, a forged specialist grants no rights over foreign
  rows (workflow transitions / row ownership unchanged), and a
  specialist write under an identity keeps ``agent:<identity>`` (the
  brief's exact ``specialist=alice, token=zcode`` leg).
* **Smoke**: search/list_recent find rows by the ``specialist:<slug>``
  tag (exact-tag filters work for the namespace for free).

Every store in this file is EPHEMERAL (``tmp_path``-backed settings via
the ``store_factory`` fixture) — no test touches the live store.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.test_mcp_namespace import (
    _base_settings,
    _call_tool_text,
    _dispatch_text,
    mcp_identity,
)
from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.mcp_namespace import (
    McpIdentity,
    NamespaceViolationError,
    enforce_agent_tags,
    record_owner,
)
from vesma.models import (
    SPECIALIST_SLUG_RE,
    SPECIALIST_TAG_PREFIX,
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    mint_specialist_tag,
    normalize_project_slug,
    normalize_specialist_slug,
    validate_tag_contract,
)

# ---------------------------------------------------------------------------
# Shared fixtures (self-contained copy of the W3 matrix's store_factory —
# a local definition avoids the parameter-shadowing F811 an imported
# fixture would trip, and keeps the module self-contained like its
# neighbors)
# ---------------------------------------------------------------------------


@pytest.fixture
def store_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Build isolated MemoryManagers wired into the ``mcp_server`` singleton.

    Yields a factory ``settings -> MemoryManager``; the singleton is
    reset after each test so neighbor modules never see a closed manager.
    (Byte-identical copy of the ``tests.test_mcp_namespace`` fixture.)
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


# ---------------------------------------------------------------------------
# Unit — the one mint authority
# ---------------------------------------------------------------------------


class TestSpecialistSlugMint:
    def test_none_passes_through(self) -> None:
        """The layer is optional — an absent argument mints nothing."""
        assert mint_specialist_tag(None) is None

    def test_plain_slug_minted(self) -> None:
        assert mint_specialist_tag("tech-lead") == "specialist:tech-lead"

    def test_case_normalized_to_lowercase(self) -> None:
        """The brief's «регистр — нормализовать в нижний»."""
        assert mint_specialist_tag("Tech-Lead") == "specialist:tech-lead"
        assert mint_specialist_tag("SENIOR_SYSTEM_ENGINEER") == (
            "specialist:senior_system_engineer"
        )

    def test_spaces_normalize_to_hyphens(self) -> None:
        assert mint_specialist_tag("Tech Lead") == "specialist:tech-lead"

    def test_surrounding_whitespace_stripped(self) -> None:
        assert mint_specialist_tag("  tech-lead  ") == "specialist:tech-lead"

    def test_minted_slug_matches_public_regex(self) -> None:
        tag = mint_specialist_tag("tech-lead")
        assert tag is not None
        assert tag.startswith(SPECIALIST_TAG_PREFIX)
        assert SPECIALIST_SLUG_RE.fullmatch(tag[len(SPECIALIST_TAG_PREFIX) :]) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "project:x",
            "agent:x",
            "task:x",
            "vesma:x",
            "mnemos:x",
            "priority:x",
            "owner:x",
            "size:x",
            "source:x",
            "severity:x",
            "stack:x",
            "applyTo:x",
            "APPLYTO:x",
            "committee",
            "Committee",
            "committee-2026",
        ],
    )
    def test_reserved_namespaces_rejected(self, value: str) -> None:
        """A specialist value never impersonates an existing tag namespace
        (or the reserved bare word) — the precise error, not a generic
        invalid-slug one."""
        with pytest.raises(ValueError, match="reserved"):
            mint_specialist_tag(value)

    @pytest.mark.parametrize("value", ["foo:bar", "specialist:x", "a:b:c"])
    def test_prefix_injection_rejected(self, value: str) -> None:
        """Any other colon-carrying value is a prefix injection — the
        caller must pass the BARE slug (the server mints the tag)."""
        with pytest.raises(ValueError, match="BARE"):
            mint_specialist_tag(value)

    @pytest.mark.parametrize("value", ["a.b", "a!!b", "a" * 65])
    def test_off_alphabet_rejected(self, value: str) -> None:
        """Same slug class as the agent slug: 1-64 of [a-z0-9_-]."""
        with pytest.raises(ValueError, match="1-64 characters"):
            mint_specialist_tag(value)

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_rejected(self, value: str) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            mint_specialist_tag(value)

    def test_non_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="slug string"):
            mint_specialist_tag(123)  # type: ignore[arg-type]

    def test_minted_tag_passes_tag_contract_untouched(self) -> None:
        """The minted tag is a valid member of a contract-clean tag set —
        strict AND lax — the contract does not police (or rewrite) the
        specialist namespace."""
        tag = mint_specialist_tag("tech-lead")
        assert tag is not None
        base = ["project:p", "agent:a", "vesma:learning"]
        assert validate_tag_contract([*base, tag], strict=True) == [*base, tag]
        assert validate_tag_contract([*base, tag], strict=False) == [*base, tag]

    def test_minted_tag_cannot_be_a_foreign_agent_mint(self) -> None:
        """The mint helper is itself the first wall against smuggling an
        ``agent:``-namespace mint through the specialist channel."""
        with pytest.raises(ValueError, match="reserved"):
            mint_specialist_tag("agent:other-agent")


class TestSpecialistNormalize:
    def test_parity_with_project_slug_normalization(self) -> None:
        """The #400 single-authority doctrine: a specialist slug can never
        normalize differently from a project slug of the same shape."""
        for value in ("Tech Lead", "  X  ", "Mixed_Case", "a-b"):
            assert normalize_specialist_slug(value) == normalize_project_slug(value)

    def test_empty_returns_empty(self) -> None:
        assert normalize_specialist_slug("   ") == ""


# ---------------------------------------------------------------------------
# Dispatch — the four write surfaces mint server-side
# ---------------------------------------------------------------------------


class TestSpecialistMcpDispatch:
    async def test_add_specialist_tag_stored(self, store_factory: Any, tmp_path: Path) -> None:
        """vesma_add with ``specialist``: the row carries BOTH the minted
        specialist tag AND the enforced agent tag."""
        mgr = store_factory(_base_settings(tmp_path))
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_add",
                {
                    "content": "specialist-attributed body",
                    "tags": ["project:ns-wave", "agent:harness-a", "vesma:learning"],
                    "specialist": "tech-lead",
                },
            )
        assert "❌" not in text
        row = next(m for m in mgr.list_recent(limit=10) if "specialist-attributed" in m.content)
        assert "specialist:tech-lead" in row.tags
        assert "agent:harness-a" in row.tags

    async def test_specialist_never_replaces_agent_identity(
        self, store_factory: Any, tmp_path: Path
    ) -> None:
        """THE brief pin: write with specialist=alice under the
        harness-zcode identity → the record carries agent:harness-zcode
        AND specialist:alice. Attribution joins, never replaces."""
        mgr = store_factory(_base_settings(tmp_path))
        async with mcp_identity("harness-zcode"):
            await _dispatch_text(
                "vesma_add",
                {
                    "content": "dual-identity body",
                    "tags": ["project:ns-wave", "agent:harness-zcode", "vesma:learning"],
                    "specialist": "alice",
                },
            )
        row = next(m for m in mgr.list_recent(limit=10) if "dual-identity" in m.content)
        assert record_owner(row.tags) == "harness-zcode"
        assert "specialist:alice" in row.tags

    async def test_add_specialist_case_normalized_in_dispatch(
        self, store_factory: Any, tmp_path: Path
    ) -> None:
        mgr = store_factory(_base_settings(tmp_path))
        async with mcp_identity("harness-a"):
            await _dispatch_text(
                "vesma_add",
                {
                    "content": "mixed-case specialist body",
                    "tags": ["project:ns-wave", "agent:harness-a", "vesma:learning"],
                    "specialist": "Tech Lead",
                },
            )
        row = next(m for m in mgr.list_recent(limit=10) if "mixed-case specialist" in m.content)
        assert "specialist:tech-lead" in row.tags

    async def test_add_invalid_specialist_loud_no_row(
        self, store_factory: Any, tmp_path: Path
    ) -> None:
        """A reserved/injected specialist value fails loud BEFORE a row is
        created (the ValueError rides the generic ``❌ Error`` rendering)."""
        mgr = store_factory(_base_settings(tmp_path))
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_add",
                {
                    "content": "must never persist",
                    "tags": ["project:ns-wave", "agent:harness-a", "vesma:learning"],
                    "specialist": "project:ns-wave",
                },
            )
        assert "❌" in text
        assert "reserved" in text
        assert all("must never persist" not in m.content for m in mgr.list_recent(limit=10))

    async def test_specialist_tag_in_tags_list_untouched(
        self, store_factory: Any, tmp_path: Path
    ) -> None:
        """A client-supplied ``specialist:`` tag inside ``tags`` rides the
        pre-existing free-tag class (like ``priority:``) — layer 2 adds a
        FIRST-CLASS channel, it does not police the raw-tag path."""
        mgr = store_factory(_base_settings(tmp_path))
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_add",
                {
                    "content": "raw specialist tag body",
                    "tags": [
                        "project:ns-wave",
                        "agent:harness-a",
                        "vesma:learning",
                        "specialist:raw-tag",
                    ],
                },
            )
        assert "❌" not in text
        row = next(m for m in mgr.list_recent(limit=10) if "raw specialist tag" in m.content)
        assert "specialist:raw-tag" in row.tags

    async def test_save_context_specialist_minted_at_boundary(
        self, store_factory: Any, tmp_path: Path
    ) -> None:
        """vesma_save_context with ``specialist``: the save boundary mints
        the tag next to task (one mint point); ownership stays the
        identity agent."""
        mgr = store_factory(_base_settings(tmp_path))
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_save_context",
                {
                    "project": "ns-wave",
                    "goals": "checkpoint with specialist attribution",
                    "specialist": "senior-system-engineer",
                },
            )
        assert "✅" in text
        recalled = mgr.recall_context(project="ns-wave", limit=5)
        row = next(m for m in recalled if "specialist attribution" in m.content)
        assert "specialist:senior-system-engineer" in row.tags
        assert record_owner(row.tags) == "harness-a"

    async def test_save_context_invalid_specialist_loud(
        self, store_factory: Any, tmp_path: Path
    ) -> None:
        store_factory(_base_settings(tmp_path))
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_save_context",
                {
                    "project": "ns-wave",
                    "goals": "bad specialist checkpoint",
                    "specialist": "vesma:checkpoint",
                },
            )
        assert "❌" in text
        assert "reserved" in text

    async def test_save_context_specialist_not_in_dedup_payload(
        self, store_factory: Any, tmp_path: Path
    ) -> None:
        """canon §10 consistency (language/task precedent): the dedup
        payload excludes the specialist slug — an identical checkpoint
        under a different specialist is a DUPLICATE hit returning the
        first-minted row, never a second row."""
        mgr = store_factory(_base_settings(tmp_path))
        async with mcp_identity("harness-a"):
            first = await _dispatch_text(
                "vesma_save_context",
                {"project": "ns-wave", "goals": "dedup body", "specialist": "alpha"},
            )
            second = await _dispatch_text(
                "vesma_save_context",
                {"project": "ns-wave", "goals": "dedup body", "specialist": "beta"},
            )
        assert "✅" in first and "duplicate=true" in second
        checkpoints = [m for m in mgr.list_recent(limit=10) if "dedup body" in m.content]
        assert len(checkpoints) == 1
        assert "specialist:alpha" in checkpoints[0].tags

    async def test_ingest_url_specialist_threaded_to_manager(
        self, store_factory: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """vesma_ingest_url with ``specialist``: the dispatch mints the tag
        and threads it to the manager with the rest of the tag list
        (network mocked — the fetch itself is not this wave's surface)."""
        mgr = store_factory(_base_settings(tmp_path))
        captured: dict[str, Any] = {}

        def fake_ingest_url(url: str, *, tags: list[str], project: str, agent: str) -> Any:
            captured["tags"] = tags
            return mgr.add(
                MemoryCreate(
                    content=f"ingested {url} offline stub",
                    tags=tags,
                    source=MemorySource.WEB,
                ),
                project=project,
                agent=agent,
            )

        monkeypatch.setattr(mgr, "ingest_url", fake_ingest_url)
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_ingest_url",
                {
                    "url": "https://example.org/offline-test",
                    "tags": ["project:ns-wave", "agent:harness-a", "vesma:learning"],
                    "specialist": "tech-lead",
                },
            )
        assert "❌" not in text
        assert "specialist:tech-lead" in captured["tags"]
        row = next(m for m in mgr.list_recent(limit=10) if "offline stub" in m.content)
        assert "specialist:tech-lead" in row.tags

    async def test_ingest_document_specialist_on_chunks(
        self, store_factory: Any, tmp_path: Path
    ) -> None:
        """vesma_ingest_document with ``specialist``: every chunk inherits
        the attribution tag with the rest of the tag set."""
        mgr = store_factory(_base_settings(tmp_path))
        async with mcp_identity("harness-a"):
            text = await _dispatch_text(
                "vesma_ingest_document",
                {
                    "text": "chunk one body\n\nchunk two body",
                    "doc_id": "doc-specialist-1",
                    "tags": ["project:ns-wave", "agent:harness-a", "vesma:learning"],
                    "specialist": "tech-lead",
                },
            )
        assert "❌" not in text
        rows = [m for m in mgr.list_recent(limit=20) if m.tags and "vesma:learning" in m.tags]
        chunk_rows = [m for m in rows if "specialist:tech-lead" in m.tags]
        assert chunk_rows, "at least one chunk must carry the specialist tag"

    async def test_off_plane_specialist_still_minted(
        self, store_factory: Any, tmp_path: Path
    ) -> None:
        """identity=None (stdio/CLI/local): the specialist parameter still
        mints — the layer is plane-independent attribution, not an
        enforcement feature."""
        mgr = store_factory(_base_settings(tmp_path))
        text = await _dispatch_text(
            "vesma_add",
            {
                "content": "off-plane specialist body",
                "tags": ["project:ns-wave", "agent:local-agent", "vesma:learning"],
                "specialist": "tech-lead",
            },
        )
        assert "❌" not in text
        row = next(m for m in mgr.list_recent(limit=10) if "off-plane specialist" in m.content)
        assert "specialist:tech-lead" in row.tags


# ---------------------------------------------------------------------------
# The attribution invariant — specialist is NOT ownership
# ---------------------------------------------------------------------------


class TestSpecialistAttributionInvariant:
    def test_record_owner_reads_agent_only(self) -> None:
        """The ONE owner convention: ``specialist:`` is invisible to
        ownership — an owner-carrying agent tag wins; a specialist-only
        tag set is UNOWNED (""), not a specialist-owned row."""
        assert record_owner(["specialist:alice", "agent:harness-a"]) == "harness-a"
        assert record_owner(["agent:harness-a", "specialist:alice"]) == "harness-a"
        assert record_owner(["specialist:alice"]) == ""
        assert record_owner(["specialist:alice", "vesma:learning"]) == ""

    def test_specialist_never_trips_agent_enforcement(self) -> None:
        """Layer 1 is untouched: ``specialist:`` tags pass
        ``enforce_agent_tags`` under any identity (only ``agent:*`` tags
        are judged)."""
        tags = ["project:p", "agent:harness-a", "vesma:learning", "specialist:alice"]
        identity = McpIdentity(agent_id="harness-a", jti="jti-pin")
        assert enforce_agent_tags(tags, identity) == tags

    def test_forged_agent_tag_still_refused_despite_matching_specialist(self) -> None:
        """Smuggling attempt: a matching specialist does NOT legitimize a
        foreign ``agent:`` mint — layer 1 rejects it exactly as before."""
        tags = ["project:p", "agent:other-agent", "vesma:learning", "specialist:harness-a"]
        identity = McpIdentity(agent_id="harness-a", jti="jti-pin")
        with pytest.raises(NamespaceViolationError):
            enforce_agent_tags(tags, identity)

    async def test_forged_specialist_grants_no_workflow_rights(
        self, store_factory: Any, tmp_path: Path
    ) -> None:
        """The archcom-P0-1 pin: a foreign row tagged
        ``specialist:tech-lead`` is NOT claimable by a harness whose
        specialist channel says ``tech-lead`` — workflow transitions key
        on ``agent:`` ownership, the specialist namespace is invisible
        to them."""
        mgr = store_factory(_base_settings(tmp_path))
        foreign = mgr.add(
            MemoryCreate(
                content="foreign row with specialist tag",
                tags=[
                    "project:ns-wave",
                    "agent:other-agent",
                    "vesma:learning",
                    "specialist:tech-lead",
                ],
                source=MemorySource.MCP,
                status=MemoryStatus.PUBLISHED,
            ),
            project="ns-wave",
            agent="other-agent",
        )
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_workflow",
                {"action": "set", "memory_id": foreign.id, "to": "in-progress", "actor": "x"},
            )
        assert "❌" in text
        assert "owned by" in text

    async def test_specialist_write_cannot_mutate_foreign_rows(
        self, store_factory: Any, tmp_path: Path
    ) -> None:
        """Same invariant through the bulk tag-mutation surface: the
        row-ownership gate keys on ``agent:`` — a specialist-tagged
        foreign row is refused exactly like an untagged one."""
        mgr = store_factory(_base_settings(tmp_path))
        mgr.add(
            MemoryCreate(
                content="foreign row for tag mutation",
                tags=[
                    "project:ns-wave",
                    "agent:other-agent",
                    "vesma:learning",
                    "specialist:tech-lead",
                ],
                source=MemorySource.MCP,
                status=MemoryStatus.PUBLISHED,
            ),
            project="ns-wave",
            agent="other-agent",
        )
        async with mcp_identity("harness-a"):
            text = await _call_tool_text(
                "vesma_tags",
                {"action": "remove", "tags": ["vesma:learning"]},
            )
        assert "❌" in text

    async def test_cross_read_not_widened_by_specialist(
        self, store_factory: Any, tmp_path: Path
    ) -> None:
        """own_only: a foreign row carrying OUR specialist tag stays
        foreign (specialist is not an ownership credential); an own row
        with ANY specialist tag stays own."""
        mgr = store_factory(_base_settings(tmp_path, cross_read="own_only"))
        mgr.add(
            MemoryCreate(
                content="foreign row, our specialist tag",
                tags=[
                    "project:ns-wave",
                    "agent:other-agent",
                    "vesma:learning",
                    "specialist:tech-lead",
                ],
                source=MemorySource.MCP,
                status=MemoryStatus.PUBLISHED,
            ),
            project="ns-wave",
            agent="other-agent",
        )
        mgr.add(
            MemoryCreate(
                content="own row with foreign specialist tag",
                tags=[
                    "project:ns-wave",
                    "agent:harness-a",
                    "vesma:learning",
                    "specialist:other-role",
                ],
                source=MemorySource.MCP,
                status=MemoryStatus.PUBLISHED,
            ),
            project="ns-wave",
            agent="harness-a",
        )
        async with mcp_identity("harness-a"):
            results = await _dispatch_text(
                "vesma_search",
                {"query": "specialist tag", "limit": 10},
            )
        rows = json.loads(results) if isinstance(results, str) else results
        contents = {r["content"] for r in rows}
        assert any("own row with foreign specialist" in c for c in contents)
        assert not any("foreign row, our specialist" in c for c in contents)


# ---------------------------------------------------------------------------
# Smoke — search/list_recent by the specialist tag
# ---------------------------------------------------------------------------


class TestSpecialistSearchSmoke:
    async def test_search_by_specialist_tag_finds(self, store_factory: Any, tmp_path: Path) -> None:
        """The brief's smoke: «поиск по specialist:tech-lead находит»."""
        mgr = store_factory(_base_settings(tmp_path))
        mgr.add(
            MemoryCreate(
                content="attribution smoke body by tech-lead",
                tags=[
                    "project:ns-wave",
                    "agent:harness-a",
                    "vesma:learning",
                    "specialist:tech-lead",
                ],
                source=MemorySource.MCP,
                status=MemoryStatus.PUBLISHED,
            ),
            project="ns-wave",
            agent="harness-a",
        )
        mgr.add(
            MemoryCreate(
                content="attribution smoke body by another role",
                tags=[
                    "project:ns-wave",
                    "agent:harness-a",
                    "vesma:learning",
                    "specialist:qa-engineer",
                ],
                source=MemorySource.MCP,
                status=MemoryStatus.PUBLISHED,
            ),
            project="ns-wave",
            agent="harness-a",
        )
        hits = mgr.search(query="attribution smoke body", tags=["specialist:tech-lead"], limit=10)
        assert hits
        assert all("specialist:tech-lead" in r.memory.tags for r in hits)
        assert all("qa-engineer" not in " ".join(r.memory.tags) for r in hits)

    async def test_list_recent_by_specialist_tag(self, store_factory: Any, tmp_path: Path) -> None:
        mgr = store_factory(_base_settings(tmp_path))
        mgr.add(
            MemoryCreate(
                content="list-recent smoke body",
                tags=[
                    "project:ns-wave",
                    "agent:harness-a",
                    "vesma:learning",
                    "specialist:senior-system-engineer",
                ],
                source=MemorySource.MCP,
                status=MemoryStatus.PUBLISHED,
            ),
            project="ns-wave",
            agent="harness-a",
        )
        rows = mgr.list_recent(limit=10, tags=["specialist:senior-system-engineer"])
        assert rows
        assert all("specialist:senior-system-engineer" in m.tags for m in rows)


# ---------------------------------------------------------------------------
# CLI parity — vesma add --specialist (no live store; the mint + wiring)
# ---------------------------------------------------------------------------


class TestSpecialistCliParity:
    def test_cli_runner_calls_typer_help(self) -> None:
        """The option is registered on the CLI command (click parameter
        parity with the MCP tool)."""
        from typer.testing import CliRunner

        from vesma.cli.main import app

        result = CliRunner().invoke(app, ["add", "--help"])
        assert result.exit_code == 0
        assert "--specialist" in result.output

    def test_invalid_specialist_value_fails_loud_via_mint(self) -> None:
        """The CLI routes the value through the SAME mint authority — the
        rejection contract is byte-identical to the MCP surface's."""
        from typer.testing import CliRunner

        from vesma.cli.main import app

        with tempfile.TemporaryDirectory() as tmp:
            result = CliRunner().invoke(
                app,
                [
                    "add",
                    "cli specialist body",
                    "--tags",
                    "project:ns-wave,agent:local-agent,vesma:learning",
                    "--specialist",
                    "project:ns-wave",
                    "--config",
                    tmp,
                ],
            )
            assert result.exit_code == 1
            assert "specialist" in result.output.lower()
