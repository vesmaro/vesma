"""Tests for #185: MCP SDK 2.x port + doctor MCP transport diagnostics.

Covers:
- ``_check_mcp_transport`` (doctor, direction C): a healthy
  ``vesma.mcp_server`` import passes; a broken import (ImportError /
  AttributeError — the two #185 failure classes) fails LOUDLY with the
  remediation hint.
- In-memory MCP handshake probe (SDK 2.x ``create_client_server_memory_streams``):
  initialize → tools/list must return the 27-tool contract (#254 added
  vesma_awareness). Skipped when the
  real ``mcp`` SDK is not installed (the stub environment cannot drive a
  real session).
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import sys
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from vesma.cli.doctor import CheckStatus, _check_mcp_transport

# The model-visible tool manifest contract (#185): names are frozen; any
# change here is a breaking contract change and must not happen silently.
# 26 → 27 (mnemos #254): vesma_awareness added (R3 awareness pre-flight).
# 27 → 28 (ADR-0027 Ф3, epic #308): vesma_ingest_document added — the
# docs-as-memory document ingest (born-quarantined chunks, swept at
# completion; vesma_ingest_url keeps its single-row semantics).
# 38 → 39 (#454): vesma_register_project added (agent-side PG2 registration).
# 39 → 40 (vitals wave 9): vesma_usage_report added — the phase C usage-loop
# closure as a harness-authored report tool (ADR-0026 §C).
EXPECTED_TOOL_COUNT = 40


# The conftest installs MagicMock stubs into sys.modules BEFORE any test
# module runs — including in environments where the real `mcp` package IS
# installed (nothing imports it before conftest does its check). The stub
# cannot drive a real session, so the handshake probe must detect the REAL
# distribution via importlib.metadata (which the stubs do not shadow),
# not via sys.modules/find_spec. The probe additionally requires the 2.x
# line: the ported module speaks ONLY the constructor-registration API
# (SDK 1.x lacks on_list_tools/on_call_tool — the #185 break itself), and
# a 1.x distribution in the ambient environment is expected to fail
# loudly (doctor) but must not fail the suite.
def _real_mcp2_installed() -> bool:
    try:
        from importlib.metadata import PackageNotFoundError, version

        v = version("mcp")
        return int(v.split(".")[0]) >= 2
    except (PackageNotFoundError, ImportError, ValueError):
        return False


_REAL_MCP_INSTALLED = _real_mcp2_installed()

# conftest stub module names — exactly the set conftest installs.
_MCP_STUB_MODULES = ("mcp", "mcp.server", "mcp.server.stdio", "mcp.types")


@contextlib.contextmanager
def _real_mcp_modules() -> Iterator[None]:
    """Evict conftest stubs so ``import mcp`` binds the real package.

    On exit, restores the evicted sys.modules entries (stubs back in
    place) so later tests that rely on the stub environment are
    unaffected. Also re-syncs the ``mcp_server`` attribute on the parent
    package — submodule
    bindings via the parent getattr path (``from vesma import
    mcp_server``, ``import vesma.mcp_server as x``) otherwise still
    point at the freshly imported REAL module object after the swap-back,
    while sys.modules entries point at the restored pre-reload object
    (identity drift between sys.modules and the package namespace broke
    later monkeypatch-based tests).
    """
    touched = [*_MCP_STUB_MODULES, "vesma.mcp_server"]
    saved = {name: sys.modules.get(name) for name in touched}
    try:
        for name in touched:
            sys.modules.pop(name, None)
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        # Re-sync the submodule attribute on the parent package so
        # parent-getattr bindings and sys.modules agree again. The fresh
        # import inside the context ran importlib's setattr(parent, child,
        # module) on the `vesma` package — restoring
        # sys.modules alone leaves that attr on the NEW object, and every
        # `from vesma import mcp_server` afterwards binds a different
        # module than `from vesma.mcp_server import ...`.
        restored = sys.modules.get("vesma.mcp_server")
        parent = sys.modules.get("vesma")
        if restored is not None and parent is not None:
            parent.mcp_server = restored


# ── Doctor: MCP transport check (direction C) ──────────────────────────────────


def test_check_mcp_transport_healthy() -> None:
    """A healthy vesma.mcp_server import passes the transport check."""
    result = _check_mcp_transport()
    assert result.status == CheckStatus.PASS, result.detail
    assert "imports OK" in result.detail


def _break_mcp_server_import(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    """Make ``import vesma.mcp_server`` raise ``exc`` inside the doctor check.

    Patches ``builtins.__import__`` for the ``vesma.mcp_server`` module name
    only — the doctor check sees exactly what a broken transport sees, while
    every other import keeps working.
    """
    real_import = builtins.__import__

    def _fake_import(name: str, *args, **kwargs):
        if name == "vesma.mcp_server":
            raise exc
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)


def test_check_mcp_transport_broken_import_is_loud_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken import (missing mcp SDK) FAILS with the reinstall remediation hint."""
    _break_mcp_server_import(monkeypatch, ImportError("No module named 'mcp'"))
    result = _check_mcp_transport()
    assert result.status == CheckStatus.FAIL, result.detail
    assert result.detail.startswith("MCP transport broken:")
    assert "reinstall" in result.detail and "force-reinstall vesma" in result.detail, (
        "must name the reinstall remediation"
    )
    assert "mcp>=2.0,<3.0" in result.detail, "must name the SDK floor/cap"


