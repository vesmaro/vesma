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

pytest.importorskip("tree_sitter_python", reason="code-graph extra not installed")
from fastapi.testclient import TestClient

from vesma.codegraph.audit import GraphAudit
from vesma.codegraph.indexer import IndexLimitError
from vesma.codegraph.service import (
    BYTES_PER_TOKEN,
    DEFAULT_MAX_OUTPUT_TOKENS,
    POISONED_FIXTURE_HINT,
    CodeGraphService,
    GraphAttributionError,
    GraphBudgetError,
    GraphConfinementError,
    GraphDisabledError,
    GraphToolError,
    _is_forbidden_root,
    poisoned_fixture_hint,
    resolve_token_budget,
    window_rows,
)
from vesma.config import CodeGraphConfig, Settings
from vesma.storage.code_graph_store import CodeGraphStore

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
    description: str = ""


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

    def save_project(self, project: object) -> None:
        """Upsert by id (the register/repoint lifecycle, #450/#454)."""
        for i, existing in enumerate(self.projects):
            if existing.id == project.id:
                self.projects[i] = project
                return
        self.projects.append(project)

    def delete_project(self, project_id: str) -> bool:
        """Remove by id OR name (the ghost-delete surface)."""
        before = len(self.projects)
        self.projects = [p for p in self.projects if project_id not in (p.id, p.name)]
        return len(self.projects) < before

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
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'miniproj'\n", encoding="utf-8"
    )  # packaging marker: the register/repoint root gate (#450/#454)
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

    def test_total_matches_is_the_honest_count_not_the_page_slice(self, tmp_path: Path) -> None:
        # Review 10173a2a-5: with >2x limit matches the cursor pages the
        # top-limit slice; total_matches derived from that slice
        # undercounted (60 matches, limit 10 → reported 10).
        repo = tmp_path / "many"
        repo.mkdir()
        lines = [f"def batch_target_{i:02d}():\n    return {i}\n\n" for i in range(60)]
        (repo / "batch.py").write_text("".join(lines), encoding="utf-8")
        service, _ = make_service(tmp_path, repo)
        try:
            service.index_project(PROJECT, agent=AGENT)
            result = service.search_graph(PROJECT, "batch_target", agent=AGENT, limit=10)
            assert len(result["results"]) == 10
            assert result["total_matches"] == 60
        finally:
            service.close()


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

    def test_trace_symbol_resolution_missing_refused(self, indexed: CodeGraphService) -> None:
        # W-H: a missing symbol stays a clear not-found refusal (the
        # AMBIGUOUS case is no longer an error — see the hybrid suite).
        with pytest.raises(GraphToolError, match="not found in the project graph"):
            indexed.trace_path(PROJECT, "no.such.symbol", agent=AGENT)

    def test_file_outline(self, indexed: CodeGraphService) -> None:
        outline = indexed.get_file_outline(PROJECT, "pkg/helper.py", agent=AGENT)
        names = {row["name"] for row in outline["outline"]}
        assert {"Base", "make_base"} <= names
        assert outline["parse_error"] is None

    def test_coverage_verdicts(self, indexed: CodeGraphService, mini_repo: Path) -> None:
        # A REAL file the index has not covered (created after indexing):
        # this is what "unindexed" means — distinct from a path that
        # does not exist on disk (#452b: "missing").
        (mini_repo / "added_later.py").write_text("X = 1\n", encoding="utf-8")
        verdicts = {
            v["path"]: v["verdict"]
            for v in indexed.check_coverage(
                PROJECT,
                ["notes.py", "added_later.py", "secret.py", "never-existed.py"],
                agent=AGENT,
            )["coverage"]
        }
        assert verdicts["notes.py"] == "indexed"
        assert verdicts["added_later.py"] == "unindexed"
        assert verdicts["secret.py"] == "poisoned"
        assert verdicts["never-existed.py"] == "missing"

    def test_coverage_stale_verdict(self, indexed: CodeGraphService, mini_repo: Path) -> None:
        (mini_repo / "notes.py").write_text("LINE_A = 'alpha'\nCHANGED = 1\n", encoding="utf-8")
        verdicts = indexed.check_coverage(PROJECT, ["notes.py"], agent=AGENT)["coverage"]
        assert verdicts[0]["verdict"] == "stale"

    def test_schema_and_list_projects(self, indexed: CodeGraphService) -> None:
        schema = indexed.get_graph_schema(PROJECT, agent=AGENT)
        # Schema v2 (card vesma-graph-command-route-nodes): the
        # Command/Route node kinds and the INVOKES/HANDLES edge kinds
        # are part of the reported contract.
        assert schema["schema_version"] == 2
        assert "Function" in schema["node_kinds"]
        assert "Command" in schema["node_kinds"]
        assert "Route" in schema["node_kinds"]
        assert "INVOKES" in schema["edge_kinds"]
        assert "HANDLES" in schema["edge_kinds"]
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
        import vesma.codegraph.service as service_module

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

        import vesma.codegraph.indexer as indexer_module

        monkeypatch.setattr(indexer_module, "detect_secrets", lambda source: [])
        reindexed = service.index_project(PROJECT, agent=AGENT, incremental=False)
        assert reindexed["status"] == "ok"
        assert reindexed["poisoned"] == []  # the scan genuinely missed

        with pytest.raises(GraphToolError, match="POISONED"):
            service.get_code_snippet(PROJECT, "secret.py", 1, 1, agent=AGENT)

        service.delete_graph_project(PROJECT, agent=AGENT)
        with pytest.raises(GraphToolError, match="not indexed"):
            service.get_code_snippet(PROJECT, "secret.py", 1, 1, agent=AGENT)


# ── PG3 + secret_allowlist (issue #449) ──────────────────────────────────────


