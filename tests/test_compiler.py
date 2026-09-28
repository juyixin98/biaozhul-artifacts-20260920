"""Compiler tests: range restriction, arity, negation stratification."""

import pytest

pytestmark = pytest.mark.unit

from datalog_service.language.compiler import compile_program
from datalog_service.language.errors import CompileError
from datalog_service.language.parser import parse_program


def issue_codes(exc: CompileError):
    return [issue.code for issue in exc.value.issues]


def test_head_variable_not_in_positive_body_is_unsafe():
    with pytest.raises(CompileError) as exc:
        compile_program(parse_program("p(X, Y) :- q(X)."))
    assert issue_codes(exc) == ["UNSAFE_VARIABLE"]
    issue = exc.value.issues[0]
    assert issue.location["variable"] == "Y"
    assert issue.location["position"] == "head"


def test_variable_only_in_negation_is_unsafe():
    with pytest.raises(CompileError) as exc:
        compile_program(
            parse_program("q(a). p(X) :- q(X), not r(X, Y).")
        )
    codes = issue_codes(exc)
    assert codes == ["UNSAFE_VARIABLE"]
    assert exc.value.issues[0].location["position"] == "negation"
    assert exc.value.issues[0].location["variable"] == "Y"


def test_negated_variable_bound_by_another_positive_literal_is_safe():
    compiled = compile_program(
        parse_program(
            "q(a). r(a, b). p(X, Y) :- q(X), r(X, Y), not s(Y)."
        )
    )
    assert len(compiled.rules) == 1


def test_unsafe_constant_only_rule_is_rejected_even_without_variables():
    # Constant-only body that never constrains a head var is fine in
    # Datalog (head ground), but a free head var still is not:
    with pytest.raises(CompileError) as exc:
        compile_program(parse_program("p(Z) :- q(a)."))
    assert issue_codes(exc) == ["UNSAFE_VARIABLE"]


def test_arity_mismatch_is_rejected():
    with pytest.raises(CompileError) as exc:
        compile_program(parse_program("p(a, b). p(X) :- q(X). q(a)."))
    assert issue_codes(exc) == ["ARITY_MISMATCH"]
    loc = exc.value.issues[0].location
    assert loc["predicate"] == "p"
    assert loc["expected_arity"] == 2
    assert loc["found_arity"] == 1


def test_direct_negation_cycle_is_rejected():
    with pytest.raises(CompileError) as exc:
        compile_program(
            parse_program("edge(a, b). p(X) :- edge(_, X), not p(X).")
        )
    codes = issue_codes(exc)
    assert codes == ["NEGATION_CYCLE"]
    cycle = exc.value.issues[0].location["cycle"]
    assert cycle[0] == "p" and cycle[-1] == "p"
    assert "p" in cycle[1:-1]


def test_indirect_negation_cycle_is_rejected():
    program = """

    e(a).
    p(X) :- e(X), not q(X).
    q(X) :- e(X), not p(X).
    """
    with pytest.raises(CompileError) as exc:
        compile_program(parse_program(program))
    assert issue_codes(exc) == ["NEGATION_CYCLE"]


def test_longer_negation_cycle_is_rejected():
    program = """
    e(a).
    p(X) :- e(X), not q(X).
    q(X) :- r(X).
    r(X) :- p(X).
    """
    with pytest.raises(CompileError) as exc:
        compile_program(parse_program(program))
    assert issue_codes(exc) == ["NEGATION_CYCLE"]


def test_positive_recursion_is_allowed_and_on_single_stratum():
    compiled = compile_program(
        parse_program(
            """
            parent(a, b).
            ancestor(X, Y) :- parent(X, Y).
            ancestor(X, Y) :- parent(X, Z), ancestor(Z, Y).
            """
        )
    )
    assert compiled.predicates["ancestor"].stratum == 0
    assert len(compiled.strata) == 1


def test_stratified_negation_gets_higher_stratum():
    compiled = compile_program(
        parse_program(
            """
            node(a). node(b).
            edge(a, b).
            reach(X, Y) :- edge(X, Y).
            unreachable(Y) :- node(Y), not reach(a, Y).
            """
        )
    )
    assert compiled.predicates["reach"].stratum == 0
    assert compiled.predicates["unreachable"].stratum == 1


def test_strata_are_well_formed_for_chained_negation():
    compiled = compile_program(
        parse_program(
            """
            base(a). base(b).
            p(X) :- base(X), not base_empty(X).
            base_empty(X) :- base(X), impossible(X).
            q(X) :- p(X), not r(X).
            r(X) :- p(X).
            """
        )
    )
    # q depends negatively on r, and both depend on p.
    assert compiled.predicates["q"].stratum > compiled.predicates["r"].stratum


def test_multiple_issues_are_reported_together():
    with pytest.raises(CompileError) as exc:
        compile_program(
            parse_program(
                "p(X, Y) :- q(X). p(Z) :- q(Z), not s(Z, W)."
            )
        )
    codes = issue_codes(exc)
    assert codes.count("UNSAFE_VARIABLE") == 2  # Y in head, W in negation


def test_rule_version_is_stable_under_source_ordering():
    text_a = "q(a). p(X) :- q(X).\n"
    text_b = "p(X) :- q(X).\nq(a).\n"
    ca = compile_program(parse_program(text_a))
    cb = compile_program(parse_program(text_b))
    assert ca.rule_version == cb.rule_version


def test_rule_version_changes_when_rules_change():
    ca = compile_program(parse_program("q(a). p(X) :- q(X)."))
    cb = compile_program(parse_program("q(a). q(b). p(X) :- q(X)."))
    # rule version hashes rules only (facts handled separately), so equal
    assert ca.rule_version == cb.rule_version
    cc = compile_program(parse_program("q(a). p(X) :- q(X), q(X)."))
    assert cc.rule_version != ca.rule_version
