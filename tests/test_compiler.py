"""Tests for compile-time safety, arity, stratification and versioning."""

import pytest

from app.language.compiler import compile_program
from app.language.errors import (
    ArityError,
    NegationCycleError,
    UnsafeVariableError,
)
from app.language.parser import parse_program

from .fixtures_programs import INVALID_PROGRAMS


def compile_src(src):
    return compile_program(parse_program(src))


# -- safety ----------------------------------------------------------------


def test_unsafe_head_variable_rejected():
    with pytest.raises(UnsafeVariableError) as exc:
        compile_src(INVALID_PROGRAMS["unsafe_head"])
    assert exc.value.category == "unsafe_variable"


def test_unsafe_negation_variable_rejected():
    with pytest.raises(UnsafeVariableError) as exc:
        compile_src(INVALID_PROGRAMS["unsafe_negation"])
    assert "flounder" in str(exc.value)


def test_unsafe_comparison_variable_rejected():
    with pytest.raises(UnsafeVariableError) as exc:
        compile_src(INVALID_PROGRAMS["unsafe_comparison"])
    assert exc.value.category == "unsafe_variable"


def test_bound_through_join_is_safe():
    # Y is bound by the positive r(Y) before appearing under negation.
    compiled = compile_src("p(X, Y) :- q(X), r(Y), NOT s(X, Y).")
    assert compiled is not None


def test_wildcard_under_negation_is_safe():
    compiled = compile_src("sink(X) :- node(X), NOT edge(X, _).")
    assert compiled is not None


def test_wildcard_in_head_rejected():
    with pytest.raises(UnsafeVariableError):
        compile_src("p(_) :- q(X).")


# -- arity -----------------------------------------------------------------


def test_arithmetic_arity_conflict_rejected():
    with pytest.raises(ArityError) as exc:
        compile_src(INVALID_PROGRAMS["arity_mismatch"])
    assert exc.value.category == "arity_mismatch"


# -- stratification --------------------------------------------------------


def test_negation_cycle_rejected():
    with pytest.raises(NegationCycleError) as exc:
        compile_src(INVALID_PROGRAMS["negation_cycle"])
    assert exc.value.category == "negation_cycle"


def test_positive_recursion_is_single_stratum():
    compiled = compile_src(
        "anc(X,Y) :- parent(X,Y).\nanc(X,Y) :- parent(X,Z), anc(Z,Y).\nparent(a,b).\n"
    )
    assert compiled.stratum_of[("anc", 2)] == 0
    assert compiled.stratum_of[("parent", 2)] == 0


def test_negation_raises_stratum():
    compiled = compile_src(
        "q(a).\np(X) :- q(X), NOT r(X).\nr(X) :- q(X).\n"
    )
    # p depends negatively on r => p strictly above r.
    assert compiled.stratum_of[("p", 1)] > compiled.stratum_of[("r", 1)]
    assert compiled.strata[compiled.stratum_of[("r", 1)]] == (("r", 1),)


def test_indirect_negation_cycle_rejected():
    src = "a(X) :- b(X), NOT c(X).\nc(X) :- d(X).\nd(X) :- a(X).\nb(x).\n"
    with pytest.raises(NegationCycleError):
        compile_src(src)


# -- versioning ------------------------------------------------------------


def test_version_changes_with_rules():
    c1 = compile_src("p(a).\np(X) :- q(X).\nq(b).\n")
    c2 = compile_src("p(a).\np(X) :- q(X).\nq(c).\n")
    assert c1.version != c2.version


def test_version_stable_for_same_program():
    src = "p(a).\np(X) :- q(X).\nq(b).\n"
    # Different textual layout must not change the fingerprint.
    src2 = "p(X) :- q(X).\nq(b).\np(a).\n"
    assert compile_src(src).version == compile_src(src2).version
