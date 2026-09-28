"""Tests for the independent replay kernel with concrete expected outcomes."""

from __future__ import annotations

from fractions import Fraction

from tplan.model import (
    EventKind,
    FailureCode,
    Problem,
    ScheduledAction,
)
from tplan.simulator import simulate

from .cases import (
    BOUNDARY_RELEASE,
    MIDPOINT_VIOLATION,
    START_CONDITION,
    ZERO_DURATION,
)


def test_invariant_checked_at_interior_point_not_only_endpoints() -> None:
    """F [0,4) needs x>=1; G drops x to 0 with its end effect at t=2.

    Endpoints t=0 and t=3 look fine; the failure is the interior point t=2.
    """
    problem = Problem.from_dict(MIDPOINT_VIOLATION)
    outcome = simulate(problem, [ScheduledAction("F", 0, 4), ScheduledAction("G", 1, 2)])
    assert outcome.ok is False
    assert outcome.error is not None
    assert outcome.error.code is FailureCode.INVARIANT_VIOLATED
    assert outcome.error.time == 2
    assert "t=2" in outcome.error.message
    assert "[0,4)" in outcome.error.message


def test_no_failure_when_invariant_holds_throughout() -> None:
    problem = Problem.from_dict(MIDPOINT_VIOLATION)
    # G runs only after F has finished: F [0,4), G [4,5).
    outcome = simulate(problem, [ScheduledAction("F", 0, 4), ScheduledAction("G", 4, 5)])
    assert outcome.ok is True
    # State at t=2 still has x=1; G's effect at t=5 reduces it afterwards.
    assert Fraction(outcome.states[2]["x"]) == 1
    assert Fraction(outcome.states[5]["x"]) == 0


def test_half_open_boundary_release_allows_back_to_back_actions() -> None:
    problem = Problem.from_dict(BOUNDARY_RELEASE)
    outcome = simulate(problem, [ScheduledAction("A", 0, 3), ScheduledAction("B", 3, 5)])
    assert outcome.ok is True
    assert outcome.goal_met is True
    # At the shared boundary t=3 the END precedes the START: usage returns to
    # 0 then goes back to 1; it never exceeds capacity.
    end_evt = next(e for e in outcome.events if e.time == 3 and e.kind is EventKind.END)
    start_evt = next(e for e in outcome.events if e.time == 3 and e.kind is EventKind.START)
    assert end_evt.order < start_evt.order
    assert end_evt.resources["robot"] == 0
    assert start_evt.resources["robot"] == 1


def test_overlap_by_one_grid_point_rejected_as_resource_conflict() -> None:
    problem = Problem.from_dict(BOUNDARY_RELEASE)
    # A [0,3) and B [2,4) both hold the robot at t=2 (capacity 1).
    outcome = simulate(problem, [ScheduledAction("A", 0, 3), ScheduledAction("B", 2, 4)])
    # B's start condition also fails (x still 0 at t=2); either way the
    # schedule is rejected with a specific code at t=2 -- never as success.
    assert outcome.ok is False
    assert outcome.error is not None
    assert outcome.error.time == 2
    assert outcome.error.code in {
        FailureCode.RESOURCE_CONFLICT,
        FailureCode.START_CONDITION_VIOLATED,
    }


def test_zero_duration_action_applies_effect_at_single_instant() -> None:
    problem = Problem.from_dict(ZERO_DURATION)
    outcome = simulate(problem, [ScheduledAction("Z", 1, 1)])
    assert outcome.ok is True
    assert outcome.goal_met is True
    tick = [e for e in outcome.events if e.kind is EventKind.TICK]
    assert len(tick) == 1
    assert tick[0].time == 1
    assert Fraction(tick[0].state["x"]) == 5
    # Before the tick x is still 0; afterwards 5 at the same time's end state.
    assert Fraction(outcome.states[0]["x"]) == 0
    assert Fraction(outcome.states[1]["x"]) == 5


def test_zero_duration_start_condition_checked_before_effect() -> None:
    """Z requires x==0 to start and sets x=5; running it at t=1 after x has
    already changed must fail START_CONDITION_VIOLATED."""
    raw = {
        "horizon": 3, "fluents": {"x": 0}, "resources": [],
        "actions": [
            {"id": "Z", "duration": 0,
             "start_condition": [{"fluent": {"id": "x", "op": "==", "value": 0}}],
             "invariant": [], "effects": [{"fluent": "x", "op": "=", "amount": 5}]},
            {"id": "set9", "duration": 1, "start_condition": [], "invariant": [],
             "effects": [{"fluent": "x", "op": "=", "amount": 9}]},
        ],
        "goal": {"all": []},
    }
    problem = Problem.from_dict(raw)
    # set9 finishes at t=1 (effect x=9); the tick at t=2 collides with no
    # other event, so its start condition x==0 is evaluated in state x=9.
    outcome = simulate(problem, [ScheduledAction("set9", 0, 1), ScheduledAction("Z", 2, 2)])
    assert outcome.ok is False
    assert outcome.error is not None
    assert outcome.error.code is FailureCode.START_CONDITION_VIOLATED
    assert outcome.error.time == 2


