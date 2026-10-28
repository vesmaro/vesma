"""Home-wide schema-generation stamp (ADR-0049 Lock 1, wave 0).

``PRAGMA user_version`` on every SQLite file of a store home answers ONE
question: do the home's databases belong to the same schema GENERATION?
The store mover refuses a transit when the stamps disagree (a home
assembled from parts of different installs must never be moved as if it
were one store) or when a stamp falls outside the supported band.

This is the GENERATION axis, deliberately orthogonal to each store's
internal migration state (``_run_migrations`` in the main store,
``SCHEMA_VERSION``/``graph_meta`` in the graph sidecar, the metrics
rebuilds): per-store migrations keep evolving inside a generation; the
generation stamp moves only when the home-wide contract does.

Physical map of a home (the contract's «8 баз» counts logical stores;
they share 4 files — verified against the wiring, 2026-10-28; the metrics
sidecar's file name is owned by :mod:`vesma.metrics.schema` and is
deliberately not spelled here — the C1 isolation canary keeps that name
inside the package):

========================  =============================================
file                      logical stores living in it
========================  =============================================
``vesma.db``              main store + sessions + auth + agent tokens
``vectors.db``            vector store
``code_graph.db``         project graph + its audit trail
the metrics sidecar       vitals/awareness sink (name: metrics.schema)
========================  =============================================

Legacy homes predate the stamp and read ``user_version == 0`` on every
file — that is a COHERENT generation for the mover's parity gate (band
floor 0), not a defect: the mover must not write the source (C6), so the
era advances only through newly created stores and through moved targets.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

#: The current home-wide schema generation (wave 0 minted it).
SCHEMA_STAMP_VERSION: int = 1

#: Oldest accepted generation for the mover's band gate. 0 = the
#: pre-stamp era: an all-zero home is parity-coherent legacy, a MIXED
#: zero/non-zero home is the parity violation the gate exists for.
MIN_SUPPORTED_SCHEMA_STAMP: int = 0

STAMP_PRAGMA_SQL = "PRAGMA user_version"


def _ro_uri(db_path: Path) -> str:
    """Read-only SQLite URI with the path percent-encoded (a literal
    ``?``/``%``/``#`` in a store path would be parsed as URI syntax)."""
    return f"file:{quote(str(db_path))}?mode=ro"


def read_schema_stamp(db_path: Path) -> int:
    """The ``user_version`` generation stamp of one SQLite file.

    Read-only open — no WAL recovery, nothing absorbed, safe against a
    live writer. Raises ``sqlite3.DatabaseError`` when the file is not a
    readable SQLite database and ``OSError`` when the filesystem layer
    fails; callers decide the verdict (the mover classifies both as its
    typed stamp refusal).
    """
    conn = sqlite3.connect(_ro_uri(db_path), uri=True, timeout=5.0)
    try:
        row = conn.execute(STAMP_PRAGMA_SQL).fetchone()
    finally:
        conn.close()
    if row is None:  # pragma: no cover - sqlite always returns one row
        raise sqlite3.DatabaseError(f"no user_version row: {db_path}")
    return int(row[0])


def apply_schema_stamp(conn: sqlite3.Connection) -> None:
    """Stamp the home-wide schema generation onto an open connection.

    Store-bootstrap choke point: writes the stamp ONLY when the file is
    unstamped (``user_version == 0``) — idempotent across the stores that
    share one file, never downgrades, and never overwrites a stamp a
    newer binary owns (a newer-era home refuses our mover instead).
    Commits: bootstrap callers may not have an open transaction of their
    own at this point (the stamp must survive even when they roll back).
    """
    row = conn.execute(STAMP_PRAGMA_SQL).fetchone()
    if row is not None and int(row[0]) == 0:
        conn.execute(f"PRAGMA user_version = {SCHEMA_STAMP_VERSION:d}")
        conn.commit()


__all__ = [
    "MIN_SUPPORTED_SCHEMA_STAMP",
    "SCHEMA_STAMP_VERSION",
    "apply_schema_stamp",
    "read_schema_stamp",
]
