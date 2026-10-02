"""Regression tests for project slug normalization — one project, one namespace.

mnemos #400 (supersedes the June-era closed PR #62; fix f00de78 + tests
51c2f65 were never merged — 430 commits stale at re-diagnosis time).
Adapted to the CURRENT APIs (post-rebrand ``vesmaro`` package, post-#263
identity hardening, ``_PROJECT_RE`` anchored with ``\\Z`` since #387).

The June bug, re-confirmed against current main by the #400 re-diagnosis
probes:

* ``MemoryManager.save_checkpoint`` (the single authority behind
  ``vesma_save_context`` and the REST twin ``POST /context/save``)
  accepted the ``project`` argument VERBATIM — it never passes through
  the tag contract like ``vesma_add`` does, so ``MyProject`` /
  ``My Project`` persisted raw into the ``project`` column and the
  ``project:`` tag, while the same logical project written through
  ``vesma_add`` landed (strict) rejected or (lax) normalized — two
  store keys for one project: a namespace island.
* Read paths (``search``, ``recall_context``, ``list_recent``,
  ``agent_recall``) predicated on the raw query string, so a
  differently-spelled query returned zero in-scope rows (and search
  then tripped the #313 cross-project soft fallback — silently
  WIDENING the "island" into other projects' rows).
* ``_detect_project`` (cwd auto-derivation) returned the raw folder
  name (``Project-Umbra`` → a third spelling).

The fix (single-point normalization, the #263 single-authority doctrine):

* ``vesmaro.models.normalize_project_slug`` — the ONE normalization
  (strip → lower → spaces-to-hyphens), shared by the tag-contract lax
  mode (which previously had its own private copy) and every direct
  ``project``-field boundary.
* SAVE boundary: ``MemoryManager.save_checkpoint`` normalizes then
  fail-loud-validates against ``_PROJECT_RE`` (an unsalvageable slug is
  a clean ValueError, never a silently-different namespace).
* QUERY boundary: ``search`` / ``recall_context`` / ``list_recent`` /
  ``agent_recall`` normalize a non-empty scope; ``None``/empty stays the
  explicit global mode. ``recall_context`` additionally fail-loud-
  validates (a checkpoint scope that cannot exist is information, not a
  fallback candidate).
* ``_detect_project`` normalizes at the entry point.

These tests pin every leg: the unit helper, the save boundary (MCP +
REST twins), each read boundary, the auto-derivation, and the
fail-loud rejections.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vesmaro.api import main as api_main
from vesmaro.api.main import app, lifespan
from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.mcp_server import call_tool
from vesmaro.models import _PROJECT_RE, MemoryCreate, MemorySource, normalize_project_slug

# The June canonical example: PascalCase folder vs lowercase slug.
PASCAL = "Project-Umbra"
LOWER = "project-umbra"


# ---------------------------------------------------------------------------
# Fixtures — isolated REAL manager per test (mirrors test_workflow_e2e.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            mnemos={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
        )
        settings.resolve_paths()
        yield settings


@pytest.fixture
def real_manager(tmp_settings):
    mgr = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


def _strip_reminder(text: str) -> str:
    """Strip the checkpoint reminder appended after the JSON payload.

    Same helper shape as test_workflow_e2e / test_tags_grouped_e2e /
    test_workflow (#401): the module-global ``_checkpoint_tracker`` can
    fire mid-suite; the reminder is MCP client metadata, not payload.
    """
    idx = text.find("\n\n⚠️ [mnemos]")
    return text[:idx] if idx != -1 else text


async def _call(mgr: MemoryManager, name: str, args: dict) -> str:
    """Real call_tool round-trip against an isolated manager, reminder-stripped."""
    with patch("vesmaro.mcp_server.get_manager", return_value=mgr):
        contents = await call_tool(name, args)
    assert len(contents) == 1
    return _strip_reminder(contents[0].text)


# ---------------------------------------------------------------------------
# Unit: the single normalization authority
# ---------------------------------------------------------------------------


class TestNormalizeProjectSlug:
    def test_lowercases(self) -> None:
        assert normalize_project_slug(PASCAL) == LOWER

    def test_replaces_spaces(self) -> None:
        assert normalize_project_slug("My Project") == "my-project"

    def test_strips_whitespace(self) -> None:
        assert normalize_project_slug(f"  {PASCAL}  ") == LOWER

    def test_preserves_canonical(self) -> None:
        assert normalize_project_slug(LOWER) == LOWER

    def test_preserves_hyphens_and_digits(self) -> None:
        assert normalize_project_slug("M2-Release-v2") == "m2-release-v2"

    def test_underscores_survive(self) -> None:
        assert normalize_project_slug("My_Project") == "my_project"

    def test_empty_string(self) -> None:
        assert normalize_project_slug("") == ""

    def test_normalized_form_matches_project_re(self) -> None:
        # Contract of the helper: a non-empty normalized slug is exactly
        # what _PROJECT_RE accepts as project:<slug> (#387 \Z anchor).
        assert _PROJECT_RE.match(f"project:{normalize_project_slug(PASCAL)}")


# ---------------------------------------------------------------------------
# SAVE boundary — save_checkpoint normalizes (MCP + REST twins)
# ---------------------------------------------------------------------------


class TestSaveBoundaryNormalizes:
    def test_save_checkpoint_persists_normalized_slug(self, real_manager: MemoryManager) -> None:
        mem, dup = real_manager.save_checkpoint(
            {"goals": "test normalization"}, project=PASCAL, agent="qa", session="s-1"
        )
        assert dup is False
        assert mem.project == LOWER
        assert f"project:{LOWER}" in mem.tags

    async def test_mcp_save_context_persists_normalized_slug(
        self, real_manager: MemoryManager
    ) -> None:
        text = await _call(
            real_manager,
            "vesma_save_context",
            {"project": PASCAL, "goals": "mcp normalization", "agent": "qa", "session": "s-2"},
        )
        assert "Context saved" in text
        rows = real_manager.sqlite.list_all(project=LOWER, limit=5)
        assert any(f"project:{LOWER}" in m.tags and m.project == LOWER for m in rows), (
            f"expected lowercase {LOWER}, got projects={[m.project for m in rows]}"
        )

    def test_save_dedup_across_spellings(self, real_manager: MemoryManager) -> None:
        """Same logical project saved twice with different spellings = ONE row.

        The issuer-keyed dedup hashes (project, agent, fields) — without
        boundary normalization the two spellings were two issuers and a
        re-save of the identical checkpoint created a duplicate island.
        """
        first, dup1 = real_manager.save_checkpoint(
            {"goals": "same goal"}, project=PASCAL, agent="qa", session="s-3"
        )
        second, dup2 = real_manager.save_checkpoint(
            {"goals": "same goal"}, project=LOWER, agent="qa", session="s-3"
        )
        assert dup1 is False
        assert dup2 is True, "cross-spelling re-save must dedup on the normalized slug"
        assert second.id == first.id

    def test_rest_twin_save_normalizes(self, real_manager: MemoryManager) -> None:
        api_main._manager = real_manager
        test_app = FastAPI(title="probe", lifespan=lifespan)
        for route in app.routes:
            test_app.routes.append(route)
        try:
            with TestClient(test_app) as tc:
                resp = tc.post(
                    "/context/save", json={"project": PASCAL, "goals": "rest twin", "agent": "qa"}
                )
                assert resp.status_code == 201, resp.text
                rows = real_manager.sqlite.list_all(project=LOWER, limit=5)
                assert any(m.project == LOWER for m in rows)
        finally:
            api_main._manager = None


# ---------------------------------------------------------------------------
# QUERY boundary — read paths normalize the filter
# ---------------------------------------------------------------------------


class TestQueryBoundaryNormalizes:
    def _seed(self, mgr: MemoryManager) -> None:
        mgr.add(
            MemoryCreate(
                content="lowercase-namespace-entry",
                tags=[f"project:{LOWER}", "agent:qa", "mnemos:decision"],
                source=MemorySource.MCP,
            ),
            project=LOWER,
            agent="qa",
        )

    def test_search_matches_via_pascalcase_filter(self, real_manager: MemoryManager) -> None:
        self._seed(real_manager)
        results = real_manager.search(
            query="lowercase-namespace-entry", project=PASCAL, include_raw=True
        )
        assert any(r.memory.project == LOWER for r in results), (
            "PascalCase filter failed to match lowercase entry"
        )

    def test_search_no_soft_fallback_for_variant_spelling(
        self, real_manager: MemoryManager
    ) -> None:
        """A differently-spelled scope must find the rows IN SCOPE — and must
        NOT trip the #313 cross-project soft fallback (which silently widens
        the result set into other projects' rows)."""
        self._seed(real_manager)
        # A second, genuinely different project that would match the query.
        real_manager.add(
            MemoryCreate(
                content="lowercase-namespace-entry",
                tags=["project:other-project", "agent:qa", "mnemos:decision"],
                source=MemorySource.MCP,
            ),
            project="other-project",
            agent="qa",
        )
        results = real_manager.search(
            query="lowercase-namespace-entry", project=PASCAL, include_raw=True
        )
        assert results, "scoped search must now find the normalized rows"
        assert all(not r.project_scope_fallback for r in results), (
            "no cross-project soft fallback may fire for a spelling variant"
        )
        assert {r.memory.project for r in results} == {LOWER}

    def test_recall_context_matches_via_pascalcase_filter(
        self, real_manager: MemoryManager
    ) -> None:
        real_manager.save_checkpoint(
            {"goals": "recall test"}, project=LOWER, agent="qa", session="s-4"
        )
        memories = real_manager.recall_context(project=PASCAL)
        assert any("recall test" in m.content for m in memories), (
            "PascalCase recall filter failed to match lowercase checkpoint"
        )

    async def test_mcp_recall_context_via_variant(self, real_manager: MemoryManager) -> None:
        text = await _call(
            real_manager,
            "vesma_save_context",
            {"project": LOWER, "goals": "recall mcp", "agent": "qa", "session": "s-5"},
        )
        assert "Context saved" in text
        text = await _call(real_manager, "vesma_recall_context", {"project": PASCAL})
        assert "recall mcp" in text, "PascalCase recall failed to match lowercase checkpoint"

    def test_list_recent_matches_via_variant_filter(self, real_manager: MemoryManager) -> None:
        self._seed(real_manager)
        memories = real_manager.list_recent(project="Project Umbra")
        assert any(m.project == LOWER for m in memories)

    def test_agent_recall_matches_via_variant_filters(self, real_manager: MemoryManager) -> None:
        from vesmaro.models import AgentRecallQuery

        self._seed(real_manager)
        results = real_manager.agent_recall(AgentRecallQuery(agent="qa", project=PASCAL))
        assert any(r.memory.project == LOWER for r in results), (
            "PascalCase agent-recall filter failed to match lowercase entry"
        )

    def test_search_none_project_stays_global_mode(self, real_manager: MemoryManager) -> None:
        """The explicit global mode (project=None) is untouched by the fix."""
        self._seed(real_manager)
        results = real_manager.search(query="lowercase-namespace-entry", project=None)
        assert results, "global search must still find rows across projects"


# ---------------------------------------------------------------------------
# Auto-derivation — _detect_project normalizes
# ---------------------------------------------------------------------------


class TestDetectProjectNormalizes:
    def test_detect_project_lowercases_cwd(self) -> None:
        from vesmaro.mcp_server import _detect_project

        with patch("os.getcwd", return_value=f"/tmp/{PASCAL}"):
            assert _detect_project() == LOWER


# ---------------------------------------------------------------------------
# Fail-loud — an unsalvageable slug is a clean error, never a fake namespace
# ---------------------------------------------------------------------------


class TestUnsalvageableSlugRejected:
    def test_save_checkpoint_rejects_invalid_slug(self, real_manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="project must be 1-64 characters"):
            real_manager.save_checkpoint({"goals": "x"}, project="my/project", agent="qa")

    def test_save_checkpoint_rejects_oversize_slug(self, real_manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="project must be 1-64 characters"):
            real_manager.save_checkpoint({"goals": "x"}, project="a" * 65, agent="qa")

    async def test_mcp_save_context_rejects_invalid_slug(self, real_manager: MemoryManager) -> None:
        text = await _call(
            real_manager, "vesma_save_context", {"project": "my/project", "goals": "x"}
        )
        assert text.startswith("❌")
        assert "project must be 1-64 characters" in text

    def test_recall_context_rejects_invalid_slug(self, real_manager: MemoryManager) -> None:
        with pytest.raises(ValueError, match="project must be 1-64 characters"):
            real_manager.recall_context(project="my/project")

    def test_rest_twin_maps_rejection_to_400(self, real_manager: MemoryManager) -> None:
        api_main._manager = real_manager
        test_app = FastAPI(title="probe", lifespan=lifespan)
        for route in app.routes:
            test_app.routes.append(route)
        try:
            with TestClient(test_app) as tc:
                resp = tc.post("/context/save", json={"project": "my/project", "goals": "x"})
                assert resp.status_code == 400
                assert "project must be 1-64 characters" in resp.text
        finally:
            api_main._manager = None

    def test_rest_twin_recall_maps_rejection_to_400(self, real_manager: MemoryManager) -> None:
        """The recall twin mirrors the save twin's ValueError → 400 mapping.

        #407 review P2-2: without the handler, an unsalvageable slug raised
        through ``recall_context`` surfaced as a bare 500 (the save twin was
        the only one carrying the mapping). A bad request must be a 4xx
        with the actionable message, never a server error.
        """
        api_main._manager = real_manager
        test_app = FastAPI(title="probe", lifespan=lifespan)
        for route in app.routes:
            test_app.routes.append(route)
        try:
            with TestClient(test_app) as tc:
                resp = tc.post("/context/recall", json={"project": "my/project"})
                assert resp.status_code == 400, resp.text
                assert "project must be 1-64 characters" in resp.text
                # The happy path keeps the legacy 200 "no context" shape.
                ok = tc.post("/context/recall", json={"project": "valid-proj"})
                assert ok.status_code == 200
                assert ok.json()["checkpoints"] == []
        finally:
            api_main._manager = None


# ---------------------------------------------------------------------------
# End-to-end island regression — the exact June scenario, current APIs
# ---------------------------------------------------------------------------


class TestNoNamespaceIslands:
    async def test_two_spellings_one_namespace(self, real_manager: MemoryManager) -> None:
        await _call(
            real_manager,
            "vesma_save_context",
            {
                "project": "MyProject",
                "goals": "ship the feature",
                "agent": "alice",
                "session": "s-alice",
            },
        )
        await _call(
            real_manager,
            "vesma_add",
            {
                "content": "bob decision note",
                "tags": ["project:myproject", "agent:bob", "mnemos:decision"],
            },
        )

        # Both rows carry the SAME normalized store key.
        conn = real_manager.sqlite._get_conn()
        projects = {r[0] for r in conn.execute("SELECT project FROM memories").fetchall()}
        assert projects == {"myproject"}, f"namespace split: {projects}"

        # Recall via the OTHER spelling sees alice's checkpoint.
        text = await _call(real_manager, "vesma_recall_context", {"project": "myproject"})
        assert "ship the feature" in text

        # Scoped search via the OTHER spelling finds bob's note IN SCOPE
        # (no cross-project soft fallback).
        raw = await _call(
            real_manager,
            "vesma_search",
            {"query": "bob decision", "project": "MyProject", "include_raw": True},
        )
        hits = json.loads(raw)
        assert hits, "scoped search must find the same-namespace row"
        assert all(h["tags"][0] == "project:myproject" for h in hits)
        assert all(not h.get("project_scope_fallback") for h in hits)
