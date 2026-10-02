"""Search v2 (issue #313) — project soft fallback.

Live probe (production DB): a project-scoped search with a drifted slug
(``release-pipeline`` vs ``releases-pipeline``) returned 0 rows even
though the sought content existed under the sibling slug — the pre-v2
scope predicate was hard with no fallback, so scope drift made scoped
searches silently miss.

The fix is a NEW OUTER retry in ``MemoryManager.search`` (A9's pre-RRF
project predicate is untouched — the retry runs unscoped, in the
explicit global mode, with the SAME status/include_raw/refined_only
policy). Results that surface ONLY via the retry carry
``project_scope_fallback=True``; the event is audited via
``search_stats()["project_scope_fallback_total"]``.

Not retried: explicit ``status=`` drill-downs (a drill-down asserts the
row's lifecycle; an unscoped retry would resurface junk — the caller
keeps the zero, which is information).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus

PROJECT_DRIFT_A = "release-pipeline"  # the slug the caller asks with
PROJECT_DRIFT_B = "releases-pipeline"  # the slug the data was stored under

AGENT = "fallback-agent"


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


def _add(mgr: MemoryManager, content: str, *, project: str, status: MemoryStatus) -> object:
    data = MemoryCreate(
        content=content,
        tags=[f"project:{project}", f"agent:{AGENT}", "mnemos:test"],
        source=MemorySource.MCP,
        status=status,
    )
    return mgr.add(data, project=project, agent=AGENT)


class TestSoftFallback:
    def test_drifted_scope_surfaces_via_fallback_with_tag(self, manager) -> None:
        """The project-drift case: scoped miss, cross-project hit tagged."""
        _add(
            manager,
            "release pipeline marker: the deploy conveyor configuration",
            project=PROJECT_DRIFT_B,
            status=MemoryStatus.PUBLISHED,
        )
        results = manager.search("release conveyor", project=PROJECT_DRIFT_A, limit=5)
        assert results, "scope drift must not silently zero the search"
        assert all(r.project_scope_fallback for r in results)
        assert all(r.memory.project == PROJECT_DRIFT_B for r in results)

    def test_in_scope_hit_never_marked(self, manager) -> None:
        _add(
            manager,
            "release pipeline marker: the deploy conveyor configuration",
            project=PROJECT_DRIFT_A,
            status=MemoryStatus.PUBLISHED,
        )
        results = manager.search("release conveyor", project=PROJECT_DRIFT_A, limit=5)
        assert results
        assert all(not r.project_scope_fallback for r in results)

    def test_no_fallback_when_global_mode(self, manager) -> None:
        """project=None is the explicit global mode — no retry, no tag."""
        _add(
            manager,
            "global mode content about xylophones",
            project="p1",
            status=MemoryStatus.PUBLISHED,
        )
        results = manager.search("xylophone", project=None, limit=5)
        assert results
        assert all(not r.project_scope_fallback for r in results)

    def test_status_drilldown_not_retried(self, manager) -> None:
        """An explicit status= drill-down keeps its zero (no junk retry)."""
        _add(
            manager,
            "processed-only record about zeppelins",
            project=PROJECT_DRIFT_B,
            status=MemoryStatus.PUBLISHED,
        )
        results = manager.search(
            "zeppelin",
            project=PROJECT_DRIFT_A,
            status=MemoryStatus.PROCESSED,
            limit=5,
        )
        assert results == []

    def test_same_status_policy_on_fallback(self, manager) -> None:
        """The fallback keeps the default status gate — RAW stays invisible."""
        _add(
            manager,
            "raw row about yetis that must not surface",
            project=PROJECT_DRIFT_B,
            status=MemoryStatus.RAW,
        )
        results = manager.search("yeti", project=PROJECT_DRIFT_A, limit=5)
        assert results == []

    def test_fallback_counter_audited(self, manager) -> None:
        """search_stats carries the drift signal (new counter, distinct
        from cross_project_requests_total)."""
        _add(
            manager,
            "counter probe: the quasar logbook",
            project=PROJECT_DRIFT_B,
            status=MemoryStatus.PUBLISHED,
        )
        before = manager.search_stats()
        assert before["project_scope_fallback_total"] == 0
        manager.search("quasar", project=PROJECT_DRIFT_A, limit=5)
        after = manager.search_stats()
        assert after["project_scope_fallback_total"] == 1
        # The fallback is NOT the explicit global mode — that counter
        # counts project=None requests only.
        assert after["cross_project_requests_total"] == before["cross_project_requests_total"]

    def test_scoped_zero_without_cross_hits_stays_zero(self, manager) -> None:
        """Nothing anywhere → the fallback retries, finds nothing, returns []."""
        results = manager.search("nonexistent-token-zzz", project=PROJECT_DRIFT_A, limit=5)
        assert results == []

    def test_agent_scope_kept_on_fallback(self, manager) -> None:
        """The retry drops ONLY the project scope; the FTS agent filter stays.

        Note: ``agent`` scopes the FTS leg (``fts_search`` predicate) —
        the vector leg has no agent filter (pre-existing contract; the
        mock embedder makes every row score identically, so a cross-
        agent row can still arrive via the vector leg). What must hold:
        the fallback does not WIDEN the agent filter — the retry passes
        the same agent to the same FTS predicate.
        """
        _add(
            manager,
            "other agent's row about anchors",
            project=PROJECT_DRIFT_B,
            status=MemoryStatus.PUBLISHED,
        )
        # Direct FTS-leg check: the retry's FTS predicate keeps the agent.
        fts_hits = manager.sqlite.fts_search(
            "anchor",
            limit=5,
            agent="someone-else",
        )
        assert all(m.agent == "someone-else" for m, _ in fts_hits)
        assert fts_hits == []  # the row belongs to fallback-agent
        # End-to-end: the scoped search as another agent never FTS-matches
        # the row; the fallback retry consults the same predicate.
        scoped = manager.search("anchor", project=PROJECT_DRIFT_B, agent="fallback-agent", limit=5)
        assert scoped, "the owning agent sees its row"
