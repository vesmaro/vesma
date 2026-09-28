"""Acceptance suite for wave PG-0 (ADR-0032 §7 PGT-1..7, contract §7/§9.7).

This is the ACCEPTANCE layer: every test walks the full stack (files on
disk → indexer → sidecar store → CodeGraphService) on real tmp trees —
no parser mocks. It closes the wave-level holes the per-slice suites
leave open and does NOT duplicate their coverage:

* PGT-1 zero bytes — indexer-level dump: tests/test_codegraph_indexer.py
  (``TestZeroSourceBytes``). HERE: end-to-end through the service facade
  with a comment-secret marker added and the PG7 ``graph_audit`` table
  included in the dump.
* PGT-2 confinement — covered at indexer (symlink rejection, ibid.
  ``TestFileSurface``) and tool (registration-only resolution, path
  escape: tests/test_codegraph_tools.py ``TestPGMechanics``) level.
* PGT-3 poisoned — indexer flag/record + tools refusal that survives a
  reindex whose detector MISSES (patched). HERE: the unpatched
  real-flow variant — the secret is REMOVED from the file, the REAL
  detector rescans clean, and the poisoned marker still holds.
* PGT-4 issuance — tools level (``TestSnippetPG4``).
* PGT-5 export/federation — NOWHERE before this suite. HERE: the export
  surface (JSON payload + sqlite snapshot) is proven blind to the
  sidecar while the sidecar is loaded and populated.
* PGT-6 limits — indexer + tool level (fail-closed, no partial graph).
* PGT-7 token contract — tools level (``TestTokenContract*``).

Plus the PG-0 acceptance on THIS repository (contract §4): the
self-index over ``src/`` with tmp-only storage (the real ``~/.mnemos``
and the main DB are never touched), the honest status circuit, and the
config gate (default-on since the owner decision of 2026-09-28).
"""

from __future__ import annotations

import io
import json
import sqlite3
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from vesmaro.codegraph.service import (
    CodeGraphService,
    GraphDisabledError,
    GraphToolError,
)
from vesmaro.config import CodeGraphConfig, Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import Project

AGENT = "pgt-acceptance"
PROJECT = "pgtproj"

# Canonical detector hit (poisoning fixture — NOT used in PGT-1, where
# the file must stay unpoisoned; custom markers below are shaped to
# miss every detector pattern: no AKIA/sk-/JWT prefixes, no 32+ char
# continuous [A-Za-z0-9+/=_-] spans).
SECRET_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"

DOC_MARKER = "PGT1-DOC-SECRET-2468"
COMMENT_MARKER = "PGT1-COMMENT-SECRET-1357"
LITERAL_MARKER = "pgt1-literal-secret-8642"

SIDE_CAR_TABLES = (
    "project_nodes",
    "project_edges",
    "graph_files",
    "graph_meta",
    "graph_audit",
)


# ── shared fixtures ──────────────────────────────────────────────────────────


class _FakeProject:
    def __init__(self, project_id: str, paths: list[str]) -> None:
        self.id = project_id
        self.name = project_id
        self.paths = paths


class _FakeMainStore:
    """Main-DB twin the service may see (PG2 registration + meta)."""

    def __init__(self, project: _FakeProject) -> None:
        self.project = project
        self.meta: dict[str, str] = {}

    def get_project(self, project_id: str) -> _FakeProject | None:
        return self.project if self.project.id == project_id else None

    def get_project_by_name(self, name: str) -> _FakeProject | None:
        return self.project if self.project.name == name else None

    def list_projects(self) -> list[_FakeProject]:
        return [self.project]

    def get_meta(self, key: str) -> str | None:
        return self.meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value


def make_service(
    tmp_path: Path, root: Path, project: str = PROJECT, *, enabled: bool = True, **config: Any
) -> CodeGraphService:
    """A service over a registered project with a TMP sidecar only."""
    main = _FakeMainStore(_FakeProject(project, [str(root)]))
    service = CodeGraphService(
        main,
        tmp_path / "data",
        CodeGraphConfig(enabled=enabled, **config),
    )
    return service


def _dump_tables(store_db: Path) -> dict[str, list[str]]:
    """Every text value of every row of EVERY sidecar table, stringified."""
    dumped: dict[str, list[str]] = {}
    conn = sqlite3.connect(store_db)
    try:
        for table in SIDE_CAR_TABLES:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            dumped[table] = [json.dumps([str(v) for v in row]) for row in rows]
    finally:
        conn.close()
    return dumped


# ── PGT-1: zero source bytes, end to end ─────────────────────────────────────


