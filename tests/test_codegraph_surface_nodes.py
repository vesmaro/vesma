"""Integration tests for the surface node-source extension (card
vesma-graph-command-route-nodes).

The suite pins the extension contract at FOUR layers:

* the generic seam (:mod:`vesma.codegraph.node_sources`): idempotent
  registration, non-applying roots are skipped silently, a failing
  source degrades to NO contribution — an extension never breaks an
  index;
* the store (schema v2): the new ``Command``/``Route`` node kinds and
  ``INVOKES``/``HANDLES`` edge kinds pass the CHECKs on a fresh file,
  and an OLD (v1) sidecar migrates IN PLACE losslessly — no silent
  graph emptying, no forced reindex;
* the concrete vesma surface (:mod:`vesma.graph_surface_ext`): a
  marker-detected vesma repo gets Command/Route nodes from the LIVE
  engine registries (the completion engine's click tree, FastAPI
  ``app.routes``), a foreign repo gets NONE (no pollution), handlers
  bind with INVOKES/HANDLES edges only when they resolve (unique suffix
  match — the src-layout ``src.`` prefix case), and the node metadata
  stays COMPACT (one-line help, option count, ≤16 params — the
  ~300-token search answer);
* the tool surface: ``project_graph_status`` reports the per-kind
  breakdown, ``get_graph_schema`` reports schema v2.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("tree_sitter_python", reason="code-graph extra not installed")

from vesma.codegraph.incremental import index_project
from vesma.codegraph.node_sources import (
    ProjectNodeSource,
    SourceContribution,
    aux_node_id,
    clear_node_sources,
    node_sources,
    register_node_source,
    run_node_sources,
)
from vesma.codegraph.service import GRAPH_SCHEMA_VERSION, CodeGraphService
from vesma.config import CodeGraphConfig
from vesma.graph_surface_ext import (
    SURFACE_SOURCE,
    _first_line,
    _resolve_handler,
    _safe,
)
from vesma.storage.code_graph_store import (
    SCHEMA_VERSION,
    CodeGraphEdge,
    CodeGraphNode,
    CodeGraphStore,
)

AGENT = "surface-under-test"

# The canonical AWS sample the secrets detector flags (the detector
# behaviour is NOT under test here — only that a hit drops the string).
SECRET_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


# ── fixtures ─────────────────────────────────────────────────────────────────


class _FakeMainStore:
    """Minimal main-store meta surface (slice-1 MainStoreMeta twin)."""

    def __init__(self) -> None:
        self.meta: dict[str, str] = {}

    def get_meta(self, key: str) -> str | None:
        return self.meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Isolate the module-level source registry: every test here runs
    with the host registration in place (mirroring the wiring), and
    NOTHING leaks into the other suites (whose fixtures carry no
    markers, but the hygiene stays explicit)."""
    clear_node_sources()
    from vesma.graph_surface_ext import register_surface_source

    register_surface_source()
    yield
    clear_node_sources()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[CodeGraphStore]:
    s = CodeGraphStore(tmp_path / "data")
    yield s
    s.close()


@pytest.fixture
def vesma_repo(tmp_path: Path) -> Path:
    """A root carrying the engine's cli/api package markers — the
    detection contract under test (marker FILES, not a hardcoded path).
    The actual command/route payloads come from the LIVE engine
    registries; the fixture tree itself holds only the markers plus one
    parseable module."""
    root = tmp_path / "repo"
    (root / "src/vesma/cli").mkdir(parents=True)
    (root / "src/vesma/api").mkdir(parents=True)
    (root / "src/vesma/cli/main.py").write_text("# cli marker\n", encoding="utf-8")
    (root / "src/vesma/api/main.py").write_text("# api marker\n", encoding="utf-8")
    pkg = root / "src/pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname = 'marker-repo'\n", encoding="utf-8")
    return root


@pytest.fixture
def foreign_repo(tmp_path: Path) -> Path:
    """A perfectly ordinary repo: no engine markers anywhere."""
    root = tmp_path / "foreign"
    root.mkdir()
    (root / "main.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname = 'foreign'\n", encoding="utf-8")
    return root


