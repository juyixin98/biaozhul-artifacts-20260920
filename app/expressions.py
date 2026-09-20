"""Restricted condition expressions.

Conditions may only compare workflow variables against literals using a small
whitelisted grammar.  Expressions are parsed with the standard ``ast`` module
but **never** compiled or evaluated with :func:`eval` — the AST is walked
manually, so attribute access, imports, calls and dunder tricks cannot execute
arbitrary code.

Supported grammar::

    expr        := or_expr
    or_expr     := and_expr ("or" and_expr)*
    and_expr    := not_expr ("and" not_expr)*
    not_expr    := "not" not_expr | comparison
    comparison  := operand (op operand)*
    op          := "==" | "!=" | "<" | "<=" | ">" | ">="
    operand     := name | literal | "-" operand
    literal     := number | string | bool | None
    name        := identifier looked up in the supplied context

Missing variables evaluate to ``None``.  Any comparison involving ``None``
other than ``==`` / ``!=`` evaluates to ``False`` (SQL-like NULL semantics),
which makes a missing-variable branch simply not match rather than raising.
"""
from __future__ import annotations

import ast
from typing import Any

_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.UnaryOp,
    ast.BinOp,
    ast.Compare,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.USub,
    ast.UAdd,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
)

_OPS = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
}

_ORDERED_OPS = {
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}

_ALLOWED_CONSTANTS = (str, int, float, bool, type(None))


class ConditionSyntaxError(ValueError):
    """Raised when an expression uses anything outside the whitelist."""


def parse_expression(source: str) -> ast.Expression:
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ConditionSyntaxError(f"invalid expression: {exc.msg}") from exc
    _validate(tree)
    return tree


def _validate(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ConditionSyntaxError(
                f"forbidden syntax: {type(node).__name__}"
            )
        if isinstance(node, ast.Constant) and not isinstance(
            node.value, _ALLOWED_CONSTANTS
        ):
            raise ConditionSyntaxError(
                f"literal of type {type(node.value).__name__} is not allowed"
            )
        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                raise ConditionSyntaxError("dunder names are not allowed")


def evaluate(source: str, context: dict[str, Any]) -> bool:
    """Evaluate a previously unparsed expression against ``context``.

    Raises :class:`ConditionSyntaxError` for disallowed syntax.  Type errors
    *inside* a comparison (e.g. ``"a" < 2``) make that comparison False rather
    than aborting the workflow.
    """
    tree = parse_expression(source)
    return bool(_eval_node(tree.body, context))


def _eval_node(node: ast.AST, ctx: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, ctx)
    if isinstance(node, ast.Name):
        return ctx.get(node.id)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _eval_node(node.operand, ctx)
        return -value if isinstance(node.op, ast.USub) else +value
    if isinstance(node, ast.BoolOp):
        values = [_eval_node(v, ctx) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        return any(values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_node(node.operand, ctx)
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, ctx)
        for op_node, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval_node(comparator, ctx)
            if not _compare(left, op_node, right):
                return False
            left = right
        return True
    raise ConditionSyntaxError(f"unsupported node: {type(node).__name__}")


def _compare(left: Any, op_node: ast.cmpop, right: Any) -> bool:
    op_type = type(op_node)
    if op_type in _OPS:
        return _OPS[op_type](left, right)
    if op_type in _ORDERED_OPS:
        if left is None or right is None:
            return False
        try:
            return _ORDERED_OPS[op_type](left, right)
        except TypeError:
            return False
    raise ConditionSyntaxError(f"unsupported operator: {op_type.__name__}")
