"""Tests for the grouped ``vesma_tags`` MCP tool pilot (issue #97).

Covers the ``action: enum [rename, remove, add]`` dispatch and the
non-breaking ``vesma_tags_rename`` alias:

  - ``MemoryManager.tags_remove`` — exact + wildcard removal, dry-run,
    idempotency, contract enforcement, FTS5 consistency.
  - ``MemoryManager.tags_add`` — append to filtered memories, dry-run,
    duplicate-suppression, contract-breaking tag is rejected per memory.
  - ``MemoryManager.tags_rename`` still passes through the shared
    ``_commit_tags`` path (regression vs ``test_tags_rename.py``).
  - MCP ``_dispatch``: ``vesma_tags`` with each action, the alias, and
    unknown-action / missing-arg error returns.

The grouped tool must stay non-breaking: every existing
``vesma_tags_rename`` call still works (alias routes to action='rename').
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import MemoryCreate, MemorySource, MemoryStatus

# ---------------------------------------------------------------------------
# Fixtures — isolated MemoryManager per test (mirrors test_tags_rename.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings():
    """Yield a Settings object backed by a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            vesma={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
        )
        settings.resolve_paths()
        yield settings


@pytest.fixture
def tmp_manager(tmp_settings):
    """Yield a MemoryManager with isolated storage and a mock embedder."""
    mgr = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


def _make_create(
    content: str,
    tags: list[str],
    *,
    status: MemoryStatus = MemoryStatus.PUBLISHED,
) -> MemoryCreate:
    """Build a MemoryCreate with explicit tags and status."""
    return MemoryCreate(
        content=content,
        tags=tags,
        source=MemorySource.MANUAL,
        status=status,
    )


def _add_memory(
    mgr: MemoryManager,
    *,
    content: str = "memory",
    tags: list[str] | None = None,
    project: str = "test-proj",
    agent: str = "test-agent",
) -> str:
    """Add a published memory and return its id.

    Keeps the denormalised ``project``/``agent`` columns consistent with the
    ``project:``/``agent:`` tags (the manager's ``_commit_tags`` re-derives
    the columns from the tags, so the test data must not diverge).
    """
    if tags is None:
        tags = [f"project:{project}", f"agent:{agent}", "mnemos:decision"]
    data = _make_create(content, tags)
    mem = mgr.add(data, project=project, agent=agent)
    return mem.id


def _add_gcw_memory(mgr: MemoryManager, *, subtype: str = "decision") -> str:
    """Add a published memory carrying a legacy gcw:<subtype> tag."""
    tags = ["project:test-proj", "agent:test-agent", f"gcw:{subtype}"]
    data = _make_create(f"gcw memory with {subtype}", tags)
    mem = mgr.add(data, project="test-proj", agent="test-agent")
    return mem.id


# ---------------------------------------------------------------------------
# action='remove' — MemoryManager.tags_remove
# ---------------------------------------------------------------------------