class TestPGT1ZeroBytesEndToEnd:
    """The indexer-level dump exists (test_codegraph_indexer.py); this
    walks the same invariant through the SERVICE facade — the path the
    MCP/REST tools actually take — with a comment-secret marker and the
    audit table added to the dump."""

    @pytest.fixture
    def secretish_repo(self, tmp_path: Path) -> Path:
        root = tmp_path / "repo"
        root.mkdir()
        # (a) secret-like string in a DOCSTRING, (b) in a string LITERAL,
        # (c) in a COMMENT, (d) normal code — none are detector hits,
        # so the file must NOT be poisoned; and none of the bytes may
        # survive into the sidecar.
        (root / "vaulted.py").write_text(
            f'"""Usage notes for {DOC_MARKER}."""\n'
            f"# rotating helper, see {COMMENT_MARKER}\n"
            f'LABEL_TEXT = "{LITERAL_MARKER}"\n'
            "\n"
            "def normal_fn(offset: int) -> int:\n"
            "    total = offset + 1\n"
            "    return total\n",
            encoding="utf-8",
        )
        (root / "plain.py").write_text("def plain_fn():\n    return 0\n", encoding="utf-8")
        return root

    def test_no_source_bytes_in_any_table(self, tmp_path: Path, secretish_repo: Path) -> None:
        service = make_service(tmp_path, secretish_repo)
        result = service.index_project(PROJECT, agent=AGENT)
        assert result["status"] == "ok"
        assert result["poisoned"] == []  # markers are not detector hits
        service.close()

        dumped = _dump_tables(tmp_path / "data" / "code_graph.db")
        assert any(dumped.values()), "the sidecar must not be empty"
        forbidden = [
            DOC_MARKER,
            COMMENT_MARKER,
            LITERAL_MARKER,
            "total = offset + 1",
            "return total",
            "Usage notes for",
            "rotating helper",
        ]
        for table, rows in dumped.items():
            for row in rows:
                for marker in forbidden:
                    assert marker not in row, f"{marker} leaked into {table}: {row}"

    def test_names_and_structure_still_alive(self, tmp_path: Path, secretish_repo: Path) -> None:
        service = make_service(tmp_path, secretish_repo)
        service.index_project(PROJECT, agent=AGENT)
        # Names/paths/structure are the ALLOWED payload — they survive.
        names = {
            row["name"]
            for row in service.get_file_outline(PROJECT, "vaulted.py", agent=AGENT)["outline"]
        }
        assert {"normal_fn", "vaulted.py"} <= names
        assert service.store.count_files(PROJECT) == 2
        service.close()


# ── PGT-5: export/federation never carries the graph ─────────────────────────


