"""Differential tests against the fully independent test-side oracle.

The oracle in ``tests/oracle.py`` imports nothing from ``app``: it is a
separate implementation of the same documented semantics and a separate
search structure. These tests therefore check the production engine
against a genuinely independent reference rather than answers generated
by the code under test.

Coverage:

* fixture plans + hundreds of randomly generated plans agree on
  validity, categorized/timed violations, final state and every
  state-changing phase transition;
* the three search engines (flat reference, incremental DFS solver and
  the oracle's time-bucketed search) agree on feasibility and optimal
  makespan for fixtures and random small problems.
"""
from __future__ import annotations

import logging
import random

import pytest

from app.planner.enumerate import exhaustive_reference
from app.planner.replay import replay
from app.planner.solver import SolverConfig, solve
from app.rules.models import Plan, ScheduledAction

from . import oracle

logger = logging.getLogger("temporal-planner")


# ---- helpers ----------------------------------------------------------

def problem_to_dict(problem) -> dict:
    return problem.model_dump(mode="json")


def plan_to_dict(plan: Plan) -> dict:
    return plan.model_dump(mode="json")


def state_changes(result) -> list[tuple[int, str, dict]]:
    """The (time, action, post-state) sequence of phases that mutate state."""
    return [
        (e.time, e.action or "", e.state_after)
        for e in result.events
        if e.kind.value in {"END", "ZERO_DURATION"} and e.state_before != e.state_after
    ]


def oracle_state_changes(timeline) -> list[tuple[int, str, dict]]:
    return [
        (e["time"], e["action"] or "", e["after"])
        for e in timeline
        if e["phase"] in {"END", "ZERO"} and e["before"] != e["after"]
    ]


def violation_index(result) -> set[tuple]:
    return {
        (v.category, v.time, v.action or "", v.resource or "")
        for v in result.violations
    }


def oracle_violation_index(answer) -> set[tuple]:
    return {
        (v["category"], v["time"], v.get("action") or "", v.get("resource") or "")
        for v in answer["violations"]
    }


def assert_replay_matches_oracle(problem, plan: Plan, *, case_id: str) -> None:
    production = replay(problem, plan)
    independent = oracle.validate_plan(problem_to_dict(problem), plan_to_dict(plan))

    assert production.is_valid == independent["valid"], (
        f"{case_id}: validity mismatch production={production.is_valid} "
        f"oracle={independent['valid']}\nproduction={production.violations}\n"
        f"oracle={independent['violations']}"
    )
    assert production.goal_satisfied == independent["goal_satisfied"], case_id
    assert production.final_state == independent["final_state"], (
        f"{case_id}: final state mismatch {production.final_state} vs {independent['final_state']}"
    )
    assert violation_index(production) == oracle_violation_index(independent), (
        f"{case_id}: violation mismatch\nprod={sorted(violation_index(production))}\n"
        f"oracle={sorted(oracle_violation_index(independent))}"
    )
    assert state_changes(production) == oracle_state_changes(independent["timeline"]), (
        f"{case_id}: state-changing phase sequence mismatch"
    )


# ---- fixture plans ----------------------------------------------------

@pytest.mark.differential
def test_replay_semantics_match_oracle_on_fixture_scenarios(
    drone_problem, workshop_problem, reactor_problem
) -> None:
    scenarios = {
        "drone-valid": (
            drone_problem,
            Plan(steps=[
                ScheduledAction(action="fly", start=0, duration=2),
                ScheduledAction(action="recharge", start=0, duration=0),
                ScheduledAction(action="fly", start=2, duration=2),
            ]),
        ),
        "workshop-mid-invariant": (
            workshop_problem,
            Plan(steps=[ScheduledAction(action="cut_a", start=0, duration=2),
                        ScheduledAction(action="inspect", start=2, duration=0)]),
        ),
        "reactor-vent-mid": (
            reactor_problem,
            Plan(steps=[ScheduledAction(action="run_pump", start=0, duration=3),
                        ScheduledAction(action="vent", start=1, duration=0)]),
        ),
        "reactor-boundary": (
            reactor_problem,
            Plan(steps=[ScheduledAction(action="run_pump", start=0, duration=3),
                        ScheduledAction(action="cool_down", start=3, duration=1)]),
        ),
    }
    for case_id, (problem, plan) in scenarios.items():
        assert_replay_matches_oracle(problem, plan, case_id=case_id)


# ---- random problem + plan fuzzing ------------------------------------

FACTS = ["f0", "f1", "f2"]
RESOURCES = ["r0", "r1"]


