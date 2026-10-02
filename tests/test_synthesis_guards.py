"""Tests for issue #250 — P0 collapse/synthesis pipeline guards.

Covers the four acceptance criteria plus the review-repair findings
(P1-1 quarantine at the synthesis read, P1-2 drafts-never-feed-synthesis,
P2 supersession of stale drafts):
  - F1: quarantined RAW rows never enter clusters (and cannot silently
    satisfy min_cluster_size)
  - F1b/P1-1: a member quarantined AFTER clustering never feeds a draft
  - F2: applyTo:/severity: tags are stripped from the synthesized record
    (strip-by-default, extends #248)
  - F2b: mnemos:no-federate propagates by ANY-member rule
  - F3: the synthesis idempotency key includes an input_set_hash over
    (id, content) of all members — a same-ID content swap (ADR-0019
    §Swap semantics) is a cache miss, not a stale cache hit
  - P1-2/P2: the fresh draft consumes source members only, and the prior
    draft is superseded (archived), never re-fed
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import MemoryCreate, MemoryStatus, MemoryUpdate, is_context_admissible
from vesma.pipeline.cluster import cluster_raw_memories
from vesma.pipeline.synthesize import synthesize_cluster

# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/test_pipeline.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings():
    """Yield a Settings object backed by a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            vesma={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
        )
        settings.resolve_paths()
        yield settings


@pytest.fixture
def tmp_manager(tmp_settings):
    """Yield a MemoryManager with isolated storage and mocked embedder."""
    mgr = MemoryManager(tmp_settings)
    # Mock embedder: deterministic 384-dim embeddings based on content hash
    mock_embedder = MagicMock()

    def _fake_embed(text: str) -> list[float]:
        # Deterministic float vector from text hash — stable across runs
        h = int(hashlib.sha256(text.encode()).hexdigest(), 16) % (2**31)
        rng = np.random.default_rng(seed=h)
        vec = rng.random(384).astype(np.float32)
        # Normalise
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.tolist()

    mock_embedder.embed.side_effect = _fake_embed
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add_raw(
    mgr: MemoryManager,
    content: str,
    extra_tags: list[str] | None = None,
    agent: str = "reviewer",
    project: str = "mnemos",
):
    """Add a raw memory with optional extra tags (e.g. applyTo:, no-federate).

    Status is EXPLICIT (see tests/test_pipeline.py::_add_raw): these tests
    exercise the RAW→PROCESSING legacy pipeline flow.
    """
    data = MemoryCreate(
        content=content,
        tags=[f"project:{project}", f"agent:{agent}", "mnemos:learning", *(extra_tags or [])],
        status=MemoryStatus.RAW,
    )
    return mgr.add(data, project=project, agent=agent)


# ---------------------------------------------------------------------------
# F1 — quarantined rows never enter clusters
# ---------------------------------------------------------------------------


class TestQuarantineIntakeGuard:
    def test_quarantined_raw_memory_excluded_from_clusters(self, tmp_manager):
        """A quarantined RAW memory does not appear in any cluster result
        even when it is highly similar to other RAW rows."""
        mgr = tmp_manager
        m1 = _add_raw(mgr, "SQL injection in auth module via user input")
        m2 = _add_raw(mgr, "SQL injection vulnerability found in authentication")
        mq = _add_raw(mgr, "SQL injection attack pattern in authentication layer")
        assert mgr.quarantine_entry(mq.id, reason="test-danger", source="test")

        clusters = cluster_raw_memories(mgr, similarity_threshold=0.5, min_cluster_size=2)

        # The two clean rows still cluster together...
        clustered_ids = {mid for c in clusters for mid in c.memory_ids}
        assert m1.id in clustered_ids
        assert m2.id in clustered_ids
        # ...and the quarantined row is in NO cluster.
        assert mq.id not in clustered_ids
        # It stays untouched by the collapse intake (zero-loss).
        reloaded = mgr.sqlite.get(mq.id)
        assert reloaded is not None
        assert reloaded.status == MemoryStatus.RAW
        assert reloaded.cluster_id is None

    def test_quarantined_row_cannot_satisfy_min_cluster_size(self, tmp_manager):
        """One clean RAW row + one quarantined RAW row → no cluster: the
        filter runs BEFORE the min_cluster_size check, so an excluded row
        cannot silently satisfy the minimum."""
        mgr = tmp_manager
        m1 = _add_raw(mgr, "note one")
        mq = _add_raw(mgr, "note two")
        assert mgr.quarantine_entry(mq.id, reason="test-danger", source="test")

        clusters = cluster_raw_memories(mgr, similarity_threshold=0.5, min_cluster_size=2)
        assert clusters == []

        # The clean row was not swept into processing either.
        reloaded = mgr.sqlite.get(m1.id)
        assert reloaded is not None
        assert reloaded.status == MemoryStatus.RAW


