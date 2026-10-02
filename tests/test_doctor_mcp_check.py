"""Issue #467 — ``vesma doctor``'s MCP-config check is registry-driven.

The generic MCP check scans every target the integration registry
(``integrations/targets.yaml``) declares with an ``mcp.config`` — JSON
surfaces in their declared shape, the Codex TOML config via stdlib
``tomllib`` — and accepts BOTH key generations (brand-primary ``vesma``,
legacy ``mnemos``) during the dual period until 6.0. Report-only: the
check never writes or migrates configs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vesmaro.cli.doctor import CheckStatus, _check_mcp_server


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# Registry JSON MCP surfaces: target name → (config path under home, servers key path).
_JSON_SURFACES: dict[str, tuple[str, tuple[str, ...]]] = {
    "cursor": (".cursor/mcp.json", ("mcpServers",)),
    "agents": (".agents/mcp.json", ("mcpServers",)),
    "claude-code": (".claude.json", ("mcpServers",)),
    "windsurf": (".codeium/windsurf/mcp_config.json", ("mcpServers",)),
    "zcode": (".zcode/cli/config.json", ("mcp", "servers")),
    "opencode": (".config/opencode/opencode.json", ("mcp",)),
}

KEYGENS = ["vesma", "mnemos", "both", "absent"]


def _write_json_surface(home: Path, rel: str, key_path: tuple[str, ...], keygen: str) -> None:
    """Write one JSON MCP config carrying the given key generation."""
    cfg: dict = {}
    node = cfg
    for key in key_path[:-1]:
        node[key] = {}
        node = node[key]
    servers: dict = {}
    if keygen in ("vesma", "both"):
        servers["vesma"] = {"command": "vesma", "args": ["mcp-server"]}
    if keygen in ("mnemos", "both"):
        servers["mnemos"] = {"command": "vesma", "args": ["mcp-server"]}
    node[key_path[-1]] = servers
    dest = home / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(cfg), encoding="utf-8")


def _write_codex_toml(home: Path, keygen: str) -> None:
    lines: list[str] = []
    if keygen in ("vesma", "both"):
        lines += ["[mcp_servers.vesma]", 'command = "vesma"', 'args = ["mcp-server"]', ""]
    if keygen in ("mnemos", "both"):
        lines += ["[mcp_servers.mnemos]", 'command = "vesma"', 'args = ["mcp-server"]', ""]
    dest = home / ".codex" / "config.toml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines), encoding="utf-8")


# ── Fake-home matrix: JSON surface x key generation ──────────────────────────


@pytest.mark.parametrize("keygen", KEYGENS)
@pytest.mark.parametrize(("target", "spec"), sorted(_JSON_SURFACES.items()))
def test_json_surface_keygen_matrix(
    fake_home: Path, target: str, spec: tuple[str, tuple[str, ...]], keygen: str
) -> None:
    rel, key_path = spec
    _write_json_surface(fake_home, rel, key_path, keygen)
    result = _check_mcp_server()
    if keygen == "vesma":
        assert result.status == CheckStatus.PASS
        assert f"{target}: vesma key" in result.detail
        assert "migrate to vesma" not in result.detail
    elif keygen == "mnemos":
        assert result.status == CheckStatus.PASS, "legacy-only hosts stay green until 6.0"
        assert f"{target}: legacy mnemos key" in result.detail
        assert "migrate to vesma" in result.detail, "green, but with the deprecation note"
    elif keygen == "both":
        assert result.status == CheckStatus.PASS
        assert f"{target}: vesma + legacy mnemos keys" in result.detail
    else:
        assert result.status == CheckStatus.WARN
        assert "not registered in any known harness" in result.detail


@pytest.mark.parametrize("keygen", KEYGENS)
def test_codex_toml_keygen_matrix(fake_home: Path, keygen: str) -> None:
    _write_codex_toml(fake_home, keygen)
    result = _check_mcp_server()
    if keygen == "vesma":
        assert result.status == CheckStatus.PASS
        assert "codex: vesma key" in result.detail
    elif keygen == "mnemos":
        assert result.status == CheckStatus.PASS
        assert "codex: legacy mnemos key" in result.detail
        assert "migrate to vesma" in result.detail
    elif keygen == "both":
        assert result.status == CheckStatus.PASS
        assert "codex: vesma + legacy mnemos keys" in result.detail
    else:
        assert result.status == CheckStatus.WARN


# ── Whole-host verdicts ───────────────────────────────────────────────────────


def test_fully_migrated_host_reports_green(fake_home: Path) -> None:
    """Every registry MCP surface carries the brand-primary ``vesma`` key."""
    for rel, key_path in _JSON_SURFACES.values():
        _write_json_surface(fake_home, rel, key_path, "vesma")
    _write_codex_toml(fake_home, "vesma")
    bridge = fake_home / ".pi" / "agent" / "extensions" / "vesma-mcp.ts"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("// vesma MCP bridge extension\n", encoding="utf-8")

    result = _check_mcp_server()

    assert result.status == CheckStatus.PASS
    # The deprecation note is an exact phrase — asserting it verbatim keeps
    # the check immune to tmp-path substrings ("migrated") in the detail.
    assert "LEGACY mnemos key only" not in result.detail
    assert "migrate to vesma" not in result.detail
    for target in _JSON_SURFACES:
        assert f"{target}: vesma key" in result.detail
    assert "codex: vesma key" in result.detail
    assert "pi: MCP bridge deployed" in result.detail


def test_legacy_only_host_is_green_with_deprecation_note(fake_home: Path) -> None:
    """A host still running the pre-rebrand generation: green + migrate hint."""
    _write_json_surface(fake_home, ".cursor/mcp.json", ("mcpServers",), "mnemos")
    result = _check_mcp_server()
    assert result.status == CheckStatus.PASS
    assert "cursor: legacy mnemos key" in result.detail
    assert "vesma integration setup" in result.detail


def test_unreadable_config_is_reported_not_fatal(fake_home: Path) -> None:
    dest = fake_home / ".cursor" / "mcp.json"
    dest.parent.mkdir(parents=True)
    dest.write_text("{not json", encoding="utf-8")
    result = _check_mcp_server()
    assert result.status == CheckStatus.WARN
    assert "cursor: config unreadable" in result.detail
    assert "not registered in any known harness" in result.detail


def test_check_is_report_only(fake_home: Path) -> None:
    """No config anywhere → the check creates nothing (report-only contract)."""
    result = _check_mcp_server()
    assert result.status == CheckStatus.WARN
    assert list(fake_home.iterdir()) == [], "the check must never write anything"


def test_registry_without_mcp_surfaces_is_not_applicable(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pack that declares no MCP-config surfaces at all → PASS (n/a).

    Never WARN: otherwise ``doctor --fix`` would try to "register" against
    paths this registry does not describe (the test_proposals regression).
    """
    import yaml as _yaml

    from vesmaro.cli.integration import load_targets as _real_load

    pack = fake_home / "pack"
    pack.mkdir()
    (pack / "targets.yaml").write_text(
        _yaml.dump(
            {
                "targets": {
                    "mcp-less": {
                        "detect": [{"path": str(fake_home / "marker")}],
                        "deploy": {"instructions": str(pack / "d") + "/"},
                        "format": "copy",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = _real_load(pack / "targets.yaml")
    monkeypatch.setattr(
        "vesmaro.cli.integration.load_targets", lambda config_path=None, home=None: cfg
    )
    result = _check_mcp_server()
    assert result.status == CheckStatus.PASS
    assert "check not applicable" in result.detail
