"""Shared test setup and fixtures for the Vesma test suite.

MCP stub
--------
We inject minimal stubs into
``sys.modules`` here - before any test file imports ``vesma.mcp_server`` -
so that the dispatch / routing tests can run without the real SDK.

The stubs replicate the MCP SDK 2.x contract (#185): ``Server`` registers
handlers via constructor kwargs (``on_list_tools`` / ``on_call_tool``) and
the wire types are plain attribute holders.

If the real ``mcp`` package is installed (a core dependency since 4.1.0)
the guard ``if "mcp" not in sys.modules`` ensures the stubs are skipped and
the real implementation is used instead.

Rate-limiter reset
------------------
The ``reset_rate_limiter`` autouse fixture clears the in-process slowapi
storage before every test so one test's calls do not bleed into the next
test's quota (all TestClient requests share ``host="testclient"``).

Import pin (#288)
-----------------
``src/`` of THIS checkout is front-pinned on ``sys.path`` before any
``vesma`` import, with a fail-loud provenance assert on
``vesma.__file__``. A version-skew shadow-import (user-site editable
install / ``.venv`` / another checkout on ``PYTHONPATH``) once silently
pointed the suite at a stale build and produced 7 phantom sweeper
failures — the pin makes that impossible to miss instead.

Store-home isolation (wave 61 test-isolation slice)
----------------------------------------------------
The ``isolated_store_home`` autouse fixture flips ``HOME`` into ``tmp_path``
and strips path-bearing env vars (``VESMA_CONFIG``, store-path overrides,
XDG roots), so no test resolves the invoking user's live ``~/.vesma`` —
neither the config-search read nor call-time ``Path.home()`` writers such
as the sync/scanner audit logs. Guard: ``tests/test_store_isolation_guard.py``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import pin (#288) — the suite MUST import THIS checkout's vesma
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

# Unconditional front-pin: whichever interpreter/environment runs pytest,
# `import vesma` hits <checkout>/src first — ahead of any shadow install
# (user-site editable, .venv, another checkout on PYTHONPATH). Note: this
# pin does not itself cover the gitignored gRPC stubs in
# federation/gen/python/ — those are loaded by src/vesma/_mesh_gen.py
# via a path resolved from its own __file__, so pinning the package
# transitively pins the generated stubs to the same checkout as well.
# A hook-based __editable__ install (MetaPathFinder) intercepts imports
# before sys.path is consulted — the pin cannot win there; the provenance
# assert below is what converts that skew into a loud collection-time
# failure instead of phantom test results.
sys.path.insert(0, str(SRC_ROOT))

import vesma  # noqa: E402  — deliberately AFTER the sys.path pin

_resolved = Path(vesma.__file__).resolve()
_expected = (SRC_ROOT / "vesma" / "__init__.py").resolve()
assert _resolved == _expected, (
    "vesma imported from the wrong checkout: "
    f"{_resolved} — the test suite MUST run against {SRC_ROOT}. "
    "A shadow install (user-site editable / .venv / another checkout) "
    "shadow-imports a stale build and produces phantom failures (#288)."
)
del _resolved, _expected

# ---------------------------------------------------------------------------
# Minimal MCP stubs - only installed when mcp is not already present
# ---------------------------------------------------------------------------

if "mcp" not in sys.modules:

    class _Server:
        """Stub replicating the MCP SDK 2.x Server constructor contract.

        Handlers are registered via the ``on_list_tools`` / ``on_call_tool``
        constructor kwargs (the 1.x runtime decorators were removed in
        SDK 2.0 — see #185). The stub keeps the same attribute surface the
        ported ``vesma.mcp_server`` module relies on.
        """

        def __init__(
            self,
            name: str,
            *,
            version: str = "",
            on_list_tools=None,
            on_call_tool=None,
            **_kwargs,
        ) -> None:
            self.name = name
            self.version = version
            self.on_list_tools = on_list_tools
            self.on_call_tool = on_call_tool

        def create_initialization_options(self):
            return {}

    class _TextContent:
        """Stub for mcp.types.TextContent - supports attribute access on .text."""

        def __init__(self, *, type: str, text: str) -> None:
            self.type = type
            self.text = text

    class _Tool:
        """Stub for mcp.types.Tool - preserves name/description/input_schema.

        The SDK 2.x attribute is ``input_schema`` (the wire alias
        ``inputSchema`` is serialization-only). The stub mirrors that.
        """

        def __init__(
            self,
            *,
            name: str,
            description: str | None = None,
            input_schema: dict,  # canonical 2.x name (alias: inputSchema)
        ) -> None:
            self.name = name
            self.description = description
            self.input_schema = input_schema

    class _ListToolsResult:
        """Stub for mcp.types.ListToolsResult."""

        def __init__(self, *, tools: list) -> None:
            self.tools = tools

    class _CallToolResult:
        """Stub for mcp.types.CallToolResult."""

        def __init__(self, *, content: list, is_error: bool = False) -> None:
            self.content = content
            self.is_error = is_error

    class _CallToolRequestParams:
        """Stub for mcp.types.CallToolRequestParams."""

        def __init__(self, *, name: str, arguments: dict | None = None) -> None:
            self.name = name
            self.arguments = arguments

    class _PaginatedRequestParams:
        """Stub for mcp.types.PaginatedRequestParams."""

        def __init__(self, *, cursor: str | None = None) -> None:
            self.cursor = cursor

    _mcp_stub = MagicMock()

    _mcp_server_stub = MagicMock()
    _mcp_server_stub.Server = _Server

    _mcp_stdio_stub = MagicMock()

    _mcp_types_stub = MagicMock()
    _mcp_types_stub.TextContent = _TextContent
    _mcp_types_stub.Tool = _Tool
    _mcp_types_stub.ListToolsResult = _ListToolsResult
    _mcp_types_stub.CallToolResult = _CallToolResult
    _mcp_types_stub.CallToolRequestParams = _CallToolRequestParams
    _mcp_types_stub.PaginatedRequestParams = _PaginatedRequestParams

    sys.modules.update(
        {
            "mcp": _mcp_stub,
            "mcp.server": _mcp_server_stub,
            "mcp.server.stdio": _mcp_stdio_stub,
            "mcp.types": _mcp_types_stub,
        }
    )


@pytest.fixture(autouse=True)
def reset_rate_limiter() -> None:
    """Reset the in-process rate-limiter storage before every test.

    The slowapi ``Limiter`` is a module-level singleton keyed by client host.
    Starlette's ``TestClient`` always presents ``host="testclient"``, so
    all test requests share the same bucket.  Resetting between tests
    prevents one test's calls from bleeding into the next test's quota.
    """
    from vesma.api.rate_limit import limiter

    limiter._storage.reset()
    yield


@pytest.fixture(autouse=True)
def no_update_check_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the update-check kill switch for the whole suite (issue #445).

    The check is default-ON in product and rides ``MemoryManager.stats()`` —
    without this guard every stats()-calling test would attempt a PyPI fetch
    (fresh tmp data_dir = cache miss, 3s timeout offline). The suite is
    offline by contract; tests that exercise the check itself re-enable it
    locally (``monkeypatch.delenv``) or inject a fetcher.
    """
    monkeypatch.setenv("VESMA_UPDATES_CHECK", "off")
    yield


# ---------------------------------------------------------------------------
# Store-home isolation (wave 61 test-isolation slice)
# ---------------------------------------------------------------------------

#: Path-bearing environment variables the suite must never inherit from the
#: invoking shell. On an operator machine ``VESMA_CONFIG`` points at the LIVE
#: store config (``~/.vesma/config.yaml``), and the two canonical + two alias
#: variables (issue #139) override store paths directly. Deliberately a fixed
#: list, NOT a ``VESMA_*`` wildcard: the kill-switch set by
#: ``no_update_check_network`` must survive regardless of fixture ordering.
_PATH_ENV_VARS: tuple[str, ...] = (
    "VESMA_CONFIG",
    "VESMA_VESMA__DATA_DIR",
    "VESMA_VESMA__VAULT_PATH",
    "VESMA_DATA_DIR",
    "VESMA_VAULT__VAULT_PATH",
    # XDG roots: unset (not redirected) means the product defaults resolve
    # under HOME — which this fixture flips — so default-path-resolution
    # tests stay self-consistent. Same shape as test_service_doctor's
    # local ``isolated_home`` fixture.
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
)


@pytest.fixture(autouse=True)
def isolated_store_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Isolate EVERY test from the invoking user's real store home.

    Call-time ``Path.home()`` resolution is everywhere in the product
    (``vesma.audit.sync_audit_path``, ``vesma.cli.doctor``,
    ``vesma.cli.completion``, ``vesma.cli.update_cmd``,
    ``vesma.store_migration``, ``vesma.config.find_config_file`` — plus the
    ``~`` expansion of every config path), so a test that never touches a
    store surface can still READ the live config — and audit-writing
    surfaces WRITE the live store. Observed live (2026-10-10): a solo run
    of ``tests/test_b2b_semantics.py`` appended a ``sync-import`` line to
    the REAL ``~/.vesma/logs/sync-audit.jsonl`` via
    ``vesma.audit.log_sync_audit`` (wave-61 test-isolation card).

    This autouse fixture flips ``HOME`` to a per-test fake home next to
    ``tmp_path`` and strips the path-bearing env vars listed in
    ``_PATH_ENV_VARS``, so ``find_config_file()`` falls through to "no user
    config" and every default resolves under the fake home. Tests that
    isolate further (their own ``HOME``/``VESMA_CONFIG``/``Path.home``
    patching — the established idiom in test_cli, test_home_flip,
    test_zero_config, test_doctor_*, test_service_*) compose cleanly:
    monkeypatch restores LIFO. Tests that exercise import-time path
    constants (``vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR``,
    ``vesma.updates.FALLBACK_UPDATE_DIR``, ``vesma.cli.main`` ai-brain
    defaults) already redirect those explicitly; those constants predate
    any fixture and cannot be re-pointed from here.

    ``tests/test_store_isolation_guard.py`` fails loudly if this guarantee
    regresses.

    Placement: the fake home is a SIBLING of ``tmp_path``
    (``<tmp_path>-home``), not a child. Some suites assert on the FULL
    content of ``tmp_path`` (report-only contracts:
    ``test_doctor_mcp_check.py::test_check_is_report_only`` requires
    ``list(tmp_path.iterdir()) == []``), so the fixture must not add
    entries there; the pytest tmp-world parent dir owns the cleanup.
    """
    home = tmp_path.parent / f"{tmp_path.name}-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in _PATH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield home
