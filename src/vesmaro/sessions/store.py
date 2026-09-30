"""SQLite-backed store for A2A Sessions (M16).

Responsibilities:

  * Issue new ``session_id`` values in the contract-mandated format
    ``conv-YYYY-MM-DD-<short-uuid>``.
  * Insert and read ``sessions`` rows.
  * Insert and read ``turns`` rows with **atomic** write semantics
    (single transaction, explicit commit, rollback on any error).
  * Provide **idempotency** for ``POST /v1/sessions/{id}/turns`` via a
    UNIQUE ``message_id`` constraint and a check-before-insert pattern.
  * Compute ``step_number`` and ``turn_id`` deterministically from the
    current row count of the session — no separate counter table.
  * Keep the FTS5 index in sync via the schema triggers (no manual
    inserts here).

This module deliberately does **not** import :mod:`vesmaro.manager`.  It
opens its own SQLite connection, sharing the same database file as the
rest of Vesma (WAL allows concurrent readers and one writer).  The
``SQLiteStore`` used by ``MemoryManager`` is independent — the two
speak to the same file but maintain their own connection pools.

Errors are surfaced as two small exceptions
(:class:`SessionNotFoundError`, :class:`TurnNotFoundError`) so the API
layer can map them to HTTP 404 without leaking SQL details.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from vesmaro.sessions.models import (
    LoadMode,
    Outcome,
    Role,
    SessionCreate,
    SessionRead,
    TurnCreate,
    TurnRangeRequest,
    TurnRead,
)
from vesmaro.sessions.summary import extract_key_decisions, extract_summary

# Short-uuid length used inside the session id (after the date prefix).
# 8 hex chars = 32 bits of entropy — plenty for "short" but enough that
# collisions inside one day are astronomically unlikely.
_SHORT_UUID_LEN = 8


class SessionNotFoundError(LookupError):
    """Raised when a session id is not present in the ``sessions`` table."""


class TurnNotFoundError(LookupError):
    """Raised when a (session_id, turn_id) pair is not in the ``turns`` table."""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _new_session_id(now: datetime | None = None) -> str:
    """Build a session id in the contract format ``conv-YYYY-MM-DD-<short>``.

    The date segment is the UTC date the session was created, not the local
    date, so a node that crosses midnight UTC does not get two ids that
    pretend to be the same "day".  The short suffix is the first
    ``_SHORT_UUID_LEN`` hex chars of a fresh uuid4 — collision-resistant
    within one day across the entire deployment.
    """
    moment = now or datetime.now(UTC)
    short = uuid.uuid4().hex[:_SHORT_UUID_LEN]
    return f"conv-{moment.strftime('%Y-%m-%d')}-{short}"


def _row_to_session(row: sqlite3.Row) -> SessionRead:
    return SessionRead(
        session_id=row["id"],
        user_id=row["user_id"] or "",
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        turns_count=row["turns_count"] or 0,
        metadata=json.loads(row["metadata"] or "{}"),
        ttl_expires_at=(
            datetime.fromisoformat(row["ttl_expires_at"]) if row["ttl_expires_at"] else None
        ),
    )


def _row_to_turn(
    row: sqlite3.Row,
    *,
    include_content: bool = True,
) -> TurnRead:
    key_decisions = json.loads(row["key_decisions"] or "[]")
    tags = json.loads(row["tags"] or "[]")
    outcome = Outcome(row["outcome"]) if row["outcome"] else None
    return TurnRead(
        turn_id=row["turn_id"],
        session_id=row["session_id"],
        step_number=row["step_number"],
        role=Role(row["role"]),
        from_=row["from_agent"],
        to=row["to_agent"],
        summary=row["summary"],
        key_decisions=key_decisions,
        content=(row["content"] if include_content else None),
        outcome=outcome,
        tags=tags,
        context_pointer=row["context_pointer"],
        message_id=row["message_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


# ── Store ─────────────────────────────────────────────────────────────────────


class SessionStore:
    """Thread-safe SQLite store for sessions and turns.

    Connections are thread-local and opened lazily on first use, mirroring
    the pattern used by :class:`vesmaro.storage.sqlite_store.SQLiteStore`
    so both can safely share the same database file via WAL mode.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # The schema and triggers live in ``vesmaro.storage.sqlite_store``'s
        # ``_DB_SCHEMA`` and are executed by the main SQLite store when
        # it opens the file.  When this store is opened standalone (e.g.
        # from a fresh test that bypasses ``MemoryManager``), the schema
        # may not yet exist.  ``_ensure_schema`` is therefore called on
        # every first connection to be self-sufficient.
        self._schema_sql: str | None = None

    # ── Connection management ────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._ensure_schema(conn)
            self._local.conn = conn
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create the A2A tables if they don't yet exist.

        The full schema is owned by :mod:`vesmaro.storage.sqlite_store`,
        which runs the same DDL on its first connection.  This fallback
        exists so the test suite can use a fresh database file without
        having to import the main store.
        """
        if self._schema_sql is None:
            # Import lazily to avoid a circular import at module load
            # time — both modules reference each other's symbols.
            from vesmaro.storage.sqlite_store import _DB_SCHEMA

            self._schema_sql = _DB_SCHEMA
        conn.executescript(self._schema_sql)
        conn.commit()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ── Sessions ─────────────────────────────────────────────────────────

    def create_session(self, payload: SessionCreate) -> SessionRead:
        """Insert a new session and return the persisted row.

        No idempotency key for sessions — callers that need one should
        re-use the returned ``session_id`` (or implement a higher-level
        dedup above the API).  Atomic via single-statement INSERT +
        commit.
        """
        now = datetime.now(UTC)
        session_id = _new_session_id(now)
        conn = self._get_conn()
        try:
            conn.execute(
                """
                INSERT INTO sessions (id, user_id, created_at, updated_at,
                                      metadata, ttl_expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    payload.user_id,
                    now.isoformat(),
                    now.isoformat(),
                    json.dumps(payload.metadata, ensure_ascii=False),
                    payload.ttl_expires_at.isoformat() if payload.ttl_expires_at else None,
                ),
            )
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
        return SessionRead(
            session_id=session_id,
            user_id=payload.user_id,
            created_at=now,
            updated_at=now,
            turns_count=0,
            metadata=payload.metadata,
            ttl_expires_at=payload.ttl_expires_at,
        )

    def get_session(self, session_id: str) -> SessionRead:
        """Return the session row or raise :class:`SessionNotFoundError`."""
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT s.id, s.user_id, s.created_at, s.updated_at,
                   s.metadata, s.ttl_expires_at,
                   (SELECT COUNT(*) FROM turns t WHERE t.session_id = s.id) AS turns_count
            FROM sessions s
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise SessionNotFoundError(f"Session {session_id!r} not found")
        return _row_to_session(row)

    # ── Turns ────────────────────────────────────────────────────────────

    def create_turn(self, session_id: str, payload: TurnCreate) -> TurnRead:
        """Atomically insert a turn with idempotency via ``message_id``.

        Behaviour:

          1. If the session does not exist, raise
             :class:`SessionNotFoundError`.
          2. If ``payload.message_id`` is set, look up an existing turn
             with that id; if found, return it untouched (idempotency).
          3. Otherwise compute ``step_number`` and ``turn_id`` from the
             current row count for the session and INSERT inside a
             single transaction.  On any error, roll back and re-raise.
          4. Update ``sessions.updated_at`` in the same transaction so
             that a reader can always use it to detect "recently
             changed" without scanning ``turns``.
        """
        conn = self._get_conn()
        try:
            # 1. Confirm session exists.  Locking the row with ``FOR UPDATE``
            # would be ideal for true serial writes, but SQLite serializes
            # all writers via the WAL — a single connection's transaction
            # is enough for the contract we promise to callers.
            session = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if session is None:
                raise SessionNotFoundError(f"Session {session_id!r} not found")

            # 2. Idempotency check first — read inside the same transaction
            # so a concurrent writer cannot slip in between this SELECT
            # and the INSERT below (SQLite's default isolation is
            # SERIALIZABLE for write transactions, so this holds).
            if payload.message_id:
                existing = conn.execute(
                    "SELECT * FROM turns WHERE message_id = ?",
                    (payload.message_id,),
                ).fetchone()
                if existing is not None:
                    # No mutation; commit is implicit (no writes).
                    return _row_to_turn(existing, include_content=True)

            # 3. Compute step_number as MAX(step)+1 atomically within the
            # transaction.  The aggregation is a single SELECT, so there
            # is no observable race.
            step_row = conn.execute(
                "SELECT COALESCE(MAX(step_number), 0) + 1 FROM turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            step_number = int(step_row[0])
            turn_id = f"turn-{step_number}"
            context_pointer = f"memory://{session_id}#step-{step_number}"

            # Extract summary / key decisions up front so the row is
            # fully populated at INSERT time.
            summary = extract_summary(payload.content)
            key_decisions = extract_key_decisions(payload.content)

            # 4. Insert the turn. No FTS shadow anymore — C8 (ArchCom
            # 2026-08-27) removed turns_fts + the turns_ai/ad/au triggers
            # (dead index, second plaintext copy at rest).
            row_id = uuid.uuid4().hex
            now = datetime.now(UTC).isoformat()
            conn.execute(
                """
                INSERT INTO turns (
                    id, session_id, turn_id, step_number, role,
                    from_agent, to_agent, message_id, content, summary,
                    key_decisions, outcome, tags, context_pointer, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    session_id,
                    turn_id,
                    step_number,
                    payload.role.value,
                    payload.from_,
                    payload.to,
                    payload.message_id,
                    payload.content,
                    summary,
                    json.dumps(key_decisions, ensure_ascii=False),
                    payload.outcome.value if payload.outcome is not None else None,
                    json.dumps(payload.tags, ensure_ascii=False),
                    context_pointer,
                    now,
                ),
            )

            # 5. Bump the session's updated_at in the same transaction.
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            conn.commit()
        except SessionNotFoundError:
            conn.rollback()
            raise
        except sqlite3.IntegrityError:
            # Race window: another writer inserted with the same
            # message_id between our SELECT and INSERT.  Re-read and
            # return the canonical row.  This is the same contract as
            # the pre-check idempotency path.
            conn.rollback()
            if payload.message_id:
                existing = conn.execute(
                    "SELECT * FROM turns WHERE message_id = ?",
                    (payload.message_id,),
                ).fetchone()
                if existing is not None:
                    return _row_to_turn(existing, include_content=True)
            raise
        except sqlite3.Error:
            conn.rollback()
            raise

        return TurnRead(
            turn_id=turn_id,
            session_id=session_id,
            step_number=step_number,
            role=payload.role,
            from_=payload.from_,
            to=payload.to,
            summary=summary,
            key_decisions=key_decisions,
            content=payload.content,
            outcome=payload.outcome,
            tags=payload.tags,
            context_pointer=context_pointer,
            message_id=payload.message_id,
            created_at=datetime.fromisoformat(now),
        )

    def get_turn(
        self,
        session_id: str,
        turn_id: str,
        *,
        mode: LoadMode = LoadMode.SUMMARY,
    ) -> TurnRead:
        """Return a single turn in ``summary`` or ``full`` mode.

        Raises :class:`SessionNotFoundError` if the session does not exist
        (so the API can distinguish "no such session" from "no such turn"
        in 404 bodies if it wants to), and :class:`TurnNotFoundError` if
        the turn is absent for a known session.
        """
        conn = self._get_conn()
        # Verify the session first so 404 messages are accurate.
        session = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if session is None:
            raise SessionNotFoundError(f"Session {session_id!r} not found")

        row = conn.execute(
            "SELECT * FROM turns WHERE session_id = ? AND turn_id = ?",
            (session_id, turn_id),
        ).fetchone()
        if row is None:
            raise TurnNotFoundError(f"Turn {turn_id!r} not found in session {session_id!r}")
        return _row_to_turn(row, include_content=(mode == LoadMode.FULL))

    def get_turns_range(
        self,
        session_id: str,
        request: TurnRangeRequest,
    ) -> tuple[list[TurnRead], int]:
        """Return turns whose ``step_number`` is in ``[from_step, to_step]``.

        The result is sorted by ``step_number`` ascending.  ``total`` is
        the count of turns in the requested range (not the count for the
        whole session) — that's the most useful value for a UI showing a
        paginated window.
        """
        conn = self._get_conn()
        session = conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if session is None:
            raise SessionNotFoundError(f"Session {session_id!r} not found")

        rows = conn.execute(
            """
            SELECT * FROM turns
            WHERE session_id = ?
              AND step_number BETWEEN ? AND ?
            ORDER BY step_number ASC
            """,
            (session_id, request.from_step, request.to_step),
        ).fetchall()
        turns = [_row_to_turn(r, include_content=(request.mode == LoadMode.FULL)) for r in rows]
        return turns, len(turns)


__all__ = [
    "LoadMode",
    "Outcome",
    "Role",
    "SessionCreate",
    "SessionNotFoundError",
    "SessionRead",
    "SessionStore",
    "TurnCreate",
    "TurnNotFoundError",
    "TurnRangeRequest",
    "TurnRead",
]
