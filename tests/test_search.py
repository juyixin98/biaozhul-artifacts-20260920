"""Backtracking search tests: outcomes, failure classes and restoration."""

from __future__ import annotations

from app.solver import CSPModel, SearchStatus, Solver
from app.fixtures import get_fixture

from tests import oracle
from tests.conftest import log_verdict


def solve_payload(payload: dict, **kwargs):
    return Solver(CSPModel.model_validate(payload)).solve(**kwargs)


def test_hall_conflict_is_unsat_with_hall_failure_kind() -> None:
    result = solve_payload(get_fixture("hall_conflict"))
    assert result.status is SearchStatus.UNSAT
    assert result.failure is not None
    assert result.failure["kind"] == "hall_violation"
    assert result.failure["at"] == "root"
    assert set(result.failure["hall_values"]) == {1, 2}
    log_verdict(
        "test_hall_conflict_is_unsat_with_hall_failure_kind",
        "root Hall violation -> status unsat, failure.kind hall_violation",
    )


def test_deep_backtrack_unsat_failure_kind() -> None:
    payload = get_fixture("deep_backtrack_unsat")
    assert oracle.count_solutions(payload) == 0
    result = solve_payload(payload)
    assert result.status is SearchStatus.UNSAT
    assert result.stats["backtracks"] >= 20, result.stats
    log_verdict(
        "test_deep_backtrack_unsat_failure_kind",
        "enumeration=0 solutions and solver proves unsat after deep search",
        backtracks=result.stats["backtracks"],
    )


def test_multiple_solutions_reports_sat_and_one_valid_solution() -> None:
    payload = get_fixture("multiple_solutions")
    solutions = oracle.enumerate_solutions(payload)
    assert len(solutions) == 6
    result = solve_payload(payload)
    assert result.status is SearchStatus.SAT
    assert result.solution in solutions
    log_verdict(
        "test_multiple_solutions_reports_sat_and_one_valid_solution",
        "one of exactly 6 enumerated permutations returned",
        solution=result.solution,
    )


def test_deep_backtrack_sat_unique_solution() -> None:
    payload = get_fixture("deep_backtrack_sat")
    solutions = oracle.enumerate_solutions(payload)
    assert len(solutions) == 1
    result = solve_payload(payload)
    assert result.status is SearchStatus.SAT
    assert result.solution == solutions[0]
    assert result.stats["backtracks"] >= 10
    log_verdict(
        "test_deep_backtrack_sat_unique_solution",
        "enumeration gives exactly 1 solution; solver finds it after "
        "deep backtracking",
        solution=result.solution,
        backtracks=result.stats["backtracks"],
    )


def test_forced_chain_unique_without_search_nodes() -> None:
    payload = get_fixture("forced_chain")
    result = solve_payload(payload)
    assert result.status is SearchStatus.SAT
    assert result.solution == {"d1": 1, "d2": 2, "d3": 3, "d4": 4}
    assert result.stats["nodes"] == 0  # solved at root by propagation
    log_verdict(
        "test_forced_chain_unique_without_search_nodes",
        "strictly increasing chain + all-different is fully decided at root",
    )


def test_queens_four_matches_enumerated_pair() -> None:
    payload = get_fixture("queens_4")
    solutions = oracle.enumerate_solutions(payload)
    assert len(solutions) == 2
    result = solve_payload(payload)
    assert result.status is SearchStatus.SAT
    assert result.solution in solutions


def test_queens_eight_returns_valid_solution() -> None:
    payload = get_fixture("queens_8")
    result = solve_payload(payload)
    assert result.status is SearchStatus.SAT
    assert oracle._assignment_satisfies(payload, result.solution)
    rows = list(result.solution.values())
    assert len(set(rows)) == 8
    columns = list(result.solution)
    for i, ci in enumerate(columns):
        for j in range(i + 1, len(columns)):
            cj = columns[j]
            assert result.solution[ci] != result.solution[cj]
            assert abs(result.solution[ci] - result.solution[cj]) != j - i
    log_verdict(
        "test_queens_eight_returns_valid_solution",
        "distinct rows and no diagonal clash; enumeration check not "
        "executed (8**8 assignments)",
    )


def test_budget_exhaustion_is_unknown_not_success() -> None:
    # queens_8 genuinely needs search; one node cannot decide it.
    payload = get_fixture("queens_8")
    result = solve_payload(payload, max_nodes=1, max_backtracks=1_000_000)
    assert result.status is SearchStatus.UNKNOWN
    assert result.solution is None
    assert result.failure is not None
    assert result.failure["kind"] == "budget"
    log_verdict(
        "test_budget_exhaustion_is_unknown_not_success",
        "node budget exhausted -> status unknown with budget failure",
    )


def test_backtracking_restores_domains_for_later_values() -> None:
    # If a failed branch corrupted the domains, the solver could miss this
    # solution. Exact property checked against full enumeration anyway.
    payload = get_fixture("deep_backtrack_sat")
    solutions = oracle.enumerate_solutions(payload)
    result = solve_payload(payload)
    assert result.solution == solutions[0]
    rejected = [b for b in result.branches if b["status"] == "rejected"]
    restored = [b for b in result.branches if b["restored"]]
    assert rejected, "this instance must reject branches before succeeding"
    assert len(restored) >= len(rejected)
    log_verdict(
        "test_backtracking_restores_domains_for_later_values",
        "every rejected branch reports restoration; final solution valid",
        rejected=len(rejected),
        restored=len(restored),
    )


def test_reason_trace_documents_pruning_causes() -> None:
    result = solve_payload(get_fixture("forced_chain"))
    assert result.reason_trace, "expected pruning reasons"
    for step in result.reason_trace:
        assert step["constraint"]
        assert step["kind"] in {"binary_support", "alldifferent_matching"}
        assert "detail" in step and step["detail"]
