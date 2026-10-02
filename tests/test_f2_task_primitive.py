"""Ф2 (epic #308, ADR-0027 Phase 2) — the task-scoped switcher as a
first-class primitive: contract tests.

The owner's arbitration (2026-09-28, mnemos 2b3ae42f): the F1 experiment
measured H1 PASS (+17.71pp) with H2/H3 ties — the primitive is built FOR
ERGONOMICS AND OPTIONALITY, and a standing comparative metric watches for
drift. This wave pins:

1. **Read-side parity** — ``task=`` on ``search`` / ``recall_context`` /
   ``list_recent`` / ``agent_recall`` mirrors how ``tags=`` rides those
   surfaces: semantics are the STRICT tag intersection, exactly what
   ``tags=["task:<slug>"]`` does (the F1 arm-C surface).
2. **Write-side convenience** — ``task=`` on ``save_checkpoint`` (the
   MCP/REST single save boundary) mints the ``task:<slug>`` tag; the
   zero-or-one-per-record invariant holds by construction.
3. **Equivalence pins (the A==C doctrine)** — on EVERY surface that
   gains ``task=``, ``task="X"`` ≡ ``tags=["task:X"]`` byte-identical
   results; the combined ``task=`` + ``tags=`` case intersects (both
   must hold).
4. **Boundary normalization (the #407 canon)** — ``My Task`` →
   ``my-task`` salvage through the SAME normalization the project
   dimension uses; ``my/task`` fails loud per-surface (manager
   ValueError → REST 400 / MCP error string, the twin discipline).
5. **Comparative telemetry** — the owner's standing metric:
   ``search_stats()`` counters split task-scoped queries by switcher
   form (``task_param_queries_total`` vs ``task_tag_queries_total``).

The F1 runner/corpus (benchmarks/experiments/f1_task_scope) is FROZEN
by this wave — its A==C finding is what the equivalence pins here
encode; tests/test_f1_* stay untouched and green.

Test embedder: ``_HashEmbedder`` (deterministic hashed bag-of-tokens) —
same rationale as tests/test_f0_task_scope.py: a MagicMock embedder
cannot discriminate and would fake the ranking/gate semantics under
test.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import (
    AgentRecallQuery,
    MemoryCreate,
    MemoryStatus,
    normalize_task_slug,
)

PROJECT = "f2-proj"
AGENT = "f2-agent"
TASK = "f2-task"
TASK_B = "f2-other"

VALID_BASE = ["project:x", "agent:y", "mnemos:learning"]


# ---------------------------------------------------------------------------
# Test embedder (deterministic — mirrors the F0 suite)
# ---------------------------------------------------------------------------


class _HashEmbedder:
    """Deterministic test embedder: hashed bag-of-tokens vectors.

    ``embed`` is a pure function of the text; cosine tracks token
    overlap, so lexical overlap is the only similarity signal and every
    scope gate under test is the DECIDER.
    """

    DIM = 256

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.DIM
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % self.DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def _settings(tmp: Path) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        scanner={"enabled": False},  # type: ignore[arg-type]
    )
    settings.resolve_paths()
    return settings


@pytest.fixture
def mgr(tmp_path: Path) -> Iterator[MemoryManager]:
    manager = MemoryManager(_settings(tmp_path))
    manager._embedder = _HashEmbedder()
    yield manager
    manager.close()


def _add(
    mgr: MemoryManager,
    content: str,
    *,
    project: str = PROJECT,
    agent: str = AGENT,
    task: str | None = None,
    status: MemoryStatus = MemoryStatus.PUBLISHED,
) -> None:
    """Store one memory through the tag-contract channel (task tag optional).

    Mirrors the F0 suite's ``_row``: the denormalised ``project``/``agent``
    columns ride ``add(project=…, agent=…)`` — the A9 pre-RRF predicate
    reads those columns, not the tags.
    """
    tags = [f"project:{project}", f"agent:{agent}", "mnemos:learning"]
    if task is not None:
        tags.append(f"task:{task}")
    mgr.add(
        MemoryCreate(content=content, tags=tags, status=status),
        project=project,
        agent=agent,
    )


@pytest.fixture
def mixed_corpus(mgr: MemoryManager) -> MemoryManager:
    """Task-tagged rows in two tasks + task-less rows, one project/agent."""
    _add(mgr, "alpha shared knowledge row", task=TASK)
    _add(mgr, "alpha second row same task", task=TASK)
    _add(mgr, "beta other task row", task=TASK_B)
    _add(mgr, "gamma task-less row")
    return mgr


# ---------------------------------------------------------------------------
# 1. Boundary normalization (the #407 canon) — the shared helper
# ---------------------------------------------------------------------------


class TestNormalizeTaskSlug:
    def test_salvages_uppercase_and_spaces(self) -> None:
        # The salvage contract: lowercase, strip, spaces→hyphens.
        assert normalize_task_slug("My_Task") == "my_task"
        assert normalize_task_slug("  My Task  ") == "my-task"

    def test_empty_stays_empty(self) -> None:
        assert normalize_task_slug("") == ""
        assert normalize_task_slug("   ") == ""

    def test_unsalvageable_kept_for_caller_rejection(self) -> None:
        # The helper never mints a silently-different namespace: the
        # slash survives normalization so the boundary can reject it.
        assert normalize_task_slug("my/task") == "my/task"


class TestTaskBoundaryValidation:
    """Every task= surface shares ONE boundary: _normalize_task_boundary."""

    @pytest.mark.parametrize(
        "bad",
        ["my/task", "task-" * 33, "", "   ", "task:t1"],
    )
    def test_search_rejects_unsalvageable(self, mgr: MemoryManager, bad: str) -> None:
        with pytest.raises(ValueError, match="task"):
            mgr.search("q", project=PROJECT, task=bad)

    def test_prefix_carrying_value_gets_actionable_message(self, mgr: MemoryManager) -> None:
        with pytest.raises(ValueError, match="pass 't1'"):
            mgr.search("q", project=PROJECT, task="task:t1")

    def test_recall_context_rejects_unsalvageable(self, mgr: MemoryManager) -> None:
        with pytest.raises(ValueError, match="task"):
            mgr.recall_context(project=PROJECT, task="my/task")

    def test_list_recent_rejects_unsalvageable(self, mgr: MemoryManager) -> None:
        with pytest.raises(ValueError, match="task"):
            mgr.list_recent(project=PROJECT, task="my/task")

    def test_agent_recall_rejects_unsalvageable(self, mgr: MemoryManager) -> None:
        with pytest.raises(ValueError, match="task"):
            mgr.agent_recall(AgentRecallQuery(agent=AGENT, project=PROJECT, task="my/task"))

    def test_save_checkpoint_rejects_unsalvageable(self, mgr: MemoryManager) -> None:
        with pytest.raises(ValueError, match="task"):
            mgr.save_checkpoint({"goals": "g"}, project=PROJECT, agent=AGENT, task="my/task")

    def test_salvaged_slug_round_trips(self, mixed_corpus: MemoryManager) -> None:
        """The boundary salvage round-trips: a row stored under the
        canonical slug is found by a caller passing the salvageable form
        ('My Task' → my-task — the SAME normalization the project
        dimension uses, the #407 canon)."""
        _add(mixed_corpus, "delta salvage probe row", task="my-task")
        results = mixed_corpus.search("delta", project=PROJECT, task="My Task")
        assert all("task:my-task" in r.memory.tags for r in results)
        assert len(results) == 1


# ---------------------------------------------------------------------------
# 2. Equivalence pins (the A==C doctrine) — every read surface
# ---------------------------------------------------------------------------


def _search_result_ids(m: MemoryManager, **kwargs: Any) -> list[str]:
    return [r.memory.id for r in m.search(**kwargs)]


class TestEquivalencePins:
    """task="X" ≡ tags=["task:X"] byte-identical — the F1 A==C doctrine."""

    def test_search_task_param_equals_task_tag(self, mixed_corpus: MemoryManager) -> None:
        via_param = _search_result_ids(
            mixed_corpus, query="alpha", project=PROJECT, task=TASK, limit=10
        )
        via_tag = _search_result_ids(
            mixed_corpus, query="alpha", project=PROJECT, tags=[f"task:{TASK}"], limit=10
        )
        assert via_param == via_tag
        assert len(via_param) == 2  # only the two TASK rows, not TASK_B/task-less

    def test_search_scores_and_types_identical(self, mixed_corpus: MemoryManager) -> None:
        via_param = mixed_corpus.search(query="alpha", project=PROJECT, task=TASK, limit=10)
        via_tag = mixed_corpus.search(
            query="alpha", project=PROJECT, tags=[f"task:{TASK}"], limit=10
        )
        assert [(r.memory.id, r.score, r.search_type) for r in via_param] == [
            (r.memory.id, r.score, r.search_type) for r in via_tag
        ]

    def test_search_task_plus_tags_intersects(self, mixed_corpus: MemoryManager) -> None:
        """task= + tags= combined: BOTH must hold (intersection)."""
        both = _search_result_ids(
            mixed_corpus,
            query="alpha",
            project=PROJECT,
            task=TASK,
            tags=["mnemos:learning"],
            limit=10,
        )
        assert both == _search_result_ids(
            mixed_corpus,
            query="alpha",
            project=PROJECT,
            tags=[f"task:{TASK}", "mnemos:learning"],
            limit=10,
        )
        # A non-overlapping tags filter + task= → empty (both must hold).
        empty = _search_result_ids(
            mixed_corpus,
            query="alpha",
            project=PROJECT,
            task=TASK,
            tags=["mnemos:decision"],
            limit=10,
        )
        assert empty == []

    def test_search_task_scoped_never_soft_falls_back(self, mixed_corpus: MemoryManager) -> None:
        """The param form inherits the slice-1 fallback exemption (F0 pin,
        now verified for the NEW switcher form too)."""
        assert (
            _search_result_ids(
                mixed_corpus, query="zzz-no-hit", project=PROJECT, task="no-such-task"
            )
            == []
        )
        # The control: the unscoped query still soft-falls back (the
        # exemption is task-specific — the F0 non-task control case).
        assert _search_result_ids(mixed_corpus, query="zzz-no-hit", project="no-such-project") != []

    def test_list_recent_task_param_equals_task_tag(self, mixed_corpus: MemoryManager) -> None:
        via_param = [m.id for m in mixed_corpus.list_recent(project=PROJECT, task=TASK, limit=10)]
        via_tag = [
            m.id for m in mixed_corpus.list_recent(project=PROJECT, tags=[f"task:{TASK}"], limit=10)
        ]
        assert via_param == via_tag
        assert len(via_param) == 2

    def test_list_recent_task_plus_tags_intersects(self, mixed_corpus: MemoryManager) -> None:
        both = [
            m.id
            for m in mixed_corpus.list_recent(
                project=PROJECT, task=TASK, tags=["mnemos:learning"], limit=10
            )
        ]
        assert len(both) == 2

    def test_recall_context_task_param_equals_task_tag(self, mgr: MemoryManager) -> None:
        """save → recall: the param form ≡ the checkpoint+task tag filter."""
        mgr.save_checkpoint(
            {"goals": "checkpoint in task"}, project=PROJECT, agent=AGENT, task=TASK
        )
        mgr.save_checkpoint(
            {"goals": "checkpoint in other task"}, project=PROJECT, agent=AGENT, task=TASK_B
        )
        mgr.save_checkpoint({"goals": "task-less checkpoint"}, project=PROJECT, agent=AGENT)
        via_param = [m.id for m in mgr.recall_context(project=PROJECT, task=TASK, limit=10)]
        via_tag = [
            m.id for m in mgr.recall_context(project=PROJECT, limit=10) if f"task:{TASK}" in m.tags
        ]
        assert via_param == via_tag
        assert len(via_param) == 1
        # The query leg (search path) is equivalent too.
        via_param_q = [
            m.id
            for m in mgr.recall_context(project=PROJECT, query="checkpoint", task=TASK, limit=10)
        ]
        via_tag_q = [
            m.id
            for m in mgr.recall_context(project=PROJECT, query="checkpoint", limit=10)
            if f"task:{TASK}" in m.tags
        ]
        assert via_param_q == via_tag_q

    def test_agent_recall_task_param_equals_tag_filter(self, mixed_corpus: MemoryManager) -> None:
        """Recency leg: task= post-filter ≡ the client-side tag filter."""
        via_param = [
            r.memory.id
            for r in mixed_corpus.agent_recall(
                AgentRecallQuery(agent=AGENT, project=PROJECT, task=TASK, limit=10)
            )
        ]
        via_tag = [
            r.memory.id
            for r in mixed_corpus.agent_recall(
                AgentRecallQuery(agent=AGENT, project=PROJECT, limit=10)
            )
            if f"task:{TASK}" in r.memory.tags
        ]
        assert via_param == via_tag
        assert len(via_param) == 2
        # Query leg: task= rides search's tags filter — the same code path.
        via_param_q = [
            r.memory.id
            for r in mixed_corpus.agent_recall(
                AgentRecallQuery(agent=AGENT, project=PROJECT, query="alpha", task=TASK, limit=10)
            )
        ]
        via_tag_q = [
            r.memory.id
            for r in mixed_corpus.agent_recall(
                AgentRecallQuery(agent=AGENT, project=PROJECT, query="alpha", limit=10)
            )
            if f"task:{TASK}" in r.memory.tags
        ]
        assert via_param_q == via_tag_q


# ---------------------------------------------------------------------------
# 3. Write side — save_checkpoint task= mint + zero-or-one invariant
# ---------------------------------------------------------------------------


class TestSaveCheckpointTask:
    def test_task_param_mints_task_tag(self, mgr: MemoryManager) -> None:
        memory, duplicate = mgr.save_checkpoint(
            {"goals": "g"}, project=PROJECT, agent=AGENT, task=TASK
        )
        assert not duplicate
        stored = mgr.sqlite.get(memory.id)
        assert stored is not None
        assert f"task:{TASK}" in stored.tags
        # Exactly one task tag — the zero-or-one invariant.
        assert len([t for t in stored.tags if t.startswith("task:")]) == 1

    def test_salvaged_slug_normalized_at_save_boundary(self, mgr: MemoryManager) -> None:
        memory, _ = mgr.save_checkpoint(
            {"goals": "g"}, project=PROJECT, agent=AGENT, task="My Task"
        )
        stored = mgr.sqlite.get(memory.id)
        assert stored is not None
        assert "task:my-task" in stored.tags

    def test_saved_task_reachable_by_both_switcher_forms(self, mgr: MemoryManager) -> None:
        """The write/read round trip: save with task=, find with BOTH forms."""
        mgr.save_checkpoint({"goals": "g"}, project=PROJECT, agent=AGENT, task=TASK)
        via_param = mgr.recall_context(project=PROJECT, task=TASK, limit=5)
        via_tag = [
            m for m in mgr.recall_context(project=PROJECT, limit=5) if f"task:{TASK}" in m.tags
        ]
        assert [m.id for m in via_param] == [m.id for m in via_tag]
        assert len(via_param) == 1

    def test_task_not_in_dedup_payload(self, mgr: MemoryManager) -> None:
        """canon §10 (new-records-only): a dedup hit returns the FIRST row
        with ITS task scope — the new call's task never rewrites it."""
        first, _first_dup = mgr.save_checkpoint(
            {"goals": "g"}, project=PROJECT, agent=AGENT, task=TASK
        )
        second, dup2 = mgr.save_checkpoint(
            {"goals": "g"}, project=PROJECT, agent=AGENT, task=TASK_B
        )
        assert dup2
        assert second.id == first.id
        stored = mgr.sqlite.get(first.id)
        assert stored is not None
        assert f"task:{TASK}" in stored.tags
        assert f"task:{TASK_B}" not in stored.tags

    def test_multiple_task_tags_impossible_via_channel(self, mgr: MemoryManager) -> None:
        """The checkpoint channel mints its own tags, so task= can never
        produce two task tags; the tag contract's always-fatal check stays
        the invariant's last line of defense on OTHER write paths."""
        memory, _ = mgr.save_checkpoint({"goals": "g"}, project=PROJECT, agent=AGENT, task=TASK)
        stored = mgr.sqlite.get(memory.id)
        assert stored is not None
        assert len([t for t in stored.tags if t.startswith("task:")]) == 1


# ---------------------------------------------------------------------------
# 4. Comparative telemetry (the owner's standing metric)
# ---------------------------------------------------------------------------


class TestComparativeTelemetry:
    def test_counters_split_by_switcher_form(self, mixed_corpus: MemoryManager) -> None:
        stats0 = mixed_corpus.search_stats()
        assert stats0["task_param_queries_total"] == 0
        assert stats0["task_tag_queries_total"] == 0

        mixed_corpus.search("alpha", project=PROJECT, task=TASK)
        stats1 = mixed_corpus.search_stats()
        # The param form lands in BOTH counters: it mints the tag through
        # the same filter (param = the param-form share, tag = the total
        # task-scoped volume; tag - param = the tag-only share).
        assert stats1["task_param_queries_total"] == 1
        assert stats1["task_tag_queries_total"] == 1

        mixed_corpus.search("alpha", project=PROJECT, tags=[f"task:{TASK}"])
        stats2 = mixed_corpus.search_stats()
        assert stats2["task_param_queries_total"] == 1  # unchanged
        assert stats2["task_tag_queries_total"] == 2

    def test_taskless_queries_counted_nowhere(self, mixed_corpus: MemoryManager) -> None:
        mixed_corpus.search("alpha", project=PROJECT)
        mixed_corpus.search("alpha", project=PROJECT, tags=["mnemos:learning"])
        stats = mixed_corpus.search_stats()
        assert stats["task_param_queries_total"] == 0
        assert stats["task_tag_queries_total"] == 0

    def test_composite_surfaces_attribute_at_caller_intent(self, mgr: MemoryManager) -> None:
        """Review fix 2 (P2, TL decision): the INTERNAL translation form
        must not leak into the comparative metric. A caller who used the
        first-class ``task=`` on a composite surface increments the param
        counter uniformly — the query leg of ``recall_context`` threads
        the PARAM form, exactly like ``search`` / ``agent_recall`` — so
        ``task_tag - task_param`` measures the genuine tag-only share,
        not an implementation shape."""
        mgr.save_checkpoint({"goals": "g"}, project=PROJECT, agent=AGENT, task=TASK)

        s0 = mgr.search_stats()
        mgr.recall_context(project=PROJECT, query="checkpoint", task=TASK)
        s1 = mgr.search_stats()
        assert s1["task_param_queries_total"] - s0["task_param_queries_total"] == 1
        assert s1["task_tag_queries_total"] - s0["task_tag_queries_total"] == 1

        # agent_recall(query=, task=): unchanged behavior (already +1/+1).
        s0 = mgr.search_stats()
        mgr.agent_recall(
            AgentRecallQuery(agent=AGENT, project=PROJECT, query="checkpoint", task=TASK)
        )
        s1 = mgr.search_stats()
        assert s1["task_param_queries_total"] - s0["task_param_queries_total"] == 1
        assert s1["task_tag_queries_total"] - s0["task_tag_queries_total"] == 1

        # The recency legs run no search call at all — counters untouched
        # (the disclosed slice-1 trade-off; list_all paths carry no
        # counters, so attribution is unaffected).
        s0 = mgr.search_stats()
        mgr.recall_context(project=PROJECT, task=TASK)
        mgr.agent_recall(AgentRecallQuery(agent=AGENT, project=PROJECT, task=TASK))
        s1 = mgr.search_stats()
        assert s1["task_param_queries_total"] == s0["task_param_queries_total"]
        assert s1["task_tag_queries_total"] == s0["task_tag_queries_total"]

    def test_conflicting_task_read_is_silent_empty_not_fatal(self, mgr: MemoryManager) -> None:
        """Review fix 3 (doc-wording pin): the READ path runs no
        tag-contract validation — ``task=`` + a DIFFERENT ``task:`` tag
        in ``tags`` is a strict-AND no row can satisfy: a silent EMPTY
        result, never a rejection (the always-fatal multiple-``task:``
        check binds to the WRITE paths, where the contract validates)."""
        _add(mgr, "alpha in task one", task=TASK)
        _add(mgr, "beta in task two", task=TASK_B)
        results = mgr.search("alpha", project=PROJECT, task=TASK, tags=[f"task:{TASK_B}"], limit=10)
        assert results == []  # strict-AND: silent empty, not TagContractError


# ---------------------------------------------------------------------------
# 5. MCP twins — dispatch threading + fail-loud mapping
# ---------------------------------------------------------------------------


class TestMcpTwins:
    async def test_save_context_threads_task(self, mgr: MemoryManager) -> None:
        from vesmaro.mcp_server import _dispatch

        with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
            out = await _dispatch(
                "vesma_save_context",
                {"project": PROJECT, "goals": "g", "task": TASK},
            )
        assert isinstance(out, str) and "Context saved" in out
        stored = mgr.list_recent(limit=1, project=PROJECT)[0]
        assert f"task:{TASK}" in stored.tags

    async def test_save_context_task_error_surfaces(self, mgr: MemoryManager) -> None:
        from vesmaro.mcp_server import _call_tool_dispatch

        with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
            # The generic dispatch exception path: the "❌ Error: ..."
            # mapping (the surface's pre-W2 contract), never a traceback.
            out = await _call_tool_dispatch(
                "vesma_save_context",
                {"project": PROJECT, "goals": "g", "task": "my/task"},
            )
        text = out[0].text
        assert "Error" in text and "task" in text
        assert mgr.stats()["total"] == 0  # nothing stored

    async def test_recall_context_threads_task(self, mgr: MemoryManager) -> None:
        from vesmaro.mcp_server import _dispatch

        mgr.save_checkpoint({"goals": "g"}, project=PROJECT, agent=AGENT, task=TASK)
        mgr.save_checkpoint({"goals": "g2"}, project=PROJECT, agent=AGENT)
        with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
            out = await _dispatch(
                "vesma_recall_context",
                {"project": PROJECT, "task": TASK},
            )
        assert isinstance(out, str)
        assert "g" in out
        assert "g2" not in out  # the task-less checkpoint is filtered out

    async def test_search_threads_task(self, mixed_corpus: MemoryManager) -> None:
        from vesmaro.mcp_server import _dispatch

        with patch("vesmaro.mcp_server.get_manager", return_value=mixed_corpus):
            out = await _dispatch(
                "vesma_search",
                {"query": "alpha", "project": PROJECT, "task": TASK},
            )
        assert isinstance(out, list)
        assert len(out) == 2

    async def test_list_recent_threads_task(self, mixed_corpus: MemoryManager) -> None:
        from vesmaro.mcp_server import _dispatch

        with patch("vesmaro.mcp_server.get_manager", return_value=mixed_corpus):
            out = await _dispatch(
                "vesma_list_recent",
                {"project": PROJECT, "task": TASK},
            )
        assert isinstance(out, list)
        assert len(out) == 2


# ---------------------------------------------------------------------------
# 6. REST twins — request models + fail-loud 400 mapping
# ---------------------------------------------------------------------------


@pytest.fixture
def client_factory(mgr: MemoryManager):
    """FastAPI TestClient factory over the shared isolated manager
    (mirrors tests/test_checkpoint_canon_envelope.py)."""
    from vesmaro.api import main as api_main
    from vesmaro.api.main import app, lifespan

    class _Factory:
        def __enter__(self) -> TestClient:
            test_app = FastAPI(title="Mnemos-Test-F2", version="0.1.0", lifespan=lifespan)
            for route in app.routes:
                test_app.routes.append(route)
            api_main._manager = mgr
            self._tc = TestClient(test_app)
            return self._tc

        def __exit__(self, *exc: object) -> None:
            self._tc.close()
            api_main._manager = None

    return _Factory()


class TestRestTwins:
    def test_search_task_param(self, mixed_corpus: MemoryManager, client_factory: Any) -> None:
        with client_factory as tc:
            resp = tc.post(
                "/search",
                json={"query": "alpha", "project": PROJECT, "task": TASK, "limit": 10},
            )
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) == 2
        assert all(f"task:{TASK}" in r["tags"] for r in results)

    def test_search_task_param_equals_task_tag(
        self, mixed_corpus: MemoryManager, client_factory: Any
    ) -> None:
        with client_factory as tc:
            via_param = tc.post(
                "/search",
                json={"query": "alpha", "project": PROJECT, "task": TASK, "limit": 10},
            ).json()
            via_tag = tc.post(
                "/search",
                json={"query": "alpha", "project": PROJECT, "tags": [f"task:{TASK}"], "limit": 10},
            ).json()
        assert [r["id"] for r in via_param] == [r["id"] for r in via_tag]

    def test_search_bad_task_maps_to_400(
        self, mixed_corpus: MemoryManager, client_factory: Any
    ) -> None:
        """Unsalvageable task on /search: the manager ValueError maps to
        HTTP 400 (the #407 twin discipline — the SAME error string the
        recall/save twins map, never a raw 500, never a silent empty
        200)."""
        with client_factory as tc:
            resp = tc.post(
                "/search",
                json={"query": "alpha", "project": PROJECT, "task": "my/task"},
            )
        assert resp.status_code == 400
        assert "task" in resp.json()["detail"]

    def test_memories_task_param_equals_task_tag(
        self, mixed_corpus: MemoryManager, client_factory: Any
    ) -> None:
        with client_factory as tc:
            via_param = tc.get(f"/memories?project={PROJECT}&task={TASK}").json()
            via_tag = tc.get(f"/memories?project={PROJECT}&tags=task:{TASK}").json()
        assert [m["id"] for m in via_param] == [m["id"] for m in via_tag]
        assert len(via_param) == 2

    def test_memories_bad_task_maps_to_400(
        self, mixed_corpus: MemoryManager, client_factory: Any
    ) -> None:
        with client_factory as tc:
            resp = tc.get(f"/memories?project={PROJECT}&task=my/task")
        assert resp.status_code == 400
        assert "task" in resp.json()["detail"]

    def test_context_save_task_mints_tag(self, mgr: MemoryManager, client_factory: Any) -> None:
        with client_factory as tc:
            resp = tc.post(
                "/context/save",
                json={"project": PROJECT, "goals": "g", "task": TASK},
            )
        assert resp.status_code == 201
        stored = mgr.sqlite.get(resp.json()["id"])
        assert stored is not None
        assert f"task:{TASK}" in stored.tags

    def test_context_save_bad_task_maps_to_400(
        self, mgr: MemoryManager, client_factory: Any
    ) -> None:
        with client_factory as tc:
            resp = tc.post(
                "/context/save",
                json={"project": PROJECT, "goals": "g", "task": "my/task"},
            )
        assert resp.status_code == 400
        assert "task" in resp.json()["detail"]
        assert mgr.stats()["total"] == 0  # nothing stored

    def test_context_recall_task_param_equals_tag_filter(
        self, mgr: MemoryManager, client_factory: Any
    ) -> None:
        mgr.save_checkpoint({"goals": "g"}, project=PROJECT, agent=AGENT, task=TASK)
        mgr.save_checkpoint({"goals": "g2"}, project=PROJECT, agent=AGENT)
        with client_factory as tc:
            via_param = tc.post("/context/recall", json={"project": PROJECT, "task": TASK}).json()
            via_tag = tc.post("/context/recall", json={"project": PROJECT}).json()
        assert len(via_param["checkpoints"]) == 1
        assert f"task:{TASK}" in via_param["checkpoints"][0]["tags"]
        # The tag-form twin: same request minus task=, filtered client-side
        # by the same tag — the surfaces carry identical payloads.
        assert all(
            f"task:{TASK}" in c["tags"]
            for c in via_tag["checkpoints"]
            if c["id"] == via_param["checkpoints"][0]["id"]
        )

    def test_agent_recall_task_param(
        self, mixed_corpus: MemoryManager, client_factory: Any
    ) -> None:
        with client_factory as tc:
            resp = tc.get(f"/recall/agent/{AGENT}?project={PROJECT}&task={TASK}&limit=10").json()
        assert len(resp) == 2

    def test_agent_recall_bad_task_maps_to_400(
        self, mixed_corpus: MemoryManager, client_factory: Any
    ) -> None:
        """Review fix 1 (P1): the task boundary's ValueError maps to 400
        like every other REST twin of this wave — the probe
        ``GET /recall/agent/x?task=my/task`` used to surface as a 500,
        contradicting the documented 400 (the #407 twin discipline)."""
        with client_factory as tc:
            resp = tc.get(f"/recall/agent/{AGENT}?project={PROJECT}&task=my/task")
        assert resp.status_code == 400
        assert "task" in resp.json()["detail"]

    def test_agent_recall_task_param_equals_tag_filter(
        self, mixed_corpus: MemoryManager, client_factory: Any
    ) -> None:
        with client_factory as tc:
            via_param = tc.get(
                f"/recall/agent/{AGENT}?project={PROJECT}&task={TASK}&limit=10"
            ).json()
            via_all = tc.get(f"/recall/agent/{AGENT}?project={PROJECT}&limit=10").json()
        via_tag = [r for r in via_all if f"task:{TASK}" in r["tags"]]
        assert [r["id"] for r in via_param] == [r["id"] for r in via_tag]
