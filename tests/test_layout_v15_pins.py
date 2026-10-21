"""Zero-regression pins: every existing user-profile resolve (layout v1.5).

GOLDEN FILE, written BEFORE the layout-v2 resolver wave (ADR-0045 rollout
wave: resolver + pin tests). Contract: specs/layout/v2 §3.3 — installations
deployed in the v1 form (the XDG spread) keep resolving per the v1 tables
until an explicit consolidation runs; the canon change must never rewrite
a working installation.

An "existing v1.5 installation" is recognized two ways after the wave —
both pinned here, and both MUST hold on the pre-wave code too (where every
resolve is trivially the v1.5 form):

- **artifact leg** — the v1.5 service-stack roots exist on disk, no stamp;
- **stamp leg** — the generation stamp says ``v1.5`` (the stamp file
  format below is the normative one the resolver reads; pre-wave the file
  is simply ignored, which is exactly why the pin is green both sides).

Everything here is exact-path: a single character of drift in any legacy
resolve fails the wave.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from vesma.service import layout

#: The generation stamp (specs/layout/v2 §3.1/§3.3; stored at the shared
#: ``~/.vesma`` root — the one directory that exists in BOTH generations).
#: Minimal YAML; the resolver reads the ``generation:`` key defensively.
STAMP_V15 = (
    "# vesma install-state (specs/layout/v2 §3.1/§3.3)\n"
    "generation: v1.5\n"
    "profile: user\n"
)


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


def _materialize_v15_artifacts(home: Path) -> None:
    """Create the four v1.5 XDG service-stack roots (an existing install)."""
    for root in (
        home / ".config" / "vesma",
        home / ".local" / "share" / "vesma",
        home / ".local" / "state" / "vesma",
        home / ".cache" / "vesma",
    ):
        root.mkdir(parents=True)


@pytest.fixture
def v15_by_artifacts(isolated_home: Path) -> Path:
    """An existing v1.5 install: roots on disk, no stamp (pre-wave form)."""
    _materialize_v15_artifacts(isolated_home)
    return isolated_home


@pytest.fixture
def v15_by_stamp(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An existing v1.5 install pinned by an explicit generation stamp."""
    vesma_home = isolated_home / ".vesma"
    vesma_home.mkdir()
    (vesma_home / "install-state.yaml").write_text(STAMP_V15, encoding="utf-8")
    return isolated_home


