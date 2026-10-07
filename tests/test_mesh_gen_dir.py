"""GEN_DIR resolution of ``vesma._mesh_gen`` (issue #514).

The shim resolves its generated-stubs directory from candidate paths +
the ``VESMA_MESH_GEN_DIR`` env override. The failure class fixed here:
the old single-candidate arithmetic assumed the source-checkout depth
(``src/vesma/_mesh_gen.py`` -> repo root); in a wheel install
(``<venv>/lib/python3.14/site-packages/vesma/``) three ``parent`` hops
land in ``<venv>/lib/python3.14/`` and the stubs are never found.

Test strategy: the REAL ``vesma._mesh_gen`` is already imported by the
suite (its ``_GEN_DIR`` pins the source-checkout candidate via the real
generated stubs). For the other layouts a COPY of ``_mesh_gen.py`` is
exec'd from a fabricated directory tree — the copy derives candidates
from its own ``__file__`` exactly like a production install would. The
eager ``mnemos_core_api_pb2`` imports resolve from ``sys.modules`` (the
real shim has imported them), so no stub files need to exist unless the
resolution path under test would genuinely read them; the ``sys.path``
entries the copy inserts are restored afterwards.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from vesma import _mesh_gen


def _exec_shim_copy(shim_source: Path, tree_root: Path, module_name: str):
    """Copy ``shim_source`` into ``tree_root/vesma/`` and exec it there.

    Returns the executed module; restores any ``sys.path`` entries the
    copy inserted so the fake tree never leaks into other tests.
    """

    package_dir = tree_root / "vesma"
    package_dir.mkdir(parents=True, exist_ok=True)
    shim_copy = package_dir / "_mesh_gen.py"
    shim_copy.write_text(shim_source.read_text(encoding="utf-8"), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(module_name, shim_copy)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    path_before = list(sys.path)
    try:
        spec.loader.exec_module(module)
    finally:
        added = set(map(str, tree_root.iterdir()))
        sys.path[:] = [
            entry
            for entry in sys.path
            if (entry not in added and entry in path_before) or entry in path_before
        ]
        # exact restore: sys.path must be byte-equivalent to before
        sys.path[:] = path_before + [
            entry
            for entry in sys.path
            if entry not in map(str, path_before) and _is_added_by_copy(entry, tree_root)
        ]
        sys.modules.pop(module_name, None)
    return module


def _is_added_by_copy(entry: str, tree_root: Path) -> bool:  # pragma: no cover - helper
    return entry.startswith(str(tree_root))


def _real_shim_source() -> Path:
    source = Path(_mesh_gen.__file__)
    assert source is not None
    return source


class TestSourceCheckoutLayout:
    def test_real_gen_dir_is_the_source_checkout_candidate(self) -> None:
        """The live suite's stubs resolve to candidate (a): repo-root form.

        The worktree must have run ``scripts/gen-proto.sh`` (the whole
        mesh test family already requires it), so candidate (a) exists
        and wins for ``src/vesma/_mesh_gen.py``.
        """

        expected = Path(_mesh_gen.__file__).resolve().parents[2] / "federation" / "gen" / "python"
        assert expected == _mesh_gen._GEN_DIR
        assert _mesh_gen._GEN_DIR.is_dir()

    def test_candidate_arithmetic_matches_both_layouts(self, tmp_path: Path) -> None:
        """The two structural candidates for a given ``__file__`` depth:
        source checkout (3 hops -> repo root) and site-packages (2 hops ->
        site-packages dir)."""

        source_root = tmp_path / "repo"
        candidates = [
            source_root / "src" / "vesma" / "_mesh_gen.py",
            source_root / "lib" / "python3.14" / "site-packages" / "vesma" / "_mesh_gen.py",
        ]
        resolved = [Path(str(p)).resolve() for p in candidates]
        expected = [
            resolved[0].parents[2] / "federation" / "gen" / "python",
            resolved[1].parents[1] / "federation" / "gen" / "python",
        ]
        assert expected[0] == source_root / "federation" / "gen" / "python"
        assert expected[1] == (
            source_root / "lib" / "python3.14" / "site-packages" / "federation" / "gen" / "python"
        )


class TestSitePackagesLayout:
    def test_wheel_layout_resolves_to_site_packages_candidate(self, tmp_path: Path) -> None:
        """Issue #514 layout (b): ``vesma/`` directly inside
        ``site-packages`` — two hops to ``site-packages``, stub sibling
        ``federation/gen/python`` there. Candidate (a) (tmp root form)
        does NOT exist, so (b) must win."""

        site_packages = tmp_path / "lib" / "python3.14" / "site-packages"
        gen_dir = site_packages / "federation" / "gen" / "python"
        gen_dir.mkdir(parents=True)
        module = _exec_shim_copy(_real_shim_source(), site_packages, "mesh_gen_wheel_layout")
        assert gen_dir == module._GEN_DIR

    def test_lib64_symlink_resolves_before_arithmetic(self, tmp_path: Path) -> None:
        """A ``lib64 -> lib`` symlink (Fedora-style venvs) must be
        resolved away so the two-hop arithmetic lands in the REAL
        site-packages directory."""

        lib = tmp_path / "lib"
        site_packages = lib / "python3.14" / "site-packages"
        gen_dir = site_packages / "federation" / "gen" / "python"
        gen_dir.mkdir(parents=True)
        lib64 = tmp_path / "lib64"
        lib64.symlink_to(lib)
        module = _exec_shim_copy(
            _real_shim_source(),
            lib64 / "python3.14" / "site-packages",
            "mesh_gen_lib64_layout",
        )
        assert gen_dir == module._GEN_DIR


class TestEnvOverride:
    def test_env_override_is_authoritative(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        override = tmp_path / "stubs"
        override.mkdir()
        monkeypatch.setenv("VESMA_MESH_GEN_DIR", str(override))
        module = _exec_shim_copy(_real_shim_source(), tmp_path / "elsewhere", "mesh_gen_env_ok")
        assert override == module._GEN_DIR

    def test_missing_env_override_fails_without_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wrong override is an error NAMING the variable — the operator's
        stated intent is never silently discarded in favor of structural
        candidates."""

        monkeypatch.setenv("VESMA_MESH_GEN_DIR", str(tmp_path / "nope"))
        with pytest.raises(ImportError, match="VESMA_MESH_GEN_DIR"):
            _exec_shim_copy(_real_shim_source(), tmp_path / "elsewhere", "mesh_gen_env_miss")

    def test_empty_env_override_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An UNSET/empty override leaves the structural candidates in
        charge: a fake source-checkout tree with a real candidate (a) dir
        resolves to it."""

        monkeypatch.delenv("VESMA_MESH_GEN_DIR", raising=False)
        gen_dir = tmp_path / "federation" / "gen" / "python"
        gen_dir.mkdir(parents=True)
        module = _exec_shim_copy(_real_shim_source(), tmp_path / "src", "mesh_gen_env_empty")
        assert gen_dir == module._GEN_DIR


class TestMissIsLoud:
    def test_no_candidate_import_error_enumerates_probed_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No structural candidate and no override -> ImportError listing
        every probed path AND the generation command (issue #514: the
        single-candidate resolution failed silently or confusingly)."""

        monkeypatch.delenv("VESMA_MESH_GEN_DIR", raising=False)
        empty_root = tmp_path / "empty"
        empty_root.mkdir()
        with pytest.raises(ImportError) as excinfo:
            _exec_shim_copy(_real_shim_source(), empty_root / "site-packages", "mesh_gen_miss")
        message = str(excinfo.value)
        assert "gen-proto.sh" in message
        assert "VESMA_MESH_GEN_DIR" in message
        # both structural candidates are enumerated — probed, not guessed
        here = Path(str(empty_root / "site-packages" / "vesma" / "_mesh_gen.py")).resolve()
        assert str(here.parents[2] / "federation" / "gen" / "python") in message
        assert str(here.parents[1] / "federation" / "gen" / "python") in message
