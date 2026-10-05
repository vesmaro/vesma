"""Board bind pinning (cascade 2026-10-05 P1-C).

Loopback-only is the v1 security posture (public-bind edge): the bundled
manifest's ``bind`` schema refuses any non-loopback host — the supervisor
turns the schema violation into a fail-closed start refusal
(CONFIG_INVALID family). Widening the bind is an explicit config change
for a future wave, never a schema afterthought.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

import vesmaro.service
from vesmaro.service.logsink import HistoryJournal
from vesmaro.service.manifest import load_manifest
from vesmaro.service.supervisor import Supervisor

BOARD_YAML = Path(vesmaro.service.__file__).parent / "components" / "board.yaml"


@pytest.fixture
def isolated_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate HOME + every XDG root so nothing leaks into the real home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)
    return home


def _wait_until(predicate: Any, message: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {message}")


def _schema() -> dict[str, Any]:
    doc = yaml.safe_load(BOARD_YAML.read_text(encoding="utf-8"))
    schema = doc["config"]["schema_inline"]
    assert isinstance(schema, dict)
    return schema


class TestBindSchemaPin:
    @pytest.mark.parametrize(
        "value",
        ["127.0.0.1:8080", "127.0.0.1", "localhost", "localhost:8443", "::1", "[::1]:8443"],
    )
    def test_loopback_forms_accepted(self, value: str) -> None:
        jsonschema.validate({"bind": value}, _schema())  # must not raise

    @pytest.mark.parametrize(
        "value",
        [
            "0.0.0.0:9999",
            "0.0.0.0",
            "[::]:8080",
            "::",
            "example.com:80",
            "192.168.1.10:8080",
            "127.0.0.2:8080",
            "[::1]:123456",  # 6-digit port is outside {1,5}
        ],
    )
    def test_non_loopback_or_malformed_refused(self, value: str) -> None:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"bind": value}, _schema())


class TestSupervisorBindRefusal:
    def test_bind_0_0_0_0_refused_fail_closed(self, isolated_xdg: Path, tmp_path: Path) -> None:
        """A public bind is a config-schema violation → the start is
        refused (CONFIG_INVALID), the component parks, and the journal
        carries the honest refusal line."""
        journal = HistoryJournal(directory=tmp_path / "history")
        sup = Supervisor(
            {"board": load_manifest(BOARD_YAML)},
            journal=journal,
            component_configs={"board": {"bind": "0.0.0.0:9999"}},
        )
        sup.start()
        try:
            _wait_until(
                lambda: sup._components["board"].spawn_refused,
                "the 0.0.0.0 bind must refuse the start",
            )
        finally:
            sup.shutdown()
        assert sup.component_state("board") == "stopped"  # fail-closed park
        rows = [ln.partition(" ")[2] for ln in journal.read_records()]
        assert (
            "vesma.supervisor component=board event=degraded pid=none "
            "state=stopped reason=config-invalid attempts=0 window=none"
        ) in rows


class TestSupervisorLoopbackServe:
    def test_bind_127_0_0_1_serves(self, isolated_xdg: Path, tmp_path: Path) -> None:
        """The schema-accepted loopback bind serves for real. Port 0 keeps
        the test hermetic (ephemeral port; the schema/parse path under test
        is identical to a fixed port)."""
        journal = HistoryJournal(directory=tmp_path / "history")
        sup = Supervisor(
            {"board": load_manifest(BOARD_YAML)},
            journal=journal,
            component_configs={"board": {"bind": "127.0.0.1:0"}},
        )
        sup.start()
        try:
            _wait_until(
                lambda: sup.component_state("board") == "healthy",
                "board never reached healthy on a loopback bind",
                timeout=30.0,
            )
            instance = sup._components["board"].in_process
            assert instance is not None
            port = instance.port
            assert port is not None and port > 0
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            assert payload == {"health": "healthy"}
        finally:
            sup.shutdown()
