"""Search v2 (issue #313) — graph leg v1 (memory_edges expansion).

``memory_edges`` (supersedes, ADR-0018 Phase 1) was unused by search —
a superseded record was invisible to a query that matched its
replacement. The graph leg v1 expands the top-``limit`` fused ids 1 hop
in BOTH directions (superseding→superseded and vice versa), appends
edge-sourced rows with a decayed RRF slot and ``via_graph=True``,
and passes the SAME status / quarantine / refined_only gates.

Decay rule (deterministic, documented in the code): an expansion row's
weight is ``1/(rrf_k + 2*anchor_rank)`` where anchor_rank is its first
anchor's 1-based position in the fused ranking — the appended block is a
pure function of the fused ranking + the edge table.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus, PipelineState

PROJECT = "graph-proj"
AGENT = "graph-agent"


def _settings(tmp: Path) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        scanner={"enabled": False},
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def manager() -> MemoryManager:
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = MemoryManager(_settings(Path(tmpdir)))
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        mgr._embedder = mock_embedder
        yield mgr
        mgr.close()


def _fts_only(mgr: MemoryManager) -> None:
    """Drop every vector row — the vector leg returns nothing and the
    search becomes FTS-only.

    The vector store returns top-k without a score threshold, so on a
    two-row corpus BOTH rows always enter ``vector_resolved`` (identical
    mock embeddings → cosine 1.0) — the sibling then counts as a fused
    hit and never carries ``via_graph``. The graph leg is what is under
    test; pinning it requires the sibling to be reachable ONLY through
    the edge. (Same pattern as test_search_recall_bugs' fts_only
    note: clear the vector store.)
    """
    mgr.vectors.wipe()


def _add(
    mgr: MemoryManager,
    content: str,
    *,
    status: MemoryStatus = MemoryStatus.PUBLISHED,
    project: str = PROJECT,
) -> object:
    data = MemoryCreate(
        content=content,
        tags=[f"project:{project}", f"agent:{AGENT}", "mnemos:test"],
        source=MemorySource.MCP,
        status=status,
    )
    return mgr.add(data, project=project, agent=AGENT)


class TestGraphExpansion:
    def test_superseded_sibling_surfaces_with_via_graph(self, manager) -> None:
        """The canonical case: search hits the replacement, the leg
        surfaces the superseded sibling with provenance.

        The sibling's content deliberately shares NO query token — a
        row the lexical/vector legs already matched is a fused hit and
        must keep its own provenance; the graph leg only appends rows
        the legs could NOT see.
        """
        new = _add(manager, "deployment v2: the new conveyor configuration")
        old = _add(manager, "obsolete prior revision of this runbook")
        manager.add_memory_edge(new.id, old.id, kind="supersedes")
        _fts_only(manager)

        results = manager.search("deployment v2 conveyor", limit=5)
        ids = [r.memory.id for r in results]
        assert new.id in ids
        assert old.id in ids, "the superseded sibling must surface via the graph"
        by_id = {r.memory.id: r for r in results}
        assert by_id[old.id].via_graph is True
        assert by_id[new.id].via_graph is False

    def test_reverse_direction_also_surfaces(self, manager) -> None:
        """Searching the OLD record surfaces the NEWER one (incoming edge)."""
        new = _add(manager, "runbook v2: rewritten gate policy")
        old = _add(manager, "runbook v1: original gate policy")
        manager.add_memory_edge(new.id, old.id, kind="supersedes")

        results = manager.search("original gate policy", limit=5)
        ids = {r.memory.id for r in results}
        assert old.id in ids
        assert new.id in ids, "the superseding replacement must surface (incoming edge)"

    def test_no_edges_no_expansion(self, manager) -> None:
        """Edges absent → the leg is a no-op; nothing marked via_graph."""
        _add(manager, "lonely record about windmills")
        results = manager.search("windmill", limit=5)
        assert results
        assert all(not r.via_graph for r in results)

    def test_fused_rows_never_remarked_via_graph(self, manager) -> None:
        """A row already matched by FTS/vector is never re-appended with a
        graph provenance — the fused hit keeps its own."""
        a = _add(manager, "alpha note about harbours")
        b = _add(manager, "beta note about harbours")
        manager.add_memory_edge(a.id, b.id, kind="supersedes")
        results = manager.search("harbours", limit=5)
        ids = [r.memory.id for r in results]
        assert len(ids) == len(set(ids)), "no duplicates"

    def test_cap_at_limit_extra_rows(self, manager) -> None:
        """Expansion is capped at ``limit`` extra rows (a search at most
        doubles, never floods)."""
        anchor = _add(manager, "hub record about lighthouses")
        siblings = [_add(manager, f"lighthouse sibling record number {i}") for i in range(10)]
        for s in siblings:
            manager.add_memory_edge(anchor.id, s.id, kind="supersedes")
        limit = 4
        results = manager.search("hub lighthouses", limit=limit)
        assert len(results) <= limit * 2
        graph_rows = [r for r in results if r.via_graph]
        assert len(graph_rows) <= limit

    def test_quarantined_neighbour_never_surfaces(self, manager) -> None:
        """ADR-0019 §5 holds on the graph path — an edge is never a
        quarantine side door."""
        new = _add(manager, "gate note about tunnels")
        old = _add(manager, "old tunnels note superseded and quarantined")
        manager.add_memory_edge(new.id, old.id, kind="supersedes")
        manager.sqlite.update_fields(
            old.id,
            pipeline_state=PipelineState.QUARANTINED.value,
            quarantine_reason="test quarantine",
        )
        results = manager.search("gate tunnels", limit=5)
        ids = {r.memory.id for r in results}
        assert old.id not in ids

    def test_raw_neighbour_gated_by_status_policy(self, manager) -> None:
        """RAW edge neighbours stay invisible under the default gate."""
        new = _add(manager, "published note about meadows")
        raw = _add(manager, "raw meadows note superseded", status=MemoryStatus.RAW)
        manager.add_memory_edge(new.id, raw.id, kind="supersedes")
        results = manager.search("published meadows", limit=5)
        ids = {r.memory.id for r in results}
        assert raw.id not in ids

    def test_refined_only_gates_graph_leg(self, manager) -> None:
        """ADR-0019 §4 composes: a pending neighbour never surfaces under
        refined_only."""
        new = _add(manager, "refined note about bridges")
        old = _add(manager, "pending bridges note superseded")
        manager.add_memory_edge(new.id, old.id, kind="supersedes")
        manager.sqlite.update_fields(old.id, pipeline_state=PipelineState.PENDING.value)
        results = manager.search("bridges", limit=5, refined_only=True)
        ids = {r.memory.id for r in results}
        assert old.id not in ids

    def test_decayed_score_below_fused_hits(self, manager) -> None:
        """Deterministic decay: the expansion row scores below its anchor
        (rank weight 2x the anchor's position)."""
        new = _add(manager, "signal record about beacons")
        old = _add(manager, "the earlier iteration of this document")
        manager.add_memory_edge(new.id, old.id, kind="supersedes")
        _fts_only(manager)
        results = manager.search("signal beacons", limit=5)
        by_id = {r.memory.id: r for r in results}
        assert new.id in by_id
        assert old.id in by_id, "the sibling arrives via the graph"
        assert by_id[old.id].via_graph
        # Anchor rank 1 ⇒ decay weight 1/(60+2) < the anchor's fused
        # score (which is at least (1-alpha)/(60+1)).
        assert by_id[old.id].score < by_id[new.id].score

    def test_get_incoming_edges_store_contract(self, manager) -> None:
        """Store-level: the incoming-edge read mirrors get_direct_edges."""
        a = _add(manager, "incoming probe a")
        b = _add(manager, "incoming probe b")
        manager.add_memory_edge(a.id, b.id, kind="supersedes")
        outgoing = manager.sqlite.get_direct_edges(a.id)
        incoming = manager.sqlite.get_incoming_edges(b.id)
        assert [e["to_memory_id"] for e in outgoing] == [b.id]
        assert [e["from_memory_id"] for e in incoming] == [a.id]
        assert manager.sqlite.get_incoming_edges(a.id) == []

    def test_explicit_status_drilldown_gates_graph_leg(self, manager) -> None:
        """Review F1 regression: an explicit ``status=`` drill-down gates the
        graph leg exactly like the fused legs — an edge must not widen an
        explicit status request.

        Pre-fix probe: ``search(status=PUBLISHED)`` surfaced a RAW
        sibling via_graph; ``search(status=ARCHIVED)`` surfaced a
        PUBLISHED sibling. The graph block only checked the default
        ``allowed`` set (which an explicit status skips entirely), so
        the drill-down's status predicate never reached the neighbours.
        The fixture rows all lexically match the query (the graph leg
        anchors from FUSED ids, so an anchor row must be findable by
        the query itself; the siblings differ from the anchors by
        STATUS, not by tokens — the gates under test are status gates).
        """
        query = "zeppelin fleet record"
        pub = _add(manager, "published zeppelin fleet record entry", status=MemoryStatus.PUBLISHED)
        raw = _add(manager, "raw zeppelin fleet record draft superseded", status=MemoryStatus.RAW)
        archived = _add(
            manager, "archived zeppelin fleet record ancestor", status=MemoryStatus.ARCHIVED
        )
        manager.add_memory_edge(pub.id, raw.id, kind="supersedes")
        manager.add_memory_edge(archived.id, pub.id, kind="supersedes")
        _fts_only(manager)  # pin: siblings reachable ONLY through the edges

        # status=PUBLISHED: the RAW sibling must not surface.
        ids = {r.memory.id for r in manager.search(query, status=MemoryStatus.PUBLISHED, limit=5)}
        assert pub.id in ids
        assert raw.id not in ids, "RAW sibling leaked through the graph into a PUBLISHED drill-down"

        # status=ARCHIVED: the PUBLISHED sibling must not surface.
        ids = {r.memory.id for r in manager.search(query, status=MemoryStatus.ARCHIVED, limit=5)}
        assert archived.id in ids
        assert pub.id not in ids, (
            "PUBLISHED sibling leaked through the graph into an ARCHIVED drill-down"
        )

    def test_scoped_search_never_leaks_cross_project_edges(self, manager) -> None:
        """Review F2 regression: the A9 authoritative project guard gates the
        graph leg — a cross-project neighbour must not surface in a scoped
        search, untagged and uncounted (only the soft-fallback retry may
        widen the scope, and it tags).

        Pre-fix probe: ``search(project="pa")`` surfaced a project-"pb"
        sibling via_graph. The edge stores ids only, so the resolve loop
        had to re-check the SQLite ``Memory.project`` (mirroring the
        vector-leg A9 guard).
        """
        pa_anchor = _add(manager, "anchor lighthouses beacon record")
        pb_sibling = _add(manager, "superseded lighthouse sibling from pb", project="pb")
        manager.add_memory_edge(pa_anchor.id, pb_sibling.id, kind="supersedes")
        _fts_only(manager)

        # Scoped: the pb sibling is invisible, and the result carries NO
        # fallback tag (the zero scoped page stays a scoped zero — the
        # soft-fallback retry DID run for the empty page, but the project
        # gate must hold on the retry's graph leg too; the pb sibling can
        # never arrive).
        scoped = manager.search("anchor lighthouses beacon", project=PROJECT, limit=5)
        assert pa_anchor.id in {r.memory.id for r in scoped}
        assert all(r.memory.project == PROJECT for r in scoped), "cross-project edge leaked"

        # Unscoped sanity: the same edge DOES expand in the explicit
        # global mode — the gate is scope-only, not a kill of the leg.
        unscoped = manager.search("anchor lighthouses beacon", limit=5)
        by_id = {r.memory.id: r for r in unscoped}
        assert pb_sibling.id in by_id
        assert by_id[pb_sibling.id].via_graph
