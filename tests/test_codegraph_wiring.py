"""Wiring tests for the project-graph slice 6 (ADR-0032 §3.5 + §3.2).

Pins the MANAGER-level wiring — the points slices 1-5 left open:

* the lazy ``CodeGraphService`` ownership on the manager
  (``get_codegraph_service`` — ``None`` while the master flag is off,
  one weak-keyed instance per manager once enabled);
* the recall beacon v1 (§3.5): ONE tail line in the
  ``assemble_context`` output — shown only when the master flag AND the
  beacon flag are on AND the project has an index; capped at 200 bytes;
  OUTSIDE the budget blocks (``blocks`` / ``tokens.estimated`` /
  ``stats.budget`` byte-identical with and without the line);
* the cheap-staleness contract on the beacon path: the classification
  is mtime+size against ``graph_files`` — NO sha256 run over the tree
  (the session-start latency ban);
* the watch poll (§3.2 trigger (b)): registration → status → manual
  stop; the global registration cap; an actual file change reindexes
  within the poll interval and lands a PG7 audit event with reason
  ``watch``; the legacy M8 vault-watcher form refuses with an
  actionable error instead of the old silent no-op.

Poll intervals are shrunk through the config fields (injection) — no
test sleeps on the production 5s/60s numbers.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("tree_sitter_python", reason="code-graph extra not installed")

from vesmaro.codegraph.audit import GraphAudit
from vesmaro.codegraph.service import (
    CodeGraphService,
    GraphConfinementError,
    GraphDisabledError,
    GraphToolError,
    close_graph_service,
)
from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.models import Project

AGENT = "wiring-agent"
SESSION = "sess-wiring"
PROJECT = "minirepo"


# ── fixtures ─────────────────────────────────────────────────────────────────


def _write_repo(root: Path) -> None:
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "alpha.py").write_text(
        "import os\n\n\ndef alpha_fn():\n    return os.sep\n", encoding="utf-8"
    )
    (pkg / "beta.py").write_text("def beta_fn():\n    return 2\n", encoding="utf-8")


def _settings(tmp: Path, **code_graph: Any) -> Settings:
    cg: dict[str, Any] = {"enabled": True, "watch": True}
    cg.update(code_graph)
    return Settings(
        mnemos={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        },
        scanner={"enabled": False},
        code_graph=cg,
    )


def _make_manager(tmp: Path, **code_graph: Any) -> MemoryManager:
    settings = _settings(tmp, **code_graph)
    settings.resolve_paths()
    return MemoryManager(settings)


@pytest.fixture
def manager(tmp_path: Path) -> Iterator[MemoryManager]:
    mgr = _make_manager(tmp_path)
    yield mgr
    mgr.close()


def _register_project(mgr: MemoryManager, root: Path, name: str = PROJECT) -> None:
    mgr.sqlite.save_project(Project(name=name, paths=[str(root)]))


def _index(mgr: MemoryManager, name: str = PROJECT) -> None:
    service = mgr.get_codegraph_service()
    assert service is not None
    service.index_project(name, agent=AGENT)


@pytest.fixture
def indexed_manager(manager: MemoryManager, tmp_path: Path) -> MemoryManager:
    """A manager over a registered, ALREADY-INDEXED mini repo."""
    repo = tmp_path / "repo"
    _write_repo(repo)
    _register_project(manager, repo)
    _index(manager)
    return manager


# ── get_codegraph_service (lazy ownership, flag gate) ────────────────────────


def test_service_is_none_while_disabled(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, enabled=False)
    try:
        assert mgr.get_codegraph_service() is None
    finally:
        mgr.close()


def test_service_is_built_once_when_enabled(manager: MemoryManager) -> None:
    service = manager.get_codegraph_service()
    assert service is not None
    assert manager.get_codegraph_service() is service  # one instance per manager


# ── the beacon (§3.5) ────────────────────────────────────────────────────────


def _assemble(mgr: MemoryManager, project: str = PROJECT) -> dict[str, Any]:
    result: dict[str, Any] = mgr.assemble_context(session=SESSION, project=project)
    return result


def test_beacon_shown_when_enabled_and_indexed(
    indexed_manager: MemoryManager,
) -> None:
    result = _assemble(indexed_manager)
    text = result["text"]
    assert "project-graph: minirepo indexed " in text
    assert "2/2 files fresh (0 stale, 0 poisoned)" in text
    assert text.rstrip().endswith("call vesma_search_graph")
    assert len(text.encode("utf-8")) <= 200


def test_beacon_absent_when_disabled(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, enabled=False, beacon=True)
    try:
        assert "project-graph" not in _assemble(mgr)["text"]
    finally:
        mgr.close()


def test_beacon_absent_when_beacon_flag_off(
    indexed_manager: MemoryManager,
) -> None:
    indexed_manager.settings.code_graph.beacon = False
    assert "project-graph" not in _assemble(indexed_manager)["text"]


def test_beacon_absent_without_index(manager: MemoryManager, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_repo(repo)
    _register_project(manager, repo)  # registered but never indexed
    assert "project-graph" not in _assemble(manager)["text"]


def test_beacon_absent_for_unregistered_project(
    indexed_manager: MemoryManager,
) -> None:
    assert "project-graph" not in _assemble(indexed_manager, project="no-such")["text"]


def test_beacon_rides_outside_budget_blocks(
    indexed_manager: MemoryManager,
) -> None:
    """The beacon adds the tail line and NOTHING else: blocks, the
    token estimate and the budget stage stats are identical with and
    without it (it never enters the token budget)."""
    on = _assemble(indexed_manager)
    indexed_manager.settings.code_graph.beacon = False
    off = _assemble(indexed_manager)
    assert on["blocks"] == off["blocks"]
    assert on["tokens"] == off["tokens"]
    assert on["stats"]["budget"] == off["stats"]["budget"]
    beacon_seg = on["text"].split("\n\n")[-1]
    base = (
        on["text"][: len(on["text"]) - len(beacon_seg) - 2]
        if len(on["text"]) > len(beacon_seg)
        else ""
    )
    assert base == off["text"]


def test_beacon_staleness_never_hashes(
    indexed_manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract §3.2 (c) / the latency ban: the beacon's freshness
    classification reads NO file bytes — no sha256 anywhere on the
    path. The hashing primitive itself is spiked."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("sha256 must not run on the beacon path")

    monkeypatch.setattr(hashlib, "sha256", _boom)
    line = indexed_manager.get_codegraph_service()
    assert line is not None
    assert line.beacon_line(PROJECT) is not None
    assert "project-graph" in _assemble(indexed_manager)["text"]


def test_beacon_line_byte_cap_under_absurd_slug(
    indexed_manager: MemoryManager,
) -> None:
    from vesmaro.codegraph.service import _fit_beacon_line

    long_slug = "п" * 500  # 1000 bytes of slug
    line = _fit_beacon_line(
        long_slug, " indexed t, 1/1 files fresh (0 stale, 0 poisoned) — call vesma_search_graph"
    )
    assert len(line.encode("utf-8")) <= 200
    assert line.startswith("project-graph: ")
    assert line.endswith("call vesma_search_graph")


# ── the watch poll (§3.2 trigger (b)) ────────────────────────────────────────


def test_watch_register_status_stop_cycle(
    indexed_manager: MemoryManager,
) -> None:
    started = indexed_manager.watch_start(PROJECT, agent=AGENT)
    assert started["status"] == "registered"
    assert started["project"] == PROJECT

    status = indexed_manager.watch_status()
    assert status["running"] is True
    assert len(status["registrations"]) == 1
    reg = status["registrations"][0]
    assert reg["project"] == PROJECT
    assert reg["agent"] == AGENT
    assert reg["last_run_at"] is None  # not polled yet

    stopped = indexed_manager.watch_stop()
    assert stopped == {"stopped": 1}
    after = indexed_manager.watch_status()
    assert after["running"] is False
    assert after["registrations"] == []


def test_watch_reregister_is_idempotent(
    indexed_manager: MemoryManager,
) -> None:
    indexed_manager.watch_start(PROJECT, agent=AGENT)
    again = indexed_manager.watch_start(PROJECT, agent=AGENT)
    assert again["status"] == "already-registered"
    assert len(indexed_manager.watch_status()["registrations"]) == 1
    indexed_manager.watch_stop()


def test_watch_registration_cap(indexed_manager: MemoryManager, tmp_path: Path) -> None:
    # Two MORE registered+indexed projects → three total, cap = 2.
    for name in ("mini-b", "mini-c"):
        repo = tmp_path / name
        _write_repo(repo)
        _register_project(indexed_manager, repo, name)
        _index(indexed_manager, name)
    indexed_manager.settings.code_graph.watch_max_registrations = 2
    indexed_manager.watch_start(PROJECT, agent=AGENT)
    indexed_manager.watch_start("mini-b", agent=AGENT)
    with pytest.raises(GraphToolError, match="cap"):
        indexed_manager.watch_start("mini-c", agent=AGENT)
    indexed_manager.watch_stop()


def test_watch_reindexes_on_actual_change(
    tmp_path: Path,
) -> None:
    mgr = _make_manager(
        tmp_path,
        watch_base_interval_sec=0.05,
        watch_interval_per_500_files=0.0,
        watch_max_interval_sec=0.5,
    )
    try:
        repo = tmp_path / "repo"
        _write_repo(repo)
        _register_project(mgr, repo)
        _index(mgr)
        mgr.watch_start(PROJECT, agent=AGENT)

        # ACTUAL change on disk — the poll must pick it up and reindex.
        alpha = repo / "pkg" / "alpha.py"
        alpha.write_text(
            alpha.read_text(encoding="utf-8") + "\n\ndef delta_fn():\n    return 3\n",
            encoding="utf-8",
        )

        deadline = time.monotonic() + 5.0
        reg: dict[str, Any] = {}
        while time.monotonic() < deadline:
            regs = mgr.watch_status()["registrations"]
            reg = regs[0] if regs else {}
            if reg.get("reindexes", 0) >= 1:
                break
            time.sleep(0.05)
        assert reg.get("reindexes", 0) >= 1, f"poll never reindexed: {reg}"

        # The new symbol is in the graph, and the audit row carries the cause.
        service = mgr.get_codegraph_service()
        assert service is not None
        rows = service.store.search_nodes(PROJECT, "delta_fn", limit=5)
        assert any(n.name == "delta_fn" for n in rows)
        audit_rows = GraphAudit(service.store.db_path).recent(project=PROJECT, limit=50)
        assert any(r["reason"] == "watch" for r in audit_rows)
    finally:
        mgr.close()


def test_watch_refusals(manager: MemoryManager, tmp_path: Path) -> None:
    # Master flag off.
    manager.settings.code_graph.enabled = False
    manager.settings.code_graph.watch = True
    with pytest.raises(GraphDisabledError):
        manager.watch_start(PROJECT, agent=AGENT)

    # Watch flag off (separate opt-in).
    manager.settings.code_graph.enabled = True
    manager.settings.code_graph.watch = False
    with pytest.raises(GraphToolError, match="watch=false"):
        manager.watch_start(PROJECT, agent=AGENT)

    # PG7: anonymous registration refused.
    manager.settings.code_graph.watch = True
    with pytest.raises(GraphToolError, match="attribution"):
        manager.watch_start(PROJECT)

    # Unregistered project.
    repo = tmp_path / "repo"
    _write_repo(repo)
    _register_project(manager, repo)
    with pytest.raises(GraphConfinementError):
        manager.watch_start("no-such-project", agent=AGENT)

    # Registered but never indexed — the poll never seeds a first index.
    with pytest.raises(GraphToolError, match="no index"):
        manager.watch_start(PROJECT, agent=AGENT)
    assert manager.watch_status()["registrations"] == []


def test_watch_legacy_form_refused(indexed_manager: MemoryManager) -> None:
    with pytest.raises(ValueError, match="project_id"):
        indexed_manager.watch_start(paths=["/tmp"])
    assert indexed_manager.watch_status()["registrations"] == []


def test_watch_status_shape_without_scheduler(manager: MemoryManager) -> None:
    status = manager.watch_status()
    assert status["running"] is False
    assert status["registrations"] == []
    assert status["watch_enabled"] is True  # enabled+watch in the fixture config
    assert status["cap"] >= 1


def test_manager_close_stops_poll(indexed_manager: MemoryManager) -> None:
    indexed_manager.watch_start(PROJECT, agent=AGENT)
    assert indexed_manager.watch_status()["running"] is True
    indexed_manager.close()
    assert indexed_manager.watch_status()["running"] is False


def test_manager_close_closes_graph_service(
    indexed_manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 10173a2a-2: the lazily built sidecar service (store +
    audit connections) must be closed BY manager.close() — not left to
    GC. Observable: service.close() is called exactly once with the
    registered instance and is idempotent."""
    service = indexed_manager.get_codegraph_service()
    assert service is not None
    closed: list[CodeGraphService] = []
    original = CodeGraphService.close

    def _spy(self: CodeGraphService) -> None:
        closed.append(self)
        original(self)

    monkeypatch.setattr(CodeGraphService, "close", _spy)
    indexed_manager.close()
    assert closed == [service]
    # Idempotent: the store/audit close paths tolerate a repeat.
    service.close()


def test_close_graph_service_never_builds(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)  # graph enabled, service never built
    try:
        assert close_graph_service(mgr) is False
    finally:
        mgr.close()
