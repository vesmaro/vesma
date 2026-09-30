"""Tests for ``mnemos doctor`` paths overview (Part 3).

Covers:
- ``--paths`` flag shows only the paths table
- ``--paths --json`` emits a ``"paths"`` key
- Regular ``--json`` output includes ``"paths"``
- Paths table renders in normal (non-JSON) output
- Paths dict contains all expected keys
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vesmaro.cli.doctor import _collect_paths, doctor_app
from vesmaro.config import Settings

runner = CliRunner()


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch Path.home() and HOME env to a tmp directory and create a minimal config."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Create a config so doctor can load settings
    cfg = tmp_path / ".mnemos" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        f"mnemos:\n"
        f"  vault_path: {tmp_path / '.mnemos' / 'vault'}\n"
        f"  data_dir: {tmp_path / '.mnemos' / 'data'}\n"
    )
    return tmp_path


# ── _collect_paths ────────────────────────────────────────────────────────────


def test_collect_paths_returns_all_keys(isolated_home: Path) -> None:
    """_collect_paths returns all expected keys."""
    settings = Settings()
    settings.resolve_paths()
    paths = _collect_paths(settings)
    expected_keys = {
        "root",
        "config",
        "data_dir",
        "db_path",
        "vault",
        "logs",
        "cache",
        "completion",
        "mcp_config",
    }
    assert set(paths.keys()) == expected_keys


def test_collect_paths_includes_completion(isolated_home: Path) -> None:
    """_collect_paths includes the completion directory."""
    settings = Settings()
    settings.resolve_paths()
    paths = _collect_paths(settings)
    assert "completion" in paths
    assert paths["completion"].startswith("~")
    assert paths["completion"].endswith(".mnemos/completion")


def test_collect_paths_uses_tilde_abbreviation(isolated_home: Path) -> None:
    """Paths under home are abbreviated with ~."""
    settings = Settings()
    settings.resolve_paths()
    paths = _collect_paths(settings)
    assert paths["root"].startswith("~")
    assert paths["config"].startswith("~")


# ── --paths flag ──────────────────────────────────────────────────────────────


def test_doctor_paths_flag_exits_zero(isolated_home: Path) -> None:
    """``mnemos doctor --paths`` exits 0 and shows paths."""
    result = runner.invoke(doctor_app, ["--paths"])
    assert result.exit_code == 0


def test_doctor_paths_flag_shows_paths_table(isolated_home: Path) -> None:
    """``--paths`` renders a paths table (not the health check table)."""
    result = runner.invoke(doctor_app, ["--paths"])
    assert "Paths" in result.output
    assert "Root" in result.output
    assert "Config" in result.output
    assert "Vault" in result.output


def test_doctor_paths_flag_no_health_checks(isolated_home: Path) -> None:
    """``--paths`` should NOT run health checks (no 'Vesma Health Check' table)."""
    result = runner.invoke(doctor_app, ["--paths"])
    assert "Mnemos Health Check" not in result.output


def test_doctor_paths_json(isolated_home: Path) -> None:
    """``--paths --json`` emits a JSON object with a 'paths' key."""
    result = runner.invoke(doctor_app, ["--paths", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "paths" in payload
    paths = payload["paths"]
    assert "root" in paths
    assert "vault" in paths
    assert "db_path" in paths


# ── Regular doctor includes paths ─────────────────────────────────────────────


def test_doctor_json_includes_paths(isolated_home: Path) -> None:
    """Regular ``--json`` output includes a ``"paths"`` key."""
    result = runner.invoke(doctor_app, ["--json"])
    # Exit code may be 0, 1, or 2 depending on environment — that's fine.
    payload = json.loads(result.output)
    assert "paths" in payload
    assert isinstance(payload["paths"], dict)


def test_doctor_normal_shows_paths_section(isolated_home: Path) -> None:
    """Normal (non-JSON) output includes a Paths section after the table."""
    result = runner.invoke(doctor_app, [])
    # Exit code may vary (warnings in CI) — we just check output.
    assert "Paths" in result.output
