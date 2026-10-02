"""Coverage tests for `storage/vector_store.py` (M18).

The vector store is simple — SQLite + numpy cosine. These tests cover the
search / has / count / get_embeddings / batch_upsert / delete paths that
weren't exercised by integration tests.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from vesma.storage.vector_store import VectorStore


@pytest.fixture
def data_dir() -> Path:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def vs(data_dir: Path) -> VectorStore:
    store = VectorStore(data_dir)
    yield store
    store.close()


class TestVectorStore:
    def test_empty_store_count_is_zero(self, vs: VectorStore) -> None:
        """A fresh store has no embeddings."""
        assert vs.count() == 0

    def test_upsert_and_has(self, vs: VectorStore) -> None:
        """After upsert, count increments and has() returns True."""
        emb = [0.1] * 16
        vs.upsert("m1", emb)
        assert vs.count() == 1
        assert vs.has("m1") is True
        assert vs.has("missing") is False

    def test_upsert_replaces_existing(self, vs: VectorStore) -> None:
        """INSERT OR REPLACE: a second upsert with the same id replaces the row."""
        vs.upsert("m1", [0.1] * 16)
        vs.upsert("m1", [0.2] * 16)
        assert vs.count() == 1
        # Metadata is also replaced (passing {} the second time wipes it)
        out = vs.get_embeddings(["m1"])
        assert out["m1"] == pytest.approx([0.2] * 16)

    def test_delete_removes_embedding(self, vs: VectorStore) -> None:
        """delete() removes the row; subsequent has() is False."""
        vs.upsert("m1", [0.1] * 16)
        vs.delete("m1")
        assert vs.has("m1") is False
        assert vs.count() == 0

    def test_search_empty_store(self, vs: VectorStore) -> None:
        """search() on an empty store returns an empty list (no rows)."""
        result = vs.search([0.1] * 16)
        assert result == []

    def test_search_zero_norm_query(self, vs: VectorStore) -> None:
        """A zero-vector query short-circuits to an empty result."""
        vs.upsert("m1", [0.1] * 16)
        result = vs.search([0.0] * 16)
        assert result == []

    def test_search_returns_sorted_by_similarity(self, vs: VectorStore) -> None:
        """search() returns (id, score) tuples sorted by descending score."""
        # Two unit-ish vectors; identical dir => high score, opposite => low.
        vs.upsert("same", [1.0, 0.0, 0.0, 0.0])
        vs.upsert("opp", [-1.0, 0.0, 0.0, 0.0])
        result = vs.search([1.0, 0.0, 0.0, 0.0], limit=2)
        assert len(result) == 2
        # Top hit should be "same" with score ≈ 1.0
        top_id, top_score = result[0]
        assert top_id == "same"
        assert top_score == pytest.approx(1.0, abs=1e-6)
        # Second hit "opp" with score ≈ -1.0
        assert result[1][0] == "opp"
        assert result[1][1] == pytest.approx(-1.0, abs=1e-6)

    def test_get_embeddings_empty_input(self, vs: VectorStore) -> None:
        """get_embeddings([]) short-circuits to {} without hitting the DB."""
        assert vs.get_embeddings([]) == {}

    def test_get_embeddings_returns_vectors(self, vs: VectorStore) -> None:
        """get_embeddings returns the unpacked float vectors per id."""
        vs.upsert("a", [0.5, 0.5])
        vs.upsert("b", [1.0, 0.0])
        out = vs.get_embeddings(["a", "b", "missing"])
        assert set(out.keys()) == {"a", "b"}
        assert out["a"] == pytest.approx([0.5, 0.5])
        assert out["b"] == pytest.approx([1.0, 0.0])

    def test_batch_upsert(self, vs: VectorStore) -> None:
        """batch_upsert inserts all (id, vector) pairs."""
        ids = ["a", "b", "c"]
        vecs = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
        vs.batch_upsert(ids, vecs)
        assert vs.count() == 3
        for i, vid in enumerate(ids):
            out = vs.get_embeddings([vid])
            assert out[vid] == pytest.approx(vecs[i])

    def test_pack_unpack_roundtrip(self) -> None:
        """_pack and _unpack preserve float32 round-trip semantics."""
        blob = VectorStore._pack([0.1, 0.2, 0.3])
        arr = VectorStore._unpack(blob)
        assert arr.dtype == np.float32
        assert arr.tolist() == pytest.approx([0.1, 0.2, 0.3])
