"""Child FSM conformance (service-lifecycle v1 §3.3, SL-07).

The normative table is exercised EXHAUSTIVELY: every (state, event,
budget_exhausted) triple outside the table must raise, every row inside
it must produce the mandated target state. Illegal transitions are
impossible by construction — SL-07.
"""

from __future__ import annotations

import threading

import pytest

from vesmaro.service.fsm import (
    TRANSITION_TABLE,
    ChildFsm,
    ChildState,
    FsmEvent,
    TransitionError,
)

ALL_EVENTS = list(FsmEvent)


# ── The T1-T14 rows themselves ────────────────────────────────────────


class TestNormativeRows:
    def test_t1_spawn(self) -> None:
        fsm = ChildFsm("a")
        result = fsm.transition(FsmEvent.SPAWN)
        assert (result.from_state, result.to_state) == (ChildState.STOPPED, ChildState.STARTING)

    def test_t2_dep_block(self) -> None:
        fsm = ChildFsm("a")
        assert fsm.transition(FsmEvent.DEP_BLOCK).to_state is ChildState.BLOCKED

    def test_t3_dep_up(self) -> None:
        fsm = ChildFsm("a")
        fsm.transition(FsmEvent.DEP_BLOCK)
        assert fsm.transition(FsmEvent.DEP_UP).to_state is ChildState.STARTING

    def test_t4_health_ok(self) -> None:
        fsm = ChildFsm("a")
        fsm.transition(FsmEvent.SPAWN)
        assert fsm.transition(FsmEvent.HEALTH_OK).to_state is ChildState.HEALTHY

    def test_t5_exit_before_healthy(self) -> None:
        fsm = ChildFsm("a")
        fsm.transition(FsmEvent.SPAWN)
        assert fsm.transition(FsmEvent.EXIT).to_state is ChildState.BACKOFF

    def test_t6_stop_from_starting(self) -> None:
        fsm = ChildFsm("a")
        fsm.transition(FsmEvent.SPAWN)
        assert fsm.transition(FsmEvent.STOP).to_state is ChildState.STOPPED

    def test_t7_health_fail(self) -> None:
        fsm = ChildFsm("a")
        fsm.transition(FsmEvent.SPAWN)
        fsm.transition(FsmEvent.HEALTH_OK)
        assert fsm.transition(FsmEvent.HEALTH_FAIL).to_state is ChildState.DEGRADED

    def test_t8_exit_from_healthy(self) -> None:
        fsm = ChildFsm("a")
        fsm.transition(FsmEvent.SPAWN)
        fsm.transition(FsmEvent.HEALTH_OK)
        assert fsm.transition(FsmEvent.EXIT).to_state is ChildState.BACKOFF

    def test_t9_recovery(self) -> None:
        fsm = ChildFsm("a")
        fsm.transition(FsmEvent.SPAWN)
        fsm.transition(FsmEvent.HEALTH_OK)
        fsm.transition(FsmEvent.HEALTH_FAIL)
        assert fsm.transition(FsmEvent.HEALTH_RECOVER).to_state is ChildState.HEALTHY

    def test_t10_exit_from_degraded_live_budget(self) -> None:
        fsm = ChildFsm("a")
        fsm.transition(FsmEvent.SPAWN)
        fsm.transition(FsmEvent.HEALTH_OK)
        fsm.transition(FsmEvent.HEALTH_FAIL)
        assert fsm.transition(FsmEvent.EXIT).to_state is ChildState.BACKOFF

    def test_t11_exit_exhausted_budget_terminal_degraded(self) -> None:
        for start in (ChildState.STARTING, ChildState.HEALTHY, ChildState.DEGRADED):
            fsm = ChildFsm("a")
            if start is ChildState.HEALTHY:
                fsm.transition(FsmEvent.SPAWN)
                fsm.transition(FsmEvent.HEALTH_OK)
            elif start is ChildState.DEGRADED:
                fsm.transition(FsmEvent.SPAWN)
                fsm.transition(FsmEvent.HEALTH_OK)
                fsm.transition(FsmEvent.HEALTH_FAIL)
            else:
                fsm.transition(FsmEvent.SPAWN)
            assert fsm.transition(FsmEvent.EXIT, budget_exhausted=True).to_state is (
                ChildState.DEGRADED
            )
        # T11 literal row: degraded -> degraded is a legal self-loop.
        fsm = ChildFsm("a")
        fsm.transition(FsmEvent.SPAWN)
        fsm.transition(FsmEvent.EXIT, budget_exhausted=True)
        assert fsm.transition(FsmEvent.EXIT, budget_exhausted=True).to_state is (
            ChildState.DEGRADED
        )

    def test_t12_respawn_due(self) -> None:
        fsm = ChildFsm("a")
        fsm.transition(FsmEvent.SPAWN)
        fsm.transition(FsmEvent.EXIT)
        assert fsm.transition(FsmEvent.RESPAWN_DUE).to_state is ChildState.STARTING

    def test_t13_stop_from_backoff(self) -> None:
        fsm = ChildFsm("a")
        fsm.transition(FsmEvent.SPAWN)
        fsm.transition(FsmEvent.EXIT)
        assert fsm.transition(FsmEvent.STOP).to_state is ChildState.STOPPED

    def test_t14_shutdown_from_every_state(self) -> None:
        for state in ChildState:
            fsm = ChildFsm("a", initial=state)
            assert fsm.transition(FsmEvent.SHUTDOWN).to_state is ChildState.STOPPED

    def test_stop_is_t14_from_live_states(self) -> None:
        # The per-component graceful stop (T14 applied to one component)
        # is legal from every state a live component can be found in.
        for state in (ChildState.HEALTHY, ChildState.DEGRADED, ChildState.BLOCKED):
            fsm = ChildFsm("a", initial=state)
            assert fsm.transition(FsmEvent.STOP).to_state is ChildState.STOPPED


