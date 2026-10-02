"""Coverage tests for `storage/code_graph_store.py` (ADR-0032 PG-0 slice 1).

The sidecar store is the vectors.db precedent applied to the project
graph. These tests pin the contract surface of the slice: idempotent
schema creation, the binding CHECK constraints (node kind, edge kind,
self-loop), the atomic per-project delete with FK cascade, transactional
bulk writes, and the index set the status/search tools will lean on.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from vesma.storage.code_graph_store import (
    CodeGraphEdge,
    CodeGraphNode,
    CodeGraphStore,
    GraphFileRecord,
    bump_project_graph_epoch,
    last_indexed_key,
    project_graph_epoch_key,
    read_project_graph_epoch,
)


@pytest.fixture
def data_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def store(data_dir: Path) -> Iterator[CodeGraphStore]:
    s = CodeGraphStore(data_dir)
    yield s
    s.close()


class _FakeMainStore:
    """Minimal main-store meta surface (get_meta/set_meta).

    Mirrors the structural MainStoreMeta Protocol so the epoch helper is
    tested without constructing the full SQLiteStore.
    """

    def __init__(self) -> None:
        self.meta: dict[str, str] = {}

    def get_meta(self, key: str) -> str | None:
        return self.meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value


def _node(
    project: str, name: str, kind: str = "Function", path: str = "pkg/a.py", line: int = 1
) -> CodeGraphNode:
    """Canonical node: id = <project>#<rel_path>#<symbol>#<line> (ADR-0032)."""
    return CodeGraphNode(
        id=f"{project}#{path}#{name}#{line}",
        project=project,
        kind=kind,
        name=name,
        qname=f"pkg.{name}",
        path=path,
        start_line=line,
        end_line=line + 5,
        lang="python",
        signature=f"def {name}(x)",
    )


class TestSchema:
    def test_schema_created_idempotently(self, store: CodeGraphStore) -> None:
        """A second create_schema() (and a second open) is a no-op."""
        store.create_schema()
        # A fresh store instance over the SAME file replays the same
        # idempotent bootstrap (CREATE IF NOT EXISTS convergence).
        store2 = CodeGraphStore(Path(store._db_path).parent)
        try:
            assert store2.count_nodes() == 0
        finally:
            store2.close()

    def test_all_four_tables_exist(self, store: CodeGraphStore) -> None:
        """The sidecar has exactly the four contract tables."""
        conn = store._conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = {str(r["name"]) for r in rows}
        assert {"project_nodes", "project_edges", "graph_files", "graph_meta"} <= tables

    def test_indexes_exist(self, store: CodeGraphStore) -> None:
        """All five contract indexes are present (PRAGMA index_list)."""
        conn = store._conn()
        node_idx = {
            str(r["name"]) for r in conn.execute("PRAGMA index_list(project_nodes)").fetchall()
        }
        edge_idx = {
            str(r["name"]) for r in conn.execute("PRAGMA index_list(project_edges)").fetchall()
        }
        assert {"idx_nodes_project", "idx_nodes_qname", "idx_nodes_path"} <= node_idx
        assert {"idx_edges_from", "idx_edges_to"} <= edge_idx

    def test_wal_mode(self, store: CodeGraphStore) -> None:
        """The sidecar runs in WAL (journal_mode pragma)."""
        row = store._conn().execute("PRAGMA journal_mode").fetchone()
        assert str(row[0]).lower() == "wal"


