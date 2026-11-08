"""The unitgen injector-unit form (ADR-0052): the generated systemd --user
unit mirrors the ratified host-local deployment directive-for-directive
(SEC-02/SEC-04 amendments included) — the generator is the canonical
source, the live unit is a deployed copy. Pins: the loopback-only
refusal at generation time, the %h path rendering, the identity argv
(port + token-file + secret-file ARE the per-harness identity).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vesma.service.unitgen import (
    INJECTOR_UNIT_NAME,
    UnitGenerationError,
    generate_injector_unit,
    parse_unit,
)

HOME = Path("/home/operator")
VENV = HOME / ".vesma" / "venv"
TOKEN = HOME / ".secrets" / "vesma_zcode_agent_token"
SECRET = HOME / ".secrets" / "vesma_proxy_listener_zcode.secret"


@pytest.fixture()
def unit_text() -> str:
    return generate_injector_unit(HOME, VENV, TOKEN, SECRET)


def _directives(text: str) -> dict[str, str]:
    directives, _commented = parse_unit(text)
    return directives


def test_unit_name_and_header_markers(unit_text: str) -> None:
    assert "Description=vesma MCP token injector" in unit_text
    assert "# vesma:template=user-injector" in unit_text
    assert INJECTOR_UNIT_NAME == "vesma-mcp-injector.service"


def test_normative_directives_match_the_ratified_deployment(unit_text: str) -> None:
    """The live-deployment directive set, byte-for-byte (ADR-0052)."""
    directives = _directives(unit_text)
    assert directives["StartLimitIntervalSec"] == "0"  # SEC-04
    assert directives["Restart"] == "always"
    assert directives["RestartSec"] == "2"
    assert directives["NoNewPrivileges"] == "true"
    assert directives["PrivateTmp"] == "true"
    assert directives["ProtectSystem"] == "strict"
    assert directives["ProtectHome"] == "read-only"
    assert directives["RestrictAddressFamilies"] == "AF_UNIX AF_INET"
    assert directives["TasksMax"] == "64"  # SEC-02
    assert directives["MemoryMax"] == "256M"  # SEC-02
    assert directives["After"] == "vesma.service"
    assert directives["WantedBy"] == "default.target"
    assert "ExecStop" not in directives  # default SIGTERM is the contract stop


def test_execstart_identity_argv_and_path_rendering(unit_text: str) -> None:
    exec_start = _directives(unit_text)["ExecStart"]
    assert exec_start.startswith("%h/.vesma/venv/bin/python -m vesma.mcp_injector")
    assert "--port 8789" in exec_start
    assert "--upstream-port 8787" in exec_start
    assert "--token-file %h/.secrets/vesma_zcode_agent_token" in exec_start
    assert "--secret-file %h/.secrets/vesma_proxy_listener_zcode.secret" in exec_start
    assert "--listen-host 127.0.0.1" in exec_start


def test_custom_ports_move_together(unit_text: str) -> None:
    text = generate_injector_unit(HOME, VENV, TOKEN, SECRET, port=8793, upstream_port=8787)
    assert "Description=vesma MCP token injector (127.0.0.1:8793 -> core 8787" in text
    assert "--port 8793" in _directives(text)["ExecStart"]


def test_non_loopback_listen_host_refused_at_generation_time() -> None:
    with pytest.raises(UnitGenerationError, match=r"127\.0\.0\.1"):
        generate_injector_unit(HOME, VENV, TOKEN, SECRET, listen_host="0.0.0.0")


def test_paths_outside_home_render_absolute() -> None:
    text = generate_injector_unit(
        HOME, VENV, Path("/etc/vesma/agent-token"), Path("/etc/vesma/listener.secret")
    )
    exec_start = _directives(text)["ExecStart"]
    assert "--token-file /etc/vesma/agent-token" in exec_start
    assert "--secret-file /etc/vesma/listener.secret" in exec_start
