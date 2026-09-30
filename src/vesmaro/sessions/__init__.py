"""A2A Sessions API for GCW v0.6.0 (M16).

Provides 5 HTTP endpoints for the GCW A2A routing layer:

  1. ``POST   /v1/sessions``                        — create session
  2. ``GET    /v1/sessions/{id}``                    — read session metadata
  3. ``POST   /v1/sessions/{id}/turns``              — write turn (idempotent)
  4. ``GET    /v1/sessions/{id}/turns/{turn_id}``    — lazy load turn
  5. ``POST   /v1/sessions/{id}/turns/range``        — bulk load range

The storage backend is the same SQLite database the rest of Vesma uses
(via :class:`vesmaro.storage.sqlite_store.SQLiteStore`), with WAL mode for
concurrent reads. Idempotency is provided by a UNIQUE constraint on
``turns.message_id`` and a check-before-insert pattern in
:class:`SessionStore.create_turn`.

This package deliberately does **not** import the heavyweight
:class:`vesmaro.manager.MemoryManager` — the A2A module is independent of
the knowledge pipeline and is safe to use as a lightweight dependency.
"""

from vesmaro.sessions.api import router
from vesmaro.sessions.models import (
    LoadMode,
    Outcome,
    Role,
    SessionCreate,
    SessionRead,
    TurnCreate,
    TurnRangeRequest,
    TurnRangeResponse,
    TurnRead,
)
from vesmaro.sessions.store import SessionNotFoundError, SessionStore, TurnNotFoundError
from vesmaro.sessions.summary import extract_key_decisions, extract_summary

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
    "TurnRangeResponse",
    "TurnRead",
    "extract_key_decisions",
    "extract_summary",
    "router",
]
