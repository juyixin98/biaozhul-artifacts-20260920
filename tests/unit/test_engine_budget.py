"""Budget behaviour: propagation terminates, and exceeding a budget only
marks results incomplete -- ungenerated environments are never reported
as nonexistent (contract items 3 and 4)."""

from app.config import Budget
from app.core.engine import ATMS
from app.core.types import Rule


def test_label_budget_marks_incomplete_and_status_partial():
    budget = Budget(max_envs_per_label=3)
    engine = ATMS(budget)
    for i in range(5):
        engine.add_assumption(f"A{i}")
        engine.add_rule(Rule(f"r{i}", (f"A{i}",), "X"))

    result = engine.query("X")
    assert result["complete"] is False
    assert result["status"] == "supported-partial"
    assert len(result["environments"]) == 3  # capped, all of them genuine
    assert any("label-budget-exceeded" in r for r in result["incomplete_reasons"])


def test_empty_label_under_incompleteness_is_unknown_not_unsupported():
    budget = Budget(max_envs_per_label=1)
    engine = ATMS(budget)
    engine.add_assumption("A0")
    engine.add_assumption("A1")
    engine.add_rule(Rule("r0", ("A0",), "X"))
    engine.add_rule(Rule("r1", ("A1",), "X"))
    assert engine.incomplete  # second environment busted the label budget

    # A node nothing derives: the engine must not claim "unsupported".
    result = engine.query("never-derived")
    assert result["environments"] == []
    assert result["status"] == "unknown"


def test_propagation_step_budget_terminates():
    budget = Budget(max_propagation_steps=3)
    engine = ATMS(budget)
    # Rules first, premise last: the whole chain then propagates in a
    # single run, which must stop at the step budget.
    for i in range(10):
        engine.add_rule(Rule(f"r{i}", (f"p{i}",), f"p{i+1}"))
    engine.add_premise("p0")

    assert engine.incomplete
    assert any(
        "propagation-step-budget-exceeded" in r for r in engine.incomplete_reasons
    )


def test_combination_budget_marks_incomplete():
    budget = Budget(max_combinations_per_rule=4)
    engine = ATMS(budget)
    for i in range(3):
        engine.add_assumption(f"A{i}")
        engine.add_rule(Rule(f"rx{i}", (f"A{i}",), "X"))
        engine.add_assumption(f"B{i}")
        engine.add_rule(Rule(f"ry{i}", (f"B{i}",), "Y"))
    # label(X) x label(Y) = 3 x 3 = 9 combinations > budget of 4.
    engine.add_rule(Rule("rjoin", ("X", "Y"), "Z"))

    assert engine.incomplete
    assert any(
        "combination-budget-exceeded" in r for r in engine.incomplete_reasons
    )
    # Z may be derivable, but the engine stayed silent rather than guessing.
    assert engine.query("Z")["status"] == "unknown"


def test_complete_run_reports_complete(budget):
    engine = ATMS(budget)
    engine.add_assumption("A")
    engine.add_rule(Rule("r1", ("A",), "B"))
    result = engine.query("B")
    assert result["complete"] is True
    assert result["incomplete_reasons"] == []
