"""E2E tests for the grouped ``mnemos_tags`` MCP tool (issue #97).

Unlike ``test_tags_grouped.py`` (which exercises ``MemoryManager`` methods
directly and drives ``_dispatch`` with a *mock* manager), these tests drive
the **real MCP server dispatch end-to-end** through ``call_tool`` →
``_dispatch`` with a **real isolated ``MemoryManager``** (tmp_path-backed
SQLite + vault). The only patch is ``get_manager`` swapping the module-level
singleton for the isolated one — the dispatch logic, the tool registry, and
the manager are all production code.

This is the harness the user explicitly requested: a real MCP-client →
server round-trip proving the new grouped tool is registered, callable, and
behaves correctly including the contract-safety fixes (blocker scenarios E5
and E6 — remove-last-project / add-invalid-subtype must NOT corrupt the
store).

Coverage matrix:

  E1 — action="rename" gcw: → mnemos:                  (tag rewritten)
  E2 — action="add" severity:low                        (tag appended)
  E3 — action="remove" severity:low                     (tag dropped)
  E4 — action="add" default dry_run                     (no writes)
  E5 — action="remove" of last project:                 (contract guard, E2E)
  E6 — action="add" mnemos:bogus_subtype                (contract guard, E2E)
  E7 — legacy alias mnemos_tags_rename                  (non-breaking)
  E8 — unknown action                                   (clear error, no mutation)
  E9 — rename missing from_prefix/to_prefix             (clear error)

Report-shape note (F1 FIXED — uniform report contract):
  ``tags_rename`` now returns ``{action, scanned, renamed, changed,
  skipped_invalid, errors, dry_run, from_prefix, to_prefix}`` and DOES
  include ``action="rename"``, matching ``tags_remove`` / ``tags_add``
  (which expose ``action="remove"`` / ``action="add"``). Every
  ``mnemos_tags`` action therefore exposes a uniform report shape keyed
  by ``action`` first; the rename/rename-alias tests assert on
  ``result["action"] == "rename"`` in addition to ``renamed``/``changed``.

Isolation: every test uses a fresh ``MemoryManager`` in a per-test
``TemporaryDirectory``. The real ``~/.mnemos/`` store is never touched.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vesmaro.config import Settings
from vesmaro.manager import MemoryManager
from vesmaro.mcp_server import _dispatch, call_tool, list_tools

# ---------------------------------------------------------------------------
# Fixtures — isolated MemoryManager per test
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings():
    """Yield a Settings object backed by a temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            mnemos={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
        )
        settings.resolve_paths()
        yield settings


@pytest.fixture
def real_manager(tmp_settings):
    """A REAL MemoryManager with isolated storage and a mock embedder.

    The embedder is mocked (returns a fixed vector) to keep the test fast and
    offline, but every other subsystem — SQLite store, vault, FTS5 triggers,
    TagContract validation, _commit_tags — is production code. This is what
    makes the round-trip a genuine e2e probe rather than a unit test.
    """
    mgr = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


# ---------------------------------------------------------------------------
# Dispatch helpers — drive the REAL MCP call_tool / _dispatch path
# ---------------------------------------------------------------------------


def _strip_checkpoint_reminder(text: str) -> str:
    """Strip a checkpoint reminder appended after the JSON payload.

    ``mcp_server._checkpoint_reminder()`` may append a nudge like
    ``\n\n⚠️ [vesma] N tool calls since last checkpoint … Consider
    calling mnemos_save_context …`` after the tool's JSON response. It is
    informational metadata for MCP clients, NOT part of the tool's return
    value — a correct client must ignore it before parsing.

    The module-global ``_checkpoint_tracker`` accumulates
    ``calls_since_save`` across the whole test session, so the reminder can
    fire mid-suite even when this module's own call count is low. Without
    stripping, ``json.loads`` raises ``JSONDecodeError`` (extra data) on
    ``"{…json…}\n\n⚠️ …"`` and the helper falls through to the raw string,
    breaking downstream ``result["action"]`` / ``result["renamed"]``
    assertions with ``TypeError: string indices must be integers``.

    Mirrors the same fix in ``test_workflow_e2e.py`` — one theme: e2e
    helpers must ignore the checkpoint reminder appended by ``mcp_server``.
    """
    marker = "\n\n⚠️ [vesma]"
    idx = text.find(marker)
    return text[:idx] if idx != -1 else text


