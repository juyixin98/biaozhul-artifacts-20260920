"""AST rendering tests: canonical text distinguishes value types."""

import pytest

pytestmark = pytest.mark.unit

from datalog_service.language.ast import Atom, Constant, Literal, Variable, format_goal, format_value
from datalog_service.language.parser import parse_program


def test_symbol_and_quoted_string_constants_render_differently():
    symbol = Constant("alice")
    string = Constant("Alice")
    assert symbol.canonical() == "alice"
    assert string.canonical() == '"Alice"'


def test_integer_and_string_rendering():
    assert Constant(7).canonical() == "7"
    assert Constant('7').canonical() == '"7"'
    assert format_value('he is "x"') == '"he is \\"x\\""'


def test_variable_and_atom_canonical_text():
    assert Variable("X").canonical() == "X"
    atom = Atom("p", (Variable("X"), Constant("a"), Constant(2)))
    assert atom.canonical() == "p(X, a, 2)"
    assert Atom("started").canonical() == "started"


def test_literal_keeps_negation_flag():
    positive = parse_program("p(X) :- q(X).").rules[0].body[0]
    negative = parse_program("p(X) :- q(X), not r(X).").rules[0].body[1]
    assert positive.negated is False
    assert negative.negated is True
    assert negative.canonical() == "not r(X)"
    assert isinstance(negative.atom, Atom)


def test_format_goal_renders_concrete_rows():
    assert format_goal("parent", ("a", "b")) == "parent(a, b)"
    assert format_goal("p", ("A", 1)) == 'p("A", 1)'
    assert format_goal("p", ("a",), negated=True) == "not p(a)"
    assert format_goal("flag", ()) == "flag"

