"""Unit tests for the Hermes VesmaMemoryProvider contract shim (#125 W5).

The plugin at ``integrations/hermes/__init__.py`` imports two Hermes-internal
modules that are not available in the Vesma test environment:

  - ``agent.memory_provider.MemoryProvider``  — the Hermes ABC
  - ``tools.registry.tool_error``              — Hermes tool-error helper

We install minimal stubs into ``sys.modules`` (mirroring how
``conftest.py`` stubs the optional ``mcp`` package) so the plugin can be
imported and its pure-Python surface tested without a running Hermes
installation. Since the W5 migration the plugin is a THIN shim over
``vesmaro.adapters.hermes.HermesMemoryAdapter``; the adapter's own behavior
(write-sparing policy, tag contract, scans, hooks) is pinned IN-PROCESS by
``tests/test_hermes_adapter.py`` — this suite pins the SHIM: registration,
tool schemas, config surface, lifecycle delegation (via a mocked adapter —
no manager is constructed here), the harness-never-blocks guard, and
session rebinding.

NOTE: the plugin imports ``vesmaro.adapters.hermes`` — run the suite with
the working-tree ``src/`` first on ``sys.path`` (the repo CI layout); the
``tests/test_hermes_adapter.py`` bootstrap does this for the whole run.
"""

from __future__ import annotations

import json
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub the Hermes-internal imports the plugin depends on.
# ---------------------------------------------------------------------------


class _StubMemoryProvider:
    """Minimal stand-in for ``agent.memory_provider.MemoryProvider``."""


def _stub_tool_error(msg: str) -> str:
    """Mirror ``tools.registry.tool_error`` — returns a JSON error string."""
    return json.dumps({"error": msg})


def _install_hermes_stubs() -> None:
    """Inject ``agent.memory_provider`` and ``tools.registry`` stubs."""
    if "agent.memory_provider" not in sys.modules:
        agent_pkg = types.ModuleType("agent")
        agent_pkg.__path__ = []  # mark as package
        mp_mod = types.ModuleType("agent.memory_provider")
        mp_mod.MemoryProvider = _StubMemoryProvider
        sys.modules["agent"] = agent_pkg
        sys.modules["agent.memory_provider"] = mp_mod

    if "tools.registry" not in sys.modules:
        tools_pkg = types.ModuleType("tools")
        tools_pkg.__path__ = []
        reg_mod = types.ModuleType("tools.registry")
        reg_mod.tool_error = _stub_tool_error
        sys.modules["tools"] = tools_pkg
        sys.modules["tools.registry"] = reg_mod


_install_hermes_stubs()

# Make ``integrations`` importable as a package (repo root on sys.path).
_REPO_ROOT = None
for _p in sys.path:
    if _p and os.path.isdir(os.path.join(_p, "integrations", "hermes")):
        _REPO_ROOT = _p
        break