# ── Exhaustive legality fuzz (SL-07: forbidden transitions impossible) ─


class TestExhaustiveFuzz:
    def test_every_triple_is_table_row_or_raises(self) -> None:
        for state in ChildState:
            for event in ALL_EVENTS:
                for budget in (False, True):
                    fsm = ChildFsm("fuzz", initial=state)
                    expected = TRANSITION_TABLE.get((state, event, budget))
                    if expected is None:
                        with pytest.raises(TransitionError):
                            fsm.transition(event, budget_exhausted=budget)
                        assert fsm.state is state  # refusal left it untouched
                    else:
                        result = fsm.transition(event, budget_exhausted=budget)
                        assert result.to_state is expected
                        assert fsm.state is expected

    def test_transition_error_carries_context(self) -> None:
        fsm = ChildFsm("ctx")
        with pytest.raises(TransitionError) as excinfo:
            fsm.transition(FsmEvent.HEALTH_OK)  # stopped -> healthy is illegal
        assert excinfo.value.name == "ctx"
        assert excinfo.value.from_state is ChildState.STOPPED
        assert excinfo.value.event is FsmEvent.HEALTH_OK


# ── Mechanics ─────────────────────────────────────────────────────────


class TestMechanics:
    def test_sequence_numbers_are_strictly_ordered(self) -> None:
        fsm = ChildFsm("seq")
        results = [
            fsm.transition(event)
            for event in (FsmEvent.SPAWN, FsmEvent.EXIT, FsmEvent.RESPAWN_DUE)
        ]
        assert [r.seq for r in results] == [0, 1, 2]

    def test_thread_safety_smoke(self) -> None:
        fsms = [ChildFsm(f"c{i}") for i in range(8)]
        errors: list[Exception] = []

        def worker(fsm: ChildFsm) -> None:
            try:
                for _ in range(50):
                    fsm.transition(FsmEvent.SPAWN)
                    fsm.transition(FsmEvent.HEALTH_OK)
                    fsm.transition(FsmEvent.STOP)
            except Exception as exc:  # pragma: no cover - failure reporting
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(fsm,)) for fsm in fsms for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        assert all(fsm.transition_count == 300 for fsm in fsms)
