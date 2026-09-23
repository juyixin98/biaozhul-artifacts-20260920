"""AST 解释器与 IR 解释器共享的运算语义。

整数除法 / 取模采用**向零截断**（和 C/Java 一致，而不是 Python 的
floor 语义），这样 L0 程序的语义不依赖宿主语言。
"""

from __future__ import annotations

from .source import DIV_ZERO, RuntimeErr, Span, SourceText


def trunc_div(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def trunc_mod(a: int, b: int) -> int:
    r = abs(a) % abs(b)
    return -r if a < 0 else r


def apply_binop(op: str, a: int, b: int, span: Span | None = None,
                source: SourceText | None = None) -> int:
    """对两个具体整数值应用二元运算。

    比较 / 逻辑运算产生 0/1。``/`` 与 ``%`` 在 b==0 时抛
    :class:`RuntimeErr`（code = division-by-zero）。
    """
    def err():
        name = "divide" if op == "/" else "modulo"
        return RuntimeErr(f"{name} by zero", DIV_ZERO, span, source)

    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        if b == 0:
            raise err()
        return trunc_div(a, b)
    if op == "%":
        if b == 0:
            raise err()
        return trunc_mod(a, b)
    if op == "==":
        return int(a == b)
    if op == "!=":
        return int(a != b)
    if op == "<":
        return int(a < b)
    if op == ">":
        return int(a > b)
    if op == "<=":
        return int(a <= b)
    if op == ">=":
        return int(a >= b)
    raise ValueError(f"unknown binary op {op!r}")


def apply_unop(op: str, a: int) -> int:
    if op == "-":
        return -a
    if op == "!":
        return int(not bool(a))
    raise ValueError(f"unknown unary op {op!r}")


def truthy(a: int) -> bool:
    return bool(a)
