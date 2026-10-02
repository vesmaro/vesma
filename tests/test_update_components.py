"""Tests for ``vesma update components`` + ``timer status`` (board card W-C).

NO NETWORK anywhere in this file: every subprocess/local-API seam is
monkeypatched; the cortex manifest is read from the shipped package
resource (a local file, deterministic).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

runner = CliRunner()

updates_cli = pytest.importorskip("vesmaro.cli.update_cmd")

INSTALLED = "5.3.1"


@pytest.fixture(autouse=True)
def wide_and_not_a_box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HOME + wide console + container signals off."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "300")
    monkeypatch.setattr(updates_cli, "_CONTAINERENV", Path("/nonexistent/.containerenv"))
    monkeypatch.delenv("CONTAINER_ID", raising=False)
    monkeypatch.delenv("DISTROBOX_ENTER_ENV", raising=False)
    monkeypatch.setattr(updates_cli, "_PROD_VENV_BASES", (tmp_path / "absent-share",))
    return tmp_path


@pytest.fixture(autouse=True)
def quiet_systemctl(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """systemctl --user says "not enabled" everywhere, recording calls."""
    calls: list[tuple[str, ...]] = []

    def fake_run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(cmd))
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    return calls


def _invoke(args: list[str]) -> object:
    from vesmaro.cli.main import app

    return runner.invoke(app, ["update", *args])


def test_components_table_covers_all_surfaces(
    wide_and_not_a_box: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = wide_and_not_a_box
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "vesma-agent").write_text("#!/bin/sh\n")
    share = home / ".local" / "share"
    (share / "mnemos-prod" / "venv-main").mkdir(parents=True)
    monkeypatch.setattr(updates_cli, "_PROD_VENV_BASES", (share,))
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr("shutil.which", lambda name: None)

    result = _invoke(["components"])
    assert result.exit_code == 0, result.output
    for fragment in (
        "pip dist",
        f"vesma {INSTALLED}",
        "integration pack",
        "cortex bundle",
        "vesma-cortex-v1",
        "embedder",
        "npm package @vesmaro/vesma",
        "update timer",
        "prod venvs",
        "venv-main",
        "MANUAL GATE",
        "go binaries",
        "vesma-agent",
        "goreleaser",
    ):
        assert fragment in result.output, f"missing component fragment: {fragment}"


def test_components_json_shape(wide_and_not_a_box: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(
        updates_cli,
        "_integration_component",
        lambda: ("pack v5.3.1, targets: codex — ok", "`vesma integration update`"),
    )
    result = _invoke(["components", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    rows = payload["components"]
    assert len(rows) == 8
    by_name = {row["component"]: row for row in rows}
    assert by_name["pip dist"]["installed"] == f"vesma {INSTALLED}"
    assert "vesma update apply" in by_name["pip dist"]["update_path"]
    assert "vesma integration update" in by_name["integration pack"]["update_path"]
    assert by_name["cortex bundle"]["update_path"] == "ships with the wheel"
    assert "vesma reindex" in by_name["embedder"]["update_path"]
    assert (
        "vesma update timer install" in by_name["update timer (vesma-update.timer)"]["update_path"]
    )


def test_components_requires_no_network(
    wide_and_not_a_box: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any PyPI/network call during the inventory is a failure."""

    def no_urlopen(*a: object, **kw: object) -> object:  # pragma: no cover — asserted
        raise AssertionError("components must not touch the network")

    def no_fetch(dist: str) -> str:  # pragma: no cover — asserted
        raise AssertionError(f"components must not fetch PyPI ({dist})")

    monkeypatch.setattr("urllib.request.urlopen", no_urlopen)
    monkeypatch.setattr(updates_cli, "fetch_latest", no_fetch)
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: None)
    result = _invoke(["components"])
    assert result.exit_code == 0, result.output


def test_components_pip_row_when_nothing_installed(
    wide_and_not_a_box: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: None)
    result = _invoke(["components", "--json"])
    payload = json.loads(result.stdout)
    pip_row = next(r for r in payload["components"] if r["component"] == "pip dist")
    assert pip_row["installed"] == "-"
    assert "pip install --user vesma" in pip_row["update_path"]


