"""Propagation kernel tests: concrete prunings, Hall detection and reasons."""

from __future__ import annotations

import itertools
import random

import pytest

from app.solver import (
    AllDifferentUnsatisfiable,
    BinaryConstraint,
    BinaryRelation,
    CSPModel,
    ComparisonOp,
    DomainStore,
    EmptyDomain,
    Propagator,
    PropagationStats,
    RelationKind,
    run_propagation,
)
from app.fixtures.catalog import (
    hall_conflict,
    induced_hall_conflict,
    isolated_variable,
)

from tests.conftest import log_verdict
from tests import oracle


def _propagate(model: CSPModel, changed=None):
    store = DomainStore(model.domains)
    stats = PropagationStats()
    trail: list = []
    run_propagation(Propagator(model), store, trail, stats, changed=changed)
    return store, stats, trail


def _model(payload: dict) -> CSPModel:
    return CSPModel.model_validate(payload)


def test_hall_conflict_detected_at_root() -> None:
    model = _model(hall_conflict())
    store = DomainStore(model.domains)
    with pytest.raises(AllDifferentUnsatisfiable) as exc_info:
        run_propagation(Propagator(model), store, [], PropagationStats())
    assert set(exc_info.value.hall_values) == {1, 2}
    assert set(exc_info.value.hall_vars) == {"a", "b", "c"}
    log_verdict(
        "test_hall_conflict_detected_at_root",
        "maximum matching < variables; Hall set {a,b,c} over {1,2}",
        hall_vars=exc_info.value.hall_vars,
        hall_values=exc_info.value.hall_values,
    )


def test_induced_hall_conflict_only_after_binary_propagation() -> None:
    payload = induced_hall_conflict()
    model = _model(payload)
    store = DomainStore(model.domains)
    with pytest.raises(AllDifferentUnsatisfiable):
        run_propagation(Propagator(model), store, [], PropagationStats())
    # The initial bipartite graph itself has a perfect matching; the Hall
    # violation is induced by binary propagation shrinking a,b,c to {1,2}.
    log_verdict(
        "test_induced_hall_conflict_only_after_binary_propagation",
        "binary tables force a,b,c to {1,2}, then matching fails",
    )


def test_isolated_variable_keeps_whole_domain() -> None:
    model = _model(isolated_variable())
    store, stats, _ = _propagate(model)
    assert set(store.domain("lonely")) == {7, 8, 9}
    assert set(store.domain("a")) == {1, 2}
    assert set(store.domain("b")) == {1, 2}
    log_verdict(
        "test_isolated_variable_keeps_whole_domain",
        "no incident propagator -> domain untouched",
        lonely=sorted(store.domain("lonely")),
    )


def test_arc_consistency_prunes_with_binary_reason() -> None:
    model = CSPModel(
        name="arc",
        domains={"x": [1, 2, 3], "y": [1, 2, 3]},
        binary_constraints=[
            BinaryConstraint(
                left="x",
                right="y",
                relation=BinaryRelation(
                    kind=RelationKind.COMPARISON, op=ComparisonOp.LT
                ),
            )
        ],
    )
    store, stats, _ = _propagate(model)
    # x=3 has no larger y; y=1 has no smaller x.
    assert set(store.domain("x")) == {1, 2}
    assert set(store.domain("y")) == {2, 3}
    reason_kinds = {step["kind"] for step in stats.steps}
    assert reason_kinds == {"binary_support"}
    pruned = {(step["variable"], step["value"]) for step in stats.steps}
    assert ("x", 3) in pruned and ("y", 1) in pruned
    for step in stats.steps:
        assert "target_domain" in step["detail"]
    log_verdict(
        "test_arc_consistency_prunes_with_binary_reason",
        "no support for x=3 / y=1 in the x<y relation",
        pruned=sorted(pruned),
    )


def test_arc_wipeout_raises_empty_domain() -> None:
    model = _model(
        {
            "name": "wipe",
            "domains": {"x": [1, 2, 3], "y": [1, 2, 3]},
            "binary_constraints": [
                {"left": "x", "right": "y",
                 "relation": {"kind": "comparison", "pairs": [], "op": "lt"}},
                {"left": "y", "right": "x",
                 "relation": {"kind": "comparison", "pairs": [], "op": "lt"}},
            ],
            "all_different": [],
        }
    )
    store = DomainStore(model.domains)
    with pytest.raises(EmptyDomain):
        run_propagation(Propagator(model), store, [], PropagationStats())


