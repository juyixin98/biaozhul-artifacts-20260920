"""Solver: statuses, budgets, backtracking restore, multi-solution search."""
from __future__ import annotations

from csp_service.kernel.solver import SAT, UNKNOWN, UNSAT, SolveConfig, Solver
from csp_service.model import Problem


def _solve(spec, **config_kwargs):
    return Solver(Problem(**spec), SolveConfig(**config_kwargs)).solve()


def test_hall_conflict_unsat_at_root(fixture_loader):
    result = _solve(fixture_loader("hall_conflict")["problem"], mode="all")
    assert result.status == UNSAT
    assert result.solutions == []
    assert result.stats["nodes"] == 0  # failed in root propagation
    halls = [e for e in result.events if e["kind"] == "hall_violation"]
    assert halls and halls[0]["vars"] == ["a", "b", "c"]


def test_multi_solution_count(fixture_loader):
    result = _solve(fixture_loader("multi_solution")["problem"], mode="all")
    assert result.status == SAT
    assert len(result.solutions) == 6
    values = [tuple(sorted(s.items())) for s in result.solutions]
    assert len(set(values)) == 6  # no duplicates


def test_first_mode_stops_at_one(fixture_loader):
    result = _solve(fixture_loader("multi_solution")["problem"], mode="first")
    assert result.status == SAT
    assert len(result.solutions) == 1


def test_backtracking_restores_domains(fixture_loader):
    """If restore were broken, branches after the first would see shrunken
    domains and the 53-solution count could not be reached."""
    result = _solve(fixture_loader("deep_backtrack")["problem"], mode="all")
    assert result.status == SAT
    assert len(result.solutions) == 53
    assert result.stats["backtracks"] >= 1
    assert result.stats["max_depth"] >= 4
    for sol in result.solutions:
        assert len(set(sol.values())) == 5  # all-different respected


def test_restore_visible_across_sibling_branches():
    """x=1 forces y=2; after backtracking, x=2 must still allow y=1."""
    spec = {
        "name": "siblings",
        "variables": [{"name": "x", "domain": [1, 2]}, {"name": "y", "domain": [1, 2]}],
        "constraints": [
            {"type": "table", "id": "neq", "vars": ["x", "y"],
             "allowed": [[1, 2], [2, 1]]}
        ],
    }
    result = _solve(spec, mode="all")
    assert result.status == SAT
    assert {tuple(sorted(s.items())) for s in result.solutions} == {
        (("x", 1), ("y", 2)), (("x", 2), ("y", 1)),
    }


def test_node_budget_gives_unknown_not_unsat(fixture_loader):
    result = _solve(fixture_loader("deep_backtrack")["problem"],
                    mode="all", max_nodes=1)
    assert result.status == UNKNOWN
    assert result.partial
    assert any(e["kind"] == "budget_exceeded" and e["budget"] == "nodes"
               for e in result.events)


def test_propagation_budget_gives_unknown(fixture_loader):
    result = _solve(fixture_loader("deep_backtrack")["problem"],
                    mode="all", max_propagation_steps=1)
    assert result.status == UNKNOWN
    assert result.partial


def test_statuses_are_distinct(fixture_loader):
    """The three terminal statuses must be reachable and mutually exclusive."""
    unsat = _solve(fixture_loader("hall_conflict")["problem"])
    sat = _solve(fixture_loader("multi_solution")["problem"], mode="first")
    unknown = _solve(fixture_loader("deep_backtrack")["problem"], max_nodes=1)
    assert {unsat.status, sat.status, unknown.status} == {SAT, UNSAT, UNKNOWN}
