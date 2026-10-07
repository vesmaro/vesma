"""Wave W-H hybrid graph UX: literal-content fallback in search_graph +
bare-tail disambiguation in trace_path.

Contract pins:

* search_graph stays symbol-first and backward compatible — symbol rows
  carry ``match_kind: "symbol"`` and the ``fallback_used`` marker is
  PRESENT ONLY when the literal leg ran (shape policy: absent, never
  null/empty); ``total_matches`` counts the WHOLE answer set — a
  non-empty literal leg is never reported as ``total_matches: 0``
  (card vesma-graph-roughness-repeat1);
* the literal leg is confinement-bound (the REGISTERED root only,
  denied trees/names never opened), bounded (file/time/match caps), and
  PG4-redacted: a secret-detected literal row is DROPPED — raw secret
  content never reaches a response — and poisoned paths (PG3) never
  issue content;
* the ``code_graph.literal_fallback`` knob (default ON) disables the
  leg;
* trace_path: exact qname byte-identical to the pre-W-H tool, a UNIQUE
  bare tail traces directly, an AMBIGUOUS tail — and an IDENTICAL-qname
  collision (same qname, several files; card
  vesma-graph-roughness-repeat1) — answers a ranked candidate list
  (helpful payload, NOT an error), a missing symbol stays a clear
  not-found refusal.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_python", reason="code-graph extra not installed")
from fastapi.testclient import TestClient

from tests.test_codegraph_tools import (
    AGENT,
    PROJECT,
    SECRET_AWS_KEY,
    _rest_client,
    make_service,
)
from vesma.codegraph.literal_scan import (
    LITERAL_SNIPPET_MAX_CHARS,
    scan_literals,
)
from vesma.codegraph.service import (
    CodeGraphService,
    GraphConfinementError,
    GraphToolError,
)

# A string literal that is NOT a symbol name / path fragment anywhere in
# the fixture trees — the exact shape the value report called invisible.
LITERAL_TOOL_NAME = "mnemos_widget_flush"


def _hybrid_repo(tmp_path: Path) -> Path:
    """The hybrid fixture tree: symbol shapes (``Base``), plain
    assignments (``LINE_A..C`` — never symbols), a string literal, and
    the canonical fake-secret fixture (indexed → poisoned, PG3)."""
    root = tmp_path / "hybridrepo"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'hybridrepo'\n", encoding="utf-8")
    (root / "server.py").write_text(
        f'TOOL_NAME = "{LITERAL_TOOL_NAME}"\n\ndef run():\n    return 1\n',
        encoding="utf-8",
    )
    (root / "shapes.py").write_text(
        "class Base:\n    def greet(self):\n        return 'hi'\n",
        encoding="utf-8",
    )
    (root / "notes.py").write_text(
        "LINE_A = 'alpha'\nLINE_B = 'beta'\nLINE_C = 'gamma'\n", encoding="utf-8"
    )
    (root / "secret.py").write_text(f'AWS_ID = "{SECRET_AWS_KEY}"\n', encoding="utf-8")
    return root


@pytest.fixture
def hybrid_rest_client(tmp_path: Path) -> Iterator[TestClient]:
    """The REST twin over the hybrid tree (reuses the shared helper)."""
    yield from _rest_client(tmp_path, _hybrid_repo(tmp_path), enabled=True)


def _literal_repo(tmp_path: Path) -> Path:
    """A repo whose only mention of the needle is a string literal
    (plain assignment — never a graph symbol)."""
    root = tmp_path / "litrepo"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'litrepo'\n", encoding="utf-8")
    (root / "server.py").write_text(
        f'TOOL_NAME = "{LITERAL_TOOL_NAME}"\n\ndef run():\n    return 1\n',
        encoding="utf-8",
    )
    return root


def _dup_repo(tmp_path: Path, *, ambiguous: bool) -> Path:
    """A repo with one (unique) or two (ambiguous) ``update_fields``
    definitions, plus substring-only noise names for the unique case."""
    root = tmp_path / f"duprepo-{ambiguous}"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'duprepo'\n", encoding="utf-8")
    body = "class A:\n    def update_fields(self):\n        return 1\n"
    if ambiguous:
        (root / "a.py").write_text(body, encoding="utf-8")
        (root / "b.py").write_text(
            "class B:\n    def update_fields(self):\n        return 2\n",
            encoding="utf-8",
        )
    else:
        (root / "a.py").write_text(body, encoding="utf-8")
        # Substring noise: hits the search predicate but never the tail.
        (root / "b.py").write_text(
            "def update_fields_util():\n    return 2\n\n"
            "def update_fields_handler():\n    return 3\n",
            encoding="utf-8",
        )
    return root


def _collision_repo(tmp_path: Path) -> Path:
    """A repo with an IDENTICAL-qname collision: the same top-level
    function name defined in two files — both nodes carry the same
    bare qname (the vitals repeat #1 case, ``get_manager`` x4)."""
    root = tmp_path / "collisionrepo"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'collisionrepo'\n", encoding="utf-8")
    (root / "a.py").write_text("def reload():\n    return 1\n", encoding="utf-8")
    (root / "b.py").write_text("def reload():\n    return 2\n", encoding="utf-8")
    return root


@pytest.fixture
def lit_service(tmp_path: Path) -> Iterator[CodeGraphService]:
    svc, _ = make_service(tmp_path, _hybrid_repo(tmp_path))
    yield svc
    svc.close()


@pytest.fixture
def lit_indexed(lit_service: CodeGraphService) -> CodeGraphService:
    result = lit_service.index_project(PROJECT, agent=AGENT)
    assert result["status"] == "ok", result
    return lit_service


# ── hybrid search: the literal leg ───────────────────────────────────────────


class TestLiteralFallbackSearch:
    def test_literal_hit_on_string_literal(self, tmp_path: Path) -> None:
        repo = _literal_repo(tmp_path)
        svc, _ = make_service(tmp_path, repo)
        try:
            svc.index_project(PROJECT, agent=AGENT)
            result = svc.search_graph(PROJECT, LITERAL_TOOL_NAME, agent=AGENT)
            # The symbol graph has ZERO hits — the fallback answers, and
            # total_matches counts the literal rows it returned (card
            # vesma-graph-roughness-repeat1: a non-empty fallback is
            # never reported as total_matches: 0).
            assert result["fallback_used"] is True
            assert result["total_matches"] == len(result["results"]) == 1
            row = result["results"][0]
            assert row["match_kind"] == "literal"
            assert row["path"] == "server.py"
            assert row["line"] == 1
            assert LITERAL_TOOL_NAME in row["snippet"]
            # Literal rows carry no fake node identity.
            assert "id" not in row and "qname" not in row
        finally:
            svc.close()

    def test_literal_multi_row_count_covers_all_rows(self, tmp_path: Path) -> None:
        # Regression pin (card vesma-graph-roughness-repeat1): with
        # several literal rows, total_matches reflects ALL of them.
        repo = _literal_repo(tmp_path)
        (repo / "extra.py").write_text(f'X = "{LITERAL_TOOL_NAME}"\n', encoding="utf-8")
        svc, _ = make_service(tmp_path, repo)
        try:
            svc.index_project(PROJECT, agent=AGENT)
            result = svc.search_graph(PROJECT, LITERAL_TOOL_NAME, agent=AGENT)
            assert result["fallback_used"] is True
            assert result["total_matches"] == len(result["results"]) == 2
            assert all(r["match_kind"] == "literal" for r in result["results"])
        finally:
            svc.close()

    def test_symbol_hits_are_untouched_no_fallback_marker(
        self, lit_indexed: CodeGraphService
    ) -> None:
        result = lit_indexed.search_graph(PROJECT, "Base", agent=AGENT)
        assert result["total_matches"] > 0
        assert result["results"]
        for row in result["results"]:
            assert row["match_kind"] == "symbol"
            assert "path" in row and "qname" in row
        # Shape policy: the marker is ABSENT when the leg did not run.
        assert "fallback_used" not in result

    def test_literal_miss_is_honest_empty(self, lit_indexed: CodeGraphService) -> None:
        result = lit_indexed.search_graph(PROJECT, "zzz_absent_everywhere_42", agent=AGENT)
        assert result["results"] == []
        assert result["total_matches"] == 0
        # The leg RAN and found nothing — the marker says so honestly.
        assert result["fallback_used"] is True

    def test_knob_disables_the_fallback(self, tmp_path: Path) -> None:
        svc, _ = make_service(tmp_path, _hybrid_repo(tmp_path), literal_fallback=False)
        try:
            svc.index_project(PROJECT, agent=AGENT)
            result = svc.search_graph(PROJECT, "LINE_C", agent=AGENT)
            assert result["results"] == []
            assert "fallback_used" not in result
        finally:
            svc.close()

    def test_scan_confined_to_the_registered_root(self, tmp_path: Path) -> None:
        repo = _literal_repo(tmp_path)
        # A sibling tree OUTSIDE the registered root with the same literal.
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leak.py").write_text(f'X = "{LITERAL_TOOL_NAME}"\n', encoding="utf-8")
        svc, _ = make_service(tmp_path, repo)
        try:
            svc.index_project(PROJECT, agent=AGENT)
            result = svc.search_graph(PROJECT, LITERAL_TOOL_NAME, agent=AGENT)
            paths = [r["path"] for r in result["results"]]
            assert paths == ["server.py"]  # the sibling tree is never scanned
            assert all(not p.startswith("..") and not p.startswith("/") for p in paths)
        finally:
            svc.close()

    def test_denied_trees_and_names_never_scanned(self, tmp_path: Path) -> None:
        repo = _literal_repo(tmp_path)
        for denied in ("node_modules/dep.js", ".venv/pkg.py", ".hidden.txt", "creds.env"):
            p = repo / denied
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f'X = "{LITERAL_TOOL_NAME}"\n', encoding="utf-8")
        svc, _ = make_service(tmp_path, repo)
        try:
            svc.index_project(PROJECT, agent=AGENT)
            result = svc.search_graph(PROJECT, LITERAL_TOOL_NAME, agent=AGENT)
            assert [r["path"] for r in result["results"]] == ["server.py"]
        finally:
            svc.close()

    def test_fake_secret_literal_is_dropped_never_issued(self, tmp_path: Path) -> None:
        repo = _literal_repo(tmp_path)
        # .txt is outside the index's language allowlist → the file is
        # NOT indexed and NOT poisoned — the PG4 issuance scan is the
        # only guard, and it must hold.
        (repo / "leak.txt").write_text(f'AWS_ID = "{SECRET_AWS_KEY}"\n', encoding="utf-8")
        svc, _ = make_service(tmp_path, repo)
        try:
            svc.index_project(PROJECT, agent=AGENT)
            result = svc.search_graph(PROJECT, SECRET_AWS_KEY, agent=AGENT)
            assert result["fallback_used"] is True  # the scan ran...
            assert result["results"] == []  # ...and issued NOTHING
            assert SECRET_AWS_KEY not in json.dumps(result)
        finally:
            svc.close()

    def test_poisoned_path_literal_never_issues(self, lit_indexed: CodeGraphService) -> None:
        # secret.py in the mini fixture is POISONED (PG3): the literal
        # scan sees the file on disk but the path never issues content.
        result = lit_indexed.search_graph(PROJECT, SECRET_AWS_KEY, agent=AGENT)
        assert result["fallback_used"] is True
        assert result["results"] == []
        assert SECRET_AWS_KEY not in json.dumps(result)

    def test_unregistered_project_refused_before_any_scan(self, tmp_path: Path) -> None:
        svc, _ = make_service(tmp_path, _hybrid_repo(tmp_path), register=False)
        try:
            with pytest.raises(GraphConfinementError):
                svc.search_graph(PROJECT, LITERAL_TOOL_NAME, agent=AGENT)
        finally:
            svc.close()

    def test_rest_search_twin_inherits_the_fallback(self, hybrid_rest_client: TestClient) -> None:
        # LINE_A..C are plain assignments — symbols, not graph nodes.
        resp = hybrid_rest_client.post(
            "/graph/search",
            json={"project_id": "restproj", "query": "LINE_C", "agent": "tester"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_matches"] == 1
        assert body["fallback_used"] is True
        row = body["results"][0]
        assert row["match_kind"] == "literal"
        assert row["path"] == "notes.py" and row["line"] == 3
        assert "gamma" in row["snippet"]


# ── literal scanner bounds (unit) ────────────────────────────────────────────


class TestScanLiteralBounds:
    def test_file_count_cap_sets_truncated(self, tmp_path: Path) -> None:
        root = tmp_path / "many"
        root.mkdir()
        for name in ("a.py", "b.py", "c.py"):
            (root / name).write_text(f'X = "{LITERAL_TOOL_NAME}"\n', encoding="utf-8")
        matches, truncated = scan_literals(str(root), LITERAL_TOOL_NAME, max_files=2)
        assert truncated is True
        assert [m.path for m in matches] == ["a.py", "b.py"]

    def test_time_budget_cap_sets_truncated(self, tmp_path: Path) -> None:
        root = tmp_path / "timed"
        root.mkdir()
        (root / "a.py").write_text(f'X = "{LITERAL_TOOL_NAME}"\n', encoding="utf-8")
        matches, truncated = scan_literals(str(root), LITERAL_TOOL_NAME, time_budget_sec=0.0)
        assert matches == [] and truncated is True

    def test_snippet_line_is_capped(self, tmp_path: Path) -> None:
        root = tmp_path / "long"
        root.mkdir()
        long_line = f'{LITERAL_TOOL_NAME} = "{"x" * 500}"\n'
        (root / "long.py").write_text(long_line, encoding="utf-8")
        matches, truncated = scan_literals(str(root), LITERAL_TOOL_NAME)
        assert truncated is False
        assert len(matches[0].snippet) == LITERAL_SNIPPET_MAX_CHARS
        assert matches[0].snippet.startswith(LITERAL_TOOL_NAME)

    def test_binary_and_unreadable_files_skipped(self, tmp_path: Path) -> None:
        root = tmp_path / "bin"
        root.mkdir()
        (root / "blob.py").write_bytes(b"\x00\x01\x02" + LITERAL_TOOL_NAME.encode() + b"\x00")
        matches, truncated = scan_literals(str(root), LITERAL_TOOL_NAME)
        assert matches == [] and truncated is False


# ── trace_path: bare-tail disambiguation ─────────────────────────────────────


class TestTraceTailResolution:
    def test_unique_tail_traces_directly(self, tmp_path: Path) -> None:
        repo = _dup_repo(tmp_path, ambiguous=False)
        svc, _ = make_service(tmp_path, repo)
        try:
            svc.index_project(PROJECT, agent=AGENT)
            result = svc.trace_path(PROJECT, "update_fields", agent=AGENT)
            # Substring noise (update_fields_util/handler) does not
            # block the unique tail — it traces directly.
            assert "candidates" not in result
            assert result["start"] == "A.update_fields"
            assert result["nodes"][0]["depth"] == 0
        finally:
            svc.close()

    def test_ambiguous_tail_returns_candidates_not_error(self, tmp_path: Path) -> None:
        repo = _dup_repo(tmp_path, ambiguous=True)
        svc, _ = make_service(tmp_path, repo)
        try:
            svc.index_project(PROJECT, agent=AGENT)
            result = svc.trace_path(PROJECT, "update_fields", agent=AGENT)
            assert result["candidates"] is True
            assert result["candidate_count"] == 2
            assert len(result["candidate_list"]) == 2
            qnames = {row["qname"] for row in result["candidate_list"]}
            assert qnames == {"A.update_fields", "B.update_fields"}
            for row in result["candidate_list"]:
                assert {"qname", "kind", "path", "start_line", "end_line"} <= set(row)
            assert "qualified name" in result["hint"]
            # A helpful payload, NOT an error-shaped trace: no trace keys.
            assert "start" not in result and "nodes" not in result and "edges" not in result
        finally:
            svc.close()

    def test_candidates_capped_at_ten(self, tmp_path: Path) -> None:
        root = tmp_path / "capped"
        root.mkdir()
        (root / "pyproject.toml").write_text("[project]\nname = 'capped'\n", encoding="utf-8")
        for i in range(12):
            (root / f"m{i:02d}.py").write_text(
                f"class C{i:02d}:\n    def dup_tail(self):\n        return 1\n",
                encoding="utf-8",
            )
        svc, _ = make_service(tmp_path, root)
        try:
            svc.index_project(PROJECT, agent=AGENT)
            result = svc.trace_path(PROJECT, "dup_tail", agent=AGENT)
            assert result["candidates"] is True
            assert result["candidate_count"] == 12
            assert len(result["candidate_list"]) == 10
        finally:
            svc.close()

    def test_identical_qname_collision_returns_candidates_not_first_pick(
        self, tmp_path: Path
    ) -> None:
        # Card vesma-graph-roughness-repeat1: the same top-level function
        # name in two files shares ONE qname — trace_path must answer the
        # ambiguity contract (candidates), never silently trace the first.
        repo = _collision_repo(tmp_path)
        svc, _ = make_service(tmp_path, repo)
        try:
            svc.index_project(PROJECT, agent=AGENT)
            result = svc.trace_path(PROJECT, "reload", agent=AGENT)
            assert result["candidates"] is True
            assert result["candidate_count"] == 2
            qnames = {row["qname"] for row in result["candidate_list"]}
            assert qnames == {"reload"}  # ONE qname, several files
            paths = [row["path"] for row in result["candidate_list"]]
            assert paths == ["a.py", "b.py"]  # deterministic (path, line) order
            for row in result["candidate_list"]:
                assert {"qname", "kind", "path", "start_line", "end_line"} <= set(row)
            # The default tail hint is a dead end here — the collision
            # hint points at the path/line disambiguator.
            assert "identical-qname collision" in result["hint"]
            # A helpful payload, NOT an error-shaped trace: no trace keys.
            assert "start" not in result and "nodes" not in result and "edges" not in result
        finally:
            svc.close()

    def test_unique_exact_qname_still_traces_despite_collision_check(self, tmp_path: Path) -> None:
        # The collision pre-check must not disturb the pre-W-H exact
        # path: a unique qname traces byte-identically (same pin as
        # test_exact_qname_output_byte_identical_to_unique_tail, on the
        # collision fixture's sibling).
        repo = _collision_repo(tmp_path)
        (repo / "c.py").write_text("def unique_name():\n    return 3\n", encoding="utf-8")
        svc, _ = make_service(tmp_path, repo)
        try:
            svc.index_project(PROJECT, agent=AGENT)
            result = svc.trace_path(PROJECT, "unique_name", agent=AGENT)
            assert "candidates" not in result
            assert result["start"] == "unique_name"
            assert result["nodes"][0]["depth"] == 0
        finally:
            svc.close()

    def test_missing_symbol_stays_clear_not_found(self, lit_indexed: CodeGraphService) -> None:
        with pytest.raises(GraphToolError, match="not found in the project graph"):
            lit_indexed.trace_path(PROJECT, "no.such.symbol", agent=AGENT)

    def test_exact_qname_output_byte_identical_to_unique_tail(self, tmp_path: Path) -> None:
        repo = _dup_repo(tmp_path, ambiguous=False)
        svc, _ = make_service(tmp_path, repo)
        try:
            svc.index_project(PROJECT, agent=AGENT)
            by_exact = svc.trace_path(PROJECT, "A.update_fields", agent=AGENT)
            # Pre-W-H output shape is pinned key-for-key.
            assert set(by_exact) == {
                "project",
                "start",
                "depth",
                "nodes",
                "edges",
                "truncated",
                "cursor",
                "has_more",
                "last_indexed_at",
            }
            # Bare tail resolves to the SAME symbol → identical payload.
            by_tail = svc.trace_path(PROJECT, "update_fields", agent=AGENT)
            assert by_tail == by_exact
        finally:
            svc.close()
