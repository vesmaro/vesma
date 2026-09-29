"""Native auto-indexing tests — ADR-0032 wave PG-0.5 (owner directive
2026-09-29: indexing happens BY ITSELF, no explicit call, no skill).

Pins the manager+dispatcher level contract of
``vesmaro.codegraph.autoindex.AutoIndexer``:

* first contact (a hint from the MCP dispatcher or a hook) over a
  marker-carrying cwd AUTO-REGISTERS the project (attribution lands in
  the description — the ``projects`` table has no ``registered_by``
  column) and background-indexes it: nodes exist, audit carries
  ``auto-register`` + ``auto-first``, the assemble beacon appears;
* a changed file + a post-throttle-window hint reindexes with reason
  ``auto-stale`` and freshness returns;
* a cwd WITHOUT a project marker never registers and never indexes;
* ``auto_index=False`` makes hints a no-op (nothing queued, nothing
  built);
* the per-project throttle turns two back-to-back hints into ONE task;
* ``hint()`` never joins the scheduler (recorder double for the
  scheduler — coalescing pinned on the same double);
* PG7 fail-closed limits hold on the AUTO path exactly as on the
  manual one: a limit breach aborts the whole index (audit with the
  cause, zero nodes, nothing partial).

Throttle windows are injected as 0 (or a wide 60s for the throttle
test) — no test sleeps on production numbers; scheduler joins go
through the deterministic ``wait_for_idle``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from vesmaro.codegraph.audit import GraphAudit
from vesmaro.codegraph.autoindex import AutoIndexer, project_marker
from vesmaro.codegraph.service import CodeGraphService
from vesmaro.config import CodeGraphConfig, Settings
from vesmaro.manager import MemoryManager

AGENT = "auto-agent"
SESSION = "sess-auto"
PROJECT = "markerrepo"


# ── fixtures ────────────────────────────────────────────────────────────────


def _write_repo(root: Path, *, marker: bool = True) -> None:
    """A minimal but real marker repo: pyproject.toml + one package."""
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "mod.py").write_text(
        "import os\n\n\ndef mod_fn():\n    return os.sep\n", encoding="utf-8"
    )
    (pkg / "extra.py").write_text("def extra_fn():\n    return 2\n", encoding="utf-8")
    if marker:
        (root / "pyproject.toml").write_text("[project]\nname = 'markerrepo'\n", encoding="utf-8")


def _settings(tmp: Path, **code_graph: Any) -> Settings:
    cg: dict[str, Any] = {"enabled": True, "auto_reindex_min_interval_sec": 0.0}
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
def repo(tmp_path: Path) -> Path:
    root = tmp_path / PROJECT
    _write_repo(root)
    return root


@pytest.fixture
def manager(tmp_path: Path) -> Iterator[MemoryManager]:
    mgr = _make_manager(tmp_path)
    yield mgr
    mgr.close()


def _hint(mgr: MemoryManager, repo: Path, **overrides: Any) -> bool:
    kwargs: dict[str, Any] = {"cwd": str(repo), "agent": AGENT, "session": SESSION}
    kwargs.update(overrides)
    return mgr.codegraph_activity_hint(PROJECT, **kwargs)


def _idle(mgr: MemoryManager) -> None:
    autoindex = mgr._graph_autoindex
    assert autoindex is not None
    assert autoindex.wait_for_idle(timeout=10.0), "auto-index drain never went idle"


def _service(mgr: MemoryManager) -> CodeGraphService:
    service = mgr.get_codegraph_service()
    assert service is not None
    return service


def _audit(service: CodeGraphService) -> list[dict[str, Any]]:
    return GraphAudit(service.store.db_path).recent(project=PROJECT, limit=100)


# ── (a) native e2e: first contact registers + indexes + beacons ─────────────


def test_first_hint_registers_and_indexes(manager: MemoryManager, repo: Path) -> None:
    assert manager.sqlite.get_project_by_name(PROJECT) is None  # pre-condition

    assert _hint(manager, repo) is True
    _idle(manager)

    # Auto-registration: projects row with attribution in the description
    # (the table has no registered_by column — that IS the v1 contract).
    project = manager.sqlite.get_project_by_name(PROJECT)
    assert project is not None
    assert project.paths == [str(repo)]
    assert "auto-registered by" in project.description and AGENT in project.description

    # Background first index happened with auto attribution.
    service = _service(manager)
    assert service.store.count_nodes(PROJECT) > 0
    rows = _audit(service)
    assert any(r["action"] == "auto-register" for r in rows)
    assert any(r["reason"] == "auto-first" and r["actor"] == AGENT for r in rows)

    # The beacon self-appeared in the assembly output (zero-touch discovery).
    assembled = manager.assemble_context(
        session=SESSION, project=PROJECT, budget=256, mode="sync", agent=AGENT
    )
    assert "project-graph:" in assembled["text"]


def test_first_contact_via_mcp_dispatcher(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatcher cut-in: ONE point at the top of ``_dispatch`` —
    a regular tool call (no graph args at all) auto-indexes the cwd's
    marker repo through the manager wiring."""
    mgr = _make_manager(tmp_path)
    try:
        monkeypatch.chdir(repo)  # the dispatcher hints with os.getcwd()
        from vesmaro import mcp_server

        with (
            patch("vesmaro.mcp_server.get_manager", return_value=mgr),
            patch("vesmaro.mcp_server._detect_project", return_value=PROJECT),
        ):
            result = asyncio.run(
                mcp_server._dispatch(
                    "mnemos_assemble_context",
                    {"session": SESSION, "project": PROJECT, "agent": AGENT, "budget": 64},
                )
            )
        assert isinstance(result, dict) and "error" not in result
        _idle(mgr)
        assert mgr.sqlite.get_project_by_name(PROJECT) is not None
        assert _service(mgr).store.count_nodes(PROJECT) > 0
    finally:
        mgr.close()


