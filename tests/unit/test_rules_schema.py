"""Rule-language validation tests."""

import pytest
from pydantic import ValidationError

from app.rules.schema import AssumptionSpec, PremiseSpec, RuleSpec


def test_valid_rule_accepted():
    spec = RuleSpec(rule_id="r1", antecedents=["A", "B"], consequent="C")
    rule = spec.to_rule()
    assert rule.antecedents == ("A", "B")
    assert rule.consequent == "C"


def test_contradiction_allowed_as_consequent():
    spec = RuleSpec(rule_id="rx", antecedents=["A"], consequent="⊥")
    assert spec.to_rule().consequent == "⊥"


def test_contradiction_rejected_as_assumption_and_premise():
    with pytest.raises(ValidationError):
        AssumptionSpec(name="⊥")
    with pytest.raises(ValidationError):
        PremiseSpec(node="⊥")


def test_invalid_symbol_names_rejected():
    with pytest.raises(ValidationError):
        RuleSpec(rule_id="r1", antecedents=["bad name!"], consequent="C")
    with pytest.raises(ValidationError):
        AssumptionSpec(name="")


def test_duplicate_antecedents_rejected():
    with pytest.raises(ValidationError):
        RuleSpec(rule_id="r1", antecedents=["A", "A"], consequent="C")