class TestNodeEdgeConstraints:
    def test_bad_node_kind_rejected(self, store: CodeGraphStore) -> None:
        """CHECK on kind: a non-contract node kind fails the insert."""
        with pytest.raises(sqlite3.IntegrityError):
            store.upsert_nodes([_node("p", "x", kind="NotAThing")])

    def test_bad_edge_kind_rejected(self, store: CodeGraphStore) -> None:
        """CHECK on kind: a non-contract edge kind fails the insert."""
        store.upsert_nodes([_node("p", "a"), _node("p", "b", path="pkg/b.py")])
        with pytest.raises(sqlite3.IntegrityError):
            store.upsert_edges(
                [
                    CodeGraphEdge(
                        from_id=_node("p", "a").id,
                        to_id=_node("p", "b", path="pkg/b.py").id,
                        kind="CALLZ",
                    )
                ]
            )

    def test_self_loop_rejected(self, store: CodeGraphStore) -> None:
        """CHECK (from_id <> to_id): a self-loop fails the insert."""
        n = _node("p", "a")
        store.upsert_nodes([n])
        with pytest.raises(sqlite3.IntegrityError):
            store.upsert_edges([CodeGraphEdge(from_id=n.id, to_id=n.id, kind="CALLS")])

    def test_edge_to_missing_node_rejected(self, store: CodeGraphStore) -> None:
        """FK + ON DELETE CASCADE are enforced: an edge to a missing node fails."""
        store.upsert_nodes([_node("p", "a")])
        with pytest.raises(sqlite3.IntegrityError):
            store.upsert_edges(
                [CodeGraphEdge(from_id=_node("p", "a").id, to_id="p#ghost.py#g#1", kind="CALLS")]
            )


class TestBulkWrites:
    def test_bulk_insert_nodes_and_edges(self, store: CodeGraphStore) -> None:
        """executemany bulk path: counts land, defaults apply."""
        a = _node("p", "a")
        b = _node("p", "b", path="pkg/b.py", kind="Class")
        store.upsert_nodes([a, b])
        store.upsert_edges([CodeGraphEdge(from_id=a.id, to_id=b.id, kind="DEFINES")])
        assert store.count_nodes("p") == 2
        assert store.count_edges("p") == 1
        row = (
            store._conn()
            .execute("SELECT weight, provenance FROM project_edges WHERE from_id=?", (a.id,))
            .fetchone()
        )
        assert row is not None
        assert float(row["weight"]) == 1.0
        assert str(row["provenance"]) == "tree-sitter"

    def test_bulk_transaction_rolls_back_on_bad_row(self, store: CodeGraphStore) -> None:
        """One bad row fails the WHOLE batch — no partial graph is published."""
        good = [_node("p", f"f{i}") for i in range(3)]
        bad = _node("p", "boom", kind="Bogus")
        with pytest.raises(sqlite3.IntegrityError):
            store.upsert_nodes([*good, bad])
        assert store.count_nodes("p") == 0

    def test_edge_bulk_rolls_back_on_bad_row(self, store: CodeGraphStore) -> None:
        """Same atomicity for the edge batch."""
        a = _node("p", "a")
        b = _node("p", "b", path="pkg/b.py")
        store.upsert_nodes([a, b])
        with pytest.raises(sqlite3.IntegrityError):
            store.upsert_edges(
                [
                    CodeGraphEdge(from_id=a.id, to_id=b.id, kind="CALLS"),
                    CodeGraphEdge(from_id=a.id, to_id=b.id, kind="NOPE"),
                ]
            )
        assert store.count_edges("p") == 0

    def test_upsert_replaces_existing(self, store: CodeGraphStore) -> None:
        """INSERT OR REPLACE: a second upsert of the same node id replaces the row."""
        n1 = _node("p", "a", line=10)
        store.upsert_nodes([n1])
        # frozen dataclass — rebuild with the new name, same id
        replacement = CodeGraphNode(
            id=n1.id,
            project="p",
            kind="Function",
            name="renamed",
            qname=n1.qname,
            path=n1.path,
            start_line=n1.start_line,
            end_line=n1.end_line,
            lang=n1.lang,
            signature=n1.signature,
        )
        store.upsert_nodes([replacement])
        assert store.count_nodes("p") == 1
        row = (
            store._conn().execute("SELECT name FROM project_nodes WHERE id=?", (n1.id,)).fetchone()
        )
        assert row is not None
        assert str(row["name"]) == "renamed"


