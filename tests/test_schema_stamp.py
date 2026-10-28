"""ADR-0049 wave 0: the home-wide schema-generation stamp (user_version).

Pins: every physical store file of a home joins the generation on
bootstrap (stamp-if-unstamped, idempotent across the stores that share
one file), legacy zero-stamped files upgrade, a foreign/newer stamp is
never overwritten, and the reader refuses non-SQLite files loudly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from vesma.schema_stamp import (
    MIN_SUPPORTED_SCHEMA_STAMP,
    SCHEMA_STAMP_VERSION,
    apply_schema_stamp,
    read_schema_stamp,
)


def _stamp(db_path: Path) -> int:
    return read_schema_stamp(db_path)


def test_fresh_main_store_is_stamped(tmp_path: Path) -> None:
    from vesma.storage.sqlite_store import SQLiteStore

    store = SQLiteStore(tmp_path / "vesma.db")
    store.count()  # trigger the lazy first connection (schema bootstrap)
    assert _stamp(tmp_path / "vesma.db") == SCHEMA_STAMP_VERSION


def test_fresh_vector_store_is_stamped(tmp_path: Path) -> None:
    from vesma.storage.vector_store import VectorStore

    VectorStore(tmp_path)
    assert _stamp(tmp_path / "vectors.db") == SCHEMA_STAMP_VERSION


def test_fresh_code_graph_sidecar_is_stamped(tmp_path: Path) -> None:
    from vesma.storage.code_graph_store import CodeGraphStore

    CodeGraphStore(tmp_path)
    assert _stamp(tmp_path / "code_graph.db") == SCHEMA_STAMP_VERSION


def test_fresh_metrics_sidecar_is_stamped(tmp_path: Path) -> None:
    from vesma.metrics.sink import MetricsStore

    store = MetricsStore(tmp_path / "metrics.sqlite")
    store._conn()  # trigger the lazy bootstrap (the stamp rides SCHEMA_SQL init)
    assert _stamp(tmp_path / "metrics.sqlite") == SCHEMA_STAMP_VERSION


def test_shared_file_stores_are_idempotent_on_one_file(tmp_path: Path) -> None:
    """sessions/auth/agent-tokens live in vesma.db — each bootstrap must
    leave the shared file's stamp at exactly one generation."""
    from vesma.agent_tokens import AgentTokenStore
    from vesma.api.auth_store import AuthStore
    from vesma.sessions.store import SessionStore
    from vesma.storage.sqlite_store import SQLiteStore

    db = tmp_path / "vesma.db"
    SQLiteStore(db)
    SessionStore(db)
    AuthStore(db)
    AgentTokenStore(db)
    assert _stamp(db) == SCHEMA_STAMP_VERSION


def test_session_store_standalone_bootstraps_the_stamp(tmp_path: Path) -> None:
    """A store opening a FRESH file first (no main store ran) stamps it —
    any bootstrap path is a stamping path."""
    from vesma.sessions.models import SessionCreate
    from vesma.sessions.store import SessionStore

    db = tmp_path / "vesma.db"
    SessionStore(db).create_session(SessionCreate())
    assert _stamp(db) == SCHEMA_STAMP_VERSION


def test_legacy_zero_stamped_file_upgrades_on_bootstrap(tmp_path: Path) -> None:
    from vesma.storage.vector_store import VectorStore

    db = tmp_path / "vectors.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE IF NOT EXISTS embeddings (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    assert _stamp(db) == 0  # pre-stamp era

    VectorStore(tmp_path)
    assert _stamp(db) == SCHEMA_STAMP_VERSION


def test_apply_never_overwrites_a_newer_stamp(tmp_path: Path) -> None:
    """A stamp a NEWER binary owns is foreign state — never downgraded."""
    db = tmp_path / "x.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA user_version = 99")
    apply_schema_stamp(conn)
    assert _stamp(db) == 99
    conn.close()


def test_read_refuses_non_sqlite_file(tmp_path: Path) -> None:
    fake = tmp_path / "not-a-db.db"
    fake.write_bytes(b"plain text, no sqlite header at all" * 8)
    with pytest.raises(sqlite3.DatabaseError):
        read_schema_stamp(fake)


def test_band_floor_is_zero_for_the_legacy_era() -> None:
    """The mover's band includes 0: an all-zero home is coherent legacy,
    not a defect (the mover must not write the source, C6)."""
    assert MIN_SUPPORTED_SCHEMA_STAMP == 0
    assert SCHEMA_STAMP_VERSION >= 1