@pytest.fixture
def vesma_store(vesma_repo: Path, tmp_path: Path) -> CodeGraphStore:
    s = CodeGraphStore(tmp_path / "vesma-data")
    index_project(
        "vesmarepo", vesma_repo, s, _FakeMainStore(), CodeGraphConfig(), incremental=False
    )
    return s


@pytest.fixture
def vesma_store_close(vesma_store: CodeGraphStore) -> Iterator[CodeGraphStore]:
    yield vesma_store
    vesma_store.close()


# ── the seam ─────────────────────────────────────────────────────────────────


class TestSeam:
    def test_registration_idempotent_by_name(self) -> None:
        before = len(node_sources())
        from vesma.graph_surface_ext import register_surface_source

        register_surface_source()
        assert len(node_sources()) == before  # replaced, not duplicated
        assert any(s.name == "vesma-surface" for s in node_sources())

    def test_non_applying_root_is_skipped_silently(self, foreign_repo: Path) -> None:
        result = run_node_sources("proj", foreign_repo, {})
        assert result.nodes == [] and result.edges == []

    def test_failing_source_degrades_to_no_contribution(self, tmp_path: Path) -> None:
        class _Boom:
            name = "boom"

            def applies(self, root: str | Any) -> bool:
                return True

            def contribute(
                self, project: str, root: str | Any, handler_ids: dict[str, str]
            ) -> SourceContribution:
                raise RuntimeError("boom")

        register_node_source(_Boom())
        result = run_node_sources("proj", tmp_path, {})
        # the boom source contributed nothing; the registered vesma
        # surface does not apply to a bare tmp_path either
        assert result.nodes == [] and result.edges == []

    def test_aux_node_id_is_length_prefixed(self) -> None:
        node_id = aux_node_id("p", "src/vesma/cli/main.py", "cli:vesma x")
        assert node_id == "1:p#src/vesma/cli/main.py#cli:vesma x#0"

    def test_protocol_structural_match(self) -> None:
        source: ProjectNodeSource = SURFACE_SOURCE
        assert source.name == "vesma-surface"


# ── the store: schema v2 kinds + the v1 migration ────────────────────────────


