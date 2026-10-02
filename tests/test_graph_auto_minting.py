"""ADR-0030 A0 (issue #322) — deterministic ``relates_to`` auto-minting on write.

The write-path fuel leg: after every ``MemoryManager.add`` ONE
synchronous retrieval over the existing legs (query embed + cosine
top-k over ``VectorStore.search``) mints up to 3 ``relates_to`` edges
to near-duplicate candidates (provenance ``auto-dedupe``, raised
weight). Covered here:

* flag-off default: zero edges, search semantics untouched;
* the happy path: near-dup write mints the edge with provenance and
  weight; a related-but-not-near row stays below the threshold;
  top-3 cap;
* the candidate-set EXCLUSIONS (each a hard ADR-0030/issue-322
  requirement): ``mnemos:no-federate`` (I4 generation side), §5
  quarantine (ADR-0019, absolute), cross-project rows (intra-project
  only), non-admissible statuses (raw/processing), plus self and
  ``via_graph`` rows — both at the pure-selector level and end-to-end.
  The exclusion fixtures use one-token-off twins (measured cosine
  ~0.97, comfortably above the threshold) so the EXCLUSION is what
  drops them, not a weak similarity;
* idempotency per (from, to, provenance): re-minting inserts nothing,
  and minted PAIRS are undirected (review L1 — a backfill re-mint of
  the older end never duplicates the pair in reverse; a declared
  reverse edge stays insertable);
* from-side gates (review M1): a no-federate write and a raw write
  mint nothing — the endpoint exclusions bind BOTH sides of an edge;
* fuel policy (review M2, TL decision): organic user writes only —
  checkpoint saves and ``mint_relates_to=False`` adds never mint;
* failure isolation: a broken vector leg, embedder or edge insert NEVER
  fails the write (best-effort, logged);
* determinism (ADR-0028): the same write over the same corpus mints
  the same edges;
* minted ``relates_to`` edges are INERT for search (the walk is #324 —
  Security's "inert until I1-I3 tests exist" condition);
* no LLM / exactly one synchronous retrieval per write / no FTS rent;
* minting-rate telemetry: per-project counters through
  ``graph_mint_stats`` / ``dashboard_stats`` / the Prometheus text.

Test embedder: ``_HashEmbedder`` maps text to a deterministic hashed
bag-of-tokens vector, so cosine ≈ token overlap — near-dups measure
~0.98, tag-only sharers ~0.6, disjoint texts ~0.0. A MagicMock embedder
(cosine 1.0 for everything) cannot discriminate and would fake the
threshold semantics.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from vesmaro.config import Settings
from vesmaro.graph_minting import (
    AUTO_DEDUPE_EDGE_WEIGHT,
    AUTO_DEDUPE_PROVENANCE,
    AUTO_DEDUPE_SIMILARITY_THRESHOLD,
    select_auto_dedupe_candidates,
)
from vesmaro.manager import MemoryManager
from vesmaro.models import (
    NO_FEDERATE_TAG,
    Memory,
    MemoryCreate,
    MemorySource,
    MemoryStatus,
    PipelineState,
    SearchResult,
)

PROJECT = "mint-proj"
PROJECT_B = "mint-other"
AGENT = "mint-agent"

NEAR_DUP_A = "conveyor belt alignment procedure for the packing line station seven quality gate"
# One extra token: cosine ~0.98 against NEAR_DUP_A (measured) — mints.
NEAR_DUP_B = f"{NEAR_DUP_A} revision"
# Five extra tokens: cosine ~0.91 — related but NOT a near-duplicate.
RELATED_BELOW = f"{NEAR_DUP_A} with four appended tokens recorded tonight"
# No shared content tokens (only the tag-contract tokens): cosine ~0.6.
FAR_TEXT = "unrelated notes about coastal lighthouse maintenance schedules"


class _HashEmbedder:
    """Deterministic test embedder: hashed bag-of-tokens vectors.

    ``embed`` is a pure function of the text (md5-stable — no per-process
    salt), so embeddings are reproducible across corpora (the determinism
    test) and cosine tracks token overlap, giving the threshold real
    semantics. Mirrors the real provider contract: ``embed(text) ->
    list[float]``.
    """

    DIM = 256

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.DIM
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % self.DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def _settings(tmp: Path, *, flag: bool = False) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
            "graph_auto_mint": flag,
        },
        scanner={"enabled": False},  # type: ignore[arg-type]
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def mint_manager(tmp_path: Path) -> Iterator[MemoryManager]:
    """Flag ON — the leg under test."""
    mgr = MemoryManager(_settings(tmp_path, flag=True))
    mgr._embedder = _HashEmbedder()
    yield mgr
    mgr.close()


@pytest.fixture
def plain_manager(tmp_path: Path) -> Iterator[MemoryManager]:
    """Flag OFF (explicit) — baseline semantics; the shipped default is ON (owner 2026-09-28)."""
    mgr = MemoryManager(_settings(tmp_path, flag=False))
    mgr._embedder = _HashEmbedder()
    yield mgr
    mgr.close()


def _add(
    mgr: MemoryManager,
    content: str,
    *,
    status: MemoryStatus = MemoryStatus.PUBLISHED,
    project: str = PROJECT,
    extra_tags: list[str] | None = None,
) -> Memory:
    tags = [f"project:{project}", f"agent:{AGENT}", "mnemos:test"]
    if extra_tags:
        tags.extend(extra_tags)
    return mgr.add(
        MemoryCreate(content=content, tags=tags, source=MemorySource.MCP, status=status),
        project=project,
        agent=AGENT,
    )


def _relates_to_rows(mgr: MemoryManager) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in mgr.sqlite._get_conn()
        .execute(
            "SELECT from_memory_id, to_memory_id, weight, provenance "
            "FROM memory_edges WHERE kind = 'relates_to'"
        )
        .fetchall()
    ]


def _out_edges(mgr: MemoryManager, memory_id: str) -> list[dict[str, Any]]:
    return mgr.get_memory_edges(memory_id, kind="relates_to")


# ── Flag-off: the shipped default mints nothing ───────────────────────────────


class TestFlagOff:
    def test_default_settings_flag_on(self) -> None:
        # Owner decision 2026-09-28 «graphs on by default»: the A0
        # density gate was met (ADR-0030 addendum B.1) — minting is the
        # fuel line; the flag-off behaviour is pinned right below.
        assert Settings().vesma.graph_auto_mint is True

    def test_flag_off_mints_zero_edges(self, plain_manager: MemoryManager) -> None:
        _add(plain_manager, NEAR_DUP_A)
        _add(plain_manager, NEAR_DUP_B)
        assert _relates_to_rows(plain_manager) == []
        assert plain_manager.graph_mint_stats() == {
            "auto_dedupe_edges_total": 0,
            "auto_dedupe_edges_by_project": {},
        }

    def test_flag_off_search_unchanged(self, plain_manager: MemoryManager) -> None:
        """Search over a near-dup corpus behaves exactly as before: both
        rows surface by content, nothing is graph-sourced."""
        a = _add(plain_manager, NEAR_DUP_A)
        b = _add(plain_manager, NEAR_DUP_B)
        results = plain_manager.search("conveyor belt alignment procedure", limit=5)
        ids = {r.memory.id for r in results}
        assert {a.id, b.id} <= ids
        assert all(not r.via_graph for r in results)


# ── Happy path: the minted edge contract ──────────────────────────────────────


class TestMinting:
    def test_write_mints_edge_with_provenance_and_weight(self, mint_manager: MemoryManager) -> None:
        """The integration contract: write → edge exists with kind
        relates_to, provenance auto-dedupe and the raised weight."""
        a = _add(mint_manager, NEAR_DUP_A)
        b = _add(mint_manager, NEAR_DUP_B)

        rows = _relates_to_rows(mint_manager)
        assert len(rows) == 1
        row = rows[0]
        assert row["from_memory_id"] == b.id  # direction: new → existing
        assert row["to_memory_id"] == a.id
        assert row["provenance"] == AUTO_DEDUPE_PROVENANCE == "auto-dedupe"
        assert row["weight"] == AUTO_DEDUPE_EDGE_WEIGHT == 2.0

        # The extended read surface carries weight/provenance too.
        edges = _out_edges(mint_manager, b.id)
        assert len(edges) == 1
        assert edges[0]["to_memory_id"] == a.id
        assert edges[0]["provenance"] == "auto-dedupe"
        assert edges[0]["weight"] == 2.0

    def test_related_but_not_near_stays_below_threshold(self, mint_manager: MemoryManager) -> None:
        """The threshold does real work: a row sharing the topic and
        most tokens (cosine ~0.91) is related, NOT a near-duplicate —
        no edge."""
        _add(mint_manager, NEAR_DUP_A)
        below = _add(mint_manager, RELATED_BELOW)
        assert _relates_to_rows(mint_manager) == []  # A↔BELOW: no mint either way

        b = _add(mint_manager, NEAR_DUP_B)
        to_ids = {e["to_memory_id"] for e in _out_edges(mint_manager, b.id)}
        # B mints to A only — BELOW (cos ~0.91) and FAR never qualify.
        assert below.id not in to_ids

    def test_dissimilar_row_never_mints(self, mint_manager: MemoryManager) -> None:
        """Topically different rows (cosine ~0.6 — only the shared
        tag-contract tokens) never become endpoints."""
        far = _add(mint_manager, FAR_TEXT)
        a = _add(mint_manager, NEAR_DUP_A)
        assert _relates_to_rows(mint_manager) == []

        b = _add(mint_manager, NEAR_DUP_B)
        rows = _relates_to_rows(mint_manager)
        assert len(rows) == 1
        assert rows[0]["from_memory_id"] == b.id
        assert rows[0]["to_memory_id"] == a.id
        assert far.id not in {rows[0]["from_memory_id"], rows[0]["to_memory_id"]}

    def test_top_three_cap(self, mint_manager: MemoryManager) -> None:
        """Five exact re-adds of the same text — all five are cosine-1.0
        near-dups of the sixth write; the cap keeps the minted set at
        exactly the top-3 (ADR-0030 Decision 2: top-1..3)."""
        for _ in range(5):
            _add(mint_manager, NEAR_DUP_A)
        sixth = _add(mint_manager, NEAR_DUP_A)
        assert len(_out_edges(mint_manager, sixth.id)) == 3

    def test_minted_edges_are_inert_for_search(self, mint_manager: MemoryManager) -> None:
        """ADR-0030 clause 2 item 4: with the walk flag OFF (the shipped
        default), ``relates_to`` edges stay INERT for search. The I1-I3
        walk tests exist as of #324 (tests/test_graph_walk_invariants.py)
        and the flag-on traversal is covered there. Mutation-verified:
        the same corpus searched with and without the minted edges
        answers byte-identically (ids, scores, provenance)."""
        _add(mint_manager, NEAR_DUP_A)
        b = _add(mint_manager, NEAR_DUP_B)
        assert _relates_to_rows(mint_manager), "fixture: edges were minted"

        query = "conveyor belt alignment procedure"
        with_edges = mint_manager.search(query, limit=5)

        conn = mint_manager.sqlite._get_conn()
        conn.execute("DELETE FROM memory_edges WHERE kind = 'relates_to'")
        conn.commit()
        without_edges = mint_manager.search(query, limit=5)

        assert [(r.memory.id, r.score, r.via_graph) for r in with_edges] == [
            (r.memory.id, r.score, r.via_graph) for r in without_edges
        ]
        assert all(not r.via_graph for r in with_edges)
        assert b.id in {r.memory.id for r in with_edges}


# ── Candidate-set exclusions (I4 / §5 / intra-project / statuses) ─────────────


class TestExclusions:
    """Each fixture pairs a one-token-off twin (cosine ~0.97 — the
    similarity PASSES) with the exclusion under test, so a dropped twin
    proves the exclusion, not a weak signal."""

    def test_no_federate_candidate_excluded(self, mint_manager: MemoryManager) -> None:
        """I4 generation side: a no-federate node never becomes a minting
        endpoint."""
        nofed = _add(mint_manager, f"{NEAR_DUP_A} flagged", extra_tags=[NO_FEDERATE_TAG])
        clean = _add(mint_manager, NEAR_DUP_A)
        new = _add(mint_manager, NEAR_DUP_B)

        to_ids = {e["to_memory_id"] for e in _out_edges(mint_manager, new.id)}
        assert nofed.id not in to_ids, "I4 violated: edge minted to a no-federate node"
        assert clean.id in to_ids, "fixture sanity: the clean twin did qualify"

    def test_quarantined_candidate_excluded(self, mint_manager: MemoryManager) -> None:
        """§5 (ADR-0019) absolute: a quarantined row is never a candidate."""
        quarantined = _add(mint_manager, f"{NEAR_DUP_A} flagged")
        clean = _add(mint_manager, NEAR_DUP_A)
        mint_manager.sqlite.update_fields(
            quarantined.id,
            pipeline_state=PipelineState.QUARANTINED.value,
            quarantine_reason="test quarantine",
        )
        new = _add(mint_manager, NEAR_DUP_B)

        to_ids = {e["to_memory_id"] for e in _out_edges(mint_manager, new.id)}
        assert quarantined.id not in to_ids, "§5 violated: edge minted to quarantined row"
        assert clean.id in to_ids

    def test_cross_project_candidate_excluded(self, mint_manager: MemoryManager) -> None:
        """Intra-project only: a near-dup living in another project never
        receives an edge (and the scoped write still mints in-project)."""
        foreign = _add(mint_manager, NEAR_DUP_A, project=PROJECT_B)
        local = _add(mint_manager, NEAR_DUP_A)
        new = _add(mint_manager, NEAR_DUP_B)

        to_ids = {e["to_memory_id"] for e in _out_edges(mint_manager, new.id)}
        assert foreign.id not in to_ids, "cross-project edge minted"
        assert to_ids == {local.id}

    def test_raw_and_processing_candidates_excluded(self, mint_manager: MemoryManager) -> None:
        """General gates: raw/processing rows are not near-dup fuel."""
        raw = _add(mint_manager, f"{NEAR_DUP_A} draft", status=MemoryStatus.RAW)
        processing = _add(mint_manager, f"{NEAR_DUP_A} draft two", status=MemoryStatus.PROCESSING)
        clean = _add(mint_manager, NEAR_DUP_A)
        new = _add(mint_manager, NEAR_DUP_B)

        to_ids = {e["to_memory_id"] for e in _out_edges(mint_manager, new.id)}
        assert raw.id not in to_ids
        assert processing.id not in to_ids
        assert clean.id in to_ids

    def test_self_never_a_candidate(self, mint_manager: MemoryManager) -> None:
        """The new memory cosines 1.0 with its own query and tops the
        pool — it must never link to itself."""
        mem = _add(mint_manager, NEAR_DUP_B)
        assert all(r["from_memory_id"] != r["to_memory_id"] for r in _relates_to_rows(mint_manager))
        assert mem.id not in {r["to_memory_id"] for r in _out_edges(mint_manager, mem.id)}


# ── Pure selector: every branch, no store ─────────────────────────────────────


def _mem(
    mid: str,
    content: str = NEAR_DUP_A,
    *,
    project: str = PROJECT,
    status: MemoryStatus = MemoryStatus.PUBLISHED,
    tags: list[str] | None = None,
    pipeline_state: PipelineState | None = None,
) -> Memory:
    return Memory(
        id=mid,
        content=content,
        tags=tags if tags is not None else [f"project:{project}", "mnemos:test"],
        project=project,
        status=status,
        pipeline_state=pipeline_state,
    )


def _res(memory: Memory, score: float = 0.98, *, via_graph: bool = False) -> SearchResult:
    return SearchResult(memory=memory, score=score, search_type="semantic", via_graph=via_graph)


class TestSelectorUnit:
    def test_plain_near_dup_passes(self) -> None:
        new = _mem("new")
        cand = _res(_mem("cand"))
        assert select_auto_dedupe_candidates(new, [cand]) == [cand]

    def test_self_excluded(self) -> None:
        new = _mem("new")
        picked = select_auto_dedupe_candidates(new, [_res(new), _res(_mem("cand"))])
        assert [r.memory.id for r in picked] == ["cand"]

    def test_via_graph_excluded_even_above_threshold(self) -> None:
        new = _mem("new")
        edge_sourced = _res(_mem("cand"), score=1.0, via_graph=True)
        assert select_auto_dedupe_candidates(new, [edge_sourced]) == []

    def test_no_federate_excluded(self) -> None:
        new = _mem("new")
        nofed = _mem("nf", tags=[f"project:{PROJECT}", NO_FEDERATE_TAG])
        assert select_auto_dedupe_candidates(new, [_res(nofed)]) == []

    def test_quarantined_excluded(self) -> None:
        new = _mem("new")
        q = _mem("q", pipeline_state=PipelineState.QUARANTINED)
        assert select_auto_dedupe_candidates(new, [_res(q)]) == []

    @pytest.mark.parametrize("status", [MemoryStatus.RAW, MemoryStatus.PROCESSING])
    def test_non_admissible_status_excluded(self, status: MemoryStatus) -> None:
        new = _mem("new")
        assert select_auto_dedupe_candidates(new, [_res(_mem("s", status=status))]) == []

    def test_cross_project_excluded(self) -> None:
        new = _mem("new", project=PROJECT)
        foreign = _mem("foreign", project=PROJECT_B)
        assert select_auto_dedupe_candidates(new, [_res(foreign)]) == []

    def test_unscoped_write_links_only_unscoped_rows(self) -> None:
        """``''`` is a project: an unscoped write never links into a
        named project and vice versa."""
        new = _mem("new", project="")
        unscoped = _mem("peer", project="")
        scoped = _mem("scoped", project=PROJECT)
        picked = select_auto_dedupe_candidates(new, [_res(unscoped), _res(scoped)])
        assert [r.memory.id for r in picked] == ["peer"]

    def test_threshold_boundary(self) -> None:
        new = _mem("new")
        below = _mem("below")
        at = _mem("at")
        picked = select_auto_dedupe_candidates(
            new,
            [
                _res(below, score=AUTO_DEDUPE_SIMILARITY_THRESHOLD - 1e-9),
                _res(at, score=AUTO_DEDUPE_SIMILARITY_THRESHOLD),
            ],
        )
        assert [r.memory.id for r in picked] == ["at"]  # >= threshold, inclusive

    def test_top_three_cap_and_id_tiebreak(self) -> None:
        """Five equal-score candidates: the cap keeps 3 and the ADR-0028
        id tiebreak (score desc, id asc) picks them independent of the
        input order — the store's tie order never leaks."""
        new = _mem("new")
        shuffled = [_res(_mem(f"c{i}"), score=0.95) for i in (3, 0, 4, 1, 2)]
        picked = select_auto_dedupe_candidates(new, shuffled)
        assert [r.memory.id for r in picked] == ["c0", "c1", "c2"]

    def test_score_order_desc(self) -> None:
        new = _mem("new")
        picked = select_auto_dedupe_candidates(
            new, [_res(_mem("low"), score=0.93), _res(_mem("high"), score=0.99)]
        )
        assert [r.memory.id for r in picked] == ["high", "low"]


