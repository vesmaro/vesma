"""A9 (ArchCom 2026-08-27) — pre-RRF project predicate in the vector leg.

Before A9, ``MemoryManager.search`` scoped only the FTS leg by project;
the vector leg resolved candidates from the WHOLE store, so a
project-scoped search could surface other projects' rows through the
vector resolve path. The fix is two-layer, both BEFORE RRF fusion:

* native store predicate — ``VectorStore.search(project=...)`` filters
  candidates on the embedding metadata's ``project`` (pre-top-k, so
  foreign rows never enter the candidate set);
* authoritative resolve-time guard — the SQLite ``Memory.project``
  (the source of truth) is re-checked before the candidate's score is
  fused, guarding against vector-metadata drift.

Also covered: the explicit global mode (``project=None``) stays
cross-project and is flagged in the search stats
(``cross_project_requests_total``); the 4x over-fetch never leaks past
``limit`` after fusion; and the interim assemble.py boundary drop
(``stats.recall.project_scoped_out``) is REMOVED — the systemic fix
supersedes the channel patch.

The mock embedder returns an identical vector for every text on purpose:
identical embeddings are the worst case for the old bug (the vector leg
could not prefer the in-project copy), so any leak here is structural,
not a ranking accident.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesmaro.assemble import assemble_context
from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus
from vesmaro.storage.vector_store import VectorStore

PROJECT_A = "proj-alpha"
PROJECT_B = "proj-beta"
AGENT = "a9-agent"

# Same content stored under both projects — the canonical A9 fixture:
# identical rows, identical embeddings, distinguishable ONLY by project.
SHARED_CONTENT = (
    "alpha deployment runbook for the handler service: "
    "check the manifest, rotate the access policy, verify the baseline."
)
# Content that lives ONLY in project B — a scoped search of A over these
# terms must return nothing (pre-fix: the vector leg surfaced B's rows).
B_ONLY_CONTENT = (
    "betatopic falcon metrics collector: gathers betatopic spans "
    "from the betatopic stream and writes betatopic summaries."
)


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


def _add(
    mgr: MemoryManager,
    content: str,
    *,
    project: str,
) -> None:
    data = MemoryCreate(
        content=content,
        tags=[f"project:{project}", f"agent:{AGENT}", "mnemos:learning"],
        source=MemorySource.MCP,
        status=MemoryStatus.PUBLISHED,
    )
    mgr.add(data, project=project, agent=AGENT)


def _seed_cross_project(mgr: MemoryManager) -> None:
    for _ in range(3):
        _add(mgr, SHARED_CONTENT, project=PROJECT_A)
        _add(mgr, SHARED_CONTENT, project=PROJECT_B)
    _add(mgr, B_ONLY_CONTENT, project=PROJECT_B)


# ── VectorStore: native project predicate ─────────────────────────────────────


class TestVectorStorePredicate:
    def test_scoped_search_returns_only_project_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            vs = VectorStore(Path(tmpdir))
            try:
                vs.upsert("a1", [0.1] * 16, {"project": "pa", "agent": "x"})
                vs.upsert("b1", [0.1] * 16, {"project": "pb", "agent": "x"})
                hits = vs.search([0.1] * 16, limit=10, project="pa")
                assert [mid for mid, _ in hits] == ["a1"]
            finally:
                vs.close()

    def test_global_search_returns_all_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            vs = VectorStore(Path(tmpdir))
            try:
                vs.upsert("a1", [0.1] * 16, {"project": "pa", "agent": "x"})
                vs.upsert("b1", [0.1] * 16, {"project": "pb", "agent": "x"})
                hits = vs.search([0.1] * 16, limit=10)
                assert {mid for mid, _ in hits} == {"a1", "b1"}
                # Empty slug mirrors the FTS leg's truthiness: global mode.
                assert {mid for mid, _ in vs.search([0.1] * 16, limit=10, project="")} == {
                    "a1",
                    "b1",
                }
            finally:
                vs.close()

    def test_missing_metadata_excluded_from_scoped_search(self) -> None:
        """A row whose metadata cannot attest a project never matches a scope."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vs = VectorStore(Path(tmpdir))
            try:
                vs.upsert("a1", [0.1] * 16, {"project": "pa", "agent": "x"})
                vs.upsert("legacy", [0.1] * 16)  # no metadata stamped
                scoped = vs.search([0.1] * 16, limit=10, project="pa")
                assert [mid for mid, _ in scoped] == ["a1"]
                # Global mode is unaffected by missing metadata.
                assert {mid for mid, _ in vs.search([0.1] * 16, limit=10)} == {
                    "a1",
                    "legacy",
                }
            finally:
                vs.close()

    def test_corrupt_metadata_excluded_from_scoped_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)
            vs = VectorStore(path)
            try:
                vs.upsert("a1", [0.1] * 16, {"project": "pa", "agent": "x"})
                vs.upsert("rotten", [0.1] * 16, {"project": "pa", "agent": "x"})
            finally:
                vs.close()
            # Corrupt one row's metadata out-of-band (hand-edited DB).
            conn = sqlite3.connect(str(path / "vectors.db"))
            conn.execute("UPDATE embeddings SET metadata='not-json' WHERE id='rotten'")
            conn.commit()
            conn.close()
            vs2 = VectorStore(path)
            try:
                scoped = vs2.search([0.1] * 16, limit=10, project="pa")
                assert [mid for mid, _ in scoped] == ["a1"]
            finally:
                vs2.close()