def test_hint_via_pre_llm_call_hook(tmp_path: Path, repo: Path) -> None:
    """The hook mirror: ``pre_llm_call`` carries project/agent/session —
    the same manager hint fires from the harness path (cwd = process
    cwd, so the test chdirs into the repo)."""
    import os as _os

    from vesmaro import hooks

    mgr = _make_manager(tmp_path)
    try:
        prev = _os.getcwd()
        _os.chdir(repo)
        try:
            out = hooks.pre_llm_call(mgr, session=SESSION, project=PROJECT, agent=AGENT, budget=64)
        finally:
            _os.chdir(prev)
        assert out["hook"] == "pre_llm_call"
        _idle(mgr)
        assert mgr.sqlite.get_project_by_name(PROJECT) is not None
        assert _service(mgr).store.count_nodes(PROJECT) > 0
    finally:
        mgr.close()


# ── (b) stale: change + post-window hint → auto-stale reindex ───────────────


def test_stale_file_reindexes_after_window(manager: MemoryManager, repo: Path) -> None:
    _hint(manager, repo)
    _idle(manager)
    service = _service(manager)
    assert service.store.count_nodes(PROJECT) > 0

    # ACTUAL change on disk; throttle window is 0 in the fixture, so the
    # next hint is already past the window.
    mod = repo / "pkg" / "mod.py"
    mod.write_text(
        mod.read_text(encoding="utf-8") + "\n\ndef fresh_fn():\n    return 42\n",
        encoding="utf-8",
    )
    assert _hint(manager, repo) is True
    _idle(manager)

    rows = _audit(service)
    assert any(r["reason"] == "auto-stale" for r in rows)
    status = service.status(PROJECT, agent=AGENT)
    assert status["staleness"]["changed_files"] == []
    assert any(
        n.name == "fresh_fn" for n in service.store.search_nodes(PROJECT, "fresh_fn", limit=5)
    )


# ── (v) no marker → no registration, no index ───────────────────────────────


def test_cwd_without_marker_is_ignored(tmp_path: Path) -> None:
    bare = tmp_path / "bare-dir"
    bare.mkdir()
    (bare / "loose.py").write_text("x = 1\n", encoding="utf-8")  # files but no marker
    mgr = _make_manager(tmp_path)
    try:
        assert project_marker(str(bare)) is None
        assert _hint(mgr, bare) is True  # queued…
        _idle(mgr)  # …and silently classified as not-a-project
        assert mgr.sqlite.get_project_by_name(PROJECT) is None
        assert _service(mgr).store.count_nodes(PROJECT) == 0
        assert all(r["action"] != "auto-register" for r in _audit(_service(mgr)))
    finally:
        mgr.close()


