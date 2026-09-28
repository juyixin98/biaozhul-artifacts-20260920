"""Tests for lexer/parser/term AST."""

import pytest

from app.language.errors import ArityError
from app.language.lexer import Tokenizer, T_IDENT, T_STRING, T_TURNSTILE, T_NOT
from app.language.parser import Parser, ParseError, parse_program
from app.language.terms import Atom, Comparison, Const, NegAtom, Var


def test_lexer_basic_tokens():
    toks = Tokenizer('ancestor(X, Y) :- parent(X, Z), "a b".').tokenize()
    kinds = [t.kind for t in toks]
    assert kinds.count("LPAREN") == 2
    assert T_TURNSTILE in kinds
    assert T_STRING in [t.kind for t in toks]
    assert any(t.kind == T_IDENT and t.value == "ancestor" for t in toks)


def test_parser_fact_is_ground():
    prog = parse_program("parent(ann, bob).")
    assert len(prog.facts) == 1
    fact = prog.facts[0]
    assert fact.pred == "parent"
    assert fact.args == (Const("ann"), Const("bob"))


def test_parser_distinguishes_vars_and_constants():
    atom = Parser("p(X, 12, foo)")._parse_atom()
    (a, b, c) = atom.args
    assert isinstance(a, Var) and a.name == "X"
    assert b == Const("12")
    assert c == Const("foo")


def test_parser_rule_with_negation_and_comparison():
    prog = parse_program("p(X) :- q(X), NOT r(X), X != y.")
    rule = prog.rules[0]
    assert isinstance(rule.body[0], Atom)
    assert isinstance(rule.body[1], NegAtom)
    assert isinstance(rule.body[2], Comparison)
    assert rule.body[2].op == "!="


def test_parser_wildcard_becomes_fresh_variable():
    prog = parse_program("sink(X) :- node(X), NOT edge(X, _).")
    neg = prog.rules[0].body[1]
    assert isinstance(neg, NegAtom)
    wildcard = neg.atom.args[1]
    assert isinstance(wildcard, Var) and wildcard.name.startswith("_w")


def test_parser_rejects_non_ground_fact():
    with pytest.raises(ParseError):
        parse_program("p(X).")


@pytest.mark.parametrize("bad", ["p(a :- q(a).", "p(a) q(a).", "p(X).", "p(!).", "p(a) :- q(a),"])
def test_parser_rejects_malformed(bad):
    with pytest.raises((ParseError, ValueError)):
        parse_program(bad)


def test_comments_are_ignored():
    prog = parse_program("% full line comment\np(a). % trailing\n")
    assert len(prog.facts) == 1


def test_quoted_constant_preserves_spaces():
    prog = parse_program('name("Ann Lee").')
    assert prog.facts[0].args[0].value == "Ann Lee"


def test_not_is_case_insensitive_keyword():
    prog = parse_program("p(X) :- q(X), not r(X).")
    assert isinstance(prog.rules[0].body[1], NegAtom)