# ── MemoryManager.search: pre-RRF scoping on the vector leg ──────────────────


class TestManagerVectorLegScoping:
    def test_project_scoped_search_returns_only_that_project(self, manager: MemoryManager) -> None:
        _seed_cross_project(manager)
        results = manager.search("alpha deployment", project=PROJECT_A, limit=10)
        assert results, "in-project same-content rows must surface"
        assert all(r.memory.project == PROJECT_A for r in results)

    def test_foreign_only_vector_hits_do_not_surface(self, manager: MemoryManager) -> None:
        """The A9 regression: query terms exist ONLY in project B.

        The FTS leg (project-scoped) matches nothing in A, so any result
        can arrive ONLY through the vector leg — pre-fix it resolved B's
        rows into a project-A-scoped search; post-fix both predicate
        layers exclude them. The mock embedder ties every cosine score,
        which is exactly the leak-enabling worst case.
        """
        _seed_cross_project(manager)
        results = manager.search("betatopic falcon", project=PROJECT_A, limit=10)
        assert all(r.memory.project == PROJECT_A for r in results)
        assert not any("betatopic" in r.memory.content for r in results)
        # Sanity: the terms ARE findable in B's own scope.
        b_results = manager.search("betatopic falcon", project=PROJECT_B, limit=10)
        assert b_results
        assert all(r.memory.project == PROJECT_B for r in b_results)

    def test_global_mode_cross_project_and_flagged(self, manager: MemoryManager) -> None:
        _seed_cross_project(manager)
        before = manager.search_stats()["cross_project_requests_total"]
        results = manager.search("alpha deployment", limit=10)
        projects = {r.memory.project for r in results}
        assert projects == {PROJECT_A, PROJECT_B}
        stats = manager.search_stats()
        assert stats["cross_project_requests_total"] == before + 1
        # A project-scoped search never increments the cross-project flag.
        manager.search("alpha deployment", project=PROJECT_A, limit=10)
        assert manager.search_stats()["cross_project_requests_total"] == before + 1

    def test_overfetch_does_not_leak_past_limit_after_fusion(self, manager: MemoryManager) -> None:
        _seed_cross_project(manager)
        results = manager.search("alpha deployment", project=PROJECT_A, limit=3)
        assert 1 <= len(results) <= 3
        global_results = manager.search("alpha deployment", limit=3)
        assert len(global_results) <= 3


# ── assemble.py: interim boundary drop removed ────────────────────────────────


class TestAssembleBoundaryDropRemoved:
    def test_recall_stats_have_no_boundary_guard_and_blocks_are_project_pure(
        self, manager: MemoryManager
    ) -> None:
        """A9 removal: the channel patch is gone, the systemic fix holds.

        Pre-A9 the recall stage dropped foreign-project candidates at its
        own boundary and counted ``project_scoped_out``; with the pre-RRF
        predicate the guard is redundant — search results arrive
        project-pure, so the stats key is gone and every assembled block
        carries the requested project.
        """
        for _ in range(3):
            _add(manager, SHARED_CONTENT, project=PROJECT_A)
            _add(manager, SHARED_CONTENT, project=PROJECT_B)
        result = assemble_context(manager, session="a9-sess", project=PROJECT_A, budget=4096)
        assert "project_scoped_out" not in result["stats"]["recall"]
        assert result["blocks"], "same-content rows in-scope must assemble"
        assert all(b["project"] == PROJECT_A for b in result["blocks"])
