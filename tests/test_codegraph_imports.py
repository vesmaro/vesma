"""IMPORTS resolution regression tests (PG-0 acceptance slice 7 finding).

The wave-1 resolver assumed the indexing root IS the only sys.path
entry. When a src-layout tree is indexed at the package directory
(``root=src/vesma``), absolute imports ``vesma.*`` produced ZERO
IMPORTS edges — the module's dotted path from the root is one segment
shorter than the import alias. These tests pin the dotted-tail
fallback: alias and module qname must match as dotted suffixes in one
direction or the other, ONLY against modules that exist in the
project. Anything unproven (a nonexistent module, a stdlib-topped
alias) gets NO edge — no fiction.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_python", reason="code-graph extra not installed")

from vesma.codegraph.incremental import index_project
from vesma.storage.code_graph_store import CodeGraphStore

PROJECT = "proj"


class _FakeMainStore:
    """Minimal main-store meta surface (twin of the slice-1 meta)."""

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


def _package_tree(root: Path, name: str = "mypkg") -> None:
    """``<root>/<name>/{__init__,core,util}.py`` — core imports the
    sibling by its absolute package-qualified name, plus one module
    that does not exist (negative case)."""
    pkg = root / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text(
        f"import {name}.util\nimport nonexistent_module\n", encoding="utf-8"
    )
    (pkg / "util.py").write_text("def help_x() -> int:\n    return 2\n", encoding="utf-8")


def _mid(rel: str) -> str:
    """Module node id (slice-1 contract, length-prefixed project)."""
    return f"{len(PROJECT)}:{PROJECT}#{rel}##0#module"


def _imports(store: CodeGraphStore) -> set[tuple[str, str]]:
    rows = (
        store._conn()
        .execute("SELECT from_id, to_id FROM project_edges WHERE kind='IMPORTS'")
        .fetchall()
    )
    return {(str(r["from_id"]), str(r["to_id"])) for r in rows}


def test_direct_resolves_when_root_is_sys_path_entry(store: CodeGraphStore, tmp_path: Path) -> None:
    """root=<repo> with <repo>/mypkg/ inside — the wave-1 direct hit
    (the prescribed fixture; alias path == module path from the root)."""
    root = tmp_path / "repo"
    _package_tree(root)
    result = index_project(PROJECT, root, store, _FakeMainStore())
    assert result.status == "ok"
    # exactly one edge: the real import; nonexistent_module stays edgeless
    assert _imports(store) == {(_mid("mypkg/core.py"), _mid("mypkg/util.py"))}


def test_fallback_resolves_when_root_is_package_dir(store: CodeGraphStore, tmp_path: Path) -> None:
    """The acceptance finding: root=src/vesma (the package dir itself)
    yielded 0 IMPORTS edges — alias ``mypkg.util`` vs module qname
    ``util``. The dotted-tail fallback, anchored by the root's own
    name, resolves it; the nonexistent import still gets no edge."""
    repo = tmp_path / "repo"
    _package_tree(repo)
    result = index_project(PROJECT, repo / "mypkg", store, _FakeMainStore())
    assert result.status == "ok"
    assert _imports(store) == {(_mid("core.py"), _mid("util.py"))}


def test_regression_root_at_src_layout_sys_path(store: CodeGraphStore, tmp_path: Path) -> None:
    """Acceptance-scenario mini (root=<repo>/src, ``pkg.*`` imports):
    the root IS the sys.path entry — direct resolution keeps working."""
    root = tmp_path / "repo" / "src"
    _package_tree(root, name="pkg")
    result = index_project(PROJECT, root, store, _FakeMainStore())
    assert result.status == "ok"
    assert _imports(store) == {(_mid("pkg/core.py"), _mid("pkg/util.py"))}


def test_fallback_resolves_when_root_is_above_src(store: CodeGraphStore, tmp_path: Path) -> None:
    """Root indexed ABOVE the sys.path entry (``<repo>/src/mypkg``):
    alias ``mypkg.util`` is a dotted suffix of qname ``src.mypkg.util``."""
    root = tmp_path / "repo"
    _package_tree(root / "src")
    result = index_project(PROJECT, root, store, _FakeMainStore())
    assert result.status == "ok"
    assert _imports(store) == {(_mid("src/mypkg/core.py"), _mid("src/mypkg/util.py"))}


def test_stdlib_topped_alias_never_invents_edge(store: CodeGraphStore, tmp_path: Path) -> None:
    """Honesty boundary: ``import os.path`` must NOT produce an edge to
    a project module that merely shares the tail name ``path`` — the
    stripped alias head must be the root's own name."""
    pkg = tmp_path / "repo" / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text("import os.path\n", encoding="utf-8")
    (pkg / "path.py").write_text("x = 1\n", encoding="utf-8")
    index_project(PROJECT, pkg, store, _FakeMainStore())  # root = package dir
    assert _imports(store) == set()


def test_stdlib_named_root_never_invents_edge(store: CodeGraphStore, tmp_path: Path) -> None:
    """Honesty boundary: a project package shadowing a stdlib name
    (``json``) indexed at the package dir — ``import json.tool`` is
    indistinguishable from the stdlib import, so it stays edgeless."""
    pkg = tmp_path / "repo" / "json"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text("import json.tool\n", encoding="utf-8")
    (pkg / "tool.py").write_text("x = 1\n", encoding="utf-8")
    index_project(PROJECT, pkg, store, _FakeMainStore())  # root = package dir
    assert _imports(store) == set()
