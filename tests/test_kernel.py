"""Unit tests for the ATMS kernel invariants."""

from __future__ import annotations

import pytest

from atms_backend.core.atms import ATMS, _is_minimal_new
from atms_backend.core.budgets import Budgets
from atms_backend.core.types import Justification


def build(assumptions, facts, rules, budgets=None):
    atms = ATMS(budgets or Budgets())
    for a in assumptions:
        atms.declare_assumption(a)
    for f in facts:
        atms.add_justification(Justification("fact:" + f, (), f))
    for rid, ants, cons in rules:
        atms.add_justification(Justification(rid, tuple(ants), cons))
    return atms


def E(*xs):
    return frozenset(xs)


def test_facts_hold_under_empty_environment():
    atms = build([], ["P"], [])
    atms.propagate()
    assert atms.holding_environments("P") == [frozenset()]
    assert atms.holds("P", frozenset())


def test_labels_are_subset_minimal():
    # X via A alone and via {A,B}; the superset env must be dropped.
    atms = build(
        ["A", "B"],
        [],
        [("r1", ["A"], "X"), ("r2", ["A", "B"], "X")],
    )
    atms.propagate()
    assert atms.holding_environments("X") == [E("A")]


def test_nogood_and_its_supersets_are_invalid_support():
    atms = build(
        ["A", "B", "C"],
        [],
        [
            ("r1", ["A", "C"], "FALSE"),
            ("r2", ["A"], "X"),
            ("r3", ["A", "B"], "Y"),
        ],
    )
    atms.propagate()
    assert atms.nogoods == [E("A", "C")]
    # X only has env {A}, which is consistent, so it survives.
    assert atms.holding_environments("X") == [E("A")]
    # Y's only env {A,B} is consistent as such ({A,C} not a subset)...
    assert atms.holding_environments("Y") == [E("A", "B")]
    # ...but a context extending the nogood is rejected.
    assert atms.holds("X", E("A", "C")) is False


def test_new_nogood_removes_already_derived_supersets():
    # Order: X receives {A,B} before a later rule reveals {A,B} nogood.
    atms = build(
        ["A", "B"],
        [],
        [("rx", ["A", "B"], "X"), ("rc", ["B", "A"], "FALSE")],
    )
    atms.propagate()
    assert atms.nogoods == [E("A", "B")]
    assert atms.holding_environments("X") == []


def test_two_independent_proofs_both_retained():
    atms = build(
        ["A", "B"],
        ["P"],
        [("r1", ["A", "P"], "X"), ("r2", ["B", "P"], "X"), ("r3", ["X"], "Y")],
    )
    atms.propagate()
    assert atms.holding_environments("X") == [E("A"), E("B")]
    assert atms.holding_environments("Y") == [E("A"), E("B")]


def test_contradiction_minimalization_keeps_only_smallest_nogood():
    atms = build(
        ["A", "B"],
        [],
        [("r1", ["A"], "FALSE"), ("r2", ["A", "B"], "FALSE")],
    )
    atms.propagate()
    assert atms.nogoods == [E("A")]


def test_propagation_reaches_fixpoint_and_is_idempotent():
    atms = build(
        ["A", "B"],
        [],
        [("r1", ["A"], "X"), ("r2", ["B", "X"], "Y"), ("r3", ["Y"], "Z")],
    )
    first = atms.propagate()
    assert first.incomplete is False
    labels_after = {n: list(e) for n, e in atms.labels.items()}
    second = atms.propagate()
    assert second.total_envs == 0  # nothing new on a second pass
    assert {n: list(e) for n, e in atms.labels.items()} == labels_after


def test_label_budget_reports_incomplete_without_exceeding_cap():
    atms = build(
        ["A1", "A2", "A3"],
        [],
        [
            ("r1", ["A1"], "X"),
            ("r2", ["A2"], "X"),
            ("r3", ["A3"], "X"),
        ],
        Budgets(max_label_envs=2, max_total_envs=1000, max_steps=1000),
    )
    result = atms.propagate()
    assert result.incomplete is True
    assert "label_envs" in result.reason
    assert len(atms.labels["X"]) <= 2
    assert atms.incomplete is True


def test_incomplete_state_does_not_claim_absence():
    atms = build(
        ["A1", "A2", "A3"],
        [],
        [
            ("r1", ["A1"], "X"),
            ("r2", ["A2"], "X"),
            ("r3", ["A3"], "ONLY_LATE"),
        ],
        Budgets(max_label_envs=1, max_total_envs=1000, max_steps=1000),
    )
    atms.propagate()
    # X is supported by whichever env landed first (positive result valid).
    assert atms.holds("X") is True
    # The never-derived node must be reported as unknown, not unsupported:
    # callers consult `incomplete`; holds() alone must not be treated as
    # a proven negative.
    assert atms.holds("ONLY_LATE") is False
    assert atms.incomplete is True


def test_steps_budget_bounds_work():
    atms = build(
        ["A1", "A2"],
        [],
        [("r1", ["A1"], "X"), ("r2", ["A2"], "X")],
        Budgets(max_steps=0),
    )
    result = atms.propagate()
    assert result.incomplete is True
    assert "steps" in result.reason


def test_resume_after_budget_eventually_completes_with_larger_budget():
    rules = [("r1", ["A1"], "X"), ("r2", ["A2"], "X")]
    atms = build(["A1", "A2"], [], rules,
                 Budgets(max_label_envs=1, max_total_envs=1000, max_steps=1000))
    atms.propagate()
    assert atms.incomplete is True
    # Hydrate into a fresh, more generously budgeted engine and resume.
    resumed = build([], [], [], Budgets())
    for a in ["A1", "A2"]:
        resumed.declare_assumption(a)
    for rid, ants, cons in rules:
        resumed.add_justification(Justification(rid, tuple(ants), cons))
    resumed.hydrate(
        {n: list(e) for n, e in atms.labels.items()},
        list(atms.nogoods),
        incomplete=True,
        incomplete_reason=atms.incomplete_reason,
    )
    result = resumed.propagate()
    assert result.incomplete is False
    assert resumed.holding_environments("X") == [E("A1"), E("A2")]


def test_empty_premise_false_is_rejected():
    atms = ATMS()
    with pytest.raises(ValueError):
        atms.add_justification(Justification("bad", (), "FALSE"))


def test_is_minimal_new_helper():
    chain = [E("A"), E("B")]
    assert _is_minimal_new(chain, E("C")) is True
    assert _is_minimal_new(chain, E("A", "B")) is False  # subsumed
    assert _is_minimal_new(chain, E("A")) is False  # present
