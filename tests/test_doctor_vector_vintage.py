"""ADR-0021 round-3 swap — ``mnemos doctor`` vector vintage diagnostics.

After an embedder weights swap every pre-swap vector is cut in another
geometry; a mixed-space index silently degrades vector search. The
doctor's Vector store check counts rows whose metadata
``model_fingerprint`` is missing (pre-swap vintage) or differs from the
configured embedder, and points at the two remediation paths (the
background heal sweeper / ``vesma reindex``). Diagnostics ONLY: the
doctor must never re-embed itself.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vesma.cli.doctor import CheckStatus, _check_vector_store, doctor_app
from vesma.config import Settings
from vesma.embeddings import config_fingerprint

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


def _vectors_db(settings: Settings, metas: list[str]) -> None:
    """Create vectors.db directly with one embeddings row per metadata JSON."""
    data_dir = Path(settings.vesma.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(data_dir / "vectors.db"))
    try:
        conn.execute("CREATE TABLE embeddings (id TEXT PRIMARY KEY, vector BLOB, metadata TEXT)")
        for i, meta in enumerate(metas):
            conn.execute(
                "INSERT INTO embeddings (id, vector, metadata) VALUES (?, ?, ?)",
                (f"id-{i}", b"\x00" * 8, meta),
            )
        conn.commit()
    finally:
        conn.close()


def _meta(fingerprint: str | None) -> str:
    payload = {"project": "p", "agent": "a", "content_hash": "x"}
    if fingerprint is not None:
        payload["model_fingerprint"] = fingerprint
    return json.dumps(payload)


# ── The check itself ──────────────────────────────────────────────────────────


def test_missing_store_is_warn(tmp_path: Path) -> None:
    result = _check_vector_store(_settings(tmp_path))
    assert result.status == CheckStatus.WARN
    assert "missing" in result.detail


def test_current_vintage_is_pass(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    current = config_fingerprint(settings.embedding)
    _vectors_db(settings, [_meta(current), _meta(current)])
    result = _check_vector_store(settings)
    assert result.status == CheckStatus.PASS
    assert "vintage current" in result.detail


def test_vintage_mismatch_warns_with_count(tmp_path: Path) -> None:
    """Pre-swap rows (old/missing fingerprint) are counted and named."""
    settings = _settings(tmp_path)
    current = config_fingerprint(settings.embedding)
    _vectors_db(settings, [_meta(current), _meta("nano:sha256:oldweights"), _meta(None)])
    result = _check_vector_store(settings)
    assert result.status == CheckStatus.WARN
    assert "2 " in result.detail, "one mismatched + one pre-fingerprint row"
    assert "another embedder" in result.detail
    assert "vesma reindex" in result.detail, "the recommendation names the fix"


def test_corrupt_metadata_counts_as_stale(tmp_path: Path) -> None:
    """A row whose metadata cannot be parsed attests no vintage — stale."""
    settings = _settings(tmp_path)
    _vectors_db(settings, ["not-json"])
    result = _check_vector_store(settings)
    assert result.status == CheckStatus.WARN
    assert "1 " in result.detail


# ── Wired into the doctor command ─────────────────────────────────────────────


def test_doctor_json_includes_vintage_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The vintage verdict reaches the scripting surface (``--json``)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / ".mnemos" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        f"vesma:\n"
        f"  vault_path: {tmp_path / '.mnemos' / 'vault'}\n"
        f"  data_dir: {tmp_path / '.mnemos' / 'data'}\n"
    )
    settings = Settings()
    settings.resolve_paths()
    _vectors_db(settings, [_meta("nano:sha256:pre-swap")])

    result = runner.invoke(doctor_app, ["--json"])

    payload = json.loads(result.output)
    checks = {c["name"]: c for c in payload["checks"]}
    assert checks["Vector store"]["status"] == "warn"
    assert "another embedder" in checks["Vector store"]["detail"]
