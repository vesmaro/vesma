"""Isolated store copies for the S4 stand (ADR-0020 §Stands, wave BF-2).

S4 measures whether the WHOLE memory is available and correct at any
moment — and it must do that WITHOUT touching the store under test. Two
isolation layers:

* **The fixture store is a fresh tmp store** the stand populates itself
  (a representative population: published / refined / failed /
  quarantined / raw rows) — no production store is ever probed
  (ADR-0020 alternatives table: "S4 probes on the production store"
  rejected).
* **The probe store is an ISOLATED COPY of the fixture store** made via
  the SQLite **backup API** (:meth:`sqlite3.Connection.backup`), never a
  file copy: both stores run WAL-mode, so cloning the db file while a
  WAL exists can lose committed transactions or copy a torn state. The
  backup API streams a consistent snapshot page-by-page including WAL
  content.

The store is TWO sqlite files plus a vault directory —
``<data_dir>/<db_name>`` (memories/FTS/CCR/traces) and
``<data_dir>/vectors.db`` (the numpy vector index) — so the copy is made
per-file through the backup API and the vault is skipped entirely
(vault write-back is non-fatal and never read by the probes; probe
managers point the vault at the COPY's root so the source tree stays
untouched even on a misbehaving path).

Write-orientation of the probes (``probe.py``): the probe manager opens
the COPY, every probe is strictly reading (search / get / list_recent /
assemble / marker parse). The ONLY writes on the copy are the audit
marking wave (§Audit marking), which runs inside a dedicated wave
container and is checksummed back by ``run.py`` against a fresh
checksum of the probe store — the read-only invariant of the probes
themselves still holds.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from vesmaro.config import Settings


def store_files(settings: Settings) -> dict[str, str]:
    """The sqlite files a manager persists under ``settings``.

    Returns ``{path: kind}`` — ``kind`` is the file's role
    (``memories`` / ``vectors``). Both files are (or will be) in WAL
    journal mode: ``SQLiteStore._get_conn`` and ``VectorStore.__init__``
    both PRAGMA ``journal_mode=WAL``. WAL files must never be
    file-copied — the reason this module routes every clone through the
    backup API.
    """
    return {
        str(settings.db_path): "memories",
        str(settings.vesma.data_dir / "vectors.db"): "vectors",
    }


def clone_store(source_settings: Settings, target_settings: Settings) -> None:
    """Clone one store's databases into the target layout via the backup API.

    Both databases (``<db_name>`` + ``vectors.db``) are backed up
    source → target with :meth:`sqlite3.Connection.backup` — a
    consistent snapshot that includes committed WAL content, unlike a
    byte copy of the file (+ its uncheckpointed WAL) which can race the
    source or arrive torn. The target must NOT have been opened by a
    manager yet (no schema yet): the backup materialises the full source
    schema and rows; the first connect on the target then runs
    idempotent migrations over an already-current schema (no-ops).

    The vault directory is deliberately NOT copied: ``MemoryManager``
    writes the vault mirror non-fatally and no probe reads it back;
    probe managers get their own vault root under the probe directory.
    """
    for source_path, _kind in store_files(source_settings).items():
        source = Path(source_path)
        target_path = target_settings.vesma.data_dir / source.name
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            # A store file that never materialised (e.g. vectors.db with
            # zero published rows) — the target manager recreates it.
            continue
        conn = sqlite3.connect(str(source))
        try:
            target = sqlite3.connect(str(target_path))
            try:
                conn.backup(target)
            finally:
                target.close()
        finally:
            conn.close()


def store_fingerprint(settings: Settings) -> dict[str, int]:
    """Cheap integrity counters of a store (rows per table, all dbs).

    NOT a byte hash — the manager's own audit-marking wave legitimately
    mutates lifecycle bookkeeping. Used by the run harness to prove the
    PROBE waves left the memory content untouched (the mutation the S4
    read-only invariant must catch: a probe that writes would move
    ``memories`` / ``embeddings`` / ``ccr_cache`` content, never only
    counters).
    """
    counts: dict[str, int] = {}
    for path, kind in store_files(settings).items():
        if not Path(path).exists():
            counts[kind] = -1  # never materialised — recorded honestly
            continue
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if kind == "memories":
                counts["memories"] = int(
                    conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                )
                counts["ccr_cache"] = int(
                    conn.execute("SELECT COUNT(*) FROM ccr_cache").fetchone()[0]
                )
                counts["traces"] = int(conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0])
            else:
                counts["embeddings"] = int(
                    conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
                )
        except sqlite3.OperationalError:
            counts[kind] = -1  # table absent (never opened) — recorded honestly
        finally:
            conn.close()
    return counts
