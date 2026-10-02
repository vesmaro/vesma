"""ADR-0030 A1-S1 (issue #325) — walk mechanics contract tests.

Committee-ratified surface (ArchCom 2026-09-27, verdict (a)/(c)):
the reserved walk quota (flag-ON composition rule), the layered BFS-2
expansion (fanout + total-work caps, first-anchor-wins depth 2), the
per-project ``graph_epoch`` meta-counter, and the flag-OFF
byte-identity pin. Invariants I1-I3 at depth 2 (the committee
condition: the depth-2 killers must exist BEFORE BFS-2 enables) are
pinned here as mutation-verified contract tests in the same two-test
pattern as tests/test_graph_walk_invariants.py:

* **Byte-identity** — walk-ON + empty graph == walk-OFF page,
  byte-identical: proves slot-return AND the recall guard
  simultaneously (verdict (a) condition 3).
* **Quota** — k = min(ceil(limit/5), limit//2), 0 for limit < 3, a
  PURE function of ``limit``; the page totals at most ``limit``; the
  fused block fills ``limit - k`` and the surplus backfills a walk
  shortfall in (score desc, id asc) order.
* **BFS-2** — a depth-2 neighbour surfaces only behind the walk flag
  with a depth-2 decay (0.7^(depth-1)); fanout and total-work caps
  bound the expansion; first-anchor-wins holds across layers.
* **graph_epoch** — bumped per project on NEW edge writes (manager
  wrapper + the mint path), idempotent re-inserts bump nothing,
  exposed via ``search_stats()["graph_epoch_by_project"]``.
* **I1-I3 depth-2 killers** — worst-link gates, quarantine absorption
  and post-gate weights bind across the WHOLE 2-node path, not the
  current traversal frontier (the committee condition: the killers
  ship BEFORE BFS-2 enables).

MUTATION PROTOCOL — each named killer test lists the minimal plausible
mutant that turns it RED (mutant diff → failing test id → revert in
the PR body).
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from test_graph_walk_invariants import (  # noqa: E402 — sys.path bootstrap above
    AGENT,
    PROJECT,
    PROJECT_B,
    _add,
    _fts_only,
    _HashEmbedder,
    _settings,
)

from vesma.manager import MemoryManager, _walk_quota  # noqa: E402 — after bootstrap
from vesma.models import (  # noqa: E402 — after bootstrap
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    PipelineState,
)


@pytest.fixture
def walk_manager(tmp_path: Path) -> Iterator[MemoryManager]:
    """Walk flag ON (mint OFF — edges here are hand-declared)."""
    mgr = MemoryManager(_settings(tmp_path, walk=True))
    mgr._embedder = _HashEmbedder()
    yield mgr
    mgr.close()


@pytest.fixture
def plain_manager(tmp_path: Path) -> Iterator[MemoryManager]:
    """Both flags OFF (the shipped default)."""
    mgr = MemoryManager(_settings(tmp_path))
    mgr._embedder = _HashEmbedder()
    yield mgr
    mgr.close()


# ── Byte-identity: walk-ON + empty graph == walk-OFF page ────────────────────


class TestByteEqualityPin:
    def test_walk_on_empty_graph_equals_flag_off_page(self, tmp_path: Path) -> None:
        """The committee's double-proof pin (verdict (a) condition 3):
        with an EMPTY graph the flag-ON page — fused block cut at
        ``limit - k``, reserved slots returning via the surplus
        backfill — must equal the flag-OFF page EXACTLY: same ids,
        same scores, same order, same provenance flags. This proves
        slot-return (no quality rent on an edge-less corpus) AND the
        recall guard (bench-s1 runs flag-off; this pin extends the
        guarantee to flag-on) in one assertion surface.

        One manager, flag flipped between passes — the SAME corpus
        (same row ids) rides both paths, so the comparison is
        byte-exact, not corpus-coincident.

        Killer mutant: drop the surplus backfill on the flag-ON path —
        the flag-ON page ships ``limit - k`` rows and this goes RED.
        """
        mgr = MemoryManager(_settings(tmp_path, walk=True))
        mgr._embedder = _HashEmbedder()
        try:
            rows = [
                "harbour cranes maintenance schedule winter",
                "harbour cranes spare parts ledger reconciliation",
                "harbour crane electrical schematics archive note",
                "lighthouse beacon rotation service calendar",
                "lighthouse lens polishing procedure dormant",
                "dry dock gate operation manual entry",
                "dry dock gate winch maintenance ledger",
            ]
            for content in rows:
                _add(mgr, content)
            for limit in (5, 8, 12, 20):
                mgr.settings.vesma.graph_walk = True
                page_on = mgr.search("harbour cranes lighthouse", limit=limit)
                mgr.settings.vesma.graph_walk = False
                page_off = mgr.search("harbour cranes lighthouse", limit=limit)
                assert len(page_on) == len(page_off), (
                    f"slot-return violated at limit={limit}: "
                    f"walk-on page {len(page_on)} rows vs walk-off {len(page_off)}"
                )
                on_view = [(r.memory.id, r.score, r.via_graph) for r in page_on]
                off_view = [(r.memory.id, r.score, r.via_graph) for r in page_off]
                assert on_view == off_view, (
                    f"byte-equality violated at limit={limit}: "
                    f"walk-on {on_view} != walk-off {off_view}"
                )
        finally:
            mgr.close()


# ── Reserved quota — the composition rule ────────────────────────────────────


class TestReservedQuota:
    def test_quota_is_pure_function_of_limit(self) -> None:
        """k = min(ceil(limit/5), limit//2); 0 for limit < 3 — the exact
        committee formula, quotable as a table. No store state enters
        the quota (the adaptive-floor alternative was rejected as a
        determinism hazard).

        Killer mutant: change the formula (e.g. drop the limit//2 clamp
        or the limit<3 guard) — the table goes RED.
        """
        assert _walk_quota(0) == 0
        assert _walk_quota(1) == 0
        assert _walk_quota(2) == 0
        assert _walk_quota(3) == 1
        assert _walk_quota(4) == 1
        assert _walk_quota(5) == 1
        assert _walk_quota(6) == 2
        assert _walk_quota(7) == 2
        assert _walk_quota(8) == 2
        assert _walk_quota(10) == 2
        assert _walk_quota(20) == 4
        assert _walk_quota(50) == 10
        assert _walk_quota(100) == 20

    def test_full_fused_page_still_enriches_flag_on(self, walk_manager: MemoryManager) -> None:
        """THE headroom fix (the A0 walk-DOA measurement): a FULL fused
        page needs no enrichment under flag-OFF — but flag-ON reserves
        k slots, so a graph-eligible neighbour surfaces even when the
        fused block alone would fill the page. The fused block yields
        exactly k tail rows to the walk.

        Killer mutant: keep the A0 ``len(results) < limit`` headroom
        gate on the flag-ON path — the dormant sibling never surfaces
        and this goes RED (the pre-A1 dead-on-arrival finding).
        """
        fused_rows = [
            "harbour cranes maintenance schedule winter entry",
            "harbour cranes spare parts ledger reconciliation entry",
            "harbour crane electrical schematics archive entry",
            "harbour crane gearbox service calendar entry",
            "harbour crane paint inspection dormant entry",
        ]
        for content in fused_rows:
            _add(walk_manager, content)
        anchor = _add(walk_manager, "harbour cranes maintenance schedule winter entry")
        dormant = _add(walk_manager, "unrelated dormant ledger sibling note")
        walk_manager.add_memory_edge(anchor.id, dormant.id, kind="relates_to")
        _fts_only(walk_manager)

        limit = 5  # k = 1
        results = walk_manager.search("harbour cranes", limit=limit)
        assert len(results) <= limit, "total page must stay <= limit"
        by_id = {r.memory.id: r for r in results}
        assert dormant.id in by_id, "the reserved slot must enrich a full fused page"
        assert by_id[dormant.id].via_graph is True

    def test_page_never_exceeds_limit_flag_on(self, walk_manager: MemoryManager) -> None:
        """Total page ≤ limit (committee condition 7): the fused block +
        walk block + surplus backfill never overshoots ``limit``.

        Killer mutant: let the backfill run past ``limit`` — this and
        the cap test in test_search_v2_graph_leg.py both go RED.
        """
        anchor = _add(walk_manager, "hub record about lighthouses")
        siblings = [_add(walk_manager, f"lighthouse sibling record number {i}") for i in range(10)]
        for s in siblings:
            walk_manager.add_memory_edge(anchor.id, s.id, kind="relates_to")
        _fts_only(walk_manager)

        results = walk_manager.search("hub lighthouses", limit=5)
        assert len(results) <= 5

    def test_surplus_backfill_returns_slots_partial_walk(self, walk_manager: MemoryManager) -> None:
        """Walk shortfall: ONE eligible walk row but k=2 reserved — the
        fused surplus fills the second reserved slot in (score desc,
        id asc) order; the page ships FULL ``limit`` rows with the walk
        row after the fused block (ADR-0028 order preserved).

        Killer mutant: drop the post-walk backfill — the page ships
        limit-1 rows and this goes RED (the partial counterpart of the
        byte-equality pin: slots return on PARTIAL walk fuel too).
        """
        rows = [
            "windmill gear inspection calendar entry",
            "windmill gear lubrication ledger entry",
            "windmill gear alignment procedure entry",
            "windmill gear replacement history entry",
            "windmill gear paint inspection entry",
        ]
        for content in rows:
            _add(walk_manager, content)
        # The anchor is the FUSED FIRST HIT (its content is a corpus row
        # above — the edge hangs off the page's own top row, so the
        # dormant sibling is reachable ONLY through the walk).
        anchor = walk_manager.sqlite.fts_search(
            "windmill gear", limit=1, project=None, agent=None, status=None
        )[0][0]
        dormant = _add(walk_manager, "unrelated dormant sibling note")
        walk_manager.add_memory_edge(anchor.id, dormant.id, kind="relates_to")
        _fts_only(walk_manager)

        limit = 6  # k = 2, fused fill = 4
        results = walk_manager.search("windmill gear", limit=limit)
        assert len(results) == limit, (
            "the surplus backfill must return unfilled slots "
            f"(page has {len(results)} rows; corpus has 5 FTS-matching rows "
            "+ 1 walk row)"
        )
        walk_rows = [r for r in results if r.via_graph]
        assert len(walk_rows) == 1
        assert walk_rows[0].memory.id == dormant.id
        # The fused block (4 rows) precedes the walk row; the backfilled
        # fused rows carry NO graph provenance.
        assert all(not r.via_graph for r in results[:4])


# ── BFS-2 — layered expansion mechanics ──────────────────────────────────────


class TestBFS2Layered:
    def test_depth2_neighbour_surfaces_with_depth_decay(self, walk_manager: MemoryManager) -> None:
        """The layered BFS-2 (ADR-0030 §3): anchor → mid (depth 1) →
        far (depth 2, reached ONLY through mid — no direct edge to the
        anchor). The far row surfaces behind the walk flag with the
        0.7^(depth-1) depth factor: its decay is exactly 0.7 x the
        depth-1 decay of the same anchor position and weight.

        Killer mutant: drop the depth factor (flat 0.7^0) — the exact
        formula assertion goes RED; drop the layer-2 expansion — the
        far row never surfaces.
        """
        anchor = _add(walk_manager, "anchor note about tide schedules")
        mid = _add(walk_manager, "middle dormant ledger reconciliation note")
        far = _add(walk_manager, "far flung quarterly figures note")
        walk_manager.add_memory_edge(anchor.id, mid.id, kind="relates_to", weight=1.0)
        walk_manager.add_memory_edge(mid.id, far.id, kind="relates_to", weight=1.0)
        _fts_only(walk_manager)

        results = walk_manager.search("anchor tide schedules", limit=10)
        by_id = {r.memory.id: r for r in results}
        assert mid.id in by_id and by_id[mid.id].via_graph is True
        assert far.id in by_id and by_id[far.id].via_graph is True
        # Exact A1 formula: depth-1 decay (1-0.5)/(60+2*1) * 1.0 * 0.7^0;
        # depth-2 decay (1-0.5)/(60+2*1) * 1.0 * 0.7^1 (inherited anchor
        # pos 1, first-anchor-wins attribution).
        assert by_id[mid.id].score == pytest.approx((1.0 - 0.5) / 62.0)
        assert by_id[far.id].score == pytest.approx((1.0 - 0.5) / 62.0 * 0.7)
        assert by_id[far.id].score < by_id[mid.id].score

    def test_depth2_reached_directly_stays_depth1(self, walk_manager: MemoryManager) -> None:
        """First-anchor-wins across layers: a neighbour with BOTH a
        direct anchor edge (depth 1) and a 2-hop path through a mid
        row is claimed at depth 1 by its first anchor — the direct
        edge's weight and no depth factor apply.

        Killer mutant: prefer the deeper discovery (or overwrite the
        depth on the second sighting) — the exact-score assertion goes
        RED (the row would decay 0.7x).
        """
        anchor = _add(walk_manager, "anchor note about tide schedules")
        mid = _add(walk_manager, "middle dormant ledger reconciliation note")
        both = _add(walk_manager, "directly linked quarterly figures note")
        walk_manager.add_memory_edge(anchor.id, mid.id, kind="relates_to", weight=1.0)
        walk_manager.add_memory_edge(anchor.id, both.id, kind="relates_to", weight=2.0)
        walk_manager.add_memory_edge(mid.id, both.id, kind="relates_to", weight=10.0)
        _fts_only(walk_manager)

        results = walk_manager.search("anchor tide schedules", limit=10)
        by_id = {r.memory.id: r for r in results}
        assert both.id in by_id
        # Claimed at depth 1 via the DIRECT edge: weight 2.0, no 0.7
        # factor — the depth-2 route (weight 10) neither outranks nor
        # re-attributes the row (first-edge-wins within the anchor's
        # discovery order: supersedes pass, then relates in (w, id)).
        assert by_id[both.id].score == pytest.approx((1.0 - 0.5) / 62.0 * 2.0)

    def test_fused_row_never_resurfaced_from_depth2(self, walk_manager: MemoryManager) -> None:
        """A FUSED row reachable at depth 2 through a mid row is never
        re-appended — the walk expands around the fused page, never
        re-serves it (the A0 exclusion rule extends to layer 2). The
        fused row's content carries EVERY query token (FTS AND
        semantics — the row must genuinely fuse), while the mid row
        carries none (its only path is the depth-1 edge).

        Killer mutant: drop the ``in scores`` exclusion for depth-2
        claims — the fused row duplicates (or gets re-marked
        ``via_graph``) and this goes RED.
        """
        anchor = _add(walk_manager, "anchor note about tide schedules")
        mid = _add(walk_manager, "middle dormant ledger reconciliation note")
        fused = _add(walk_manager, "anchor tide schedules fused sibling reachable at depth two")
        walk_manager.add_memory_edge(anchor.id, mid.id, kind="relates_to")
        walk_manager.add_memory_edge(mid.id, fused.id, kind="relates_to")
        _fts_only(walk_manager)

        results = walk_manager.search("anchor tide schedules", limit=10)
        by_id = {r.memory.id: r for r in results}
        assert fused.id in by_id  # it FUSES (the query matches it lexically)
        assert by_id[fused.id].via_graph is False
        assert by_id[anchor.id].via_graph is False

    def test_total_work_cap_bounds_expansion(self, walk_manager: MemoryManager) -> None:
        """The total-work cap (ADR-0030 §3): a hub fixture with more
        candidates than the cap still returns a bounded page — the
        collection is work-bounded as a function of the corpus, not of
        ``limit`` (the fanout/total caps exist so a tag-hub row cannot
        dominate a search).

        Killer mutant: remove the work cap from the collector — the
        per-anchor claim count exceeds the cap and the (bounded)
        assertion surface goes RED on the exact collected count.
        """
        from vesma.manager import WALK_TOTAL_WORK_CAP

        anchor = _add(walk_manager, "hub record about lighthouses")
        # More candidates than the cap, all claimable from one anchor.
        for i in range(WALK_TOTAL_WORK_CAP + 50):
            s = _add(walk_manager, f"lighthouse sibling record number {i:04d} dormant")
            walk_manager.add_memory_edge(anchor.id, s.id, kind="relates_to")
        _fts_only(walk_manager)

        cands = walk_manager._collect_walk_candidates([anchor.id], scores={anchor.id: 1.0})
        assert len(cands) <= WALK_TOTAL_WORK_CAP, (
            "the total-work cap must bound the collected candidate set"
        )
        # The page itself stays within limit regardless of the hub size.
        results = walk_manager.search("hub lighthouses", limit=10)
        assert len(results) <= 10


# ── Depth-2 I1/I2/I3 killers (committee condition 1: BEFORE BFS-2 enables) ────


class TestDepth2Killers:
    def test_i1_depth2_gates_bind_to_query_scope(self, walk_manager: MemoryManager) -> None:
        """I1 worst-link at depth 2: a 2-hop path anchor → mid → raw
        never rides the path's admissibility into a default-gated
        page — the depth-2 row is gated on its OWN status, exactly as
        at depth 1 (gates bind to the query scope across the WHOLE
        traversed path).

        Killer mutant: gate only the depth-1 layer (skip the depth-2
        rows at append) — the raw row leaks and this goes RED.
        """
        anchor = _add(walk_manager, "published anchor about harbour cranes")
        mid = _add(walk_manager, "middle dormant ledger reconciliation note")
        raw_far = _add(walk_manager, "raw far dormant ledger sibling note", status=MemoryStatus.RAW)
        walk_manager.add_memory_edge(anchor.id, mid.id, kind="relates_to")
        walk_manager.add_memory_edge(mid.id, raw_far.id, kind="relates_to", weight=1000.0)
        _fts_only(walk_manager)

        results = walk_manager.search("published anchor harbour cranes", limit=10)
        ids = {r.memory.id for r in results}
        assert anchor.id in ids
        assert mid.id in ids, "fixture: the depth-1 row is eligible and must surface"
        assert raw_far.id not in ids, (
            "I1 violated at depth 2: the RAW row rode the 2-hop path into a default page"
        )

    def test_i2_depth2_quarantine_absorbs(self, walk_manager: MemoryManager) -> None:
        """I2 absorbing quarantine at depth 2: a quarantined row at the
        far end of a 2-hop path never surfaces — including under an
        explicit ``status=PUBLISHED`` drill-down, where the §5
        predicate is the ONLY standing guard (quarantined rows carry
        status='published').

        Killer mutant: skip the §5 check for depth-2 rows — the
        drill-down assertion goes RED (nothing else stands guard).
        """
        anchor = _add(walk_manager, "gate note about tunnels")
        mid = _add(walk_manager, "middle dormant ledger reconciliation note")
        quarantined_far = _add(walk_manager, "old far tunnels note now quarantined")
        walk_manager.add_memory_edge(anchor.id, mid.id, kind="relates_to")
        walk_manager.add_memory_edge(mid.id, quarantined_far.id, kind="relates_to")
        walk_manager.sqlite.update_fields(
            quarantined_far.id,
            pipeline_state=PipelineState.QUARANTINED.value,
            quarantine_reason="test quarantine",
        )
        _fts_only(walk_manager)

        default = walk_manager.search("gate tunnels", limit=10)
        assert quarantined_far.id not in {r.memory.id for r in default}, (
            "I2 violated at depth 2 under the default gate"
        )
        drilldown = walk_manager.search("gate tunnels", status=MemoryStatus.PUBLISHED, limit=10)
        assert quarantined_far.id not in {r.memory.id for r in drilldown}, (
            "I2 violated at depth 2: the quarantined row leaked into a PUBLISHED "
            "drill-down — §5 was its only guard"
        )

    def test_i3_depth2_weight_never_restores_eligibility(self, walk_manager: MemoryManager) -> None:
        """I3 post-gate weights at depth 2: a heavy (1000.0) discovery
        edge on the mid → far hop buys the gated far row NOTHING —
        weights rank within the walked block only and never restore
        eligibility (the M3b mutant at depth 2).

        Killer mutant: apply w_edge before the gate at depth 2 — the
        raw/quarantined rows surface and this goes RED.
        """
        anchor = _add(walk_manager, "published anchor about windmills")
        mid = _add(walk_manager, "middle dormant ledger reconciliation note")
        raw_heavy = _add(
            walk_manager, "raw far dormant ledger sibling heavy", status=MemoryStatus.RAW
        )
        walk_manager.add_memory_edge(anchor.id, mid.id, kind="relates_to")
        walk_manager.add_memory_edge(mid.id, raw_heavy.id, kind="relates_to", weight=1000.0)
        _fts_only(walk_manager)

        default = walk_manager.search("published anchor windmills", limit=10)
        ids = {r.memory.id for r in default}
        assert mid.id in ids, "fixture: the eligible depth-1 row must surface"
        assert raw_heavy.id not in ids, (
            "I3 violated at depth 2: a 1000-weight edge restored a gated row's eligibility"
        )

    def test_depth2_project_guard_holds(self, walk_manager: MemoryManager) -> None:
        """A9 project guard at depth 2: a cross-project row at the far
        end of a 2-hop path never leaks into a scoped search (the
        edge stores ids only — the resolve-time guard is the only
        thing standing between the id and the page, at ANY depth).

        Killer mutant: skip the project guard for depth-2 rows — the
        scoped assertion goes RED.
        """
        anchor = _add(walk_manager, "anchor lighthouses beacon record")
        mid = _add(walk_manager, "middle dormant ledger reconciliation note")
        foreign_far = _add(
            walk_manager,
            "foreign far lighthouse sibling from another project",
            project=PROJECT_B,
        )
        walk_manager.add_memory_edge(anchor.id, mid.id, kind="relates_to")
        walk_manager.add_memory_edge(mid.id, foreign_far.id, kind="relates_to")
        _fts_only(walk_manager)

        scoped = walk_manager.search("anchor lighthouses beacon", project=PROJECT, limit=10)
        assert all(r.memory.project == PROJECT for r in scoped), (
            "A9 violated at depth 2: a cross-project row leaked into a scoped search"
        )


# ── Backfill gate binding (#415 review, P1/P2) ──────────────────────────────


class TestBackfillGates:
    def test_starved_fused_tags_query_serves_no_untagged_rows(
        self, walk_manager: MemoryManager
    ) -> None:
        """P2 killer (#415 review, I1 worst-link at the composition
        boundary): a tags-filtered query with a PARTIALLY filled fused
        block — 2 tagged rows fuse, 6 untagged candidates sit in the
        surplus — must never backfill the page with untagged rows: the
        fused loop's tags filter binds identically on the backfill leg.
        The query's gates bind to the composition rule, never the
        other way round.

        Killer mutant: drop the backfill's tags filter — 6 untagged
        rows land on the page and this goes RED.
        """
        for i in range(2):
            walk_manager.add(
                MemoryCreate(
                    content=f"wanted tagged row number {i} about omega psi chi",
                    tags=[f"project:{PROJECT}", f"agent:{AGENT}", "wanted", "mnemos:test"],
                    source=MemorySource.MCP,
                    status=MemoryStatus.PUBLISHED,
                ),
                project=PROJECT,
                agent=AGENT,
            )
        for i in range(6):
            _add(walk_manager, f"untagged corpus row number {i:02d} about omega psi chi")
        _fts_only(walk_manager)

        results = walk_manager.search("omega psi chi", tags=["wanted"], limit=8)
        untagged = [r for r in results if "wanted" not in r.memory.tags]
        assert results, "fixture: the tagged fused rows must surface"
        assert untagged == [], (
            f"P2/I1 violated: {len(untagged)} untagged rows backfilled a "
            "tags-filtered page — the backfill skipped the fused loop's tags filter"
        )
        assert all("wanted" in r.memory.tags for r in results)

    def test_starved_fused_walk_on_returns_empty_page_no_crash(
        self, walk_manager: MemoryManager
    ) -> None:
        """P1 crash-pin (#415 review): an EMPTY fused block on the
        flag-ON path (the tags filter drops every candidate at
        ``limit >= 3`` — ``fused_surplus`` holds ALL candidates, the
        fused block holds none) must return an empty page GRACEFULLY:
        the composition rule is a no-op on an empty graph, never an
        UnboundLocalError that takes down the whole search. The
        flag-OFF path already returns an empty page for the same query
        (the two surfaces must agree).

        Killer mutant: initialise ``seen`` only inside the walk branch
        — the search raises UnboundLocalError and this goes RED.
        """
        _add(walk_manager, "omega psi chi note one")
        _add(walk_manager, "omega psi chi note two")
        _fts_only(walk_manager)

        page = walk_manager.search("omega psi chi", tags=["wanted"], limit=5)
        assert page == [], (
            "the starved fused block must yield an empty page, not rows the tags filter rejected"
        )
        # The flag-OFF surface agrees (the empty page is the shared
        # contract, not a flag-ON quirk).
        walk_manager.settings.vesma.graph_walk = False
        page_off = walk_manager.search("omega psi chi", tags=["wanted"], limit=5)
        assert page_off == []


# ── graph_epoch — per-project meta-counter (committee condition 2) ──────────


class TestGraphEpoch:
    def test_epoch_bumps_on_new_edge_per_project(self, walk_manager: MemoryManager) -> None:
        """The epoch is PER-PROJECT (NOT global — a global epoch would
        be a cheap fleet-wide cache-DoS lever, ArchCom verdict (c)
        condition 2): a declared edge write bumps ONLY the from-row's
        project counter; a different project's epoch stays untouched.

        Killer mutant: bump a single global key — the per-project
        isolation assertion goes RED.
        """
        a = _add(walk_manager, "anchor note about tide schedules", project=PROJECT)
        b = _add(walk_manager, "sibling note about tide schedules", project=PROJECT)
        other = _add(walk_manager, "foreign anchor note", project=PROJECT_B)
        other_sibling = _add(walk_manager, "foreign sibling note", project=PROJECT_B)

        assert walk_manager.graph_epoch(PROJECT) == 0
        assert walk_manager.graph_epoch(PROJECT_B) == 0

        walk_manager.add_memory_edge(a.id, b.id, kind="relates_to")
        assert walk_manager.graph_epoch(PROJECT) == 1
        assert walk_manager.graph_epoch(PROJECT_B) == 0, "the epoch is per-project"

        walk_manager.add_memory_edge(other.id, other_sibling.id, kind="relates_to")
        assert walk_manager.graph_epoch(PROJECT) == 1
        assert walk_manager.graph_epoch(PROJECT_B) == 1

    def test_epoch_idempotent_reinsert_bumps_nothing(self, walk_manager: MemoryManager) -> None:
        """The epoch tracks EDGE-TABLE CHANGE, not call volume: an
        idempotent re-insert of an existing edge bumps nothing.

        Killer mutant: bump unconditionally in the wrapper — the
        re-insert assertion goes RED.
        """
        a = _add(walk_manager, "anchor note about tide schedules")
        b = _add(walk_manager, "sibling note about tide schedules")
        assert walk_manager.add_memory_edge(a.id, b.id, kind="relates_to") is True
        assert walk_manager.graph_epoch(PROJECT) == 1
        assert walk_manager.add_memory_edge(a.id, b.id, kind="relates_to") is False
        assert walk_manager.graph_epoch(PROJECT) == 1, (
            "an idempotent re-insert must not bump the epoch"
        )

    def test_epoch_exposed_in_search_stats_and_dashboard(self, walk_manager: MemoryManager) -> None:
        """The epoch is exposed via ``search_stats()["graph_epoch_by_project"]``
        (and the dashboard graph section) so any consumer can key on
        it — there is NO in-repo search cache key today; the counter is
        the surface a future consumer keys on (committee note).

        Killer mutant: drop the exposure — the key goes missing and
        both assertions go RED.
        """
        a = _add(walk_manager, "anchor note about tide schedules")
        b = _add(walk_manager, "sibling note about tide schedules")
        walk_manager.add_memory_edge(a.id, b.id, kind="relates_to")

        stats = walk_manager.search_stats()
        assert stats["graph_epoch_by_project"].get(PROJECT) == 1
        assert stats["graph_epoch_by_project"].get(PROJECT_B, 0) == 0

        dashboard = walk_manager.dashboard_stats()
        assert dashboard["graph"]["graph_epoch_by_project"].get(PROJECT) == 1

    def test_mint_path_bumps_epoch(self, tmp_path: Path) -> None:
        """The mint path (``graph_auto_mint`` ON) routes through the
        manager wrapper, so a minted edge bumps the epoch — the
        committee's "bumped on edge writes" covers BOTH write surfaces.

        Killer mutant: call ``sqlite.add_memory_edge`` directly from
        the mint loop — the epoch stalls at 0 and this goes RED.
        """
        mgr = MemoryManager(_settings(tmp_path, walk=True, mint=True))
        mgr._embedder = _HashEmbedder()
        try:
            from tests.test_graph_auto_minting import NEAR_DUP_A, NEAR_DUP_B  # noqa: F401

            mgr.add(
                MemoryCreate(
                    content="conveyor belt alignment procedure station seven",
                    tags=[f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:test"],
                    source=MemorySource.MCP,
                    status=MemoryStatus.PUBLISHED,
                ),
                project=PROJECT,
                agent=AGENT,
            )
            mgr.add(
                MemoryCreate(
                    content="conveyor belt alignment procedure station seven revision",
                    tags=[f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:test"],
                    source=MemorySource.MCP,
                    status=MemoryStatus.PUBLISHED,
                ),
                project=PROJECT,
                agent=AGENT,
            )
            minted = mgr.graph_mint_stats()["auto_dedupe_edges_total"]
            assert minted > 0, "fixture: minting must produce the edge"
            assert mgr.graph_epoch(PROJECT) == minted, (
                "every minted edge must bump the epoch exactly once"
            )
        finally:
            mgr.close()