class TestDeleteProject:
    def test_delete_removes_subtree_keeps_other_project(self, store: CodeGraphStore) -> None:
        """Atomic per-project delete: the project's nodes+edges+file
        records vanish; ANOTHER project's rows are untouched."""
        # project "one": 3 nodes, 2 edges, 1 file record
        a1 = _node("one", "a")
        b1 = _node("one", "b", path="pkg/b.py")
        c1 = _node("one", "c", path="pkg/c.py")
        store.upsert_nodes([a1, b1, c1])
        store.upsert_edges(
            [
                CodeGraphEdge(from_id=a1.id, to_id=b1.id, kind="CALLS"),
                CodeGraphEdge(from_id=a1.id, to_id=c1.id, kind="USES"),
            ]
        )
        store.upsert_file_record(
            GraphFileRecord(
                project="one", path="pkg/b.py", mtime=1.0, size=10, hash="h1", parse_ok=1
            )
        )
        # project "two": 1 node, 1 file record
        a2 = _node("two", "a")
        store.upsert_nodes([a2])
        store.upsert_file_record(GraphFileRecord(project="two", path="pkg/z.py"))

        deleted = store.delete_project("one")

        assert deleted == 3
        assert store.count_nodes("one") == 0
        assert store.count_edges("one") == 0
        assert store.count_files("one") == 0
        assert store.count_nodes("two") == 1
        assert store.count_files("two") == 1

    def test_delete_cascades_edges_inside_transaction(self, store: CodeGraphStore) -> None:
        """ON DELETE CASCADE: edges referencing deleted nodes are gone."""
        a = _node("p", "a")
        b = _node("p", "b", path="pkg/b.py")
        store.upsert_nodes([a, b])
        store.upsert_edges([CodeGraphEdge(from_id=a.id, to_id=b.id, kind="IMPORTS")])
        store.delete_project("p")
        # Cross-project edge target would cascade too — assert total edges = 0.
        assert store.count_edges() == 0

    def test_delete_missing_project_is_noop(self, store: CodeGraphStore) -> None:
        """Deleting an unknown project returns 0 and commits cleanly."""
        assert store.delete_project("ghost") == 0


class TestFileRecords:
    def test_upsert_and_get_roundtrip(self, store: CodeGraphStore) -> None:
        """upsert_file_record writes; get_file_records reads back."""
        store.upsert_file_record(
            GraphFileRecord(
                project="p",
                path="pkg/a.py",
                mtime=1.5,
                size=42,
                hash="abc",
                parse_ok=1,
            )
        )
        records = store.get_file_records("p")
        assert len(records) == 1
        r = records[0]
        assert r.path == "pkg/a.py"
        assert r.mtime == 1.5
        assert r.size == 42
        assert r.hash == "abc"
        assert r.parse_ok == 1
        assert r.indexed_at is not None  # auto-stamped ISO timestamp

    def test_parse_error_stays_visible(self, store: CodeGraphStore) -> None:
        """«clean ≠ proof»: a parse failure is persisted, not swallowed."""
        store.upsert_file_record(
            GraphFileRecord(
                project="p",
                path="bad.py",
                parse_ok=0,
                parse_error="SyntaxError: x",
            )
        )
        r = store.get_file_records("p")[0]
        assert r.parse_ok == 0
        assert r.parse_error == "SyntaxError: x"

    def test_get_files_scoped_by_project(self, store: CodeGraphStore) -> None:
        """get_file_records returns only the requested project's rows."""
        store.upsert_file_record(GraphFileRecord(project="p1", path="a.py"))
        store.upsert_file_record(GraphFileRecord(project="p2", path="b.py"))
        assert [r.path for r in store.get_file_records("p1")] == ["a.py"]


class TestMetaAndCounts:
    def test_sidecar_meta_roundtrip(self, store: CodeGraphStore) -> None:
        """graph_meta set/get; absent key is None; upsert replaces."""
        assert store.get_meta("k") is None
        store.set_meta("k", "v1")
        assert store.get_meta("k") == "v1"
        store.set_meta("k", "v2")
        assert store.get_meta("k") == "v2"

    def test_counts_scoped_and_global(self, store: CodeGraphStore) -> None:
        """count_* with and without a project scope."""
        store.upsert_nodes([_node("p1", "a"), _node("p2", "b", path="x/b.py")])
        store.upsert_file_record(GraphFileRecord(project="p1", path="a.py"))
        assert store.count_nodes() == 2
        assert store.count_nodes("p1") == 1
        assert store.count_files("p1") == 1
        assert store.count_files() == 1

    def test_empty_store_counts(self, store: CodeGraphStore) -> None:
        """A fresh sidecar reports zeros."""
        assert store.count_nodes() == 0
        assert store.count_edges() == 0
        assert store.count_files() == 0


