"""构造内置默认安全不变量：所有整数余额非负 且 总额守恒。"""

from __future__ import annotations

from .eval import wrap64


def _var(name: str) -> dict:
    return {"t": "var", "name": name}


def _num(v: int) -> dict:
    return {"t": "num", "value": v}


def _ge0(name: str) -> dict:
    return {"t": "cmp", "op": ">=", "lhs": _var(name), "rhs": _num(0)}


def default_invariant(model: dict) -> dict:
    """and(每个 int 状态变量 >= 0,  全部 int 变量之和 == 初始总和)。

    加法按 64 位补码回绕（与检查器/解释器一致），因此守恒性质检测的是
    模 2^64 的总量守恒。
    """
    int_vars = [v["name"] for v in model["state_vars"] if v["type"] == "int"]
    conds = [_ge0(n) for n in int_vars]
    if int_vars:
        total_ast = {"t": "arith", "op": "+", "args": [_var(n) for n in int_vars]}
        init_total = wrap64(sum(wrap64(v["init"]) for v in model["state_vars"]
                                if v["type"] == "int"))
        conds.append({"t": "cmp", "op": "==", "lhs": total_ast,
                      "rhs": _num(init_total)})
    return {"expr": {"t": "boolop", "op": "and", "args": conds}}