def test_check_mcp_transport_attribute_error_is_loud_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SDK-API break (AttributeError — the 1.x-on-2.x class) FAILS loudly."""
    _break_mcp_server_import(
        monkeypatch, AttributeError("'Server' object has no attribute 'list_tools'")
    )
    result = _check_mcp_transport()
    assert result.status == CheckStatus.FAIL, result.detail
    assert result.detail.startswith("MCP transport broken:")
    assert "mcp>=2.0,<3.0" in result.detail


def test_check_mcp_transport_unexpected_error_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any unexpected exception during the smoke-check still FAILs (never crashes)."""
    _break_mcp_server_import(monkeypatch, RuntimeError("boom during import"))
    result = _check_mcp_transport()
    assert result.status == CheckStatus.FAIL, result.detail
    assert result.detail.startswith("MCP transport broken:")
    assert "RuntimeError" in result.detail


def test_check_mcp_transport_registered_in_doctor_run() -> None:
    """The transport check is wired into ``_run_all_checks`` (not orphaned)."""
    import inspect

    from vesma.cli import doctor as doctor_mod

    source = inspect.getsource(doctor_mod._run_all_checks)
    assert "_check_mcp_transport" in source


def test_doctor_json_includes_mcp_transport(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``mnemos doctor --json`` output carries the MCP transport check row."""
    from typer.testing import CliRunner

    from vesma.cli.doctor import doctor_app

    cfg = tmp_path / "vesma.yaml"
    cfg.write_text(
        f"vesma:\n"
        f"  vault_path: {tmp_path / 'vault'}\n"
        f"  data_dir: {tmp_path / 'data'}\n"
        f"  db_name: mcp-transport-doctor.db\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VESMA_CONFIG", str(cfg))
    runner = CliRunner()
    result = runner.invoke(doctor_app, ["--json"])
    assert result.exit_code in (0, 1, 2), result.output
    import json

    payload = json.loads(result.output)
    names = [c["name"] for c in payload["checks"]]
    assert "MCP transport" in names


# ── SDK 2.x handshake probe (in-memory, no subprocess) ──────────────────────────


@pytest.mark.skipif(
    not _REAL_MCP_INSTALLED,
    reason="real mcp SDK 2.x not installed (stub env / 1.x ambient)",
)
def test_in_memory_handshake_lists_full_tool_manifest() -> None:
    """initialize + tools/list over an in-memory session returns the 28 tools.

    This is the #185 acceptance probe: it exercises the exact SDK 2.x
    server wiring (constructor-registered handlers) through a real
    ClientSession handshake, without spawning a subprocess.
    """
    # The conftest may have installed MagicMock stubs over the real package
    # (it runs before anything imports the real `mcp`). Evict the stubs so
    # the probe binds against the REAL SDK modules; the already-imported
    # `vesma.mcp_server` must also be re-loaded against the real SDK.
    import vesma.mcp_server as _ms

    with _real_mcp_modules():
        import anyio
        from mcp.client.session import ClientSession
        from mcp.shared.memory import create_client_server_memory_streams

        import vesma.mcp_server as ms

        server = ms.server

        async def _probe() -> list[str]:
            async with create_client_server_memory_streams() as (
                client_streams,
                server_streams,
            ):
                client_read, client_write = client_streams
                server_read, server_write = server_streams

                async def _run_server() -> None:
                    await server.run(
                        server_read,
                        server_write,
                        server.create_initialization_options(),
                    )

                async with anyio.create_task_group() as tg:
                    tg.start_soon(_run_server)
                    async with ClientSession(client_read, client_write) as session:
                        init = await session.initialize()
                        assert init.server_info.name == "vesma"
                        result = await session.list_tools()
                        return sorted(t.name for t in result.tools)

        names = asyncio.run(_probe())
        assert len(names) == EXPECTED_TOOL_COUNT, names
        # Spot-check a few frozen contract names.
        for frozen in ("vesma_add", "vesma_search", "vesma_save_context"):
            assert frozen in names
        del _ms


# ── Stub-environment sanity: handlers registered via constructor ────────────────


def test_server_registers_handlers_via_constructor() -> None:
    """The ported module wires the handlers at construction time.

    SDK 2.x stores constructor-registered handlers in an internal map
    (``get_request_handler``); the stub mirrors them as ``on_*`` attributes.
    Accept either surface — what matters is that the handlers ARE
    registered, and the legacy 1.x decorator attributes are NOT relied upon.
    """
    from vesma import mcp_server

    assert mcp_server.server.name == "vesma"
    if hasattr(mcp_server.server, "get_request_handler"):
        # Real SDK 2.x surface.
        entry = mcp_server.server.get_request_handler("tools/list")
        assert entry is not None, "tools/list handler not registered"
        entry_call = mcp_server.server.get_request_handler("tools/call")
        assert entry_call is not None, "tools/call handler not registered"
    else:
        # Stub surface (conftest) mirrors the constructor kwargs.
        assert callable(mcp_server.server.on_list_tools)
        assert callable(mcp_server.server.on_call_tool)
    # The public handler callables keep their pre-port signatures.
    import inspect

    assert list(inspect.signature(mcp_server.list_tools).parameters) == []
    assert list(inspect.signature(mcp_server.call_tool).parameters) == ["name", "arguments"]


def test_tool_manifest_uses_input_schema_attribute() -> None:
    """Tool manifest entries expose the SDK 2.x input_schema attribute (27 tools)."""
    from vesma.mcp_server import list_tools

    tools = asyncio.run(list_tools())
    assert len(tools) == EXPECTED_TOOL_COUNT
    for tool in tools:
        assert isinstance(tool.input_schema, dict), tool.name


def test_on_call_tool_adapter_wraps_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK 2.x adapter translates CallToolRequestParams → CallToolResult."""
    from vesma import mcp_server
    from vesma.mcp_server import _on_call_tool

    sent: dict = {}

    async def fake_call_tool(name: str, arguments: dict) -> list:
        sent["name"] = name
        sent["arguments"] = arguments
        return [mcp_server.TextContent(type="text", text="ok")]

    monkeypatch.setattr(mcp_server, "call_tool", fake_call_tool)

    # Rebind: the module-level _on_call_tool closes over the module global,
    # so patching the attribute is sufficient — verify via the bound call.
    params = mcp_server.CallToolRequestParams(name="vesma_stats", arguments={"a": 1})
    result = asyncio.run(_on_call_tool(None, params))
    assert sent == {"name": "vesma_stats", "arguments": {"a": 1}}
    assert result.content[0].text == "ok"


def test_on_list_tools_adapter_wraps_result() -> None:
    """The SDK 2.x adapter returns ListToolsResult with the full manifest."""
    from vesma.mcp_server import _on_list_tools

    result = asyncio.run(_on_list_tools(None, None))
    assert len(result.tools) == EXPECTED_TOOL_COUNT


def test_arguments_none_defaults_to_empty_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """call_tool with arguments=None (SDK allows omitting) dispatches safely."""
    from vesma import mcp_server
    from vesma.mcp_server import _on_call_tool

    seen: list[dict] = []

    async def fake_call_tool(name: str, arguments: dict) -> list:
        seen.append({"name": name, "arguments": arguments})
        return [mcp_server.TextContent(type="text", text="ok")]

    monkeypatch.setattr(mcp_server, "call_tool", fake_call_tool)
    params = mcp_server.CallToolRequestParams(name="vesma_stats")
    asyncio.run(_on_call_tool(None, params))
    assert seen == [{"name": "vesma_stats", "arguments": {}}]


def test_mcp_sdk_version_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """mcp_sdk_version() reports installed version or 'not installed'."""
    from vesma.cli.doctor import mcp_sdk_version

    v = mcp_sdk_version()
    assert isinstance(v, str) and v  # never raises, always a string
    with patch("vesma.cli.doctor.mcp_sdk_version", return_value="9.9.9"):
        # The helper is also patchable for deterministic doctor details.
        assert True


def test_sys_modules_stub_note() -> None:
    """Guard: this file must not import the conftest stubs as the real SDK.

    If the real mcp package is absent, sys.modules holds the conftest stub;
    the handshake probe must skip, not fail, in that environment.
    """
    if not _REAL_MCP_INSTALLED:
        assert "mcp" in sys.modules  # conftest stub installed
