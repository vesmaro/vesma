"""Layout resolver tests — generation matrix (layout v1 §3 + v2 §3.1-3.3).

The v1.5 resolve table is pinned byte-exact in
:mod:`tests.test_layout_v15_pins` (the zero-regression golden file); this
module covers the living behavior: the v2 self-contained table as the
new-install canon, the v2 XDG-exclusion rule (§3.2-b), the generation
stamp (read/write/detect), the reserved-name registry, the cache-plane
mode discipline (0750 survives intermediate walks — regression for the
pre-wave re-tightening bug), and the mode/verify primitives under both
generations.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from vesma.service import layout

#: The normative stamp text (same format as the resolver writes).
STAMP_V2 = "# vesma install-state (specs/layout/v2 §3.1/§3.3)\ngeneration: v2\nprofile: user\n"


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


@pytest.fixture
def v15_home(isolated_home: Path) -> Path:
    """An existing v1.5 installation (service-stack artifacts, no stamp)."""
    for root in (
        isolated_home / ".config" / "vesma",
        isolated_home / ".local" / "state" / "vesma",
        isolated_home / ".cache" / "vesma",
    ):
        root.mkdir(parents=True)
    # The data plane counts as a service-stack artifact only with a
    # service entry BEYOND the engine-venv markers (see layout docs).
    (isolated_home / ".local" / "share" / "vesma" / "board").mkdir(parents=True)
    return isolated_home


@pytest.fixture
def v2_home(isolated_home: Path) -> Path:
    """A v2 installation: explicit generation stamp, no legacy artifacts."""
    vesma_home = isolated_home / ".vesma"
    vesma_home.mkdir()
    (vesma_home / "install-state.yaml").write_text(STAMP_V2, encoding="utf-8")
    return isolated_home


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

    @pytest.mark.parametrize("home_fixture", ["v15_home", "v2_home"])
    @pytest.mark.parametrize("umask", [0o077, 0o000])
    def test_canonical_dir_modes_after_ensure(
        self, home_fixture: str, umask: int, request: pytest.FixtureRequest
    ) -> None:
        request.getfixturevalue(home_fixture)
        old_umask = os.umask(umask)
        try:
            for path, mode in layout.canonical_dir_specs():
                layout.ensure_dir(path, mode)
        finally:
            os.umask(old_umask)
        for path, mode in layout.canonical_dir_specs():
            assert layout.verify_dir(path, mode), f"{path} must be {mode:04o}"

    def test_ensure_dir_is_idempotent_and_repairs_modes(self, tmp_path: Path) -> None:
        target = tmp_path / "d"
        layout.ensure_dir(target, 0o700)
        os.chmod(target, 0o777)
        layout.ensure_dir(target, 0o700)
        assert oct(target.stat().st_mode & 0o777) == oct(0o700)

    @pytest.mark.parametrize("home_fixture", ["v15_home", "v2_home"])
    def test_intermediate_segments_explicit_under_permissive_umask(
        self, home_fixture: str, request: pytest.FixtureRequest
    ) -> None:
        # Rights are explicit for EVERY directory the flow creates — deep
        # canonical paths must not leave umask-inherited (0777 here)
        # intermediate segments.
        home = request.getfixturevalue(home_fixture)
        deep = layout.data_dir("comp") / "deeper" / "leaf"
        old_umask = os.umask(0o000)
        try:
            target = layout.ensure_dir(deep, layout.MODE_DIR_DEFAULT)
        finally:
            os.umask(old_umask)
        for segment in (
            layout.data_root(),
            layout.data_dir("comp"),
            layout.data_dir("comp") / "deeper",
            target,
        ):
            assert layout.verify_dir(segment, 0o700), f"{segment} must be 0700"
        # Segments ABOVE the vesma-owned roots are not ours: never chmod'ed
        # (created under umask 000 → 0777 here).
        if home_fixture == "v15_home":
            assert not layout.verify_dir(home / ".local" / "share", 0o700)
        else:
            assert not layout.verify_dir(home, 0o700)

    @pytest.mark.parametrize("home_fixture", ["v15_home", "v2_home"])
    def test_intermediate_repair_on_preexisting_drifted_segment(
        self, home_fixture: str, request: pytest.FixtureRequest
    ) -> None:
        # A manually loosened intermediate segment is re-tightened by the
        # next ensure_dir underneath it (verify-style repair, LY-01).
        request.getfixturevalue(home_fixture)
        deep = layout.logs_dir("comp")
        layout.ensure_dir(deep, layout.MODE_DIR_DEFAULT)
        os.chmod(layout.logs_root(), 0o777)
        layout.ensure_dir(deep / "rotated", layout.MODE_DIR_DEFAULT)
        assert layout.verify_dir(layout.logs_root(), 0o700)


class TestCachePlaneModeDiscipline:
    """Regression: the intermediate walk used to re-tighten the 0750 cache
    plane to 0700 (DR-01 then WARNed "stricter than 0750" on every install
    with component caches). Cache-plane segments keep their 0750."""

    @pytest.mark.parametrize("home_fixture", ["v15_home", "v2_home"])
    def test_cache_root_survives_component_cache_ensure(
        self, home_fixture: str, request: pytest.FixtureRequest
    ) -> None:
        request.getfixturevalue(home_fixture)
        layout.ensure_dir(layout.cache_base(), layout.MODE_DIR_CACHE)
        layout.ensure_dir(layout.cache_dir("comp"), layout.MODE_DIR_CACHE)
        assert layout.verify_dir(layout.cache_base(), 0o750)
        assert layout.verify_dir(layout.cache_dir("comp"), 0o750)

    @pytest.mark.parametrize("home_fixture", ["v15_home", "v2_home"])
    def test_deep_cache_segments_stay_0750(
        self, home_fixture: str, request: pytest.FixtureRequest
    ) -> None:
        request.getfixturevalue(home_fixture)
        layout.ensure_dir(layout.cache_dir("comp") / "sub" / "leaf", layout.MODE_DIR_CACHE)
        assert layout.verify_dir(layout.cache_base(), 0o750)
        assert layout.verify_dir(layout.cache_dir("comp"), 0o750)
        assert layout.verify_dir(layout.cache_dir("comp") / "sub", 0o750)


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


class TestV2TableIsTheNewInstallCanon:
    """A virgin HOME (no stamp, no legacy artifacts) resolves v2 §3.2."""

    def test_v2_table(self, isolated_home: Path) -> None:
        assert layout.detect_generation() is layout.LayoutGeneration.V2
        tree = isolated_home / ".vesma"
        assert layout.vesma_home() == tree
        assert layout.config_root() == tree
        assert layout.canonical_config_path() == tree / "config.yaml"
        assert layout.components_dir() == tree / "components.d"
        assert layout.env_dir() == tree / "env.d"
        assert layout.env_file_path("comp") == tree / "env.d" / "comp.env"
        assert layout.data_root() == tree / "components"
        assert layout.data_dir("comp") == tree / "components" / "comp"
        assert layout.engine_venv_dir() == tree / "venv"
        assert layout.component_venvs_root() == tree / "venvs"
        assert layout.component_venv_dir("comp") == tree / "venvs" / "comp"
        assert layout.component_venv_bin("comp") == tree / "venvs" / "comp" / "bin"
        assert layout.logs_root() == tree / "logs"
        assert layout.logs_dir("comp") == tree / "logs" / "comp"
        assert layout.history_dir() == tree / "history"
        assert layout.run_fallback_dir() == tree / "run"
        assert layout.cache_base() == tree / "cache"
        assert layout.cache_dir("comp") == tree / "cache" / "comp"
        assert layout.unit_canonical_path() == tree / "systemd" / "user" / "vesma.service"

    def test_v2_ignores_xdg_base_dirs(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """§3.2-b: the ONLY XDG variable of the v2 profile is RUNTIME_DIR."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        tree = isolated_home / ".vesma"
        assert layout.config_root() == tree
        assert layout.data_dir("comp") == tree / "components" / "comp"
        assert layout.logs_dir("comp") == tree / "logs" / "comp"
        assert layout.cache_dir("comp") == tree / "cache" / "comp"

    def test_v2_keeps_xdg_runtime_dir(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
        resolution = layout.resolve_runtime_dir()
        assert resolution.path == tmp_path / "run" / "vesma"
        assert resolution.used_fallback is False

    def test_v2_placeholder_table(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
        paths = layout.resolve_component_paths("board")
        tree = isolated_home / ".vesma"
        assert paths.config_path == tree / "config.yaml"
        assert paths.data_dir == tree / "components" / "board"
        assert paths.runtime_dir == tmp_path / "run" / "vesma"
        assert paths.venv_bin == tree / "venvs" / "board" / "bin"

    def test_v2_unit_rw_ro_paths(self, v2_home: Path) -> None:
        tree = v2_home / ".vesma"
        assert layout.unit_rw_paths() == (
            tree / "logs",
            tree / "history",
            tree / "run",
            tree / "cache",
            tree / "components",
        )
        assert layout.unit_ro_paths() == (tree / "venv", tree / "venvs")

    def test_state_root_refuses_on_v2(self, v2_home: Path) -> None:
        """No honest v2 state-plane slot exists — fail loud, not wrong."""
        with pytest.raises(layout.LayoutGenerationError, match="state_root"):
            layout.state_root()


class TestV15PlanesUnderLegacyFixture:
    """The v1.5 table under the artifact fixture (goldens live in the pin
    file; here only the slots this module exercises)."""

    def test_v15_table(self, v15_home: Path) -> None:
        assert layout.detect_generation() is layout.LayoutGeneration.V1_5
        assert layout.config_root() == v15_home / ".config" / "vesma"
        assert layout.data_dir("comp") == v15_home / ".local" / "share" / "vesma" / "comp"
        assert layout.logs_dir("comp") == (
            v15_home / ".local" / "state" / "vesma" / "logs" / "comp"
        )
        assert layout.cache_dir("comp") == v15_home / ".cache" / "vesma" / "comp"
        assert layout.state_root() == v15_home / ".local" / "state" / "vesma"
        assert layout.unit_canonical_path() == layout.installed_unit_path()

    def test_v15_unit_rw_ro_paths(self, v15_home: Path) -> None:
        assert layout.unit_rw_paths() == (
            v15_home / ".local" / "state" / "vesma",
            v15_home / ".cache" / "vesma",
            v15_home / ".local" / "share" / "vesma",
        )
        assert layout.unit_ro_paths() == (
            v15_home / ".local" / "share" / "vesma" / "venv",
            v15_home / ".local" / "share" / "vesma" / "venvs",
        )


class TestRuntimeResolution:
    def test_empty_runtime_dir_falls_back_with_warning(
        self,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", "")
        with caplog.at_level(logging.WARNING, logger="vesma.service.layout"):
            resolution = layout.resolve_runtime_dir()
        assert resolution.path == isolated_home / ".vesma" / "run"
        assert resolution.used_fallback is True
        assert any("XDG_RUNTIME_DIR" in record.message for record in caplog.records)

    def test_unset_runtime_dir_falls_back(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        resolution = layout.resolve_runtime_dir()
        assert resolution.used_fallback is True

    def test_v15_fallback_targets_the_state_plane(self, v15_home: Path) -> None:
        resolution = layout.resolve_runtime_dir()
        assert resolution.path == v15_home / ".local" / "state" / "vesma" / "run"
        assert resolution.used_fallback is True

    def test_set_but_unwritable_runtime_dir_falls_back_with_warning(
        self,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        """Issue #509: ``ProtectHome=read-only`` leaves XDG_RUNTIME_DIR SET
        but the mount read-only — the resolution must PROBE and take the
        fallback with the structured warning."""
        xdg_runtime = isolated_home / ".run"
        xdg_runtime.mkdir(mode=0o555)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg_runtime))
        with caplog.at_level(logging.WARNING, logger="vesma.service.layout"):
            resolution = layout.resolve_runtime_dir()
        assert resolution.path == isolated_home / ".vesma" / "run"
        assert resolution.used_fallback is True
        assert any("not writable" in record.message for record in caplog.records)
        assert not (xdg_runtime / "vesma" / ".vesma-write-probe-*").exists()

    def test_writable_runtime_dir_probe_passes(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        xdg_runtime = tmp_path / "run"
        xdg_runtime.mkdir()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg_runtime))
        resolution = layout.resolve_runtime_dir()
        assert resolution.path == xdg_runtime / "vesma"
        assert resolution.used_fallback is False
        assert (xdg_runtime / "vesma").is_dir()
        probes = [
            p for p in (xdg_runtime / "vesma").iterdir() if p.name.startswith(".vesma-write-probe-")
        ]
        assert probes == []


class TestGenerationStamp:
    def test_stamp_roundtrip(self, isolated_home: Path) -> None:
        assert layout.read_generation_stamp() is None
        path = layout.write_generation_stamp(layout.LayoutGeneration.V2)
        assert path == isolated_home / ".vesma" / "install-state.yaml"
        assert layout.read_generation_stamp() is layout.LayoutGeneration.V2
        assert layout.detect_generation() is layout.LayoutGeneration.V2

    def test_v15_stamp_wins_over_absent_artifacts(self, isolated_home: Path) -> None:
        isolated_home.joinpath(".vesma").mkdir()
        (isolated_home / ".vesma" / "install-state.yaml").write_text(
            "generation: v1.5\n", encoding="utf-8"
        )
        assert layout.detect_generation() is layout.LayoutGeneration.V1_5

    def test_stamp_write_is_atomic_and_0600(self, isolated_home: Path) -> None:
        path = layout.write_generation_stamp(layout.LayoutGeneration.V2)
        assert path.exists()
        assert oct(path.stat().st_mode & 0o777) == oct(0o600)
        assert not list(path.parent.glob("*.tmp-*"))

    def test_unparsable_stamp_degrades_to_detection(self, isolated_home: Path) -> None:
        tree = isolated_home / ".vesma"
        tree.mkdir()
        (tree / "install-state.yaml").write_text("generation: 42\n", encoding="utf-8")
        assert layout.read_generation_stamp() is None
        assert layout.detect_generation() is layout.LayoutGeneration.V2

    def test_artifacts_beat_no_stamp(self, v15_home: Path) -> None:
        assert layout.read_generation_stamp() is None
        assert layout.detect_generation() is layout.LayoutGeneration.V1_5
        assert layout.legacy_service_artifacts() == (
            v15_home / ".config" / "vesma",
            v15_home / ".local" / "state" / "vesma",
            v15_home / ".cache" / "vesma",
            v15_home / ".local" / "share" / "vesma",
        )
        # An EMPTY data root (the install.sh --no-venv shape: mkdir of the
        # marker parent only) is NOT a service-stack artifact.
        empty = v15_home / ".local" / "share" / "vesma" / "board"
        empty.rmdir()
        assert layout.legacy_service_artifacts() == (
            v15_home / ".config" / "vesma",
            v15_home / ".local" / "state" / "vesma",
            v15_home / ".cache" / "vesma",
        )

    def test_engine_venv_only_data_root_is_not_a_legacy_signal(self, isolated_home: Path) -> None:
        """The install.sh engine venv at the old canonical root (with its
        markers) is an UPDATE-FLOW artifact, not a v1.5 service stack — the
        machine still gets a v2 service stack."""
        venv_root = isolated_home / ".local" / "share" / "vesma" / "venv"
        venv_root.mkdir(parents=True)
        (isolated_home / ".local" / "share" / "vesma" / ".vesma-venv").write_text(
            "installer=install.sh\n", encoding="utf-8"
        )
        (isolated_home / ".local" / "share" / "vesma" / ".user-site-install").write_text(
            "installer=install.sh\n", encoding="utf-8"
        )
        assert layout.legacy_service_artifacts() == ()
        assert layout.detect_generation() is layout.LayoutGeneration.V2

    def test_component_data_alongside_engine_venv_is_legacy(self, isolated_home: Path) -> None:
        venv_root = isolated_home / ".local" / "share" / "vesma" / "venv"
        venv_root.mkdir(parents=True)
        (isolated_home / ".local" / "share" / "vesma" / "board").mkdir()
        assert layout.detect_generation() is layout.LayoutGeneration.V1_5

    def test_v2_only_service_dirs_markers(self, v2_home: Path) -> None:
        markers = layout.v2_only_service_dirs()
        tree = v2_home / ".vesma"
        assert markers == (
            tree / "venv",
            tree / "venvs",
            tree / "components",
            tree / "components.d",
            tree / "env.d",
            tree / "systemd" / "user",
        )
        assert not any(p.exists() for p in markers)


class TestReservedTreeNames:
    def test_registry_is_exact(self) -> None:
        assert (
            frozenset(
                {
                    "venv",
                    "venvs",
                    "components",
                    "components.d",
                    "env.d",
                    "cache",
                    "logs",
                    "history",
                    "run",
                    "completion",
                    "systemd",
                    "data",
                    "vault",
                    "install-state.yaml",
                }
            )
            == layout.RESERVED_TREE_NAMES
        )

    @pytest.mark.parametrize(
        ("name", "reserved"),
        [
            ("venv", True),
            ("install-state.yaml", True),
            ("env.d", True),
            ("board", False),
            ("metrics", False),
            ("my-agent-2", False),
        ],
    )
    def test_is_reserved_tree_name(self, name: str, reserved: bool) -> None:
        assert layout.is_reserved_tree_name(name) is reserved