def test_components_npm_row_states(
    wide_and_not_a_box: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    result = _invoke(["components", "--json"])
    assert result.exit_code == 0
    rows = json.loads(result.stdout)["components"]
    npm_row = next(r for r in rows if r["component"].startswith("npm"))
    assert npm_row["installed"] == "-"
    assert "npm not found" in npm_row["update_path"]


def test_components_timer_row_names_the_box(
    wide_and_not_a_box: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = wide_and_not_a_box / "hosthome"
    box_home = host_home / ".distrobox" / "ubuntu" / "home"
    box_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(box_home))
    monkeypatch.setenv("CONTAINER_ID", "ubuntu")
    result = _invoke(["components", "--json"])
    assert result.exit_code == 0
    timer_row = next(
        r
        for r in json.loads(result.stdout)["components"]  # type: ignore[index]
        if str(r["component"]).startswith("update timer")  # type: ignore[index]
    )
    assert "in distrobox 'ubuntu'" in timer_row["installed"]  # type: ignore[index]


def test_integration_component_reuses_manager_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row aggregates verify() across detected targets — no logic fork."""
    import vesmaro.cli.integration as integration_module

    targets = types.SimpleNamespace(
        detected=lambda: [
            types.SimpleNamespace(name="codex"),
            types.SimpleNamespace(name="vscode"),
        ]
    )

    class FakeManager:
        def __init__(self, version: str, **kw: object) -> None:
            self.version = version

        def verify(self, target_name: str) -> types.SimpleNamespace:
            # codex is stale, vscode is clean → 2 stale, 0 missing total
            return types.SimpleNamespace(
                stale_count=2 if target_name == "codex" else 0,
                missing_count=0,
            )

    monkeypatch.setattr(integration_module, "load_targets", lambda *a, **kw: targets)
    monkeypatch.setattr(integration_module, "IntegrationManager", FakeManager)

    installed, path = updates_cli._integration_component()
    assert installed.startswith("pack v")
    assert "codex, vscode" in installed
    assert "2 stale, 0 missing" in installed
    assert path == "`vesma integration update`"


def test_integration_component_degrades_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vesmaro.cli.integration as integration_module

    def boom() -> object:
        raise FileNotFoundError("targets.yaml not found")

    monkeypatch.setattr(integration_module, "load_targets", boom)
    installed, path = updates_cli._integration_component()
    assert installed == "-"
    assert "vesma integration setup" in path


def test_cortex_component_reads_shipped_manifest() -> None:
    installed, path = updates_cli._cortex_component()
    assert installed.startswith("vesma-cortex-v1 (rev ")
    assert "trained " in installed
    assert path == "ships with the wheel"


def test_cortex_component_degrades_on_corrupt_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> dict[str, object]:
        raise ValueError("corrupt manifest")

    monkeypatch.setattr(updates_cli, "_cortex_manifest", boom)
    installed, path = updates_cli._cortex_component()
    assert installed == "-"
    assert path == "ships with the wheel"


def test_embedder_component_settings_failure_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vesmaro.config as config_module

    def boom() -> object:
        raise RuntimeError("broken config")

    monkeypatch.setattr(config_module, "load_settings", boom)
    installed, path = updates_cli._embedder_component()
    assert installed == "-"
    assert "vesma reindex" in path


def test_embedder_component_flags_vintage_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored fingerprint differing from the configured embedder is loud."""
    import vesmaro.config as config_module
    import vesmaro.embeddings as embeddings_module

    cfg = types.SimpleNamespace(provider="nano", model="vesma-embed-v1")
    settings = types.SimpleNamespace(
        embedding=cfg, mnemos=types.SimpleNamespace(data_dir=str(tmp_path))
    )
    monkeypatch.setattr(config_module, "load_settings", lambda: settings)
    monkeypatch.setattr(embeddings_module, "config_fingerprint", lambda c: "nano:sha256:aaa")

    vectors = tmp_path / "vectors.db"
    conn = sqlite3.connect(str(vectors))
    try:
        conn.execute("CREATE TABLE embeddings (id INTEGER PRIMARY KEY, metadata TEXT)")
        conn.execute(
            "INSERT INTO embeddings (metadata) VALUES (?)",
            (json.dumps({"model_fingerprint": "nano:sha256:bbb"}),),
        )
        conn.commit()
    finally:
        conn.close()

    installed, path = updates_cli._embedder_component()
    assert "nano:vesma-embed-v1" in installed
    assert "VINTAGE MISMATCH" in installed
    assert path == "`vesma reindex`"


def test_embedder_component_matching_vintage_is_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vesmaro.config as config_module
    import vesmaro.embeddings as embeddings_module

    cfg = types.SimpleNamespace(provider="nano", model="vesma-embed-v1")
    settings = types.SimpleNamespace(
        embedding=cfg, mnemos=types.SimpleNamespace(data_dir=str(tmp_path))
    )
    monkeypatch.setattr(config_module, "load_settings", lambda: settings)
    monkeypatch.setattr(embeddings_module, "config_fingerprint", lambda c: "nano:sha256:aaa")

    vectors = tmp_path / "vectors.db"
    conn = sqlite3.connect(str(vectors))
    try:
        conn.execute("CREATE TABLE embeddings (id INTEGER PRIMARY KEY, metadata TEXT)")
        conn.execute(
            "INSERT INTO embeddings (metadata) VALUES (?)",
            (json.dumps({"model_fingerprint": "nano:sha256:aaa"}),),
        )
        conn.commit()
    finally:
        conn.close()

    installed, _ = updates_cli._embedder_component()
    assert "VINTAGE MISMATCH" not in installed


# ── timer status ──────────────────────────────────────────────────────────────


def test_timer_status_json_local_not_installed(wide_and_not_a_box: Path) -> None:
    result = _invoke(["timer", "status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["unit"] == "vesma-update.timer"
    assert payload["state"] == "not installed"
    assert payload["last_trigger"] == "never"
    assert payload["container"] is False
    assert "box" in payload


def test_timer_status_enabled_with_units(
    wide_and_not_a_box: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit_dir = wide_and_not_a_box / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "vesma-update.timer").write_text("[Timer]\n")

    def fake_run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
        if "is-enabled" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="enabled\n", stderr="")
        if "show" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="LastTriggerUSec=Thu 2026-10-01 12:00:00 UTC\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    result = _invoke(["timer", "status", "--json"])
    payload = json.loads(result.stdout)
    assert payload["state"] == "enabled"
    assert "2026-10-01" in str(payload["last_trigger"])


def test_timer_status_reports_host_leg_from_a_box(
    wide_and_not_a_box: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = wide_and_not_a_box / "hosthome"
    box_home = host_home / ".distrobox" / "ubuntu" / "home"
    box_home.mkdir(parents=True)
    host_units = host_home / ".config" / "systemd" / "user"
    host_units.mkdir(parents=True)
    (host_units / "vesma-update.timer").write_text("[Timer]\n")
    (host_units / "vesma-update.service").write_text("[Service]\n")
    monkeypatch.setenv("HOME", str(box_home))

    result = _invoke(["timer", "status", "--json"])
    payload = json.loads(result.stdout)
    assert payload["container"] is True
    assert payload["box"] == "ubuntu"
    assert payload["host_state"] == "installed (disabled)"


def test_systemctl_property_parses_show_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd, 0, stdout="LastTriggerUSec=Fri 2026-09-25 03:11:00 UTC\n", stderr=""
        )

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    value = updates_cli._systemctl_property("vesma-update.timer", "LastTriggerUSec")
    assert value == "Fri 2026-09-25 03:11:00 UTC"


def test_systemctl_property_none_when_manager_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str] | None:
        return None  # the real _run_cmd's degradation contract

    monkeypatch.setattr(updates_cli, "_run_cmd", failing_run)
    assert updates_cli._systemctl_property("vesma-update.timer", "LastTriggerUSec") is None
