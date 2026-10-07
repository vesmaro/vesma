"""B3 — tests for the ``vesma migrate-store`` mover (5.x → 6.0).

The fabricated store mirrors the live 5.x layout: ``<home>/data/mnemos.db``
with the real schema (``_DB_SCHEMA``), mixed statuses, ``project:mnemos`` /
``project:vesma`` / ``project:vesma-cortex`` silos and ``mnemos:*`` tags —
including the byte-stable ``mnemos:no-federate`` trust marker.

The env-gated drill test (``VESMA_B3_DRILL_FROM`` / ``VESMA_B3_DRILL_TO``)
runs the full cycle against operator-provided CLONE paths and skips by
default — never point it at the live store.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from vesma.cli.main import app
from vesma.storage.sqlite_store import _DB_SCHEMA
from vesma.store_migration import reslag_project_slug, reslag_tags_json

runner = CliRunner()


def _output(result: Any) -> str:
    """stdout + stderr of a CliRunner invocation (streams split on click 8.2+)."""
    try:
        return result.output + (result.stderr or "")
    except ValueError:  # pragma: no cover - click <8.2 mixes the streams
        return result.output


# ── Fabricated store ──────────────────────────────────────────────────────────

# (id, status, project, tags) — expectations computed in test constants below.
_RECORDS: list[tuple[str, str, str, list[str]]] = [
    ("id-01", "published", "mnemos", ["project:mnemos", "agent:user", "mnemos:learning"]),
    ("id-02", "processed", "mnemos", ["project:mnemos", "agent:user", "mnemos:checkpoint"]),
    ("id-03", "raw", "vesma-cortex", ["project:vesma-cortex", "agent:user", "mnemos:decision"]),
    ("id-04", "archived", "vesma", ["project:vesma", "agent:user", "vesma:learning"]),
    (
        "id-05",
        "published",
        "mnemos",
        [
            "project:mnemos",
            "agent:user",
            "mnemos:no-federate",
            "mnemos:learning",
        ],
    ),
    (
        "id-06",
        "processing",
        "mnemos",
        [
            "project:mnemos",
            "project:vesma",
            "agent:user",
            "mnemos:note",
        ],
    ),
    ("id-07", "published", "mnemos", ["project:mnemos", "agent:user", "custom-tag"]),
    ("id-08", "published", "mnemos", ["project:mnemos", "agent:user", "mnemos:learning"]),
    ("id-09", "processed", "vesma-cortex", ["project:vesma-cortex", "agent:user", "mnemos:fact"]),
    ("id-10", "archived", "vesma", ["project:vesma", "agent:user", "mnemos:snippet"]),
    (
        "id-11",
        "raw",
        "mnemos",
        [
            "project:mnemos",
            "agent:user",
            "mnemos:decision",
            "mnemos:learning",
        ],
    ),
    ("id-12", "processing", "mnemos", ["project:mnemos", "agent:user"]),
]

_EXPECTED_TOTAL = len(_RECORDS)
_EXPECTED_SUBTYPE_SWAPS = 10
_EXPECTED_PROJECT_SWAPS = 8  # project column exact matches (mnemos → vesma)
_EXPECTED_DUPES = 1  # id-06 project:vesma duplicate after rewrite
_EXPECTED_TRUST_MARKERS = 1  # id-05 mnemos:no-federate
_EXPECTED_STATUS = {
    "published": 4,
    "processed": 2,
    "raw": 2,
    "archived": 2,
    "processing": 2,
}
_EXPECTED_PROJECT_TARGET = {"vesma": 10, "vesma-cortex": 2}


def _insert_records(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_DB_SCHEMA)
        for memory_id, status, project, tags in _RECORDS:
            conn.execute(
                """INSERT INTO memories
                   (id, content, title, tags, source, memory_type, created_at,
                    updated_at, metadata, project, agent, status, marker_version)
                   VALUES (?, ?, ?, ?, 'mcp', 'note', '2026-10-07T00:00:00Z',
                           '2026-10-07T00:00:00Z', '{}', ?, 'user', ?, 1)""",
                (
                    memory_id,
                    f"content-for-{memory_id} — юникод и «кавычки»",
                    f"title-{memory_id}",
                    json.dumps(tags),
                    project,
                    status,
                ),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def store_home(tmp_path: Path) -> Path:
    """A fabricated 5.x store home: data/mnemos.db + siblings + vault."""
    home = tmp_path / "old-home"
    data = home / "data"
    data.mkdir(parents=True)
    _insert_records(data / "mnemos.db")

    vectors = sqlite3.connect(str(data / "vectors.db"))
    try:
        vectors.execute("CREATE TABLE vec (id TEXT PRIMARY KEY, blob BLOB)")
        vectors.execute("INSERT INTO vec VALUES ('id-01', x'00ff')")
        vectors.commit()
    finally:
        vectors.close()

    metrics = sqlite3.connect(str(data / "metrics.sqlite"))
    try:
        metrics.execute("CREATE TABLE m (k TEXT, v INTEGER)")
        metrics.commit()
    finally:
        metrics.close()

    notes = data / "notes"
    notes.mkdir()
    (notes / "note1.txt").write_text("note body", encoding="utf-8")

    vault = home / "vault"
    (vault / "sub").mkdir(parents=True)
    (vault / "a.txt").write_text("vault-a", encoding="utf-8")
    (vault / "sub" / "b.txt").write_text("vault-b", encoding="utf-8")
    return home


def _target_of(store_home: Path, tmp_path: Path) -> Path:
    return tmp_path / "new-home"


def _db(target_home: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(target_home / "data" / "vesma.db"))
    conn.row_factory = sqlite3.Row
    return conn


def _tags_of(conn: sqlite3.Connection, memory_id: str) -> list[str]:
    row = conn.execute("SELECT tags FROM memories WHERE id = ?", (memory_id,)).fetchone()
    return json.loads(str(row["tags"]))


# ── Re-slag primitives (unit) ─────────────────────────────────────────────────


def test_reslag_tags_basic_counts_and_marker() -> None:
    raw = json.dumps(["project:mnemos", "agent:user", "mnemos:learning", "mnemos:no-federate"])
    new, sub, proj, dup, marker = reslag_tags_json(raw)
    assert json.loads(new) == [
        "project:vesma",
        "agent:user",
        "vesma:learning",
        "mnemos:no-federate",
    ]
    assert (sub, proj, dup, marker) == (1, 1, 0, 1)


def test_reslag_tags_keeps_everything_else_byte_identical() -> None:
    raw = json.dumps(["project:mnemosis", "mnemosy", "project:vesma-cortex", "☀ tagged"])
    new, sub, proj, _dup, _marker = reslag_tags_json(raw)
    # Prefix rewrites are EXACT: mnemosy / project:mnemosis stay untouched.
    assert json.loads(new) == ["project:mnemosis", "mnemosy", "project:vesma-cortex", "☀ tagged"]
    assert (sub, proj) == (0, 0)


def test_reslag_tags_deduplicates_after_rewrite() -> None:
    new, sub, proj, dup, _marker = reslag_tags_json(
        json.dumps(["project:mnemos", "project:vesma", "mnemos:note", "vesma:note"])
    )
    assert json.loads(new) == ["project:vesma", "vesma:note"]
    assert (sub, proj, dup) == (1, 1, 2)


def test_reslag_project_slug_exact_only() -> None:
    assert reslag_project_slug("mnemos") == ("vesma", True)
    assert reslag_project_slug("mnemos-cortex") == ("mnemos-cortex", False)
    assert reslag_project_slug("vesma") == ("vesma", False)


# ── CLI: dry-run contract ─────────────────────────────────────────────────────


def test_dry_run_is_the_default_and_writes_nothing(store_home: Path, tmp_path: Path) -> None:
    target = _target_of(store_home, tmp_path)
    db_bytes_before = (store_home / "data" / "mnemos.db").read_bytes()

    result = runner.invoke(app, ["migrate-store", "--from", str(store_home), "--to", str(target)])
    assert result.exit_code == 0, result.output
    assert not target.exists()
    assert not list(target.parent.glob("migrate-snapshot-*"))
    assert (store_home / "data" / "mnemos.db").read_bytes() == db_bytes_before
    assert "dry-run" in result.output
    assert "mnemos:<subtype> -> vesma:<subtype>:  10" in result.output


def test_dry_run_json_reports_counts_only(store_home: Path, tmp_path: Path) -> None:
    target = _target_of(store_home, tmp_path)
    result = runner.invoke(
        app,
        ["migrate-store", "--from", str(store_home), "--to", str(target), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "dry-run"
    assert payload["records"] == _EXPECTED_TOTAL
    assert payload["tag_subtypes_to_rewrite"] == _EXPECTED_SUBTYPE_SWAPS
    assert payload["trust_markers_kept"] == _EXPECTED_TRUST_MARKERS
    assert payload["status_breakdown"] == _EXPECTED_STATUS
    assert payload["project_breakdown"] == {
        "mnemos": 8,
        "vesma": 2,
        "vesma-cortex": 2,
    }


# ── CLI: apply end-to-end ─────────────────────────────────────────────────────


def test_apply_produces_verified_6_0_store(store_home: Path, tmp_path: Path) -> None:
    target = _target_of(store_home, tmp_path)
    result = runner.invoke(
        app,
        ["migrate-store", "--from", str(store_home), "--to", str(target), "--apply"],
    )
    assert result.exit_code == 0, result.output

    # Layout: vesma.db, no mnemos.db; snapshot + no staging leftovers.
    assert (target / "data" / "vesma.db").is_file()
    assert not (target / "data" / "mnemos.db").exists()
    snapshots = list(target.parent.glob("migrate-snapshot-*"))
    assert len(snapshots) == 1
    assert (snapshots[0] / "data" / "mnemos.db").is_file()  # source naming kept
    assert not list(target.parent.glob("*.staging-*"))

    # Vault and data side files carried byte-for-byte.
    assert (target / "vault" / "a.txt").read_text(encoding="utf-8") == "vault-a"
    assert (target / "vault" / "sub" / "b.txt").read_text(encoding="utf-8") == "vault-b"
    assert (target / "data" / "notes" / "note1.txt").read_text(encoding="utf-8") == "note body"

    conn = _db(target)
    try:
        assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == _EXPECTED_TOTAL
        # Re-slag: exact expectations per fabricated record.
        assert _tags_of(conn, "id-01") == ["project:vesma", "agent:user", "vesma:learning"]
        assert _tags_of(conn, "id-04") == ["project:vesma", "agent:user", "vesma:learning"]
        assert _tags_of(conn, "id-05") == [
            "project:vesma",
            "agent:user",
            "mnemos:no-federate",  # trust marker byte-stable
            "vesma:learning",
        ]
        assert _tags_of(conn, "id-06") == ["project:vesma", "agent:user", "vesma:note"]
        assert _tags_of(conn, "id-07") == ["project:vesma", "agent:user", "custom-tag"]
        # No vesma:no-federate anywhere (the marker is never invented).
        rows = conn.execute("SELECT tags FROM memories").fetchall()
        for row in rows:
            assert "vesma:no-federate" not in json.loads(str(row["tags"]))
        # Project column denormalization rewritten.
        assert (
            conn.execute("SELECT count(*) FROM memories WHERE project = 'mnemos'").fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT count(*) FROM memories WHERE project = 'vesma'").fetchone()[0]
            == 10
        )  # 8 re-slagged from mnemos + 2 pre-existing
        # FTS mirror carries the rewritten tags (rebuild ran).
        assert (
            conn.execute("SELECT count(*) FROM memories_fts WHERE tags MATCH 'vesma'").fetchone()[0]
            >= 10
        )
    finally:
        conn.close()

    # Idempotence marker written; report numbers match the fixture constants.
    marker = json.loads((target / "data" / ".migrate-store-applied.json").read_text())
    assert marker["records"] == _EXPECTED_TOTAL
    assert marker["tag_subtypes_rewritten"] == _EXPECTED_SUBTYPE_SWAPS
    assert marker["trust_markers_kept"] == _EXPECTED_TRUST_MARKERS
    assert "applied and verified" in result.output


def test_apply_source_sidecars_and_sibling_dbs_move(store_home: Path, tmp_path: Path) -> None:
    # A stale WAL sidecar in the SOURCE must not leak into the target as-is:
    # its content rides the backup API. Create one to prove the skip.
    (store_home / "data" / "mnemos.db-wal").write_bytes(b"stale-wal-junk")
    target = _target_of(store_home, tmp_path)
    result = runner.invoke(
        app,
        ["migrate-store", "--from", str(store_home), "--to", str(target), "--apply"],
    )
    assert result.exit_code == 0, result.output
    assert not (target / "data" / "mnemos.db-wal").exists()
    assert (target / "data" / "vectors.db").is_file()
    assert (target / "data" / "metrics.sqlite").is_file()
    conn = sqlite3.connect(str(target / "data" / "vectors.db"))
    try:
        assert conn.execute("SELECT count(*) FROM vec").fetchone()[0] == 1
    finally:
        conn.close()


# ── P1-1 (security review): everything the mover writes stays private ────────


def test_apply_target_and_snapshot_fully_private(store_home: Path, tmp_path: Path) -> None:
    """Pin (CWE-732): no file or directory wider than 0600/0700 survives.

    The mover materializes memories, vault content and the config — all
    secret-bearing surfaces. Walk the FULL target and the rollback snapshot
    and refuse any group/other permission bit.
    """
    import os

    target = _target_of(store_home, tmp_path)
    result = runner.invoke(
        app,
        ["migrate-store", "--from", str(store_home), "--to", str(target), "--apply"],
    )
    assert result.exit_code == 0, result.output
    snapshots = list(target.parent.glob("migrate-snapshot-*"))
    assert len(snapshots) == 1
    checked_files = 0
    for root in (target, snapshots[0]):
        for dirpath, _dirnames, filenames in os.walk(root):
            dir_mode = Path(dirpath).stat().st_mode
            assert dir_mode & 0o077 == 0, f"directory too wide: {dirpath} ({oct(dir_mode)})"
            for name in filenames:
                file_mode = (Path(dirpath) / name).stat().st_mode
                assert file_mode & 0o077 == 0, f"file too wide: {dirpath}/{name} ({oct(file_mode)})"
                checked_files += 1
    assert checked_files >= 7  # 2 dbs + siblings + notes + config + marker + vault files


def test_second_run_on_migrated_store_politely_refuses(store_home: Path, tmp_path: Path) -> None:
    target = _target_of(store_home, tmp_path)
    first = runner.invoke(
        app,
        ["migrate-store", "--from", str(store_home), "--to", str(target), "--apply"],
    )
    assert first.exit_code == 0, first.output
    second = runner.invoke(
        app, ["migrate-store", "--from", str(target), "--to", str(tmp_path / "x")]
    )
    assert second.exit_code == 7
    assert "already carries the vesma:* namespace" in _output(second)


def test_empty_store_moves_copy_only(tmp_path: Path) -> None:
    home = tmp_path / "empty-home"
    (home / "data").mkdir(parents=True)
    conn = sqlite3.connect(str(home / "data" / "mnemos.db"))
    try:
        conn.executescript(_DB_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    target = tmp_path / "empty-target"
    result = runner.invoke(
        app, ["migrate-store", "--from", str(home), "--to", str(target), "--apply"]
    )
    assert result.exit_code == 0, result.output
    assert (target / "data" / "vesma.db").is_file()
    conn = sqlite3.connect(str(target / "data" / "vesma.db"))
    try:
        assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
    finally:
        conn.close()


# ── Quiesce gate ──────────────────────────────────────────────────────────────


def test_quiesce_refuses_when_source_is_locked(store_home: Path, tmp_path: Path) -> None:
    target = _target_of(store_home, tmp_path)
    holder = sqlite3.connect(str(store_home / "data" / "mnemos.db"))
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute(
            "UPDATE memories SET title = 'held' WHERE id = 'id-01'"
        )  # write txn still open — no commit
        result = runner.invoke(
            app, ["migrate-store", "--from", str(store_home), "--to", str(target), "--apply"]
        )
        assert result.exit_code == 4
        assert "not quiescent" in _output(result)
        assert not target.exists()
        assert not list(target.parent.glob("migrate-snapshot-*"))
    finally:
        holder.rollback()
        holder.close()


# ── Discovery and usage refusals ──────────────────────────────────────────────


def _mk_candidate(home: Path, rel: Path) -> Path:
    store = home / rel
    (store / "data").mkdir(parents=True)
    conn = sqlite3.connect(str(store / "data" / "mnemos.db"))
    try:
        conn.executescript(_DB_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return store


def test_discovery_without_from_suggests_and_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    only = _mk_candidate(tmp_path, Path(".mnemos"))
    result = runner.invoke(app, ["migrate-store", "--to", str(tmp_path / "t")])
    assert result.exit_code == 3
    assert str(only) in _output(result)
    assert "--from" in _output(result)
    assert not (tmp_path / "t").exists()


def test_discovery_with_several_candidates_refuses_without_picking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    first = _mk_candidate(tmp_path, Path(".mnemos"))
    second = _mk_candidate(tmp_path, Path(".local/share/vesma/core"))
    result = runner.invoke(app, ["migrate-store", "--to", str(tmp_path / "t")])
    assert result.exit_code == 3
    assert str(first) in _output(result) and str(second) in _output(result)
    assert not (tmp_path / "t").exists()


def test_discovery_without_any_candidate_explains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(app, ["migrate-store", "--to", str(tmp_path / "t")])
    assert result.exit_code == 3
    assert "No known 5.x store home" in _output(result)


def test_existing_empty_target_refused_before_any_work(
    store_home: Path, tmp_path: Path
) -> None:
    """Pin (P2-1): even an EMPTY existing --to is refused at plan time.

    The refusal must happen BEFORE any work: no snapshot, no staging, the
    source untouched — not a mid-run rename failure.
    """
    target = _target_of(store_home, tmp_path)
    target.mkdir()  # exists and is empty — used to slip through to run_migration
    db_bytes_before = (store_home / "data" / "mnemos.db").read_bytes()
    result = runner.invoke(
        app,
        ["migrate-store", "--from", str(store_home), "--to", str(target), "--apply"],
    )
    assert result.exit_code == 2
    assert "choose a fresh target" in _output(result)
    assert "appeared during migration" not in _output(result)
    assert not list(target.parent.glob("migrate-snapshot-*"))
    assert not list(target.parent.glob("*.staging-*"))
    assert (store_home / "data" / "mnemos.db").read_bytes() == db_bytes_before


def test_usage_errors_nested_and_nonempty_target(store_home: Path, tmp_path: Path) -> None:
    same = runner.invoke(
        app,
        ["migrate-store", "--from", str(store_home), "--to", str(store_home)],
    )
    assert same.exit_code == 2

    nested_to = store_home / "inner"
    nested = runner.invoke(
        app, ["migrate-store", "--from", str(store_home), "--to", str(nested_to)]
    )
    assert nested.exit_code == 2

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "junk").write_text("x", encoding="utf-8")
    occupied_run = runner.invoke(
        app, ["migrate-store", "--from", str(store_home), "--to", str(occupied)]
    )
    assert occupied_run.exit_code == 2


def test_invalid_source_home_refused(tmp_path: Path) -> None:
    empty = tmp_path / "not-a-store"
    empty.mkdir()
    result = runner.invoke(
        app, ["migrate-store", "--from", str(empty), "--to", str(tmp_path / "t")]
    )
    assert result.exit_code == 8
    assert "does not contain a mnemos.db" in _output(result)


def test_unparseable_tags_refused_before_any_write(tmp_path: Path) -> None:
    home = tmp_path / "broken-home"
    (home / "data").mkdir(parents=True)
    conn = sqlite3.connect(str(home / "data" / "mnemos.db"))
    try:
        conn.executescript(_DB_SCHEMA)
        conn.execute(
            """INSERT INTO memories (id, content, tags, created_at, updated_at,
               metadata, project, agent, status)
               VALUES ('bad-1', 'c', 'not-json', '2026', '2026', '{}',
                       'mnemos', 'user', 'raw')"""
        )
        conn.commit()
    finally:
        conn.close()
    target = tmp_path / "t"
    result = runner.invoke(
        app, ["migrate-store", "--from", str(home), "--to", str(target), "--apply"]
    )
    assert result.exit_code == 8
    assert "unparseable tags" in _output(result)
    assert not target.exists()


# ── Config migration ──────────────────────────────────────────────────────────


def test_config_mnemos_section_maps_to_vesma_and_reroots(store_home: Path, tmp_path: Path) -> None:
    import yaml

    (store_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "mnemos": {
                    "data_dir": f"{store_home}/data",
                    "vault_path": f"{store_home}/vault",
                    "db_name": "mnemos.db",
                    "strict_tag_contract": False,
                },
                "logging": {"log_file": f"{store_home}/logs/mnemos.log"},
            }
        ),
        encoding="utf-8",
    )
    target = _target_of(store_home, tmp_path)
    result = runner.invoke(
        app,
        ["migrate-store", "--from", str(store_home), "--to", str(target), "--apply"],
    )
    assert result.exit_code == 0, result.output
    cfg = yaml.safe_load((target / "config.yaml").read_text(encoding="utf-8"))
    assert "mnemos" not in cfg
    assert cfg["vesma"]["db_name"] == "vesma.db"
    assert cfg["vesma"]["data_dir"] == str(target / "data")
    assert cfg["vesma"]["vault_path"] == str(target / "vault")
    assert cfg["vesma"]["strict_tag_contract"] is False
    assert cfg["logging"]["log_file"] == str(target / "logs" / "mnemos.log")
    # The produced config validates against the canonical model.
    from vesma.config import VesmaConfig

    VesmaConfig.model_validate(cfg["vesma"])


def test_config_synthesized_when_source_has_none(store_home: Path, tmp_path: Path) -> None:
    target = _target_of(store_home, tmp_path)
    result = runner.invoke(
        app,
        ["migrate-store", "--from", str(store_home), "--to", str(target), "--apply"],
    )
    assert result.exit_code == 0, result.output
    import yaml

    cfg = yaml.safe_load((target / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["vesma"]["data_dir"] == str(target / "data")
    assert cfg["vesma"]["db_name"] == "vesma.db"


def test_config_unmapped_key_refuses_apply(tmp_path: Path) -> None:
    home = tmp_path / "cfg-home"
    (home / "data").mkdir(parents=True)
    conn = sqlite3.connect(str(home / "data" / "mnemos.db"))
    try:
        conn.executescript(_DB_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    (home / "config.yaml").write_text("ololo:\n  key: 1\n", encoding="utf-8")
    target = tmp_path / "t"
    result = runner.invoke(
        app, ["migrate-store", "--from", str(home), "--to", str(target), "--apply"]
    )
    assert result.exit_code == 9
    assert "ololo" in _output(result)  # key NAME printed, never values
    assert not target.exists()


def test_config_unknown_key_inside_section_refuses_apply(
    store_home: Path, tmp_path: Path
) -> None:
    """Pin (P1-2/CWE-1188): a typo INSIDE a mapped section must abort.

    model_validate with extra=ignore would silently drop e.g. a typo'd
    ``vual_path`` and the produced config would fork the store onto the
    default path. Key NAMES are named; values never print.
    """
    import yaml

    (store_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "mnemos": {
                    "db_name": "mnemos.db",
                    "vual_path": f"{store_home}/vault",
                },
            }
        ),
        encoding="utf-8",
    )
    target = _target_of(store_home, tmp_path)
    result = runner.invoke(
        app,
        ["migrate-store", "--from", str(store_home), "--to", str(target), "--apply"],
    )
    assert result.exit_code == 9
    assert "vual_path" in _output(result)  # key NAME printed, never values
    assert not target.exists()
    assert not list(target.parent.glob("*.staging-*"))


# ── Verification net (whitebox tamper check) ──────────────────────────────────


def test_tampered_target_fails_field_checksum(store_home: Path, tmp_path: Path) -> None:
    from vesma.store_migration import (
        _materialize_target,
        _resolve_layout,
        make_snapshot,
        verify_target,
    )

    layout = _resolve_layout(store_home)
    snapshot = make_snapshot(layout, tmp_path / "snap")
    staging = tmp_path / "staging"
    _materialize_target(snapshot, staging)
    target_db = staging / "data" / "vesma.db"
    # Baseline: verification passes on the honest copy.
    verify_target(target_db, snapshot, [target_db])

    conn = sqlite3.connect(str(target_db))
    try:
        conn.execute("UPDATE memories SET content = content || ' tampered' WHERE id = 'id-03'")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(Exception, match="checksums mismatched"):
        verify_target(target_db, snapshot, [target_db])


# ── Point 11: legacy `vesma migrate` (ai-brain M13) stays intact ─────────────


def test_legacy_migrate_command_untouched_and_no_collision() -> None:
    top = runner.invoke(app, ["--help"])
    assert top.exit_code == 0
    assert "migrate-store" in top.output
    legacy = runner.invoke(app, ["migrate", "--help"])
    assert legacy.exit_code == 0
    assert "from-ai-brain" in legacy.output
    assert "tags" in legacy.output


# ── Env-gated drill (skipped by default; clones only, never the live store) ──


def _drill_env() -> tuple[str, str] | None:
    import os

    src = os.environ.get("VESMA_B3_DRILL_FROM", "")
    dst = os.environ.get("VESMA_B3_DRILL_TO", "")
    return (src, dst) if src and dst else None


@pytest.mark.skipif(
    _drill_env() is None,
    reason="set VESMA_B3_DRILL_FROM / VESMA_B3_DRILL_TO (store CLONE paths) to run",
)
def test_drill_full_cycle_on_operator_clone() -> None:
    src, dst = _drill_env() or ("", "")
    assert src and dst
    dry = runner.invoke(app, ["migrate-store", "--from", src, "--to", dst, "--json"])
    assert dry.exit_code == 0, dry.output
    dry_payload = json.loads(dry.output)
    assert dry_payload["mode"] == "dry-run"

    result = runner.invoke(app, ["migrate-store", "--from", src, "--to", dst, "--apply", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["records"] == dry_payload["records"]
    assert payload["verification"]["sample_mismatches"] == 0

    src_count = sqlite3.connect(f"file:{Path(src) / 'data' / 'mnemos.db'}?mode=ro", uri=True)
    try:
        expected = src_count.execute("SELECT count(*) FROM memories").fetchone()[0]
    finally:
        src_count.close()
    assert payload["records"] == expected
