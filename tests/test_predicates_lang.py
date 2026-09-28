"""Unit tests: DSL/JSON parser, compiler static semantics, predicates."""

from __future__ import annotations

import pytest

from reteapp.lang import parse_rules, parse_rules_json
from reteapp.lang.compiler import CompileError, compile_rules
from reteapp.lang.model import Operator
from reteapp.lang.parser import RuleParseError
from reteapp.lang.predicates import PredicateError, evaluate


def test_json_frontend_parses_constraint_kinds() -> None:
    rules = parse_rules_json(
        {
            "rules": [
                {
                    "name": "r",
                    "conditions": [
                        {"type": "A", "constraints": [
                            {"field": "x", "variable": "?x"},
                            {"field": "s", "op": "==", "value": "on"},
                        ]},
                        {"type": "B", "constraints": [
                            {"field": "y", "variable": "?x", "op": ">"},
                        ]},
                    ],
                    "action": {"assert": [{"type": "C", "fields": {"x": {"variable": "x"}}}]},
                }
            ]
        }
    )
    compiled = compile_rules(rules)
    ce0 = compiled[0].plans[0]
    ce1 = compiled[0].plans[1]
    assert ce0.binds == {"x": "x"}
    assert ce1.requires == frozenset({"x"})
    stages = {(t["kind"], t["stage"]) for t in ce1.tests}
    assert ("var", "join") in stages


def test_text_dsl_matches_json_semantics() -> None:
    text = '''
    rule "pair" salience 7
    on Employee(id = ?eid, dept = ?d)
    on Assignment(who = ?eid)
    action:
      assert Matched(who = ?eid)
    end
    '''
    rules = parse_rules(text)
    compiled = compile_rules(rules)
    assert compiled[0].name == "pair"
    assert compiled[0].salience == 7
    assert compiled[0].plans[1].requires == frozenset({"eid"})


def test_text_dsl_actions_retract_stop_literals_and_comments() -> None:
    text = '''
    # a comment line must be ignored
    rule "escalate" salience -5
    on Ticket(priority = ?p, severity > 3)
    on Agent(level = ?lvl, max <= 10)
    action:
      assert Alert(priority = ?p, code = "X1")
      retract 0, 1
      stop
    end
    '''
    rules = parse_rules(text)
    action = rules[0].action
    assert action.stop is True
    assert action.retract_ce_indices == (0, 1)
    assert action.asserts[0].type == "Alert"
    assert action.asserts[0].fields["code"] == {"value": "X1"}
    assert action.asserts[0].fields["priority"] == {"variable": "p"}
    ticket = rules[0].conditions[0]
    assert any(c.field == "severity" for c in ticket.constraints)


def test_text_dsl_rejects_bad_lines() -> None:
    with pytest.raises(RuleParseError, match="bad rule header"):
        parse_rules('rule without quotes\non A(x = ?x)\naction:\nend\n')
    with pytest.raises(RuleParseError):
        parse_rules('rule "r"\non A(x = ?x)\naction:\n  frobnicate\nend\n')
    with pytest.raises(RuleParseError):
        parse_rules("")


def test_dsl_and_json_support_in_operator_list_literal_and_bare_binding() -> None:
    text = '''
    rule "open_states"
    on Order(state in ["new", "paid"], ref)
    action:
      assert Open(ref = ?ref)
    end
    '''
    rule = parse_rules(text)[0]
    ce = rule.conditions[0]
    in_constraint = next(c for c in ce.constraints if c.field == "state")
    assert in_constraint.op.value == "in"
    assert in_constraint.value == ["new", "paid"]
    # Bare ``ref`` becomes a binding of field ref to ?ref.
    bare = next(c for c in ce.constraints if c.field == "ref")
    from reteapp.lang.model import Binding

    assert isinstance(bare, Binding) and bare.variable == "ref"
    assert rule.action.asserts[0].fields["ref"] == {"variable": "ref"}


def test_json_list_literal_requires_primitives() -> None:
    doc = {
        "rules": [
            {"name": "r",
             "conditions": [{"type": "T", "constraints": [
                 {"field": "s", "op": "in", "value": [{"nested": 1}]}]}],
             "action": {}}
        ]
    }
    with pytest.raises(RuleParseError, match="list literals"):
        compile_rules(parse_rules_json(doc))


def test_retract_index_out_of_range_rejected() -> None:
    doc = {
        "rules": [
            {"name": "r",
             "conditions": [{"type": "A", "constraints": [
                 {"field": "x", "variable": "?x"}]}],
             "action": {"retract_ce_indices": [3]}}
        ]
    }
    with pytest.raises(RuleParseError, match="retract index out of range"):
        compile_rules(parse_rules_json(doc))


def test_unbound_variable_is_compile_error_not_silent_match() -> None:
    doc = {
        "rules": [
            {
                "name": "bad",
                "conditions": [
                    {"type": "A", "constraints": [{"field": "x", "variable": "?x"}]},
                    {"type": "B", "constraints": [
                        {"field": "y", "op": "!=", "variable": "?z"}]},
                ],
                "action": {},
            }
        ]
    }
    with pytest.raises(CompileError, match="used before it is bound"):
        compile_rules(parse_rules_json(doc))


def test_action_using_unbound_variable_rejected() -> None:
    doc = {
        "rules": [
            {
                "name": "bad",
                "conditions": [{"type": "A", "constraints": [
                    {"field": "x", "variable": "?x"}]}],
                "action": {"assert": [{"type": "C", "fields": {"y": {"variable": "ghost"}}}]},
            }
        ]
    }
    with pytest.raises(CompileError, match="unbound variable"):
        compile_rules(parse_rules_json(doc))


def test_duplicate_rule_names_rejected() -> None:
    doc = {"rules": [
        {"name": "r", "conditions": [{"type": "A", "constraints": []}], "action": {}},
        {"name": "r", "conditions": [{"type": "B", "constraints": []}], "action": {}},
    ]}
    with pytest.raises(CompileError, match="duplicate rule name"):
        compile_rules(parse_rules_json(doc))


@pytest.mark.parametrize(
    "document,match",
    [
        ({"rules": [{"name": "", "conditions": [], "action": {}}]}, "non-empty string"),
        ({"rules": [{"name": "x"}]}, "non-empty list"),
        ({"rules": [{"name": "x", "conditions": [{"constraints": []}], "action": {}}]},
         "'type'"),
        ("{not json", "invalid JSON"),
    ],
)
def test_malformed_documents_report_parse_error(document, match) -> None:
    with pytest.raises(RuleParseError, match=match):
        parse_rules_json(document)


def test_predicate_operators() -> None:
    assert evaluate(Operator.EQ, 1, 1)
    assert evaluate(Operator.NEQ, 1, 2)
    assert evaluate(Operator.LT, 1, 2)
    assert evaluate(Operator.IN, "a", ["a", "b"])
    assert evaluate(Operator.NOT_IN, "z", ["a"])
    assert not evaluate(Operator.GTE, 2, 3)


def test_ordering_between_incomparable_types_raises_not_false() -> None:
    # The explicit failure contract: undefined comparison is an error,
    # never silently "no match".
    with pytest.raises(PredicateError):
        evaluate(Operator.LT, "abc", 5)
    with pytest.raises(PredicateError):
        evaluate(Operator.GT, True, 1)