class TestStoreSchemaV2:
    def test_fresh_store_accepts_surface_kinds(self, store: CodeGraphStore) -> None:
        node = CodeGraphNode(
            id=aux_node_id("p", "src/vesma/cli/main.py", "cli:vesma x"),
            project="p",
            kind="Command",
            name="vesma x",
            qname="vesma x",
            path="src/vesma/cli/main.py",
            metadata={"help": "Do x.", "options": 2, "params": {"--y": "Why."}},
        )
        route = CodeGraphNode(
            id=aux_node_id("p", "src/vesma/api/main.py", "GET /h"),
            project="p",
            kind="Route",
            name="GET /h",
            qname="GET /h",
            path="src/vesma/api/main.py",
        )
        store.upsert_nodes([node, route])
        store.upsert_edges(
            [
                CodeGraphEdge(
                    from_id=node.id,
                    to_id=route.id,
                    kind="INVOKES",
                    provenance="surface-introspection",
                )
            ]
        )
        kinds = store.count_nodes_by_kind("p")
        assert kinds["Command"] == 1 and kinds["Route"] == 1

    def test_edge_kinds_invokes_and_handles_pass_check(self, store: CodeGraphStore) -> None:
        cmd = aux_node_id("p", "c.py", "cli:vesma x")
        route = aux_node_id("p", "a.py", "GET /h")
        fn = aux_node_id("p", "h.py", "handler")
        store.upsert_nodes(
            [
                CodeGraphNode(id=cmd, project="p", kind="Command", name="vesma x", qname="vesma x"),
                CodeGraphNode(id=route, project="p", kind="Route", name="GET /h", qname="GET /h"),
                CodeGraphNode(id=fn, project="p", kind="Function", name="handler", qname="handler"),
            ]
        )
        store.upsert_edges(
            [
                CodeGraphEdge(from_id=cmd, to_id=fn, kind="INVOKES"),
                CodeGraphEdge(from_id=route, to_id=fn, kind="HANDLES"),
            ]
        )
        assert store.count_edges("p") == 2

    @staticmethod
    def _make_v1_db(db_dir: Path) -> None:
        """A pre-v2 sidecar: the OLD CHECK constraints, one node and one
        edge already in it (the migration must not lose either)."""
        db_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_dir / "code_graph.db")
        conn.executescript(
            """
            CREATE TABLE project_nodes (
                id TEXT PRIMARY KEY, project TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN
                    ('Project','File','Module','Class','Function','Method','Type')),
                name TEXT NOT NULL, qname TEXT NOT NULL, path TEXT,
                start_line INTEGER, end_line INTEGER, lang TEXT,
                signature TEXT, metadata TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE project_edges (
                from_id TEXT NOT NULL REFERENCES project_nodes(id) ON DELETE CASCADE,
                to_id TEXT NOT NULL REFERENCES project_nodes(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK (kind IN
                    ('CONTAINS_FILE','DEFINES','IMPORTS','CALLS','INHERITS','TESTS','USES')),
                weight REAL NOT NULL DEFAULT 1.0,
                provenance TEXT NOT NULL DEFAULT 'tree-sitter',
                PRIMARY KEY (from_id, to_id, kind), CHECK (from_id <> to_id));
            CREATE TABLE graph_files (project TEXT NOT NULL, path TEXT NOT NULL,
                mtime REAL, size INTEGER, hash TEXT, parse_ok INTEGER,
                parse_error TEXT, indexed_at TEXT, PRIMARY KEY (project, path));
            CREATE TABLE graph_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE graph_audit (id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
                session TEXT, reason TEXT, details TEXT NOT NULL DEFAULT '{}',
                ts TEXT NOT NULL);
            INSERT INTO project_nodes VALUES
                ('p#f.py#sym#1', 'p', 'Function', 'sym', 'm.sym', 'f.py',
                 1, 2, 'python', NULL, '{}');
            INSERT INTO project_edges VALUES
                ('p#f.py#sym#1', 'p#f.py#sym#1x', 'CALLS', 1.0, 'tree-sitter');
            """
        )
        conn.commit()
        conn.close()

    def test_v1_store_migrates_losslessly(self, tmp_path: Path) -> None:
        db_dir = tmp_path / "v1"
        self._make_v1_db(db_dir)
        with CodeGraphStore(db_dir) as migrated:
            assert migrated.get_meta("schema_version") == str(SCHEMA_VERSION)
            # old rows survived the rebuild
            assert migrated.count_nodes("p") == 1
            assert migrated.count_edges("p") == 1
            # and the NEW kinds insert fine (the whole point of v2)
            migrated.upsert_nodes(
                [
                    CodeGraphNode(
                        id=aux_node_id("p", "cli.py", "cli:vesma x"),
                        project="p",
                        kind="Command",
                        name="vesma x",
                        qname="vesma x",
                    )
                ]
            )
            assert migrated.count_nodes_by_kind("p")["Command"] == 1

    def test_migration_is_idempotent_on_reopen(self, tmp_path: Path) -> None:
        db_dir = tmp_path / "v1"
        self._make_v1_db(db_dir)
        with CodeGraphStore(db_dir):
            pass
        with CodeGraphStore(db_dir) as again:
            assert again.get_meta("schema_version") == str(SCHEMA_VERSION)
            assert again.count_nodes("p") == 1


# ── the concrete vesma surface ───────────────────────────────────────────────


class TestApplies:
    def test_markers_decide(self, vesma_repo: Path, foreign_repo: Path) -> None:
        assert SURFACE_SOURCE.applies(vesma_repo) is True
        assert SURFACE_SOURCE.applies(foreign_repo) is False

    def test_cli_marker_alone_applies(self, tmp_path: Path) -> None:
        root = tmp_path / "cli-only"
        (root / "vesma/cli").mkdir(parents=True)  # flat layout, not src/
        (root / "vesma/cli/main.py").write_text("", encoding="utf-8")
        assert SURFACE_SOURCE.applies(root) is True