# ---------------------------------------------------------------------------
# P1-1 — quarantined members never feed a synthesis (post-clustering gap)
# ---------------------------------------------------------------------------


class TestQuarantineSynthesisGuard:
    def test_quarantined_member_excluded_from_synthesis(self, tmp_manager):
        """A member quarantined in the window between clustering and the
        synthesis tick never feeds the draft — its content must not leak
        into a PROCESSED, context-admissible record."""
        mgr = tmp_manager
        m1 = _add_raw(mgr, "ordinary cluster note one")
        m2 = _add_raw(mgr, "ordinary cluster note two with secret payload x9k")
        clusters = cluster_raw_memories(mgr, similarity_threshold=0.5, min_cluster_size=2)
        assert len(clusters) == 1
        cluster_id = clusters[0].cluster_id

        assert mgr.quarantine_entry(m2.id, reason="secret-payload", source="test")

        result = synthesize_cluster(mgr, cluster_id)
        assert result is not None
        draft = mgr.sqlite.get(result.draft_id)
        assert draft is not None
        # The draft is born admissible, so the quarantined payload must
        # be absent from it.
        assert is_context_admissible(draft) is True
        assert "secret payload x9k" not in draft.content
        assert m2.id not in draft.derived_from
        assert m1.id in draft.derived_from
        assert draft.source_coverage == 1

    def test_all_quarantined_cluster_returns_none(self, tmp_manager):
        """Every member quarantined → nothing left to synthesize: the
        cluster falls out via the empty-members guard."""
        mgr = tmp_manager
        _add_raw(mgr, "note one")
        _add_raw(mgr, "note two")
        clusters = cluster_raw_memories(mgr, similarity_threshold=0.5, min_cluster_size=2)
        assert len(clusters) == 1
        cluster_id = clusters[0].cluster_id

        for m in mgr.sqlite.list_by_cluster(cluster_id):
            assert mgr.quarantine_entry(m.id, reason="test-danger", source="test")

        assert synthesize_cluster(mgr, cluster_id) is None


# ---------------------------------------------------------------------------
# F2 / F2b — synthesized-record tag policy
# ---------------------------------------------------------------------------


class TestSynthesizedTagPolicy:
    def test_applyto_and_severity_stripped_from_synthesized_record(self, tmp_manager):
        """members[0] carries applyTo:** (and a severity tag) → the
        synthesized record has NEITHER; it still has mnemos:synthesized
        and the project tag."""
        mgr = tmp_manager
        _add_raw(mgr, "security note one", extra_tags=["applyTo:**", "severity:high"])
        _add_raw(mgr, "security note two")

        clusters = cluster_raw_memories(mgr, similarity_threshold=0.5, min_cluster_size=2)
        assert len(clusters) == 1

        result = synthesize_cluster(mgr, clusters[0].cluster_id)
        assert result is not None
        draft = mgr.sqlite.get(result.draft_id)
        assert draft is not None

        assert not any(t.startswith("applyTo:") for t in draft.tags)
        assert not any(t.startswith("severity:") for t in draft.tags)
        assert "mnemos:synthesized" in draft.tags
        assert "project:mnemos" in draft.tags

    def test_no_federate_propagates_from_any_member(self, tmp_manager):
        """A member at position 2+ carrying mnemos:no-federate (e.g.
        secret-bearing) → the synthesized record is born no-federate."""
        mgr = tmp_manager
        _add_raw(mgr, "secret-bearing note one")  # first member: clean
        _add_raw(mgr, "secret-bearing note two", extra_tags=["mnemos:no-federate"])

        clusters = cluster_raw_memories(mgr, similarity_threshold=0.5, min_cluster_size=2)
        assert len(clusters) == 1

        # Precondition: the no-federate tag sits on the SECOND member
        # (list_by_cluster orders by created_at ASC), so only the
        # ANY-member rule — not members[0] inheritance — can carry it.
        members = mgr.sqlite.list_by_cluster(clusters[0].cluster_id)
        assert "mnemos:no-federate" not in members[0].tags
        assert "mnemos:no-federate" in members[1].tags

        result = synthesize_cluster(mgr, clusters[0].cluster_id)
        assert result is not None
        draft = mgr.sqlite.get(result.draft_id)
        assert draft is not None
        assert "mnemos:no-federate" in draft.tags

    def test_no_federate_from_first_member_not_duplicated(self, tmp_manager):
        """When members[0] already carries no-federate (inherited through
        the normal tag pass-through) the ANY-member rule must not append
        it a second time.

        Reviewer note (P3-5): this pin also passes on pre-fix code —
        the pre-fix wholesale inheritance carried the tag exactly once
        too. It is kept deliberately as a no-duplication regression pin
        on the dedup guard in the ANY-member append."""
        mgr = tmp_manager
        _add_raw(mgr, "secret note one", extra_tags=["mnemos:no-federate"])
        _add_raw(mgr, "secret note two")

        clusters = cluster_raw_memories(mgr, similarity_threshold=0.5, min_cluster_size=2)
        assert len(clusters) == 1

        result = synthesize_cluster(mgr, clusters[0].cluster_id)
        assert result is not None
        draft = mgr.sqlite.get(result.draft_id)
        assert draft is not None
        assert draft.tags.count("mnemos:no-federate") == 1


