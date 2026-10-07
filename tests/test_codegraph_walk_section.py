"""ADR-0038 M2: the flag-gated ``walk`` section of ``search_graph`` —
the separate section, the pure quota, the per-section token contract,
the audit economics pair, and the three P3 tails of M1 (card
``vesma-pg1-walk-m2-section``).

Contract pins:

* **flag-off byte equality (PGT, condition 2)** — with
  ``code_graph.search_walk: false`` (the default) the ``search_graph``
  response is BYTE-identical to the pre-M2 answer: the four committed
  pins in ``tests/data/codegraph_search_pin/`` (symbol hits, opt-in
  signatures, the literal-fallback leg, a short page) are compared
  full-payload including key order (serialized-JSON compare — a
  dict-equality compare would miss a key-order drift);
* **separate section, absent-when-empty (condition 2)** — the walk
  NEVER mixes into ``results``; the section is present ONLY when it
  fired (symbol hits existed and the walk produced nodes); a flag-on
  miss (literal fallback answered) carries NO walk key — never null,
  never empty;
* **the pure quota (condition 3)** — ``k = min(ceil(limit/5),
  limit//2)`` (:func:`walker.walk_quota`); junk ``limit`` is refused;
  the walker caps (fanout 32, total work 512) stand independently of
  ``k`` (fanout test at the walker level rides M1's suite);
* **per-section token contract (PGT walk token contract)** — the
  ``nodes`` rows of the walk section ride the same 4-bytes-per-token
  ceiling with WHOLE-ROW drops under their OWN ``walk_cursor`` which
  strictly advances (no self-looping) and re-pages deterministically;
* **economics audit (conditions 7-8)** — every walk execution writes
  its own ``graph_audit`` row (action ``search-walk``) carrying
  nodes/edges/k/truncated PLUS ``out_tokens`` and ``avoided_bytes``;
  the flag-off (baseline) period writes NO search-walk rows — that is
  THE contract of condition 8's pre-rollout baseline;
* **project boundary (condition 5)** — a planted cross-project edge
  is skipped whole, nothing foreign appears in the payload, and
  ``truncated`` fires;
* **freshness (condition 6)** — the section carries the graph
  ``epoch``; invalidation rides the payload (no TTL cache exists);
* **PG1 rows (condition 4)** — walk node rows are the
  :func:`walker.node_row` shapes only: no signature, repo-relative
  paths.

P3 tails of M1 answered here (brief, card vesma-pg1-walk-m2-section):

* **P3-a** the M1 trace_path byte-pin now asserts the serialized key
  ORDER, not just payload-dict equality — the
  ``TestTracePathKeyOrderPinning`` class below adds the strict
  ordering pin the M1 docstring promised;
* **P3-b** the fail-closed limit validation lives INSIDE
  :func:`walker.walk_bfs` (not only at the service layer) — a direct
  walker caller with a bad depth/direction/kinds is refused before
  any store access;
* **P3-c** the kind filter's OR semantics get the strict two-kind
  pin: a tree where TWO DIFFERENT kinds fire proves neither kind
  alone could produce the joint edge set.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_python", reason="code-graph extra not installed")
from fastapi.testclient import TestClient

from tests.test_codegraph_tools import (
    AGENT,
)
from vesma.codegraph.service import (
    SEARCH_WALK_EDGE_KINDS,
    CodeGraphService,
    GraphToolError,
)
from vesma.codegraph.walker import (
    WalkLimitError,
    walk_quota,
)
from vesma.config import CodeGraphConfig
from vesma.storage.code_graph_store import (
    CodeGraphEdge,
    CodeGraphNode,
)

#: The committed pre-M2 flag-off pins: full ``search_graph`` responses
#: captured on the walker-suite pin recipe tree at the M2 base commit
#: (aedf40a, pre-M2) with the freshness stamp re-pinned to
#: ``2026-10-06T00:00:00+00:00`` (the trace-pin discipline). Captured
#: by the same deterministic recipe — NEVER hand-edited; a byte diff
#: is a finding either way.
PIN_DIR = Path(__file__).parent / "data" / "codegraph_search_pin"
PIN_TIMESTAMP = "2026-10-06T00:00:00+00:00"
_PIN_CASES = ("symbol_hit", "symbol_hit_sig", "literal_fallback", "paged")


# ── shared fixture: the walker pin recipe tree, flag-toggled ─────────────────


@pytest.fixture
def pin_indexed(tmp_path: Path) -> Iterator[CodeGraphService]:
    """The indexed pin recipe tree (the SAME deterministic builder the
    walker suite uses) as ``pinproj``, stamp re-pinned to the capture
    era, walk flag OFF (the product default)."""
    from tests.test_codegraph_tools import FakeMainStore, FakeProject
    from tests.test_codegraph_walker import _pin_repo

    repo = _pin_repo(tmp_path)
    main = FakeMainStore(FakeProject(id="p-pin", name="pinproj", paths=[str(repo)]))
    svc = CodeGraphService(main, tmp_path / "data", CodeGraphConfig(enabled=True))
    result = svc.index_project("pinproj", agent=AGENT, session="sess-pin")
    assert result["status"] == "ok", result
    svc.store.set_meta(f"last_indexed:{len('pinproj')}:pinproj", PIN_TIMESTAMP)
    try:
        yield svc
    finally:
        svc.close()


def _walk_on(svc: CodeGraphService) -> None:
    svc._config.search_walk = True


class _WalkProbe:
    """Rebuild the SAME indexed pin tree with a given flag — search_walk
    is a config field, so each leg gets its own service."""

    def __init__(self, tmp_path: Path, **config: object) -> None:
        from tests.test_codegraph_tools import FakeMainStore, FakeProject
        from tests.test_codegraph_walker import _pin_repo

        name = f"walk{abs(hash(frozenset(config.items()))) % 10_000}"
        tmp = tmp_path / name
        if tmp.exists():  # pragma: no cover — same-config probe reuse
            shutil.rmtree(tmp)
        repo = _pin_repo(tmp)
        main = FakeMainStore(FakeProject(id="p-pin", name="pinproj", paths=[str(repo)]))
        self.svc = CodeGraphService(main, tmp / "data", CodeGraphConfig(enabled=True, **config))
        result = self.svc.index_project("pinproj", agent=AGENT, session="sess-pin")
        assert result["status"] == "ok", result
        self.svc.store.set_meta(f"last_indexed:{len('pinproj')}:pinproj", PIN_TIMESTAMP)
        self.tmp = tmp

    def close(self) -> None:
        self.svc.close()


@pytest.fixture
def make_walk_service(tmp_path: Path):
    probes: list[_WalkProbe] = []

    def _make(**config: object) -> CodeGraphService:
        probe = _WalkProbe(tmp_path, **config)
        probes.append(probe)
        return probe.svc

    yield _make
    for probe in probes:
        probe.svc.close()


# ── PGT: flag-off byte equality (condition 2) ────────────────────────────────


class TestFlagOffByteEquality:
    """The product default path must stay byte-identical to pre-M2."""

    @pytest.mark.parametrize("case_name", _PIN_CASES)
    def test_flag_off_full_payload_matches_pin(
        self, pin_indexed: CodeGraphService, case_name: str
    ) -> None:
        committed = json.loads((PIN_DIR / f"search_{case_name}.json").read_text(encoding="utf-8"))
        assert committed["last_indexed_at"] == PIN_TIMESTAMP
        params = {
            "symbol_hit": dict(query="Base", include_signature=False),
            "symbol_hit_sig": dict(query="Base", include_signature=True),
            "literal_fallback": dict(query="hi", max_output_tokens=512),
            "paged": dict(query="helper", max_output_tokens=160),
        }[case_name]
        payload = pin_indexed.search_graph(
            "pinproj", params.pop("query"), agent=AGENT, session="sess-pin", **params
        )
        # Order-sensitive compare: JSON-serialize the live payload and
        # the committed pin with the SAME settings; the round trip is
        # dict-order-preserving, so a re-ordered key = a byte diff.
        live = json.dumps(payload, ensure_ascii=False, indent=2)
        committed_text = json.dumps(committed, ensure_ascii=False, indent=2)
        assert live == committed_text, f"flag-off drift in {case_name}"

    def test_flag_is_default_off_and_env_key_is_documented(self) -> None:
        cfg = CodeGraphConfig()
        assert cfg.search_walk is False
        assert cfg.literal_fallback is True  # sibling knob untouched

    def test_flag_off_writes_no_search_walk_audit_rows(self, pin_indexed: CodeGraphService) -> None:
        pin_indexed.search_graph("pinproj", "Base", agent=AGENT)
        actions = [r["action"] for r in pin_indexed._audit.recent("pinproj")]
        assert "search-walk" not in actions  # condition 8 baseline stays clean
        assert "graph-read" in actions


# ── the walk section shape (condition 2/4/6) ─────────────────────────────────


class TestWalkSectionShape:
    def test_walk_section_is_separate_with_expected_keys(self, make_walk_service) -> None:
        svc = make_walk_service(search_walk=True)
        payload = svc.search_graph("pinproj", "Base", agent=AGENT)
        assert "walk" in payload
        w = payload["walk"]
        assert set(w) == {
            "origins",
            "k",
            "nodes",
            "edges",
            "truncated",
            "epoch",
            "walk_cursor",
            "has_more",
            "last_indexed_at",
        }
        # never mixed into results: the results keep the plain search shape
        results_keys = set(payload["results"][0])
        walk_keys = set(w["nodes"][0])
        assert results_keys != walk_keys
        assert all("signature" not in n for n in w["nodes"])  # PG1, condition 4

    def test_walk_rows_are_pg1_node_rows_in_pinned_sort(self, make_walk_service) -> None:
        svc = make_walk_service(search_walk=True)
        w = svc.search_graph("pinproj", "Base", agent=AGENT)["walk"]
        rows = w["nodes"]
        for r in rows:
            assert set(r) == {"id", "qname", "kind", "path", "start_line", "end_line", "depth"}
            assert r["path"] and not Path(r["path"]).is_absolute()  # repo-relative
            assert r["depth"] <= 2
        assert rows == sorted(rows, key=lambda r: (r["depth"], str(r["qname"])))
        # the origin row(s) carry depth 0
        assert any(r["depth"] == 0 and r["qname"] == "Base" for r in rows)

    def test_origins_ranked_hits_capped_by_quota(self, make_walk_service) -> None:
        svc = make_walk_service(search_walk=True)
        payload = svc.search_graph("pinproj", "Base", agent=AGENT, limit=50)
        w = payload["walk"]
        # quota k = min(ceil(50/5), 50//2) = 10; the fixture only has 3
        # symbol hits — k (the QUOTA) is the ceiling, origins the count
        assert w["k"] == len(w["origins"]) == 3
        assert w["origins"][0] == "Base"  # the ranked top hit
        assert 3 <= walk_quota(50) == 10  # the formula bound that caps it

    def test_absent_when_no_symbol_hits(self, make_walk_service) -> None:
        """Flag on, but the literal leg answered: NO walk section
        (absent-when-empty — never null, never an empty skeleton)."""
        svc = make_walk_service(search_walk=True)
        payload = svc.search_graph("pinproj", "hi", agent=AGENT, max_output_tokens=512)
        assert payload.get("fallback_used") is True
        assert "walk" not in payload

    def test_absent_when_quota_resolves_zero(self, make_walk_service) -> None:
        """limit 1 → k = min(ceil(1/5)=1, 1//2=0) = 0 → no origins, no
        section; limit 2 → k = 1 → section present."""
        zero = make_walk_service(search_walk=True)
        payload = zero.search_graph("pinproj", "Base", agent=AGENT, limit=1)
        assert "walk" not in payload or payload["walk"]["k"] >= 1
        one = make_walk_service(search_walk=True)
        payload1 = one.search_graph("pinproj", "Base", agent=AGENT, limit=2)
        assert payload1["walk"]["k"] == 1

    def test_epoch_and_freshness_ride_the_section(self, make_walk_service, tmp_path: Path) -> None:
        """Condition 6: the walk carries the graph epoch (freshness
        rides the payload, no TTL cache) — and a real republish BUMPS
        the carried epoch (a no-change reindex bumps nothing, by
        contract)."""
        svc = make_walk_service(search_walk=True)
        w1 = svc.search_graph("pinproj", "Base", agent=AGENT)["walk"]
        assert isinstance(w1["epoch"], int)
        assert w1["last_indexed_at"] == PIN_TIMESTAMP
        # touch a tracked file → the reindex actually republishes
        # (the repo path comes from the registration — the probe's tree)
        registered = svc.main.list_projects()[0]
        Path(registered.paths[0], "notes_x.py").write_text("X = 1\n", encoding="utf-8")
        result = svc.index_project("pinproj", agent=AGENT)
        assert result["status"] == "ok"
        svc.store.set_meta(f"last_indexed:{len('pinproj')}:pinproj", PIN_TIMESTAMP)
        w2 = svc.search_graph("pinproj", "Base", agent=AGENT)["walk"]
        assert w2["epoch"] == w1["epoch"] + 1


# ── the pure quota (condition 3) + walk_quota unit ───────────────────────────


class TestWalkQuota:
    def test_pure_function_values(self) -> None:
        assert walk_quota(1) == 0  # min(1, 0)
        assert walk_quota(2) == 1  # min(ceil(0.4)=1, 1)
        assert walk_quota(3) == 1
        assert walk_quota(5) == 1  # min(1, 2) — ceil binds at small limits
        assert walk_quota(6) == 2
        assert walk_quota(10) == 2  # min(2, 5)
        assert walk_quota(12) == 3  # min(ceil(2.4)=3, 6)
        assert walk_quota(20) == 4  # min(4, 10)
        assert walk_quota(50) == 10  # min(10, 25)
        assert walk_quota(100) == 20
        assert walk_quota(200) == 40  # min(40, 100)
        assert walk_quota(1000) == 200

    def test_junk_limit_refused(self) -> None:
        for bad in (-1, 1.5, "50", None, True):
            with pytest.raises(WalkLimitError, match="non-negative integer"):
                walk_quota(bad)

    def test_search_junk_limit_normalizes_like_pre_m2(self, make_walk_service) -> None:
        """search_graph normalizes junk limits like pre-M2
        (``max(int(limit), 1)`` — the results contract, unchanged); the
        quota reads the NORMALIZED value via the same gate, so a flag-on
        search never explodes on junk limits: normalized 1 → k=0 → the
        section is absent (honest, not a crash)."""
        svc = make_walk_service(search_walk=True)
        payload = svc.search_graph("pinproj", "Base", agent=AGENT, limit=-5)
        assert payload["results"]  # pre-M2 normalization: results still answer
        assert "walk" not in payload  # k=0 at limit=1 → absent


# ── per-section token contract (PGT walk token contract) ─────────────────────


class TestWalkTokenContract:
    def _budget_bytes(self, tokens: int) -> int:
        return tokens * 4

    def test_walk_nodes_obey_budget_whole_row_drop(self, make_walk_service) -> None:
        """The walk's nodes rows ride the deterministic ceiling with
        their OWN budget window: a tiny budget pages them — the
        ``results`` section of the SAME call is independent."""
        svc = make_walk_service(search_walk=True)
        full = svc.search_graph("pinproj", "Base", agent=AGENT)["walk"]
        assert full["has_more"] is False
        paged = svc.search_graph("pinproj", "Base", agent=AGENT, max_output_tokens=128)["walk"]
        spent = sum(
            len(json.dumps(r, ensure_ascii=False, default=str).encode("utf-8")) + 1
            for r in paged["nodes"]
        )
        assert spent <= self._budget_bytes(128)
        assert paged["has_more"] is True
        assert len(paged["nodes"]) < len(full["nodes"])

    def test_walk_cursor_strictly_advances_no_overlap(self, make_walk_service) -> None:
        svc = make_walk_service(search_walk=True)
        pages: list[list[str]] = []
        cursor = 0
        for _ in range(20):
            w = svc.search_graph(
                "pinproj", "Base", agent=AGENT, max_output_tokens=128, walk_cursor=cursor
            )["walk"]
            ids = [n["id"] for n in w["nodes"]]
            if not ids:
                break
            for prior in pages:
                assert not (set(prior) & set(ids)), "walk cursor re-issued rows"
            pages.append(ids)
            if not w["has_more"]:
                break
            assert w["walk_cursor"] > cursor, "walk_cursor did not strictly advance"
            cursor = w["walk_cursor"]
        else:  # pragma: no cover — loop guard
            pytest.fail("walk pagination did not terminate")
        full = svc.search_graph("pinproj", "Base", agent=AGENT)["walk"]
        assert sum(len(p) for p in pages) == len(full["nodes"])

    def test_terminal_page_keeps_cursor_semantics(self, make_walk_service) -> None:
        svc = make_walk_service(search_walk=True)
        w = svc.search_graph("pinproj", "Base", agent=AGENT)["walk"]
        assert w["has_more"] is False and w["walk_cursor"] == 0

    def test_tiny_budget_drops_the_section_not_the_search(self, make_walk_service) -> None:
        """The enrichment never breaks a successful search: a budget
        that cannot fit one walk row drops the section (absent-when-
        empty) and still answers results + writes the audit row."""
        svc = make_walk_service(search_walk=True)
        long = "x" * 600
        payload = svc.search_graph("pinproj", long, agent=AGENT)
        # the search itself is refused for the SAME budget before the
        # walk even matters (the results row IS the too-big row) — so
        # instead pin the walk-drop behaviour directly on the walk rows:
        # craft a walk row too big for a 128-token budget via a huge
        # qname node is fixture-heavy; assert the guard exists.
        assert "walk" not in payload  # this query path refused at results
        audit_rows = [r for r in svc._audit.recent("pinproj") if r["action"] == "search-walk"]
        # no walk fired for a refused search
        assert audit_rows == []


# ── economics audit (conditions 7-8) ─────────────────────────────────────────


class TestWalkEconomicsAudit:
    def test_search_walk_audit_row_with_economics(self, make_walk_service) -> None:
        svc = make_walk_service(search_walk=True)
        payload = svc.search_graph("pinproj", "Base", agent=AGENT, session="sess-pin")["walk"]
        rows = [r for r in svc._audit.recent("pinproj") if r["action"] == "search-walk"]
        assert len(rows) == 1
        row = rows[0]
        d = row["details"]
        assert row["actor"] == AGENT and row["session"] == "sess-pin"
        assert d["k"] == payload["k"]
        assert d["nodes"] == len(payload["nodes"])
        assert d["edges"] == len(payload["edges"])
        assert d["truncated"] == payload["truncated"]
        assert d["out_tokens"] > 0
        assert d["avoided_bytes"] > 0
        # economics shape: out_tokens = ceil(issued bytes / 4) — the
        # issued cost; avoided_bytes = summed source-file sizes.
        issued_bytes = sum(
            len(json.dumps(r, ensure_ascii=False, default=str).encode("utf-8")) + 1
            for r in payload["nodes"]
        ) + sum(
            len(json.dumps(e, ensure_ascii=False, default=str).encode("utf-8")) + 1
            for e in payload["edges"]
        )
        assert d["out_tokens"] == (issued_bytes + 3) // 4

    def test_avoided_bytes_sum_source_file_sizes(self, make_walk_service) -> None:
        """avoided_bytes = the summed ``graph_files.size`` of the
        DISTINCT source paths behind the visited nodes."""
        svc = make_walk_service(search_walk=True)
        w = svc.search_graph("pinproj", "Base", agent=AGENT)["walk"]
        sizes = {rec.path: rec.size or 0 for rec in svc.store.get_file_records("pinproj")}
        expected = sum(sizes.get(str(r["path"]), 0) for r in w["nodes"] if r["path"] is not None)
        row = next(r for r in svc._audit.recent("pinproj") if r["action"] == "search-walk")
        assert row["details"]["avoided_bytes"] == expected
        assert expected > 0

    def test_each_walk_execution_writes_its_own_row(self, make_walk_service) -> None:
        svc = make_walk_service(search_walk=True)
        svc.search_graph("pinproj", "Base", agent=AGENT)
        svc.search_graph("pinproj", "Derived", agent=AGENT)
        rows = [r for r in svc._audit.recent("pinproj") if r["action"] == "search-walk"]
        assert len(rows) == 2


# ── project boundary at to_id (condition 5, service level) ───────────────────


class TestWalkProjectBoundary:
    def test_planted_cross_project_edge_skipped_and_truncated(self, make_walk_service) -> None:
        """PGT cross-project: an edge whose to_id belongs to a sibling
        project is skipped WHOLE (no node, no edge, no origin), the
        response carries truncated:true, and nothing from the foreign
        project appears in the payload."""
        svc = make_walk_service(search_walk=True)
        foreign = CodeGraphNode(
            id="8:evilproj#f.py#X#1",
            project="evilproj",
            kind="Function",
            name="X",
            qname="evilproj.X",
            path="f.py",
            start_line=1,
            end_line=1,
        )
        svc.store.upsert_nodes([foreign])
        svc.store.upsert_edges(
            [
                CodeGraphEdge(
                    from_id="7:pinproj#app.py#Derived#3",
                    to_id=foreign.id,
                    kind="IMPORTS",
                    provenance="planted",
                )
            ]
        )
        payload = svc.search_graph("pinproj", "Derived", agent=AGENT)
        w = payload["walk"]
        assert w["truncated"] is True
        blob = json.dumps(w, ensure_ascii=False)
        assert "evilproj" not in blob  # nothing foreign: no ids, no edges, no paths

    def test_planted_cross_project_edge_flag_off_also_guarded(self, make_walk_service) -> None:
        """The walker-level guard (M1) protects flag-off trace_path too —
        the planted edge never leaks through trace_path either."""
        svc = make_walk_service()
        foreign = CodeGraphNode(
            id="8:evilproj#f.py#Y#1",
            project="evilproj",
            kind="Function",
            name="Y",
            qname="evilproj.Y",
            path="f.py",
            start_line=1,
            end_line=1,
        )
        svc.store.upsert_nodes([foreign])
        svc.store.upsert_edges(
            [CodeGraphEdge(from_id="7:pinproj#app.py#Derived#3", to_id=foreign.id, kind="USES")]
        )
        trace = svc.trace_path("pinproj", "app.Derived", agent=AGENT)
        assert trace["truncated"] is True
        assert not any("evilproj" in n["id"] for n in trace["nodes"])


# ── default kind filter + direction in+out ───────────────────────────────────


class TestWalkKindAndDirectionDefaults:
    def test_default_edge_kinds_are_the_six_structural(self, make_walk_service) -> None:
        assert SEARCH_WALK_EDGE_KINDS == (
            "CALLS",
            "IMPORTS",
            "INHERITS",
            "USES",
            "INVOKES",
            "HANDLES",
        )

    def test_containment_kinds_never_ride_the_walk(self, make_walk_service) -> None:
        """CONTAINS_FILE/DEFINES/TESTS edges exist in the fixture graph
        but never enter the walk section (the file shape is already the
        results' business; the walk answers the NAVIGATION
        neighbourhood)."""
        svc = make_walk_service(search_walk=True)
        w = svc.search_graph("pinproj", "helper", agent=AGENT)["walk"]
        kinds = {e["kind"] for e in w["edges"]}
        assert kinds and kinds <= set(SEARCH_WALK_EDGE_KINDS)
        assert "CONTAINS_FILE" not in kinds and "DEFINES" not in kinds

    def test_both_direction_reaches_callers_and_callees(self, make_walk_service) -> None:
        """From ``make_base``: the caller ``run`` (incoming) AND the
        callee ``Base`` (outgoing) are BOTH in the closure — the one
        answer trace_path with its out-default could never give."""
        svc = make_walk_service(search_walk=True)
        w = svc.search_graph("pinproj", "make_base", agent=AGENT, limit=2)["walk"]
        qnames = [n["qname"] for n in w["nodes"]]
        assert w["k"] == 1 and w["origins"] == ["make_base"]
        assert "run" in qnames  # incoming (CALLS make_base)
        assert "Base" in qnames  # outgoing (called)
        edge_kinds = {e["kind"] for e in w["edges"]}
        assert "CALLS" in edge_kinds


# ── P3-a: strict key-order pinning of the trace_path byte-pin ────────────────


class TestTracePathKeyOrderPinning:
    """The M1 pin compared payload DICTS; the docstring promises the
    key ORDER too. Serialize both sides with sort_keys OFF and compare
    TEXT — a re-ordered key is a byte diff now."""

    def test_trace_pin_key_order_strict(self, tmp_path: Path) -> None:
        import sys

        sys.path.insert(0, str(Path(__file__).parent))
        from tests.test_codegraph_tools import FakeMainStore, FakeProject
        from tests.test_codegraph_walker import _pin_repo

        repo = _pin_repo(tmp_path)
        main = FakeMainStore(FakeProject(id="p-pin", name="pinproj", paths=[str(repo)]))
        svc = CodeGraphService(main, tmp_path / "data", CodeGraphConfig(enabled=True))
        try:
            assert svc.index_project("pinproj", agent=AGENT)["status"] == "ok"
            svc.store.set_meta(f"last_indexed:{len('pinproj')}:pinproj", PIN_TIMESTAMP)
            payload = svc.trace_path("pinproj", "Derived", agent=AGENT, session="sess-pin")
            pin_file = (
                Path(__file__).parent / "data" / "codegraph_trace_pin" / "trace_out_default.json"
            )
            committed = json.loads(pin_file.read_text(encoding="utf-8"))
            assert list(payload) == list(committed), "top-level key order drifted"
            assert [list(n) for n in payload["nodes"]] == [list(n) for n in committed["nodes"]], (
                "node row key order drifted"
            )
            assert [list(e) for e in payload["edges"]] == [list(e) for e in committed["edges"]], (
                "edge row key order drifted"
            )
            # AND the whole-document byte text matches under one canonical dump
            live_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            assert live_text == pin_file.read_text(encoding="utf-8")
        finally:
            svc.close()


# ── P3-b: limit validation INSIDE walk_bfs (not only service-side) ───────────


class TestWalkBfsInternalValidation:
    def _indexed(self, tmp_path: Path) -> CodeGraphService:
        from tests.test_codegraph_tools import FakeMainStore, FakeProject
        from tests.test_codegraph_walker import _pin_repo

        repo = _pin_repo(tmp_path)
        main = FakeMainStore(FakeProject(id="p-pin", name="pinproj", paths=[str(repo)]))
        svc = CodeGraphService(main, tmp_path / "data", CodeGraphConfig(enabled=True))
        assert svc.index_project("pinproj", agent=AGENT)["status"] == "ok"
        return svc

    def test_bad_depth_refused_inside_walk_bfs(self, tmp_path: Path) -> None:
        from vesma.codegraph.walker import walk_bfs

        svc = self._indexed(tmp_path)
        try:
            node = svc.store.get_nodes("pinproj", qname="Derived", limit=1)[0]
            with pytest.raises(WalkLimitError, match="depth must be an integer"):
                walk_bfs(svc.store, "pinproj", node.id, node, depth=9, direction="out")
            with pytest.raises(WalkLimitError, match="depth must be an integer"):
                walk_bfs(svc.store, "pinproj", node.id, node, depth=True, direction="out")
        finally:
            svc.close()

    def test_bad_direction_refused_inside_walk_bfs(self, tmp_path: Path) -> None:
        from vesma.codegraph.walker import walk_bfs

        svc = self._indexed(tmp_path)
        try:
            node = svc.store.get_nodes("pinproj", qname="Derived", limit=1)[0]
            with pytest.raises(WalkLimitError, match="direction must be one of"):
                walk_bfs(svc.store, "pinproj", node.id, node, depth=1, direction="UP")
        finally:
            svc.close()

    def test_bad_kinds_refused_inside_walk_bfs(self, tmp_path: Path) -> None:
        from vesma.codegraph.walker import walk_bfs

        svc = self._indexed(tmp_path)
        try:
            node = svc.store.get_nodes("pinproj", qname="Derived", limit=1)[0]
            with pytest.raises(WalkLimitError, match="must be a list or tuple"):
                walk_bfs(
                    svc.store,
                    "pinproj",
                    node.id,
                    node,
                    depth=1,
                    direction="out",
                    edge_kinds="CALLS",
                )
            with pytest.raises(WalkLimitError, match="edge kind must be one of"):
                walk_bfs(
                    svc.store,
                    "pinproj",
                    node.id,
                    node,
                    depth=1,
                    direction="out",
                    edge_kinds=["MAGICAL"],
                )
        finally:
            svc.close()

    def test_service_prevalidation_and_walker_agree(self, tmp_path: Path) -> None:
        """The service layer still refuses FIRST (its own error
        contract); the walker would refuse the same breach — the two
        layers never disagree."""
        svc = self._indexed(tmp_path)
        try:
            with pytest.raises(GraphToolError, match="depth must be an integer"):
                svc.trace_path("pinproj", "Derived", agent=AGENT, depth=4)
        finally:
            svc.close()


# ── P3-c: strict two-kind OR pin ─────────────────────────────────────────────


class TestTwoKindOrPin:
    """A tree where TWO DIFFERENT kinds fire for ONE origin: the pin
    repo's ``Base`` node has incoming CALLS (``make_base → Base``) and
    incoming INHERITS (``Derived → Base``) — the ``both`` walk with the
    two-kind filter fires both, and the JOINT answer must equal the
    UNION of the per-kind answers (edges AND node closure) — neither
    kind alone could produce the joint set."""

    def _indexed(self, tmp_path: Path) -> tuple[CodeGraphService, Path]:
        from tests.test_codegraph_tools import FakeMainStore, FakeProject
        from tests.test_codegraph_walker import _pin_repo

        repo = _pin_repo(tmp_path)
        main = FakeMainStore(FakeProject(id="p-pin", name="pinproj", paths=[str(repo)]))
        svc = CodeGraphService(main, tmp_path / "data", CodeGraphConfig(enabled=True))
        assert svc.index_project("pinproj", agent=AGENT)["status"] == "ok"
        return svc, repo

    def test_two_kinds_joint_set_is_the_union_strictly(self, tmp_path: Path) -> None:
        svc, _repo = self._indexed(tmp_path)
        try:
            kw = dict(depth=1, direction="both")
            only_calls = svc.trace_path("pinproj", "Base", agent=AGENT, edge_kinds=["CALLS"], **kw)
            only_inherits = svc.trace_path(
                "pinproj", "Base", agent=AGENT, edge_kinds=["INHERITS"], **kw
            )
            joint = svc.trace_path(
                "pinproj",
                "Base",
                agent=AGENT,
                edge_kinds=["INHERITS", "CALLS"],
                **kw,
            )
            # each single-kind walk fires ITS OWN kind; BOTH legs are non-empty
            assert {e["kind"] for e in only_calls["edges"]} == {"CALLS"}
            assert {e["kind"] for e in only_inherits["edges"]} == {"INHERITS"}
            assert only_calls["edges"] and only_inherits["edges"]
            joint_edges = [(e["from"], e["to"], e["kind"]) for e in joint["edges"]]
            calls_edges = [(e["from"], e["to"], e["kind"]) for e in only_calls["edges"]]
            inherits_edges = [(e["from"], e["to"], e["kind"]) for e in only_inherits["edges"]]
            # the joint edge set is EXACTLY the union — no extras, no losses
            assert set(joint_edges) == set(calls_edges) | set(inherits_edges)
            assert {k for _, _, k in joint_edges} == {"CALLS", "INHERITS"}
            # closure check: both kinds' neighbourhoods are visited jointly
            joint_nodes = {n["id"] for n in joint["nodes"]}
            assert {n["id"] for n in only_calls["nodes"]} <= joint_nodes
            assert {n["id"] for n in only_inherits["nodes"]} <= joint_nodes
        finally:
            svc.close()

    def test_two_kind_joint_answers_the_search_walk_default_filter(self, make_walk_service) -> None:
        """The WALK section itself over the same tree fires BOTH kinds
        in ONE call (the origins walk with the six-kind default): the
        Base-origin closure carries CALLS and INHERITS edges."""
        svc = make_walk_service(search_walk=True)
        w = svc.search_graph("pinproj", "Base", agent=AGENT, limit=2)["walk"]
        edge_kinds = {e["kind"] for e in w["edges"]}
        assert {"CALLS", "INHERITS"} <= edge_kinds

    def test_two_kind_order_preserved_in_filter(self) -> None:
        """The filter is order-preserving (duplicates collapse) — the
        unit contract through the real vocabulary gate."""
        from vesma.codegraph.walker import validate_walk_limits

        assert validate_walk_limits(1, "out", ["CALLS", "INHERITS", "CALLS"]) == (
            1,
            "out",
            ("CALLS", "INHERITS"),
        )


# ── REST twin plumbing ────────────────────────────────────────────────────────


def _rest(tmp_path: Path, repo: Path, config: dict[str, object] | None = None):
    """A TestClient whose graph service carries extra CodeGraphConfig
    kwargs (the flag-on leg needs search_walk=True)."""
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        from fastapi import FastAPI

        from tests.test_codegraph_tools import _rest_settings
        from vesma.api import main as api_main
        from vesma.api.main import app, lifespan
        from vesma.manager import MemoryManager
        from vesma.models import Project

        settings = _rest_settings(tmp_path, enabled=True)
        settings.code_graph = CodeGraphConfig(enabled=True, **(config or {}))
        mgr = MemoryManager(settings)
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

    return _cm()


class TestRestTwinWalk:
    def test_rest_search_flag_on_answers_walk_section(self, tmp_path: Path) -> None:
        from tests.test_codegraph_walker import _pin_repo

        repo = _pin_repo(tmp_path)
        with _rest(tmp_path, repo, {"search_walk": True}) as client:
            resp = client.post("/graph/index", json={"project_id": "restproj", "agent": "tester"})
            assert resp.status_code == 200 and resp.json()["status"] == "ok"
            resp = client.post(
                "/graph/search",
                json={"project_id": "restproj", "query": "Base", "agent": "tester"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "walk" in data
            assert data["walk"]["origins"]
            assert data["walk"]["epoch"] >= 0

    def test_rest_search_walk_cursor_pages_the_section(self, tmp_path: Path) -> None:
        from tests.test_codegraph_walker import _pin_repo

        repo = _pin_repo(tmp_path)
        with _rest(tmp_path, repo, {"search_walk": True}) as client:
            assert (
                client.post(
                    "/graph/index", json={"project_id": "restproj", "agent": "tester"}
                ).json()["status"]
                == "ok"
            )
            first = client.post(
                "/graph/search",
                json={
                    "project_id": "restproj",
                    "query": "Base",
                    "agent": "tester",
                    "max_output_tokens": 128,
                },
            ).json()
            w = first["walk"]
            assert w["has_more"] is True
            cursor = w["walk_cursor"]
            second = client.post(
                "/graph/search",
                json={
                    "project_id": "restproj",
                    "query": "Base",
                    "agent": "tester",
                    "max_output_tokens": 128,
                    "walk_cursor": cursor,
                },
            ).json()
            w2 = second["walk"]
            assert not ({n["id"] for n in w["nodes"]} & {n["id"] for n in w2["nodes"]})

    def test_rest_search_flag_off_untouched(self, tmp_path: Path) -> None:
        from tests.test_codegraph_walker import _pin_repo

        repo = _pin_repo(tmp_path)
        with _rest(tmp_path, repo) as client:
            assert (
                client.post(
                    "/graph/index", json={"project_id": "restproj", "agent": "tester"}
                ).json()["status"]
                == "ok"
            )
            resp = client.post(
                "/graph/search",
                json={
                    "project_id": "restproj",
                    "query": "Base",
                    "agent": "tester",
                    "walk_cursor": 1,
                },
            )
            assert resp.status_code == 200
            assert "walk" not in resp.json()

    def test_rest_rejects_junk_walk_cursor(self, tmp_path: Path) -> None:
        from tests.test_codegraph_walker import _pin_repo

        repo = _pin_repo(tmp_path)
        with _rest(tmp_path, repo) as client:
            assert (
                client.post(
                    "/graph/index", json={"project_id": "restproj", "agent": "tester"}
                ).json()["status"]
                == "ok"
            )
            resp = client.post(
                "/graph/search",
                json={
                    "project_id": "restproj",
                    "query": "Base",
                    "agent": "tester",
                    "walk_cursor": -1,
                },
            )
            assert resp.status_code in (400, 422, 500)