class TestCommandNodes:
    def test_vesma_repo_grows_command_nodes(self, vesma_store: CodeGraphStore) -> None:
        kinds = vesma_store.count_nodes_by_kind("vesmarepo")
        assert kinds.get("Command", 0) > 50  # the live tree: ~40+ commands + groups

    def test_search_finds_graph_delete(self, vesma_store: CodeGraphStore) -> None:
        matches = vesma_store.search_nodes("vesmarepo", "graph delete", kind="Command")
        assert len(matches) == 1
        node = matches[0]
        assert node.name == "vesma graph delete"
        assert node.qname == "vesma graph delete"
        assert node.path == "src/vesma/cli/main.py"
        meta = node.metadata or {}
        assert isinstance(meta["options"], int) and meta["options"] >= 1
        assert "--force" in meta["params"]
        # one-line help only (the compaction contract), string-typed
        assert isinstance(meta["help"], str)
        assert "\n" not in meta["help"]
        # the whole row stays compact (the ~300-token answer budget)
        row_bytes = len(json.dumps(node.metadata, ensure_ascii=False).encode("utf-8"))
        assert row_bytes <= 1600

    def test_params_listed_are_capped(self, vesma_store: CodeGraphStore) -> None:
        rows = vesma_store.search_nodes("vesmarepo", "vesma ", kind="Command", limit=200)
        assert rows
        for node in rows:
            params = (node.metadata or {}).get("params", {})
            assert len(params) <= 17  # 16 listed + the "... +N" overflow marker

    def test_foreign_repo_gets_no_surface_nodes(self, foreign_repo: Path, tmp_path: Path) -> None:
        store = CodeGraphStore(tmp_path / "foreign-data")
        try:
            index_project(
                "foreign",
                foreign_repo,
                store,
                _FakeMainStore(),
                CodeGraphConfig(),
                incremental=False,
            )
            kinds = store.count_nodes_by_kind("foreign")
            assert "Command" not in kinds and "Route" not in kinds
        finally:
            store.close()


class TestRouteNodes:
    def test_metrics_route_is_a_node(self, vesma_store: CodeGraphStore) -> None:
        matches = vesma_store.search_nodes("vesmarepo", "/api/v1/metrics", kind="Route")
        assert len(matches) == 1
        node = matches[0]
        assert node.name == "GET /api/v1/metrics"
        meta = node.metadata or {}
        assert meta["method"] == "GET"
        assert meta["path"] == "/api/v1/metrics"
        assert meta["endpoint"] == "vesma.api.main.prometheus_metrics"

    def test_route_volume_matches_live_app(self, vesma_store: CodeGraphStore) -> None:
        kinds = vesma_store.count_nodes_by_kind("vesmarepo")
        assert kinds.get("Route", 0) > 40  # the live REST surface: ~52 routes


class TestHandlerBinding:
    """Store qnames are FILE-scoped — binding is by exact address
    (marker-derived layout prefix + module → rel_path, qualname →
    qname), never a cross-file suffix guess."""

    SRC_SYMBOLS: Mapping[str, Mapping[str, str]] = {
        "src/vesma/cli/graph_cmd.py": {"delete_cmd": "node-1"},
        "src/vesma/api/main.py": {"prometheus_metrics": "node-2"},
    }

    def test_src_layout_address_resolves(self) -> None:
        assert (
            _resolve_handler(self.SRC_SYMBOLS, "vesma.cli.graph_cmd", "delete_cmd", "src/")
            == "node-1"
        )
        assert (
            _resolve_handler(self.SRC_SYMBOLS, "vesma.api.main", "prometheus_metrics", "src/")
            == "node-2"
        )

    def test_flat_layout_address_resolves(self) -> None:
        symbols = {"vesma/cli/graph_cmd.py": {"delete_cmd": "node-1"}}
        assert _resolve_handler(symbols, "vesma.cli.graph_cmd", "delete_cmd", "") == "node-1"

    def test_unknown_file_binds_nothing(self) -> None:
        assert _resolve_handler({}, "vesma.cli.graph_cmd", "delete_cmd", "src/") is None

    def test_wrong_name_binds_nothing(self) -> None:
        symbols = {"src/vesma/cli/graph_cmd.py": {"other": "node-1"}}
        assert _resolve_handler(symbols, "vesma.cli.graph_cmd", "delete_cmd", "src/") is None

    def test_locals_qualname_binds_nothing(self) -> None:
        assert _resolve_handler({"a.py": {"g": "1"}}, "x", "f.<locals>.g") is None

    def test_missing_module_binds_nothing(self) -> None:
        assert _resolve_handler({"a.py": {"g": "1"}}, None, "g") is None

    def test_contribute_binds_edges_to_given_handlers(self, vesma_repo: Path) -> None:
        from vesma.storage.code_graph_store import CodeGraphEdge as Edge

        symbols = {
            "src/vesma/cli/graph_cmd.py": {"delete_cmd": "handler-node"},
            "src/vesma/api/main.py": {"prometheus_metrics": "endpoint-node"},
        }
        contribution = SURFACE_SOURCE.contribute("p", vesma_repo, symbols)
        assert contribution.nodes
        invokes = [e for e in contribution.edges if isinstance(e, Edge) and e.kind == "INVOKES"]
        handles = [e for e in contribution.edges if isinstance(e, Edge) and e.kind == "HANDLES"]
        assert any(e.to_id == "handler-node" for e in invokes)
        assert any(e.to_id == "endpoint-node" for e in handles)
        # provenance marks the extension origin (never 'tree-sitter')
        assert all(e.provenance == "surface-introspection" for e in contribution.edges)


