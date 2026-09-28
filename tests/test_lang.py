"""Unit tests for the rule language: parsing, schema validation, grounding."""

from __future__ import annotations

import pytest

from htn_planner.lang import (
    BUILTIN_COMPARISONS,
    Domain,
    LangError,
    Literal,
    ParseError,
    SchemaError,
    ground_literal,
    literal_holds,
    parse_domain,
    parse_problem,
    parse_sexpr,
    satisfy_preconditions,
    unify_heads,
)


class TestSExpressionReader:
    def test_parses_nested_forms_with_numbers(self) -> None:
        form = parse_sexpr("(:operator (!n ?x) (:pre (p ?x 3)))")
        assert form[0] == ":operator"
        assert form[1] == ("!n", "?x")
        assert form[2] == (":pre", ("p", "?x", 3))

    def test_semicolons_are_comments(self) -> None:
        form = parse_sexpr("(a b) ; trailing comment\n")
        assert form == ("a", "b")

    def test_float_and_negative_int_tokens(self) -> None:
        form = parse_sexpr("(x -1 2.5)")
        assert form[1] == -1
        assert form[2] == 2.5

    def test_empty_input_is_rejected(self) -> None:
        with pytest.raises(ParseError):
            parse_sexpr("  ; only a comment\n")

    def test_unbalanced_paren_reports_error(self) -> None:
        with pytest.raises(ParseError):
            parse_sexpr("(a (b c)")

    def test_extra_tokens_rejected(self) -> None:
        with pytest.raises(ParseError):
            parse_sexpr("(a) (b)")


class TestDomainSchema:
    def test_operator_requires_bang_prefix(self) -> None:
        text = "(:domain d (:operator (o ?x) (:pre) (:del) (:add)))"
        with pytest.raises(SchemaError, match="start with '!'"):
            parse_domain(text)

    def test_operator_requires_all_three_sections(self) -> None:
        text = "(:domain d (:operator (!o ?x) (:pre (p ?x)) (:add (q ?x))))"
        with pytest.raises(SchemaError, match=":pre/:del/:add"):
            parse_domain(text)

    def test_duplicate_operator_params_rejected(self) -> None:
        text = (
            "(:domain d (:operator (!o ?x ?x)"
            " (:pre) (:del) (:add)))"
        )
        with pytest.raises(SchemaError, match="duplicate parameters"):
            parse_domain(text)

    def test_operator_effect_unbound_variable_rejected(self) -> None:
        text = (
            "(:domain d (:operator (!o ?x)"
            " (:pre (p ?x)) (:del (p ?x)) (:add (q ?y))))"
        )
        with pytest.raises(SchemaError, match="unbound var"):
            parse_domain(text)

    def test_method_requires_explicit_name(self) -> None:
        text = (
            "(:domain d"
            " (:method (t ?x) (:pre) (:tasks (!o ?x))))"
        )
        with pytest.raises(SchemaError, match="name symbol"):
            parse_domain(text)

    def test_partial_order_self_loop_rejected(self) -> None:
        text = """
        (:domain d
          (:operator (!a) (:pre) (:del) (:add))
          (:method m (t) (:pre)
            (:tasks (:partial ((!a) (!a)) (:before 0 0)))))
        """
        with pytest.raises(SchemaError, match="self-loop"):
            parse_domain(text)

    def test_partial_order_cycle_rejected_at_parse_time(self) -> None:
        text = """
        (:domain d
          (:operator (!a) (:pre) (:del) (:add))
          (:method m (t) (:pre)
            (:tasks (:partial ((!a) (!a) (!a)) (:before 0 1) (:before 1 2) (:before 2 0)))))
        """
        with pytest.raises(SchemaError, match="cycle"):
            parse_domain(text)

    def test_partial_index_out_of_range_rejected(self) -> None:
        text = """
        (:domain d
          (:operator (!a) (:pre) (:del) (:add))
          (:method m (t) (:pre)
            (:tasks (:partial ((!a)) (:before 0 1)))))
        """
        with pytest.raises(SchemaError, match="out of range"):
            parse_domain(text)

    def test_methods_group_under_task_head(self) -> None:
        domain = parse_domain(
            """
            (:domain d
              (:operator (!a) (:pre) (:del) (:add))
              (:method m1 (t) (:pre (x)) (:tasks (!a)))
              (:method m2 (t) (:pre (y)) (:tasks (!a))))
            """
        )
        assert {m.name for m in domain.methods["t"]} == {"m1", "m2"}

    def test_safe_negation_unguarded_variable_rejected(self) -> None:
        text = """
        (:domain d
          (:operator (!a) (:pre) (:del) (:add))
          (:method m (t ?x)
            (:pre (not (p ?x ?y)))
            (:tasks (!a))))
        """
        with pytest.raises(SchemaError, match="unguarded variable"):
            parse_domain(text)

    def test_existential_positive_precondition_binds_subtask_var(self) -> None:
        domain = parse_domain(
            """
            (:domain d
              (:operator (!a ?y) (:pre (q ?y)) (:del (q ?y)) (:add (r ?y)))
              (:method m (t ?x)
                (:pre (link ?x ?y))
                (:tasks (!a ?y))))
            """
        )
        method = domain.methods["t"][0]
        assert method.subtasks[0][1] == "?y"


