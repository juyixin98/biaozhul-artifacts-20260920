"""Parser tests: concrete accepted syntax and rejected malformed input."""

import pytest

pytestmark = pytest.mark.unit

from datalog_service.language.ast import Constant, Variable
from datalog_service.language.errors import ParseError
from datalog_service.language.parser import parse_program, parse_query


def test_parses_facts_with_symbol_integer_and_string_constants():
    program = parse_program('parent(alice, bob). age(bob, 7). label(bob, "Bob").')
    assert len(program.facts) == 3
    assert program.facts[0].args == (Constant("alice"), Constant("bob"))
    assert program.facts[1].args[1] == Constant(7)
    assert program.facts[2].args[1] == Constant("Bob")


def test_parses_rule_with_negation_and_variables():
    program = parse_program(
        "blocked(X) :- person(X), not trusted(X)."
    )
    rule = program.rules[0]
    assert rule.head.predicate == "blocked"
    assert rule.body[0].negated is False
    assert rule.body[1].negated is True
    assert isinstance(rule.body[1].atom.args[0], Variable)
    assert rule.body[1].atom.args[0].name == "X"


def test_comments_and_blank_lines_are_ignored():
    text = "% full line comment\nparent(a, b).\n\n % another\n"
    program = parse_program(text)
    assert len(program.facts) == 1


def test_zero_arity_predicates_are_supported():
    program = parse_program("started. running :- started.")
    assert program.facts[0].arity == 0
    assert program.rules[0].head.arity == 0


def test_query_parser_requires_question_mark():
    goal = parse_query("ancestor(alice, X)?")
    assert goal.predicate == "ancestor"
    assert goal.args[1] == Variable("X")


@pytest.mark.parametrize(
    "text,expected_fragment",
    [
        ("parent(a b).", "expected"),
        ("parent(a, b)", "'.'"),
        ("Parent(alice).", "predicate name"),
        ("p(X).", "facts must be ground"),
        ("p(X) :- .", "predicate name"),
        ("p(X) :- q(X) :- r(X).", "expected '.'"),
        ("p(01).", "leading zero"),
    ],
)
def test_parse_errors_are_reported(text, expected_fragment):
    with pytest.raises(ParseError) as exc:
        parse_program(text)
    assert expected_fragment in str(exc.value)


def test_query_trailing_input_is_rejected():
    with pytest.raises(ParseError):
        parse_query("p(X)? q(Y)?")


def test_string_escape_sequences_are_decoded():
    program = parse_program(r'label(a, "line\nend").')
    assert program.facts[0].args[1].value == "line\nend"
    program2 = parse_program(r'q("a\\b").')
    assert program2.facts[0].args[0].value == "a\\b"


def test_unterminated_string_reports_position():
    with pytest.raises(ParseError) as exc:
        parse_program('p("abc).')
    assert "unterminated string" in str(exc.value)


def test_unexpected_character_is_a_parse_error():
    with pytest.raises(ParseError):
        parse_program("p(a) & q(b).")