if _REPO_ROOT is None:
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from integrations.hermes import (  # noqa: E402
    VesmaMemoryProvider,
    _load_config,
    register,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(**overrides) -> VesmaMemoryProvider:
    """Build a provider with a config dict (skips env/yaml loading)."""
    cfg = {
        "data_dir": "",
        "vault_path": "",
        "project": "hermes",
        "agent": "hermes-default",
        "auto_sync": True,
        "publish_on_write": True,
        "sync_interval": 10,
        "sync_min_user_chars": 50,
    }
    cfg.update(overrides)
    return VesmaMemoryProvider(cfg)


def _make_mock_adapter() -> MagicMock:
    """A mocked adapter standing in for the constructed one."""
    adapter = MagicMock()
    adapter.project = "hermes"
    adapter.agent = "hermes-default"
    adapter.session = "sess-1"
    return adapter


# ---------------------------------------------------------------------------
# 1. Plugin imports correctly
# ---------------------------------------------------------------------------


class TestPluginImport:
    def test_register_function_exists(self):
        assert callable(register)

    def test_provider_class_exists(self):
        assert VesmaMemoryProvider is not None
        from agent.memory_provider import MemoryProvider

        assert issubclass(VesmaMemoryProvider, MemoryProvider)

    def test_provider_name_property(self):
        p = _make_provider()
        assert p.name == "vesma"


# ---------------------------------------------------------------------------
# 2. 15 tool schemas (model-facing contract unchanged by the migration)
# ---------------------------------------------------------------------------


class TestToolSchemas:
    def test_exactly_15_schemas(self):
        p = _make_provider()
        assert len(p.get_tool_schemas()) == 15

    def test_all_names_start_with_vesma(self):
        p = _make_provider()
        for s in p.get_tool_schemas():
            assert s["name"].startswith("vesma_"), s["name"]
            # 6.0.0: the legacy mnemos_* spellings are gone everywhere —
            # the in-process Hermes manifest mirrors the MCP wire surface.
            assert not s["name"].startswith("mnemos_"), s["name"]

    def test_schema_names_unique(self):
        p = _make_provider()
        names = [s["name"] for s in p.get_tool_schemas()]
        assert len(names) == len(set(names))

    def test_schemas_have_parameters(self):
        p = _make_provider()
        for s in p.get_tool_schemas():
            assert "parameters" in s
            assert "type" in s["parameters"]

    def test_legacy_http_config_keys_absent_from_schemas(self):
        """The migration removed the HTTP transport — no schema may
        advertise base_url/api_key/totp parameters."""
        p = _make_provider()
        for s in p.get_tool_schemas():
            props = s["parameters"].get("properties", {})
            assert "base_url" not in props
            assert "api_key" not in props


# ---------------------------------------------------------------------------
# 3. Config loading defaults
# ---------------------------------------------------------------------------


class TestConfigLoading:
    def test_default_project_and_agent(self, monkeypatch):
        for var in ("VESMARO_PROJECT", "VESMARO_AGENT"):
            monkeypatch.delenv(var, raising=False)
        cfg = _load_config()
        assert cfg["project"] == "hermes"
        assert cfg["agent"] == "hermes-default"

    def test_default_sync_interval(self, monkeypatch):
        monkeypatch.delenv("VESMARO_SYNC_INTERVAL", raising=False)
        cfg = _load_config()
        assert cfg["sync_interval"] == 10

    def test_default_store_paths_empty(self, monkeypatch):
        """Empty data_dir/vault_path = mnemos defaults (no HTTP base_url)."""
        for var in ("VESMARO_DATA_DIR", "VESMARO_VAULT__VAULT_PATH"):
            monkeypatch.delenv(var, raising=False)
        cfg = _load_config()
        assert cfg["data_dir"] == ""
        assert cfg["vault_path"] == ""
        assert "base_url" not in cfg
        assert "api_key" not in cfg

    def test_default_publish_on_write_true(self, monkeypatch):
        monkeypatch.delenv("VESMARO_PUBLISH_ON_WRITE", raising=False)
        cfg = _load_config()
        assert cfg["publish_on_write"] is True


# ---------------------------------------------------------------------------
# 4. Config schema (new contract surface)
# ---------------------------------------------------------------------------


class TestConfigSchema:
    def test_eight_fields(self):
        p = _make_provider()
        assert len(p.get_config_schema()) == 8

    def test_expected_keys(self):
        p = _make_provider()
        keys = {f["key"] for f in p.get_config_schema()}
        assert keys == {
            "data_dir",
            "vault_path",
            "project",
            "agent",
            "auto_sync",
            "publish_on_write",
            "sync_interval",
            "sync_min_user_chars",
        }

    def test_no_legacy_http_keys(self):
        p = _make_provider()
        keys = {f["key"] for f in p.get_config_schema()}
        assert "base_url" not in keys
        assert "api_key" not in keys
        assert "totp_secret" not in keys


# ---------------------------------------------------------------------------
# 5. save_config writes to memory.mnemos
# ---------------------------------------------------------------------------


class TestSaveConfig:
    def test_writes_to_memory_mnemos(self, tmp_path):
        """save_config must write under memory.vesma, not plugins.*."""
        p = _make_provider()
        hermes_home = str(tmp_path)
        values = {"project": "test", "agent": "hermes-main"}

        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML not installed")

        p.save_config(values, hermes_home)

        config_path = tmp_path / "config.yaml"
        assert config_path.exists()
        with open(config_path) as f:
            data = yaml.safe_load(f)
        assert "memory" in data
        assert "vesma" in data["memory"]
        assert data["memory"]["vesma"] == values
        assert "plugins" not in data or "vesma" not in data.get("plugins", {})


# ---------------------------------------------------------------------------
# 6. Lifecycle delegation to the adapter (mocked — no manager here)
# ---------------------------------------------------------------------------


class TestLifecycleDelegation:
    def test_sync_turn_delegates_with_guard(self):
        p = _make_provider()
        adapter = _make_mock_adapter()
        p._adapter = adapter
        p.sync_turn("x" * 200, "y", session_id="sess-1")
        adapter.sync_turn.assert_called_once_with("x" * 200, "y")

    def test_sync_turn_never_raises(self):
        """Harness-never-blocks: adapter failures degrade to a log line."""
        p = _make_provider()
        adapter = _make_mock_adapter()
        adapter.sync_turn.side_effect = RuntimeError("boom")
        p._adapter = adapter
        p.sync_turn("x" * 200, "y", session_id="sess-1")  # must not raise

    def test_sync_turn_skips_non_primary_context(self):
        p = _make_provider()
        adapter = _make_mock_adapter()
        p._adapter = adapter
        p._agent_context = "cron"
        p.sync_turn("x" * 200, "y", session_id="sess-1")
        adapter.sync_turn.assert_not_called()

    def test_on_memory_write_delegates(self):
        p = _make_provider()
        adapter = _make_mock_adapter()
        p._adapter = adapter
        p.on_memory_write("add", "user", "prefer concise answers")
        adapter.mirror_memory_write.assert_called_once_with(
            "add", "user", "prefer concise answers", None
        )

    def test_on_session_end_delegates(self):
        p = _make_provider()
        adapter = _make_mock_adapter()
        p._adapter = adapter
        messages = [{"role": "user", "content": "x" * 200}]
        p.on_session_end(messages)
        adapter.session_end.assert_called_once_with(messages)

    def test_on_pre_compress_reports_rewrite_event(self):
        """The ADR-0018 bridge: discarded user messages become ONE
        on_context_rewrite event via the adapter."""
        p = _make_provider()
        adapter = _make_mock_adapter()
        p._adapter = adapter
        messages = [
            {"role": "user", "content": "decision about the gateway rotation"},
            {"role": "assistant", "content": "ok"},
        ]
        hint = p.on_pre_compress(messages)
        adapter.report_context_rewrite.assert_called_once()
        original = adapter.report_context_rewrite.call_args[0][0]
        assert "gateway rotation" in original
        assert "Vesma" in hint

    def test_on_pre_compress_no_user_messages_no_report(self):
        p = _make_provider()
        adapter = _make_mock_adapter()
        p._adapter = adapter
        assert p.on_pre_compress([{"role": "assistant", "content": "only"}]) == ""
        adapter.report_context_rewrite.assert_not_called()

    def test_queue_prefetch_uses_assemble_context(self):
        """Prefetch routes through pre_llm_call (assemble_context), and the
        cached result is served by prefetch() exactly once."""
        p = _make_provider()
        adapter = _make_mock_adapter()
        adapter.pre_llm_call.return_value = {
            "text": "block",
            "blocks": [{"provenance": "[mnemos:id1]", "content": "excerpt"}],
        }
        p._adapter = adapter

        p.queue_prefetch("cert rotation")
        if p._prefetch_thread:
            p._prefetch_thread.join(timeout=5.0)

        adapter.pre_llm_call.assert_called_once_with(query="cert rotation")
        result = p.prefetch("cert rotation")
        assert "[mnemos:id1]" in result
        # Served once — the next prefetch drains empty.
        assert p.prefetch("cert rotation") == ""


# ---------------------------------------------------------------------------
# 7. Session rebinding
# ---------------------------------------------------------------------------


class TestSessionRebind:
    def test_initialize_binds_session_and_identity(self):
        p = _make_provider()
        p._sdk = MagicMock()  # sdk present → _rebind builds a REAL adapter
        p.initialize("sess-1", agent_identity="hermes-main")
        # agent_identity from Hermes overrides the config agent slug.
        assert p._config["agent"] == "hermes-main"
        assert p._adapter is not None
        assert p._adapter.session == "sess-1"
        assert p._adapter.agent == "hermes-main"

    def test_switch_reset_rebuilds_adapter(self):
        p = _make_provider()
        adapter = _make_mock_adapter()
        sdk = MagicMock()
        p._adapter = adapter
        p._sdk = sdk
        p.on_session_switch("sess-2", reset=True)
        # reset=True rebuilds a fresh adapter over the same SDK (fresh
        # turn counters) and binds the new session.
        assert p._adapter is not adapter
        assert p._adapter.session == "sess-2"
        assert p._sdk is sdk

    def test_switch_continue_keeps_adapter(self):
        p = _make_provider()
        adapter = _make_mock_adapter()
        p._adapter = adapter
        p._sdk = MagicMock()
        p.on_session_switch("sess-2", reset=False)
        # Continuation keeps the adapter (and its counters), rebinds only.
        assert p._adapter is adapter
        adapter.bind_session.assert_called_once_with("sess-2")


# ---------------------------------------------------------------------------
# 8. Tool dispatch sanity
# ---------------------------------------------------------------------------


class TestHandleToolCall:
    def test_unknown_tool_returns_error(self):
        p = _make_provider()
        result = p.handle_tool_call("mnemos_bogus", {})
        data = json.loads(result)
        assert "error" in data

    def test_tool_failure_returns_error_not_exception(self):
        """A failing tool degrades to a tool_error string, never an
        exception into the harness."""
        p = _make_provider()
        adapter = _make_mock_adapter()
        adapter.search.side_effect = RuntimeError("boom")
        p._adapter = adapter
        result = p.handle_tool_call("vesma_search", {"query": "x"})
        data = json.loads(result)
        assert "error" in data

    def test_auto_collect_counter_increments(self):
        p = _make_provider()
        adapter = _make_mock_adapter()
        p._adapter = adapter
        p.handle_tool_call("vesma_search", {"query": "x"})
        result = p.handle_tool_call("vesma_auto_collect_status", {})
        data = json.loads(result)
        assert data["signals"]["call_counter"]["calls_since_save"] >= 1
