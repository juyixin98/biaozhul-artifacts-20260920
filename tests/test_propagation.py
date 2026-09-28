"""Propagation engine: arc consistency, reasons, wipeout, budgets."""
from __future__ import annotations

from collections import deque

from csp_service.kernel.propagation import (
    BUDGET_EXCEEDED,
    INCONSISTENT,
    OK,
    Propagator,
    full_queue,
)
from csp_service.model import Problem


def _make(problem_dict):
    problem = Problem(**problem_dict)
    events = []
    propagator = Propagator(problem, lambda kind, payload: events.append(
        {"kind": kind, **payload}))
    domains = {v.name: set(v.domain) for v in problem.variables}
    return problem, propagator, domains, events


def test_arc_consistency_prunes_unsupported(fixture_loader):
    spec = fixture_loader("isolated_variable")["problem"]
    problem, propagator, domains, events = _make(spec)
    outcome = propagator.propagate(domains, full_queue(problem), 1000,
                                   {"node": 0, "depth": 0})
    assert outcome.status == OK
    assert domains["x"] == {1, 2}
    assert domains["y"] == {2, 3}
    assert domains["z"] == {1, 2, 3, 4}  # isolated variable untouched
    prunes = [e for e in events if e["kind"] == "prune"]
    assert sorted((e["var"], e["value"]) for e in prunes) == [("x", 3), ("y", 1)]
    for e in prunes:
        assert e["reason"]["kind"] == "arc_no_support"
        assert e["reason"]["constraint"] == "t_xy"
        assert e["reason"]["other_domain"]  # evidence: supporting domain snapshot


def test_wipeout_detected():
    spec = {
        "name": "wipeout",
        "variables": [{"name": "x", "domain": [1]}, {"name": "y", "domain": [2]}],
        "constraints": [
            {"type": "table", "id": "t", "vars": ["x", "y"], "allowed": []}
        ],
    }
    problem, propagator, domains, events = _make(spec)
    outcome = propagator.propagate(domains, full_queue(problem), 1000,
                                   {"node": 0, "depth": 0})
    assert outcome.status == INCONSISTENT
    assert any(e["kind"] == "wipeout" and e["var"] == "x" for e in events)


def test_propagation_step_budget():
    spec = {
        "name": "budget",
        "variables": [{"name": "x", "domain": [1, 2]}, {"name": "y", "domain": [1, 2]}],
        "constraints": [
            {"type": "table", "id": "t", "vars": ["x", "y"], "allowed": [[1, 1]]}
        ],
    }
    problem, propagator, domains, events = _make(spec)
    outcome = propagator.propagate(domains, full_queue(problem), 0,
                                   {"node": 0, "depth": 0})
    assert outcome.status == BUDGET_EXCEEDED


def test_all_different_reason_carries_matching_certificate():
    spec = {
        "name": "cert",
        "variables": [
            {"name": "x1", "domain": [1, 2]},
            {"name": "x2", "domain": [1, 2]},
            {"name": "x3", "domain": [1, 2, 3]},
        ],
        "constraints": [
            {"type": "all_different", "id": "ad", "vars": ["x1", "x2", "x3"]}
        ],
    }
    problem, propagator, domains, events = _make(spec)
    outcome = propagator.propagate(domains, full_queue(problem), 1000,
                                   {"node": 0, "depth": 0})
    assert outcome.status == OK
    assert domains["x3"] == {3}
    prunes = [e for e in events if e["kind"] == "prune"]
    assert sorted((e["var"], e["value"]) for e in prunes) == [("x3", 1), ("x3", 2)]
    for e in prunes:
        assert e["reason"]["kind"] == "alldifferent_no_max_matching"
        assert set(e["reason"]["matching"]) == {"x1", "x2", "x3"}


def test_queue_drains_to_fixpoint():
    """Chain reaction: pruning x must re-trigger revision of y via the queue."""
    spec = {
        "name": "chain",
        "variables": [
            {"name": "x", "domain": [1, 2]},
            {"name": "y", "domain": [1, 2]},
            {"name": "w", "domain": [2]},
        ],
        "constraints": [
            {"type": "table", "id": "t_xw", "vars": ["x", "w"], "allowed": [[1, 2]]},
            {"type": "table", "id": "t_xy", "vars": ["x", "y"], "allowed": [[1, 2]]},
        ],
    }
    problem, propagator, domains, events = _make(spec)
    queue = full_queue(problem)
    outcome = propagator.propagate(domains, queue, 1000, {"node": 0, "depth": 0})
    assert outcome.status == OK
    assert domains == {"x": {1}, "y": {2}, "w": {2}}
    assert len(queue) == 0  # queue fully drained at fixpoint