# ── Idempotency per (from, to, provenance) ────────────────────────────────────


class TestIdempotency:
    def test_repeated_mint_inserts_nothing(self, mint_manager: MemoryManager) -> None:
        _add(mint_manager, NEAR_DUP_A)
        b = _add(mint_manager, NEAR_DUP_B)
        assert len(_relates_to_rows(mint_manager)) == 1

        reloaded = mint_manager.sqlite.get(b.id)
        assert reloaded is not None
        assert mint_manager._mint_relates_to_edges(reloaded) == 0
        assert len(_relates_to_rows(mint_manager)) == 1
        # The idempotent re-mint counts nothing.
        assert mint_manager.graph_mint_stats()["auto_dedupe_edges_total"] == 1

    def test_readding_same_content_no_duplicate_edges(self, mint_manager: MemoryManager) -> None:
        """A re-add is a NEW memory: it mints its own edge to the old
        duplicate (a new (from, to) pair — correct fuel, not a
        duplication); the old row's edge set is untouched."""
        a = _add(mint_manager, NEAR_DUP_A)
        b = _add(mint_manager, NEAR_DUP_A)  # identical content

        rows = _relates_to_rows(mint_manager)
        assert [(r["from_memory_id"], r["to_memory_id"]) for r in rows] == [(b.id, a.id)]
        assert _out_edges(mint_manager, a.id) == []

    def test_declared_edge_survives_mint_collision(self, mint_manager: MemoryManager) -> None:
        """The ``(from, to, kind)`` PK: when the pair already carries a
        DECLARED relates_to edge, the minted insert is an INSERT OR
        IGNORE no-op — one row, original provenance and weight kept, and
        the idempotent mint counts nothing."""
        x = _add(mint_manager, NEAR_DUP_A)
        y = _add(mint_manager, NEAR_DUP_A)  # y minted y→x at add time
        assert mint_manager.add_memory_edge(
            x.id, y.id, kind="relates_to", weight=1.0, provenance="declared"
        )

        stats_before = mint_manager.graph_mint_stats()["auto_dedupe_edges_total"]
        reloaded = mint_manager.sqlite.get(x.id)
        assert reloaded is not None
        assert mint_manager._mint_relates_to_edges(reloaded) == 0  # x→y collides

        row = next(
            r
            for r in _relates_to_rows(mint_manager)
            if (r["from_memory_id"], r["to_memory_id"]) == (x.id, y.id)
        )
        assert row["provenance"] == "declared"
        assert row["weight"] == 1.0
        assert len(_relates_to_rows(mint_manager)) == 2  # y→x minted + x→y declared
        assert mint_manager.graph_mint_stats()["auto_dedupe_edges_total"] == stats_before


