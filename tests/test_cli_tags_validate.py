"""`vesma tags validate` — real store scan (cli-audit 2026-10-08, finding #6).

The command was a stub that printed "Full vault scan not yet implemented"
and exited 0 — a silent success on a contract-violating store. Now it runs
the same conformance scan as `vesma tags audit` over the live store,
lists every non-conformant entry (id / tags / missing prefixes) and
exits 1 when violations exist, 0 on a clean store.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

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
        f"  db_name: cli-tags-validate.db\n"
        f"embedding:\n"
        f"  provider: nano\n"
    )
    monkeypatch.setenv("VESMA_CONFIG", str(cfg))
    yield cfg
    reset_manager()


def _seed(isolated_config: Path, content: str, tags: list[str], project: str = "proj") -> None:
    from vesma.cli._manager import get_manager
    from vesma.models import Memory, MemorySource, MemoryStatus, MemoryType

    mgr = get_manager(str(isolated_config))
    mgr.sqlite.save(
        Memory(
            content=content,
            title=content,
            tags=tags,
            source=MemorySource.CLI,
            memory_type=MemoryType.NOTE,
            status=MemoryStatus.RAW,
            project=project,
            agent="cli",
        )
    )


def test_clean_store_passes_with_exit_zero(isolated_config: Path) -> None:
    """A conformant store exits 0 and reports the scan."""
    _seed(isolated_config, "conformant entry", ["project:p", "agent:a", "vesma:note"])
    result = runner.invoke(app, ["tags", "validate"])
    assert result.exit_code == 0, result.output
    assert "non-conformant" in result.output
    assert "1" in result.output  # 1 entries scanned
    assert "Traceback" not in result.output


def test_violations_listed_with_exit_one(isolated_config: Path) -> None:
    """A contract-violating store is listed entry-by-entry and exits 1."""
    _seed(isolated_config, "no subtype tag here", ["project:p", "agent:a"])
    result = runner.invoke(app, ["tags", "validate"])
    assert result.exit_code == 1
    assert "non-conformant" in result.output
    assert "vesma:*" in result.output  # the missing prefix is named
    assert "no subtype tag here" in result.output  # the entry is identified
    assert "Traceback" not in result.output


def test_legacy_mnemos_subtype_counts_as_conformant(isolated_config: Path) -> None:
    """Legacy `mnemos:*` subtypes satisfy the contract (same as tags audit)."""
    _seed(isolated_config, "legacy entry", ["project:p", "agent:a", "mnemos:test"])
    result = runner.invoke(app, ["tags", "validate"])
    assert result.exit_code == 0, result.output


def test_positional_vault_accepted_but_not_required(isolated_config: Path) -> None:
    """The old positional vault is optional; passing it works with a note."""
    _seed(isolated_config, "conformant entry", ["project:p", "agent:a", "vesma:note"])
    result = runner.invoke(app, ["tags", "validate", "/some/vault"])
    assert result.exit_code == 0, result.output
    assert "not used" in result.output
