"""Differential tests against the independent brute-force oracle.

The oracle (``tests/oracle.py``) never imports the simulator or solver; it
re-derives feasibility with a different method (interval summation and a
time-stepped loop). These tests therefore cannot pass through self-generated
reference answers:

1. For *every* candidate schedule on the small grids, the oracle verdict and
   the simulator verdict must agree on the failure category.
2. The solver's proven optimum (cost, goal time) must equal the oracle's
   exhaustively enumerated optimum.
"""

from __future__ import annotations

import pytest

from tplan.model import FailureCode, Problem
from tplan.solver import Solver
from tplan.simulator import simulate

from . import oracle
from .cases import ORACLE_CASES

# Oracle verdict string -> expected simulator failure code.
CATEGORY_MAP = {
    oracle.OracleVerdict.FEASIBLE: None,
    oracle.OracleVerdict.START_CONDITION: FailureCode.START_CONDITION_VIOLATED,
    oracle.OracleVerdict.INVARIANT: FailureCode.INVARIANT_VIOLATED,
    oracle.OracleVerdict.RESOURCE: FailureCode.RESOURCE_CONFLICT,
    oracle.OracleVerdict.SIMULTANEOUS: FailureCode.SIMULTANEOUS_CONFLICT,
}

# Fractional accumulation needs the action three times; other cases at most once.
REPEATS = {"fractional": 3}


@pytest.mark.parametrize("label,raw", ORACLE_CASES, ids=[c[0] for c in ORACLE_CASES])
def test_simulator_matches_oracle_on_every_candidate(label: str, raw: dict) -> None:
    problem = Problem.from_dict(raw)
    repeats = REPEATS.get(label, 1)
    candidates = oracle.enumerate_schedules(problem, max_repeats=repeats)
    assert candidates, "oracle must enumerate at least the empty schedule"

    agreements = 0
    for instances in candidates:
        verdict, _otime = oracle.check_schedule(problem, instances)
        outcome = simulate(problem, oracle.to_scheduled(instances))
        expected = CATEGORY_MAP[verdict]
        if expected is None:
            assert outcome.ok, (
                f"[{label}] oracle says feasible but simulator rejected "
                f"{instances}: {outcome.error}"
            )
        else:
            assert not outcome.ok, f"[{label}] oracle rejected {instances} ({verdict}) but simulator accepted"
            assert outcome.error is not None
            assert outcome.error.code is expected, (
                f"[{label}] category mismatch for {instances}: "
                f"oracle={verdict} simulator={outcome.error.code}"
            )
        agreements += 1
    # Every enumerated candidate was classified by both engines.
    assert agreements == len(candidates)
    assert agreements >= 1


@pytest.mark.parametrize("label,raw", ORACLE_CASES, ids=[c[0] for c in ORACLE_CASES])
def test_solver_optimum_equals_exhaustive_oracle(label: str, raw: dict) -> None:
    problem = Problem.from_dict(raw)
    repeats = REPEATS.get(label, 1)
    expected = oracle.optimal(problem, max_repeats=repeats)

    result = Solver(problem, max_repeats=repeats).solve()

    if expected is None:
        assert result.status == "unsat"
        assert result.failure_code is FailureCode.UNSAT_PROVEN
    else:
        assert result.status == "optimal", result.message
        exp_cost, exp_time = expected
        assert result.best_cost == exp_cost
        assert result.goal_time == exp_time
        # Independently re-derive feasibility of the solver's own schedule.
        instances = [(s.action_id, s.start, s.end) for s in result.schedule]
        verdict, _ = oracle.check_schedule(problem, instances)
        assert verdict is oracle.OracleVerdict.FEASIBLE
        assert oracle.goal_time(problem, instances) == exp_time


@pytest.mark.slow
def test_medium_grid_exhaustive_agreement() -> None:
    """A slightly larger grid with two capacity-1 users: thousands of
    candidates, all classifications must still agree."""
    raw = {
        "horizon": 8,
        "fluents": {"x": 0},
        "resources": [{"id": "r", "capacity": 1, "kind": "renewable"}],
        "actions": [
            {"id": "a", "duration": 2, "start_condition": [], "invariant": [],
             "effects": [{"fluent": "x", "op": "+", "amount": 1}],
             "resource_use": [{"resource": "r", "amount": 1}]},
            {"id": "b", "duration": 1,
             "start_condition": [{"fluent": {"id": "x", "op": ">=", "value": 1}}],
             "invariant": [], "effects": [],
             "resource_use": [{"resource": "r", "amount": 1}]},
        ],
        "goal": {"all": []},
    }
    problem = Problem.from_dict(raw)
    candidates = oracle.enumerate_schedules(problem, max_repeats=2)
    assert len(candidates) > 1000
    for instances in candidates:
        verdict, _ = oracle.check_schedule(problem, instances)
        outcome = simulate(problem, oracle.to_scheduled(instances))
        expected = CATEGORY_MAP[verdict]
        if expected is None:
            assert outcome.ok, instances
        else:
            assert outcome.ok is False and outcome.error is not None
            assert outcome.error.code is expected, instances