def test_alldifferent_domain_size_pruning() -> None:
    # a=1, b=2 consume values 1 and 2; matching reasoning must remove both
    # from c and d, leaving both on {3,4}. Pairwise deletion of assigned
    # values would produce the same domains here, so this test additionally
    # asserts that the recorded reason kind is the global matching rule.
    model = CSPModel(
        name="ad",
        domains={"a": [1], "b": [2], "c": [1, 2, 3, 4], "d": [1, 2, 3, 4]},
        binary_constraints=[],
        all_different=[["a", "b", "c", "d"]],
    )
    store, stats, _ = _propagate(model)
    assert set(store.domain("c")) == {3, 4}
    assert set(store.domain("d")) == {3, 4}
    ad_reasons = [s for s in stats.steps if s["kind"] == "alldifferent_matching"]
    pruned_edges = {tuple(s["detail"]["removed_edge"]) for s in ad_reasons}
    assert ("c", 1) in pruned_edges and ("d", 2) in pruned_edges
    log_verdict(
        "test_alldifferent_domain_size_pruning",
        "a=1,b=2 consume values 1,2; matching reasoning prunes them from c,d",
        reasons=len(ad_reasons),
    )


def test_tight_hall_set_pruning_beyond_pairwise_deletion() -> None:
    # Nothing is assigned, so pairwise deletion of assigned values would
    # prune nothing. But {a,b} is a tight Hall set over {1,2}, so matching
    # reasoning must remove 1 and 2 from c and d.
    model = CSPModel(
        name="hallset",
        domains={
            "a": [1, 2],
            "b": [1, 2],
            "c": [1, 2, 3, 4, 5],
            "d": [1, 2, 3, 4, 5],
        },
        binary_constraints=[],
        all_different=[["a", "b", "c", "d"]],
    )
    store, stats, _ = _propagate(model)
    assert set(store.domain("a")) == {1, 2}
    assert set(store.domain("b")) == {1, 2}
    assert set(store.domain("c")) == {3, 4, 5}
    assert set(store.domain("d")) == {3, 4, 5}
    assert stats.steps, "expected matching-based prunings"
    assert all(s["kind"] == "alldifferent_matching" for s in stats.steps)
    log_verdict(
        "test_tight_hall_set_pruning_beyond_pairwise_deletion",
        "tight Hall set {a,b} over {1,2} prunes c,d without any assignment",
    )


def test_propagation_soundness_against_oracle_random() -> None:
    """The central soundness property: propagation never removes a value that
    occurs in any enumerated solution."""
    rng = random.Random(777)
    ops = ["eq", "ne", "lt", "le", "gt", "ge"]
    checked = 0
    for case in range(800):
        n = rng.randint(2, 5)
        names = [f"v{i}" for i in range(n)]
        domains = {
            name: sorted(rng.sample(range(0, 5), rng.randint(2, 4)))
            for name in names
        }
        constraints = []
        pairs = list(itertools.combinations(names, 2))
        rng.shuffle(pairs)
        for left, right in pairs[: rng.randint(0, 3)]:
            constraints.append(
                {
                    "left": left,
                    "right": right,
                    "relation": {"kind": "comparison", "pairs": [],
                                 "op": rng.choice(ops)},
                }
            )
        groups = []
        if rng.random() < 0.8:
            groups.append(rng.sample(names, rng.randint(2, n)))
        payload = {
            "name": f"random_{case}",
            "domains": domains,
            "binary_constraints": constraints,
            "all_different": groups,
        }
        viable = oracle.viable_values(payload)
        if not viable or all(not values for values in viable.values()):
            continue
        model = _model(payload)
        store = DomainStore(model.domains)
        try:
            run_propagation(Propagator(model), store, [], PropagationStats())
        except (EmptyDomain, AllDifferentUnsatisfiable):
            assert oracle.satisfiability(payload) == "unsat", payload
            continue
        for variable in names:
            assert viable[variable] <= set(store.domain(variable)), payload
        checked += 1
    assert checked > 500
    log_verdict(
        "test_propagation_soundness_against_oracle_random",
        "800 random models; every remaining domain is a superset of the "
        "enumerated viable-value set",
        satisfiable_checked=checked,
    )
