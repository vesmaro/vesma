"""Workflow lifecycle state machine for memories (vesma #96).

Separates mutable *workflow state* (this module: open → in-progress → done,
blocked/resolved, terminal states) from append-only *classification* (the
tag contract: ``project:X``, ``vesma:decision``). The tag layer stays
append-only; this layer is the mutable lifecycle.

The state machine is pure: it knows nothing about SQLite, locks, or the
manager. ``MemoryManager.workflow_set`` is the ONLY place that calls
``validate_transition`` before writing — enforcement is server-side, so
neither the MCP tool nor the REST endpoints can bypass it.

State diagram (vesma #96, ArchCom 2026-07-18)::

    open ─────────────▶ in-progress ─────────────▶ done          (normal path)
             ▲              │   ▲
             │              ▼   │
             │           blocked ◀──────────────── (stuck)
             │              │
             │              ▼
             └─────── resolved ───────────────────▶ (back to in-progress or done)

    blocked → done   FORBIDDEN  (blocker must resolve first)
    done, withdrawn  terminal   (no further transitions)
    tombstoned       terminal   (soft-delete marker, ArchCom 2026-10-18;
                                 enterable from ANY state ONLY via the
                                 owner-only delete path — the generic
                                 workflow set surface refuses it)

The forbidden ``blocked → done`` edge forces a blocker to go through
``resolved`` before a memory can be marked done — an agent cannot silently
skip a stuck dependency by jumping straight to a terminal state.
"""

from __future__ import annotations

from enum import StrEnum


class WorkflowStatus(StrEnum):
    """Lifecycle states for a memory's workflow (vesma #96).

    Distinct from ``MemoryStatus`` (raw/processing/processed/published/
    archived), which tracks the *knowledge-pipeline* stage. ``WorkflowStatus``
    tracks the *work lifecycle* — whether a piece of work on the memory is
    open, in progress, blocked, resolved, done, withdrawn — or deleted
    (``tombstoned``).
    """

    OPEN = "open"
    IN_PROGRESS = "in-progress"
    BLOCKED = "blocked"
    RESOLVED = "resolved"
    DONE = "done"
    WITHDRAWN = "withdrawn"
    #: Stage 1 delete (ArchCom 2026-10-18, mnemos 24f32fa7): soft-delete
    #: marker. Enterable from ANY state through the owner-only delete path
    #: (deletion knows no workflow state — a done/blocked work item can be
    #: deleted just like an open one). Terminal: no outgoing edges —
    #: Stage 1 has NO undelete; Stage 2 (hard purge) consumes the row.
    TOMBSTONED = "tombstoned"


#: Terminal states — once reached, no further transition is permitted.
TERMINAL_STATUSES: frozenset[WorkflowStatus] = frozenset(
    {WorkflowStatus.DONE, WorkflowStatus.WITHDRAWN, WorkflowStatus.TOMBSTONED}
)