async def _call_tool_real(real_manager: MemoryManager, name: str, args: dict):
    """Invoke the real MCP ``call_tool`` handler with an isolated real manager.

    Patches ``get_manager`` so the production singleton is swapped for the
    isolated manager under test; nothing else is mocked. Returns the parsed
    JSON payload of the single TextContent the MCP handler returns (or the
    raw text when it is not JSON, e.g. error strings).
    """
    with patch("vesmaro.mcp_server.get_manager", return_value=real_manager):
        contents = await call_tool(name, args)
    assert len(contents) == 1, f"expected exactly one TextContent, got {len(contents)}"
    text = _strip_checkpoint_reminder(contents[0].text)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


async def _dispatch_real(real_manager: MemoryManager, name: str, args: dict):
    """Invoke the real ``_dispatch`` directly with an isolated real manager.

    ``call_tool`` wraps ``_dispatch``; calling ``_dispatch`` directly lets a
    test inspect the raw dict return (before JSON serialisation) for
    negative paths that return an ``{"error": ...}`` dict.
    """
    with patch("vesmaro.mcp_server.get_manager", return_value=real_manager):
        return await _dispatch(name, args)


def _seed(
    mgr: MemoryManager,
    *,
    content: str = "seed memory",
    tags: list[str],
    project: str = "e2e-proj",
    agent: str = "e2e-agent",
) -> str:
    """Insert a published memory directly via the manager and return its id.

    Seeding goes through the manager (not raw SQL) so tags are validated and
    the denormalised project/agent columns stay consistent with the tags.
    """
    from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus

    data = MemoryCreate(
        content=content,
        tags=list(tags),
        source=MemorySource.MANUAL,
        status=MemoryStatus.PUBLISHED,
    )
    mem = mgr.add(data, project=project, agent=agent)
    return mem.id


def _tags_of(mgr: MemoryManager, memory_id: str) -> list[str]:
    """Read a memory's tags back from SQLite (post-condition verification)."""
    mem = mgr.sqlite.get(memory_id)
    assert mem is not None, f"memory {memory_id} vanished after operation"
    return list(mem.tags)


# ---------------------------------------------------------------------------
# E0 — registration: both tools are listed by the real MCP server
# ---------------------------------------------------------------------------


async def test_e0_both_tools_registered_in_mcp_registry() -> None:
    """list_tools() (the real MCP tool registration path) advertises both
    the grouped ``mnemos_tags`` tool and the legacy ``mnemos_tags_rename``
    alias, and ``mnemos_tags`` declares the ``action`` enum."""
    tools = await list_tools()
    names = {t.name for t in tools}
    assert "mnemos_tags" in names, "grouped mnemos_tags tool not registered"
    assert "mnemos_tags_rename" in names, "legacy mnemos_tags_rename alias not registered"

    grouped = next(t for t in tools if t.name == "mnemos_tags")
    action_schema = grouped.input_schema["properties"]["action"]
    assert action_schema["enum"] == ["rename", "remove", "add"]


# ---------------------------------------------------------------------------
# E1 — action="rename" gcw: → mnemos:
# ---------------------------------------------------------------------------


async def test_e1_rename_gcw_to_mnemos(real_manager: MemoryManager) -> None:
    mid = _seed(
        real_manager,
        content="gcw memory with decision",
        tags=["project:e2e-proj", "agent:e2e-agent", "gcw:decision"],
    )

    result = await _call_tool_real(
        real_manager,
        "mnemos_tags",
        {
            "action": "rename",
            "from_prefix": "gcw:",
            "to_prefix": "mnemos:",
            "dry_run": False,
        },
    )

    # rename report is now uniform: action="rename", renamed/changed, tag
    # rewritten (F1 fixed — see module docstring).
    assert result["action"] == "rename", result
    assert result["renamed"] >= 1, f"expected renamed>=1, got {result}"
    assert result["changed"] >= 1, f"expected changed>=1, got {result}"
    tags_after = _tags_of(real_manager, mid)
    assert "gcw:decision" not in tags_after, f"gcw:decision still present: {tags_after}"
    assert "mnemos:decision" in tags_after, f"mnemos:decision missing: {tags_after}"


