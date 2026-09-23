"""Tests for expression sorting, concrete evaluation, and bv wrap semantics."""

import pytest

from cbmc.errors import ContractError
from cbmc.model import Param, Var, load_contract
from cbmc.terms import SymbolicBuilder, evaluate, sort_expr

SCOPE = {
    "alice": Var("alice", "int"),
    "x8": Var("x8", "int", 8),
    "locked": Var("locked", "bool"),
    "P": Param("P", "int"),
    "P8": Param("P8", "int", None, None, 8),
}


def test_lit_and_var_sorts():
    assert sort_expr({"lit": 3}, SCOPE, "t") == ("int", None)
    assert sort_expr({"var": "locked"}, SCOPE, "t") == ("bool", None)
    assert sort_expr({"var": "x8"}, SCOPE, "t") == ("int", 8)


def test_arith_compare_bool_sorts():
    e = {"op": "+", "args": [{"var": "alice"}, {"lit": 2}]}
    assert sort_expr(e, SCOPE, "t") == ("int", None)
    cmp_e = {"op": "<=", "args": [e, {"lit": 5}]}
    assert sort_expr(cmp_e, SCOPE, "t") == ("bool", None)
    assert sort_expr({"op": "not", "args": [{"var": "locked"}]}, SCOPE,
                     "t") == ("bool", None)


def test_ite_branches_must_match():
    ok = {"op": "ite", "args": [
        {"var": "locked"}, {"lit": 1}, {"lit": 2}]}
    assert sort_expr(ok, SCOPE, "t") == ("int", None)
    bad = {"op": "ite", "args": [
        {"var": "locked"}, {"lit": 1}, {"bool": True}]}
    with pytest.raises(ContractError):
        sort_expr(bad, SCOPE, "t")


def test_expected_sort_mismatch_rejected():
    with pytest.raises(ContractError):
        sort_expr({"op": "+", "args": [{"lit": 1}, {"bool": False}]},
                  SCOPE, "t")
    with pytest.raises(ContractError):
        sort_expr({"lit": 1}, SCOPE, "t", expected="bool")


def test_mixed_width_rejected():
    # x8 (8-bit) combined with math int
    with pytest.raises(ContractError):
        sort_expr({"op": "+", "args": [{"var": "x8"}, {"var": "alice"}]},
                  SCOPE, "t")


def test_unknown_operator_rejected():
    with pytest.raises(ContractError):
        sort_expr({"op": "system", "args": []}, SCOPE, "t")


def test_unknown_variable_rejected():
    with pytest.raises(ContractError):
        sort_expr({"var": "evil"}, SCOPE, "t")


def test_concrete_math_arith():
    env = {"alice": 5, "x8": 250, "locked": True, "P": 4, "P8": 20}
    assert evaluate({"op": "-", "args": [{"var": "alice"}, {"lit": 7}]},
                    env, {n: v.width for n, v in SCOPE.items()}) == -2
    assert evaluate({"op": "ite", "args": [
        {"var": "locked"}, {"lit": 10}, {"lit": 20}]}, env,
        {n: v.width for n, v in SCOPE.items()}) == 10


def test_concrete_bv_wraps_modulo_256():
    widths = {n: v.width for n, v in SCOPE.items()}
    # 250 + 20 wraps to 14
    assert evaluate({"op": "+", "args": [{"var": "x8"}, {"var": "P8"}]},
                    {"x8": 250, "P8": 20}, widths) == 14
    # unsigned subtraction wraps upward: 1 - 2 == 255
    assert evaluate({"op": "-", "args": [{"var": "x8"}, {"lit": 249}]},
                    {"x8": 0}, widths) == 7


def test_z3_and_concrete_agree():
    """Symbolic compilation and concrete eval must agree on a fixed model."""
    import z3

    expr = {"op": "ite", "args": [
        {"op": "<=", "args": [{"var": "alice"}, {"lit": 10}]},
        {"op": "*", "args": [{"var": "alice"}, {"lit": 2}]},
        {"lit": 0},
    ]}
    widths = {n: v.width for n, v in SCOPE.items()}
    builder = SymbolicBuilder(widths)
    sym = {"alice": z3.Int("alice"), "locked": z3.Bool("locked"),
           "x8": z3.BitVec("x8", 8), "P": z3.Int("P"),
           "P8": z3.BitVec("P8", 8)}
    z3_expr = builder.build(expr, sym)
    for value in (4, 10, 11):
        s = z3.Solver()
        s.add(sym["alice"] == value, z3_expr == z3.Int("out"))
        assert s.check() == z3.sat
        z3_val = s.model().eval(z3.Int("out")).as_long()
        py_val = evaluate(expr,
                          {"alice": value, "locked": True, "x8": 0,
                           "P": 0, "P8": 0}, widths)
        assert z3_val == py_val


def test_load_rejects_non_object_and_bad_schema():
    with pytest.raises(ContractError):
        load_contract([1, 2, 3])
    with pytest.raises(ContractError):
        load_contract({"schema": "eval/javascript", "actions": []})


def test_load_rejects_assignment_to_unknown_target():
    doc = {
        "state": {"int": {"a": 1}},
        "actions": [
            {"name": "x", "assign": [
                {"target": "ghost", "expr": {"lit": 1}}]}
        ],
    }
    with pytest.raises(ContractError):
        load_contract(doc)


def test_bool_cannot_receive_int_and_vice_versa():
    doc = {
        "state": {"int": {"a": 1}, "bool": {"l": False}},
        "actions": [{"name": "x", "assign": [
            {"target": "l", "expr": {"lit": 1}}]}],
    }
    with pytest.raises(ContractError):
        load_contract(doc)