def _cond(rng: random.Random) -> dict:
    fact = rng.choice(FACTS)
    return {"fact": {"fact": fact, "op": rng.choice(["==", ">=", "<="]), "value": rng.randint(0, 3)}}


def _effects(rng: random.Random) -> list[dict]:
    if rng.random() < 0.2:
        return []
    fact = rng.choice(FACTS)
    return [{"fact": fact, "op": rng.choice(["=", "+=", "-="]), "value": rng.randint(0, 2)}]


def make_random_problem(rng: random.Random) -> "object":
    from app.rules.models import Action, Problem

    action_count = rng.randint(2, 4)
    actions = []
    used = set()
    while len(actions) < action_count:
        name = f"a{len(actions)}"
        used.add(name)
        dmin = rng.choice([0, 0, 1, 1, 2])
        dmax = dmin + rng.randint(0, 1)
        resources = rng.sample(RESOURCES, k=rng.randint(0, 1))
        actions.append(
            Action(
                name=name,
                duration_min=dmin,
                duration_max=dmax,
                precondition=_cond(rng) if rng.random() < 0.6 else None,
                invariant=_cond(rng) if rng.random() < 0.6 and dmin >= 0 else None,
                effects=_effects(rng),
                resources=resources,
            )
        )
    return Problem(
        name=f"random-{rng.randint(0, 10**9)}",
        horizon=4,
        initial={fact: rng.randint(0, 2) for fact in FACTS},
        goal=_cond(rng),
        resources=list(RESOURCES),
        actions=actions,
    )


def make_random_plan(rng: random.Random, problem) -> Plan:
    steps = []
    for _ in range(rng.randint(0, 4)):
        action = rng.choice(problem.actions)
        duration = rng.choice(list(action.duration_choices))
        start = rng.randint(0, problem.horizon - duration)
        steps.append(ScheduledAction(action=action.name, start=start, duration=duration))
    return Plan(steps=steps)


@pytest.mark.differential
def test_randomized_plans_match_independent_oracle(run_id: str) -> None:
    rng = random.Random(20260928)
    compared = 0
    for problem_index in range(12):
        problem = make_random_problem(rng)
        for _ in range(25):
            plan = make_random_plan(rng, problem)
            case_id = f"{run_id}/problem{problem_index}/plan{compared}"
            assert_replay_matches_oracle(problem, plan, case_id=case_id)
            compared += 1
    logger.info("run_id=%s differential replay comparisons=%d", run_id, compared)
    assert compared == 300


# ---- three-engine search agreement ------------------------------------

def assert_engines_agree(problem, *, case_id: str) -> None:
    reference = exhaustive_reference(problem, max_steps=5, max_occurrences_per_action=3)
    solver = solve(problem, SolverConfig(budget_nodes=200_000, max_steps=5,
                                         max_occurrences_per_action=3))
    independent = oracle.optimal_plan(
        problem_to_dict(problem), max_steps=5, max_occurrences=3
    )

    assert reference.found == independent["found"], (
        f"{case_id}: reference found={reference.found} oracle found={independent['found']}"
    )
    assert reference.optimal_makespan == independent["makespan"], (
        f"{case_id}: reference makespan={reference.optimal_makespan} "
        f"oracle makespan={independent['makespan']}"
    )
    assert (solver.plan is not None) == reference.found, (
        f"{case_id}: solver feasibility {solver.plan is not None} vs reference {reference.found} "
        f"status={solver.status.value}"
    )
    if reference.found:
        assert solver.makespan == reference.optimal_makespan == independent["makespan"], case_id
        assert solver.optimal, f"{case_id}: solver did not prove optimality within budget"
        # Every engine's winning plan must independently validate.
        assert replay(problem, solver.plan).is_valid
        assert replay(problem, reference.plan).is_valid
        assert oracle.validate_plan(
            problem_to_dict(problem), independent["plan"]
        )["valid"]


@pytest.mark.differential
def test_three_search_engines_agree_on_fixtures(
    drone_problem, workshop_problem, reactor_problem
) -> None:
    assert_engines_agree(drone_problem, case_id="drone")
    assert_engines_agree(workshop_problem, case_id="workshop")
    assert_engines_agree(reactor_problem, case_id="reactor")


@pytest.mark.differential
def test_three_search_engines_agree_on_random_small_problems(run_id: str) -> None:
    rng = random.Random(7777)
    checked = 0
    for index in range(8):
        problem = make_random_problem(rng)
        assert_engines_agree(problem, case_id=f"{run_id}/random{index}")
        checked += 1
    logger.info("run_id=%s three-engine agreements=%d", run_id, checked)
    assert checked == 8