# ── (g) auto_index=False → hints are a no-op ────────────────────────────────


def test_auto_index_disabled_makes_hints_noop(tmp_path: Path, repo: Path) -> None:
    mgr = _make_manager(tmp_path, auto_index=False)
    try:
        assert _hint(mgr, repo) is False
        assert mgr._graph_autoindex is None  # nothing was even built
        assert mgr.sqlite.get_project_by_name(PROJECT) is None
    finally:
        mgr.close()


def test_flag_off_inside_indexer_drops_hint(repo: Path) -> None:
    """The indexer double-checks the flag itself: with ``auto_index``
    off the hint is refused before any queueing — no service touch."""
    submitted: list[Any] = []

    class RecordingScheduler:
        def submit(self, job: Any) -> None:
            submitted.append(job)

    cfg = CodeGraphConfig(enabled=True, auto_index=False)
    indexer = AutoIndexer(object(), RecordingScheduler(), config=cfg)  # type: ignore[arg-type]
    assert indexer.hint(PROJECT, cwd=str(repo), agent=AGENT) is False
    assert indexer.pending_hints() == 0
    assert submitted == []


# ── (d) throttle: two hints back-to-back → ONE task ────────────────────────


def test_two_rapid_hints_run_one_task(tmp_path: Path, repo: Path) -> None:
    mgr = _make_manager(tmp_path, auto_reindex_min_interval_sec=60.0)
    try:
        assert _hint(mgr, repo) is True
        assert _hint(mgr, repo) is True  # second hint inside the window
        _idle(mgr)
        rows = _audit(_service(mgr))
        assert sum(1 for r in rows if r["reason"] == "auto-first") == 1
        assert mgr._graph_autoindex is not None
        assert mgr._graph_autoindex.pending_hints() == 0
    finally:
        mgr.close()


# ── (e) hint() never joins the scheduler ───────────────────────────────────


def test_hint_returns_without_scheduler_join(repo: Path) -> None:
    """Recorder double for the scheduler: ``hint()`` must return having
    only QUEUED — a join attempt would need a method the recorder lacks
    (AttributeError → test failure). Also pins drain coalescing."""
    submitted: list[Any] = []

    class RecordingScheduler:
        def submit(self, job: Any) -> None:
            submitted.append(job)

    indexer = AutoIndexer(object(), RecordingScheduler(), config=CodeGraphConfig())  # type: ignore[arg-type]
    assert indexer.hint(PROJECT, cwd=str(repo), agent=AGENT) is True
    assert indexer.hint(PROJECT, cwd=str(repo), agent=AGENT) is True
    assert len(submitted) == 1  # one coalesced drain job, both hints inside
    assert indexer.pending_hints() == 2  # nothing consumed — no join happened


# ── (zh) PG7 fail-closed limits hold on the auto path ───────────────────────


def test_limit_breach_fails_whole_auto_index(tmp_path: Path, repo: Path) -> None:
    mgr = _make_manager(tmp_path, index_max_files=1)  # repo has 2 source files
    try:
        assert _hint(mgr, repo) is True
        _idle(mgr)
        service = _service(mgr)
        # Nothing partial: zero nodes, zero file records.
        assert service.store.count_nodes(PROJECT) == 0
        assert service.store.count_files(PROJECT) == 0
        # The audit carries the refusal with its cause.
        rows = _audit(service)
        refused = [
            r
            for r in rows
            if r["action"] == "index" and r["details"].get("outcome") == "limit-refused"
        ]
        assert refused, f"no limit-refused audit row: {rows}"
        assert "index_max_files" in str(refused[0]["reason"])
        assert refused[0]["details"].get("trigger") == "auto-first"
        # The registration itself survived (it precedes the index and is
        # not part of the graph) — the operator sees the project listed.
        assert mgr.sqlite.get_project_by_name(PROJECT) is not None
    finally:
        mgr.close()