class TestProblemSchema:
    def _op_domain(self) -> str:
        return (
            "(:domain d"
            " (:operator (!a) (:pre) (:del) (:add)))"
        )

    def test_problem_requires_domain(self) -> None:
        with pytest.raises(SchemaError, match="missing"):
            parse_problem("(:problem p (:init) (:tasks (!a)))")

    def test_problem_rejects_unbound_root_variable(self) -> None:
        with pytest.raises(SchemaError, match="unbound variable"):
            parse_problem(
                "(:problem p (:domain d) (:init) (:tasks (!a ?x)))"
            )

    def test_root_parse(self) -> None:
        problem = parse_problem(
            "(:problem p (:domain d) (:init (x 1)) (:tasks (!a) (!a)))"
        )
        assert problem.init == frozenset({("x", 1)})
        assert problem.root.ordered is True
        assert len(problem.root.tasks) == 2


class TestLiteralEvaluation:
    def test_positive_and_negated_facts(self) -> None:
        state = frozenset({("p", "a"), ("q", 1)})
        assert literal_holds(Literal("p", ("a",)), state)
        assert not literal_holds(Literal("p", ("b",)), state)
        assert literal_holds(Literal("p", ("b",), negated=True), state)
        assert not literal_holds(Literal("p", ("a",), negated=True), state)

    @pytest.mark.parametrize(
        "pred,left,right,expected",
        [
            ("=", 1, 1, True),
            ("!=", 1, 2, True),
            ("<", 1, 2, True),
            ("<=", 2, 2, True),
            (">", 3, 2, True),
            (">=", 2, 2, True),
            ("<", 2, 1, False),
        ],
    )
    def test_comparisons(self, pred: str, left: int, right: int, expected: bool) -> None:
        assert literal_holds(Literal(pred, (left, right)), frozenset()) is expected

    def test_ordering_comparison_on_symbol_is_an_error(self) -> None:
        with pytest.raises(LangError):
            literal_holds(Literal("<", ("a", "b")), frozenset())


class TestConjunctiveSatisfaction:
    def test_introduces_existential_binding(self) -> None:
        state = frozenset({("link", "a", "b"), ("link", "a", "c")})
        lits = (Literal("link", ("?x", "?y")),)
        envs = satisfy_preconditions(lits, {"?x": "a"}, state)
        bound_y = sorted(env["?y"] for env in envs)
        assert bound_y == ["b", "c"]

    def test_safe_negation_filters_candidates(self) -> None:
        state = frozenset({("link", "a", "b"), ("blocked", "b")})
        lits = (
            Literal("link", ("?x", "?y")),
            Literal("blocked", ("?y",), negated=True),
        )
        envs = satisfy_preconditions(lits, {"?x": "a"}, state)
        assert envs == []

    def test_negation_keeps_unblocked_candidate(self) -> None:
        state = frozenset(
            {("link", "a", "b"), ("link", "a", "c"), ("blocked", "c")}
        )
        lits = (
            Literal("link", ("?x", "?y")),
            Literal("blocked", ("?y",), negated=True),
        )
        envs = satisfy_preconditions(lits, {"?x": "a"}, state)
        assert [env["?y"] for env in envs] == ["b"]

    def test_comparison_filters_numeric_bindings(self) -> None:
        state = frozenset({("n", 1), ("n", 5), ("n", 10)})
        lits = (Literal("n", ("?k",)), Literal(">", ("?k", 3)))
        envs = satisfy_preconditions(lits, {}, state)
        assert sorted(env["?k"] for env in envs) == [5, 10]


class TestUnification:
    def test_head_unification_binds_params(self) -> None:
        env = unify_heads(("?a", "?b"), ("x", 2), {})
        assert env == {"?a": "x", "?b": 2}

    def test_arity_mismatch_returns_none(self) -> None:
        assert unify_heads(("?a",), (1, 2), {}) is None

    def test_constant_argument_must_match(self) -> None:
        # Params are always variables in this language, but a precondition can
        # be grounded against constants; exercise equality failures directly.
        assert literal_holds(Literal("p", ("fixed",)), frozenset({("p", "other")})) is False


def test_builtin_comparison_set_is_stable() -> None:
    assert BUILTIN_COMPARISONS == {"=", "!=", "<", "<=", ">", ">="}