# ---------------------------------------------------------------------------
# F3 — input_set_hash in the synthesis idempotency key
# ---------------------------------------------------------------------------


class TestSynthesisInputSetHash:
    def test_same_id_content_swap_invalidates_cache(self, tmp_manager):
        """Synthesize a cluster, swap one member's content in place (same
        memory id — the ADR-0019 same-ID swap write path), synthesize
        again with force=False → a NEW synthesis occurs (cache miss), not
        the stale cached one."""
        mgr = tmp_manager
        old_text = "deployment runbook step one"
        m1 = _add_raw(mgr, old_text)
        m2 = _add_raw(mgr, "deployment runbook step two")

        clusters = cluster_raw_memories(mgr, similarity_threshold=0.5, min_cluster_size=2)
        assert len(clusters) == 1
        cluster_id = clusters[0].cluster_id

        r1 = synthesize_cluster(mgr, cluster_id)
        assert r1 is not None
        # Precondition: the cache works — an immediate repeat is a hit.
        r1_again = synthesize_cluster(mgr, cluster_id)
        assert r1_again is not None
        assert r1_again.draft_id == r1.draft_id
        # Precondition: the first draft embeds the pre-swap member text.
        assert old_text in r1.content

        # Same-ID content swap through the real update write path. The
        # swapped text deliberately shares no substring with the old one
        # so presence/absence discriminates the two drafts cleanly.
        swapped_text = "incident postmortem actions for the api gateway"
        updated = mgr.update(m1.id, MemoryUpdate(content=swapped_text))
        assert updated is not None
        assert updated.id == m1.id
        assert updated.content == swapped_text

        r2 = synthesize_cluster(mgr, cluster_id, force=False)
        assert r2 is not None
        # Content-based discrimination (a fresh draft, not the stale
        # cached one): new draft id carrying the swapped text, old
        # member text absent — the placeholder builder embeds member
        # content, so the stale cached draft would answer with the
        # pre-swap text instead.
        assert r2.draft_id != r1.draft_id
        assert swapped_text in r2.content
        assert old_text not in r2.content

        new_draft = mgr.sqlite.get(r2.draft_id)
        assert new_draft is not None
        assert new_draft.status == MemoryStatus.PROCESSED
        assert is_context_admissible(new_draft) is True

        # P1-2 — the fresh draft consumed SOURCE members only: the prior
        # draft D1 (which sat in the same cluster) fed neither the
        # content (the old member text above) nor derived_from.
        assert set(new_draft.derived_from) == {m1.id, m2.id}
        assert r1.draft_id not in new_draft.derived_from
        assert new_draft.source_coverage == 2

        # P2 — supersession: the stale draft is retired, not served.
        # Zero-loss: the row is kept, ARCHIVED out of the admissible
        # set, with superseded_by pointing at its replacement.
        old_draft = mgr.sqlite.get(r1.draft_id)
        assert old_draft is not None, "zero-loss: the stale draft row is never deleted"
        assert old_draft.status == MemoryStatus.ARCHIVED
        assert is_context_admissible(old_draft) is False
        assert old_draft.metadata.get("superseded_by") == r2.draft_id
        assert swapped_text not in old_draft.content

        # Idempotency still holds for the fresh projection: an immediate
        # repeat (same members, no further swap) hits the NEW cache.
        r2_again = synthesize_cluster(mgr, cluster_id)
        assert r2_again is not None
        assert r2_again.draft_id == r2.draft_id
