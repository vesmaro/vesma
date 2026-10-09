"""`vesma import` argument-order regression (cli-audit 2026-10-08, finding #4).

The command used to be a Typer GROUP: click's MultiCommand parses options
only BEFORE the first positional argument, so any flag placed after the
source path (`import f.json --mode merge`) died with exit 2 "Missing
argument 'source'" while the flag-first order worked. Registered as a
plain command now — both orders MUST parse, and the usage line must not
carry the phantom ``{source} COMMAND [ARGS]...`` suffix.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from vesma.cli.import_ import ImportResult
from vesma.cli.main import app

runner = CliRunner()


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point VESMA_CONFIG at an empty YAML so the CLI uses tmp_path."""
    from vesma.cli._manager import reset_manager

    reset_manager()
    cfg = tmp_path / "vesma.yaml"
    cfg.write_text(
        f"vesma:\n"
        f"  vault_path: {tmp_path / 'vault'}\n"
        f"  data_dir: {tmp_path / 'data'}\n"
        f"  db_name: cli-import-args.db\n"
        f"embedding:\n"
        f"  provider: nano\n"
    )
    monkeypatch.setenv("VESMA_CONFIG", str(cfg))
    yield cfg
    reset_manager()


def _fake_result(mode: str = "merge") -> ImportResult:
    return ImportResult(mode=mode, dry_run=False, imported=3, skipped=0, updated=0)


def test_flags_after_positional_path_parse(isolated_config: Path) -> None:
    """`import f.json --mode merge` (flags AFTER the path) parses and runs."""
    export = isolated_config.parent / "export.json"
    export.write_text("{}", encoding="utf-8")
    with patch("vesma.cli.import_cmd.run_import", return_value=_fake_result()) as run:
        result = runner.invoke(app, ["import", str(export), "--mode", "merge"])
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert run.call_count == 1


def test_flags_before_positional_path_still_parse(isolated_config: Path) -> None:
    """Backward compat: the historical flag-first order keeps working."""
    export = isolated_config.parent / "export.json"
    export.write_text("{}", encoding="utf-8")
    with patch("vesma.cli.import_cmd.run_import", return_value=_fake_result()) as run:
        result = runner.invoke(app, ["import", "--mode", "merge", str(export)])
    assert result.exit_code == 0, result.output
    assert run.call_count == 1


def test_usage_line_has_no_phantom_command_suffix() -> None:
    """`import --help` shows `{SOURCE} [OPTIONS]`-style usage, not `COMMAND [ARGS]...`."""
    result = runner.invoke(app, ["import", "--help"])
    assert result.exit_code == 0, result.output
    assert "COMMAND [ARGS]" not in result.output
    assert "SOURCE" in result.output.upper()


def test_missing_argument_is_clean_usage_error() -> None:
    """Bare `import` → clean usage error (exit 2), no traceback."""
    result = runner.invoke(app, ["import"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output
