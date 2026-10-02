"""Cache contract Phase-1 (#280) — deterministic tiebreak in ``MemoryManager.search``.

The final RRF ordering in ``MemoryManager.search`` is ``(score desc, id
asc)``. Before Phase-1 the sort was ``reverse=True`` over the ``scores``
dict — stable over dict insertion order, which tracks the FTS/vector leg
iteration order; for EQUAL fused scores that order can depend on the
vector index state (the store's top-k tie behaviour), leaking
non-determinism into the retrieval boundary that feeds
``assemble_context`` (the KV-cache byte-stability surface of #305).

Test strategy: the equal-score construction needs rank control that the
real stores cannot provide (RRF contributions are per-rank, so two ids
tie only when their rank profiles sum to the same value — e.g. FTS rank
1 vs vector rank 1 under ``hybrid_alpha=0.5``). The fusion-boundary
tests therefore stub the two store-level search calls on the manager
instance (``fts_search`` / ``vectors.search``) with controlled pairs —
the REAL fusion, final sort and ``SearchResult`` construction code in
``MemoryManager.search`` runs unmodified. One store-level companion test
(distinct deterministic embeddings, shuffled insertion order) guards the
general "output is a pure function of content, not insertion order"
property end-to-end through the real stores.

Mutation check (verified by hand, not automated): reverting the sort key
to ``key=lambda kv: kv[1], reverse=True`` makes
``test_equal_fused_scores_order_by_id`` and
``test_shuffled_arrival_identical_output`` fail — the tests bite.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesma.assemble import assemble_context
from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import Memory, MemoryCreate, MemorySource, MemoryStatus

PROJECT = "determinism-proj"
AGENT = "det-agent"


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
def manager() -> Iterator[MemoryManager]:
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = MemoryManager(_settings(Path(tmpdir)))
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [0.1] * 384
        mgr._embedder = mock_embedder
        yield mgr
        mgr.close()


def _add(mgr: MemoryManager, content: str) -> Memory:
    data = MemoryCreate(
        content=content,
        tags=[f"project:{PROJECT}", f"agent:{AGENT}", "mnemos:learning"],
        source=MemorySource.MCP,
        status=MemoryStatus.PUBLISHED,
    )
    return mgr.add(data, project=PROJECT, agent=AGENT)


def _stub_legs(
    monkeypatch: pytest.MonkeyPatch,
    mgr: MemoryManager,
    fts_memories: list[Memory],
    vector_pairs: list[tuple[str, float]],
) -> None:
    """Replace the two store-level search calls with controlled pairs.

    ``MemoryManager.search`` runs its real fusion / final-sort /
    SearchResult code over these; only the store reads are fixed. The
    list ORDER inside each leg is the leg's rank order (rank 1 first).
    """
    monkeypatch.setattr(
        mgr.sqlite, "fts_search", lambda *args, **kwargs: [(m, 0.9) for m in fts_memories]
    )
    monkeypatch.setattr(mgr.vectors, "search", lambda *args, **kwargs: list(vector_pairs))


# ── Fusion-boundary determinism (equal fused scores) ──────────────────────────


class TestSearchTiebreak:
    def test_equal_fused_scores_order_by_id(self, manager: MemoryManager, monkeypatch) -> None:
        """(a) Two records with the same fused score → id-ascending order.

        FTS rank 1 carries the lexicographically GREATER id, vector rank 1
        the lower one; ``hybrid_alpha=0.5`` makes both contributions
        0.5/(60+1) — an exact tie. The tie group must come out id-sorted,
        not insertion-sorted (pre-fix: [greater, lower]).
        """
        m1 = _add(manager, "determinism alpha payload about quartz shards")
        m2 = _add(manager, "determinism beta payload about feldspar seams")
        by_id = {m.id: m for m in (m1, m2)}
        lo, hi = sorted(by_id)
        _stub_legs(monkeypatch, manager, [by_id[hi]], [(lo, 0.9)])

        results = manager.search("determinism", project=PROJECT, hybrid_alpha=0.5)

        assert [r.memory.id for r in results] == [lo, hi]
        assert results[0].score == results[1].score

    def test_shuffled_arrival_identical_output(self, manager: MemoryManager, monkeypatch) -> None:
        """(b) Same records, shuffled arrival order into the fusion → identical output.

        Two configurations over the SAME four records with the SAME per-id
        fused scores, but the leg memberships swapped (everything the FTS
        leg carried in config A arrives via the vector leg in config B and
        vice versa). The arrival order into the ``scores`` dict differs;
        the output must not. Pre-fix the two configs produce different
        orders (pure insertion order); post-fix both are
        (score desc, id asc).
        """
        records = [_add(manager, f"determinism shard payload {i}") for i in range(4)]
        by_id = {m.id: m for m in records}
        ids = sorted(by_id)
        # Two tie groups of two: group Hi (FTS rank 1 + vector rank 1 →
        # 0.5/61 each) and group Lo (rank 2 + rank 2 → 0.5/62 each).
        hi_fts, hi_vec = by_id[ids[3]], ids[0]  # id order inside group: vec < fts
        lo_fts, lo_vec = by_id[ids[2]], ids[1]

        _stub_legs(monkeypatch, manager, [hi_fts, lo_fts], [(hi_vec, 0.9), (lo_vec, 0.8)])
        out_a = manager.search("determinism", project=PROJECT, hybrid_alpha=0.5)

        _stub_legs(
            monkeypatch,
            manager,
            [by_id[hi_vec], by_id[lo_vec]],
            [(hi_fts.id, 0.9), (lo_fts.id, 0.8)],
        )
        out_b = manager.search("determinism", project=PROJECT, hybrid_alpha=0.5)

        expected = [ids[0], ids[3], ids[1], ids[2]]  # Hi group id-sorted, then Lo
        assert [r.memory.id for r in out_a] == expected
        assert [r.memory.id for r in out_b] == expected
        assert [r.score for r in out_a] == [r.score for r in out_b]

    def test_distinct_scores_keep_score_order(self, manager: MemoryManager, monkeypatch) -> None:
        """Contract guard: the tiebreak must NOT override score order.

        The lexicographically greater id gets the strictly higher score
        (FTS rank 1 vs rank 2, no vector hits) — it must come first even
        though a naive id-sort would invert the pair. Order between
        DIFFERENT scores is unchanged by Phase-1.
        """
        m1 = _add(manager, "determinism gamma payload about mica films")
        m2 = _add(manager, "determinism delta payload about halite cubes")
        by_id = {m.id: m for m in (m1, m2)}
        lo, hi = sorted(by_id)
        _stub_legs(monkeypatch, manager, [by_id[hi], by_id[lo]], [])

        results = manager.search("determinism", project=PROJECT, hybrid_alpha=0.5)

        assert [r.memory.id for r in results] == [hi, lo]
        assert results[0].score > results[1].score

    def test_assemble_block_order_follows_search_tiebreak(
        self, manager: MemoryManager, monkeypatch
    ) -> None:
        """End-to-end at the consumer boundary: equal-score blocks are id-ordered.

        ``assemble_context`` is the retrieval contract's consumer; its
        budget stage runs only STABLE sorts over the recall order, so the
        assembled block order must inherit the search tiebreak verbatim.
        """
        m1 = _add(manager, "determinism epsilon payload about beryl columns")
        m2 = _add(manager, "determinism zeta payload about opal veins")
        by_id = {m.id: m for m in (m1, m2)}
        lo, hi = sorted(by_id)
        _stub_legs(monkeypatch, manager, [by_id[hi]], [(lo, 0.9)])

        result = assemble_context(
            manager,
            session="det-session",
            project=PROJECT,
            query="determinism",
            budget=2048,
        )

        block_ids = [b["memory_id"] for b in result["blocks"]]
        assert block_ids == [lo, hi]


# ── Store-level companion (real FTS + real vector store) ──────────────────────


def _stable_embed(text: str) -> list[float]:
    """Deterministic content-addressed embedding — no model, no network.

    Distinct texts map to distinct vectors, so both legs' rankings are
    pure functions of the content set (never of insertion order).
    """
    digest = hashlib.sha256(text.encode()).digest()
    return [b / 255.0 for b in digest]


class TestStoreLevelInsertionInvariance:
    def test_shuffled_insertion_identical_output(self) -> None:
        """(b, store level) Same record set inserted in shuffled order → identical search output.

        Both legs rank content-deterministically here (strictly distinct
        bm25 term counts, strictly distinct embeddings), so the fused
        output — ids AND scores — must be identical across the two
        insertion permutations of the same store contents.
        """
        contents = [
            " ".join(["determinism"] * (i + 1)) + f" shard-{i} unique payload" for i in range(6)
        ]

        def _run(insertion_order: list[str]) -> tuple[list[str], list[float]]:
            with tempfile.TemporaryDirectory() as tmpdir:
                mgr = MemoryManager(_settings(Path(tmpdir)))
                embedder = MagicMock()
                embedder.embed.side_effect = _stable_embed
                mgr._embedder = embedder
                try:
                    for content in insertion_order:
                        _add(mgr, content)
                    results = mgr.search("determinism", project=PROJECT)
                    # Fresh store per run → fresh uuids; the record identity
                    # for the comparison is the (unique) content.
                    return (
                        [r.memory.content for r in results],
                        [round(r.score, 12) for r in results],
                    )
                finally:
                    mgr.close()

        forward = _run(list(contents))
        shuffled = _run(
            [contents[3], contents[0], contents[5], contents[1], contents[4], contents[2]]
        )

        assert len(forward[0]) == len(contents)
        assert forward == shuffled