class TestTagsRemove:
    def test_remove_exact_tag(self, tmp_manager: MemoryManager) -> None:
        """An exact-matched tag is dropped and the change is written."""
        mid = _add_memory(
            tmp_manager,
            tags=["project:p", "agent:a", "mnemos:decision", "severity:high"],
        )
        report = tmp_manager.tags_remove(["severity:high"], dry_run=False)
        assert report["action"] == "remove"
        assert report["changed"] == 1
        assert report["dry_run"] is False
        mem = tmp_manager.sqlite.get(mid)
        assert "severity:high" not in mem.tags

    def test_remove_dry_run_writes_nothing(self, tmp_manager: MemoryManager) -> None:
        """dry_run=True (default) reports intent but does not persist."""
        _add_memory(
            tmp_manager,
            tags=["project:p", "agent:a", "mnemos:decision", "severity:high"],
        )
        report = tmp_manager.tags_remove(["severity:high"])  # dry_run defaults True
        assert report["changed"] == 1
        assert report["dry_run"] is True
        mem = tmp_manager.sqlite.list_all(limit=10)[0]
        assert "severity:high" in mem.tags  # unchanged

    def test_remove_wildcard_prefix(self, tmp_manager: MemoryManager) -> None:
        """wildcard=True strips every tag starting with the prefix."""
        _add_memory(
            tmp_manager,
            tags=[
                "project:p",
                "agent:a",
                "mnemos:decision",  # survives the gcw: strip → contract stays valid
                "gcw:decision",
                "gcw:learning",
            ],
        )
        report = tmp_manager.tags_remove(["gcw:"], wildcard=True, dry_run=False)
        assert report["changed"] == 1
        mem = tmp_manager.sqlite.list_all(limit=10)[0]
        assert not any(t.startswith("gcw:") for t in mem.tags)

    def test_remove_idempotent(self, tmp_manager: MemoryManager) -> None:
        """A second run with the same args reports changed=0."""
        _add_memory(
            tmp_manager,
            tags=["project:p", "agent:a", "mnemos:decision", "severity:high"],
        )
        tmp_manager.tags_remove(["severity:high"], dry_run=False)
        report = tmp_manager.tags_remove(["severity:high"], dry_run=False)
        assert report["changed"] == 0

    def test_remove_no_match(self, tmp_manager: MemoryManager) -> None:
        """A tag absent from every memory yields changed=0, no error."""
        _add_memory(tmp_manager, tags=["project:p", "agent:a", "mnemos:decision"])
        report = tmp_manager.tags_remove(["severity:high"], dry_run=False)
        assert report["changed"] == 0
        assert report["errors"] == []

    def test_remove_empty_tags_rejected(self, tmp_manager: MemoryManager) -> None:
        """Empty tags list is rejected up front (no magic)."""
        report = tmp_manager.tags_remove([], dry_run=False)
        assert report["changed"] == 0
        assert report["errors"] and "non-empty" in report["errors"][0]

    def test_remove_required_tag_is_rejected(self, tmp_manager: MemoryManager) -> None:
        """Removing the last project: tag is rejected — strict, no corruption.

        ``action='remove'`` validates the resulting tag set in strict mode:
        dropping the last ``project:`` (or ``agent:`` / ``mnemos:``) tag breaks
        the contract, so the memory is reported as a per-memory error, the
        write is skipped, and the stored tags are left untouched. This is the
        "explicit, no magic" contract: the store is never left contract-invalid.
        """
        mid = _add_memory(
            tmp_manager,
            tags=["project:p", "agent:a", "mnemos:decision"],
            project="p",
        )
        report = tmp_manager.tags_remove(["project:p"], dry_run=False)
        assert report["changed"] == 0
        assert report["errors"]  # non-empty per-memory error
        mem = tmp_manager.sqlite.get(mid)
        assert mem is not None
        # Stored tags are unchanged — the contract-breaking write was skipped.
        assert "project:p" in mem.tags
        assert mem.tags == ["project:p", "agent:a", "mnemos:decision"]
        assert mem.project == "p"

    def test_remove_scoped_to_project(self, tmp_manager: MemoryManager) -> None:
        """project filter scopes which memories are scanned."""
        _add_memory(
            tmp_manager,
            tags=["project:alpha", "agent:a", "mnemos:decision", "severity:high"],
            project="alpha",
        )
        _add_memory(
            tmp_manager,
            tags=["project:beta", "agent:a", "mnemos:decision", "severity:high"],
            project="beta",
        )
        report = tmp_manager.tags_remove(["severity:high"], dry_run=False, project="alpha")
        assert report["changed"] == 1  # only the alpha memory


# ---------------------------------------------------------------------------
# action='add' — MemoryManager.tags_add
# ---------------------------------------------------------------------------


