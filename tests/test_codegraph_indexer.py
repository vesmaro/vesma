"""Integration tests for the code graph indexer (ADR-0032 PG-0 slices 2+3).

No tree-sitter mocks: these are integration tests on a small on-disk
fixture tree — the parser stack is the system under test. The suite
pins the slice contract:

* node/edge kinds and the resolution heuristics (IMPORTS/INHERITS/
  CALLS/TESTS, USES honesty);
* PG1 (PGT-1): a dump check — NO source bytes, docstrings, comments
  or literal defaults anywhere in the sidecar store;
* PG3: surface denylist (.env, keys, vendored trees), the extension
  allowlist, symlink rejection, secret poisoning
  (``graph_files.parse_ok=0, parse_error='secret-detected'``);
* PG7 (PGT-6): a limit breach aborts the WHOLE index — the previous
  graph survives untouched;
* incrementality: unchanged tree → ``fresh`` with no epoch bump;
  touch/delete → the change lands, removed nodes are gone;
  ``staleness_check`` stays read-only.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_python", reason="code-graph extra not installed")

from vesmaro.codegraph.file_surface import FileSurface
from vesmaro.codegraph.incremental import (
    STATUS_FRESH,
    STATUS_IN_PROGRESS,
    index_project,
    staleness_check,
)
from vesmaro.codegraph.indexer import IndexLimitError, ProjectIndexer
from vesmaro.codegraph.languages import language_for_path
from vesmaro.config import CodeGraphConfig
from vesmaro.storage.code_graph_store import (
    CodeGraphStore,
    project_graph_epoch_key,
    read_project_graph_epoch,
)

SECRET_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"

DOC_STRING_SECRET = '"""Docs mention AKIAIOSFODNN7EXAMPLE."""'


class _FakeMainStore:
    """Minimal main-store meta surface (slice-1 MainStoreMeta twin)."""

    def __init__(self) -> None:
        self.meta: dict[str, str] = {}

    def get_meta(self, key: str) -> str | None:
        return self.meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def store(data_dir: Path) -> Iterator[CodeGraphStore]:
    s = CodeGraphStore(data_dir)
    yield s
    s.close()


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    """A small but complete project tree.

    Layout: package with a helper (class + function), an app importing
    it (inheritance + call), a test file (TESTS heuristic), a poisoned
    file (secret), denylist noise (.env, dotfile, vendored dir, key
    file, node_modules) and a symlink that must never be followed.
    """
    root = tmp_path / "repo"
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "helper.py").write_text(
        "class Base:\n    def greet(self, greeting='hi'):\n        return greeting\n\n"
        "def do_work(x: int) -> int:\n    return x\n",
        encoding="utf-8",
    )
    (pkg / "app.py").write_text(
        "from pkg.helper import Base, do_work\n\n"
        "class Klass(Base):\n    def m(self, x=5):\n        return do_work(x)\n",
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_helper.py").write_text(
        "from pkg.helper import do_work\n\ndef test_x():\n    assert do_work(1)\n",
        encoding="utf-8",
    )
    (root / "poisoned.py").write_text(
        f"API_KEY = '{SECRET_AWS_KEY}'\n\ndef leaky():\n    return API_KEY\n",
        encoding="utf-8",
    )
    (root / "docstring_secret.py").write_text(
        f"{DOC_STRING_SECRET}\n\ndef clean_fn(a, b=2):\n    return a\n",
        encoding="utf-8",
    )
    # Denylist noise — none of these may reach the index.
    (root / ".env").write_text("PASSWORD=supersecret\n", encoding="utf-8")
    (root / ".hidden.py").write_text("def hidden():\n    pass\n", encoding="utf-8")
    (root / "server.pem").write_text("PRIVATE KEY MATERIAL\n", encoding="utf-8")
    vendored = root / "vendor" / "ext"
    vendored.mkdir(parents=True)
    (vendored / "ext.py").write_text("def vendored():\n    pass\n", encoding="utf-8")
    nm = root / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.py").write_text("x = 1\n", encoding="utf-8")
    # Symlink into the package — must never be followed/indexed.
    (root / "link.py").symlink_to(pkg / "helper.py")
    return root


def _rows(store: CodeGraphStore, query: str, params: tuple = ()) -> list[sqlite3.Row]:
    return store._conn().execute(query, params).fetchall()


def _node_ids(store: CodeGraphStore, kind: str | None = None) -> set[str]:
    if kind:
        return {
            str(r["id"]) for r in _rows(store, "SELECT id FROM project_nodes WHERE kind=?", (kind,))
        }
    return {str(r["id"]) for r in _rows(store, "SELECT id FROM project_nodes")}


def _edges(store: CodeGraphStore, kind: str) -> set[tuple[str, str]]:
    return {
        (str(r["from_id"]), str(r["to_id"]))
        for r in _rows(store, "SELECT from_id, to_id FROM project_edges WHERE kind=?", (kind,))
    }


# ── surface (PG3) ────────────────────────────────────────────────────────────


class TestFileSurface:
    def test_allowlist_is_python_only_wave1(self, mini_repo: Path) -> None:
        surface = FileSurface(mini_repo).collect()
        for sf in surface:
            spec = language_for_path(sf.rel_path)
            assert spec is not None and spec.name == "python"

    def test_denylist_excludes_env_keys_dotfiles_vendored(self, mini_repo: Path) -> None:
        rels = {sf.rel_path for sf in FileSurface(mini_repo).collect()}
        assert ".env" not in rels
        assert ".hidden.py" not in rels
        assert "server.pem" not in rels
        assert "vendor/ext/ext.py" not in rels
        assert "node_modules/pkg/index.py" not in rels

    def test_symlink_is_never_indexed(self, mini_repo: Path) -> None:
        rels = {sf.rel_path for sf in FileSurface(mini_repo).collect()}
        assert "link.py" not in rels
        assert "pkg/helper.py" in rels  # the target itself, via the real path


# ── full index: nodes/edges ──────────────────────────────────────────────────


class TestFullIndex:
    def test_kinds_and_edges(self, store: CodeGraphStore, mini_repo: Path) -> None:
        result = index_project("proj", mini_repo, store, _FakeMainStore())
        assert result.status == "ok"
        assert result.incremental is False

        conn = store._conn()
        kinds = dict(
            conn.execute("SELECT kind, COUNT(*) FROM project_nodes GROUP BY kind").fetchall()
        )
        assert kinds["Project"] == 1
        assert kinds["File"] == 6  # __init__, helper, app, test, poisoned, docstring_secret
        assert kinds["Module"] == 6
        assert kinds["Class"] == 2  # Base, Klass
        assert kinds["Method"] == 2  # Base.greet, Klass.m
        assert kinds["Function"] == 4  # do_work, test_x, leaky, clean_fn

        imports = _edges(store, "IMPORTS")
        assert (
            f"{len('proj')}:proj#pkg/app.py##0#module",
            f"{len('proj')}:proj#pkg/helper.py##0#module",
        ) in imports
        assert (
            f"{len('proj')}:proj#tests/test_helper.py##0#module",
            f"{len('proj')}:proj#pkg/helper.py##0#module",
        ) in imports

        inherits = _edges(store, "INHERITS")
        assert (
            f"{len('proj')}:proj#pkg/app.py#Klass#3",
            f"{len('proj')}:proj#pkg/helper.py#Base#1",
        ) in inherits

        calls = _edges(store, "CALLS")
        assert (
            f"{len('proj')}:proj#pkg/app.py#m#4",
            f"{len('proj')}:proj#pkg/helper.py#do_work#5",
        ) in calls
        assert (
            f"{len('proj')}:proj#tests/test_helper.py#test_x#3",
            f"{len('proj')}:proj#pkg/helper.py#do_work#5",
        ) in calls

        tests = _edges(store, "TESTS")
        assert (
            f"{len('proj')}:proj#tests/test_helper.py##0#module",
            f"{len('proj')}:proj#pkg/helper.py##0#module",
        ) in tests

        contains = _edges(store, "CONTAINS_FILE")
        assert len(contains) == 6  # Project -> every indexed File

    def test_uses_is_heuristic_provenance(self, store: CodeGraphStore, tmp_path: Path) -> None:
        # A call head that matches NO top-level def and NO imported def
        # but a known in-file symbol: USES with provenance='heuristic'.
        root = tmp_path / "repo"
        root.mkdir()
        (root / "a.py").write_text(
            "class Thing:\n    def run(self):\n        return 1\n\n"
            "def user():\n    return Thing().run()\n",
            encoding="utf-8",
        )
        index_project("p", root, store, _FakeMainStore())
        conn = store._conn()
        uses = conn.execute(
            "SELECT from_id, to_id, provenance FROM project_edges WHERE kind='USES'"
        ).fetchall()
        # Thing() is a top-level class: the ctor call is CALLS; the
        # .run() attribute head resolves to no top-level def → USES.
        assert any(tuple(r) for r in uses)
        for row in uses:
            assert row["provenance"] == "heuristic"

    def test_unresolved_import_makes_no_edge(self, store: CodeGraphStore, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "main.py").write_text(
            "import os\nimport missing.module\n\ndef f():\n    return os.getcwd()\n",
            encoding="utf-8",
        )
        index_project("p", root, store, _FakeMainStore())
        assert _edges(store, "IMPORTS") == set()  # nothing resolves in-tree

    def test_relative_import_resolves(self, store: CodeGraphStore, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        pkg = root / "pkg"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "a.py").write_text("def fa():\n    return 1\n", encoding="utf-8")
        (pkg / "b.py").write_text(
            "from .a import fa\n\n\ndef fb():\n    return fa()\n", encoding="utf-8"
        )
        index_project("p", root, store, _FakeMainStore())
        assert (
            f"{len('p')}:p#pkg/b.py##0#module",
            f"{len('p')}:p#pkg/a.py##0#module",
        ) in _edges(store, "IMPORTS")


# ── PG1: zero source bytes (PGT-1, dump check) ──────────────────────────────


class TestZeroSourceBytes:
    def test_no_source_bytes_in_store(self, store: CodeGraphStore, mini_repo: Path) -> None:
        index_project("proj", mini_repo, store, _FakeMainStore())
        conn = store._conn()
        # Docstring secret, literal defaults, comment text: none of it
        # may appear in ANY text column of the sidecar. Parameter NAMES
        # are identifiers — the signature shape keeps them by contract;
        # their DEFAULT VALUES ('hi', 5, 2) must not survive.
        forbidden = [SECRET_AWS_KEY, "'hi'", "x=5", "b=2", "return x", "greeting="]
        tables = ["project_nodes", "project_edges", "graph_files", "graph_meta"]
        for table in tables:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            for row in rows:
                dumped = json.dumps([str(v) for v in row], ensure_ascii=False)
                for marker in forbidden:
                    assert marker not in dumped, f"{marker} leaked into {table}: {dumped}"

    def test_signature_has_no_defaults(self, store: CodeGraphStore, mini_repo: Path) -> None:
        index_project("proj", mini_repo, store, _FakeMainStore())
        conn = store._conn()
        sig = conn.execute("SELECT signature FROM project_nodes WHERE qname='clean_fn'").fetchone()
        assert sig is not None
        text = str(sig["signature"])
        assert "b" in text  # the identifier survives
        assert "2" not in text  # the default value does not


# ── PG3: poisoned files ─────────────────────────────────────────────────────


class TestPoisonedFiles:
    def test_secret_file_is_poisoned_flag_and_record(
        self, store: CodeGraphStore, mini_repo: Path
    ) -> None:
        result = index_project("proj", mini_repo, store, _FakeMainStore())
        assert "poisoned.py" in result.poisoned
        conn = store._conn()
        rec = conn.execute(
            "SELECT parse_ok, parse_error FROM graph_files WHERE path='poisoned.py'"
        ).fetchone()
        assert rec["parse_ok"] == 0
        assert rec["parse_error"] == "secret-detected"
        # Nodes exist (names only) and carry the poisoned marker.
        node = conn.execute(
            "SELECT metadata FROM project_nodes WHERE path='poisoned.py' AND kind='Function'"
        ).fetchone()
        assert node is not None
        assert json.loads(str(node["metadata"])) == {"poisoned": True}
        # PG1 still holds for a poisoned file: the secret value itself
        # never enters the store (names only).
        leak = conn.execute(
            "SELECT COUNT(*) FROM project_nodes WHERE name LIKE ?", ("%AKIA%",)
        ).fetchone()
        assert leak[0] == 0


# ── PG7: fail-closed limits (PGT-6) ────────────────────────────────────────


class TestLimits:
    def test_max_files_breach_aborts_whole_index(
        self, store: CodeGraphStore, mini_repo: Path
    ) -> None:
        main = _FakeMainStore()
        first = index_project("proj", mini_repo, store, main)
        assert first.status == "ok"
        baseline_nodes = store.count_nodes("proj")
        assert baseline_nodes > 0

        tiny = CodeGraphConfig(index_max_files=2)  # the fixture has 5 files
        with pytest.raises(IndexLimitError, match="index_max_files=2"):
            index_project("proj", mini_repo, store, main, config=tiny)

        # The whole index FAILED: the previous graph survives untouched.
        assert store.count_nodes("proj") == baseline_nodes
        # And no epoch bump happened on the failed run.
        assert read_project_graph_epoch(main, "proj") == 1

    def test_max_source_mb_breach_aborts(self, store: CodeGraphStore, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        big = root / "big.py"
        big.write_bytes(b"x = 1\n" * 1024 * 1024)  # 6 MiB
        tiny = CodeGraphConfig(index_max_source_mb=1)
        with pytest.raises(IndexLimitError, match="index_max_source_mb=1"):
            index_project("p", root, store, _FakeMainStore(), config=tiny)
        assert store.count_nodes("p") == 0  # nothing published


# ── incrementality (slice 3) ────────────────────────────────────────────────


class TestIncremental:
    def test_fresh_tree_is_noop_no_epoch_bump(self, store: CodeGraphStore, mini_repo: Path) -> None:
        main = _FakeMainStore()
        index_project("proj", mini_repo, store, main)
        epoch1 = read_project_graph_epoch(main, "proj")
        assert epoch1 == 1
        nodes1 = store.count_nodes("proj")

        again = index_project("proj", mini_repo, store, main)
        assert again.status == STATUS_FRESH
        assert again.incremental is True
        assert read_project_graph_epoch(main, "proj") == epoch1  # NO bump
        assert store.count_nodes("proj") == nodes1

    def test_touch_reindexes_and_bumps(self, store: CodeGraphStore, mini_repo: Path) -> None:
        main = _FakeMainStore()
        index_project("proj", mini_repo, store, main)
        epoch1 = read_project_graph_epoch(main, "proj")

        # Wait: mtime granularity — ensure a real change.
        app = mini_repo / "pkg" / "app.py"
        app.write_text(
            "from pkg.helper import Base, do_work\n\n"
            "class Klass(Base):\n    def m(self, x=5):\n        return do_work(x)\n\n"
            "def added():\n    return 1\n",
            encoding="utf-8",
        )
        report = staleness_check("proj", mini_repo, store)
        assert "pkg/app.py" in report.changed_files
        assert report.fresh_percent < 100.0

        result = index_project("proj", mini_repo, store, main)
        assert result.status == "ok"
        assert result.incremental is True
        assert read_project_graph_epoch(main, "proj") == epoch1 + 1
        assert "pkg/app.py#added#" in "".join(_node_ids(store))
        report2 = staleness_check("proj", mini_repo, store)
        assert report2.changed_files == []
        assert report2.fresh_percent == 100.0

    def test_delete_removes_nodes(self, store: CodeGraphStore, mini_repo: Path) -> None:
        main = _FakeMainStore()
        index_project("proj", mini_repo, store, main)
        assert "poisoned.py#leaky#" in "".join(_node_ids(store))

        (mini_repo / "poisoned.py").unlink()
        result = index_project("proj", mini_repo, store, main)
        assert result.status == "ok"
        ids = "".join(_node_ids(store))
        assert "poisoned.py" not in ids
        rec = (
            store._conn()
            .execute("SELECT COUNT(*) FROM graph_files WHERE path='poisoned.py'")
            .fetchone()
        )
        assert rec[0] == 0
        # The staleness report saw the removal BEFORE reindexing.
        # (checked post-hoc here: the file is gone from both sides)

    def test_staleness_check_is_read_only(self, store: CodeGraphStore, mini_repo: Path) -> None:
        main = _FakeMainStore()
        index_project("proj", mini_repo, store, main)
        nodes = store.count_nodes("proj")
        files = store.count_files("proj")
        epoch = read_project_graph_epoch(main, "proj")

        report = staleness_check("proj", mini_repo, store)
        assert report.total_files == 6
        assert report.changed_files == []
        assert report.last_indexed_at is not None
        assert store.count_nodes("proj") == nodes
        assert store.count_files("proj") == files
        assert read_project_graph_epoch(main, "proj") == epoch

    def test_concurrent_call_returns_in_progress(
        self, store: CodeGraphStore, mini_repo: Path
    ) -> None:
        import threading

        from vesmaro.codegraph.incremental import _lock_for

        main = _FakeMainStore()
        index_project("proj", mini_repo, store, main)
        lock = _lock_for("proj")
        with lock:  # simulate an in-flight index
            result = index_project("proj", mini_repo, store, main)
        assert result.status == STATUS_IN_PROGRESS
        del threading

    def test_full_rebuild_after_incremental_flag(
        self, store: CodeGraphStore, mini_repo: Path
    ) -> None:
        main = _FakeMainStore()
        index_project("proj", mini_repo, store, main)
        result = index_project("proj", mini_repo, store, main, incremental=False)
        assert result.status == "ok"
        assert result.incremental is False
        assert read_project_graph_epoch(main, "proj") == 2


# ── store contract interplay ────────────────────────────────────────────────


class TestStoreContract:
    def test_project_node_and_epoch_key(self, store: CodeGraphStore, mini_repo: Path) -> None:
        main = _FakeMainStore()
        index_project("proj", mini_repo, store, main)
        assert project_graph_epoch_key("proj") == "project_graph_epoch:4:proj"
        assert main.meta[project_graph_epoch_key("proj")] == "1"
        conn = store._conn()
        project = conn.execute(
            "SELECT kind, name, qname FROM project_nodes WHERE kind='Project'"
        ).fetchone()
        assert project["name"] == "proj"
        assert project["qname"] == "proj"

    def test_no_self_loop_edges_survive_publish(
        self, store: CodeGraphStore, tmp_path: Path
    ) -> None:
        # A recursive call must not fail the edge batch (store CHECK
        # from_id <> to_id): the self-loop guard drops it.
        root = tmp_path / "repo"
        root.mkdir()
        (root / "r.py").write_text(
            "def rec(n):\n    return rec(n - 1) if n else 0\n",
            encoding="utf-8",
        )
        result = index_project("p", root, store, _FakeMainStore())
        assert result.status == "ok"
        conn = store._conn()
        selfloops = conn.execute(
            "SELECT COUNT(*) FROM project_edges WHERE from_id = to_id"
        ).fetchone()
        assert selfloops[0] == 0

    def test_indexer_direct_full_api(self, store: CodeGraphStore, mini_repo: Path) -> None:
        # The ProjectIndexer core is usable without the facade (the
        # manager wiring in a later slice holds its own locks).
        indexer = ProjectIndexer(store)
        result = indexer.index_full("direct", mini_repo)
        assert result.status == "ok"
        assert store.count_nodes("direct") == result.nodes
