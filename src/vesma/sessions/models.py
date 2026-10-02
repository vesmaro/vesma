"""Pydantic models for the A2A Sessions API (M16).

These models are deliberately isolated from the rest of the Vesma models
to keep the A2A contract (per ``docs/a2a/mnemos-requirements.md``) stable
and reviewable in one place.  They mirror the requirements document 1:1:

  * ``Role``  — the four valid role strings for a turn.
  * ``Outcome`` — the four valid outcomes for ``role == a2a_message``.
  * ``LoadMode`` — ``summary`` (default) or ``full`` for turn retrieval.
  * ``SessionCreate`` / ``SessionRead`` — request/response for sessions.
  * ``TurnCreate`` / ``TurnRead`` — request/response for turns.
  * ``TurnRangeRequest`` / ``TurnRangeResponse`` — bulk-load a step range.

Pydantic v2 is used (matches the rest of Vesma).  Field validation
covers the small set of invariants that must hold for every payload:

  * ``user_id`` and ``session_id`` are non-empty strings of bounded length.
  * ``role`` must be one of the four enum values.
  * ``outcome`` is required iff ``role == a2a_message``.
  * ``message_id`` is truncated to 256 chars defensively.
  * ``from_step`` <= ``to_step`` for range requests.

These guards run at the HTTP boundary, so anything past :class:`SessionStore`
can trust the input shape.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# Cap to defend against accidental trillion-character payloads from a buggy
# upstream MCP.  1 MB of plain text is plenty for a single A2A turn.
MAX_CONTENT_CHARS = 1_000_000
MAX_MESSAGE_ID_CHARS = 256
MAX_ID_CHARS = 256


class Role(StrEnum):
    """Discriminator for who authored a turn."""

    USER = "user"
    AGENT = "agent"
    A2A_MESSAGE = "a2a_message"
    SYSTEM = "system"


class Outcome(StrEnum):
    """Delivery outcome for ``role == a2a_message``."""

    DELIVERED = "delivered"
    REJECTED = "rejected"
    BUDGET_EXHAUSTED = "budget-exhausted"
    LOOP_DETECTED = "loop-detected"


class LoadMode(StrEnum):
    """Return shape for GET turn endpoints."""

    SUMMARY = "summary"
    FULL = "full"


# ── Session ───────────────────────────────────────────────────────────────────


class SessionCreate(BaseModel):
    """Request body for ``POST /v1/sessions``."""

    user_id: str = Field(default="", max_length=MAX_ID_CHARS)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Optional client hint for TTL — server may clamp/ignore.
    ttl_expires_at: datetime | None = None

    @field_validator("user_id")
    @classmethod
    def _strip_user_id(cls, v: str) -> str:
        return v.strip()


class SessionRead(BaseModel):
    """Response body for session endpoints (POST and GET share this shape)."""

    session_id: str
    user_id: str
    created_at: datetime
    updated_at: datetime
    turns_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    ttl_expires_at: datetime | None = None


# ── Turn ──────────────────────────────────────────────────────────────────────


class TurnCreate(BaseModel):
    """Request body for ``POST /v1/sessions/{id}/turns``."""

    role: Role
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    # Pydantic v2 reserves the attribute name ``from_`` (because ``from`` is
    # a Python keyword).  We use ``from_`` in Python and expose it as
    # ``from`` in JSON for callers — see ``model_config`` and the alias
    # below.  ``to`` is fine.
    from_: str | None = Field(default=None, max_length=MAX_ID_CHARS, alias="from")
    to: str | None = Field(default=None, max_length=MAX_ID_CHARS)
    message_id: str | None = Field(default=None, max_length=MAX_MESSAGE_ID_CHARS)
    outcome: Outcome | None = None
    tags: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @field_validator("message_id")
    @classmethod
    def _truncate_message_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        return v[:MAX_MESSAGE_ID_CHARS]

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, v: list[str]) -> list[str]:
        # Strip empties, dedupe while preserving order.
        seen: set[str] = set()
        out: list[str] = []
        for t in v:
            t = t.strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out[:32]

    @model_validator(mode="after")
    def _outcome_requires_a2a(self) -> TurnCreate:
        if self.role == Role.A2A_MESSAGE and self.outcome is None:
            # ``a2a_message`` is the agent-to-agent carrier; outcome is
            # part of its contract.  Default to DELIVERED to keep the
            # common case frictionless — callers may still override.
            object.__setattr__(self, "outcome", Outcome.DELIVERED)
        if self.role != Role.A2A_MESSAGE and self.outcome is not None:
            raise ValueError(
                "outcome is only valid for role='a2a_message'; "
                f"got role='{self.role}' with outcome='{self.outcome.value}'"
            )
        return self


class TurnRead(BaseModel):
    """Response body for turn write / read / range endpoints.

    In ``summary`` mode (default for GET turn) ``content`` is omitted.
    """

    turn_id: str
    session_id: str
    step_number: int
    role: Role
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    summary: str | None = None
    key_decisions: list[str] = Field(default_factory=list)
    content: str | None = None
    outcome: Outcome | None = None
    tags: list[str] = Field(default_factory=list)
    context_pointer: str
    message_id: str | None = None
    created_at: datetime

    model_config = {"populate_by_name": True}

    # Pydantic v2's generated stub for ``__init__`` does not reflect
    # ``populate_by_name=True`` — it only advertises the *alias* name
    # (``from``).  We restate the signature here so call sites can keep
    # using the Python attribute name ``from_`` without a ``# type: ignore``
    # on every ``TurnRead(..., from_=...)`` invocation.  Behaviour at
    # runtime is identical to the generated ``__init__``; this is a
    # type-stub shim only.
    def __init__(  # explicit kwarg surface for mypy (Pydantic v2 stub gap)
        self,
        *,
        turn_id: str,
        session_id: str,
        step_number: int,
        role: Role,
        from_: str | None = None,
        to: str | None = None,
        summary: str | None = None,
        key_decisions: list[str] | None = None,
        content: str | None = None,
        outcome: Outcome | None = None,
        tags: list[str] | None = None,
        context_pointer: str,
        message_id: str | None = None,
        created_at: datetime,
    ) -> None:
        super().__init__(
            turn_id=turn_id,
            session_id=session_id,
            step_number=step_number,
            role=role,
            from_=from_,
            to=to,
            summary=summary,
            key_decisions=key_decisions if key_decisions is not None else [],
            content=content,
            outcome=outcome,
            tags=tags if tags is not None else [],
            context_pointer=context_pointer,
            message_id=message_id,
            created_at=created_at,
        )


class TurnRangeRequest(BaseModel):
    """Request body for ``POST /v1/sessions/{id}/turns/range``."""

    from_step: int = Field(ge=1, le=10_000_000)
    to_step: int = Field(ge=1, le=10_000_000)
    mode: LoadMode = LoadMode.SUMMARY

    @model_validator(mode="after")
    def _step_order(self) -> TurnRangeRequest:
        if self.to_step < self.from_step:
            raise ValueError(f"to_step ({self.to_step}) must be >= from_step ({self.from_step})")
        # Cap the window to keep response sizes bounded.
        if self.to_step - self.from_step > 1000:
            raise ValueError("range too large: maximum 1000 turns per request")
        return self


class TurnRangeResponse(BaseModel):
    """Response body for ``POST /v1/sessions/{id}/turns/range``."""

    turns: list[TurnRead]
    total: int
    mode: LoadMode