class TestProjectGraphEpoch:
    def test_key_format_is_length_prefixed(self) -> None:
        """Key discipline mirrors graph_epoch: {prefix}{len}:{project}."""
        assert project_graph_epoch_key("demo") == "project_graph_epoch:4:demo"
        # colon-safe: a slug with ':' still parses by the {len} segment
        assert project_graph_epoch_key("a:b") == "project_graph_epoch:3:a:b"

    def test_bump_increments_monotonically(self) -> None:
        """Each bump increments; the value is durable in the main store."""
        main = _FakeMainStore()
        assert read_project_graph_epoch(main, "p") == 0
        assert bump_project_graph_epoch(main, "p") == 1
        assert bump_project_graph_epoch(main, "p") == 2
        assert read_project_graph_epoch(main, "p") == 2
        assert main.meta[project_graph_epoch_key("p")] == "2"

    def test_epoch_is_per_project(self) -> None:
        """Bumping one project does not touch another's epoch."""
        main = _FakeMainStore()
        bump_project_graph_epoch(main, "p1")
        bump_project_graph_epoch(main, "p1")
        bump_project_graph_epoch(main, "p2")
        assert read_project_graph_epoch(main, "p1") == 2
        assert read_project_graph_epoch(main, "p2") == 1

    def test_read_returns_zero_on_missing(self) -> None:
        main = _FakeMainStore()
        assert read_project_graph_epoch(main, "never") == 0

    def test_read_swallows_store_error(self) -> None:
        """read is best-effort: a failing main store reads as 0 (the
        manager's graph_epoch read posture)."""

        class _Broken(_FakeMainStore):
            def get_meta(self, key: str) -> str | None:
                raise RuntimeError("main DB unavailable")

        assert read_project_graph_epoch(_Broken(), "p") == 0


class TestContextManager:
    def test_context_manager_closes(self, data_dir: Path) -> None:
        """``with`` closes the store (no leaked connection)."""
        with CodeGraphStore(data_dir) as store:
            store.upsert_nodes([_node("p", "a")])
        # The thread-local connection is released; a fresh open still sees
        # the committed row (WAL + same file).
        with CodeGraphStore(data_dir) as store2:
            assert store2.count_nodes("p") == 1


# ── slice-4 read surface + atomic publish + poisoned set ────────────────────


class TestReadSurface:
    def test_get_nodes_filters_by_kind_path_qname(self, store: CodeGraphStore) -> None:
        store.upsert_nodes(
            [
                _node("p", "alpha", kind="Function", path="pkg/a.py"),
                _node("p", "Beta", kind="Class", path="pkg/a.py", line=9),
                _node("p", "gamma", kind="Function", path="pkg/b.py"),
                _node("q", "alpha", kind="Function", path="other/x.py"),
            ]
        )
        kinds = {n.name for n in store.get_nodes("p", kind="Function")}
        assert kinds == {"alpha", "gamma"}
        by_path = {n.name for n in store.get_nodes("p", path="pkg/a.py")}
        assert by_path == {"alpha", "Beta"}
        by_qname = store.get_nodes("p", qname="pkg.gamma")
        assert [n.name for n in by_qname] == ["gamma"]
        # project scoping: q's nodes never leak into p's reads
        assert len(store.get_nodes("p")) == 3
        assert len(store.get_nodes("p", limit=2)) == 2

    def test_get_nodes_rejects_unknown_kind(self, store: CodeGraphStore) -> None:
        with pytest.raises(ValueError, match="unknown node kind"):
            store.get_nodes("p", kind="Sorcery")

    def test_get_node_roundtrip_and_missing(self, store: CodeGraphStore) -> None:
        node = _node("p", "alpha")
        store.upsert_nodes([node])
        got = store.get_node(node.id)
        assert got is not None and got.name == "alpha" and got.project == "p"
        assert store.get_node("p#nowhere#x#0") is None

    def test_search_nodes_matches_name_qname_path_escaped(self, store: CodeGraphStore) -> None:
        store.upsert_nodes(
            [
                _node("p", "handler", path="pkg/handler.py"),
                _node("p", "other", path="pkg/plain.py"),
            ]
        )
        # a node matching on several columns is still ONE row (name,
        # qname and path all hit for "handler")
        assert [n.name for n in store.search_nodes("p", "handler")] == ["handler"]
        # a path-only hit: "plain" appears in the path, never in a name
        assert [n.name for n in store.search_nodes("p", "plain")] == ["other"]
        assert [n.name for n in store.search_nodes("p", "pkg.other")] == ["other"]
        # % / _ are LITERALS in a search box, never wildcards
        assert store.search_nodes("p", "handl%") == []
        assert store.search_nodes("p", "handl_r") == []

    def test_get_edges_by_endpoint_and_kind(self, store: CodeGraphStore) -> None:
        a, b = _node("p", "a"), _node("p", "b")
        c = _node("q", "c")
        store.upsert_nodes([a, b, c])
        store.upsert_edges([CodeGraphEdge(from_id=a.id, to_id=b.id, kind="CALLS")])
        assert len(store.get_edges("p", from_id=a.id)) == 1
        assert store.get_edges("p", kind="CALLS")[0].to_id == b.id
        assert store.get_edges("q", from_id=c.id) == []