# ---------------------------------------------------------------------------
# E2 — action="add" severity:low
# ---------------------------------------------------------------------------


async def test_e2_add_tag(real_manager: MemoryManager) -> None:
    mid = _seed(
        real_manager,
        tags=["project:e2e-proj", "agent:e2e-agent", "mnemos:decision"],
    )

    result = await _call_tool_real(
        real_manager,
        "mnemos_tags",
        {"action": "add", "tags": ["severity:low"], "project": "e2e-proj", "dry_run": False},
    )

    assert result["action"] == "add", result
    assert result["changed"] >= 1, f"expected changed>=1, got {result}"
    tags_after = _tags_of(real_manager, mid)
    assert "severity:low" in tags_after, f"severity:low missing: {tags_after}"


# ---------------------------------------------------------------------------
# E3 — action="remove" severity:low
# ---------------------------------------------------------------------------


async def test_e3_remove_tag(real_manager: MemoryManager) -> None:
    mid = _seed(
        real_manager,
        tags=["project:e2e-proj", "agent:e2e-agent", "mnemos:decision", "severity:low"],
    )

    result = await _call_tool_real(
        real_manager,
        "mnemos_tags",
        {"action": "remove", "tags": ["severity:low"], "project": "e2e-proj", "dry_run": False},
    )

    assert result["action"] == "remove", result
    assert result["changed"] >= 1, f"expected changed>=1, got {result}"
    tags_after = _tags_of(real_manager, mid)
    assert "severity:low" not in tags_after, f"severity:low still present: {tags_after}"


# ---------------------------------------------------------------------------
# E4 — action="add" with DEFAULT dry_run → nothing written
# ---------------------------------------------------------------------------


async def test_e4_add_default_dry_run_writes_nothing(real_manager: MemoryManager) -> None:
    mid = _seed(
        real_manager,
        tags=["project:e2e-proj", "agent:e2e-agent", "mnemos:decision"],
    )

    # NOTE: no dry_run key at all — exercising the default-true behaviour.
    result = await _call_tool_real(
        real_manager,
        "mnemos_tags",
        {"action": "add", "tags": ["severity:low"], "project": "e2e-proj"},
    )

    assert result["dry_run"] is True, f"expected dry_run True by default, got {result}"
    tags_after = _tags_of(real_manager, mid)
    assert "severity:low" not in tags_after, f"dry_run still wrote a tag: {tags_after}"


# ---------------------------------------------------------------------------
# E5 — remove last project: tag → contract guard, store NOT corrupted (blocker)
# ---------------------------------------------------------------------------


async def test_e5_remove_last_project_tag_is_blocked(real_manager: MemoryManager) -> None:
    mid = _seed(
        real_manager,
        tags=["project:e2e-proj", "agent:e2e-agent", "mnemos:decision", "severity:high"],
    )

    result = await _call_tool_real(
        real_manager,
        "mnemos_tags",
        {"action": "remove", "tags": ["project:e2e-proj"], "project": "e2e-proj", "dry_run": False},
    )

    # Contract guard: nothing changed, an error was reported.
    assert result["changed"] == 0, f"expected changed=0 (guard), got {result}"
    assert result["errors"], f"expected non-empty errors list, got {result}"
    # The memory must be UNCHANGED — last project: preserved.
    tags_after = _tags_of(real_manager, mid)
    assert "project:e2e-proj" in tags_after, (
        f"contract guard failed: project: tag dropped: {tags_after}"
    )


# ---------------------------------------------------------------------------
# E6 — add invalid mnemos: subtype → contract guard, invalid tag NOT persisted (blocker)
# ---------------------------------------------------------------------------


async def test_e6_add_invalid_subtype_is_blocked(real_manager: MemoryManager) -> None:
    mid = _seed(
        real_manager,
        tags=["project:e2e-proj", "agent:e2e-agent", "mnemos:decision"],
    )

    result = await _call_tool_real(
        real_manager,
        "mnemos_tags",
        {
            "action": "add",
            "tags": ["mnemos:bogus_subtype"],
            "project": "e2e-proj",
            "dry_run": False,
        },
    )

    assert result["changed"] == 0, f"expected changed=0 (guard), got {result}"
    assert result["errors"], f"expected non-empty errors list, got {result}"
    tags_after = _tags_of(real_manager, mid)
    assert "mnemos:bogus_subtype" not in tags_after, (
        f"contract guard failed: invalid subtype persisted: {tags_after}"
    )


