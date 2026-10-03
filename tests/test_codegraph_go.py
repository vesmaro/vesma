"""Integration tests for Go language support in the code graph (#470).

Same discipline as the indexer suite: no tree-sitter mocks — a real
Go fixture project (``tests/data/codegraph_go_project``, a mesh-shaped
module with two packages, a struct embedding, an interface, same-package
and package-qualified calls) is indexed by the REAL parser stack:

* node kinds/qnames: Function/Method/Class with dotted package-path
  qnames (``pkg.util.Greeter.Greet``);
* call honesty: same-package and ``pkg.Ident`` calls are proven CALLS,
  method calls are USES heuristic, external packages get NO edge;
* INHERITS via struct embedding, IMPORTS to the package representative,
  TESTS to the same-package module;
* PG1 (no source bytes/comments/strings in the store) and PG3
  (secret poisoning) hold for Go sources;
* end-to-end through the service: search/outline/trace/coverage.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_go", reason="code-graph Go pack not installed")

from vesma.codegraph.file_surface import FileSurface
from vesma.codegraph.incremental import index_project
from vesma.codegraph.languages import GO, language_for_path
from vesma.codegraph.service import CodeGraphService
from vesma.config import CodeGraphConfig
from vesma.storage.code_graph_store import CodeGraphStore

SECRET_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"

AGENT = "test-agent"
PROJECT = "goproject"

#: The fixture lives in-repo; every test indexes its own COPY so
#: mutating tests (poison/touch) never touch the repo tree.
FIXTURE = Path(__file__).parent / "data" / "codegraph_go_project"


@pytest.fixture
def go_repo(tmp_path: Path) -> Path:
    root = tmp_path / "gorepo"
    shutil.copytree(FIXTURE, root)
    return root


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def store(data_dir: Path) -> Iterator[CodeGraphStore]:
    s = CodeGraphStore(data_dir)
    yield s
    s.close()


@dataclass
class FakeProject:
    id: str
    name: str
    paths: list[str] = field(default_factory=list)
    description: str = ""


class FakeMainStore:
    """Main-DB surface the service is allowed to see (PG2 registration)."""

    def __init__(self, *projects: FakeProject) -> None:
        self.projects = list(projects)
        self.meta: dict[str, str] = {}

    def get_project(self, project_id: str) -> FakeProject | None:
        return next((p for p in self.projects if p.id == project_id), None)

    def get_project_by_name(self, name: str) -> FakeProject | None:
        return next((p for p in self.projects if p.name == name), None)

    def list_projects(self) -> list[FakeProject]:
        return list(self.projects)

    def save_project(self, project: FakeProject) -> None:
        for i, existing in enumerate(self.projects):
            if existing.id == project.id:
                self.projects[i] = project
                return
        self.projects.append(project)

    def get_meta(self, key: str) -> str | None:
        return self.meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value


@pytest.fixture
def service(tmp_path: Path, go_repo: Path) -> Iterator[CodeGraphService]:
    main = FakeMainStore(FakeProject(id="p-go", name=PROJECT, paths=[str(go_repo)]))
    svc = CodeGraphService(main, tmp_path / "data", CodeGraphConfig(enabled=True))  # type: ignore[arg-type]
    yield svc
    svc.close()


def _rows(store: CodeGraphStore, query: str, params: tuple[str, ...] = ()) -> list[sqlite3.Row]:
    return store._conn().execute(query, params).fetchall()


def _edges(store: CodeGraphStore, kind: str) -> set[tuple[str, str]]:
    return {
        (str(r["from_id"]), str(r["to_id"]))
        for r in _rows(store, "SELECT from_id, to_id FROM project_edges WHERE kind=?", (kind,))
    }


# ── surface & registry ───────────────────────────────────────────────────────


class TestGoSurface:
    def test_go_extension_is_registered(self) -> None:
        spec = language_for_path("pkg/util/greeter.go")
        assert spec is not None and spec is GO
        assert language_for_path("go.mod") is None  # manifest: never indexed

    def test_fixture_surface_is_go_only(self, go_repo: Path) -> None:
        surface = FileSurface(go_repo).collect()
        rels = {sf.rel_path for sf in surface}
        assert rels == {
            "pkg/util/greeter.go",
            "pkg/util/extra.go",
            "pkg/app/server.go",
            "pkg/app/server_test.go",
        }
        for sf in surface:
            spec = language_for_path(sf.rel_path)
            assert spec is not None and spec.name == "go"


# ── index: nodes, qnames, edges ──────────────────────────────────────────────


class TestGoIndex:
    def test_kinds_counts_and_qnames(self, store: CodeGraphStore, go_repo: Path) -> None:
        result = index_project(PROJECT, go_repo, store, FakeMainStore())
        assert result.status == "ok"
        conn = store._conn()
        kinds = dict(
            conn.execute("SELECT kind, COUNT(*) FROM project_nodes GROUP BY kind").fetchall()
        )
        assert kinds["Project"] == 1
        assert kinds["File"] == 4
        assert kinds["Module"] == 4
        assert kinds["Class"] == 4  # Speaker, Base, Greeter, Server
        assert kinds["Function"] == 5  # NewGreeter, Shout, MakeTwo, Run, TestRun
        assert kinds["Method"] == 3  # Describe, Greet, Handle
        qnames = {
            str(r["qname"]) for r in _rows(store, "SELECT qname FROM project_nodes WHERE lang='go'")
        }
        # Package-path qname convention (#470).
        assert "pkg.util.Greeter" in qnames
        assert "pkg.util.Greeter.Greet" in qnames
        assert "pkg.app.Server.Handle" in qnames
        # Module qname is the package directory.
        assert "pkg/util" in qnames

    def test_signatures_are_shape_only(self, store: CodeGraphStore, go_repo: Path) -> None:
        index_project(PROJECT, go_repo, store, FakeMainStore())
        sig = (
            store._conn()
            .execute("SELECT signature FROM project_nodes WHERE qname='pkg.util.Greeter.Greet'")
            .fetchone()
        )
        assert sig is not None
        text = str(sig["signature"])
        assert text == "(name string)"  # identifiers + type names only
        shout = (
            store._conn()
            .execute("SELECT signature FROM project_nodes WHERE qname='pkg.util.Shout'")
            .fetchone()
        )
        assert shout is not None
        assert str(shout["signature"]) == "(g Greeter, name string)"

    def test_same_package_call_is_proven(self, store: CodeGraphStore, go_repo: Path) -> None:
        """Go package scope proves plain-identifier calls — including
        ACROSS FILES of one package (MakeTwo in extra.go -> NewGreeter
        in greeter.go) and from the test file (TestRun -> Run)."""
        index_project(PROJECT, go_repo, store, FakeMainStore())
        calls = _edges(store, "CALLS")
        assert (
            f"{len(PROJECT)}:{PROJECT}#pkg/util/extra.go#MakeTwo#4",
            f"{len(PROJECT)}:{PROJECT}#pkg/util/greeter.go#NewGreeter#25",
        ) in calls
        assert (
            f"{len(PROJECT)}:{PROJECT}#pkg/app/server_test.go#TestRun#5",
            f"{len(PROJECT)}:{PROJECT}#pkg/app/server.go#Run#18",
        ) in calls

    def test_qualified_call_is_proven(self, store: CodeGraphStore, go_repo: Path) -> None:
        """``util.NewGreeter(...)`` with a resolved import -> proven
        CALLS across packages."""
        index_project(PROJECT, go_repo, store, FakeMainStore())
        calls = _edges(store, "CALLS")
        assert (
            f"{len(PROJECT)}:{PROJECT}#pkg/app/server.go#Handle#13",
            f"{len(PROJECT)}:{PROJECT}#pkg/util/greeter.go#NewGreeter#25",
        ) in calls

    def test_external_package_gets_no_edge(self, store: CodeGraphStore, go_repo: Path) -> None:
        """fmt.Println / fmt.Sprintf resolve to nothing — no fictional
        edges to stdlib; no IMPORTS edge for ``fmt`` either."""
        index_project(PROJECT, go_repo, store, FakeMainStore())
        rows = _rows(
            store,
            "SELECT to_id FROM project_edges WHERE kind IN ('CALLS','USES')",
        )
        targets = {str(r["to_id"]) for r in rows}
        assert not any("fmt" in t for t in targets)
        imports = _edges(store, "IMPORTS")
        assert not any("fmt" in t for t in {b for _, b in imports})

    def test_method_call_is_heuristic_uses(self, store: CodeGraphStore, go_repo: Path) -> None:
        """``g.Greet(...)`` / ``s.Handle(...)``: the receiver type is not
        inferred, so a name match is USES heuristic — never CALLS."""
        index_project(PROJECT, go_repo, store, FakeMainStore())
        heuristic = {(a, b) for a, b in _edges(store, "USES")}
        proven = _edges(store, "CALLS")
        greet_id = f"{len(PROJECT)}:{PROJECT}#pkg/util/greeter.go#Greet#29"
        assert any(b == greet_id for a, b in heuristic)
        assert not any(b == greet_id for a, b in proven)

    def test_type_refs_from_composite_literals(self, store: CodeGraphStore, go_repo: Path) -> None:
        """``&Server{…}`` / ``&Greeter{…}`` / nested ``Base{…}`` land as
        heuristic USES on the named types."""
        index_project(PROJECT, go_repo, store, FakeMainStore())
        uses_from_run = {
            b
            for a, b in _edges(store, "USES")
            if a == f"{len(PROJECT)}:{PROJECT}#pkg/app/server.go#Run#18"
        }
        assert f"{len(PROJECT)}:{PROJECT}#pkg/app/server.go#Server#9" in uses_from_run
        uses_from_new = {
            b
            for a, b in _edges(store, "USES")
            if a == f"{len(PROJECT)}:{PROJECT}#pkg/util/greeter.go#NewGreeter#25"
        }
        assert f"{len(PROJECT)}:{PROJECT}#pkg/util/greeter.go#Greeter#20" in uses_from_new
        assert f"{len(PROJECT)}:{PROJECT}#pkg/util/greeter.go#Base#12" in uses_from_new

    def test_inherits_via_struct_embedding(self, store: CodeGraphStore, go_repo: Path) -> None:
        index_project(PROJECT, go_repo, store, FakeMainStore())
        inherits = _edges(store, "INHERITS")
        assert (
            f"{len(PROJECT)}:{PROJECT}#pkg/util/greeter.go#Greeter#20",
            f"{len(PROJECT)}:{PROJECT}#pkg/util/greeter.go#Base#12",
        ) in inherits

    def test_imports_edge_lands_on_package_representative(
        self, store: CodeGraphStore, go_repo: Path
    ) -> None:
        """``example.com/mesh/pkg/util`` matches the pkg/util directory;
        the edge lands on the package's first (sorted) file's module."""
        index_project(PROJECT, go_repo, store, FakeMainStore())
        imports = _edges(store, "IMPORTS")
        assert imports == {
            (
                f"{len(PROJECT)}:{PROJECT}#pkg/app/server.go##0#module",
                f"{len(PROJECT)}:{PROJECT}#pkg/util/extra.go##0#module",
            )
        }

    def test_tests_edge_within_package(self, store: CodeGraphStore, go_repo: Path) -> None:
        index_project(PROJECT, go_repo, store, FakeMainStore())
        tests = _edges(store, "TESTS")
        assert (
            f"{len(PROJECT)}:{PROJECT}#pkg/app/server_test.go##0#module",
            f"{len(PROJECT)}:{PROJECT}#pkg/app/server.go##0#module",
        ) in tests


# ── PG invariants on Go sources ──────────────────────────────────────────────


class TestGoPG:
    def test_pg1_no_source_bytes_in_store(self, store: CodeGraphStore, go_repo: Path) -> None:
        index_project(PROJECT, go_repo, store, FakeMainStore())
        conn = store._conn()
        # Comment text, string literals, the import path: none of it may
        # appear in ANY text column (PG1 is language-agnostic).
        forbidden = [
            "greeting helpers",
            "HI %s",
            "world",
            "example.com/mesh",
            "satisfies implicitly",
        ]
        tables = ["project_nodes", "project_edges", "graph_files", "graph_meta"]
        for table in tables:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            for row in rows:
                dumped = json.dumps([str(v) for v in row], ensure_ascii=False)
                for marker in forbidden:
                    assert marker not in dumped, f"{marker} leaked into {table}: {dumped}"

    def test_poisoned_go_file(self, store: CodeGraphStore, go_repo: Path) -> None:
        (go_repo / "leak.go").write_text(
            f'const AWSID = "{SECRET_AWS_KEY}"\n\nfunc X() string {{ return AWSID }}\n',
            encoding="utf-8",
        )
        result = index_project(PROJECT, go_repo, store, FakeMainStore())
        assert result.poisoned == ["leak.go"]
        rec = (
            store._conn()
            .execute("SELECT parse_ok, parse_error FROM graph_files WHERE path='leak.go'")
            .fetchone()
        )
        assert rec is not None
        assert rec["parse_ok"] == 0 and rec["parse_error"] == "secret-detected"

    def test_parse_error_is_honest(self, store: CodeGraphStore, go_repo: Path) -> None:
        (go_repo / "broken.go").write_text(
            "package util\n\nfunc Broken( {this is not go\n", encoding="utf-8"
        )
        result = index_project(PROJECT, go_repo, store, FakeMainStore())
        assert result.status == "ok"
        assert result.parse_errors["broken.go"] == "syntax-error"
        rec = (
            store._conn()
            .execute("SELECT parse_ok, parse_error FROM graph_files WHERE path='broken.go'")
            .fetchone()
        )
        assert rec is not None
        assert rec["parse_ok"] == 0 and rec["parse_error"] == "syntax-error"


class TestGoIncremental:
    def test_touch_reindexes_go_file(self, store: CodeGraphStore, go_repo: Path) -> None:
        main = FakeMainStore()
        first = index_project(PROJECT, go_repo, store, main)
        assert first.status == "ok"
        greeter = go_repo / "pkg" / "util" / "greeter.go"
        greeter.write_text(
            greeter.read_text(encoding="utf-8") + "\nfunc LateAddition() int { return 1 }\n",
            encoding="utf-8",
        )
        second = index_project(PROJECT, go_repo, store, main)
        assert second.status == "ok"
        assert second.incremental is True
        qnames = {
            str(r["qname"]) for r in _rows(store, "SELECT qname FROM project_nodes WHERE lang='go'")
        }
        assert "pkg.util.LateAddition" in qnames


# ── end-to-end through the service ──────────────────────────────────────────


class TestGoService:
    def test_search_and_outline(self, service: CodeGraphService) -> None:
        result = service.index_project(PROJECT, agent=AGENT, session="s1")
        assert result["status"] == "ok"
        hits = service.search_graph(PROJECT, "Greeter", agent=AGENT, session="s1")
        # the class, its method, the ctor and the fixture path all match
        assert hits["total_matches"] >= 2
        names = {row["qname"] for row in hits["results"]}
        assert "pkg.util.Greeter" in names
        assert all(row["match_kind"] == "symbol" for row in hits["results"])
        outline = service.get_file_outline(
            PROJECT, "pkg/util/greeter.go", agent=AGENT, session="s1"
        )
        assert outline["lang"] == "go"
        assert outline["parse_error"] is None
        kinds = {row["name"]: row["kind"] for row in outline["outline"]}
        assert kinds["NewGreeter"] == "Function"
        assert kinds["Greet"] == "Method"
        assert kinds["Speaker"] == "Class"
        sig = {row["name"]: row.get("signature") for row in outline["outline"]}
        assert sig["Shout"] == "(g Greeter, name string)"

    def test_trace_and_coverage(self, service: CodeGraphService, go_repo: Path) -> None:
        service.index_project(PROJECT, agent=AGENT, session="s1")
        trace = service.trace_path(PROJECT, "pkg.app.Run", depth=2, agent=AGENT, session="s1")
        visited = {node["qname"] for node in trace["nodes"]}
        assert "pkg.app.Run" in visited
        # depth 1: the composite-literal type + the method call
        assert "pkg.app.Server" in visited
        assert "pkg.app.Server.Handle" in visited
        # depth 2: Handle's proven CALLS reaches the other package
        assert "pkg.util.NewGreeter" in visited
        coverage = service.check_coverage(
            PROJECT,
            ["pkg/app/server.go", "pkg/util/greeter.go", "go.mod"],
            agent=AGENT,
            session="s1",
        )
        verdicts = {v["path"]: v["verdict"] for v in coverage["coverage"]}
        assert verdicts["pkg/app/server.go"] == "indexed"
        assert verdicts["pkg/util/greeter.go"] == "indexed"
        assert verdicts["go.mod"] == "unindexed"  # manifest: real file, never in the surface
