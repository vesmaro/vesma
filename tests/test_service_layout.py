"""Canonical layout tests (contract specs/layout/v1 §3.1-3.6).

Covers LY-01 semantics (explicit chmod, umask-independent modes — proven
under BOTH a restrictive and a permissive umask), XDG overrides with the
empty-variable-is-default rule, the §3.6 runtime fallback + WARN, and the
§3.4 per-component path resolution table.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from vesmaro.service import layout


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate every XDG root + HOME into a tmp dir (empty vars = defaults)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    return home


class TestUmaskIndependentModes:
    @pytest.mark.parametrize("umask", [0o077, 0o000])
    def test_ensure_dir_modes_are_exact(self, tmp_path: Path, umask: int) -> None:
        old_umask = os.umask(umask)
        try:
            config = layout.ensure_dir(tmp_path / "vesma", layout.MODE_DIR_DEFAULT)
            cache = layout.ensure_dir(tmp_path / "cache-comp", layout.MODE_DIR_CACHE)
        finally:
            os.umask(old_umask)
        assert oct(config.stat().st_mode & 0o777) == oct(0o700)
        assert oct(cache.stat().st_mode & 0o777) == oct(0o750)

    @pytest.mark.parametrize("umask", [0o077, 0o000])
    def test_canonical_dir_modes_after_ensure(self, isolated_home: Path, umask: int) -> None:
        old_umask = os.umask(umask)
        try:
            layout.ensure_dir(layout.config_root(), layout.MODE_DIR_DEFAULT)
            layout.ensure_dir(layout.components_dir(), layout.MODE_DIR_DEFAULT)
            layout.ensure_dir(layout.env_dir(), layout.MODE_DIR_DEFAULT)
            layout.ensure_dir(layout.cache_dir("comp"), layout.MODE_DIR_CACHE)
        finally:
            os.umask(old_umask)
        assert layout.verify_dir(layout.config_root(), 0o700)
        assert layout.verify_dir(layout.components_dir(), 0o700)
        assert layout.verify_dir(layout.env_dir(), 0o700)
        assert layout.verify_dir(layout.cache_dir("comp"), 0o750)

    def test_ensure_dir_is_idempotent_and_repairs_modes(self, tmp_path: Path) -> None:
        target = tmp_path / "d"
        layout.ensure_dir(target, 0o700)
        os.chmod(target, 0o777)
        layout.ensure_dir(target, 0o700)
        assert oct(target.stat().st_mode & 0o777) == oct(0o700)

    def test_intermediate_segments_explicit_under_permissive_umask(
        self, isolated_home: Path
    ) -> None:
        # Layout §3.1: rights are explicit for EVERY directory the flow
        # creates — deep canonical paths must not leave umask-inherited
        # (here: 0777) intermediate segments like ~/.local/state/vesma/.
        deep = layout.data_dir("comp") / "deeper" / "leaf"
        old_umask = os.umask(0o000)
        try:
            target = layout.ensure_dir(deep, layout.MODE_DIR_DEFAULT)
        finally:
            os.umask(old_umask)
        assert layout.verify_dir(isolated_home / ".local" / "share" / "vesma", 0o700)
        assert layout.verify_dir(isolated_home / ".local" / "share" / "vesma" / "comp", 0o700)
        assert layout.verify_dir(
            isolated_home / ".local" / "share" / "vesma" / "comp" / "deeper", 0o700
        )
        assert layout.verify_dir(target, 0o700)
        # Segments ABOVE the vesma root are not vesma-owned: ensure_dir
        # must not chmod them (created under umask 000 → 0777 here).
        assert not layout.verify_dir(isolated_home / ".local" / "share", 0o700)

    def test_intermediate_repair_on_preexisting_drifted_segment(self, isolated_home: Path) -> None:
        # A manually loosened intermediate segment is re-tightened by the
        # next ensure_dir underneath it (verify-style repair, LY-01).
        deep = layout.state_root() / "logs" / "comp"
        layout.ensure_dir(deep, layout.MODE_DIR_DEFAULT)
        os.chmod(layout.state_root(), 0o777)
        layout.ensure_dir(deep / "rotated", layout.MODE_DIR_DEFAULT)
        assert layout.verify_dir(layout.state_root(), 0o700)


class TestVerifyDir:
    def test_missing_path_is_false(self, tmp_path: Path) -> None:
        assert layout.verify_dir(tmp_path / "absent", 0o700) is False

    def test_wrong_mode_is_false(self, tmp_path: Path) -> None:
        target = layout.ensure_dir(tmp_path / "d", 0o700)
        os.chmod(target, 0o755)
        assert layout.verify_dir(target, 0o700) is False
        assert layout.verify_dir(target, 0o755) is True

    def test_file_is_not_a_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "f"
        target.write_text("x", encoding="utf-8")
        assert layout.verify_dir(target, 0o700) is False


class TestXdgOverrides:
    def test_defaults_under_home(self, isolated_home: Path) -> None:
        assert layout.config_root() == isolated_home / ".config" / "vesma"
        assert layout.components_dir() == isolated_home / ".config" / "vesma" / "components.d"
        assert layout.env_file_path("comp") == (
            isolated_home / ".config" / "vesma" / "env" / "comp.env"
        )
        assert layout.data_dir("comp") == isolated_home / ".local" / "share" / "vesma" / "comp"
        assert layout.engine_venv_dir() == isolated_home / ".local" / "share" / "vesma" / "venv"
        assert layout.component_venv_bin("comp") == (
            isolated_home / ".local" / "share" / "vesma" / "venvs" / "comp" / "bin"
        )
        assert layout.logs_dir("comp") == (
            isolated_home / ".local" / "state" / "vesma" / "logs" / "comp"
        )
        assert layout.history_dir() == isolated_home / ".local" / "state" / "vesma" / "history"
        assert layout.run_fallback_dir() == isolated_home / ".local" / "state" / "vesma" / "run"
        assert layout.cache_dir("comp") == isolated_home / ".cache" / "vesma" / "comp"

    def test_xdg_env_override_respected(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        assert layout.config_root() == tmp_path / "cfg" / "vesma"
        assert layout.data_dir("comp") == tmp_path / "data" / "vesma" / "comp"
        assert layout.state_root() == tmp_path / "state" / "vesma"
        assert layout.cache_dir("comp") == tmp_path / "cache" / "vesma" / "comp"

    def test_empty_xdg_var_means_default(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", "")
        monkeypatch.setenv("XDG_DATA_HOME", "   ")
        assert layout.config_root() == isolated_home / ".config" / "vesma"
        assert layout.data_root() == isolated_home / ".local" / "share" / "vesma"


class TestRuntimeResolution:
    def test_xdg_runtime_dir_set(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
        resolution = layout.resolve_runtime_dir()
        assert resolution.path == tmp_path / "run" / "vesma"
        assert resolution.used_fallback is False

    def test_empty_runtime_dir_falls_back_with_warning(
        self,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "")
        with caplog.at_level(logging.WARNING, logger="vesmaro.service.layout"):
            resolution = layout.resolve_runtime_dir()
        assert resolution.path == isolated_home / ".local" / "state" / "vesma" / "run"
        assert resolution.used_fallback is True
        assert any("XDG_RUNTIME_DIR" in record.message for record in caplog.records)

    def test_unset_runtime_dir_falls_back(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        resolution = layout.resolve_runtime_dir()
        assert resolution.used_fallback is True


class TestResolveComponentPaths:
    def test_table_matches_layout_3_4(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
        paths = layout.resolve_component_paths("board")
        assert paths.config_path == isolated_home / ".config" / "vesma" / "vesma.yaml"
        assert paths.data_dir == isolated_home / ".local" / "share" / "vesma" / "board"
        assert paths.runtime_dir == tmp_path / "run" / "vesma"
        assert paths.venv_bin == (
            isolated_home / ".local" / "share" / "vesma" / "venvs" / "board" / "bin"
        )

    def test_fallback_runtime_flows_into_component_paths(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        paths = layout.resolve_component_paths("board")
        assert paths.runtime_dir == isolated_home / ".local" / "state" / "vesma" / "run"
