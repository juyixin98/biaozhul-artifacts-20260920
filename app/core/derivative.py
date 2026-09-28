"""Symbolic differentiation of the AST.

The derivative tree is built once per request and evaluated with the same
interval machinery as ``f`` itself. Generated nodes carry ``-1`` spans; the
evaluator substitutes the nearest enclosing source position if a generated
node triggers a domain error.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .ast import Binary, Call, Const, Node, Num, Paren, Unary, Var

_SYNTH = -1


def _num(value: str) -> Num:
    return Num(value, start=_SYNTH, end=_SYNTH)


def _bin(op: str, left: Node, right: Node) -> Binary:
    return Binary(op, left, right, start=_SYNTH, end=_SYNTH)


def _neg(node: Node) -> Unary:
    return Unary("-", node, start=_SYNTH, end=_SYNTH)


def _call(name: str, arg: Node) -> Call:
    return Call(name, arg, start=_SYNTH, end=_SYNTH)


def differentiate(node: Node) -> Node:
    """Return the AST of d/dx ``node``."""
    if isinstance(node, Paren):
        return differentiate(node.inner)
    if isinstance(node, (Num, Const)):
        return _num("0")
    if isinstance(node, Var):
        return _num("1")
    if isinstance(node, Unary):
        du = differentiate(node.operand)
        if node.op == "+":
            return du
        return _neg(du)
    if isinstance(node, Binary):
        return _diff_binary(node)
    if isinstance(node, Call):
        return _diff_call(node)
    raise TypeError(f"cannot differentiate node {type(node).__name__}")


def _diff_binary(node: Binary) -> Node:
    f, g = node.left, node.right
    df, dg = differentiate(f), differentiate(g)
    if node.op in ("+", "-"):
        return _bin(node.op, df, dg)
    if node.op == "*":
        # (f g)' = f' g + f g'
        return _bin("+", _bin("*", df, g), _bin("*", f, dg))
    if node.op == "/":
        # (f/g)' = (f' g - f g') / g^2
        numerator = _bin("-", _bin("*", df, g), _bin("*", f, dg))
        denominator = _bin("^", g, _num("2"))
        return _bin("/", numerator, denominator)
    if node.op == "^":
        # g is a numeric literal by grammar; (f^g)' = g f^(g-1) f'
        exponent_text = _literal_text(g)
        prev_exponent = _decrement_literal(exponent_text)
        prev = _bin("^", f, _num(prev_exponent))
        return _bin("*", _bin("*", _num(exponent_text), prev), df)
    raise TypeError(f"unknown operator {node.op!r}")


def _diff_call(node: Call) -> Node:
    u = node.arg
    du = differentiate(u)
    name = node.name
    if name == "sin":
        return _bin("*", _call("cos", u), du)
    if name == "cos":
        return _bin("*", _neg(_call("sin", u)), du)
    if name == "tan":
        # d tan(u) = (1 + tan(u)^2) u'
        tan_u = _call("tan", u)
        factor = _bin("+", _num("1"), _bin("^", tan_u, _num("2")))
        return _bin("*", factor, du)
    if name == "exp":
        return _bin("*", _call("exp", u), du)
    if name == "log":
        return _bin("/", du, u)
    if name == "sqrt":
        return _bin("/", du, _bin("*", _num("2"), _call("sqrt", u)))
    raise TypeError(f"cannot differentiate function {name!r}")


def _literal_text(node: Node) -> str:
    """Render a sign-prefixed numeric exponent node as a decimal string."""
    if isinstance(node, Num):
        return node.text
    if isinstance(node, Unary) and isinstance(node.operand, Num):
        sign = "-" if node.op == "-" else ""
        return f"{sign}{node.operand.text}"
    raise TypeError("exponent is not a numeric literal (grammar should enforce)")


def _decrement_literal(text: str) -> str:
    """Compute ``text - 1`` exactly for a decimal numeric literal."""
    try:
        value = Decimal(text) - Decimal(1)
    except InvalidOperation as exc:  # pragma: no cover - grammar guarantees digits
        raise TypeError(f"invalid numeric literal {text!r}") from exc
    # Decimal.__str__ keeps a finite, exact decimal representation the parser
    # accepts (it may use scientific notation, which the tokenizer supports).
    return format(value, "f").rstrip("0").rstrip(".") or "0"
