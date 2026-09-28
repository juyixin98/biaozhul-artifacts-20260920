"""Unit tests for the rule language parser/validator."""

from __future__ import annotations

import pytest

from atms_backend.rules.language import RuleLanguageError, parse_rules


def test_parses_assumptions_facts_and_rules():
    rs = parse_rules(
        "assume A, B\nfact P\nrule r1: A, P => X\nrule r2: B => FALSE\n"
    )
    assert rs.assumptions == ("A", "B")
    assert rs.facts == ("P",)
    assert rs.rules[0].rule_id == "r1"
    assert rs.rules[0].antecedents == ("A", "P")
    assert rs.rules[0].consequent == "X"
    assert rs.rules[1].consequent == "FALSE"
    assert rs.rules[0].is_premise is False


def test_comments_and_blank_lines_are_ignored():
    rs = parse_rules("# a comment\n\nassume A  # trailing\n")
    assert rs.assumptions == ("A",)


def test_reports_line_number_on_bad_statement():
    with pytest.raises(RuleLanguageError) as exc:
        parse_rules("assume A\nnonsense line without colon\n")
    assert exc.value.line_no == 2


def test_false_cannot_be_assumption_or_fact():
    with pytest.raises(RuleLanguageError):
        parse_rules("assume FALSE\n")
    with pytest.raises(RuleLanguageError):
        parse_rules("fact FALSE\n")


def test_unknown_antecedent_is_rejected():
    with pytest.raises(RuleLanguageError) as exc:
        parse_rules("assume A\nrule r1: A, Q => X\n")
    assert "Q" in str(exc.value)


def test_duplicate_rule_and_assumption_rejected():
    with pytest.raises(RuleLanguageError):
        parse_rules("assume A, A\n")
    with pytest.raises(RuleLanguageError):
        parse_rules("assume A\nrule r1: A => X\nrule r1: A => Y\n")


def test_rule_without_antecedent_must_use_fact():
    with pytest.raises(RuleLanguageError):
        parse_rules("rule r1: => X\n")


def test_globally_inconsistent_facts_rejected():
    with pytest.raises(RuleLanguageError):
        parse_rules("fact P\nfact Q\nrule r1: P, Q => FALSE\n")


def test_globally_inconsistent_facts_through_chain_rejected():
    # P -> R -> S; R,S -> FALSE: facts alone are inconsistent via a chain.
    with pytest.raises(RuleLanguageError) as exc:
        parse_rules(
            "fact P\n"
            "rule r1: P => R\nrule r2: R => S\nrule r3: R, S => FALSE\n"
        )
    assert "globally inconsistent" in str(exc.value)


def test_facts_plus_assumption_contradiction_is_allowed():
    # FALSE needs an assumption, so the theory itself is not globally bad.
    rs = parse_rules("fact P\nassume A\nrule r1: A, P => FALSE\n")
    assert rs.assumptions == ("A",)