class TestPGT5ExportNeverCarriesGraph:
    """The export door is welded BEFORE any code exists (ADR-0032 §6):
    the JSON payload reads the main DB only, and the sqlite snapshot
    enumerates mnemos.db + vectors.db explicitly — code_graph.db is a
    separate file that export NEVER touches. These tests pin it while
    the sidecar is loaded and populated."""

    @pytest.fixture
    def export_manager(self, tmp_path: Path) -> Iterator[MemoryManager]:
        settings = Settings(
            mnemos={
                "vault_path": str(tmp_path / "vault"),
                "data_dir": str(tmp_path / "data"),
                "db_name": "test.db",
            },
            scanner={"enabled": False},
            code_graph={"enabled": True},
        )
        settings.resolve_paths()
        mgr = MemoryManager(settings)
        try:
            repo = tmp_path / "pgt5repo"
            pkg = repo / "graphonly_pkg"
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            # These two markers exist ONLY in the graph sidecar.
            (pkg / "marker.py").write_text(
                "def pgt5_federation_marker():\n    return 1\n", encoding="utf-8"
            )
            mgr.sqlite.save_project(Project(name=PROJECT, paths=[str(repo)]))
            service = mgr.get_codegraph_service()
            assert service is not None
            result = service.index_project(PROJECT, agent=AGENT)
            assert result["status"] == "ok"
            # Non-vacuous premise: the sidecar is real and populated.
            assert service.store.count_nodes(PROJECT) > 0
            assert (tmp_path / "data" / "code_graph.db").exists()
            yield mgr
        finally:
            mgr.close()

    @staticmethod
    def _assert_payload_graph_free(text: str) -> None:
        for marker in (
            "pgt5_federation_marker",
            "graphonly_pkg",
            "project_nodes",
            "project_edges",
            "graph_files",
            "code_graph.db",
        ):
            assert marker not in text, f"graph artifact {marker!r} reached the export"

    def test_json_export_is_graph_free(self, tmp_path: Path, export_manager: MemoryManager) -> None:
        from vesmaro.cli.export import CompressMode, ExportFormat, run_export

        out = tmp_path / "export.json"
        result = run_export(
            export_manager, fmt=ExportFormat.JSON, output=out, compress=CompressMode.NONE
        )
        payload = json.loads(out.read_text(encoding="utf-8"))
        # The export ran over real main-DB data…
        assert result.project_count == 1
        assert payload["memories"] == []
        assert payload["projects"][0]["name"] == PROJECT
        # …and carries NO graph section and NO graph marker anywhere.
        self._assert_payload_graph_free(out.read_text(encoding="utf-8"))
        assert not any(k.startswith(("graph", "project_nodes")) for k in payload)

    def test_filtered_json_export_is_graph_free(
        self, tmp_path: Path, export_manager: MemoryManager
    ) -> None:
        """Even with status/tags filters the invariant holds — the export
        surface has no code path that could reach the sidecar."""
        from vesmaro.cli.export import CompressMode, ExportFilter, ExportFormat, run_export
        from vesmaro.models import MemoryStatus

        out = tmp_path / "export-filtered.json"
        run_export(
            export_manager,
            fmt=ExportFormat.JSON,
            output=out,
            compress=CompressMode.NONE,
            filt=ExportFilter(status=MemoryStatus.PROCESSED, tags=["wave"]),
        )
        self._assert_payload_graph_free(out.read_text(encoding="utf-8"))

    def test_sqlite_snapshot_has_no_sidecar(
        self, tmp_path: Path, export_manager: MemoryManager
    ) -> None:
        from vesmaro.cli.export import CompressMode, ExportFormat, run_export

        out = tmp_path / "export.tar"
        run_export(export_manager, fmt=ExportFormat.SQLITE, output=out, compress=CompressMode.NONE)
        with tarfile.open(fileobj=io.BytesIO(out.read_bytes()), mode="r:gz") as tar:
            members = {m.name for m in tar.getmembers()}
            # By construction: the snapshot enumerates exactly the main
            # DB files — code_graph.db (a sibling in data_dir!) is not
            # among them, and no -wal/-shm leakage either.
            assert members == {"mnemos.db", "vectors.db"}
            db_obj = tar.extractfile("mnemos.db")
            assert db_obj is not None
            mnemos_db = tmp_path / "extracted-mnemos.db"
            mnemos_db.write_bytes(db_obj.read())
            conn = sqlite3.connect(mnemos_db)
            try:
                tables = {
                    name
                    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
            finally:
                conn.close()
        for graph_table in ("project_nodes", "project_edges", "graph_files", "graph_audit"):
            assert graph_table not in tables, f"{graph_table} reached the snapshot"
        assert not any("graph" in t or "code_graph" in t for t in tables)


# ── PGT-3: poisoned survives a clean real reindex ────────────────────────────


class TestPGT3PoisonedForeverRealFlow:
    """The tools-level twin (``test_poisoned_refused_forever_even_when_
    rescan_misses``) patches the detector to MISS; THIS variant is the
    user-facing story with the REAL detector and no patches: the secret
    is genuinely removed from the file, the rescan is genuinely clean,
    and the poisoned marker STILL holds — only delete clears it."""

    def test_poison_survives_clean_reindex(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        secret_file = root / "secret.py"
        secret_file.write_text(f'AWS_ID = "{SECRET_AWS_KEY}"\n', encoding="utf-8")
        (root / "helper.py").write_text("def helper_fn():\n    return 1\n", encoding="utf-8")
        service = make_service(tmp_path, root)
        try:
            first = service.index_project(PROJECT, agent=AGENT)
            assert first["poisoned"] == ["secret.py"]
            with pytest.raises(GraphToolError, match="POISONED"):
                service.get_code_snippet(PROJECT, "secret.py", 1, 1, agent=AGENT)

            # The secret is REMOVED from the file; a full reindex runs
            # the REAL detector over the clean content — it must pass.
            secret_file.write_text("def cleaned():\n    return 2\n", encoding="utf-8")
            reindexed = service.index_project(PROJECT, agent=AGENT, incremental=False)
            assert reindexed["status"] == "ok"
            assert reindexed["poisoned"] == []

            # …and the poisoned verdict STILL holds (the detector could
            # have missed; the marker never trusts a later clean scan).
            with pytest.raises(GraphToolError, match="POISONED"):
                service.get_code_snippet(PROJECT, "secret.py", 1, 1, agent=AGENT)
            coverage = service.check_coverage(PROJECT, ["secret.py"], agent=AGENT)["coverage"]
            assert coverage[0]["verdict"] == "poisoned"
            # Names stay alive; the neighbour is unaffected.
            assert service.store.count_files(PROJECT) == 2

            # Only delete_graph_project clears it.
            service.delete_graph_project(PROJECT, agent=AGENT)
            with pytest.raises(GraphToolError, match="not indexed"):
                service.get_code_snippet(PROJECT, "secret.py", 1, 1, agent=AGENT)
        finally:
            service.close()


# ── PG-0 acceptance on THIS repository (contract §4) ─────────────────────────


class TestSelfAcceptance:
    """The wave's own acceptance: index THIS repo's source into a tmp
    sidecar (never ~/.mnemos, never the main DB) and check the contract
    §4 numbers.

    Root choice: ``src/`` — ``_resolve_import`` treats the project root
    as the single sys.path entry, and this repo's sources import
    absolutely (``vesmaro.codegraph...``); with root=src/vesmaro those
    imports can never resolve (0 IMPORTS edges, measured), with root=src/
    they do. Key paths below are therefore ``vesmaro/...``.
    """

    SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
    #: Measured baseline 1456 nodes; the conservative bound sits ~17%
    #: under it (the task's «2-3k» estimate was optimistic for 112 files).
    MIN_NODES = 1200

    @pytest.fixture
    def self_service(self, tmp_path: Path) -> Iterator[CodeGraphService]:
        service = make_service(tmp_path, self.SRC_ROOT, project="self-acceptance")
        yield service
        service.close()

    def test_index_numbers_and_latency(self, self_service: CodeGraphService) -> None:
        t0 = time.perf_counter()
        result = self_service.index_project("self-acceptance", agent=AGENT)
        elapsed = time.perf_counter() - t0
        assert result["status"] == "ok"
        assert elapsed < 60.0, f"indexation took {elapsed:.1f}s (contract: seconds)"
        assert self_service.store.count_nodes("self-acceptance") >= self.MIN_NODES
        assert result["duration_sec"] < 60.0

    def test_key_files_present(self, self_service: CodeGraphService) -> None:
        self_service.index_project("self-acceptance", agent=AGENT)
        conn = self_service.store._conn()
        for key_file in (
            "storage/code_graph_store.py",
            "codegraph/indexer.py",
            "codegraph/service.py",
            "mcp_server.py",
        ):
            row = conn.execute(
                "SELECT COUNT(*) FROM project_nodes WHERE kind='File' AND path LIKE ?",
                (f"%{key_file}",),
            ).fetchone()
            assert row[0] == 1, f"key file {key_file} missing from the self-index"

    def test_edge_kinds_present(self, self_service: CodeGraphService) -> None:
        self_service.index_project("self-acceptance", agent=AGENT)
        conn = self_service.store._conn()
        kinds = dict(
            conn.execute("SELECT kind, COUNT(*) FROM project_edges GROUP BY kind").fetchall()
        )
        for edge_kind in ("CONTAINS_FILE", "DEFINES", "IMPORTS", "INHERITS", "CALLS", "USES"):
            assert kinds.get(edge_kind, 0) > 0, f"no {edge_kind} edges in the self-index"

    def test_search_outline_trace(self, self_service: CodeGraphService) -> None:
        self_service.index_project("self-acceptance", agent=AGENT)
        # Search finds the service class.
        hits = self_service.search_graph("self-acceptance", "CodeGraphService", agent=AGENT)
        assert hits["results"][0]["name"] == "CodeGraphService"
        # Outline carries known symbols.
        outline = self_service.get_file_outline(
            "self-acceptance", "vesmaro/codegraph/service.py", agent=AGENT
        )
        names = {row["name"] for row in outline["outline"]}
        assert {"CodeGraphService", "window_rows"} <= names
        # Trace from a method: USES/CALLS neighbours (the class node has
        # no outgoing edges by construction — methods carry the calls).
        trace = self_service.trace_path(
            "self-acceptance", "CodeGraphService.index_project", agent=AGENT
        )
        assert len(trace["nodes"]) > 1
        assert {"USES", "CALLS"} <= {e["kind"] for e in trace["edges"]}
        # Trace from the module: IMPORTS neighbours.
        mod_trace = self_service.trace_path(
            "self-acceptance", "vesmaro.codegraph.service", agent=AGENT
        )
        assert any(e["kind"] == "IMPORTS" for e in mod_trace["edges"])


# ── status circuit (honest numbers) ──────────────────────────────────────────


class TestStatusCircuit:
    @pytest.fixture
    def indexed(self, tmp_path: Path) -> Iterator[tuple[CodeGraphService, Path]]:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "helper.py").write_text("def helper_fn():\n    return 1\n", encoding="utf-8")
        (root / "other.py").write_text("def other_fn():\n    return 2\n", encoding="utf-8")
        (root / "secret.py").write_text(f'AWS_ID = "{SECRET_AWS_KEY}"\n', encoding="utf-8")
        service = make_service(tmp_path, root)
        service.index_project(PROJECT, agent=AGENT)
        yield service, root
        service.close()

    def test_status_numbers_match_the_store(self, indexed: tuple[CodeGraphService, Path]) -> None:
        service, _ = indexed
        store = service.store
        status = service.status(PROJECT, agent=AGENT)
        assert status["nodes"] == store.count_nodes(PROJECT)
        assert status["edges"] == store.count_edges(PROJECT)
        assert status["files"] == store.count_files(PROJECT) == 3
        assert status["poisoned_count"] == 1
        assert "secret.py" in status["parse_errors"]
        assert status["staleness"]["changed_files"] == []
        assert status["staleness"]["fresh_percent"] == 100.0

    def test_coverage_on_explicit_paths(self, indexed: tuple[CodeGraphService, Path]) -> None:
        service, _ = indexed
        verdicts = {
            row["path"]: row["verdict"]
            for row in service.check_coverage(PROJECT, ["helper.py", "secret.py"], agent=AGENT)[
                "coverage"
            ]
        }
        assert verdicts == {"helper.py": "indexed", "secret.py": "poisoned"}

    def test_staleness_after_touch_is_exactly_one(
        self, indexed: tuple[CodeGraphService, Path]
    ) -> None:
        service, root = indexed
        (root / "helper.py").write_text("def helper_fn():\n    return 42\n", encoding="utf-8")
        status = service.status(PROJECT, agent=AGENT)
        assert status["staleness"]["changed_files"] == ["helper.py"]
        assert status["staleness"]["fresh_percent"] < 100.0
        reindexed = service.index_project(PROJECT, agent=AGENT)
        assert reindexed["status"] == "ok"
        assert service.status(PROJECT, agent=AGENT)["staleness"]["changed_files"] == []


# ── config sanity: default-on (owner decision 2026-09-28) ───────────────────


class TestConfigDefaults:
    def test_code_graph_config_defaults(self) -> None:
        config = CodeGraphConfig()
        # Owner decision 2026-09-28 «graphs on by default»: ecosystem
        # components build on the graphs; tree-sitter is core since the
        # same decision. Disabling remains a one-flag operator choice.
        assert config.enabled is True
        # The watch poll is inert until an EXPLICIT watch_start
        # registration — on-by-default costs nothing.
        assert config.watch is True
        assert config.beacon is True
        default = Settings.model_fields["code_graph"].default
        assert isinstance(default, CodeGraphConfig) and default.enabled is True

    def test_explicit_subflags_inert_while_master_off(self, tmp_path: Path) -> None:
        """beacon=True + watch=True change nothing while enabled=False."""
        root = tmp_path / "repo"
        root.mkdir()
        (root / "a.py").write_text("def a_fn():\n    return 1\n", encoding="utf-8")
        service = make_service(
            tmp_path, root, project=PROJECT, enabled=False, beacon=True, watch=True
        )
        try:
            with pytest.raises(GraphDisabledError):
                service.index_project(PROJECT, agent=AGENT)
            assert service.beacon_line(PROJECT) is None
            assert service.store.count_nodes(PROJECT) == 0
        finally:
            service.close()

    def test_default_manager_is_inert_until_used(self, tmp_path: Path) -> None:
        """Default settings (graphs ON, owner 2026-09-28): the service
        exists, but NOTHING runs by itself — no watch registrations, no
        beacon without an index, zero nodes anywhere. The graph waits
        for an explicit index_project, not the other way around."""
        settings = Settings(
            mnemos={
                "vault_path": str(tmp_path / "vault"),
                "data_dir": str(tmp_path / "data"),
                "db_name": "test.db",
            },
            scanner={"enabled": False},
        )
        settings.resolve_paths()
        mgr = MemoryManager(settings)
        try:
            service = mgr.get_codegraph_service()
            assert service is not None  # default-on since 2026-09-28
            assert mgr.watch_status()["watch_enabled"] is True
            assert mgr.watch_status()["registrations"] == []
            assert "project-graph" not in mgr.assemble_context(session="s", project=PROJECT)["text"]
            assert service.store.count_nodes(PROJECT) == 0
        finally:
            mgr.close()
