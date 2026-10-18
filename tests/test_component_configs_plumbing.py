"""Component-configs plumbing (card vesma-61-component-configs-plumbing).

The operator's ``components:`` config section must REACH the supervisor:
Settings parses it, ``service run`` forwards it through ServiceApp, and the
Supervisor validates it against each manifest's schema (CM §3.9, fail-closed)
before delivering it to the component. Before the fix the Supervisor was
built with its constructor default (``{}``): operator overrides were silently
ignored (board/metrics always ran their schema defaults, e.g. the 8080 bind)
and the whole validation path — including the loopback-only public-bind
refusal — was dead code in production. The supervisor-level delivery and
refusal are already pinned by ``tests/test_service_board.py``; this module
pins the MISSING link (Settings → ServiceApp → Supervisor) end to end.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import vesma.service
from vesma.config import load_settings
from vesma.service.backend import ServiceApp
from vesma.service.logsink import HistoryJournal
from vesma.service.manifest import load_manifest

BOARD_YAML = Path(vesma.service.__file__).parent / "components" / "board.yaml"

#: Fixed on purpose: an ephemeral port cannot prove delivery — if the config
#: were silently dropped, board would bind the schema default (8080) and the
#: assertion would only catch it by luck. A fixed uncommon port fails loudly.
CONFIGURED_PORT = 9195


@pytest.fixture
def isolated_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    return home


def test_settings_components_section_parses(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        'components:\n  board:\n    bind: "127.0.0.1:9195"\n',
        encoding="utf-8",
    )
    settings = load_settings(config_file)
    assert settings.components == {"board": {"bind": "127.0.0.1:9195"}}


def test_service_app_forwards_component_configs(tmp_path: Path) -> None:
    """The ServiceApp → Supervisor pass-through: whatever the caller passes
    lands in ``supervisor._component_configs`` (production forwards
    ``settings.components`` here — pinned separately above)."""
    app = ServiceApp(
        {"board": load_manifest(BOARD_YAML)},
        with_core=False,
        socket_path=tmp_path / "svc.sock",
        journal=HistoryJournal(directory=tmp_path / "history"),
        component_configs={"board": {"bind": f"127.0.0.1:{CONFIGURED_PORT}"}},
    )
    assert app.supervisor._component_configs == {"board": {"bind": f"127.0.0.1:{CONFIGURED_PORT}"}}


def test_service_app_board_serves_on_configured_port(isolated_xdg: Path, tmp_path: Path) -> None:
    """The whole chain live: the config value reaches the in-process
    component — board binds the CONFIGURED port, not the schema default."""
    app = ServiceApp(
        {"board": load_manifest(BOARD_YAML)},
        with_core=False,
        socket_path=tmp_path / "svc.sock",
        journal=HistoryJournal(directory=tmp_path / "history"),
        component_configs={"board": {"bind": f"127.0.0.1:{CONFIGURED_PORT}"}},
    )
    app.bind()
    app.start()
    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            component = app.supervisor._components["board"]
            if component.in_process is not None and (
                app.supervisor.component_state("board") == "healthy"
            ):
                break
            time.sleep(0.05)
        instance = app.supervisor._components["board"].in_process
        assert instance is not None, "board never constructed its component"
        assert app.supervisor.component_state("board") == "healthy"
        assert instance.port == CONFIGURED_PORT  # NOT the 8080 schema default
    finally:
        app.supervisor.shutdown()