class TestTagsAdd:
    def test_add_tag_to_filtered_memories(self, tmp_manager: MemoryManager) -> None:
        """Tags are appended to every memory matching the filter."""
        _add_memory(tmp_manager, content="m1", project="p1")
        _add_memory(tmp_manager, content="m2", project="p1")
        _add_memory(tmp_manager, content="m3", project="p2")
        report = tmp_manager.tags_add(["severity:high"], dry_run=False, project="p1")
        assert report["action"] == "add"
        assert report["changed"] == 2
        p1_mems = [m for m in tmp_manager.sqlite.list_all(limit=50) if m.project == "p1"]
        assert all("severity:high" in m.tags for m in p1_mems)

    def test_add_dry_run_writes_nothing(self, tmp_manager: MemoryManager) -> None:
        """dry_run=True reports intent but does not persist."""
        _add_memory(tmp_manager, project="p1")
        report = tmp_manager.tags_add(["severity:high"], project="p1")  # dry_run default
        assert report["changed"] == 1
        assert report["dry_run"] is True
        mem = next(m for m in tmp_manager.sqlite.list_all(limit=50) if m.project == "p1")
        assert "severity:high" not in mem.tags

    def test_add_idempotent(self, tmp_manager: MemoryManager) -> None:
        """Adding an already-present tag is a no-op (changed=0)."""
        _add_memory(tmp_manager, tags=["project:p", "agent:a", "severity:high"])
        report = tmp_manager.tags_add(["severity:high"], dry_run=False)
        assert report["changed"] == 0

    def test_add_empty_tags_rejected(self, tmp_manager: MemoryManager) -> None:
        """Empty tags list is rejected up front."""
        report = tmp_manager.tags_add([], dry_run=False)
        assert report["changed"] == 0
        assert report["errors"] and "non-empty" in report["errors"][0]

    def test_add_tag_without_prefix_rejected(self, tmp_manager: MemoryManager) -> None:
        """A tag without a ':' prefix is rejected before any write."""
        report = tmp_manager.tags_add(["nope"], dry_run=False)
        assert report["changed"] == 0
        assert report["errors"] and "prefix" in report["errors"][0]

    def test_add_contract_breaking_tag_errors_per_memory(self, tmp_manager: MemoryManager) -> None:
        """Adding a second project: tag breaks the contract → per-memory error."""
        _add_memory(tmp_manager, tags=["project:p", "agent:a", "mnemos:decision"])
        report = tmp_manager.tags_add(["project:other"], dry_run=False)
        # The full resulting set has two project: tags → contract fails →
        # counted as an error, not changed, store untouched.
        assert report["changed"] == 0
        assert report["errors"]
        mem = tmp_manager.sqlite.list_all(limit=10)[0]
        assert "project:other" not in mem.tags

    def test_add_invalid_mnemos_subtype_rejected(self, tmp_manager: MemoryManager) -> None:
        """Adding a tag with an invalid mnemos: subtype is rejected per memory.

        Strict validation rejects the resulting set because ``bogus_subtype``
        is not in ``VESMA_TAG_SUBTYPES``. The memory is reported as an error,
        nothing is written, and the store is left untouched.
        """
        _add_memory(tmp_manager, tags=["project:p", "agent:a", "mnemos:decision"])
        report = tmp_manager.tags_add(["mnemos:bogus_subtype"], dry_run=False)
        assert report["changed"] == 0
        assert report["errors"]
        mem = tmp_manager.sqlite.list_all(limit=10)[0]
        assert "mnemos:bogus_subtype" not in mem.tags
        assert mem.tags == ["project:p", "agent:a", "mnemos:decision"]

    def test_add_malformed_slug_rejected(self, tmp_manager: MemoryManager) -> None:
        """Adding a tag with a malformed slug is rejected per memory.

        The malformed tag also leaves the set without a valid ``project:``
        slug (uppercase + space fails the ``project:[a-z0-9_-]{1,64}`` pattern),
        so strict validation rejects the resulting set. ``changed=0``, the
        store is untouched.
        """
        # Memory lacks a project: tag, so adding a malformed project: slug
        # leaves the set contract-invalid — strict catches the slug format.
        _add_memory(
            tmp_manager,
            tags=["agent:a", "mnemos:decision"],
            project="p",
            agent="a",
        )
        report = tmp_manager.tags_add(["project:Bad Slug"], dry_run=False)
        assert report["changed"] == 0
        assert report["errors"]
        mem = tmp_manager.sqlite.list_all(limit=10)[0]
        assert "project:Bad Slug" not in mem.tags


# ---------------------------------------------------------------------------
# action='rename' via the shared path — regression
# ---------------------------------------------------------------------------


class TestRenameViaSharedCommit:
    def test_rename_still_works_through_commit_helper(self, tmp_manager: MemoryManager) -> None:
        """tags_rename routes through _commit_tags and remains correct."""
        _add_gcw_memory(tmp_manager)
        report = tmp_manager.tags_rename(from_prefix="gcw:", to_prefix="mnemos:", dry_run=False)
        assert report["renamed"] == 1
        mem = tmp_manager.sqlite.list_all(limit=10)[0]
        assert "mnemos:decision" in mem.tags
        assert not any(t.startswith("gcw:") for t in mem.tags)


# ---------------------------------------------------------------------------
# MCP dispatch — vesma_tags + vesma_tags_rename alias
# ---------------------------------------------------------------------------


