"""Child FSM (contract specs/service-lifecycle/v1 §3.3, SL-07).

The normative transition table T1-T14, exactly as written in the spec —
every (state, event) pair outside the table raises :class:`TransitionError`,
so a forbidden transition is impossible BY CONSTRUCTION, not by discipline.

Table notes fixed here (readings the implementation is honest about):

- The spec's T11 row is ``degraded --exit при исчерпанном бюджете--> degraded``.
  SL §3.5 demands the terminal ``degraded`` state on budget exhaustion for
  optional components from ANY live state (an optional component can burn its
  window while in ``starting`` or ``healthy`` — it never degraded via T7).
  The exhaustion exit is therefore modelled as one event family
  (``EXIT`` with ``budget_exhausted=True``): ``degraded -> degraded`` is the
  spec's T11 row; ``starting/healthy -> degraded`` are the same event applied
  from the states the budget can actually run out in. Live-budget exits stay
  exactly T5/T8/T10 (-> backoff).
- ``STOP`` is legal from ``healthy`` / ``degraded`` / ``blocked`` in addition
  to the T6/T13 rows (``starting`` / ``backoff``): a graceful per-component
  stop IS the T14 operation (``любой -> stopped``) applied to one component;
  the two extra rows only let the same operation start from the states a
  stopped component can be found in.
- ``SHUTDOWN`` (T14) is legal from every state including ``stopped -> stopped``
  (self-loop); the supervisor simply does not emit it for already-stopped
  components, so no noise enters the history journal.

Thread safety: the supervision loop and the future control plane (W3) share
one :class:`ChildFsm` — every read and write takes the instance lock.
"""

from __future__ import annotations

import dataclasses
import enum
import threading
import time

__all__ = [
    "TRANSITION_TABLE",
    "ChildFsm",
    "ChildState",
    "FsmEvent",
    "TransitionError",
    "TransitionResult",
]


class ChildState(enum.StrEnum):
    """FSM states (SL §3.3)."""

    STOPPED = "stopped"
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BACKOFF = "backoff"
    BLOCKED = "blocked"


class FsmEvent(enum.StrEnum):
    """Event vocabulary of the transition table (SL §3.3, T1-T14)."""

    SPAWN = "spawn"  # T1: stopped -> starting (start, restart, lazy-retry, manual start)
    DEP_BLOCK = "dep_block"  # T2: stopped -> blocked (core dependency not up)
    DEP_UP = "dep_up"  # T3: blocked -> starting (core dependency healthy)
    HEALTH_OK = "health_ok"  # T4: starting -> healthy (first successful pass — «up»)
    EXIT = "exit"  # T5/T8/T10 (live budget -> backoff); T11 family (exhausted -> degraded)
    STOP = "stop"  # T6/T13 (+T14 family): -> stopped
    HEALTH_FAIL = "health_fail"  # T7: healthy -> degraded (unhealthy threshold)
    HEALTH_RECOVER = "health_recover"  # T9: degraded -> healthy
    RESPAWN_DUE = "respawn_due"  # T12: backoff -> starting (backoff timer expired)
    SHUTDOWN = "shutdown"  # T14: any -> stopped (graceful supervisor shutdown)


_ALL_STATES = frozenset(ChildState)

