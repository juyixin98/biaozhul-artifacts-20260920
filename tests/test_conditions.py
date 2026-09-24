"""条件 AST 的 Kleene 三值真值表、集合语义与安全校验测试。"""
from __future__ import annotations

import itertools

import pytest

from app.conditions import UNKNOWN, Condition
from app.errors import PolicyError


def cond(node):
    c = Condition(node)
    c.validate()
    return c


# ---------------------------------------------------------------------------
# 1. 枚举三值逻辑完整真值表（3^2 = 9 种组合，外加 not）
# ---------------------------------------------------------------------------

T = True
F = False
U = UNKNOWN


def eval_op(op, values):
    node = {"op": op, "args": [{"literal": v} if isinstance(v, bool) else _unknown_leaf() for v in values]}
    # 字面量无法表达 UNKNOWN：用缺失属性代替
    return cond(node).evaluate({}, {})


def _unknown_leaf():
    return {"attr": "subject.missing"}


def test_not_truth_table_exhaustive():
    # NOT 的完整三值真值表
    table = {T: F, F: T, U: U}
    for value, expected in table.items():
        leaf = {"literal": value} if isinstance(value, bool) else _unknown_leaf()
        assert cond({"op": "not", "args": [leaf]}).evaluate({}, {}) is expected


def test_and_truth_table_exhaustive():
    expected = {}
    for a, b in itertools.product([T, F, U], repeat=2):
        # Kleene AND
        expected[(a, b)] = F if (a is F or b is F) else (U if (a is U or b is U) else T)
    for a, b in itertools.product([T, F, U], repeat=2):
        node = {
            "op": "and",
            "args": [
                {"literal": a} if isinstance(a, bool) else _unknown_leaf(),
                {"literal": b} if isinstance(b, bool) else _unknown_leaf(),
            ],
        }
        assert cond(node).evaluate({}, {}) is expected[(a, b)], f"AND({a},{b})"


def test_or_truth_table_exhaustive():
    expected = {}
    for a, b in itertools.product([T, F, U], repeat=2):
        # Kleene OR
        expected[(a, b)] = T if (a is T or b is T) else (U if (a is U or b is U) else F)
    for a, b in itertools.product([T, F, U], repeat=2):
        node = {
            "op": "or",
            "args": [
                {"literal": a} if isinstance(a, bool) else _unknown_leaf(),
                {"literal": b} if isinstance(b, bool) else _unknown_leaf(),
            ],
        }
        assert cond(node).evaluate({}, {}) is expected[(a, b)], f"OR({a},{b})"


def test_and_or_de_morgan_under_three_valued_logic():
    # 枚举全部 9 种组合验证德摩根律：NOT(A AND B) == NOT(A) OR NOT(B)
    for a, b in itertools.product([T, F, U], repeat=2):
        leaf = lambda v: {"literal": v} if isinstance(v, bool) else _unknown_leaf()
        left = {"op": "not", "args": [{"op": "and", "args": [leaf(a), leaf(b)]}]}
        right = {"op": "or", "args": [
            {"op": "not", "args": [leaf(a)]},
            {"op": "not", "args": [leaf(b)]},
        ]}
        assert cond(left).evaluate({}, {}) is cond(right).evaluate({}, {})


# ---------------------------------------------------------------------------
# 2. 缺失属性 => UNKNOWN；类型不匹配 => UNKNOWN（fail-closed）
# ---------------------------------------------------------------------------

def test_missing_attribute_is_unknown():
    c = cond({"attr": "subject.department"})
    assert c.evaluate({}, {}) is UNKNOWN
    assert cond({"attr": "subject.a.b"}).evaluate({"a": {}}, {}) is UNKNOWN
    # 中段被非对象截断
    assert cond({"attr": "subject.a.b"}).evaluate({"a": 5}, {}) is UNKNOWN
    # 值为 null 视为缺失
    assert cond({"attr": "subject.a"}).evaluate({"a": None}, {}) is UNKNOWN
    # 对象叶子不可直接比较 => UNKNOWN
    assert cond({"attr": "subject.a"}).evaluate({"a": {"x": 1}}, {}) is UNKNOWN


def test_missing_attr_propagates_through_operators():
    node = {
        "op": "and",
        "args": [
            {"op": "eq", "args": [{"attr": "subject.missing"}, {"literal": "x"}]},
            {"literal": True},
        ],
    }
    assert cond(node).evaluate({}, {}) is UNKNOWN
    node_or = {
        "op": "or",
        "args": [
            {"op": "eq", "args": [{"attr": "subject.missing"}, {"literal": "x"}]},
            {"literal": False},
        ],
    }
    assert cond(node_or).evaluate({}, {}) is UNKNOWN


