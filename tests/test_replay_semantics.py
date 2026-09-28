"""Concrete timeline semantics: mid-action invariants, zero duration,
boundary release and simultaneous-event policy.

Every test asserts specific outcomes and specific failure categories at
specific times -- never merely "the API can be called".
"""
from __future__ import annotations

import logging

import pytest

from app.rules.errors import FailureCategory
from app.rules.models import (
    Action,
    CheckOutcome,
    EventKind,
    Plan,
    Problem,
    ScheduledAction,
)
from app.planner.replay import replay

logger = logging.getLogger("temporal-planner")


def step(action: str, start: int, duration: int) -> ScheduledAction:
    return ScheduledAction(action=action, start=start, duration=duration)


def categories(result) -> dict[str, list[tuple[int, str, str | None]]]:
    grouped: dict[str, list[tuple[int, str, str | None]]] = {}
    for violation in result.violations:
        grouped.setdefault(violation.category, []).append(
            (violation.time, violation.action or "", violation.resource)
        )
    return grouped


# ---------------------------------------------------------------------
# Duration invariants must hold at EVERY interior grid point
# ---------------------------------------------------------------------

@pytest.mark.semantics
def test_pump_alone_is_valid_and_checks_three_interior_points(reactor_problem) -> None:
    plan = Plan(steps=[step("run_pump", 0, 3)])
    result = replay(reactor_problem, plan)

    assert result.outcome == CheckOutcome.VALID
    assert result.goal_satisfied
    assert result.final_state["pumped"] == 1
    inv_events = [
        e for e in result.events
        if e.kind == EventKind.INVARIANT_CHECK and e.action == "run_pump"
    ]
    assert [e.time for e in inv_events] == [0, 1, 2]
    assert all("OK" in (e.detail or "") for e in inv_events)


@pytest.mark.semantics
def test_mid_action_invariant_failure_is_detected_at_interior_point(reactor_problem) -> None:
    # Zero-duration vent at t=1 drains coolant from 2 to 0. The pump
    # started at t=0 with coolant=2 (precondition and the t=0 segment
    # were fine); the collapse happens on interior segment [1,2).
    plan = Plan(steps=[step("run_pump", 0, 3), step("vent", 1, 0)])
    result = replay(reactor_problem, plan)
    logger.info("mid-action scenario violations=%s", [v.model_dump() for v in result.violations])

    assert result.outcome == CheckOutcome.INVALID
    invariant_hits = categories(result).get(FailureCategory.INVARIANT_VIOLATION, [])
    assert (1, "run_pump", None) in invariant_hits
    # The failure is at the MIDDLE point t=1, not at start t=0 / end t=3.
    assert all(t != 0 and t != 3 for t, _, _ in invariant_hits)
    # The effect did apply (no exception masking), yet the plan is invalid.
    assert result.final_state["coolant"] == 0
    # The goal can be numerically true while the plan stays invalid.
    assert result.final_state["pumped"] == 1


@pytest.mark.semantics
def test_end_effect_knocks_down_invariant_of_still_running_action(reactor_problem) -> None:
    # cool_down [0,2) releases its -2 effect when it ENDS at t=2; the
    # pump [0,3) is still active on segment [2,3) -> invariant failure.
    plan = Plan(steps=[step("run_pump", 0, 3), step("cool_down", 0, 2)])
    result = replay(reactor_problem, plan)

    assert result.outcome == CheckOutcome.INVALID
    assert (2, "run_pump", None) in categories(result).get(FailureCategory.INVARIANT_VIOLATION, [])


# ---------------------------------------------------------------------
# Half-open resource occupation and boundary release
# ---------------------------------------------------------------------

@pytest.mark.semantics
def test_boundary_release_allows_back_to_back_resource_use(workshop_problem) -> None:
    plan = Plan(steps=[step("cut_a", 0, 2), step("cut_b", 2, 2)])
    result = replay(workshop_problem, plan)

    assert result.outcome == CheckOutcome.INVALID  # goal also needs polished
    assert FailureCategory.RESOURCE_CONFLICT not in categories(result)
    assert result.final_state["pieces"] == 2


@pytest.mark.semantics
def test_overlap_by_a_single_grid_point_is_a_resource_conflict(workshop_problem) -> None:
    plan = Plan(steps=[step("cut_a", 0, 2), step("cut_b", 1, 2)])
    result = replay(workshop_problem, plan)

    conflicts = categories(result).get(FailureCategory.RESOURCE_CONFLICT, [])
    assert (1, "cut_a", "saw") in conflicts


@pytest.mark.semantics
def test_zero_duration_action_holds_no_resource_even_if_declared() -> None:
    problem = Problem(
        name="zero-resource",
        horizon=3,
        initial={"level": 1},
        goal={"fact": {"fact": "level", "op": "==", "value": 2}},
        resources=["rig"],
        actions=[
            Action(
                name="hold",
                duration_min=2,
                resources=["rig"],
                effects=[{"fact": "level", "op": "+=", "value": 1}],
            ),
            Action(
                name="blip",
                duration_min=0,  # [1,1) is empty
                resources=["rig"],
                effects=[{"fact": "level", "op": "+=", "value": 0}],
            ),
        ],
    )
    plan = Plan(steps=[step("hold", 0, 2), step("blip", 1, 0)])
    result = replay(problem, plan)
    assert FailureCategory.RESOURCE_CONFLICT not in categories(result)
    assert result.outcome == CheckOutcome.VALID