#: The normative table. ``(state, event, budget_exhausted) -> next state``.
#: ``budget_exhausted`` is only meaningful for :attr:`FsmEvent.EXIT`.
TRANSITION_TABLE: dict[tuple[ChildState, FsmEvent, bool], ChildState] = {
    # T1 — spawn ребёнка (старт, рестарт, lazy-retry, ручной start)
    (ChildState.STOPPED, FsmEvent.SPAWN, False): ChildState.STARTING,
    # T2 — start-запрос при неподнятой core-зависимости
    (ChildState.STOPPED, FsmEvent.DEP_BLOCK, False): ChildState.BLOCKED,
    # T3 — core-зависимость достигла healthy
    (ChildState.BLOCKED, FsmEvent.DEP_UP, False): ChildState.STARTING,
    # T4 — первый успешный health-проход (определение «up»)
    (ChildState.STARTING, FsmEvent.HEALTH_OK, False): ChildState.HEALTHY,
    # T5 — exit процесса до healthy (рестарт-политика тира)
    (ChildState.STARTING, FsmEvent.EXIT, False): ChildState.BACKOFF,
    # T6 — ручной stop / остановка супервайзера
    (ChildState.STARTING, FsmEvent.STOP, False): ChildState.STOPPED,
    # T7 — health-пробы проваливаются (порог деградации)
    (ChildState.HEALTHY, FsmEvent.HEALTH_FAIL, False): ChildState.DEGRADED,
    # T8 — exit процесса при живом рестарт-бюджете
    (ChildState.HEALTHY, FsmEvent.EXIT, False): ChildState.BACKOFF,
    # T9 — восстановление health-прохода (recovery)
    (ChildState.DEGRADED, FsmEvent.HEALTH_RECOVER, False): ChildState.HEALTHY,
    # T10 — exit процесса при живом бюджете
    (ChildState.DEGRADED, FsmEvent.EXIT, False): ChildState.BACKOFF,
    # T11 family — exit при исчерпанном бюджете (optional): терминальная
    # деградация. degraded -> degraded is the literal T11 row;
    # starting/healthy -> degraded are the same exhaustion event from the
    # states the budget can burn out in (see module docstring).
    (ChildState.DEGRADED, FsmEvent.EXIT, True): ChildState.DEGRADED,
    (ChildState.STARTING, FsmEvent.EXIT, True): ChildState.DEGRADED,
    (ChildState.HEALTHY, FsmEvent.EXIT, True): ChildState.DEGRADED,
    # T12 — истечение backoff-таймера -> respawn
    (ChildState.BACKOFF, FsmEvent.RESPAWN_DUE, False): ChildState.STARTING,
    # T13 — ручной stop / остановка супервайзера
    (ChildState.BACKOFF, FsmEvent.STOP, False): ChildState.STOPPED,
    # T14 family — graceful stop applied to one component from every state
    # it can be found in (the T6/T13 rows above name starting/backoff).
    (ChildState.HEALTHY, FsmEvent.STOP, False): ChildState.STOPPED,
    (ChildState.DEGRADED, FsmEvent.STOP, False): ChildState.STOPPED,
    (ChildState.BLOCKED, FsmEvent.STOP, False): ChildState.STOPPED,
}

# T14 — graceful shutdown супервайзера: ЛЮБОЙ -> stopped.
for _state in _ALL_STATES:
    TRANSITION_TABLE[(_state, FsmEvent.SHUTDOWN, False)] = ChildState.STOPPED
del _state


class TransitionError(Exception):
    """A (state, event) pair outside the normative table — forbidden by SL-07."""

    def __init__(
        self,
        name: str,
        from_state: ChildState,
        event: FsmEvent,
        budget_exhausted: bool,
    ) -> None:
        self.name = name
        self.from_state = from_state
        self.event = event
        self.budget_exhausted = budget_exhausted
        super().__init__(
            f"forbidden FSM transition for component {name!r}: "
            f"{from_state.value} --{event.value}"
            f"{'(budget_exhausted)' if budget_exhausted else ''}--> ??? "
            "(no such row in the service-lifecycle §3.3 table)"
        )


@dataclasses.dataclass(frozen=True)
class TransitionResult:
    """One applied transition (the caller emits history/structural lines)."""

    name: str
    from_state: ChildState
    to_state: ChildState
    event: FsmEvent
    at_monotonic: float
    seq: int  # per-FSM sequence number (strict ordering for tests/journal)


class ChildFsm:
    """Thread-safe per-child FSM; forbidden transitions are impossible."""

    def __init__(self, name: str, *, initial: ChildState = ChildState.STOPPED) -> None:
        self._name = name
        self._state = initial
        self._lock = threading.Lock()
        self._seq = 0
        self._last_change_monotonic = time.monotonic()
        self._transition_count = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> ChildState:
        with self._lock:
            return self._state

    @property
    def transition_count(self) -> int:
        with self._lock:
            return self._transition_count

    def in_state(self, *states: ChildState) -> bool:
        with self._lock:
            return self._state in states

    def transition(
        self,
        event: FsmEvent,
        *,
        budget_exhausted: bool = False,
        now: float | None = None,
    ) -> TransitionResult:
        """Apply one table row; raise TransitionError for anything outside it."""
        with self._lock:
            key = (self._state, event, budget_exhausted)
            target = TRANSITION_TABLE.get(key)
            if target is None:
                raise TransitionError(self._name, self._state, event, budget_exhausted)
            result = TransitionResult(
                name=self._name,
                from_state=self._state,
                to_state=target,
                event=event,
                at_monotonic=time.monotonic() if now is None else now,
                seq=self._seq,
            )
            self._seq += 1
            self._state = target
            self._transition_count += 1
            self._last_change_monotonic = result.at_monotonic
            return result

    def snapshot(self) -> dict[str, str]:
        """Board/status-friendly view (W3 control plane reads the same)."""
        with self._lock:
            return {
                "state": self._state.value,
                "transitions": str(self._transition_count),
            }