@pytest.mark.parametrize("home_fixture", ["v15_by_artifacts", "v15_by_stamp"])
class TestV15ResolvesPinned:
    """The FULL v1.5 resolve table, byte-exact, both recognition legs."""

    def test_config_plane(self, home_fixture: str, request: pytest.FixtureRequest) -> None:
        home = request.getfixturevalue(home_fixture)
        assert layout.config_root() == home / ".config" / "vesma"
        assert layout.canonical_config_path() == home / ".config" / "vesma" / "vesma.yaml"
        assert layout.components_dir() == home / ".config" / "vesma" / "components.d"
        assert layout.env_dir() == home / ".config" / "vesma" / "env"
        assert layout.env_file_path("comp") == home / ".config" / "vesma" / "env" / "comp.env"

    def test_data_plane(self, home_fixture: str, request: pytest.FixtureRequest) -> None:
        home = request.getfixturevalue(home_fixture)
        assert layout.data_root() == home / ".local" / "share" / "vesma"
        assert layout.data_dir("comp") == home / ".local" / "share" / "vesma" / "comp"
        assert layout.engine_venv_dir() == home / ".local" / "share" / "vesma" / "venv"
        assert layout.component_venv_dir("comp") == (
            home / ".local" / "share" / "vesma" / "venvs" / "comp"
        )
        assert layout.component_venv_bin("comp") == (
            home / ".local" / "share" / "vesma" / "venvs" / "comp" / "bin"
        )

    def test_state_plane(self, home_fixture: str, request: pytest.FixtureRequest) -> None:
        home = request.getfixturevalue(home_fixture)
        assert layout.state_root() == home / ".local" / "state" / "vesma"
        assert layout.logs_dir("comp") == home / ".local" / "state" / "vesma" / "logs" / "comp"
        assert layout.history_dir() == home / ".local" / "state" / "vesma" / "history"
        assert layout.run_fallback_dir() == home / ".local" / "state" / "vesma" / "run"

    def test_cache_plane(self, home_fixture: str, request: pytest.FixtureRequest) -> None:
        home = request.getfixturevalue(home_fixture)
        assert layout.cache_base() == home / ".cache" / "vesma"
        assert layout.cache_dir("comp") == home / ".cache" / "vesma" / "comp"

    def test_placeholder_table(
        self, home_fixture: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The §3.4 (v1) argv-placeholder expansion, byte-exact."""
        home = request.getfixturevalue(home_fixture)
        runtime = home / ".run"
        runtime.mkdir()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        paths = layout.resolve_component_paths("board")
        assert paths.config_path == home / ".config" / "vesma" / "vesma.yaml"
        assert paths.data_dir == home / ".local" / "share" / "vesma" / "board"
        assert paths.runtime_dir == runtime / "vesma"
        assert paths.venv_bin == home / ".local" / "share" / "vesma" / "venvs" / "board" / "bin"

    def test_runtime_fallback(
        self, home_fixture: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = request.getfixturevalue(home_fixture)
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        resolution = layout.resolve_runtime_dir()
        assert resolution.path == home / ".local" / "state" / "vesma" / "run"
        assert resolution.used_fallback is True


class TestV15ModesPinned:
    """Canonical modes (§3.2 table) are part of the pinned behavior."""

    def test_mode_constants(self) -> None:
        assert layout.MODE_DIR_DEFAULT == 0o700
        assert layout.MODE_DIR_CACHE == 0o750
        assert layout.MODE_FILE_ENV == 0o600


class TestV15EnvOverrideSemanticsPinned:
    """XDG override + empty-var-default semantics, and the VESMA_*
    boundary: the service layout layer is driven by XDG_*/HOME only —
    engine-side VESMA_* overrides (config.py) must not leak into it."""

    def test_xdg_overrides(self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        assert layout.config_root() == tmp_path / "cfg" / "vesma"
        assert layout.data_root() == tmp_path / "data" / "vesma"
        assert layout.state_root() == tmp_path / "state" / "vesma"
        assert layout.cache_base() == tmp_path / "cache" / "vesma"

    def test_empty_xdg_var_means_default(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", "")
        monkeypatch.setenv("XDG_DATA_HOME", "   ")
        assert layout.config_root() == isolated_home / ".config" / "vesma"
        assert layout.data_root() == isolated_home / ".local" / "share" / "vesma"

    def test_vesma_star_envs_do_not_reach_the_service_layer(
        self, v15_by_artifacts: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("VESMA_DATA_DIR", str(tmp_path / "engine-data"))
        monkeypatch.setenv("VESMA_CONFIG", str(tmp_path / "engine.yaml"))
        assert layout.data_root() == v15_by_artifacts / ".local" / "share" / "vesma"
        assert layout.canonical_config_path() == (
            v15_by_artifacts / ".config" / "vesma" / "vesma.yaml"
        )


class TestV15EngineVenvOwnershipPinned:
    """``self-update apply`` ownership: a v1.5 engine venv under the legacy
    canonical root stays OWNED (update_cmd must accept the legacy slot
    regardless of the detected generation)."""

    def test_legacy_engine_venv_is_owned(
        self,
        v15_by_artifacts: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from vesma.cli import update_cmd

        venv_root = v15_by_artifacts / ".local" / "share" / "vesma" / "venv"
        venv_root.mkdir(parents=True)
        # No marker: the canonical-slot fallback is the ownership proof
        # (the pre-marker 6.2.0 field case).
        assert update_cmd._is_canonical_engine_venv(venv_root) is True


class TestV15RuntimeWarnPinned:
    def test_fallback_warns(
        self,
        v15_by_artifacts: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        with caplog.at_level(logging.WARNING, logger="vesma.service.layout"):
            layout.resolve_runtime_dir()
        assert any("XDG_RUNTIME_DIR" in r.message for r in caplog.records)