def test_type_mismatch_is_unknown():
    subj = {"a": "string", "b": 5, "flag": True, "set": [1, 2]}
    # 字符串与整数 eq => UNKNOWN（不做隐式转换）
    assert cond({"op": "eq", "args": [{"attr": "subject.a"}, {"literal": 1}]}).evaluate(subj, {}) is UNKNOWN
    # bool 不与 int 混同
    assert cond({"op": "eq", "args": [{"attr": "subject.flag"}, {"literal": 1}]}).evaluate(subj, {}) is UNKNOWN
    assert cond({"op": "eq", "args": [{"attr": "subject.b"}, {"literal": True}]}).evaluate(subj, {}) is UNKNOWN
    # in 的右值不是集合 => UNKNOWN
    assert cond({"op": "in", "args": [{"attr": "subject.b"}, {"literal": 5}]}).evaluate(subj, {}) is UNKNOWN
    # intersects 两侧必须都是集合
    assert cond({"op": "intersects", "args": [{"attr": "subject.a"}, {"literal": ["x"]}]}).evaluate(subj, {}) is UNKNOWN


# ---------------------------------------------------------------------------
# 3. 集合包含语义：in / contains / superset / subset / intersects / neq
# ---------------------------------------------------------------------------

def test_in_operator():
    node = {"op": "in", "args": [{"attr": "subject.dept"}, {"literal": ["eng", "sales"]}]}
    assert cond(node).evaluate({"dept": "eng"}, {}) is T
    assert cond(node).evaluate({"dept": "hr"}, {}) is F


def test_contains_scalar_and_set():
    tags = {"attr": "resource.tags"}
    assert cond({"op": "contains", "args": [tags, {"literal": "prod"}]}).evaluate({}, {"tags": ["prod"]}) is T
    assert cond({"op": "contains", "args": [tags, {"literal": ["a", "b"]}]}).evaluate(
        {}, {"tags": ["a", "b", "c"]}) is T
    assert cond({"op": "contains", "args": [tags, {"literal": ["a", "z"]}]}).evaluate(
        {}, {"tags": ["a", "b"]}) is F


def test_superset_subset_intersects():
    s = {"attr": "subject.roles"}
    assert cond({"op": "superset", "args": [s, {"literal": ["a"]}]}).evaluate(
        {"roles": ["a", "b"]}, {}) is T
    assert cond({"op": "subset", "args": [s, {"literal": ["a", "b", "c"]}]}).evaluate(
        {"roles": ["a", "b"]}, {}) is T
    assert cond({"op": "subset", "args": [s, {"literal": ["a"]}]}).evaluate(
        {"roles": ["a", "b"]}, {}) is F
    assert cond({"op": "intersects", "args": [s, {"literal": ["b", "c"]}]}).evaluate(
        {"roles": ["a", "b"]}, {}) is T
    assert cond({"op": "intersects", "args": [s, {"literal": ["x", "y"]}]}).evaluate(
        {"roles": ["a", "b"]}, {}) is F


def test_neq_unknown_propagation():
    assert cond({"op": "neq", "args": [{"attr": "subject.x"}, {"literal": 1}]}).evaluate(
        {"x": 2}, {}) is T
    assert cond({"op": "neq", "args": [{"attr": "subject.x"}, {"literal": 1}]}).evaluate({}, {}) is U


# ---------------------------------------------------------------------------
# 4. 安全校验：非法算子、属性路径逃逸、危险类型、嵌套上限均被拒绝
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("node", [
    {"op": "eval", "args": []},                       # 不在白名单
    {"op": "__import__", "args": []},
    {"op": "eq", "args": []},                         # 参数个数错误
    {"op": "not", "args": [{"literal": True}, {"literal": False}]},
    {"attr": "os.system"},                            # 越出 subject/resource
    {"attr": "subject"},                              # 缺少路径
    {"attr": "subject..bad"},
    {"attr": "resource.0x"},
    {"attr": 123},
    {"literal": None},                                # null 字面量
    {"literal": {"nested": "object"}},                # 对象字面量
    {"literal": [None]},
    {"foo": "bar"},                                   # 无法识别的节点
])
def test_invalid_ast_rejected(node):
    c = Condition(node)
    with pytest.raises(PolicyError):
        c.validate()


def test_unknown_operator_names_are_data_not_execution():
    # 即便恶意传入类似函数名的字符串，也只是数据，报“未知算子”
    c = Condition({"op": "().__class__.__bases__[0].__subclasses__", "args": []})
    with pytest.raises(PolicyError):
        c.validate()


def test_nested_depth_limit():
    node = {"literal": True}
    for _ in range(25):
        node = {"op": "not", "args": [node]}
    with pytest.raises(PolicyError):
        Condition(node).validate()