class TestCompactionGuards:
    def test_first_line_and_secret_drop(self) -> None:
        assert _first_line("First line.\nSecond line.") == "First line."
        assert _first_line(None) == ""
        assert _first_line("   ") == ""
        assert _safe(f"token {SECRET_AWS_KEY} leaked") == ""
        assert _safe("safe text") == "safe text"
        assert _safe("") == ""

    def test_indexed_help_is_first_line_of_live_help(self, vesma_store: CodeGraphStore) -> None:
        import typer.main

        from vesma.cli.main import app

        root = typer.main.get_command(app)
        graph_group = root.commands["graph"]
        expected = (graph_group.commands["delete"].help or "").strip().splitlines()[0]
        node = vesma_store.search_nodes("vesmarepo", "graph delete", kind="Command")[0]
        assert (node.metadata or {})["help"] == expected


# ── the tool surface ─────────────────────────────────────────────────────────


class TestServiceSurface:
    def test_status_reports_kind_breakdown(self, vesma_repo: Path, tmp_path: Path) -> None:
        service = _make_service(tmp_path, vesma_repo)
        try:
            result = service.index_project("vesmarepo", agent=AGENT, session="s1")
            assert result["status"] == "ok"
            status = service.status("vesmarepo", agent=AGENT, session="s1")
            kinds = status["node_kinds"]
            assert kinds.get("Command", 0) > 0
            assert kinds.get("Route", 0) > 0
            assert sum(kinds.values()) == status["nodes"]
            schema = service.get_graph_schema("vesmarepo", agent=AGENT, session="s1")
            assert schema["schema_version"] == GRAPH_SCHEMA_VERSION == 2
            search = service.search_graph(
                "vesmarepo", "graph delete", agent=AGENT, session="s1", kind="Command"
            )
            assert search["total_matches"] == 1
            assert search["results"][0]["name"] == "vesma graph delete"
        finally:
            service.close()


# ── helpers ──────────────────────────────────────────────────────────────────


@dataclass
class _FakeProject:
    """Main-DB project record twin (paths[0] is the registered root)."""

    id: str
    name: str
    paths: list[str] = field(default_factory=list)


class _ServiceMainStore:
    """Main-DB surface the service is allowed to see (PG2 registration
    plus the meta surface the epoch helpers use) — the
    test_codegraph_tools fixture shape, trimmed."""

    def __init__(self, project: _FakeProject) -> None:
        self.project = project

    def get_project(self, project_id: str) -> _FakeProject | None:
        return self.project if project_id in (self.project.id, self.project.name) else None

    def get_project_by_name(self, name: str) -> _FakeProject | None:
        return self.project if name == self.project.name else None

    def list_projects(self) -> list[_FakeProject]:
        return [self.project]

    def save_project(self, project: object) -> None:
        return None

    def delete_project(self, project_id: str) -> bool:
        return False

    def get_meta(self, key: str) -> str | None:
        return None

    def set_meta(self, key: str, value: str) -> None:
        return None


def _make_service(tmp_path: Path, repo: Path) -> CodeGraphService:
    """A CodeGraphService over a registered fake project pointing at
    the marker repo."""
    main = _ServiceMainStore(_FakeProject(id="p-1", name="vesmarepo", paths=[str(repo)]))
    return CodeGraphService(
        main,
        tmp_path / "data",
        CodeGraphConfig(enabled=True),  # type: ignore[arg-type]
    )
