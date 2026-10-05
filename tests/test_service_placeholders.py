"""argv placeholder expansion tests (CM §2 allowlist, layout §3.4)."""

from __future__ import annotations

from typing import Any

import pytest

from vesmaro.service.errors import PLACEHOLDER_UNKNOWN, ManifestError
from vesmaro.service.placeholders import expand


def _full_mapping() -> dict[str, str]:
    return {
        "config_path": "/home/u/.config/vesma/vesma.yaml",
        "data_dir": "/home/u/.local/share/vesma/comp",
        "runtime_dir": "/run/user/1000/vesma",
        "venv_bin": "/home/u/.local/share/vesma/venvs/comp/bin",
    }


class TestExpansion:
    def test_all_four_tokens_expand(self) -> None:
        argv = [
            "{venv_bin}/python",
            "-m",
            "worker",
            "--config",
            "{config_path}",
            "--db",
            "{data_dir}/db.sqlite",
        ]
        result = expand(argv, _full_mapping())
        assert result[0] == _full_mapping()["venv_bin"] + "/python"
        assert result[4] == _full_mapping()["config_path"]
        assert result[6] == _full_mapping()["data_dir"] + "/db.sqlite"

    def test_runtime_dir_token_expands(self) -> None:
        result = expand(["--sock", "{runtime_dir}/control.sock"], _full_mapping())
        assert result[1] == _full_mapping()["runtime_dir"] + "/control.sock"

    def test_elements_without_placeholders_pass_through(self) -> None:
        # {}, {a and {ENV_VAR} are literal braces per the runner vocabulary
        # (only well-formed {[a-z_]+} tokens are placeholders).
        argv = ["-m", "worker", "--flag", "{}", "{a", "{ENV_VAR}"]
        assert expand(argv, _full_mapping()) == argv

    def test_lowercase_token_mid_string_is_flagged(self) -> None:
        with pytest.raises(ManifestError) as exc:
            expand(["x{y}z"], _full_mapping())
        assert exc.value.code == PLACEHOLDER_UNKNOWN

    def test_repeated_token_expands_everywhere(self) -> None:
        result = expand(["{data_dir}", "{data_dir}"], _full_mapping())
        assert result == [_full_mapping()["data_dir"]] * 2

    def test_original_argv_not_mutated(self) -> None:
        argv = ["{data_dir}/x"]
        _ = expand(argv, _full_mapping())
        assert argv == ["{data_dir}/x"]


class TestRefusals:
    def test_unknown_placeholder_refused_with_allowlist(self) -> None:
        with pytest.raises(ManifestError) as exc:
            expand(["{home_dir}/x"], _full_mapping())
        assert exc.value.code == PLACEHOLDER_UNKNOWN
        assert "home_dir" in exc.value.message
        assert "venv_bin" in exc.value.message  # the allowlist is shown

    def test_venv_bin_without_venv_refused_fail_closed(self) -> None:
        mapping: dict[str, Any] = {
            "config_path": "/cfg",
            "data_dir": "/data",
            "runtime_dir": "/run",
            # venv_bin deliberately absent: component has no venv
        }
        with pytest.raises(ManifestError) as exc:
            expand(["{venv_bin}/python", "-m", "worker"], mapping)
        assert exc.value.code == PLACEHOLDER_UNKNOWN
        assert "no venv" in exc.value.message

    def test_missing_mapping_entry_refused(self) -> None:
        with pytest.raises(ManifestError) as exc:
            expand(["{data_dir}/x"], {"config_path": "/cfg"})
        assert exc.value.code == PLACEHOLDER_UNKNOWN

    def test_uppercase_token_is_not_a_placeholder(self) -> None:
        # Well-formed placeholders are {[a-z_]+}; {ENV_VAR} is literal braces
        # per the runner's vocabulary — it is NOT expanded and NOT flagged.
        argv = ["{ENV_VAR}"]
        assert expand(argv, _full_mapping()) == argv