class TestPublishProjectGraph:
    def test_publish_replaces_subtree_atomically(self, store: CodeGraphStore) -> None:
        old = _node("p", "old")
        store.upsert_nodes([old])
        new = _node("p", "new")
        rec = GraphFileRecord(project="p", path="pkg/a.py", mtime=1.0, size=2, hash="h")
        store.publish_project_graph("p", [new], [], [rec])
        assert store.count_nodes("p") == 1
        assert store.get_nodes("p")[0].name == "new"
        assert len(store.get_file_records("p")) == 1

    def test_publish_failure_rolls_back_previous_graph(self, store: CodeGraphStore) -> None:
        """A bad edge (missing endpoint) fails the WHOLE publish — the
        previous graph survives (ADR-0032 §3.2 atomicity)."""
        keeper = _node("p", "keeper")
        store.publish_project_graph("p", [keeper], [], [])
        ghost = _node("p", "ghost")
        broken_edge = CodeGraphEdge(from_id=ghost.id, to_id="p#x#y#1", kind="CALLS")
        with pytest.raises(sqlite3.Error):
            store.publish_project_graph("p", [keeper, ghost], [broken_edge], [])
        names = {n.name for n in store.get_nodes("p")}
        assert names == {"keeper"}  # pre-publish graph untouched

    def test_publish_refuses_empty_node_set(self, store: CodeGraphStore) -> None:
        with pytest.raises(ValueError, match="empty node set"):
            store.publish_project_graph("p", [], [], [])


class TestPoisonedSet:
    def test_add_union_and_get_roundtrip(self, store: CodeGraphStore) -> None:
        store.add_poisoned_paths("p", ["pkg/a.py"])
        store.add_poisoned_paths("p", {"pkg/b.py", "pkg/a.py"})  # union, no dupes
        assert store.get_poisoned_paths("p") == {"pkg/a.py", "pkg/b.py"}
        assert store.get_poisoned_paths("q") == set()

    def test_poisoned_survives_reindex_publish(self, store: CodeGraphStore) -> None:
        """«Навсегда»: publish (the reindex path) must NEVER touch the
        poisoned set — only purge_project clears it."""
        store.add_poisoned_paths("p", ["pkg/leak.py"])
        store.publish_project_graph("p", [_node("p", "a")], [], [])
        assert store.get_poisoned_paths("p") == {"pkg/leak.py"}

    def test_purge_clears_subtree_poisoned_and_stamp(self, store: CodeGraphStore) -> None:
        node = _node("p", "a")
        store.publish_project_graph("p", [node], [], [])
        store.add_poisoned_paths("p", ["pkg/a.py"])
        store.set_meta(last_indexed_key("p"), "2026-09-28T00:00:00+00:00")
        deleted = store.purge_project("p")
        assert deleted == 1
        assert store.count_nodes("p") == 0
        assert store.get_poisoned_paths("p") == set()
        assert store.get_meta(last_indexed_key("p")) is None