def test_simultaneous_starts_are_rejected() -> None:
    problem = Problem.from_dict(BOUNDARY_RELEASE)
    outcome = simulate(problem, [ScheduledAction("A", 0, 3), ScheduledAction("B", 0, 2)])
    assert outcome.ok is False
    assert outcome.error is not None
    assert outcome.error.code is FailureCode.SIMULTANEOUS_CONFLICT
    assert outcome.error.time == 0


def test_simultaneous_ends_are_rejected() -> None:
    problem = Problem.from_dict(BOUNDARY_RELEASE)
    outcome = simulate(problem, [ScheduledAction("A", 0, 3), ScheduledAction("B", 1, 3)])
    assert outcome.ok is False
    assert outcome.error is not None
    assert outcome.error.code is FailureCode.SIMULTANEOUS_CONFLICT
    assert outcome.error.time == 3


def test_tick_colliding_with_another_event_rejected() -> None:
    problem = Problem.from_dict(START_CONDITION)
    raw = {
        "horizon": 3, "fluents": {"x": 0}, "resources": [],
        "actions": [
            {"id": "a", "duration": 1, "start_condition": [], "invariant": [],
             "effects": [{"fluent": "x", "op": "+", "amount": 1}]},
            {"id": "z", "duration": 0, "start_condition": [], "invariant": [],
             "effects": [{"fluent": "x", "op": "+", "amount": 1}]},
        ],
        "goal": {"all": []},
    }
    problem = Problem.from_dict(raw)
    outcome = simulate(problem, [ScheduledAction("a", 0, 1), ScheduledAction("z", 1, 1)])
    assert outcome.ok is False
    assert outcome.error is not None
    assert outcome.error.code is FailureCode.SIMULTANEOUS_CONFLICT
    assert outcome.error.time == 1


def test_full_timeline_is_reconstructed_and_ordered() -> None:
    problem = Problem.from_dict(BOUNDARY_RELEASE)
    outcome = simulate(problem, [ScheduledAction("A", 0, 3), ScheduledAction("B", 3, 5)])
    assert outcome.ok
    kinds_in_order = [(e.time, e.order, e.kind) for e in outcome.events]
    assert kinds_in_order[0] == (0, 0, EventKind.INIT)
    # Strict global ordering of sequence numbers.
    orders = [e.order for e in outcome.events]
    assert orders == sorted(orders)
    # Effects appear exactly at the end instant: x=1 first at t=3.
    assert Fraction(outcome.states[2]["x"]) == 0
    assert Fraction(outcome.states[3]["x"]) == 1
    assert Fraction(outcome.states[5]["y"]) == 1


def test_schedule_outside_horizon_rejected() -> None:
    problem = Problem.from_dict(BOUNDARY_RELEASE)
    outcome = simulate(problem, [ScheduledAction("A", 4, 7)])
    assert outcome.ok is False
    assert outcome.error is not None
    assert outcome.error.code is FailureCode.INVALID_PROBLEM


def test_consumable_debit_and_credit() -> None:
    raw = {
        "horizon": 5, "fluents": {"d": 0},
        "resources": [{"id": "fuel", "capacity": 3, "kind": "consumable"}],
        "actions": [
            {"id": "b", "duration": 2, "start_condition": [], "invariant": [],
             "effects": [{"fluent": "d", "op": "+", "amount": 1}],
             "resource_use": [{"resource": "fuel", "amount": 2}]},
        ],
        "goal": {"all": []},
    }
    problem = Problem.from_dict(raw)
    outcome = simulate(problem, [ScheduledAction("b", 0, 2)])
    assert outcome.ok
    start_evt = next(e for e in outcome.events if e.kind is EventKind.START)
    end_evt = next(e for e in outcome.events if e.kind is EventKind.END)
    assert start_evt.resources["fuel"] == 1
    assert end_evt.resources["fuel"] == 3


def test_consumable_overdraft_rejected() -> None:
    raw = {
        "horizon": 6, "fluents": {"d": 0},
        "resources": [{"id": "fuel", "capacity": 3, "kind": "consumable"}],
        "actions": [
            {"id": "b", "duration": 3, "start_condition": [], "invariant": [],
             "effects": [], "resource_use": [{"resource": "fuel", "amount": 2}]},
            {"id": "c", "duration": 1, "start_condition": [], "invariant": [],
             "effects": [], "resource_use": [{"resource": "fuel", "amount": 2}]},
        ],
        "goal": {"all": []},
    }
    problem = Problem.from_dict(raw)
    # b [0,3) holds 2 units (stock 1); c starting at t=1 would overdraw.
    outcome = simulate(problem, [ScheduledAction("b", 0, 3), ScheduledAction("c", 1, 2)])
    assert outcome.ok is False
    assert outcome.error is not None
    assert outcome.error.code is FailureCode.RESOURCE_CONFLICT
    assert outcome.error.time == 1
