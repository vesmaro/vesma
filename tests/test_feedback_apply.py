"""ADR-0030 A1-S2 (issue #325) — feedback APPLY: the pheromone leg.

Committee-ratified surface (ArchCom 2026-09-27, verdict (c) S2; the
Security residuals are BINDING):
``mnemos.feedback_apply`` (default OFF, INDEPENDENT of
``mnemos.graph_walk``) makes the captured edge_stats ``used`` counters
multiply walked-block edge weights by a SATURATING factor at
walk-fill time:

* **I6 bounded Δ** (THE Security residual — the gate test that lands
  BEFORE any flag flip): the per-principal capture cap (10k) is the
  input bound; the SATURATION CAP enforces the structural output bound
  — one principal flooding ``used`` events up to the 10k cap CANNOT
  move one row's walked-block rank beyond the saturation bound, and a
  rank move never flips eligibility (the boost applies POST-gate).
* **feedback_epoch** (Security residual 2): a per-project meta-counter
  bumped on edge_stats CAPTURE (rows actually appended — captured > 0;
  idempotent retries bump nothing), key-discipline-identical to
  ``graph_epoch`` (``feedback_epoch:{len}:{project}``, colon-safe).
* **Flag-combo byte-identity** — graph_walk=ON + feedback_apply=OFF
  reproduces S1 pages byte-identically; graph_walk=OFF makes apply
  inert (the walk does not run).
* **Zero-capture store** — feedback_apply=ON ≡ feedback_apply=OFF when
  no edge_stats rows exist (f(0)=1.0 neutral factor — the empty-
  telemetry case the bench harness also proves).
* **Rank-only within the quota** — a used-heavy edge outranks a
  used-light sibling WITHIN the walked block; the fused block is
  untouched, eligibility is untouched, the page never expands past the
  quota/limit (I3/I6 — the S1 pins guard the mechanics; these extend
  them to the feedback factor).
* **Uniform-404 preserved** (I5) — the APPLY read path is internal
  (a batched ``used`` lookup at walk time); no surface change, the
  existing #358 pins stand unedited.

MUTATION PROTOCOL — each named killer test lists the minimal plausible
mutant that turns it RED (mutant diff → failing test id → revert in
the PR body).

Test embedder: ``_HashEmbedder`` — same rationale as the walk suite: a
MockedImage embedder cannot discriminate and would fake the ranking
semantics under test.
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
    _HashEmbedder,
    _settings,
)

from vesmaro.config import Settings  # noqa: E402 — after bootstrap
from vesmaro.manager import (  # noqa: E402 — after bootstrap
    FEEDBACK_BOOST_CAP,
    FEEDBACK_BOOST_SLOPE,
    MemoryManager,
    _feedback_boost_factor,
)
from vesmaro.models import (  # noqa: E402 — after bootstrap
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    MemoryUpdate,
)
from vesmaro.storage.sqlite_store import (  # noqa: E402 — after bootstrap
    EDGE_STATS_EVENTS_PER_PRINCIPAL_CAP,
)


def _apply_settings(tmp: Path, *, walk: bool = True, apply_: bool = True) -> Settings:
    """Walk + apply flags settable independently (the committee's
    independent-flags-per-leg condition — same ``_settings`` shape as
    the walk suite, plus the S2 flag)."""
    settings = _settings(tmp, walk=walk)
    settings.mnemos.feedback_apply = apply_
    return settings


@pytest.fixture
def apply_manager(tmp_path: Path) -> Iterator[MemoryManager]:
    """Walk ON + apply ON — the S2 leg under test (mint OFF: edges are
    hand-declared so each test pins exactly the edge it means)."""
    mgr = MemoryManager(_apply_settings(tmp_path))
    mgr._embedder = _HashEmbedder()
    yield mgr
    mgr.close()


@pytest.fixture
def s1_manager(tmp_path: Path) -> Iterator[MemoryManager]:
    """Walk ON + apply OFF — the A1-S1 surface (byte-identity baseline)."""
    mgr = MemoryManager(_apply_settings(tmp_path, apply_=False))
    mgr._embedder = _HashEmbedder()
    yield mgr
    mgr.close()


_CAPTURE_SEQ = [0]


def _capture_used(
    mgr: MemoryManager, memory_id: str, *, count: int = 1, project: str = PROJECT
) -> None:
    """Capture ``count`` ``used`` events on one memory through the REAL
    capture boundary (flag flip + report + flag restore — the report
    method is the only sanctioned write path; direct row inserts would
    bypass the I5 gates and the epoch bump). Event ids are globally
    unique across calls (a per-manager sequence): a test capturing the
    SAME memory twice (the flood test: CAP first, then up to the 10k
    per-principal cap) needs DISTINCT events — a repeated id would land
    in ``duplicates`` and append nothing."""
    mgr.settings.search.feedback_capture_enabled = True
    try:
        for i in range(count):
            _CAPTURE_SEQ[0] += 1
            outcome = mgr.report_search_feedback(
                [memory_id],
                kind="used",
                project=project,
                agent=AGENT,
                event_id=f"ev-{_CAPTURE_SEQ[0]}-{memory_id}",
            )
            assert outcome["captured"] == 1 or outcome["cap_dropped"] == 1, (
                f"fixture: event {i} must capture (or hit the I5 volume cap), got {outcome}"
            )
    finally:
        mgr.settings.search.feedback_capture_enabled = False


# ── Saturation math — the CAP/SLOPE unit pins ────────────────────────────────


class TestSaturationMath:
    def test_cap_slope_registered_constants(self) -> None:
        """The A1 calibration starting point (committee: "conservative
        values") is TABLE-PINNED: CAP=10, SLOPE=0.1 → max x2.0 boost.
        Revisitable at the A1 telemetry checkpoint — but only
        DELIBERATELY, through this table, never by drift.

        Killer mutant: change either constant without amending this
        table — RED.
        """
        assert FEEDBACK_BOOST_CAP == 10
        assert FEEDBACK_BOOST_SLOPE == 0.1
        assert pytest.approx(2.0) == 1 + FEEDBACK_BOOST_CAP * FEEDBACK_BOOST_SLOPE

    def test_factor_neutral_at_zero_and_saturating(self) -> None:
        """f(0)=1.0 (neutral — the zero-capture case), f monotone up to
        the cap, f PLATEAUS at ``1 + CAP x SLOPE`` past it however
        large the flood (the structural I6 bound; the per-principal
        10k capture cap can never push f beyond it).

        Killer mutant: drop the ``min`` in ``_feedback_boost_factor``
        (linear, unbounded) — the plateau assertions go RED.
        """
        assert _feedback_boost_factor(0) == pytest.approx(1.0)
        assert _feedback_boost_factor(1) == pytest.approx(1.1)
        assert _feedback_boost_factor(5) == pytest.approx(1.5)
        assert _feedback_boost_factor(FEEDBACK_BOOST_CAP) == pytest.approx(2.0)
        # Saturation: past the cap (incl. the 10k flood / the I5 clamp
        # at 1000) the factor NEVER grows — bounded Δ is structural.
        assert _feedback_boost_factor(FEEDBACK_BOOST_CAP + 1) == pytest.approx(2.0)
        assert _feedback_boost_factor(1_000) == pytest.approx(2.0)
        assert _feedback_boost_factor(EDGE_STATS_EVENTS_PER_PRINCIPAL_CAP) == pytest.approx(2.0)
        assert _feedback_boost_factor(10**9) == pytest.approx(2.0)
        # Negative counters cannot exist (COUNT(*) ≥ 0) but a hostile
        # or corrupt read must degrade to the neutral factor, never
        # to a NEGATIVE weight (a negative f would flip sign semantics
        # — I6 is rank-only, not direction-flipping).
        assert _feedback_boost_factor(-7) == pytest.approx(1.0)


# ── THE Security residual: I6 bounded-Δ mutation-verified gate test ─────────


class TestI6BoundedDelta:
    def test_10k_flood_cannot_move_rank_beyond_saturation(
        self, apply_manager: MemoryManager
    ) -> None:
        """THE committee gate test (verdict (c) condition 3, lands in
        THIS PR before any flag flip): ONE principal floods ``used``
        events on ONE edge up to the 10k per-principal capture cap —
        the target row's walked-block position may move, but never
        beyond the SATURATION bound (what f saturating at x2.0 can
        move against its used-light siblings), and its SCORE stays
        within ``[base, base x 2.0]``. The structural proof: past
        FEEDBACK_BOOST_CAP further events change NOTHING — the rank
        with the full 10k flood EQUALS the rank with exactly CAP
        events (plateau).

        Fixture: anchor + TWO siblings, the target one edge away with
        equal w_edge (equal anchor, equal depth, equal base score);
        the flood can lift the target above the sibling (that is the
        point of the leg) but the LIFT is exactly the saturation
        bound: ``score_flooded == score_cap x 1.0`` (f(10k) == f(CAP)),
        and the flooded score is ≤ 2 x the neutral score.

        Killer mutant: drop the ``min`` clamp in the factor (unbounded
        linear) — ``flooded == capped`` goes RED (f(10k) = 1001.0 ≠
        f(10)).
        """
        anchor = _add(apply_manager, "harbour cranes anchor note for the flood test")
        sibling = _add(apply_manager, "dormant sibling ledger reconciliation note alpha")
        target = _add(apply_manager, "dormant target ledger reconciliation note beta")
        apply_manager.add_memory_edge(anchor.id, sibling.id, kind="relates_to")
        apply_manager.add_memory_edge(anchor.id, target.id, kind="relates_to")
        apply_manager.vectors.wipe()

        # Baseline: neutral factors (zero capture) — the S1 ranking.
        # limit=6 → k=2: BOTH siblings fit the reserved walked-block quota.
        base_page = apply_manager.search("harbour cranes anchor", limit=6)
        base_scores = {r.memory.id: r.score for r in base_page}
        assert target.id in base_scores and sibling.id in base_scores

        # Flood ONE edge to exactly the saturation CAP.
        _capture_used(apply_manager, target.id, count=FEEDBACK_BOOST_CAP)
        cap_page = apply_manager.search("harbour cranes anchor", limit=6)
        cap_scores = {r.memory.id: r.score for r in cap_page}
        # Score moved by exactly the saturating factor — no more.
        assert cap_scores[target.id] == pytest.approx(
            base_scores[target.id] * (1 + FEEDBACK_BOOST_CAP * FEEDBACK_BOOST_SLOPE)
        ), "the boost must be exactly the saturating factor at the CAP"
        # The target outranks its used-light equal-weight sibling now
        # (within the walked block — the leg's purpose).
        cap_ids = [r.memory.id for r in cap_page]
        assert cap_ids.index(target.id) < cap_ids.index(sibling.id)
        # And the move stayed within the saturation bound: the lifted
        # score cannot exceed 2x the neutral score.
        assert cap_scores[target.id] <= 2.0 * base_scores[target.id]

        # THE structural assertion: flood the SAME principal up to the
        # 10k per-principal capture cap (the FIRST 10 events of the flood
        # already exist — the rest drops at the cap, exactly the I5
        # volume-cap semantics the test means to exercise) — rank and
        # score FREEZE at the saturation plateau (f(10k) == f(CAP));
        # bounded Δ. The cap-dropped tail is EXPECTED: the flood pushes
        # the bucket to the cap, then the volume cap takes over — the
        # per-memory counter stops growing either way, which is the
        # input bound of the I6 chain.
        _capture_used(apply_manager, target.id, count=EDGE_STATS_EVENTS_PER_PRINCIPAL_CAP)
        counters = apply_manager.sqlite.get_edge_stats_counters(target.id)
        assert counters["used"] <= EDGE_STATS_EVENTS_PER_PRINCIPAL_CAP
        flood_page = apply_manager.search("harbour cranes anchor", limit=6)
        flood_scores = {r.memory.id: r.score for r in flood_page}
        assert flood_scores[target.id] == pytest.approx(cap_scores[target.id]), (
            "I6 violated: a 10k-cap flood moved the row past the saturation bound"
        )
        assert [r.memory.id for r in flood_page] == cap_ids, (
            "I6 violated: a 10k-cap flood changed the walked-block ranking past saturation"
        )

    def test_boost_never_flips_eligibility(self, apply_manager: MemoryManager) -> None:
        """I6 rank-only, the eligibility axis: a gated-out row (RAW
        status — outside the admissible set) with a MASSIVE ``used``
        history NEVER surfaces — the boost applies POST-gATE, so no
        amount of captured usage can buy a row back onto the page.

        The used history is captured while the row is still
        PUBLISHED (capture itself is scoped — I5 — so a RAW row cannot
        even be REPORTED on; the fixture flips the row to RAW AFTER
        capture, exercising exactly the "history exists, eligibility
        changed later" path).

        Killer mutant: apply the factor BEFORE the gate loop (or drop
        the gate) — the gated-row assertion goes RED.
        """
        anchor = _add(apply_manager, "published anchor note about tide gates")
        gated = apply_manager.add(
            MemoryCreate(
                content="raw gated dormant ledger reconciliation note omega",
                tags=[f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:test"],
                source=MemorySource.MCP,
                status=MemoryStatus.PUBLISHED,
            ),
            project=PROJECT,
            agent=AGENT,
        )
        apply_manager.add_memory_edge(anchor.id, gated.id, kind="relates_to")
        apply_manager.vectors.wipe()
        # Heavy used history first (the row is PUBLISHED — capture-eligible).
        _capture_used(apply_manager, gated.id, count=FEEDBACK_BOOST_CAP)
        # THEN the row leaves the admissible set (a lifecycle change).
        flipped = apply_manager.update(gated.id, MemoryUpdate(status=MemoryStatus.RAW))
        assert flipped is not None and flipped.status == MemoryStatus.RAW

        page = apply_manager.search("published anchor tide gates", limit=5)
        ids = {r.memory.id for r in page}
        assert anchor.id in ids, "fixture: the published anchor must surface"
        assert gated.id not in ids, (
            "I6 violated: a boosted RAW row surfaced — the usage factor flipped eligibility"
        )


# ── Flag contract: independent flags, byte-identity combos ──────────────────


class TestFlagContract:
    def test_default_settings_apply_on(self) -> None:
        # Owner decision 2026-09-28 «graphs on by default» (rank-only,
        # bounded Δ per I6; inert while graph_walk is off — pinned below).
        assert Settings().mnemos.feedback_apply is True

    def test_graph_walk_off_makes_apply_inert(self, tmp_path: Path) -> None:
        """Independent flags (Security residual 3):
        graph_walk=OFF ⇒ apply is INERT even with feedback_apply=ON —
        the walked block never runs, so the factor has no surface to
        act on; the page is the plain A0 surface.

        Killer mutant: let the flag-OFF supersedes leg read edge_stats
        (the factor leaking past the walk boundary) — the byte-identity
        comparison with the apply=OFF twin goes RED.
        """
        mgr_on = MemoryManager(_apply_settings(tmp_path / "on", walk=False, apply_=True))
        mgr_on._embedder = _HashEmbedder()
        mgr_off = MemoryManager(_apply_settings(tmp_path / "off", walk=False, apply_=False))
        mgr_off._embedder = _HashEmbedder()
        sibling_ids: dict[str, str] = {}
        try:
            for label, mgr in (("on", mgr_on), ("off", mgr_off)):
                anchor = _add(mgr, "anchor note about lighthouse beacons")
                sibling = _add(mgr, "dormant sibling ledger reconciliation note")
                # walk=OFF: only the UNCONDITIONAL supersedes leg runs —
                # so the fixture rides a supersedes edge (the walk-only
                # relates_to leg never fires here).
                mgr.add_memory_edge(anchor.id, sibling.id, kind="supersedes")
                mgr.vectors.wipe()
                sibling_ids[label] = sibling.id
            _capture_used(mgr_on, sibling_ids["on"], count=FEEDBACK_BOOST_CAP)

            # TWO SEPARATE stores → ids differ per manager; the inertness
            # comparison runs on the QUERY-INVARIANT projection (content,
            # score, provenance) — the same discipline as the S1 cross-
            # revision A/B proof (page fingerprints over content, not ids).
            on_view = [
                (r.memory.content, r.score, r.via_graph)
                for r in mgr_on.search("anchor lighthouse beacons", limit=5)
            ]
            off_view = [
                (r.memory.content, r.score, r.via_graph)
                for r in mgr_off.search("anchor lighthouse beacons", limit=5)
            ]
            assert on_view and off_view, "fixture: pages non-empty"
            assert on_view == off_view, (
                "graph_walk=OFF must make apply inert: pages differ across the S2 flag"
            )
        finally:
            mgr_on.close()
            mgr_off.close()

    def test_apply_off_reproduces_s1_pages_byte_identically(self, tmp_path: Path) -> None:
        """THE flag-combo byte-identity pin (S2 contract point 4):
        graph_walk=ON + feedback_apply=OFF == S1 behavior — same
        store, same query, the two flag combos produce byte-identical
        pages (ids, scores, order, provenance). One manager, flag
        flipped between passes: the comparison is byte-exact, not
        corpus-coincident. Run with capture FLOWING (rows in
        edge_stats) — the OFF leg must not even READ them.

        Killer mutant: read edge_stats without the feedback_apply
        guard — the flag-off pass picks up the factor and the view
        comparison goes RED.
        """
        mgr = MemoryManager(_apply_settings(tmp_path, apply_=True))
        mgr._embedder = _HashEmbedder()
        try:
            rows = [
                "harbour cranes maintenance schedule winter entry",
                "harbour cranes spare parts ledger reconciliation entry",
                "harbour crane electrical schematics archive entry",
                "harbour crane gearbox service calendar entry",
                "harbour crane paint inspection dormant entry",
            ]
            ids = [_add(mgr, content) for content in rows]
            anchor = _add(mgr, "harbour cranes maintenance schedule winter entry extra")
            sibling = _add(mgr, "dormant lighthouse sibling ledger note")
            mgr.add_memory_edge(anchor.id, sibling.id, kind="relates_to")
            mgr.vectors.wipe()
            # Capture flows: used events land on BOTH a fused row and a
            # walk-reachable row — the OFF pass must ignore them all.
            _capture_used(mgr, ids[0].id, count=3)
            _capture_used(mgr, sibling.id, count=FEEDBACK_BOOST_CAP)

            for limit in (5, 8, 12, 20):
                mgr.settings.mnemos.feedback_apply = False
                s1_view = [
                    (r.memory.id, r.score, r.via_graph, r.via_graph_kind)
                    for r in mgr.search("harbour cranes maintenance", limit=limit)
                ]
                # P2-1 (review round 1): pin the OFF-page-neutrality
                # axis against a PRISTINE oracle, not OFF-vs-OFF — the
                # coordinated mutant "guard dropped + counter kept
                # honest" passes the view comparison (both legs mutated
                # identically, the leak cancels) and never trips the
                # counter. The capture-heavy sibling's walked score at
                # apply=OFF is its BASE decay value (no factor): with
                # FEEDBACK_BOOST_CAP used-events the mutant leaks the
                # x2.0 factor into the OFF leg and the sibling's score
                # doubles — this absolute pin reddens where the
                # relative one is blind.
                sib_scores = [s for (mid, s, *_rest) in s1_view if mid == sibling.id]
                assert sib_scores, "fixture: the capture-heavy sibling rides the walked block"
                # Base (factor-free) walked score for the sibling: the
                # S1 decay formula from the shipped code — alpha/(rrf_k
                # + 2*anchor_pos) * w_edge(=1.0 default) *
                # 0.7^(depth-1)(=1.0 at depth 1) — with the sibling's
                # anchor at fused position 2: 0.5/64 = 0.0078125. ANY
                # capture factor leaking into the OFF leg at CAP(10) x
                # SLOPE(0.1) = +1.0 doubles this value to 0.015625.
                base = 0.5 / (60 + 2 * 2)
                assert sib_scores[0] == pytest.approx(base), (
                    f"flag-off pass must not apply capture data: sibling walked score "
                    f"{sib_scores[0]!r} != factor-free base {base!r} — the x2.0 "
                    "saturation factor leaked into the OFF leg "
                    "(review P2-1 coordinated mutant)"
                )
                # Run the ON pass BETWEEN the two OFF passes — any state
                # leak from the ON leg (reads, counters, re-sorts)
                # surfaces in the second OFF pass below.
                mgr.settings.mnemos.feedback_apply = True
                mgr.search("harbour cranes maintenance", limit=limit)
                mgr.settings.mnemos.feedback_apply = False
                s1_again = [
                    (r.memory.id, r.score, r.via_graph, r.via_graph_kind)
                    for r in mgr.search("harbour cranes maintenance", limit=limit)
                ]
                assert s1_view, "fixture: page non-empty"
                # THE pin: OFF == S1, stable across an interleaved ON pass.
                assert s1_view == s1_again, (
                    f"flag-off page not byte-stable at limit={limit} "
                    f"(an ON pass leaked state into the OFF leg)"
                )
        finally:
            mgr.close()

    def test_zero_capture_store_flag_on_equals_flag_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE empty-telemetry pin (S2 contract point 6, battery: zero-
        capture): with NO edge_stats rows the neutral factor f(0)=1.0
        applies to every walked row — feedback_apply=ON reproduces the
        flag-OFF pages byte-identically (same store, same query, both
        flags). This is the case the bench harness proves on the S1
        corpus (fresh run → empty edge_stats → ≈baseline).

        Killer mutants: (a) a non-neutral base factor (e.g. f(0)=1.1)
        — the view comparison goes RED immediately; (b) an
        UNCONDITIONAL walked-block re-sort — sorting even a neutral
        block permutes the (anchor_pos, depth, id) collection order
        into score order. Mutant (b) is caught DETERMINISTICALLY: the
        ids are seeded via a monkeypatched uuid factory so the
        collection order (id-asc) provably DISAGREES with the score
        order (w_edge desc, 0.9 vs 0.7) — the ON pass must keep the
        collection order byte-for-byte to pass.
        """
        # Seeded id mint: rows are added anchor, s1, s2 → ids
        # ...000/001/002, so s1 < s2 lexicographically. Edge weights are
        # assigned so the collection order (id-asc: [s1, s2]) DISAGREES
        # with the score order (w desc: s2 first) — the mutant surface.
        seeded = iter(f"00000000-0000-4000-8000-{i:012d}" for i in range(8))
        monkeypatch.setattr(
            "vesmaro.models.uuid.uuid4",
            lambda: type("U", (), {"__str__": lambda s: next(seeded)})(),
        )
        mgr = MemoryManager(_apply_settings(tmp_path, apply_=True))
        mgr._embedder = _HashEmbedder()
        try:
            anchor = _add(mgr, "anchor note about windmill gears")
            s1 = _add(mgr, "dormant windmill sibling ledger note alpha")
            s2 = _add(mgr, "dormant windmill sibling ledger note beta")
            # s1 (id-earlier) carries the LOWER edge: collection order
            # [s1, s2] vs score order [s2, s1] — disagreement by construction.
            mgr.add_memory_edge(anchor.id, s1.id, kind="relates_to", weight=0.7)
            mgr.add_memory_edge(anchor.id, s2.id, kind="relates_to", weight=0.9)
            mgr.vectors.wipe()
            assert s1.id < anchor.id or s1.id != anchor.id, "fixture: distinct ids"
            assert s1.id < s2.id, "fixture: seeded ids must make s1 sort before s2"

            for limit in (3, 5, 10):
                mgr.settings.mnemos.feedback_apply = False
                off_view = [
                    (r.memory.id, r.score, r.via_graph_kind)
                    for r in mgr.search("anchor windmill gears", limit=limit)
                ]
                assert off_view, "fixture: page non-empty"
                mgr.settings.mnemos.feedback_apply = True
                on_view = [
                    (r.memory.id, r.score, r.via_graph_kind)
                    for r in mgr.search("anchor windmill gears", limit=limit)
                ]
                assert on_view == off_view, (
                    f"zero-capture byte-identity violated at limit={limit}: "
                    f"f(0) must be the neutral factor and the block order must stay the S1 order"
                )
            # Direct mutant-(b) surface: at limit=10 BOTH siblings ride
            # the walked block; the block order must be the COLLECTION
            # order [s1, s2] (id-asc), NOT the score order [s2, s1].
            page = mgr.search("anchor windmill gears", limit=10)
            walked = [r.memory.id for r in page if r.via_graph]
            assert walked == sorted(walked), "the walked block stays in id-asc collection order"
            assert walked[0] == s1.id and walked[1] == s2.id, (
                "the neutral block must NOT re-sort: collection order [s1, s2], not score order"
            )
        finally:
            mgr.close()


# ── Ranking: used-heavy outranks used-light WITHIN the quota ─────────────────


class TestRankOnlyWithinQuota:
    def test_used_heavy_edge_outranks_used_light_sibling(
        self, apply_manager: MemoryManager
    ) -> None:
        """The leg's purpose: a used-heavy edge outranks a used-light
        sibling WITHIN the walked block — same anchor, same depth,
        LOWER base w_edge: the saturating factor lifts the used-heavy
        row above it. Rank-only: the fused block is untouched, the
        page total stays ≤ limit.

        Killer mutant: multiply w_edge by the factor for ELIGIBILITY
        ordering pre-gate, or drop the post-append re-sort — the
        walked-block order assertion goes RED.
        """
        anchor = _add(apply_manager, "anchor note about sluice gates")
        light = _add(apply_manager, "dormant sluice sibling ledger note alpha")
        heavy = _add(apply_manager, "dormant sluice sibling ledger note beta")
        # heavy rides a LIGHTER edge: only the feedback factor can lift it.
        apply_manager.add_memory_edge(anchor.id, light.id, kind="relates_to", weight=0.9)
        apply_manager.add_memory_edge(anchor.id, heavy.id, kind="relates_to", weight=0.5)
        apply_manager.vectors.wipe()
        _capture_used(
            apply_manager, heavy.id, count=FEEDBACK_BOOST_CAP
        )  # f = 2.0; 0.5x2.0 = 1.0 > 0.9

        limit = 6  # k = 2 — BOTH siblings fit the reserved quota
        page = apply_manager.search("anchor sluice gates", limit=limit)
        ids = [r.memory.id for r in page]
        assert heavy.id in ids and light.id in ids, "fixture: both siblings must surface"
        assert ids.index(heavy.id) < ids.index(light.id), (
            "the used-heavy sibling must outrank the used-light one within the walked block"
        )
        assert len(page) <= limit, "rank-only: the boost never expands the page"

    def test_fused_block_untouched_by_boost(self, apply_manager: MemoryManager) -> None:
        """Rank-only, the fused axis: boosting a walk-reachable row
        never reorders the FUSED prefix — fused ids keep their exact
        S1 scores and order; only the walked block reorders.

        Killer mutant: apply the factor to fused scores (or sort the
        whole page) — the fused-prefix comparison goes RED.
        """
        anchor = _add(apply_manager, "anchor note about harbour cranes maintenance")
        _add(apply_manager, "harbour cranes maintenance schedule second entry")
        sibling = _add(apply_manager, "dormant harbour sibling ledger note")
        apply_manager.add_memory_edge(anchor.id, sibling.id, kind="relates_to")
        apply_manager.vectors.wipe()
        _capture_used(apply_manager, sibling.id, count=FEEDBACK_BOOST_CAP)

        apply_manager.settings.mnemos.feedback_apply = False
        off_page = apply_manager.search("harbour cranes maintenance", limit=5)
        apply_manager.settings.mnemos.feedback_apply = True
        on_page = apply_manager.search("harbour cranes maintenance", limit=5)

        fused_ids = {r.memory.id for r in off_page if not r.via_graph}
        off_fused = [(r.memory.id, r.score) for r in off_page if r.memory.id in fused_ids]
        on_fused = [(r.memory.id, r.score) for r in on_page if r.memory.id in fused_ids]
        assert off_fused == on_fused, "the boost reordered or rescored the fused block"
        assert [r.memory.id for r in on_page][: len(off_fused)] == [m for m, _ in on_fused], (
            "the fused prefix must stay the page head in the same order"
        )

    def test_quota_reservation_unchanged_by_boost(self, apply_manager: MemoryManager) -> None:
        """Rank-only, the quota axis: the boost NEVER lets walked rows
        exceed the reserved quota — a used-heavy hub cannot crowd the
        fused block beyond ``limit - k`` rows.

        Killer mutant: append boosted rows past the ``k`` cut — the
        walked-share assertion goes RED.
        """
        from vesmaro.manager import _walk_quota

        anchor = _add(apply_manager, "anchor note about beacon schedules")
        siblings = [_add(apply_manager, f"dormant beacon sibling note {i:02d}") for i in range(10)]
        for s in siblings:
            apply_manager.add_memory_edge(anchor.id, s.id, kind="relates_to")
            _capture_used(apply_manager, s.id, count=FEEDBACK_BOOST_CAP)
        apply_manager.vectors.wipe()

        limit = 5  # k = 1
        page = apply_manager.search("anchor beacon schedules", limit=limit)
        assert len(page) <= limit
        walked = [r for r in page if r.via_graph]
        assert len(walked) <= _walk_quota(limit), (
            "the boost let walked rows exceed the reserved quota"
        )


# ── feedback_epoch (Security residual 2) ─────────────────────────────────────


class TestFeedbackEpoch:
    def test_bump_on_capture_per_project(self, apply_manager: MemoryManager) -> None:
        """capture > 0 bumps the REPORTING PRINCIPAL's project counter
        — and ONLY it (per-project isolation, the same committee
        ruling as graph_epoch: a global epoch is a cheap fleet-wide
        cache-DoS lever).

        Killer mutant: bump a single global key — the isolation
        assertion goes RED.
        """
        a = _add(apply_manager, "anchor note about tide schedules")
        b = _add(apply_manager, "foreign anchor note", project=PROJECT_B)

        assert apply_manager.feedback_epoch(PROJECT) == 0
        assert apply_manager.feedback_epoch(PROJECT_B) == 0

        apply_manager.settings.search.feedback_capture_enabled = True
        try:
            apply_manager.report_search_feedback([a.id], kind="used", project=PROJECT, agent=AGENT)
            assert apply_manager.feedback_epoch(PROJECT) == 1
            assert apply_manager.feedback_epoch(PROJECT_B) == 0, "the epoch is per-project"
            apply_manager.report_search_feedback(
                [b.id], kind="used", project=PROJECT_B, agent=AGENT
            )
            assert apply_manager.feedback_epoch(PROJECT) == 1
            assert apply_manager.feedback_epoch(PROJECT_B) == 1
        finally:
            apply_manager.settings.search.feedback_capture_enabled = False

    def test_no_bump_on_idempotent_retry(self, apply_manager: MemoryManager) -> None:
        """Idempotent retries bump NOTHING (committee: "idempotent
        retries must not bump — bump only when the capture insert
        actually appended rows"): a retry of the same event_id lands
        entirely in ``duplicates`` → the epoch freezes. Also covers
        the fully-dropped report (flag off → zero writes → no bump).

        Killer mutant: bump on every call — the retry assertion goes RED.
        """
        row = _add(apply_manager, "anchor note about mooring lines")
        assert apply_manager.feedback_epoch(PROJECT) == 0

        apply_manager.settings.search.feedback_capture_enabled = True
        try:
            first = apply_manager.report_search_feedback(
                [row.id], kind="used", project=PROJECT, agent=AGENT, event_id="stable-1"
            )
            assert first["captured"] == 1
            assert apply_manager.feedback_epoch(PROJECT) == 1

            retry = apply_manager.report_search_feedback(
                [row.id], kind="used", project=PROJECT, agent=AGENT, event_id="stable-1"
            )
            assert retry["captured"] == 0 and retry["duplicates"] == 1
            assert apply_manager.feedback_epoch(PROJECT) == 1, (
                "an idempotent retry appended no rows — the epoch must not bump"
            )
        finally:
            apply_manager.settings.search.feedback_capture_enabled = False

    def test_no_bump_when_flag_off(self, apply_manager: MemoryManager) -> None:
        """Capture flag OFF (explicit; default-on since the owner
        decision of 2026-09-28) → zero writes → zero bumps (the capture
        flag gates the whole leg; the epoch tracks table change, and the
        table did not change).
        """
        apply_manager.settings.search.feedback_capture_enabled = False
        row = _add(apply_manager, "anchor note about dry dock gates")
        outcome = apply_manager.report_search_feedback(
            [row.id], kind="used", project=PROJECT, agent=AGENT
        )
        assert outcome["captured"] == 0
        assert apply_manager.feedback_epoch(PROJECT) == 0

    def test_colon_safe_project_slug_key(self, apply_manager: MemoryManager) -> None:
        """The #415 review finding applied to the feedback component
        (S2 contract point 3): a project slug containing ':' must not
        corrupt the epoch key — the ``feedback_epoch:{len}:{project}``
        length-prefix discipline survives it, and the snapshot parses
        it back unambiguously.

        Killer mutant: a bare ``f"feedback_epoch:{project}"`` key —
        the snapshot parse (or this read) goes RED for colon slugs.
        """
        colon_project = "team:edge:ops"
        row = _add(apply_manager, "anchor note about bollard inspections", project=colon_project)

        apply_manager.settings.search.feedback_capture_enabled = True
        try:
            apply_manager.report_search_feedback(
                [row.id], kind="used", project=colon_project, agent=AGENT
            )
        finally:
            apply_manager.settings.search.feedback_capture_enabled = False

        assert apply_manager.feedback_epoch(colon_project) == 1
        stats = apply_manager.search_stats()
        assert stats["feedback_epoch_by_project"].get(colon_project) == 1, (
            "a colon-bearing project slug must round-trip through the length-prefixed key"
        )
        # And it never collides with a DIFFERENT colon slug of the same
        # prefix family (the ambiguity a bare-split key would create).
        assert stats["feedback_epoch_by_project"].get("team:edge") is None

    def test_exposed_in_search_stats_and_dashboard(self, apply_manager: MemoryManager) -> None:
        """The epoch rides search_stats + the dashboard graph section
        (committee: epoch-keyed consumers must invalidate on feedback
        inserts once APPLY is live — the surface they key on).

        Killer mutant: drop the exposure — both keys go missing, RED.
        """
        row = _add(apply_manager, "anchor note about cargo manifests")
        _capture_used(apply_manager, row.id, count=1)

        stats = apply_manager.search_stats()
        assert stats["feedback_epoch_by_project"].get(PROJECT) == 1
        assert stats["feedback_epoch_by_project"].get(PROJECT_B, 0) == 0

        dashboard = apply_manager.dashboard_stats()
        assert dashboard["graph"]["feedback_epoch_by_project"].get(PROJECT) == 1
        assert dashboard["graph"]["feedback_apply_enabled"] is True


# ── The flag-on telemetry hook (S2 contract point 6) ─────────────────────────


class TestBoostedTelemetry:
    def test_boosted_counter_counts_only_influenced_queries(
        self, apply_manager: MemoryManager
    ) -> None:
        """``feedback_boosted_queries_total`` increments ONLY when a
        query's walked block actually used a non-neutral factor (a
        used-reachable row on the page) — flag-off queries and
        zero-capture queries leave it at 0.

        Killer mutant: count every walked query — the zero-assertion
        goes RED.
        """
        anchor = _add(apply_manager, "anchor note about mooring bollards")
        sibling = _add(apply_manager, "dormant mooring sibling ledger note")
        apply_manager.add_memory_edge(anchor.id, sibling.id, kind="relates_to")
        apply_manager.vectors.wipe()

        before = apply_manager.search_stats()["feedback_boosted_queries_total"]
        apply_manager.search("anchor mooring bollards", limit=5)  # zero capture
        assert apply_manager.search_stats()["feedback_boosted_queries_total"] == before

        _capture_used(apply_manager, sibling.id, count=2)
        page = apply_manager.search("anchor mooring bollards", limit=5)
        assert any(r.memory.id == sibling.id for r in page), "fixture: sibling surfaces"
        after = apply_manager.search_stats()["feedback_boosted_queries_total"]
        assert after == before + 1, "the boosted query must count exactly once"

        # Flag-off walk: the counter does not move even with capture
        # flowing (the OFF leg never reads edge_stats).
        apply_manager.settings.mnemos.feedback_apply = False
        apply_manager.search("anchor mooring bollards", limit=5)
        assert apply_manager.search_stats()["feedback_boosted_queries_total"] == after

    def test_dashboard_carries_boosted_counter(self, apply_manager: MemoryManager) -> None:
        assert "feedback_boosted_queries_total" in apply_manager.dashboard_stats()["graph"]


# ── Uniform-404 preserved (I5) — the APPLY read path stays internal ─────────


class TestUniform404Preserved:
    def test_report_surface_shape_unchanged_by_apply(self, apply_manager: MemoryManager) -> None:
        """The APPLY read path (a batched internal lookup at walk
        time) adds NO surface: the report outcome dict still carries
        counts only — no per-id reason, no existence oracle. The #358
        pins stand; this guards the read side did not grow a leg.

        Killer mutant: enrich the outcome with per-id feedback state
        — the keys assertion goes RED.
        """
        row = _add(apply_manager, "anchor note about winch controls")
        apply_manager.settings.search.feedback_capture_enabled = True
        try:
            outcome = apply_manager.report_search_feedback(
                [row.id, "nonexistent-id-000"], kind="used", project=PROJECT, agent=AGENT
            )
        finally:
            apply_manager.settings.search.feedback_capture_enabled = False
        assert set(outcome) == {
            "kind",
            "flag_enabled",
            "reported",
            "captured",
            "duplicates",
            "out_of_scope",
            "cap_dropped",
        }
        assert outcome["out_of_scope"] == 1, "fixture: the bogus id drops out of scope"
