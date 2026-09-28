"""Integration tests for the project-graph tool surface (ADR-0032 PG-0 slices 4+5).

The suite pins the slice contract at THREE layers:

* the token contract (§3.4, ported from DeusData): the 4-UTF-8-bytes-per-
  token ceiling holds, drops are WHOLE-ROW, cursors strictly advance while
  ``has_more``, and a budget that cannot fit even one row is a REFUSAL
  (no self-looping) — PGT-7;
* the 10 tools over ``CodeGraphService``: happy paths for index/status/
  search/trace/outline/snippet/coverage/schema/list/delete, the PG4 snippet
  sequence (fresh → content; changed on disk → staleness marker, never
  content; a secret hit at issuance → fail-closed; poisoned → refusal that
  survives reindexation — PGT-3/PGT-4);
* the PG mechanics: PG7 audit rows on index/read/delete, the per-agent
  attribution binding (anonymous callers are refused before any read),
  PG2 confinement (only a registered project resolves; paths cannot
  escape the root), fail-closed limits at the TOOL level (a breach
  publishes nothing), and the master flag answering ``disabled`` (default
  OFF). The MCP and REST twins are exercised over the same service.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from vesmaro.codegraph.audit import GraphAudit
from vesmaro.codegraph.indexer import IndexLimitError
from vesmaro.codegraph.service import (
    BYTES_PER_TOKEN,
    DEFAULT_MAX_OUTPUT_TOKENS,
    CodeGraphService,
    GraphAttributionError,
    GraphBudgetError,
    GraphConfinementError,
    GraphDisabledError,
    GraphToolError,
    resolve_token_budget,
    window_rows,
)
from vesmaro.config import CodeGraphConfig, Settings
from vesmaro.storage.code_graph_store import CodeGraphStore

AGENT = "agent-under-test"
PROJECT = "miniproj"

# The canonical AWS sample the secrets detector flags (same as the
# indexer suite — detector behaviour itself is NOT under test here).
SECRET_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


# ── fixtures ─────────────────────────────────────────────────────────────────


@dataclass
class FakeProject:
    id: str
    name: str
    paths: list[str] = field(default_factory=list)


class FakeMainStore:
    """Main-DB surface the service is allowed to see (PG2 registration
    plus the meta surface the epoch helpers use)."""

    def __init__(self, *projects: FakeProject) -> None:
        self.projects = list(projects)
        self.meta: dict[str, str] = {}

    def get_project(self, project_id: str) -> FakeProject | None:
        return next((p for p in self.projects if p.id == project_id), None)

    def get_project_by_name(self, name: str) -> FakeProject | None:
        return next((p for p in self.projects if p.name == name), None)

    def list_projects(self) -> list[FakeProject]:
        return list(self.projects)

    def get_meta(self, key: str) -> str | None:
        return self.meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self.meta[key] = value


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    """A small project tree: helper (class + fn), app (imports, subclass,
    call), a test, a snippet-safe file, a huge-name file (self-loop test),
    and a secret file (poisoning)."""
    root = tmp_path / "repo"
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "helper.py").write_text(
        "class Base:\n"
        "    def greet(self):\n"
        "        return 'hi'\n"
        "\n"
        "def make_base():\n"
        "    return Base()\n",
        encoding="utf-8",
    )
    (root / "app.py").write_text(
        "from pkg.helper import Base, make_base\n"
        "\n"
        "class Derived(Base):\n"
        "    pass\n"
        "\n"
        "def run():\n"
        "    b = make_base()\n"
        "    b.greet()\n"
        "    return b\n",
        encoding="utf-8",
    )
    (root / "test_app.py").write_text(
        "from app import Derived\n\ndef test_derived():\n    assert Derived is not None\n",
        encoding="utf-8",
    )
    (root / "notes.py").write_text(
        "LINE_A = 'alpha'\nLINE_B = 'beta'\nLINE_C = 'gamma'\n",
        encoding="utf-8",
    )
    # three ~180-char lines: a 128-token budget (512 bytes) fits only two
    # of them → the snippet token window must drop the third WHOLE line
    filler = "a" * 172
    (root / "longlines.py").write_text(
        f'V1 = "{filler}"\nV2 = "{filler}"\nV3 = "{filler}"\n',
        encoding="utf-8",
    )
    (root / "longname.py").write_text(
        f"def {'x' * 600}():\n    return 1\n",
        encoding="utf-8",
    )
    (root / "secret.py").write_text(f'AWS_ID = "{SECRET_AWS_KEY}"\n', encoding="utf-8")
    return root


def make_service(
    tmp_path: Path, repo: Path, *, register: bool = True, **config: object
) -> tuple[CodeGraphService, FakeMainStore]:
    main = FakeMainStore(FakeProject(id="p-1", name=PROJECT, paths=[str(repo)]))
    if not register:
        main.projects.clear()
    service = CodeGraphService(main, tmp_path / "data", CodeGraphConfig(enabled=True, **config))  # type: ignore[arg-type]
    return service, main


@pytest.fixture
def service(tmp_path: Path, mini_repo: Path) -> Iterator[CodeGraphService]:
    svc, _ = make_service(tmp_path, mini_repo)
    yield svc
    svc.close()


@pytest.fixture
def indexed(service: CodeGraphService) -> CodeGraphService:
    result = service.index_project(PROJECT, agent=AGENT, session="sess-1")
    assert result["status"] == "ok", result
    return service


def _row_bytes(row: dict[str, object]) -> int:
    """Mirror of the service row cost: serialized row + newline."""
    return len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8")) + 1


# ── token contract primitives (§3.4) ────────────────────────────────────────


class TestTokenContractUnits:
    def test_budget_validation(self) -> None:
        assert resolve_token_budget(None) == DEFAULT_MAX_OUTPUT_TOKENS
        for bad in (127, 1_000_001, True, "3200", 3.2):
            with pytest.raises(GraphBudgetError):
                resolve_token_budget(bad)
        assert resolve_token_budget(128) == 128
        assert resolve_token_budget(1_000_000) == 1_000_000

    def test_window_rows_whole_row_drop_and_ceiling(self) -> None:
        rows = [{"i": i, "pad": "y" * 300} for i in range(4)]
        budget_tokens = 128
        page, has_more, cursor = window_rows(rows, budget_tokens, 0)
        # 128 tokens x 4 bytes = 512-byte ceiling; each row ~310 bytes →
        # exactly one row fits, and rows are NEVER split mid-row.
        assert page == [rows[0]]
        assert has_more is True
        assert cursor == 1  # strictly advanced
        spent = sum(_row_bytes(r) for r in page)
        assert spent <= budget_tokens * BYTES_PER_TOKEN

    def test_window_rows_refuses_when_nothing_fits(self) -> None:
        """The self-loop refusal: a budget that fits zero rows asks for
        more budget, it never returns an empty page with the same cursor."""
        rows = [{"text": "x" * 2000}]
        with pytest.raises(GraphBudgetError, match="bigger budget"):
            window_rows(rows, 128, 0)

    def test_window_rows_final_page_keeps_cursor_semantics(self) -> None:
        rows = [{"i": 1}, {"i": 2}]
        page, has_more, cursor = window_rows(rows, DEFAULT_MAX_OUTPUT_TOKENS, 0)
        assert page == rows and has_more is False and cursor == 0


class TestTokenContractSearch:
    def test_ceiling_and_opt_in_signatures(self, indexed: CodeGraphService) -> None:
        result = indexed.search_graph(PROJECT, "Base", agent=AGENT, max_output_tokens=640, limit=50)
        budget_bytes = 640 * BYTES_PER_TOKEN
        spent = sum(_row_bytes(r) for r in result["results"])
        assert spent <= budget_bytes
        assert all("signature" not in r for r in result["results"])
        with_sig = indexed.search_graph(
            PROJECT, "Base", agent=AGENT, max_output_tokens=640, include_signature=True
        )
        assert any(r.get("signature") for r in with_sig["results"])

    def test_ranking_exact_first(self, indexed: CodeGraphService) -> None:
        result = indexed.search_graph(PROJECT, "Base", agent=AGENT)
        assert result["results"], "expected matches"
        assert result["results"][0]["name"] == "Base"

    def test_cursor_advances_strictly_and_covers_all_matches(
        self, tmp_path: Path, mini_repo: Path
    ) -> None:
        # drop the huge-name file: its row is itself a budget refusal,
        # which the dedicated self-loop test covers
        (mini_repo / "longname.py").unlink()
        service, _ = make_service(tmp_path, mini_repo)
        try:
            service.index_project(PROJECT, agent=AGENT)
            seen: list[str] = []
            cursor = 0
            previous_cursor = -1
            for _ in range(50):
                page = service.search_graph(
                    PROJECT,
                    "a",  # matches nearly every symbol/path in the fixture
                    agent=AGENT,
                    max_output_tokens=128,  # 512-byte pages ≈ 2 rows
                    cursor=cursor,
                )
                ids = [r["id"] for r in page["results"]]
                assert not (set(ids) & set(seen)), "a cursor page repeated rows"
                seen.extend(ids)
                if page["has_more"]:
                    assert page["cursor"] > cursor, "cursor did not strictly advance"
                    previous_cursor = cursor
                    cursor = page["cursor"]
                else:
                    break
            else:  # pragma: no cover — loop guard
                pytest.fail("pagination did not terminate")
            assert previous_cursor != -1, "expected at least two pages"
            total = service.search_graph(PROJECT, "a", agent=AGENT)["total_matches"]
            assert len(seen) == total
        finally:
            service.close()

    def test_budget_refused_when_no_row_fits(self, indexed: CodeGraphService) -> None:
        long_name = "x" * 600
        with pytest.raises(GraphBudgetError, match="bigger budget"):
            indexed.search_graph(PROJECT, long_name, agent=AGENT, max_output_tokens=128)


# ── tool happy paths ─────────────────────────────────────────────────────────


class TestToolHappyPaths:
    def test_index_and_status(self, indexed: CodeGraphService) -> None:
        status = indexed.status(PROJECT, agent=AGENT)
        assert status["nodes"] > 0 and status["files"] > 0
        assert status["poisoned_count"] == 1  # secret.py
        assert "secret.py" in status["parse_errors"]
        assert status["staleness"]["fresh_percent"] == 100.0
        assert status["staleness"]["last_indexed_at"] is not None

    def test_incremental_fresh_has_no_fake_staleness(self, indexed: CodeGraphService) -> None:
        again = indexed.index_project(PROJECT, agent=AGENT)
        assert again["status"] == "fresh"
        assert again["staleness"] is None

    def test_trace_path(self, indexed: CodeGraphService) -> None:
        derived = indexed.search_graph(PROJECT, "Derived", agent=AGENT)["results"][0]
        trace = indexed.trace_path(PROJECT, derived["qname"], agent=AGENT, depth=2)
        assert trace["start"] == derived["qname"]
        assert trace["nodes"][0]["depth"] == 0
        assert any(e["kind"] == "INHERITS" for e in trace["edges"])
        with pytest.raises(GraphToolError, match="depth"):
            indexed.trace_path(PROJECT, derived["qname"], agent=AGENT, depth=3)

    def test_trace_symbol_resolution_ambiguous_refused(self, indexed: CodeGraphService) -> None:
        with pytest.raises(GraphToolError, match="not found or ambiguous"):
            indexed.trace_path(PROJECT, "no.such.symbol", agent=AGENT)

    def test_file_outline(self, indexed: CodeGraphService) -> None:
        outline = indexed.get_file_outline(PROJECT, "pkg/helper.py", agent=AGENT)
        names = {row["name"] for row in outline["outline"]}
        assert {"Base", "make_base"} <= names
        assert outline["parse_error"] is None

    def test_coverage_verdicts(self, indexed: CodeGraphService) -> None:
        verdicts = {
            v["path"]: v["verdict"]
            for v in indexed.check_coverage(
                PROJECT, ["notes.py", "never-indexed.py", "secret.py"], agent=AGENT
            )["coverage"]
        }
        assert verdicts["notes.py"] == "indexed"
        assert verdicts["never-indexed.py"] == "unindexed"
        assert verdicts["secret.py"] == "poisoned"

    def test_coverage_stale_verdict(self, indexed: CodeGraphService, mini_repo: Path) -> None:
        (mini_repo / "notes.py").write_text("LINE_A = 'alpha'\nCHANGED = 1\n", encoding="utf-8")
        verdicts = indexed.check_coverage(PROJECT, ["notes.py"], agent=AGENT)["coverage"]
        assert verdicts[0]["verdict"] == "stale"

    def test_schema_and_list_projects(self, indexed: CodeGraphService) -> None:
        schema = indexed.get_graph_schema(PROJECT, agent=AGENT)
        assert schema["schema_version"] == 1
        assert "Function" in schema["node_kinds"]
        assert schema["token_contract"]["bytes_per_token"] == 4
        assert schema["volumes"]["nodes"] > 0
        projects = indexed.list_graph_projects(agent=AGENT)["projects"]
        row = next(p for p in projects if p["project"] == PROJECT)
        assert row["registered"] is True and row["nodes"] > 0

    def test_delete_drops_index_not_registration(
        self, indexed: CodeGraphService, tmp_path: Path, mini_repo: Path
    ) -> None:
        result = indexed.delete_graph_project(PROJECT, agent=AGENT, reason="cleanup")
        assert result["status"] == "deleted"
        assert indexed.store.count_nodes("miniproj") == 0
        assert indexed.store.get_poisoned_paths("miniproj") == set()
        # the project REGISTRATION (main DB) survives
        assert indexed._main.get_project_by_name(PROJECT) is not None


# ── PG4: the snippet sequence ────────────────────────────────────────────────


class TestSnippetPG4:
    def test_fresh_snippet_issues_scanned_content(self, indexed: CodeGraphService) -> None:
        snippet = indexed.get_code_snippet(PROJECT, "notes.py", 1, 2, agent=AGENT)
        assert snippet["content"] == "LINE_A = 'alpha'\nLINE_B = 'beta'"
        assert snippet["scanned"] is True and snippet["stale"] is False
        # the whole requested range fit — nothing left over
        assert snippet["has_more"] is False and snippet["next_start_line"] is None

    def test_snippet_budget_truncates_range_whole_lines(self, indexed: CodeGraphService) -> None:
        snippet = indexed.get_code_snippet(
            PROJECT, "longlines.py", 1, 3, agent=AGENT, max_output_tokens=128
        )
        lines = snippet["content"].splitlines()
        budget_bytes = 128 * BYTES_PER_TOKEN
        assert sum(len(line.encode("utf-8")) + 1 for line in lines) <= budget_bytes
        assert snippet["has_more"] is True
        assert snippet["next_start_line"] == len(lines) + 1

    def test_snippet_token_budget_whole_lines(self, indexed: CodeGraphService) -> None:
        snippet = indexed.get_code_snippet(
            PROJECT, "notes.py", 1, 3, agent=AGENT, max_output_tokens=128
        )
        budget_bytes = 128 * BYTES_PER_TOKEN
        lines = snippet["content"].splitlines()
        assert lines, "at least one line must survive"
        assert sum(len(line.encode("utf-8")) + 1 for line in lines) <= budget_bytes

    def test_changed_on_disk_is_a_marker_never_content(
        self, indexed: CodeGraphService, mini_repo: Path
    ) -> None:
        (mini_repo / "notes.py").write_text("LINE_A = 'alpha'\nSECRET_HERE\n", encoding="utf-8")
        with pytest.raises(GraphToolError, match="CHANGED on disk"):
            indexed.get_code_snippet(PROJECT, "notes.py", 1, 2, agent=AGENT)

    def test_secret_hit_at_issuance_fails_closed(
        self, indexed: CodeGraphService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A detector that learned a NEW pattern after indexation refuses
        the issuance fail-closed (the scan at issue is mandatory, PG4)."""
        import vesmaro.codegraph.service as service_module

        monkeypatch.setattr(service_module, "detect_secrets", lambda content: [object()])
        monkeypatch.setattr(
            service_module, "findings_by_pattern", lambda findings: {"new-pattern": 1}
        )
        with pytest.raises(GraphToolError, match="fail-closed"):
            indexed.get_code_snippet(PROJECT, "notes.py", 1, 2, agent=AGENT)
        reasons = [r["reason"] for r in indexed._audit.recent(PROJECT)]
        assert "secret-refusal" in reasons

    def test_poisoned_refused_forever_even_when_rescan_misses(
        self, service: CodeGraphService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PGT-3 «навсегда»: index a secret file → refusal; reindex with a
        detector that MISSES (pattern drift) → still refused; only the
        delete tool clears it."""
        service.index_project(PROJECT, agent=AGENT)
        with pytest.raises(GraphToolError, match="POISONED"):
            service.get_code_snippet(PROJECT, "secret.py", 1, 1, agent=AGENT)

        import vesmaro.codegraph.indexer as indexer_module

        monkeypatch.setattr(indexer_module, "detect_secrets", lambda source: [])
        reindexed = service.index_project(PROJECT, agent=AGENT, incremental=False)
        assert reindexed["status"] == "ok"
        assert reindexed["poisoned"] == []  # the scan genuinely missed

        with pytest.raises(GraphToolError, match="POISONED"):
            service.get_code_snippet(PROJECT, "secret.py", 1, 1, agent=AGENT)

        service.delete_graph_project(PROJECT, agent=AGENT)
        with pytest.raises(GraphToolError, match="not indexed"):
            service.get_code_snippet(PROJECT, "secret.py", 1, 1, agent=AGENT)


# ── PG mechanics: audit, attribution, confinement, flag, limits ─────────────


class TestPGMechanics:
    def test_audit_rows_on_index_read_delete(self, service: CodeGraphService) -> None:
        service.index_project(PROJECT, agent=AGENT, session="s1", reason="wave")
        service.status(PROJECT, agent=AGENT, session="s1")
        service.search_graph(PROJECT, "Base", agent=AGENT, session="s1")
        service.delete_graph_project(PROJECT, agent=AGENT, session="s1")
        rows = service._audit.recent(PROJECT)
        actions = [r["action"] for r in rows]
        assert "index" in actions
        assert "delete" in actions
        graph_reads = [r for r in rows if r["action"] == "graph-read"]
        assert graph_reads and all(r["actor"] == AGENT for r in graph_reads)
        assert all(r["session"] == "s1" for r in rows)

    def test_attribution_is_a_binding(self, service: CodeGraphService) -> None:
        for bad in ("", "   ", None):
            with pytest.raises(GraphAttributionError):
                service.index_project(PROJECT, agent=bad)  # type: ignore[arg-type]
            with pytest.raises(GraphAttributionError):
                service.status(PROJECT, agent=bad)  # type: ignore[arg-type]
        with pytest.raises(GraphAttributionError, match="non-empty string"):
            service.status(PROJECT, agent=AGENT, session=42)  # type: ignore[arg-type]
        # NO anonymous rows in the audit trail
        service.index_project(PROJECT, agent=AGENT)
        assert all(r["actor"] == AGENT for r in service._audit.recent(PROJECT))

    def test_flag_off_answers_disabled(self, tmp_path: Path, mini_repo: Path) -> None:
        main = FakeMainStore(FakeProject(id="p-1", name=PROJECT, paths=[str(mini_repo)]))
        service = CodeGraphService(main, tmp_path / "data", CodeGraphConfig(enabled=False))  # type: ignore[arg-type]
        with pytest.raises(GraphDisabledError, match="disabled"):
            service.index_project(PROJECT, agent=AGENT)
        with pytest.raises(GraphDisabledError):
            service.status(PROJECT, agent=AGENT)
        with pytest.raises(GraphDisabledError):
            service.search_graph(PROJECT, "Base", agent=AGENT)
        service.close()

    def test_pg2_only_registered_projects(self, tmp_path: Path, mini_repo: Path) -> None:
        service, _ = make_service(tmp_path, mini_repo, register=False)
        # an arbitrary path is NEVER a project — the tool does not even
        # accept a root parameter (PGT-2 at the tool layer)
        with pytest.raises(GraphConfinementError, match="not registered"):
            service.index_project(str(mini_repo), agent=AGENT)
        with pytest.raises(GraphConfinementError, match="not registered"):
            service.index_project("no-such-project", agent=AGENT)
        service.close()

    def test_pg2_paths_cannot_escape_root(self, indexed: CodeGraphService) -> None:
        for bad in ("../outside.py", "../../etc/passwd", "/etc/passwd", "pkg\\evil.py"):
            with pytest.raises(GraphConfinementError):
                indexed.get_file_outline(PROJECT, bad, agent=AGENT)
        with pytest.raises(GraphConfinementError):
            indexed.check_coverage(PROJECT, ["../../secrets.env"], agent=AGENT)

    def test_pg7_limit_breach_publishes_nothing(self, tmp_path: Path, mini_repo: Path) -> None:
        service, _ = make_service(tmp_path, mini_repo, index_max_files=1)
        with pytest.raises(IndexLimitError):
            service.index_project(PROJECT, agent=AGENT)
        # fail-closed: NO partial graph was published
        assert service.store.count_nodes(PROJECT) == 0
        assert service.store.count_files(PROJECT) == 0
        rows = service._audit.recent(PROJECT)
        assert any(r["details"].get("outcome") == "limit-refused" for r in rows)
        service.close()


# ── MCP layer ────────────────────────────────────────────────────────────────


class _FakeManager:
    """Manager duck-type for the MCP layer (weak-referenceable — the
    service registry weak-keys on the manager)."""

    def __init__(self, tmp_path: Path, repo: Path, *, enabled: bool) -> None:
        self.sqlite = FakeMainStore(FakeProject(id="p-1", name=PROJECT, paths=[str(repo)]))
        self.settings = SimpleNamespace(
            mnemos=SimpleNamespace(data_dir=str(tmp_path / "data")),
            code_graph=CodeGraphConfig(enabled=enabled),
        )


def _fake_manager(tmp_path: Path, repo: Path, *, enabled: bool) -> _FakeManager:
    return _FakeManager(tmp_path, repo, enabled=enabled)


class TestMcpLayer:
    def test_manifest_contains_the_10_graph_tools(self) -> None:
        from vesmaro.mcp_server import _canonical_tools

        names = {t.name for t in asyncio.run(_canonical_tools())}
        expected = {
            "mnemos_index_project",
            "mnemos_project_graph_status",
            "mnemos_search_graph",
            "mnemos_trace_path",
            "mnemos_get_file_outline",
            "mnemos_get_code_snippet",
            "mnemos_check_graph_coverage",
            "mnemos_get_graph_schema",
            "mnemos_list_graph_projects",
            "mnemos_delete_graph_project",
        }
        assert expected <= names

    def test_missing_agent_is_a_boundary_error(self, tmp_path: Path, mini_repo: Path) -> None:
        from vesmaro.mcp_server import _handle_graph

        mgr = _fake_manager(tmp_path, mini_repo, enabled=True)
        result = _handle_graph("mnemos_project_graph_status", mgr, {"project_id": PROJECT})
        assert result["code"] == "attribution-required"

    def test_disabled_flag_answered_as_disabled(self, tmp_path: Path, mini_repo: Path) -> None:
        from vesmaro.mcp_server import _handle_graph

        mgr = _fake_manager(tmp_path, mini_repo, enabled=False)
        result = _handle_graph(
            "mnemos_project_graph_status", mgr, {"project_id": PROJECT, "agent": AGENT}
        )
        assert result["code"] == "disabled"

    def test_index_via_mcp_handler(self, tmp_path: Path, mini_repo: Path) -> None:
        from vesmaro.mcp_server import _handle_graph

        mgr = _fake_manager(tmp_path, mini_repo, enabled=True)
        result = _handle_graph(
            "mnemos_index_project",
            mgr,
            {"project_id": PROJECT, "agent": AGENT, "session": "s"},
        )
        assert result["status"] == "ok"
        status = _handle_graph(
            "mnemos_project_graph_status", mgr, {"project_id": PROJECT, "agent": AGENT}
        )
        assert status["nodes"] > 0


# ── REST twins ───────────────────────────────────────────────────────────────


def _rest_settings(tmp_path: Path, *, enabled: bool) -> Settings:
    settings = Settings(
        mnemos={
            "vault_path": str(tmp_path / "vault"),
            "data_dir": str(tmp_path / "data"),
            "db_name": "test.db",
        },
        embedding={"provider": "onnx"},
        scanner={"enabled": False},
        code_graph={"enabled": enabled},
    )
    settings.resolve_paths()
    return settings


def _rest_client(tmp_path: Path, repo: Path, *, enabled: bool) -> Iterator[TestClient]:
    from fastapi import FastAPI

    from vesmaro.api import main as api_main
    from vesmaro.api.main import app, lifespan
    from vesmaro.manager import MemoryManager
    from vesmaro.models import Project

    mgr = MemoryManager(_rest_settings(tmp_path, enabled=enabled))
    mgr.sqlite.save_project(Project(name="restproj", paths=[str(repo)]))
    test_app = FastAPI(title="Mnemos-Graph-Test", version="0.0.0", lifespan=lifespan)
    for route in app.routes:
        test_app.routes.append(route)
    api_main._manager = mgr
    try:
        with TestClient(test_app) as tc:
            yield tc
    finally:
        api_main._manager = None
        mgr.close()


@pytest.fixture
def rest_client(tmp_path: Path, mini_repo: Path) -> Iterator[TestClient]:
    yield from _rest_client(tmp_path, mini_repo, enabled=True)


@pytest.fixture
def rest_client_disabled(tmp_path: Path, mini_repo: Path) -> Iterator[TestClient]:
    yield from _rest_client(tmp_path, mini_repo, enabled=False)


class TestRestTwins:
    def test_schema_twin(self, rest_client: TestClient) -> None:
        resp = rest_client.get("/graph/schema", params={"agent": "tester"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["schema_version"] == 1
        assert "Function" in body["node_kinds"]

    def test_index_status_search_twins(self, rest_client: TestClient) -> None:
        resp = rest_client.post(
            "/graph/index",
            json={"project_id": "restproj", "agent": "tester", "session": "s"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        status = rest_client.get(
            "/graph/status/restproj", params={"agent": "tester", "session": "s"}
        )
        assert status.status_code == 200
        assert status.json()["nodes"] > 0

        search = rest_client.post(
            "/graph/search",
            json={"project_id": "restproj", "query": "Base", "agent": "tester"},
        )
        assert search.status_code == 200
        assert search.json()["results"][0]["name"] == "Base"

    def test_delete_twin(self, rest_client: TestClient) -> None:
        rest_client.post("/graph/index", json={"project_id": "restproj", "agent": "tester"})
        resp = rest_client.request(
            "DELETE", "/graph/projects/restproj", json={"agent": "tester", "reason": "twin"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

    def test_confinement_is_403(self, rest_client: TestClient) -> None:
        resp = rest_client.post(
            "/graph/index", json={"project_id": "/etc/passwd", "agent": "tester"}
        )
        assert resp.status_code == 403

    def test_missing_attribution_is_400(self, rest_client: TestClient) -> None:
        resp = rest_client.get("/graph/schema", params={"agent": ""})
        assert resp.status_code == 400

    def test_flag_off_is_503(self, rest_client_disabled: TestClient) -> None:
        resp = rest_client_disabled.get("/graph/schema", params={"agent": "tester"})
        assert resp.status_code == 503


# ── the audit trail is inspectable (slice-5 surface) ─────────────────────────


class TestAuditSurface:
    def test_graph_audit_over_sidecar_file(self, tmp_path: Path) -> None:
        store = CodeGraphStore(tmp_path / "data")  # owns the sidecar file
        audit = GraphAudit(store.db_path)  # its own connection, same file
        audit.record("p", "index", "agent-a", session="s1", reason="wave", details={"nodes": 3})
        rows = audit.recent("p")
        assert len(rows) == 1
        assert rows[0]["actor"] == "agent-a" and rows[0]["details"]["nodes"] == 3
        with pytest.raises(ValueError, match="actor"):
            audit.record("p", "index", "  ")  # PG7 binding lives in the writer too
        with pytest.raises(ValueError, match="action"):
            audit.record("p", "teleport", "agent-a")
        audit.close()
        store.close()

    def test_store_sidecar_is_single_file(self, tmp_path: Path, mini_repo: Path) -> None:
        service, _ = make_service(tmp_path, mini_repo)
        service.index_project(PROJECT, agent=AGENT)
        assert Path(service.store.db_path).name == "code_graph.db"
        service.close()
