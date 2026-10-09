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

from vesma.cli.doctor import _collect_paths, doctor_app
from vesma.config import Settings

runner = CliRunner()


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch Path.home() and HOME env to a tmp directory and create a minimal config."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    # The resolved-config lookup consults VESMA_CONFIG; the suite gate sets
    # it globally — these tests must pin THEIR OWN layout, so drop it.
    monkeypatch.delenv("VESMA_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # keep ./config.yaml out of the search path
    # Create a config so doctor can load settings (the canonical search place)
    cfg = tmp_path / ".vesma" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        f"vesma:\n"
        f"  vault_path: {tmp_path / '.vesma' / 'vault'}\n"
        f"  data_dir: {tmp_path / '.vesma' / 'data'}\n"
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
    assert paths["completion"].endswith(".vesma/completion")


def test_collect_paths_uses_tilde_abbreviation(isolated_home: Path) -> None:
    """Paths under home are abbreviated with ~."""
    settings = Settings()
    settings.resolve_paths()
    paths = _collect_paths(settings)
    assert paths["root"].startswith("~")
    assert paths["config"].startswith("~")


def test_collect_paths_follows_vesma_config_env(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cli-audit 2026-10-08 (P1 #10): with VESMA_CONFIG active, Root/Config
    derive from the RESOLVED config — no ~/.mnemos leftovers mixed into the
    table."""
    custom = isolated_home / "custom-cfg" / "vesma.yaml"
    custom.parent.mkdir(parents=True)
    custom.write_text("vesma:\n", encoding="utf-8")
    monkeypatch.setenv("VESMA_CONFIG", str(custom))
    settings = Settings()
    settings.resolve_paths()
    paths = _collect_paths(settings)
    # Display tilde-abbreviates home-relative paths — compare the tails.
    assert paths["config"].endswith("custom-cfg/vesma.yaml")
    assert paths["root"].endswith("custom-cfg")
    assert ".mnemos" not in paths["root"]


def test_collect_paths_completion_and_cache_show_real_writers(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cascade fix 2026-10-09 (P3): Completion/Cache name the paths their
    actual writers use — a VESMA_CONFIG in a temp dir must not make doctor
    display a completion dir nothing would ever write to. The completion
    writer always uses ~/.vesma/completion (no config override); the cache
    lives at the §3.9 XDG cache root."""
    custom = isolated_home / "custom-cfg" / "vesma.yaml"
    custom.parent.mkdir(parents=True)
    custom.write_text("vesma:\n", encoding="utf-8")
    monkeypatch.setenv("VESMA_CONFIG", str(custom))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    settings = Settings()
    settings.resolve_paths()
    paths = _collect_paths(settings)

    assert paths["completion"].endswith(".vesma/completion"), paths["completion"]
    assert "custom-cfg" not in paths["completion"]
    assert paths["cache"].endswith(".cache/vesma"), paths["cache"]
    assert "custom-cfg" not in paths["cache"]


def test_host_venv_from_box_note_explains_version_mismatch(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cli-audit 2026-10-08 (P1 #10 / P2 host binary): a host engine venv
    built for a DIFFERENT interpreter gets an explanatory note, not a bare
    "broken" failure."""
    import sys

    from vesma.cli import doctor as doctor_mod

    host_home = isolated_home / "host-home"
    venv_cfg = host_home / ".local" / "share" / "vesma" / "venv" / "pyvenv.cfg"
    venv_cfg.parent.mkdir(parents=True)
    other = "3.11" if sys.version_info[:2] != (3, 11) else "3.12"
    venv_cfg.write_text(f"home = /usr\nversion = {other}.0\n", encoding="utf-8")
    monkeypatch.setattr(
        doctor_mod, "_distrobox_context_for_paths", lambda: (True, "box", host_home)
    )
    note = doctor_mod._host_venv_from_box_note()
    assert note is not None
    assert "NOT runnable from inside this box" in note
    assert other in note


def test_host_venv_from_box_note_absent_on_host(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No note outside a container (the host binary is fine there)."""
    from vesma.cli import doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "_distrobox_context_for_paths", lambda: (False, None, None))
    assert doctor_mod._host_venv_from_box_note() is None


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
    """``--paths --json`` emits a JSON object with a 'paths' key.

    The deprecated flag alias prints a one-line hint to STDERR (real
    stdout stays pure JSON — verified against a real process); this
    runner merges both streams, so parse from the first ``{``.
    """
    result = runner.invoke(doctor_app, ["--paths", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output[result.output.index("{") :])
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
