"""Ephemeral-directory exclusion on the code-graph surface (defect
2026-10-03).

The live case: a git worktree checked out INSIDE a registered root
(``git worktree add wt/graph-exclude``) was walked like first-party
sources — symbol counts doubled (376→746 files), and secret FIXTURES
inside the worktree copy were re-poisoned on every index. The contract
pinned here:

* the built-in ``DENY_DIRS`` never enters ``wt`` (any nesting depth) —
  a worktree inside the root is never indexed (regression test);
* ``CodeGraphConfig.exclude_globs`` prunes directories ON TOP of the
  built-in denylist: bare names match at any depth, ``/``-bearing
  globs match repo-relative directory paths (fnmatch semantics);
* setting the field REPLACES the default list (``DEFAULT_EXCLUDE_DIR_GLOBS``)
  but can never lift the built-in ``DENY_DIRS`` — defense in depth;
* ``staleness_check`` (the beacon/auto-stale path) uses the same
  surface, so a fresh worktree never reads as «stale files»;
* the ``VESMA_CODE_GRAPH__EXCLUDE_GLOBS`` env override reaches
  ``Settings`` (JSON array, same plumbing as ``SECRET_ALLOWLIST``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_python", reason="code-graph extra not installed")

from vesmaro.codegraph.file_surface import FileSurface, surface_allows
from vesmaro.codegraph.incremental import index_project, staleness_check
from vesmaro.config import DEFAULT_EXCLUDE_DIR_GLOBS, CodeGraphConfig, Settings
from vesmaro.storage.code_graph_store import CodeGraphStore

SECRET_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


class _FakeMainStore:
    """Minimal main-store meta surface (slice-1 MainStoreMeta twin)."""

    def __init__(self) -> None:
        self.meta: dict[str, str] = {}

    def get_meta(self, key: str) -> str | None:
        return self.meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value


@pytest.fixture
def store(tmp_path: Path) -> Iterator[CodeGraphStore]:
    s = CodeGraphStore(tmp_path / "data")
    yield s
    s.close()


@pytest.fixture
def wt_repo(tmp_path: Path) -> Path:
    """A project tree with an ephemeral git-worktree copy inside it.

    Layout: the first-party package (one module, one symbol), a full
    worktree clone under ``wt/graph-exclude`` (duplicate symbol + a
    secret-bearing FIXTURE, the live re-poisoning vector), dependency
    noise (``node_modules``) and a ``gen/`` tree that only the KNOB
    may exclude (it is NOT in the built-in denylist).
    """
    root = tmp_path / "repo"
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "helper.py").write_text(
        "def do_work(x: int) -> int:\n    return x\n",
        encoding="utf-8",
    )
    # The ephemeral worktree — a full repo copy nested in the root.
    wt = root / "wt" / "graph-exclude"
    (wt / "pkg").mkdir(parents=True)
    (wt / "pkg" / "helper.py").write_text(
        "def do_work(x: int) -> int:\n    return x\n",
        encoding="utf-8",
    )
    # Secret-bearing fixture inside the worktree — must never be
    # indexed, therefore never poisoned.
    fixtures = wt / "tests" / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "fake_aws.py").write_text(
        f"FAKE_KEY = '{SECRET_AWS_KEY}'\n",
        encoding="utf-8",
    )
    # Dependency noise.
    nm = root / "node_modules" / "dep"
    nm.mkdir(parents=True)
    (nm / "index.py").write_text("x = 1\n", encoding="utf-8")
    # Not in the built-in denylist — only the knob can exclude it.
    gen = root / "gen"
    gen.mkdir()
    (gen / "generated.py").write_text(
        "def generated():\n    pass\n",
        encoding="utf-8",
    )
    return root


class TestSurfaceDefaults:
    def test_worktree_and_node_modules_excluded_by_default(self, wt_repo: Path) -> None:
        rels = {sf.rel_path for sf in FileSurface(wt_repo).collect()}
        assert rels == {"pkg/__init__.py", "pkg/helper.py", "gen/generated.py"}

    def test_exclude_name_matches_any_depth(self, tmp_path: Path) -> None:
        root = tmp_path / "deep"
        (root / "a" / "b" / "wt" / "clone").mkdir(parents=True)
        (root / "a" / "b" / "wt" / "clone" / "c.py").write_text(
            "def deep():\n    pass\n", encoding="utf-8"
        )
        (root / "a" / "keep.py").write_text("def keep():\n    pass\n", encoding="utf-8")
        rels = {sf.rel_path for sf in FileSurface(root).collect()}
        assert rels == {"a/keep.py"}

    def test_path_glob_matches_rel_dir(self, tmp_path: Path) -> None:
        root = tmp_path / "globbed"
        (root / "builds" / "debug").mkdir(parents=True)
        (root / "builds" / "debug" / "o.py").write_text("def o():\n    pass\n", encoding="utf-8")
        (root / "builds" / "release").mkdir()
        (root / "builds" / "release" / "r.py").write_text("def r():\n    pass\n", encoding="utf-8")
        rels = {sf.rel_path for sf in FileSurface(root, ["builds/debug"]).collect()}
        assert rels == {"builds/release/r.py"}

    def test_surface_allows_predicate_matches_walker(self) -> None:
        # Built-in denylist (wt at any depth) — no globs needed.
        assert not surface_allows("wt/graph-exclude/pkg/helper.py")
        assert not surface_allows("a/b/wt/clone/c.py")
        assert surface_allows("pkg/helper.py")
        # Knob glob: bare dir name at any depth, and /-bearing rel path.
        assert not surface_allows("gen/x.py", ["gen"])
        assert not surface_allows("builds/debug/o.py", ["builds/debug"])
        assert surface_allows("gen/x.py")  # gen is not in the built-in denylist


class TestExcludeKnob:
    def test_default_config_carries_default_globs(self) -> None:
        cfg = CodeGraphConfig()
        assert cfg.exclude_globs == list(DEFAULT_EXCLUDE_DIR_GLOBS)

    def test_knob_extends_surface_beyond_builtin(self, wt_repo: Path) -> None:
        cfg = CodeGraphConfig(exclude_globs=[*DEFAULT_EXCLUDE_DIR_GLOBS, "gen"])
        rels = {sf.rel_path for sf in FileSurface(wt_repo, cfg.exclude_globs).collect()}
        assert rels == {"pkg/__init__.py", "pkg/helper.py"}

    def test_knob_replacement_cannot_lift_builtin_denylist(self, wt_repo: Path) -> None:
        # Operator replaces the whole list — wt/node_modules STILL never
        # enter the surface (DENY_DIRS is not overridable).
        cfg = CodeGraphConfig(exclude_globs=["gen"])
        assert cfg.exclude_globs == ["gen"]
        rels = {sf.rel_path for sf in FileSurface(wt_repo, cfg.exclude_globs).collect()}
        # gen/ excluded by the knob; wt/ and node_modules/ by the built-in.
        assert rels == {"pkg/__init__.py", "pkg/helper.py"}


class TestWorktreeIndexRegression:
    def test_worktree_inside_root_is_not_indexed(
        self, store: CodeGraphStore, wt_repo: Path
    ) -> None:
        """The live defect 2026-10-03: ``git worktree add wt/...`` inside
        the registered root doubled the graph (376→746). The worktree
        copy must contribute ZERO files/symbols."""
        result = index_project("proj", wt_repo, store, _FakeMainStore())
        assert result.status == "ok"
        assert result.files_indexed == 3  # __init__, helper, generated — NOT the wt copies
        assert result.poisoned == []  # the wt secret fixture never entered the surface

        file_paths = {rec.path for rec in store.get_file_records("proj")}
        assert not any(p.startswith(("wt/", "node_modules/")) for p in file_paths)

        # The duplicated symbol exists exactly once — from the
        # first-party tree, never from the worktree copy.
        do_work = (
            store._conn().execute("SELECT path FROM project_nodes WHERE name='do_work'").fetchall()
        )
        assert [row[0] for row in do_work] == ["pkg/helper.py"]

    def test_worktree_fixtures_are_never_poisoned(
        self, store: CodeGraphStore, wt_repo: Path
    ) -> None:
        index_project("proj", wt_repo, store, _FakeMainStore())
        assert store.get_poisoned_paths("proj") == set()

    def test_staleness_ignores_late_worktree(self, store: CodeGraphStore, wt_repo: Path) -> None:
        """After indexing, a worktree wave lands in the root — the
        beacon/auto-stale path must NOT classify it as changed files
        (the live «376→746» trigger was an auto reindex)."""
        index_project("proj", wt_repo, store, _FakeMainStore())
        late_wt = wt_repo / "wt" / "wave2"
        (late_wt / "pkg").mkdir(parents=True)
        (late_wt / "pkg" / "newmod.py").write_text("def brand_new():\n    pass\n", encoding="utf-8")
        report = staleness_check("proj", wt_repo, store, CodeGraphConfig())
        assert report.changed_files == []
        assert report.fresh_percent == 100.0


class TestEnvOverride:
    def test_exclude_globs_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VESMA_CODE_GRAPH__EXCLUDE_GLOBS", '["wt", "gen/**"]')
        s = Settings()
        assert s.code_graph.exclude_globs == ["wt", "gen/**"]

    def test_exclude_globs_default_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VESMA_CODE_GRAPH__EXCLUDE_GLOBS", raising=False)
        s = Settings()
        assert s.code_graph.exclude_globs == list(DEFAULT_EXCLUDE_DIR_GLOBS)