# ---------------------------------------------------------------------
# Zero-duration actions and the fixed same-time phase order
# ---------------------------------------------------------------------

@pytest.mark.semantics
def test_zero_duration_effect_visible_only_to_later_starts(workshop_problem) -> None:
    # inspect@0 (zero), polish starts at t=1: sees inspected=1 -> valid.
    good = Plan(steps=[
        step("cut_a", 0, 2), step("cut_b", 2, 2),
        step("inspect", 0, 0), step("polish", 1, 1),
    ])
    good_result = replay(workshop_problem, good)
    assert good_result.outcome == CheckOutcome.VALID, good_result.violations
    assert good_result.final_state == {"pieces": 2, "inspected": 1, "polished": 1}

    # Same-time polish@0 reads the pre-ZERO state (inspected=0) -> fail.
    bad = Plan(steps=[step("inspect", 0, 0), step("polish", 0, 1)])
    bad_result = replay(workshop_problem, bad)
    assert (0, "polish", None) in categories(bad_result).get(
        FailureCategory.PRECONDITION_VIOLATION, []
    )


@pytest.mark.semantics
def test_event_phase_order_at_one_timestamp_is_end_pre_zero_invariant(workshop_problem) -> None:
    # At t=2: cut_a ends (END), cut_b starts (PRE/START). At t=0 inspect
    # fires (ZERO). Collect the global (time, order) sequence by kind.
    plan = Plan(steps=[step("cut_a", 0, 2), step("cut_b", 2, 2), step("inspect", 0, 0)])
    result = replay(workshop_problem, plan)

    at_zero = [e.kind for e in result.events if e.time == 0]
    assert at_zero[0] == EventKind.INITIAL
    # ZERO must come after any START(pre) event at t=0, and before INV.
    assert at_zero.index(EventKind.START) < at_zero.index(EventKind.ZERO_DURATION)

    at_two = [e.kind for e in result.events if e.time == 2]
    assert at_two.index(EventKind.END) < at_two.index(EventKind.START)
    assert at_two.index(EventKind.START) < at_two.index(EventKind.INVARIANT_CHECK)


@pytest.mark.semantics
def test_zero_duration_state_transition_is_recorded(workshop_problem) -> None:
    plan = Plan(steps=[step("inspect", 2, 0)])
    result = replay(workshop_problem, plan)
    event = next(e for e in result.events if e.kind == EventKind.ZERO_DURATION and e.action == "inspect")
    assert event.time == 2
    assert event.state_before["inspected"] == 0
    assert event.state_after["inspected"] == 1


# ---------------------------------------------------------------------
# Simultaneous events: reject ambiguous write order
# ---------------------------------------------------------------------

@pytest.mark.semantics
def test_distinct_zero_actions_writing_same_fact_at_same_time_rejected(workshop_problem) -> None:
    plan = Plan(steps=[step("inspect", 2, 0), step("stamp", 2, 0)])
    result = replay(workshop_problem, plan)
    clashes = categories(result).get(FailureCategory.SIMULTANEOUS_CONFLICT, [])
    assert any(t == 2 and fact == "inspected" for t, _a, fact in clashes)


@pytest.mark.semantics
def test_two_positive_actions_ending_together_writing_same_fact_rejected() -> None:
    problem = Problem(
        name="same-end",
        horizon=3,
        initial={"x": 0},
        goal={"fact": {"fact": "x", "op": "==", "value": 2}},
        resources=[],
        actions=[
            Action(name="a", duration_min=2, effects=[{"fact": "x", "op": "+=", "value": 1}]),
            Action(name="b", duration_min=2, effects=[{"fact": "x", "op": "+=", "value": 1}]),
        ],
    )
    plan = Plan(steps=[step("a", 0, 2), step("b", 0, 2)])
    result = replay(problem, plan)
    clashes = categories(result).get(FailureCategory.SIMULTANEOUS_CONFLICT, [])
    assert (2, "a", "x") in clashes or (2, "b", "x") in clashes
    assert result.outcome == CheckOutcome.INVALID


@pytest.mark.semantics
def test_goal_failure_is_its_own_category_not_a_generic_error(drone_problem) -> None:
    result = replay(drone_problem, Plan(steps=[]))
    assert result.outcome == CheckOutcome.INVALID
    assert (5, "", None) in categories(result).get(FailureCategory.GOAL_NOT_REACHED, [])


@pytest.mark.semantics
def test_schedule_invalid_when_action_unknown_or_duration_out_of_range(reactor_problem) -> None:
    plan = Plan(steps=[
        ScheduledAction(action="ghost", start=0, duration=1),
        step("run_pump", 1, 2),  # declared duration is exactly 3
    ])
    result = replay(reactor_problem, plan)
    cats = categories(result)
    assert FailureCategory.SCHEDULE_INVALID in cats
    # duration-2 pump ending at 3 would under-occupy; replay must flag it.
    assert any("duration" in v.message for v in result.violations
               if v.category == FailureCategory.SCHEDULE_INVALID)