# ---------------------------------------------------------------------------
# E7 — legacy alias mnemos_tags_rename behaves like action="rename"
# ---------------------------------------------------------------------------


async def test_e7_legacy_alias_rename(real_manager: MemoryManager) -> None:
    mid = _seed(
        real_manager,
        content="alias gcw memory",
        tags=["project:e2e-proj", "agent:e2e-agent", "gcw:rule"],
    )

    result = await _call_tool_real(
        real_manager,
        "mnemos_tags_rename",
        {"from_prefix": "gcw:", "to_prefix": "mnemos:", "dry_run": False},
    )

    # The alias must behave exactly like action="rename": action key,
    # renamed/changed, tag rewritten (F1 fixed — see module docstring).
    assert result["action"] == "rename", result
    assert result["renamed"] >= 1, f"alias did not rename: {result}"
    assert result["changed"] >= 1, f"expected changed>=1, got {result}"
    tags_after = _tags_of(real_manager, mid)
    assert "gcw:rule" not in tags_after, f"gcw:rule still present: {tags_after}"
    assert "mnemos:rule" in tags_after, f"mnemos:rule missing: {tags_after}"


async def test_e7b_alias_ignores_stray_action_key(real_manager: MemoryManager) -> None:
    """A stray ``action`` key on a legacy alias call must NOT leak to the
    dispatcher — the alias forces action='rename' after merging args."""
    mid = _seed(
        real_manager,
        tags=["project:e2e-proj", "agent:e2e-agent", "gcw:learning"],
    )

    result = await _call_tool_real(
        real_manager,
        "mnemos_tags_rename",
        {
            "from_prefix": "gcw:",
            "to_prefix": "mnemos:",
            "dry_run": False,
            "action": "remove",  # must be overridden by the alias
        },
    )

    # The alias forces action="rename" regardless of a stray action key,
    # so the result must look like a rename (action="rename", renamed>=1)
    # and the learning tag must be rewritten — NOT removed (F1 fixed — see
    # module docstring).
    assert result["action"] == "rename", result
    assert result["renamed"] >= 1, f"stray action=remove leaked through alias: {result}"
    tags_after = _tags_of(real_manager, mid)
    assert "mnemos:learning" in tags_after, f"rename did not apply: {tags_after}"
    assert "gcw:learning" not in tags_after, f"alias did not rewrite gcw:learning: {tags_after}"


# ---------------------------------------------------------------------------
# E8 — unknown action → clear error, no mutation
# ---------------------------------------------------------------------------


async def test_e8_unknown_action_errors_cleanly(real_manager: MemoryManager) -> None:
    mid = _seed(
        real_manager,
        tags=["project:e2e-proj", "agent:e2e-agent", "mnemos:decision"],
    )
    tags_before = _tags_of(real_manager, mid)

    result = await _dispatch_real(
        real_manager,
        "mnemos_tags",
        {"action": "bogus"},
    )

    assert isinstance(result, dict), f"expected dict error, got {result!r}"
    assert "error" in result, f"missing 'error' key: {result}"
    assert "bogus" in str(result["error"]), f"error does not name the bad action: {result}"
    # No mutation.
    assert _tags_of(real_manager, mid) == tags_before, "unknown action mutated the store"


# ---------------------------------------------------------------------------
# E9 — rename missing from_prefix/to_prefix → clear error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"action": "rename"},  # both missing
        {"action": "rename", "from_prefix": "gcw:"},  # to_prefix missing
        {"action": "rename", "to_prefix": "mnemos:"},  # from_prefix missing
    ],
)
async def test_e9_rename_missing_args_errors_cleanly(
    real_manager: MemoryManager, args: dict
) -> None:
    _seed(
        real_manager,
        tags=["project:e2e-proj", "agent:e2e-agent", "gcw:decision"],
    )

    result = await _dispatch_real(real_manager, "mnemos_tags", args)

    assert isinstance(result, dict), f"expected dict error, got {result!r}"
    assert "error" in result, f"missing 'error' key: {result}"
    assert "from_prefix" in str(result["error"]) or "to_prefix" in str(result["error"]), (
        f"error does not name the missing arg: {result}"
    )
