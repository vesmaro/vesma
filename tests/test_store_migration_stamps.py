"""ADR-0049 wave 0: the mover's schema-stamp parity/band gate + the
moved-target generation stamping.

Pins: mixed-generation homes refuse (dry-run included, exit 19), a
band-outside stamp refuses, an all-zero legacy home passes, and a
successful apply stamps the TARGET family onto the current generation
while the SOURCE stays at its own stamps (C6: the source is never
written by the mover).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.test_store_migration import (
    _DB_SCHEMA,
    _insert_records,
    _output,
    _target_of,
    app,
    runner,
)
from vesma.schema_stamp import SCHEMA_STAMP_VERSION, read_schema_stamp


@pytest.fixture
def store_home(tmp_path: Path) -> Path:
    """A minimal fabricated 5.x home for the stamp gate: the main db plus
    ONE sibling sqlite — enough for parity, band and transit stamping."""
    home = tmp_path / "old-home"
    data = home / "data"
    data.mkdir(parents=True)
    _insert_records(data / "mnemos.db")

    vectors = sqlite3.connect(str(data / "vectors.db"))
    try:
        vectors.executescript(_DB_SCHEMA)  # reuse the real schema (parity sweep reads it)
        vectors.execute("CREATE TABLE vec (id TEXT PRIMARY KEY, blob BLOB)")
        vectors.execute("INSERT INTO vec VALUES ('id-01', x'00ff')")
        vectors.commit()
    finally:
        vectors.close()
    return home


def _set_stamp(db_path: Path, stamp: int) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f"PRAGMA user_version = {stamp:d}")
        conn.commit()
    finally:
        conn.close()


def _plan_json(home: Path, target: Path) -> dict[str, Any]:
    result = runner.invoke(
        app,
        ["migrate-store", "--from", str(home), "--to", str(target), "--json"],
    )
    assert result.exit_code == 0, _output(result)
    return dict(json.loads(result.output))


def test_mixed_generation_home_refuses_at_plan(store_home: Path, tmp_path: Path) -> None:
    """A home assembled from parts of different generations never
    translates as one store — the dry-run refuses, exit 19."""
    _set_stamp(store_home / "data" / "vectors.db", SCHEMA_STAMP_VERSION)
    result = runner.invoke(
        app,
        ["migrate-store", "--from", str(store_home), "--to", str(_target_of(store_home, tmp_path))],
    )
    assert result.exit_code == 19
    out = _output(result)
    assert "mixed schema generations" in out
    assert "vectors.db" in out


def test_band_outside_stamp_refuses(store_home: Path, tmp_path: Path) -> None:
    """A stamp the supported band does not cover (a NEWER vesma's store)
    refuses with the band named."""
    _set_stamp(store_home / "data" / "mnemos.db", SCHEMA_STAMP_VERSION + 1)
    result = runner.invoke(
        app,
        ["migrate-store", "--from", str(store_home), "--to", str(_target_of(store_home, tmp_path))],
    )
    assert result.exit_code == 19
    assert "outside the supported band" in _output(result)


def test_unreadable_family_member_refuses(store_home: Path, tmp_path: Path) -> None:
    """A corrupt sqlite family member refuses the stamp sweep (typed, not
    a raw traceback)."""
    (store_home / "data" / "vectors.db").write_bytes(
        b"SQLite format 3\x00" + b"\x00" * 64
    )  # header admits it as sqlite, the read fails
    result = runner.invoke(
        app,
        ["migrate-store", "--from", str(store_home), "--to", str(_target_of(store_home, tmp_path))],
    )
    assert result.exit_code == 19
    assert "unreadable" in _output(result)


def test_all_zero_legacy_home_passes_plan(store_home: Path, tmp_path: Path) -> None:
    """The pre-stamp era is a COHERENT generation: an all-zero home plans
    cleanly (the mover must not write the source, C6)."""
    payload = _plan_json(store_home, _target_of(store_home, tmp_path))
    assert payload["mode"] == "dry-run"


def test_apply_stamps_target_but_not_source(store_home: Path, tmp_path: Path) -> None:
    """A legacy home advances through the TRANSIT: the moved target comes
    up stamped onto the current generation; the source keeps its stamps."""
    target = _target_of(store_home, tmp_path)
    assert read_schema_stamp(store_home / "data" / "mnemos.db") == 0
    result = runner.invoke(
        app,
        ["migrate-store", "--from", str(store_home), "--to", str(target), "--apply"],
    )
    assert result.exit_code == 0, _output(result)
    assert "joined the current generation" in _output(result)
    for db in ("vesma.db", "vectors.db"):
        stamp = read_schema_stamp(target / "data" / db)
        assert stamp == SCHEMA_STAMP_VERSION, db
    # C6: the source was not stamped by the mover.
    assert read_schema_stamp(store_home / "data" / "mnemos.db") == 0
