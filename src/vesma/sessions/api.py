"""FastAPI router for the A2A Sessions API (M16).

Five endpoints, all under the ``/v1`` namespace (the prefix is added by
``api/main.py`` via ``app.include_router(router, prefix="/v1")``):

  1. ``POST   /sessions``                                 — create session
  2. ``GET    /sessions/{session_id}``                    — read session
  3. ``POST   /sessions/{session_id}/turns``              — write turn
  4. ``GET    /sessions/{session_id}/turns/{turn_id}``    — read one turn
  5. ``POST   /sessions/{session_id}/turns/range``        — bulk range read

The router does **not** own a ``SessionStore`` instance directly.  It
relies on the global ``app.state`` populated by the lifespan hook in
:mod:`vesma.api.main` so the test fixture (which already injects an
isolated ``MemoryManager`` per test) can override the store the same
way.  If the lifespan hook has not run for whatever reason, the router
falls back to constructing a store from the default settings — handy
for ``TestClient(app)`` outside the standard fixture.

Error mapping is uniform:

  * 422 — Pydantic validation errors (FastAPI default, no extra code).
  * 404 — :class:`SessionNotFoundError` or :class:`TurnNotFoundError`.
  * 500 — anything else (FastAPI default, with the global exception
    handler in production catching & logging).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Path, Query, Request

from vesma.config import load_settings
from vesma.sessions.models import (
    LoadMode,
    SessionCreate,
    SessionRead,
    TurnCreate,
    TurnRangeRequest,
    TurnRangeResponse,
    TurnRead,
)
from vesma.sessions.store import (
    SessionNotFoundError,
    SessionStore,
    TurnNotFoundError,
)

logger = logging.getLogger(__name__)

# The path-parameter name ``session_id`` would conflict with the body
# field ``session_id`` of the response model if FastAPI's response_model
# used the same name in two places.  We keep them separate by passing
# the URL parameter as ``session_id`` and naming the body field
# ``session_id`` too — FastAPI handles the disambiguation automatically.
router = APIRouter(tags=["a2a-sessions"])


def _get_store(request: Request) -> SessionStore:
    """Resolve the per-app ``SessionStore`` (preferred) or build a default."""
    store: SessionStore | None = getattr(request.app.state, "sessions_store", None)
    if store is not None:
        return store
    settings = load_settings()
    return SessionStore(settings.db_path)


# ── 1. Create session ─────────────────────────────────────────────────────────


@router.post(
    "/sessions",
    response_model=SessionRead,
    status_code=201,
    summary="Create a new A2A conversation session",
)
async def create_session(
    payload: SessionCreate,
    request: Request,
) -> SessionRead:
    """Mint a new ``session_id`` (``conv-YYYY-MM-DD-<short-uuid>``) and
    persist the row.  Returns 201 with the canonical session shape.
    """
    store = _get_store(request)
    return store.create_session(payload)


# ── 2. Read session ──────────────────────────────────────────────────────────


@router.get(
    "/sessions/{session_id}",
    response_model=SessionRead,
    summary="Read a session's metadata + turn count",
)
async def get_session(
    request: Request,
    session_id: str = Path(..., min_length=1, max_length=256),
) -> SessionRead:
    """Return the session row with the current ``turns_count`` aggregated
    from the ``turns`` table.  404 when the session does not exist.
    """
    store = _get_store(request)
    try:
        return store.get_session(session_id)
    except SessionNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# ── 3. Write turn ────────────────────────────────────────────────────────────


@router.post(
    "/sessions/{session_id}/turns",
    response_model=TurnRead,
    status_code=201,
    summary="Append a turn to a session (idempotent via message_id)",
)
async def create_turn(
    payload: TurnCreate,
    request: Request,
    session_id: str = Path(..., min_length=1, max_length=256),
) -> TurnRead:
    """Atomically insert a turn.  Idempotent on ``message_id``: a repeat
    POST with the same id returns the existing turn (200-equivalent body
    with 201 status) instead of duplicating.
    """
    store = _get_store(request)
    try:
        return store.create_turn(session_id, payload)
    except SessionNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# ── 4. Read one turn ─────────────────────────────────────────────────────────


@router.get(
    "/sessions/{session_id}/turns/{turn_id}",
    response_model=TurnRead,
    summary="Lazy-load a single turn (summary by default, full on demand)",
)
async def get_turn(
    request: Request,
    session_id: str = Path(..., min_length=1, max_length=256),
    turn_id: str = Path(..., min_length=1, max_length=64),
    # B008: FastAPI's standard idiom for declaring query params with
    # defaults is ``Query(default=<value>)`` in the signature; this is a
    # known false positive of B008, identical to the pattern used in
    # ``mnemos/api/main.py``.
    mode: LoadMode = Query(default=LoadMode.SUMMARY),  # noqa: B008
) -> TurnRead:
    """Return one turn in ``summary`` mode (default) or ``full`` mode.

    Summary mode is the cheap path: target agents call this with
    ``?mode=summary`` to keep the wire payload under ~500 bytes.
    """
    store = _get_store(request)
    try:
        return store.get_turn(session_id, turn_id, mode=mode)
    except SessionNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except TurnNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# ── 5. Read range of turns ───────────────────────────────────────────────────


@router.post(
    "/sessions/{session_id}/turns/range",
    response_model=TurnRangeResponse,
    summary="Bulk load a contiguous range of turns",
)
async def get_turns_range(
    payload: TurnRangeRequest,
    request: Request,
    session_id: str = Path(..., min_length=1, max_length=256),
) -> TurnRangeResponse:
    """Return turns in ``[from_step, to_step]`` (inclusive on both ends).

    Default mode is ``summary``; pass ``"full"`` to get raw content.
    Result is sorted by ``step_number`` ascending.  ``total`` reflects
    the number of turns actually returned (not the whole session).
    """
    store = _get_store(request)
    try:
        turns, total = store.get_turns_range(session_id, payload)
    except SessionNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return TurnRangeResponse(turns=turns, total=total, mode=payload.mode)