class TestMcpDispatch:
    @staticmethod
    def _dispatch(name: str, args: dict) -> dict:
        from vesma.mcp_server import _dispatch

        return asyncio.run(_dispatch(name, args))

    def test_dispatch_tags_rename_action(self, tmp_manager, monkeypatch) -> None:
        """action='rename' behaves like the legacy rename tool."""
        from vesma import mcp_server

        _add_gcw_memory(tmp_manager)
        monkeypatch.setattr(mcp_server, "get_manager", lambda: tmp_manager)
        result = self._dispatch(
            "vesma_tags",
            {
                "action": "rename",
                "from_prefix": "gcw:",
                "to_prefix": "mnemos:",
                "dry_run": False,
            },
        )
        assert result["renamed"] == 1
        mem = tmp_manager.sqlite.list_all(limit=10)[0]
        assert "mnemos:decision" in mem.tags

    def test_dispatch_tags_remove_action(self, tmp_manager, monkeypatch) -> None:
        """action='remove' routes to tags_remove."""
        from vesma import mcp_server

        _add_memory(
            tmp_manager,
            tags=["project:p", "agent:a", "mnemos:decision", "severity:high"],
        )
        monkeypatch.setattr(mcp_server, "get_manager", lambda: tmp_manager)
        result = self._dispatch(
            "vesma_tags",
            {"action": "remove", "tags": ["severity:high"], "dry_run": False},
        )
        assert result["changed"] == 1
        mem = tmp_manager.sqlite.list_all(limit=10)[0]
        assert "severity:high" not in mem.tags

    def test_dispatch_tags_add_action(self, tmp_manager, monkeypatch) -> None:
        """action='add' routes to tags_add."""
        from vesma import mcp_server

        _add_memory(tmp_manager, project="p1")
        monkeypatch.setattr(mcp_server, "get_manager", lambda: tmp_manager)
        result = self._dispatch(
            "vesma_tags",
            {"action": "add", "tags": ["severity:low"], "project": "p1", "dry_run": False},
        )
        assert result["changed"] == 1
        mem = next(m for m in tmp_manager.sqlite.list_all(limit=50) if m.project == "p1")
        assert "severity:low" in mem.tags

    def test_dispatch_dry_run_default_true(self, tmp_manager, monkeypatch) -> None:
        """dry_run defaults to True across all actions."""
        from vesma import mcp_server

        _add_memory(tmp_manager, project="p1")
        monkeypatch.setattr(mcp_server, "get_manager", lambda: tmp_manager)
        result = self._dispatch(
            "vesma_tags",
            {"action": "add", "tags": ["severity:low"], "project": "p1"},
        )
        assert result["dry_run"] is True
        mem = next(m for m in tmp_manager.sqlite.list_all(limit=50) if m.project == "p1")
        assert "severity:low" not in mem.tags

    def test_dispatch_alias_tags_rename(self, tmp_manager, monkeypatch) -> None:
        """The legacy vesma_tags_rename tool still works (non-breaking alias)."""
        from vesma import mcp_server

        _add_gcw_memory(tmp_manager)
        monkeypatch.setattr(mcp_server, "get_manager", lambda: tmp_manager)
        result = self._dispatch(
            "vesma_tags_rename",
            {"from_prefix": "gcw:", "to_prefix": "mnemos:", "dry_run": False},
        )
        # Alias must return the rename-shaped report (existing contract).
        assert result["renamed"] == 1
        assert result["dry_run"] is False
        mem = tmp_manager.sqlite.list_all(limit=10)[0]
        assert "mnemos:decision" in mem.tags

    def test_dispatch_alias_supports_invalid_to_legacy(self, tmp_manager, monkeypatch) -> None:
        """The alias forwards invalid_subtypes_to_legacy (full parity)."""
        from vesma import mcp_server

        _add_memory(
            tmp_manager,
            tags=["project:p", "agent:a", "gcw:bogus"],
        )
        monkeypatch.setattr(mcp_server, "get_manager", lambda: tmp_manager)
        result = self._dispatch(
            "vesma_tags_rename",
            {
                "from_prefix": "gcw:",
                "to_prefix": "mnemos:",
                "invalid_subtypes_to_legacy": True,
                "dry_run": False,
            },
        )
        assert result["renamed"] == 1
        mem = tmp_manager.sqlite.list_all(limit=10)[0]
        assert "mnemos:legacy" in mem.tags

    def test_dispatch_rename_missing_args(self, tmp_manager, monkeypatch) -> None:
        """action='rename' without prefixes returns an error dict, not a crash."""
        from vesma import mcp_server

        monkeypatch.setattr(mcp_server, "get_manager", lambda: tmp_manager)
        result = self._dispatch("vesma_tags", {"action": "rename"})
        assert "error" in result
        assert "from_prefix" in result["error"]

    def test_dispatch_unknown_action(self, tmp_manager, monkeypatch) -> None:
        """An unknown action returns an error dict listing valid actions."""
        from vesma import mcp_server

        monkeypatch.setattr(mcp_server, "get_manager", lambda: tmp_manager)
        result = self._dispatch("vesma_tags", {"action": "frobnicate"})
        assert "error" in result
        assert "rename" in result["error"]
        assert "remove" in result["error"]
        assert "add" in result["error"]

    def test_dispatch_remove_wildcard(self, tmp_manager, monkeypatch) -> None:
        """wildcard=true is forwarded to tags_remove."""
        from vesma import mcp_server

        _add_memory(
            tmp_manager,
            tags=[
                "project:p",
                "agent:a",
                "mnemos:decision",  # survives the gcw: strip → contract stays valid
                "gcw:decision",
                "gcw:learning",
            ],
        )
        monkeypatch.setattr(mcp_server, "get_manager", lambda: tmp_manager)
        result = self._dispatch(
            "vesma_tags",
            {"action": "remove", "tags": ["gcw:"], "wildcard": True, "dry_run": False},
        )
        assert result["changed"] == 1
        mem = tmp_manager.sqlite.list_all(limit=10)[0]
        assert not any(t.startswith("gcw:") for t in mem.tags)
