"""ADR-0019 Phase D — ``mnemos doctor`` pending-refinement diagnostics.

The B1 migration backfilled bypass-era PUBLISHED rows as
``pipeline_state='pending'``, and the B2a engine only drains them when
the background processor runs. A CLI-only deployment (no daemon) can
accumulate a queue that never drains — the doctor makes it visible
(WARN + the ``vesma processor start`` recommendation). Diagnostics
ONLY: the doctor must never start the processor itself.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vesma.cli.doctor import CheckStatus, _check_pending_refine, doctor_app
from vesma.config import Settings

runner = CliRunner()


def _settings(tmp: Path) -> Settings:
    settings = Settings(
        vesma={
            "vault_path": str(tmp / "vault"),
            "data_dir": str(tmp / "data"),
            "db_name": "test.db",
        }
    )
    settings.resolve_paths()
    return settings


def _db_with_pending(settings: Settings, pending: int) -> None:
    """Create the memories table directly with ``pending`` pending rows."""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(settings.db_path))
    try:
        conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, pipeline_state TEXT)")
        for i in range(pending):
            conn.execute(
                "INSERT INTO memories (id, pipeline_state) VALUES (?, ?)",
                (f"id-{i}", "pending" if i < pending else "refined"),
            )
        conn.commit()
    finally:
        conn.close()


# ── The check itself ──────────────────────────────────────────────────────────


def test_missing_db_is_pass(tmp_path: Path) -> None:
    result = _check_pending_refine(_settings(tmp_path))
    assert result.status == CheckStatus.PASS
    assert "no database" in result.detail


def test_zero_pending_is_pass(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _db_with_pending(settings, pending=0)
    result = _check_pending_refine(settings)
    assert result.status == CheckStatus.PASS
    assert "0 entries" in result.detail


@pytest.mark.parametrize("pending", [1, 3])
def test_pending_rows_warn_with_processor_recommendation(tmp_path: Path, pending: int) -> None:
    settings = _settings(tmp_path)
    _db_with_pending(settings, pending=pending)
    result = _check_pending_refine(settings)
    assert result.status == CheckStatus.WARN
    assert f"{pending} " in result.detail
    assert "pipeline_state=pending" in result.detail
    assert "vesma processor start" in result.detail, "the recommendation names the fix"


def test_pre_adr19_schema_is_pass(tmp_path: Path) -> None:
    """A DB without the pipeline_state column has no queue by definition."""
    settings = _settings(tmp_path)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(settings.db_path))
    try:
        conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    result = _check_pending_refine(settings)
    assert result.status == CheckStatus.PASS
    assert "pre-ADR-0019" in result.detail


# ── Wired into the doctor command ─────────────────────────────────────────────


def test_doctor_json_includes_pending_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check runs as part of ``mnemos doctor`` and reaches the JSON
    output (the scripting surface) with the WARN verdict."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / ".vesma" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        f"vesma:\n"
        f"  vault_path: {tmp_path / '.vesma' / 'vault'}\n"
        f"  data_dir: {tmp_path / '.vesma' / 'data'}\n"
    )
    settings = Settings()
    settings.resolve_paths()
    _db_with_pending(settings, pending=2)

    result = runner.invoke(doctor_app, ["--json"])

    # The overall exit code aggregates every check (0/1/2 depending on
    # the environment — see test_doctor_paths.py); only OUR check's
    # verdict is asserted here.
    payload = json.loads(result.output)
    checks = {c["name"]: c for c in payload["checks"]}
    assert "Pending refine" in checks
    assert checks["Pending refine"]["status"] == "warn"
    assert "vesma processor start" in checks["Pending refine"]["detail"]