class TestSecretAllowlistService:
    def test_allowlisted_fixture_snippet_readable_after_reindex(
        self, tmp_path: Path, mini_repo: Path
    ) -> None:
        """#449 acceptance: index (poisoned) → allowlist the fixture in
        config → reindex → the snippet path issues (the PG3 refusal is
        lifted) and the un-poison carries an audit row. PG4 is NOT
        waived: a requested range that itself trips the detector still
        refuses — the test reads the fixture's CLEAN line."""
        fixture = mini_repo / "fake_key_fixture.py"
        fixture.write_text(
            f'AWS_ID = "{SECRET_AWS_KEY}"\nBODY = "clean fixture line"\n', encoding="utf-8"
        )
        service, _ = make_service(tmp_path, mini_repo)
        first = service.index_project(PROJECT, agent=AGENT)
        assert first["poisoned"] == ["fake_key_fixture.py", "secret.py"]
        # PG3 «навсегда»: even the CLEAN line of a poisoned file refuses.
        with pytest.raises(GraphToolError, match="POISONED"):
            service.get_code_snippet(PROJECT, "fake_key_fixture.py", 2, 2, agent=AGENT)
        service.close()

        allowlisted, _ = make_service(tmp_path, mini_repo, secret_allowlist=["fake_key*"])
        try:
            again = allowlisted.index_project(PROJECT, agent=AGENT, incremental=False)
            assert again["status"] == "ok"
            assert "fake_key_fixture.py" not in again["poisoned"]
            assert again["unpoisoned"] == ["fake_key_fixture.py"]
            snippet = allowlisted.get_code_snippet(
                PROJECT, "fake_key_fixture.py", 2, 2, agent=AGENT
            )
            assert snippet["content"] == 'BODY = "clean fixture line"'
            rows = [
                r for r in allowlisted._audit.recent(PROJECT) if r["reason"] == "allowlist-unpoison"
            ]
            assert rows and rows[0]["details"]["paths"] == ["fake_key_fixture.py"]
        finally:
            allowlisted.close()

    def test_allowlisted_fixture_secret_line_still_refuses_pg4(
        self, tmp_path: Path, mini_repo: Path
    ) -> None:
        """#449 review P2-1: the allowlist lifts only the index-time PG3
        marking; the issuance-time scan (PG4) is NOT waived — a requested
        range that itself trips the detector still refuses fail-closed."""
        fixture = mini_repo / "fake_key_fixture.py"
        fixture.write_text(
            f'AWS_ID = "{SECRET_AWS_KEY}"\nBODY = "clean fixture line"\n', encoding="utf-8"
        )
        allowlisted, _ = make_service(tmp_path, mini_repo, secret_allowlist=["fake_key*"])
        try:
            allowlisted.index_project(PROJECT, agent=AGENT)
            with pytest.raises(GraphToolError, match="PG4"):
                allowlisted.get_code_snippet(PROJECT, "fake_key_fixture.py", 1, 1, agent=AGENT)
        finally:
            allowlisted.close()

    def test_non_allowlisted_poisoning_unchanged(self, tmp_path: Path, mini_repo: Path) -> None:
        service, _ = make_service(tmp_path, mini_repo, secret_allowlist=["docs/**"])
        try:
            result = service.index_project(PROJECT, agent=AGENT)
            assert result["poisoned"] == ["secret.py"]
            assert result["unpoisoned"] == []
            with pytest.raises(GraphToolError, match="POISONED"):
                service.get_code_snippet(PROJECT, "secret.py", 1, 1, agent=AGENT)
            assert all(r["reason"] != "allowlist-unpoison" for r in service._audit.recent(PROJECT))
        finally:
            service.close()


