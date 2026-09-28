"""All-different filtering: matching, Hall certificates, Régin removals."""
from __future__ import annotations

from csp_service.kernel.alldifferent import filter_all_different
from csp_service.kernel.matching import maximum_matching


def test_maximum_matching_perfect():
    matching = maximum_matching(["x", "y"], {"x": [1, 2], "y": [2]})
    assert len(matching) == 2
    assert matching["y"] == 2
    assert matching["x"] == 1


def test_maximum_matching_deficient():
    matching = maximum_matching(["a", "b", "c"], {"a": [1, 2], "b": [1, 2], "c": [1, 2]})
    assert len(matching) == 2  # 3 vars, 2 values


def test_hall_violation_certificate(fixture_loader):
    problem = fixture_loader("hall_conflict")["problem"]
    domains = {v["name"]: frozenset(v["domain"]) for v in problem["variables"]}
    result = filter_all_different(["a", "b", "c"], domains)
    assert not result.consistent
    assert result.hall.vars == ["a", "b", "c"]
    assert result.hall.values == [1, 2]


def test_hall_set_pruning_without_assignment():
    """x1,x2 consume {1,2}; x3 must lose 1 and 2 even though nothing is
    assigned — pairwise removal of assigned values could never do this."""
    domains = {
        "x1": frozenset({1, 2}),
        "x2": frozenset({1, 2}),
        "x3": frozenset({1, 2, 3}),
    }
    result = filter_all_different(["x1", "x2", "x3"], domains)
    assert result.consistent
    assert sorted(result.removals) == [("x3", 1), ("x3", 2)]


def test_complete_bipartite_keeps_everything():
    domains = {v: frozenset({1, 2, 3}) for v in ("p", "q", "r")}
    result = filter_all_different(["p", "q", "r"], domains)
    assert result.consistent
    assert result.removals == []


def test_nested_hall_sets_prune_across_components():
    """x1,x2 over {1,2}; x3,x4 over {1,2,3,4}: x3,x4 keep 3,4 only."""
    domains = {
        "x1": frozenset({1, 2}),
        "x2": frozenset({1, 2}),
        "x3": frozenset({1, 2, 3, 4}),
        "x4": frozenset({1, 2, 3, 4}),
    }
    result = filter_all_different(["x1", "x2", "x3", "x4"], domains)
    assert result.consistent
    assert sorted(result.removals) == [("x3", 1), ("x3", 2), ("x4", 1), ("x4", 2)]


def test_matching_is_deterministic():
    adjacency = {"b": [3, 1, 2], "a": [2, 1]}
    first = maximum_matching(["a", "b"], adjacency)
    second = maximum_matching(["a", "b"], adjacency)
    assert first == second