#: Allowed transitions from each state. The absence of an edge is itself a
#: rule: ``blocked`` has no edge to ``done``, so a stuck dependency must go
#: through ``resolved`` first (the forbidden edge enforced server-side).
#:
#: ``open`` → ``in-progress`` (start work), ``withdrawn`` (cancel before start),
#:           ``tombstoned`` (delete)
#:           NOTE: ``open`` has no edge to ``blocked`` — a memory must enter
#:           ``in-progress`` before it can be blocked (you cannot block work
#:           that has not started).
#: ``in-progress`` → ``blocked`` (hit a dependency/stuck), ``done`` (finish),
#:                   ``withdrawn`` (abandon mid-work), ``tombstoned`` (delete)
#: ``blocked`` → ``resolved`` (blocker cleared), ``withdrawn`` (give up),
#:              ``tombstoned`` (delete)
#:              NOTE: no edge to ``done`` — the blocker must resolve first
#: ``resolved`` → ``in-progress`` (resume work), ``done`` (finish post-resolve),
#:                ``withdrawn`` (abandon post-resolve), ``tombstoned`` (delete)
#: ``done`` / ``withdrawn`` → ``tombstoned`` (delete) — the ONLY edge out of a
#:              workflow-terminal state; ``tombstoned`` itself is terminal.
#: ``tombstoned`` → (terminal, no outgoing edges)
ALLOWED_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.OPEN: frozenset(
        {WorkflowStatus.IN_PROGRESS, WorkflowStatus.WITHDRAWN, WorkflowStatus.TOMBSTONED}
    ),
    WorkflowStatus.IN_PROGRESS: frozenset(
        {
            WorkflowStatus.BLOCKED,
            WorkflowStatus.DONE,
            WorkflowStatus.WITHDRAWN,
            WorkflowStatus.TOMBSTONED,
        }
    ),
    WorkflowStatus.BLOCKED: frozenset(
        {WorkflowStatus.RESOLVED, WorkflowStatus.WITHDRAWN, WorkflowStatus.TOMBSTONED}
    ),
    WorkflowStatus.RESOLVED: frozenset(
        {
            WorkflowStatus.IN_PROGRESS,
            WorkflowStatus.DONE,
            WorkflowStatus.WITHDRAWN,
            WorkflowStatus.TOMBSTONED,
        }
    ),
    WorkflowStatus.DONE: frozenset({WorkflowStatus.TOMBSTONED}),
    WorkflowStatus.WITHDRAWN: frozenset({WorkflowStatus.TOMBSTONED}),
    WorkflowStatus.TOMBSTONED: frozenset(),
}


class WorkflowTransitionError(ValueError):
    """Raised when a transition is forbidden by the workflow state machine."""


def is_terminal(status: WorkflowStatus) -> bool:
    """Return True if ``status`` is terminal (done / withdrawn / tombstoned)."""
    return status in TERMINAL_STATUSES


def transition_allowed(from_status: WorkflowStatus | None, to_status: WorkflowStatus) -> bool:
    """Return True if ``from_status → to_status`` is permitted.

    A ``None`` ``from_status`` means the memory has never had its workflow
    set (legacy row or freshly created). It is treated as ``open`` so the
    first transition follows the ``open`` edges.
    """
    if from_status is None:
        from_status = WorkflowStatus.OPEN
    return to_status in ALLOWED_TRANSITIONS.get(from_status, frozenset())


def validate_transition(from_status: WorkflowStatus | None, to_status: WorkflowStatus) -> None:
    """Raise ``WorkflowTransitionError`` if ``from_status → to_status`` is forbidden.

    The error message names both states and the specific forbidden edge
    (e.g. the ``blocked → done`` rule) so callers can surface a precise
    reason rather than a generic "not allowed".
    """
    if transition_allowed(from_status, to_status):
        return
    current = from_status.value if from_status is not None else "open (unset)"
    if from_status == WorkflowStatus.BLOCKED and to_status == WorkflowStatus.DONE:
        # Surface the headline forbidden edge explicitly — this is the
        # rule an agent is most likely to trip when it tries to skip a
        # stuck dependency.
        raise WorkflowTransitionError(
            "forbidden transition: blocked → done. A blocked memory must be "
            "resolved before it can be marked done (blocked → resolved → done)."
        )
    if from_status is not None and is_terminal(from_status):
        raise WorkflowTransitionError(
            f"forbidden transition: {current} → {to_status.value}. "
            f"{current} is a terminal state — no further transitions are permitted."
        )
    source = from_status or WorkflowStatus.OPEN
    allowed = sorted(s.value for s in ALLOWED_TRANSITIONS.get(source, frozenset()))
    raise WorkflowTransitionError(
        f"forbidden transition: {current} → {to_status.value}. "
        f"Allowed from {current}: {allowed or ['(terminal — no transitions)']}."
    )