class TestPoisonedFixtureHint:
    """W-G graph adoption: status points at the allowlist escape hatch
    when the poisoned set is entirely test-fixture noise — and stays
    silent when any poisoned path is NOT test-like (real scrutiny)."""

    @staticmethod
    def _fixture_repo(tmp_path: Path) -> Path:
        """A repo whose ONLY poisoned file lives under ``tests/``."""
        repo = tmp_path / "fixture-repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "pyproject.toml").write_text("[project]\nname = 'fixt'\n", encoding="utf-8")
        (repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
        (repo / "tests" / "fake_key_fixture.py").write_text(
            f'AWS_ID = "{SECRET_AWS_KEY}"\n', encoding="utf-8"
        )
        return repo

    def test_unit_all_testlike_paths_hint(self) -> None:
        hint = poisoned_fixture_hint(["tests/fixtures/fake.py", "benchmarks/bench_secret.py"])
        assert hint == [POISONED_FIXTURE_HINT]
        assert "secret_allowlist" in hint[0]

    def test_unit_real_or_mixed_or_empty_no_hint(self) -> None:
        assert poisoned_fixture_hint([]) == []
        assert poisoned_fixture_hint(["secret.py"]) == []
        # One real poisoned path suppresses the hint — mixed sets are scrutiny.
        assert poisoned_fixture_hint(["tests/fixtures/fake.py", "secret.py"]) == []
        assert poisoned_fixture_hint(["tests/fake.py", "src/leak.py"]) == []

    def test_status_hints_on_all_fixture_poisoning(self, tmp_path: Path) -> None:
        repo = self._fixture_repo(tmp_path)
        service, _ = make_service(tmp_path, repo)
        try:
            result = service.index_project(PROJECT, agent=AGENT)
            assert result["poisoned"] == ["tests/fake_key_fixture.py"]
            status = service.status(PROJECT, agent=AGENT)
            assert status["poisoned_count"] == 1
            assert status["hints"] == [POISONED_FIXTURE_HINT]
        finally:
            service.close()

    def test_status_no_hint_for_real_poison(self, indexed: CodeGraphService) -> None:
        # The mini_repo poison (secret.py at the repo root) is NOT test-like.
        status = indexed.status(PROJECT, agent=AGENT)
        assert status["poisoned_count"] == 1
        assert status["hints"] == []

    def test_status_no_hint_after_partial_allowlist_unpoison(
        self, tmp_path: Path, mini_repo: Path
    ) -> None:
        """Allowlisting the fixture tree drains the fixture poison; the
        remaining REAL poison (secret.py) keeps the hint silent."""
        fixture = mini_repo / "tests" / "fake_key_fixture.py"
        fixture.parent.mkdir(exist_ok=True)
        fixture.write_text(f'AWS_ID = "{SECRET_AWS_KEY}"\n', encoding="utf-8")
        first, _ = make_service(tmp_path, mini_repo)
        try:
            first.index_project(PROJECT, agent=AGENT)
        finally:
            first.close()
        service, _ = make_service(tmp_path, mini_repo, secret_allowlist=["tests/**"])
        try:
            again = service.index_project(PROJECT, agent=AGENT, incremental=False)
            assert again["unpoisoned"] == ["tests/fake_key_fixture.py"]
            status = service.status(PROJECT, agent=AGENT)
            assert status["poisoned_count"] == 1  # secret.py remains
            assert status["hints"] == []  # remaining poison is real, not fixtures
        finally:
            service.close()


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

    def test_first_search_marked_per_task_session(self, indexed: CodeGraphService) -> None:
        # Card vesma-graph-audit-firstcall-marking: the FIRST search call
        # of each task (actor+session scoped) carries details.first_search
        # on its own audit row; later calls of the same task carry none
        # (shape policy: absent, never null/empty). The marker makes the
        # graph-first share computable from graph_audit rows alone
        # (computing query: GraphAudit.has_search docstring).
        indexed.search_graph(PROJECT, "Base", agent=AGENT, session="s1")
        indexed.search_graph(PROJECT, "Base", agent=AGENT, session="s1")
        indexed.search_graph(PROJECT, "Base", agent=AGENT, session="s2")
        rows = [
            r
            for r in indexed._audit.recent(PROJECT)
            if r["action"] == "graph-read" and r["reason"] == "search"
        ]
        assert [r["session"] for r in rows] == ["s2", "s1", "s1"]  # newest first
        assert rows[0]["details"]["first_search"] is True  # s2's FIRST call
        assert "first_search" not in rows[1]["details"]  # s1's SECOND call
        assert rows[2]["details"]["first_search"] is True  # s1's FIRST call
        # Scope is (actor, session): a second agent in the SAME session
        # gets its own first-call marker.
        indexed.search_graph(PROJECT, "Base", agent="other-agent", session="s1")
        rows = [
            r
            for r in indexed._audit.recent(PROJECT)
            if r["action"] == "graph-read" and r["reason"] == "search"
        ]
        assert rows[0]["actor"] == "other-agent"
        assert rows[0]["details"]["first_search"] is True

    def test_has_search_index_survives_reopen_and_serves_planner(
        self, indexed: CodeGraphService
    ) -> None:
        # Card vesma-graph-audit-search-index: has_search ran a full
        # graph_audit scan on EVERY search_graph call. The
        # (action, reason, actor, session) index rides the idempotent
        # schema init — create_schema() replays _SCHEMA on EVERY open,
        # so existing stores pick it up without a version bump — and
        # the planner serves the exact has_search SELECT from it.
        indexed.search_graph(PROJECT, "Base", agent=AGENT, session="s1")
        # a SECOND open of the SAME sidecar file (the existing-store path)
        store2 = CodeGraphStore(Path(indexed.store.db_path).parent)
        try:
            audit_idx = {
                str(r["name"])
                for r in store2._conn().execute("PRAGMA index_list(graph_audit)").fetchall()
            }
            assert "idx_graph_audit_search" in audit_idx
            plan = " | ".join(
                str(r["detail"])
                for r in store2._conn()
                .execute(
                    "EXPLAIN QUERY PLAN SELECT 1 FROM graph_audit "
                    "WHERE action='graph-read' AND reason='search' "
                    "AND actor=? AND session IS ? LIMIT 1",
                    (AGENT, "s1"),
                )
                .fetchall()
            )
            # planner must name the index (often as a COVERING index);
            # a bare "SCAN graph_audit" would mean the full-scan regression
            assert "idx_graph_audit_search" in plan
            assert "SCAN graph_audit" not in plan
        finally:
            store2.close()
        # has_search semantics are unchanged with the index in place
        audit = GraphAudit(indexed.store.db_path)
        try:
            assert audit.has_search(AGENT, "s1") is True
            assert audit.has_search(AGENT, "s2") is False
        finally:
            audit.close()

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

    def test_pg7_limit_breach_audit_carries_no_absolute_root(
        self, tmp_path: Path, mini_repo: Path
    ) -> None:
        # Review 10173a2a-4: the audit trail leaves the process (sidecar
        # DB), so the absolute registered root must not ride it — the
        # basename stays; the RAISED message keeps the full root for the
        # process log.
        service, _ = make_service(tmp_path, mini_repo, index_max_files=1)
        try:
            with pytest.raises(IndexLimitError) as excinfo:
                service.index_project(PROJECT, agent=AGENT)
            assert str(mini_repo) in str(excinfo.value)  # log stays informative
            rows = service._audit.recent(PROJECT)
            breach = [r for r in rows if r["details"].get("outcome") == "limit-refused"]
            assert breach, "expected the limit-refused audit row"
            dumped = json.dumps([r["reason"] for r in breach] + [str(r["details"]) for r in breach])
            assert str(mini_repo) not in dumped  # no absolute prefix anywhere
            assert mini_repo.name in dumped  # basename keeps it actionable
        finally:
            service.close()


# ── #450: ghost re-point ─────────────────────────────────────────────────────


class TestGhostRepoint:
    """A registration whose root moved on disk is STUCK pre-#450: the
    auto path no-ops, the explicit index refuses confinement, the only
    exit was nuclear delete. ``repoint_project`` rewrites the root
    (confinement-gated) and purges the stale index."""

    def test_repoint_updates_root_then_index_succeeds(
        self, tmp_path: Path, mini_repo: Path
    ) -> None:
        service, main = make_service(tmp_path, mini_repo)
        try:
            service.index_project(PROJECT, agent=AGENT)
            assert service.store.count_nodes(PROJECT) > 0
            moved = mini_repo.with_name("repo-moved")
            mini_repo.rename(moved)  # the ghost: root gone on disk
            with pytest.raises(GraphConfinementError, match="missing on disk"):
                service.index_project(PROJECT, agent=AGENT)

            result = service.repoint_project(PROJECT, str(moved), agent=AGENT)
            assert result["status"] == "repointed"
            assert result["root"] == str(moved)
            assert result["purged_nodes"] > 0
            # registration rewritten: paths[0] is the new root
            assert main.get_project_by_name(PROJECT).paths[0] == str(moved)
            # the next index succeeds and rebuilds the graph
            rebuilt = service.index_project(PROJECT, agent=AGENT)
            assert rebuilt["status"] == "ok"
            assert service.store.count_nodes(PROJECT) > 0
        finally:
            service.close()

    def test_repoint_refuses_nonexistent_root(self, tmp_path: Path, mini_repo: Path) -> None:
        service, _ = make_service(tmp_path, mini_repo)
        try:
            with pytest.raises(GraphConfinementError, match="does not exist"):
                service.repoint_project(PROJECT, str(tmp_path / "nope"), agent=AGENT)
        finally:
            service.close()

    def test_repoint_refuses_unrelated_directory(self, tmp_path: Path, mini_repo: Path) -> None:
        """The cross-jump guard: a directory with no manifest and no
        ``.git`` is not a project root — refuse loudly."""
        bare = tmp_path / "bare"
        bare.mkdir()
        service, _ = make_service(tmp_path, mini_repo)
        try:
            with pytest.raises(GraphConfinementError, match=r"no packaging manifest and no \.git"):
                service.repoint_project(PROJECT, str(bare), agent=AGENT)
        finally:
            service.close()

    def test_repoint_refuses_root_claimed_by_other_project(
        self, tmp_path: Path, mini_repo: Path
    ) -> None:
        other = tmp_path / "other-repo"
        other.mkdir()
        (other / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        service, _ = make_service(tmp_path, mini_repo)
        try:
            service.register_project("claimant", str(other), agent=AGENT)
            with pytest.raises(GraphConfinementError, match="claimant"):
                service.repoint_project(PROJECT, str(other), agent=AGENT)
        finally:
            service.close()

    def test_repoint_same_root_is_unchanged(self, service: CodeGraphService) -> None:
        current = service._resolve_root(PROJECT).root
        result = service.repoint_project(PROJECT, str(current), agent=AGENT)
        assert result["status"] == "unchanged"
        assert all(r["action"] != "repoint" for r in service._audit.recent(PROJECT))

    def test_repoint_audit_row_and_sidecar_path_hygiene(
        self, tmp_path: Path, mini_repo: Path
    ) -> None:
        """The repoint audit row exists (action=repoint, reason
        graph-repoint) and carries BASENAMES only — the sidecar trail
        leaves the process, so no absolute prefix may ride it (review
        10173a2a-4 discipline)."""
        service, _ = make_service(tmp_path, mini_repo)
        try:
            service.index_project(PROJECT, agent=AGENT)
            moved = mini_repo.with_name("repo-moved")
            mini_repo.rename(moved)
            service.repoint_project(PROJECT, str(moved), agent=AGENT)
            rows = [r for r in service._audit.recent(PROJECT) if r["action"] == "repoint"]
            assert rows, "expected the repoint audit row"
            assert rows[0]["reason"] == "graph-repoint"
            assert rows[0]["details"]["new_root"] == "repo-moved"
            assert rows[0]["details"]["old_root"] == mini_repo.name
            assert str(tmp_path) not in json.dumps(rows)
        finally:
            service.close()

    def test_list_marks_root_missing(self, tmp_path: Path, mini_repo: Path) -> None:
        service, _ = make_service(tmp_path, mini_repo)
        try:
            healthy = service.list_graph_projects(agent=AGENT)["projects"]
            row = next(p for p in healthy if p["project"] == PROJECT)
            assert row["root_missing"] is False
            moved = mini_repo.with_name("repo-moved")
            mini_repo.rename(moved)
            ghosted = service.list_graph_projects(agent=AGENT)["projects"]
            row = next(p for p in ghosted if p["project"] == PROJECT)
            assert row["root_missing"] is True  # the ghost is visible
        finally:
            service.close()


class TestGhostDelete:
    """A ghost registration (root gone on disk) was UNDELETABLE: the
    delete went through ``_resolve_root``, which refuses exactly the
    missing-root state (confinement-refused). The delete now resolves
    BY NAME/ID (the #450 repoint precedent): a live registration loses
    only its derived index; a ghost is removed ENTIRELY — index AND
    registration row — behind the evidence gate (``confirm=true`` plus
    the ``confirm_name`` echo of the project name)."""

    def test_delete_ghost_removes_registration(self, tmp_path: Path, mini_repo: Path) -> None:
        service, main = make_service(tmp_path, mini_repo)
        try:
            service.index_project(PROJECT, agent=AGENT)
            assert service.store.count_nodes(PROJECT) > 0
            mini_repo.rename(mini_repo.with_name("repo-moved"))  # the ghost

            result = service.delete_graph_project(
                PROJECT, agent=AGENT, confirm=True, confirm_name=PROJECT
            )
            assert result["status"] == "deleted"
            assert result["ghost"] is True
            assert result["deregistered"] is True
            assert result["deleted_nodes"] > 0
            # the sidecar subtree is gone...
            assert service.store.count_nodes(PROJECT) == 0
            # ...and so is the registration row (the ghost left the list)
            assert main.get_project_by_name(PROJECT) is None
            names = [p["project"] for p in service.list_graph_projects(agent=AGENT)["projects"]]
            assert PROJECT not in names
            rows = [r for r in service._audit.recent(PROJECT) if r["action"] == "delete"]
            assert rows and rows[0]["details"]["ghost"] is True
        finally:
            service.close()

    def test_delete_ghost_refused_without_confirmation(
        self, tmp_path: Path, mini_repo: Path
    ) -> None:
        service, main = make_service(tmp_path, mini_repo)
        try:
            mini_repo.rename(mini_repo.with_name("repo-moved"))  # the ghost
            with pytest.raises(GraphConfinementError, match="evidence gate"):
                service.delete_graph_project(PROJECT, agent=AGENT)
            # the refusal removed nothing: the ghost stays visible
            assert main.get_project_by_name(PROJECT) is not None
            row = next(
                p
                for p in service.list_graph_projects(agent=AGENT)["projects"]
                if p["project"] == PROJECT
            )
            assert row["root_missing"] is True
            refused = [r for r in service._audit.recent(PROJECT) if r["action"] == "delete-refused"]
            assert refused, "expected the delete-refused audit row"
        finally:
            service.close()

    def test_delete_ghost_refused_on_name_mismatch(self, tmp_path: Path, mini_repo: Path) -> None:
        """``confirm=true`` alone is not the gate — the name echo is
        the second factor."""
        service, main = make_service(tmp_path, mini_repo)
        try:
            mini_repo.rename(mini_repo.with_name("repo-moved"))
            with pytest.raises(GraphConfinementError, match="confirm_name"):
                service.delete_graph_project(
                    PROJECT, agent=AGENT, confirm=True, confirm_name="other"
                )
            assert main.get_project_by_name(PROJECT) is not None
        finally:
            service.close()

    def test_delete_live_root_keeps_registration(self, service: CodeGraphService) -> None:
        """Back-compat: a live-root delete is the v1 index purge — no
        gate, the registration row stays."""
        service.index_project(PROJECT, agent=AGENT)
        result = service.delete_graph_project(PROJECT, agent=AGENT)
        assert result["status"] == "deleted"
        assert result["ghost"] is False
        assert result["deregistered"] is False
        assert service.main.get_project_by_name(PROJECT) is not None


# ── #454: agent-side registration ────────────────────────────────────────────


class TestManualRegister:
    def test_register_new_project_then_index_succeeds(
        self, tmp_path: Path, mini_repo: Path
    ) -> None:
        service, main = make_service(tmp_path, mini_repo, register=False)
        try:
            result = service.register_project("fresh", str(mini_repo), agent=AGENT)
            assert result["status"] == "registered"
            assert result["project"] == "fresh"
            assert main.get_project_by_name("fresh") is not None
            indexed = service.index_project("fresh", agent=AGENT)
            assert indexed["status"] == "ok"
            assert service.store.count_nodes("fresh") > 0
            rows = [r for r in service._audit.recent("fresh") if r["action"] == "manual-register"]
            assert rows and rows[0]["actor"] == AGENT
        finally:
            service.close()

    def test_register_idempotent_same_root(self, tmp_path: Path, mini_repo: Path) -> None:
        service, main = make_service(tmp_path, mini_repo, register=False)
        try:
            first = service.register_project("solo", str(mini_repo), agent=AGENT)
            assert first["status"] == "registered"
            second = service.register_project("solo", str(mini_repo) + "/", agent=AGENT)
            assert second["status"] == "already-registered"  # normpath-equal root
            assert len([p for p in main.list_projects() if p.name == "solo"]) == 1
            reused = [
                r for r in service._audit.recent("solo") if r["action"] == "manual-register-reused"
            ]
            assert reused  # the auto-register-reused precedent
        finally:
            service.close()

    def test_register_attaches_root_to_pathless_project(
        self, tmp_path: Path, mini_repo: Path
    ) -> None:
        """The common orphan: a project row auto-created by memory
        writes carries no paths — registration attaches the root."""
        service, main = make_service(tmp_path, mini_repo, register=False)
        main.projects.append(FakeProject(id="p-orphan", name="orphan", paths=[]))
        try:
            result = service.register_project("orphan", str(mini_repo), agent=AGENT)
            assert result["status"] == "registered"
            assert main.get_project_by_name("orphan").paths == [str(mini_repo)]
        finally:
            service.close()

    def test_register_name_collision_different_root_refused(
        self, tmp_path: Path, mini_repo: Path
    ) -> None:
        other = tmp_path / "other-repo"
        other.mkdir()
        (other / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        service, _ = make_service(tmp_path, mini_repo)  # PROJECT → mini_repo
        try:
            with pytest.raises(GraphConfinementError, match="already registered at"):
                service.register_project(PROJECT, str(other), agent=AGENT)
        finally:
            service.close()

    def test_register_root_of_other_project_reuses_it(
        self, tmp_path: Path, mini_repo: Path
    ) -> None:
        """One root = one graph: a second NAME over a registered root
        rides the existing project instead of duplicating the row."""
        service, main = make_service(tmp_path, mini_repo)
        try:
            result = service.register_project("alias", str(mini_repo), agent=AGENT)
            assert result["status"] == "already-registered"
            assert result["project"] == PROJECT
            assert main.get_project_by_name("alias") is None  # no duplicate row
        finally:
            service.close()

    @pytest.mark.parametrize("make_root", ["missing", "bare", "home"])
    def test_register_refuses_bad_roots(
        self, tmp_path: Path, mini_repo: Path, make_root: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if make_root == "missing":
            root = str(tmp_path / "nope")
        elif make_root == "bare":
            bare = tmp_path / "bare"
            bare.mkdir()
            root = str(bare)
        else:
            root = str(tmp_path)  # == Path.home() under the patch below
            monkeypatch.setattr(
                "vesma.codegraph.service.Path.home", classmethod(lambda cls: tmp_path)
            )
        service, _ = make_service(tmp_path, mini_repo, register=False)
        try:
            with pytest.raises(GraphConfinementError):
                service.register_project("bad", root, agent=AGENT)
        finally:
            service.close()

    def test_register_ignores_auto_cap(self, tmp_path: Path, mini_repo: Path) -> None:
        """Explicit registration does NOT count against
        ``auto_register_max_projects`` — the cap bounds the AUTO path
        (provenance: the description marker it counts, which manual
        rows never carry). Config floor is 1, so the pin is: TWO manual
        registrations under a cap of 1 both succeed."""
        service, _ = make_service(tmp_path, mini_repo, register=False, auto_register_max_projects=1)
        other = tmp_path / "other-repo"
        other.mkdir()
        (other / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        try:
            first = service.register_project("manual-one", str(mini_repo), agent=AGENT)
            second = service.register_project("manual-two", str(other), agent=AGENT)
            assert first["status"] == second["status"] == "registered"
        finally:
            service.close()

    def test_unregistered_refusal_names_the_register_tool(
        self, tmp_path: Path, mini_repo: Path
    ) -> None:
        service, _ = make_service(tmp_path, mini_repo, register=False)
        try:
            with pytest.raises(GraphConfinementError, match="vesma_register_project"):
                service.index_project("never-registered", agent=AGENT)
        finally:
            service.close()


# ── #464: registration hardening (gate / audited refusals / ghost-only / realpath) ──


class TestRegistrationHardening464:
    """Issue #464: registration IS a read-scope grant — the agent call
    path is config-gated (P2-1), every manual register/repoint refusal
    is audited (P3-2), repoint recovers ghosts only (P3-3) and the
    forbidden-root check runs on the realpath (P3-4)."""

    def _refusals(self, service: CodeGraphService, project: str, action: str) -> list[dict]:
        return [r for r in service._audit.recent(project) if r["action"] == action]

    def test_gate_off_refuses_agent_registration_and_audits(
        self, tmp_path: Path, mini_repo: Path
    ) -> None:
        """P2-1: ``agent_registration=false`` refuses the agent call
        path, registers nothing, and the refusal is audited (P3-2)."""
        service, main = make_service(tmp_path, mini_repo, register=False, agent_registration=False)
        try:
            with pytest.raises(GraphConfinementError, match="agent_registration"):
                service.register_project("fresh", str(mini_repo), agent=AGENT)
            assert main.get_project_by_name("fresh") is None  # no silent grant
            rows = self._refusals(service, "fresh", "manual-register-refused")
            assert len(rows) == 1
            assert "agent-initiated registration is disabled" in rows[0]["reason"]
            assert rows[0]["actor"] == AGENT
            assert rows[0]["details"]["source"] == "agent"
        finally:
            service.close()

    def test_gate_off_leaves_operator_path_open(self, tmp_path: Path, mini_repo: Path) -> None:
        """P2-1: the operator/CLI call path (``source="operator"``) is
        never gated — the same OFF config still registers."""
        service, main = make_service(tmp_path, mini_repo, register=False, agent_registration=False)
        try:
            result = service.register_project(
                "fresh", str(mini_repo), agent=AGENT, source="operator"
            )
            assert result["status"] == "registered"
            assert main.get_project_by_name("fresh") is not None
        finally:
            service.close()

    def test_gate_defaults_on(self) -> None:
        """P2-1: the flag follows the code_graph default-on convention —
        the plain agent path behaves exactly as before #464."""
        assert CodeGraphConfig().agent_registration is True

    def test_name_collision_refusal_audited(self, tmp_path: Path, mini_repo: Path) -> None:
        """P3-2: the name-collision refusal (existing registration at a
        different root) lands exactly one audit row, reason sanitized to
        basenames."""
        other = tmp_path / "other-repo"
        other.mkdir()
        (other / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        service, _ = make_service(tmp_path, mini_repo)  # PROJECT → mini_repo
        try:
            with pytest.raises(GraphConfinementError, match="already registered at"):
                service.register_project(PROJECT, str(other), agent=AGENT)
            rows = self._refusals(service, PROJECT, "manual-register-refused")
            assert len(rows) == 1
            assert "already registered at" in rows[0]["reason"]
            assert str(tmp_path) not in json.dumps(rows)  # basename hygiene
        finally:
            service.close()

    def test_bad_root_refusal_audited(self, tmp_path: Path, mini_repo: Path) -> None:
        """P3-2: a register attempt at a nonexistent root is refused and
        audited exactly once with the failed gate named."""
        service, _ = make_service(tmp_path, mini_repo, register=False)
        try:
            with pytest.raises(GraphConfinementError, match="does not exist"):
                service.register_project("bad", str(tmp_path / "nope"), agent=AGENT)
            rows = self._refusals(service, "bad", "manual-register-refused")
            assert len(rows) == 1
            assert "does not exist on disk" in rows[0]["reason"]
        finally:
            service.close()

    def test_repoint_live_root_refused_and_audited(self, tmp_path: Path, mini_repo: Path) -> None:
        """P3-3: repoint is GHOST recovery — a live old root is refused
        (move-root is not repoint), the registration is untouched and
        the refusal audited."""
        other = tmp_path / "moved-here"
        other.mkdir()
        (other / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        service, main = make_service(tmp_path, mini_repo)
        try:
            with pytest.raises(GraphConfinementError, match="ghost recovery only"):
                service.repoint_project(PROJECT, str(other), agent=AGENT)
            assert main.get_project_by_name(PROJECT).paths[0] == str(mini_repo)
            rows = self._refusals(service, PROJECT, "repoint-refused")
            assert len(rows) == 1
            assert "ghost recovery only" in rows[0]["reason"]
            assert str(tmp_path) not in json.dumps(rows)  # basename hygiene
        finally:
            service.close()

    def test_repoint_ghost_root_still_recovers(self, tmp_path: Path, mini_repo: Path) -> None:
        """P3-3 positive: the ghost path is intact — root gone on disk,
        repoint succeeds and no refusal row exists."""
        moved = mini_repo.with_name("repo-moved")
        service, main = make_service(tmp_path, mini_repo)
        try:
            mini_repo.rename(moved)  # the ghost
            result = service.repoint_project(PROJECT, str(moved), agent=AGENT)
            assert result["status"] == "repointed"
            assert main.get_project_by_name(PROJECT).paths[0] == str(moved)
            assert not self._refusals(service, PROJECT, "repoint-refused")
        finally:
            service.close()

    def test_repoint_relative_root_refused_and_audited(
        self, tmp_path: Path, mini_repo: Path
    ) -> None:
        """P3-3: a relative root never reaches the ghost check — the
        absolute-path gate refuses, audited per P3-2."""
        service, _ = make_service(tmp_path, mini_repo)
        try:
            with pytest.raises(GraphConfinementError, match="must be an absolute path"):
                service.repoint_project(PROJECT, "relative/path", agent=AGENT)
            rows = self._refusals(service, PROJECT, "repoint-refused")
            assert len(rows) == 1
            assert "must be an absolute path" in rows[0]["reason"]
        finally:
            service.close()

    def test_forbidden_root_symlink_to_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P3-4: a symlink to ``$HOME`` resolves to the forbidden
        target — the string compare alone would let it register."""
        monkeypatch.setattr("vesma.codegraph.service.Path.home", classmethod(lambda cls: tmp_path))
        link = tmp_path / "home-link"
        link.symlink_to(tmp_path)
        assert _is_forbidden_root(str(link)) is True

    def test_forbidden_root_symlink_to_fs_root(self, tmp_path: Path) -> None:
        """P3-4: a symlink to the filesystem root is forbidden."""
        link = tmp_path / "root-link"
        link.symlink_to(Path(link.anchor))
        assert _is_forbidden_root(str(link)) is True

    def test_forbidden_root_dotdot_spelling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P3-4: a ``..``-laden spelling of ``$HOME`` never passes the
        forbidden-root gate."""
        monkeypatch.setattr("vesma.codegraph.service.Path.home", classmethod(lambda cls: tmp_path))
        assert _is_forbidden_root(f"{tmp_path}/sub/../../{tmp_path.name}") is True


# ── MCP layer ────────────────────────────────────────────────────────────────


class _FakeManager:
    """Manager duck-type for the MCP layer (weak-referenceable — the
    service registry weak-keys on the manager)."""

    def __init__(self, tmp_path: Path, repo: Path, *, enabled: bool) -> None:
        self.sqlite = FakeMainStore(FakeProject(id="p-1", name=PROJECT, paths=[str(repo)]))
        self.settings = SimpleNamespace(
            vesma=SimpleNamespace(data_dir=str(tmp_path / "data")),
            code_graph=CodeGraphConfig(enabled=enabled),
        )


def _fake_manager(tmp_path: Path, repo: Path, *, enabled: bool) -> _FakeManager:
    return _FakeManager(tmp_path, repo, enabled=enabled)


class TestMcpLayer:
    def test_manifest_contains_the_graph_tools(self) -> None:
        from vesma.mcp_server import _canonical_tools

        names = {t.name for t in asyncio.run(_canonical_tools())}
        expected = {
            "vesma_index_project",
            "vesma_project_graph_status",
            "vesma_search_graph",
            "vesma_trace_path",
            "vesma_get_file_outline",
            "vesma_get_code_snippet",
            "vesma_check_graph_coverage",
            "vesma_get_graph_schema",
            "vesma_list_graph_projects",
            "vesma_delete_graph_project",
            "vesma_register_project",
        }
        assert expected <= names

    def test_missing_agent_is_a_boundary_error(self, tmp_path: Path, mini_repo: Path) -> None:
        from vesma.mcp_server import _handle_graph

        mgr = _fake_manager(tmp_path, mini_repo, enabled=True)
        result = _handle_graph("vesma_project_graph_status", mgr, {"project_id": PROJECT})
        assert result["code"] == "attribution-required"

    def test_disabled_flag_answered_as_disabled(self, tmp_path: Path, mini_repo: Path) -> None:
        from vesma.mcp_server import _handle_graph

        mgr = _fake_manager(tmp_path, mini_repo, enabled=False)
        result = _handle_graph(
            "vesma_project_graph_status", mgr, {"project_id": PROJECT, "agent": AGENT}
        )
        assert result["code"] == "disabled"

    def test_register_via_mcp_handler(self, tmp_path: Path, mini_repo: Path) -> None:
        """#454: the agent-facing register tool registers the project and
        a refusal carries the confinement code (never a traceback)."""
        from vesma.mcp_server import _handle_graph

        mgr = _fake_manager(tmp_path, mini_repo, enabled=True)
        mgr.sqlite.projects.clear()  # unregistered — the tool's whole point
        result = _handle_graph(
            "vesma_register_project",
            mgr,
            {"project_id": PROJECT, "root": str(mini_repo), "agent": AGENT},
        )
        assert result["status"] == "registered"
        indexable = _handle_graph(
            "vesma_index_project", mgr, {"project_id": PROJECT, "agent": AGENT}
        )
        assert indexable["status"] == "ok"
        refused = _handle_graph(
            "vesma_register_project",
            mgr,
            {"project_id": "bad", "root": str(tmp_path / "nope"), "agent": AGENT},
        )
        assert refused["code"] == "confinement-refused"

    def test_delete_ghost_dispatch_passes_the_gate(self, tmp_path: Path, mini_repo: Path) -> None:
        from vesma.mcp_server import _handle_graph

        mgr = _fake_manager(tmp_path, mini_repo, enabled=True)
        mini_repo.rename(mini_repo.with_name("repo-moved"))  # the ghost
        # without the evidence gate: confinement-refused, row stays
        refused = _handle_graph(
            "vesma_delete_graph_project", mgr, {"project_id": PROJECT, "agent": AGENT}
        )
        assert refused["code"] == "confinement-refused"
        assert mgr.sqlite.get_project_by_name(PROJECT) is not None
        # with confirm + the name echo: ghost removed entirely
        result = _handle_graph(
            "vesma_delete_graph_project",
            mgr,
            {"project_id": PROJECT, "agent": AGENT, "confirm": True, "confirm_name": PROJECT},
        )
        assert result["status"] == "deleted"
        assert result["ghost"] is True
        assert mgr.sqlite.get_project_by_name(PROJECT) is None

    def test_index_via_mcp_handler(self, tmp_path: Path, mini_repo: Path) -> None:
        from vesma.mcp_server import _handle_graph

        mgr = _fake_manager(tmp_path, mini_repo, enabled=True)
        result = _handle_graph(
            "vesma_index_project",
            mgr,
            {"project_id": PROJECT, "agent": AGENT, "session": "s"},
        )
        assert result["status"] == "ok"
        status = _handle_graph(
            "vesma_project_graph_status", mgr, {"project_id": PROJECT, "agent": AGENT}
        )
        assert status["nodes"] > 0


# ── REST twins ───────────────────────────────────────────────────────────────


def _rest_settings(tmp_path: Path, *, enabled: bool) -> Settings:
    settings = Settings(
        vesma={
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

    from vesma.api import main as api_main
    from vesma.api.main import app, lifespan
    from vesma.manager import MemoryManager
    from vesma.models import Project

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
        assert body["schema_version"] == 2
        assert "Function" in body["node_kinds"]
        assert "Command" in body["node_kinds"]
        assert "Route" in body["node_kinds"]

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


class TestRestRegisterRepointTwins:
    """REST twins for the #454 tail: ``POST /api/v1/graph/register``
    (twin of ``mnemos_register_project``) and ``POST /api/v1/graph/repoint``
    (twin of the ``vesma graph repoint`` CLI). Each twin gets the trio:
    happy path, a confinement refusal mapped by ``_graph_call`` (403,
    never a raw 500) and the attribution binding (400)."""

    @staticmethod
    def _make_repo(tmp_path: Path, name: str) -> Path:
        """A minimal registerable root: packaging marker + one code file."""
        repo = tmp_path / name
        repo.mkdir()
        (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        (repo / "mod.py").write_text("class Widget:\n    pass\n", encoding="utf-8")
        return repo

    def test_register_twin_happy_then_index(self, rest_client: TestClient, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path, "reg-repo")
        resp = rest_client.post(
            "/api/v1/graph/register",
            json={"project_id": "restreg", "root": str(repo), "agent": "tester"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"project": "restreg", "status": "registered", "root": str(repo)}

        # the registration is immediately usable by name — the whole point
        # of the twin (the «graph tools answer not registered» hole)
        idx = rest_client.post("/graph/index", json={"project_id": "restreg", "agent": "tester"})
        assert idx.status_code == 200
        assert idx.json()["status"] == "ok"

    def test_register_twin_reuses_registered_root(
        self, rest_client: TestClient, mini_repo: Path
    ) -> None:
        """One root = one graph: a second name over the fixture's root
        rides the existing registration instead of duplicating it."""
        resp = rest_client.post(
            "/api/v1/graph/register",
            json={"project_id": "alias", "root": str(mini_repo), "agent": "tester"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "already-registered"
        assert body["project"] == "restproj"

    def test_register_twin_name_collision_is_403(
        self, rest_client: TestClient, mini_repo: Path, tmp_path: Path
    ) -> None:
        """The existing registration wins: the same NAME at a DIFFERENT
        root is a loud confinement refusal (repoint is the operator's
        tool for moved roots), not a silent overwrite."""
        other = self._make_repo(tmp_path, "collision-root")
        resp = rest_client.post(
            "/api/v1/graph/register",
            json={"project_id": "restproj", "root": str(other), "agent": "tester"},
        )
        assert resp.status_code == 403
        assert "already registered at" in resp.json()["detail"]

    def test_register_twin_missing_attribution_is_400(
        self, rest_client: TestClient, mini_repo: Path
    ) -> None:
        """PG7: an empty agent is an attribution-binding breach → 400
        (the ``_graph_call`` family mapping), never a 500."""
        resp = rest_client.post(
            "/api/v1/graph/register",
            json={"project_id": "restreg", "root": str(mini_repo), "agent": ""},
        )
        assert resp.status_code == 400

    def test_repoint_twin_ghost_happy_then_index(
        self, rest_client: TestClient, tmp_path: Path
    ) -> None:
        repo = self._make_repo(tmp_path, "movable")
        reg = rest_client.post(
            "/api/v1/graph/register",
            json={"project_id": "movable", "root": str(repo), "agent": "tester"},
        )
        assert reg.status_code == 200
        idx0 = rest_client.post("/graph/index", json={"project_id": "movable", "agent": "tester"})
        assert idx0.status_code == 200

        moved = repo.with_name("movable-moved")
        repo.rename(moved)  # the ghost: old root gone on disk
        rep = rest_client.post(
            "/api/v1/graph/repoint",
            json={"project_id": "movable", "new_root": str(moved), "agent": "tester"},
        )
        assert rep.status_code == 200
        body = rep.json()
        assert body["status"] == "repointed"
        assert body["root"] == str(moved)

        # the stale sidecar was purged; the next index rebuilds fresh
        rebuilt = rest_client.post(
            "/graph/index", json={"project_id": "movable", "agent": "tester"}
        )
        assert rebuilt.status_code == 200
        assert rebuilt.json()["status"] == "ok"

    def test_repoint_twin_live_root_is_403(
        self, rest_client: TestClient, mini_repo: Path, tmp_path: Path
    ) -> None:
        """Ghost recovery only: a LIVE registration is never re-pointed
        (move-root is not repoint) — loud 403 with the actionable text."""
        other = self._make_repo(tmp_path, "live-alt")
        resp = rest_client.post(
            "/api/v1/graph/repoint",
            json={"project_id": "restproj", "new_root": str(other), "agent": "tester"},
        )
        assert resp.status_code == 403
        assert "old root still exists" in resp.json()["detail"]

    def test_repoint_twin_missing_attribution_is_400(
        self, rest_client: TestClient, mini_repo: Path
    ) -> None:
        resp = rest_client.post(
            "/api/v1/graph/repoint",
            json={"project_id": "restproj", "new_root": str(mini_repo), "agent": ""},
        )
        assert resp.status_code == 400


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
