"""Query-layer tests: matching, bindings, constant filtering."""

import pytest

pytestmark = pytest.mark.unit

from datalog_service.engine.fixpoint import evaluate
from datalog_service.language.compiler import compile_program
from datalog_service.language.errors import QueryError
from datalog_service.language.parser import parse_program, parse_query
from datalog_service.query.answering import answer_query, goal_variables


def _materialized(source: str):
    program = parse_program(source)
    compiled = compile_program(program)
    return compiled, evaluate(compiled, program.facts)


def test_query_binds_each_variable_for_every_match():
    compiled, mat = _materialized(
        "edge(a, b). edge(a, c). edge(b, c)."
    )
    answers = answer_query(mat, parse_query("edge(a, Y)?"))
    assert [a.bindings["Y"] for a in answers] == ["b", "c"]


def test_ground_goal_has_empty_bindings_and_membership_semantics():
    compiled, mat = _materialized("edge(a, b).")
    assert len(answer_query(mat, parse_query("edge(a, b)?"))) == 1
    assert len(answer_query(mat, parse_query("edge(b, a)?"))) == 0


def test_constant_in_goal_filters_the_relation():
    compiled, mat = _materialized("edge(a, b). edge(b, c). edge(a, c).")
    answers = answer_query(mat, parse_query("edge(b, Y)?"))
    assert [a.bindings["Y"] for a in answers] == ["c"]


def test_repeated_query_variable_requires_equal_columns():
    compiled, mat = _materialized(
        "t(a, a). t(a, b). t(b, b)."
    )
    answers = answer_query(mat, parse_query("t(X, X)?"))
    assert sorted(a.bindings["X"] for a in answers) == ["a", "b"]


def test_integer_constants_round_trip():
    compiled, mat = _materialized("age(bob, 42). age(amy, 30).")
    answers = answer_query(mat, parse_query("age(N, 42)?"))
    assert [a.bindings["N"] for a in answers] == ["bob"]


def test_unknown_predicate_is_a_query_error():
    compiled, mat = _materialized("p(a).")
    with pytest.raises(QueryError) as exc:
        answer_query(mat, parse_query("q(X)?"))
    assert "unknown predicate" in str(exc.value)


def test_query_wrong_arity_is_a_query_error():
    compiled, mat = _materialized("p(a, b).")
    with pytest.raises(QueryError):
        answer_query(mat, parse_query("p(X)?"))


def test_max_answers_truncates():
    compiled, mat = _materialized("p(a). p(b). p(c). p(d).")
    answers = answer_query(mat, parse_query("p(X)?"), max_answers=2)
    assert len(answers) == 2


def test_goal_variables_are_unique_and_ordered():
    goal = parse_query("q(X, Y, X, Z)?")
    assert goal_variables(goal) == ["X", "Y", "Z"]


def test_every_answer_carries_a_verifiable_proof():
    source = """

    parent(a, b). parent(b, c).
    anc(X, Y) :- parent(X, Y).
    anc(X, Y) :- parent(X, Z), anc(Z, Y).
    """
    compiled, mat = _materialized(source)
    answers = answer_query(mat, parse_query("anc(a, c)?"))
    proof = answers[0].proof
    assert proof.kind == "derived"
    assert proof.rule_id == "r2"
    child_kinds = [c.kind for c in proof.children]
    assert "fact" in child_kinds
    assert "derived" in child_kinds
