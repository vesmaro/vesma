"""Fail-closed env_file loader tests (CM §3.5 / ENV_FILE_UNSAFE, LY-03).

Non-root limitation, documented: the owner-mismatch branch (file owned by
another uid) can only be exercised as root — changing a file's owner
needs CAP_CHOWN. Under the non-root test user that case is SKIPPED with
an explicit reason; under a root test environment it runs for real.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vesmaro.service.envfile import env_mapping_is_secret_free, load_env_file
from vesmaro.service.errors import ENV_FILE_UNSAFE, ManifestError


def _write_env(path: Path, body: str = "ALPHA=one\n# comment\nBETA=two=2\n") -> Path:
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


class TestHappyPath:
    def test_parses_key_value_lines(self, tmp_path: Path) -> None:
        env_file = _write_env(tmp_path / "comp.env")
        values = load_env_file(env_file)
        assert values == {"ALPHA": "one", "BETA": "two=2"}

    def test_load_with_placement_check_outside_dir(self, tmp_path: Path) -> None:
        env_file = _write_env(tmp_path / "comp.env")
        manifests = tmp_path / "components.d"
        manifests.mkdir()
        values = load_env_file(env_file, manifests_dir=manifests)
        assert values["ALPHA"] == "one"

    def test_empty_file_is_valid(self, tmp_path: Path) -> None:
        env_file = _write_env(tmp_path / "comp.env", body="# only a comment\n\n")
        assert load_env_file(env_file) == {}

    def test_secret_free_helper(self) -> None:
        assert env_mapping_is_secret_free({"ALPHA": "x"})
        assert not env_mapping_is_secret_free({"API_TOKEN": "x"})


class TestFailClosed:
    def test_world_readable_file_refused_with_chmod_hint(self, tmp_path: Path) -> None:
        env_file = _write_env(tmp_path / "comp.env")
        os.chmod(env_file, 0o644)
        with pytest.raises(ManifestError) as exc:
            load_env_file(env_file)
        assert exc.value.code == ENV_FILE_UNSAFE
        assert exc.value.fix_hint is not None
        assert exc.value.fix_hint.startswith("chmod 600 ")
        assert str(env_file) in exc.value.fix_hint

    def test_group_readable_file_refused(self, tmp_path: Path) -> None:
        env_file = _write_env(tmp_path / "comp.env")
        os.chmod(env_file, 0o640)
        with pytest.raises(ManifestError) as exc:
            load_env_file(env_file)
        assert exc.value.code == ENV_FILE_UNSAFE

    def test_missing_file_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestError) as exc:
            load_env_file(tmp_path / "absent.env")
        assert exc.value.code == ENV_FILE_UNSAFE
        assert "missing" in exc.value.message

    def test_env_file_inside_manifests_dir_refused(self, tmp_path: Path) -> None:
        env_file = _write_env(tmp_path / "comp.env")
        with pytest.raises(ManifestError) as exc:
            load_env_file(env_file, manifests_dir=tmp_path)
        assert exc.value.code == ENV_FILE_UNSAFE
        assert exc.value.fix_hint is not None

    def test_malformed_line_refused(self, tmp_path: Path) -> None:
        env_file = _write_env(tmp_path / "comp.env", body="GOOD=1\nno_equals_sign\n")
        with pytest.raises(ManifestError) as exc:
            load_env_file(env_file)
        assert exc.value.code == ENV_FILE_UNSAFE
        assert "line 2" in exc.value.message

    def test_invalid_variable_name_refused(self, tmp_path: Path) -> None:
        env_file = _write_env(tmp_path / "comp.env", body="1BAD=1\n")
        with pytest.raises(ManifestError) as exc:
            load_env_file(env_file)
        assert exc.value.code == ENV_FILE_UNSAFE

    def test_directory_instead_of_file_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "envdir"
        target.mkdir()
        os.chmod(target, 0o600)
        with pytest.raises(ManifestError) as exc:
            load_env_file(target)
        assert exc.value.code == ENV_FILE_UNSAFE

    @pytest.mark.skipif(
        os.geteuid() != 0,
        reason="owner mismatch needs CAP_CHOWN — only testable as root "
        "(documented limitation of the non-root test user)",
    )
    def test_foreign_owner_refused_with_chown_hint(self, tmp_path: Path) -> None:
        env_file = _write_env(tmp_path / "comp.env")
        os.chown(env_file, 65534, 65534)  # nobody:nogroup
        try:
            with pytest.raises(ManifestError) as exc:
                load_env_file(env_file)
            assert exc.value.code == ENV_FILE_UNSAFE
            assert exc.value.fix_hint is not None
            assert exc.value.fix_hint.startswith("chown ")
        finally:
            os.chown(env_file, os.geteuid(), os.getgid())
