"""A9-completion (epic #308 Ф1-PREP, #360 review item 5) — the agent predicate.

``MemoryManager.search(agent=...)`` scoped only the FTS leg natively
(``fts_search(agent=...)``). The vector resolve loop checked
project/status/quarantine/refined_only but NOT agent, and the graph leg
mirrored the same four gates without agent — so an agent-scoped search
could surface another agent's rows through a stale embed or an edge
neighbour, inside the SAME project (the A9 project predicate was never
the issue; the agent axis was simply missing).

The fix is manager-side ONLY (TL decision recorded in the wave scope):
resolve-time guards on the authoritative SQLite ``Memory.agent`` in the
vector resolve loop and the graph leg. NO native VectorStore agent
predicate — the store stays project-only, tags/guards stay post-filter.
The FTS leg already filters natively and needs no pin here.

MUTATION PROTOCOL — the load-bearing pins are mutation-verified:

* ``test_vector_leg_never_surfaces_other_agent`` — M1 drops the agent
  guard in the vector resolve loop (the other-agent row surfaces).
* ``test_graph_edge_never_leaves_agent_scope`` — M2 drops the agent
  mirror on the graph leg (the edge neighbour surfaces).

The vector-leg fixture uses the A9 suite's mock embedder (identical
vectors for every text — the worst case: the vector leg cannot prefer
the in-agent copy, any leak is structural). The graph-leg fixture wipes
the vector store (FTS + graph decide), mirroring the slice-1 graph pins.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus

PROJECT = "a9a-proj"
AGENT_A = "agent-alpha"
AGENT_B = "agent-beta"

# Same project, two agents — the canonical fixture: identical scope on
# every axis except agent, so the agent guard is the only decider.
SHARED_CONTENT = (
    "alpha deployment runbook for the handler service: "
    "check the manifest, rotate the access policy, verify the baseline."
)
# Lives in the SAME project under the OTHER agent; lexically disjoint
# from the query, so it can surface ONLY through the vector resolve leg
# (identical mock embeddings) — pre-fix it leaked into agent-A searches.
B_AGENT_CONTENT = (
    "betatopic falcon metrics collector: gathers betatopic spans "
    "from the betatopic stream and writes betatopic summaries."
)


def _settings(tmp: Path, *, graph_walk: bool = False) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
            "graph_walk": graph_walk,
        },
        scanner={"enabled": False},
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def manager() -> Iterator[MemoryManager]:
    """Vector-leg fixture: identical mock embeddings (worst case)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = MemoryManager(_settings(Path(tmpdir)))
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        mgr._embedder = mock_embedder
        yield mgr
        mgr.close()


def _add(mgr: MemoryManager, content: str, *, agent: str) -> None:
    data = MemoryCreate(
        content=content,
        tags=[f"project:{PROJECT}", f"agent:{agent}", "mnemos:learning"],
        source=MemorySource.MCP,
        status=MemoryStatus.PUBLISHED,
    )
    mgr.add(data, project=PROJECT, agent=agent)


# ── Vector resolve leg: the agent guard ───────────────────────────────────────


class TestVectorLegAgentScoping:
    def test_vector_leg_never_surfaces_other_agent(self, manager: MemoryManager) -> None:
        """M1 pin — the A9 regression shape on the agent axis: query terms
        exist ONLY in agent B's row, so any result can arrive only through
        the vector resolve leg. Pre-fix the loop resolved B's row into an
        agent-A-scoped search (project passed, agent unchecked); post-fix
        only agent-A rows surface."""
        _add(manager, SHARED_CONTENT, agent=AGENT_A)
        _add(manager, B_AGENT_CONTENT, agent=AGENT_B)

        results = manager.search("betatopic falcon", project=PROJECT, agent=AGENT_A, limit=10)
        assert all(r.memory.agent == AGENT_A for r in results)
        assert not any("betatopic" in r.memory.content for r in results)
        # The A row itself may surface via the vector leg (it is in-agent);
        # what must NEVER surface is B's content.
        # Sanity: the terms ARE findable in B's own agent scope.
        b_results = manager.search("betatopic falcon", project=PROJECT, agent=AGENT_B, limit=10)
        assert b_results
        assert all(r.memory.agent == AGENT_B for r in b_results)

    def test_unscoped_agent_mode_returns_both(self, manager: MemoryManager) -> None:
        """``agent=None`` is the explicit cross-agent mode (mirrors the A9
        project global mode): no narrowing — with identical embeddings the
        vector leg surfaces BOTH agents' rows for the same query."""
        _add(manager, SHARED_CONTENT, agent=AGENT_A)
        _add(manager, B_AGENT_CONTENT, agent=AGENT_B)
        results = manager.search("betatopic falcon", project=PROJECT, limit=10)
        assert {r.memory.agent for r in results} == {AGENT_A, AGENT_B}
        # The scoped twin on the same fixture narrows to A only (the
        # guard is the agent parameter, not the fixture shape).
        scoped = manager.search("betatopic falcon", project=PROJECT, agent=AGENT_A, limit=10)
        assert all(r.memory.agent == AGENT_A for r in scoped)


# ── Graph leg: the agent mirror ───────────────────────────────────────────────


class TestGraphLegAgentScoping:
    @pytest.mark.parametrize("kind", ["supersedes", "relates_to"])
    def test_graph_edge_never_leaves_agent_scope(self, tmp_path: Path, kind: str) -> None:
        """M2 pin — an edge neighbour of the OTHER agent must not surface
        in an agent-scoped search (the edge must not widen a scope on the
        agent axis either). Both edge kinds share the gate sequence: the
        unconditional ``supersedes`` leg and the flag-gated ``relates_to``
        walk."""
        walk = kind == "relates_to"
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = MemoryManager(_settings(Path(tmpdir), graph_walk=walk))
            mock_embedder = MagicMock()
            mock_embedder.embed.return_value = [0.1] * 384
            mgr._embedder = mock_embedder
            try:
                anchor_data = MemoryCreate(
                    content="anchor tide schedule notes",
                    tags=[f"project:{PROJECT}", f"agent:{AGENT_A}", "mnemos:learning"],
                    source=MemorySource.MCP,
                    status=MemoryStatus.PUBLISHED,
                )
                anchor = mgr.add(anchor_data, project=PROJECT, agent=AGENT_A)
                neighbour_data = MemoryCreate(
                    content="dormant ledger reconciliation figures",
                    tags=[f"project:{PROJECT}", f"agent:{AGENT_B}", "mnemos:learning"],
                    source=MemorySource.MCP,
                    status=MemoryStatus.PUBLISHED,
                )
                neighbour = mgr.add(neighbour_data, project=PROJECT, agent=AGENT_B)
                mgr.add_memory_edge(anchor.id, neighbour.id, kind=kind)
                mgr.vectors.wipe()  # FTS + graph legs decide

                results = mgr.search("anchor tide", project=PROJECT, agent=AGENT_A, limit=5)
                ids = {r.memory.id for r in results}
                assert anchor.id in ids
                assert neighbour.id not in ids
                # Control: without the agent condition the edge neighbour
                # DOES surface via the graph leg — the pin above is the
                # agent gate, not a broken edge.
                control = mgr.search("anchor tide", project=PROJECT, limit=5)
                control_hits = {r.memory.id for r in control}
                assert neighbour.id in control_hits
                assert any(r.via_graph and r.memory.id == neighbour.id for r in control)
            finally:
                mgr.close()