# ── Failure isolation: minting NEVER fails the write ──────────────────────────


class TestFailureIsolation:
    def test_vector_leg_failure_never_fails_write(
        self, mint_manager: MemoryManager, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        def boom(*args: object, **kwargs: object) -> list[tuple[str, float]]:
            raise RuntimeError("vector leg down")

        monkeypatch.setattr(mint_manager.vectors, "search", boom)
        with caplog.at_level(logging.WARNING):
            mem = _add(mint_manager, NEAR_DUP_B)
        assert mint_manager.sqlite.get(mem.id) is not None, "the write MUST survive"
        assert _out_edges(mint_manager, mem.id) == []
        assert any("graph auto-mint failed" in r.getMessage() for r in caplog.records)

    def test_embedder_failure_never_fails_write(
        self, mint_manager: MemoryManager, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        _add(mint_manager, NEAR_DUP_A)

        def boom(text: str) -> list[float]:
            raise RuntimeError("embedder down")

        monkeypatch.setattr(mint_manager.embedder, "embed", boom)
        with caplog.at_level(logging.WARNING):
            mem = _add(mint_manager, NEAR_DUP_B)
        assert mint_manager.sqlite.get(mem.id) is not None
        assert _relates_to_rows(mint_manager) == []
        assert any("graph auto-mint failed" in r.getMessage() for r in caplog.records)

    def test_edge_insert_failure_never_fails_write(
        self, mint_manager: MemoryManager, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        _add(mint_manager, NEAR_DUP_A)

        def boom(self: MemoryManager, *args: object, **kwargs: object) -> bool:
            raise sqlite3.OperationalError("edges table locked")

        monkeypatch.setattr(MemoryManager, "add_memory_edge", boom)
        with caplog.at_level(logging.WARNING):
            mem = _add(mint_manager, NEAR_DUP_B)
        assert mint_manager.sqlite.get(mem.id) is not None, "the write MUST survive"
        assert any("edge insert failed" in r.getMessage() for r in caplog.records)

    def test_minting_does_not_pollute_search_stats(self, mint_manager: MemoryManager) -> None:
        """Minting rides the store legs directly: the public search
        counters stay a pure function of caller-visible searches."""
        _add(mint_manager, NEAR_DUP_A)
        _add(mint_manager, NEAR_DUP_B)
        stats = mint_manager.search_stats()
        assert stats["requests_total"] == 0
        assert stats["cross_project_requests_total"] == 0
        assert stats["project_scope_fallback_total"] == 0


# ── Contract: one synchronous retrieval, no FTS rent, no LLM ──────────────────


class TestMintingContract:
    def test_exactly_one_vector_search_no_fts_rent_per_write(
        self, mint_manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _add(mint_manager, NEAR_DUP_A)
        vector_calls: list[object] = []
        fts_calls: list[object] = []
        real_search = mint_manager.vectors.search
        real_fts = mint_manager.sqlite.fts_search

        def counting_search(*args: object, **kwargs: object) -> list[tuple[str, float]]:
            vector_calls.append(args)
            return real_search(*args, **kwargs)  # type: ignore[arg-type]

        def counting_fts(*args: object, **kwargs: object) -> list[tuple[Memory, float]]:
            fts_calls.append(args)
            return real_fts(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(mint_manager.vectors, "search", counting_search)
        monkeypatch.setattr(mint_manager.sqlite, "fts_search", counting_fts)
        _add(mint_manager, NEAR_DUP_B)
        assert len(vector_calls) == 1, "exactly ONE synchronous retrieval per write"
        assert len(fts_calls) == 0, "the minting leg pays no FTS rent"

    def test_no_llm_surface_on_minting_path(
        self, mint_manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dynamic check: minting loads no pipeline/LLM module, and the
        minting module itself imports only the models layer."""
        import sys

        import vesmaro.graph_minting as gm

        _add(mint_manager, NEAR_DUP_A)
        target = mint_manager.sqlite.get(_add(mint_manager, NEAR_DUP_B).id)

        before = set(sys.modules)
        assert mint_manager._mint_relates_to_edges(target) >= 0
        loaded = set(sys.modules) - before
        assert not any(m.startswith("vesmaro.pipeline") for m in loaded)
        assert not any("llm" in m.lower() for m in loaded)

        # Structural: the minting module's own imports touch no LLM surface.
        module_imports = [
            line
            for line in Path(gm.__file__).read_text().splitlines()
            if line.startswith(("import ", "from "))
        ]
        assert not any("pipeline" in line or "llm" in line.lower() for line in module_imports)


# ── Determinism (ADR-0028) ────────────────────────────────────────────────────


class TestDeterminism:
    def test_same_write_same_corpus_same_edges(self, tmp_path: Path) -> None:
        """Two identical corpora (fresh stores, same contents, same
        order): the minted edge sets match by content pair — the write
        is a pure function of the corpus."""
        import itertools

        counter = itertools.count()

        def build() -> list[tuple[str, str]]:
            root = tmp_path / f"corpus{next(counter)}"
            mgr = MemoryManager(_settings(root, flag=True))
            mgr._embedder = _HashEmbedder()
            contents: dict[str, str] = {}
            for text in (NEAR_DUP_A, NEAR_DUP_B, f"{NEAR_DUP_B} again"):
                contents[_add(mgr, text).id] = text
            pairs = sorted(
                (contents[str(r["from_memory_id"])], contents[str(r["to_memory_id"])])
                for r in _relates_to_rows(mgr)
            )
            mgr.close()
            return pairs

        first = build()
        second = build()
        assert first, "fixture: something was minted"
        assert first == second


# ── From-side gates (review M1): endpoint exclusions bind BOTH sides ─────────


class TestFromSideGates:
    """Review M1: the selector validates CANDIDATES; the hook validates
    the NEW memory from-side. A no-federate row (including one the
    write-path scanner just auto-tagged) and a non-admissible row
    (raw/processing/quarantined) mint NOTHING — invisible or
    non-exportable content gets no permanent graph edges."""

    def test_no_federate_new_memory_mints_nothing(self, mint_manager: MemoryManager) -> None:
        """The twins pattern: the tagged write sits at ~0.98 cosine to
        the clean twin (the happy-path pair proves that cosine mints) —
        only the from-side gate can be what drops it."""
        twin = _add(mint_manager, NEAR_DUP_A)
        tagged = _add(mint_manager, NEAR_DUP_B, extra_tags=[NO_FEDERATE_TAG])
        assert _relates_to_rows(mint_manager) == [], "a no-federate write minted FROM itself"

        # Fixture sanity: the twin IS mintable-to — a later clean write
        # links it, and the tagged row is never an endpoint on either side.
        third = _add(mint_manager, f"{NEAR_DUP_B} again")
        to_ids = {e["to_memory_id"] for e in _out_edges(mint_manager, third.id)}
        assert twin.id in to_ids
        assert tagged.id not in to_ids

    def test_raw_new_memory_mints_nothing(self, mint_manager: MemoryManager) -> None:
        """RAW writes (publish-gate refusals, explicit raw) are invisible
        content — no permanent edges from them."""
        a = _add(mint_manager, NEAR_DUP_A)
        raw_write = _add(mint_manager, NEAR_DUP_B, status=MemoryStatus.RAW)
        assert _relates_to_rows(mint_manager) == []

        # Sanity: minting is alive — a published near-dup links only `a`
        # (the raw row stays excluded on the candidate side as well).
        third = _add(mint_manager, f"{NEAR_DUP_B} again")
        assert {e["to_memory_id"] for e in _out_edges(mint_manager, third.id)} == {a.id}
        assert raw_write.id not in {r["from_memory_id"] for r in _relates_to_rows(mint_manager)}


# ── Fuel policy (review M2): organic user writes only ────────────────────────


class TestFuelPolicy:
    """Review M2 (TL decision): minting fuel is ORGANIC USER WRITES
    only — internal machine-driven ``add`` callers pass
    ``mint_relates_to=False`` (checkpoint saves, mesh ingest, migrate
    import); their mechanical self-citation is infra churn, not
    near-duplicate signal."""

    def test_checkpoint_save_mints_nothing(self, mint_manager: MemoryManager) -> None:
        """Two near-identical checkpoints (same project, same agent,
        one edited field) are high-cosine same-project rows — exactly
        what the minting leg would link if checkpoints were fuel. The
        M2 gate keeps them out of the graph."""
        base = {
            "goals": f"stabilise the {NEAR_DUP_A}",
            "completed": "aligned the packing line gate",
            "decisions": "keep the conveyor revision",
            "context": "station seven quality gate",
        }
        cp1, _ = mint_manager.save_checkpoint(base, project=PROJECT, agent=AGENT)
        near = dict(base)
        near["goals"] = f"stabilise the {NEAR_DUP_A} now"
        cp2, dup = mint_manager.save_checkpoint(near, project=PROJECT, agent=AGENT)
        assert dup is False, "fixture: the second checkpoint is a NEW write"
        assert cp2.id != cp1.id
        assert _relates_to_rows(mint_manager) == [], "checkpoint saves must never mint"

    def test_mint_relates_to_false_skips_the_hook(self, mint_manager: MemoryManager) -> None:
        """The parameter contract: an explicit ``mint_relates_to=False``
        add mints nothing even though the corpus holds a 0.98-cosine
        twin (the mesh/migrate call sites ride this)."""
        _add(mint_manager, NEAR_DUP_A)
        data = MemoryCreate(
            content=NEAR_DUP_B,
            tags=[f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:test"],
            source=MemorySource.MCP,
            status=MemoryStatus.PUBLISHED,
        )
        b = mint_manager.add(data, project=PROJECT, agent=AGENT, mint_relates_to=False)
        assert _out_edges(mint_manager, b.id) == []
        assert _relates_to_rows(mint_manager) == []


# ── Reverse-pair guard (review L1): minted pairs are undirected ───────────────


class TestReversePairGuard:
    def test_backfill_remin_of_older_end_mints_no_reverse(
        self, mint_manager: MemoryManager
    ) -> None:
        """The PK treats A→B and B→A as distinct rows; the minting rule
        holds the PAIR undirected — a re-mint of the older end (backfill
        tools, A0-review re-runs) must not duplicate it in reverse."""
        a = _add(mint_manager, NEAR_DUP_A)
        b = _add(mint_manager, NEAR_DUP_B)  # mints b→a
        rows = _relates_to_rows(mint_manager)
        assert [(r["from_memory_id"], r["to_memory_id"]) for r in rows] == [(b.id, a.id)]

        reloaded = mint_manager.sqlite.get(a.id)
        assert reloaded is not None
        assert mint_manager._mint_relates_to_edges(reloaded) == 0  # a→b blocked
        assert len(_relates_to_rows(mint_manager)) == 1  # no reverse duplicate

    def test_declared_reverse_edge_still_insertable(self, mint_manager: MemoryManager) -> None:
        """The guard is mint-specific: a DECLARED reverse edge stays
        legal — direction can be semantic for explicit callers."""
        _add(mint_manager, NEAR_DUP_A)
        b = _add(mint_manager, NEAR_DUP_B)  # mints b→a (auto-dedupe)
        a_id = _relates_to_rows(mint_manager)[0]["to_memory_id"]
        assert (
            mint_manager.add_memory_edge(a_id, b.id, kind="relates_to", provenance="declared")
            is True
        )


# ── Flag-off spy (review INFO): the early-return fires BEFORE retrieval ──────


class TestFlagOffSpy:
    def test_flag_off_never_touches_the_vector_leg(
        self, plain_manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pins the early-return placement: with the flag off, a write
        pays ZERO minting rent — no vector search, and exactly one
        embed per published row (the upsert itself)."""
        searches: list[object] = []
        embeds: list[str] = []
        real_search = plain_manager.vectors.search
        real_embed = plain_manager.embedder.embed

        def spy_search(*args: object, **kwargs: object) -> list[tuple[str, float]]:
            searches.append(args)
            return real_search(*args, **kwargs)  # type: ignore[arg-type]

        def spy_embed(text: str) -> list[float]:
            embeds.append(text)
            return real_embed(text)

        monkeypatch.setattr(plain_manager.vectors, "search", spy_search)
        monkeypatch.setattr(plain_manager.embedder, "embed", spy_embed)
        _add(plain_manager, NEAR_DUP_A)
        _add(plain_manager, NEAR_DUP_B)
        assert searches == [], "flag off: the mint hook must return before any retrieval"
        assert len(embeds) == 2, "one embed per published add (the upsert) — no minting rent"
        assert _relates_to_rows(plain_manager) == []


# ── Telemetry: the A0-review minting-rate surface ─────────────────────────────


class TestTelemetry:
    def test_graph_mint_stats_per_project(self, tmp_path: Path) -> None:
        mgr = MemoryManager(_settings(tmp_path, flag=True))
        mgr._embedder = _HashEmbedder()
        try:
            _add(mgr, NEAR_DUP_A, project=PROJECT)
            _add(mgr, NEAR_DUP_B, project=PROJECT)
            _add(mgr, NEAR_DUP_A, project=PROJECT_B)
            _add(mgr, NEAR_DUP_B, project=PROJECT_B)

            stats = mgr.graph_mint_stats()
            assert stats["auto_dedupe_edges_total"] == 2
            assert stats["auto_dedupe_edges_by_project"] == {PROJECT: 1, PROJECT_B: 1}
        finally:
            mgr.close()

    def test_unscoped_writes_bucket_under_empty_project(self, mint_manager: MemoryManager) -> None:
        _add(mint_manager, NEAR_DUP_A, project="")
        _add(mint_manager, NEAR_DUP_B, project="")
        stats = mint_manager.graph_mint_stats()
        assert stats["auto_dedupe_edges_by_project"] == {"": 1}

    def test_dashboard_carries_graph_section(self, mint_manager: MemoryManager) -> None:
        _add(mint_manager, NEAR_DUP_A)
        _add(mint_manager, NEAR_DUP_B)
        graph = mint_manager.dashboard_stats()["graph"]
        assert graph["auto_mint_enabled"] is True
        assert graph["auto_dedupe_edges_total"] == 1
        assert graph["auto_dedupe_edges_by_project"] == {PROJECT: 1}

    def test_dashboard_graph_section_flag_off(self, plain_manager: MemoryManager) -> None:
        graph = plain_manager.dashboard_stats()["graph"]
        assert graph["auto_mint_enabled"] is False
        assert graph["auto_dedupe_edges_total"] == 0

    def test_prometheus_carries_minting_counter(self, mint_manager: MemoryManager) -> None:
        from vesmaro.api.main import _prometheus_text

        _add(mint_manager, NEAR_DUP_A)
        _add(mint_manager, NEAR_DUP_B)
        text = _prometheus_text(mint_manager)
        assert "# HELP mnemos_graph_auto_dedupe_edges_total" in text
        assert "# TYPE mnemos_graph_auto_dedupe_edges_total counter" in text
        assert "mnemos_graph_auto_dedupe_edges_total 1" in text
        assert f'mnemos_graph_auto_dedupe_edges_by_project{{project="{PROJECT}"}} 1' in text
