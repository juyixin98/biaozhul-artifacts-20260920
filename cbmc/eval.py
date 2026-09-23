"""对表达式 AST 做纯数据解释的具体求值（Python 侧，用于逐步重放与交叉校验）。

整数语义与 SMT 编码保持一致：64 位有符号补码，每次算术结果回绕
(wrap-around)。这样重放溢出模型时得到的状态与 Z3 位向量编码一致。
"""

from __future__ import annotations

from typing import Any

from .errors import ModelError

MASK = (1 << 64) - 1
SIGN_BIT = 1 << 63
INT_MIN = -(1 << 63)
INT_MAX = (1 << 63) - 1


def wrap64(x: int) -> int:
    """把任意 Python 整数回绕到 64 位有符号补码表示区间。"""
    x &= MASK
    return x - (1 << 64) if x & SIGN_BIT else x


def eval_expr(expr: dict, env: dict[str, Any]) -> Any:
    t = expr["t"]
    if t == "num":
        return wrap64(expr["value"])
    if t == "bool":
        return bool(expr["value"])
    if t == "var":
        name = expr["name"]
        if name not in env:
            raise ModelError(f"求值时遇到未绑定变量 {name!r}")
        return env[name]
    if t == "arith":
        vals = [eval_expr(a, env) for a in expr["args"]]
        r = vals[0]
        for v in vals[1:]:
            if expr["op"] == "+":
                r += v
            elif expr["op"] == "-":
                r -= v
            else:
                r *= v
            r = wrap64(r)
        return r
    if t == "cmp":
        l = eval_expr(expr["lhs"], env)
        r = eval_expr(expr["rhs"], env)
        op = expr["op"]
        if op == "<":
            return l < r
        if op == "<=":
            return l <= r
        if op == ">":
            return l > r
        if op == ">=":
            return l >= r
        if op == "==":
            return l == r
        return l != r
    if t == "boolop":
        op = expr["op"]
        vals = [eval_expr(a, env) for a in expr["args"]]
        if op == "and":
            return all(vals)
        if op == "or":
            return any(vals)
        return not vals[0]
    raise ModelError(f"求值时遇到未知表达式类型 {t!r}")


def initial_state(model: dict) -> dict[str, Any]:
    return {v["name"]: wrap64(v["init"]) if v["type"] == "int" else bool(v["init"])
            for v in model["state_vars"]}


def apply_action(action: dict, state: dict, params: dict[str, int]) -> dict:
    """在具体状态上执行一个动作（调用前应已通过 guard 检查）。

    所有 effect 的右端都在「前置状态 + 参数」上同时求值，随后一次性提交，
    与 SMT 编码的 frame/frame-axiom 语义一致。
    """
    env = dict(state)
    env.update(params)
    new_state = dict(state)
    for e in action["effects"]:
        if e["kind"] == "assign":
            new_state[e["target"]] = eval_expr(e["expr"], env)
        elif e["kind"] == "lock":
            new_state[e["target"]] = True
        else:  # unlock
            new_state[e["target"]] = False
    return new_state
