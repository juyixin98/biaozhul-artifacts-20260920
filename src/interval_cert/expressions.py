"""Restricted expression language: parsing, validation, evaluation, differentiation.

The language is a deliberately small, continuously differentiable fragment:

- one free variable (default ``x``) and the constants ``pi`` / ``e``
- binary ``+  -  *  /  **`` (``^`` is accepted as an alias for ``**``)
- unary ``+  -``
- functions ``sin  cos  exp  log  sqrt``
- numeric literals

``**`` supports non-negative/negative *integer* exponents on any base, and
arbitrary exponents when the base is strictly positive (via ``exp(y*log x)``).

Everything outside this fragment is rejected at parse time with an
``UnsupportedExpressionError`` so the kernel never evaluates unknown nodes.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Callable, Union

from mpmath import iv, mp, mpf

from .errors import (
    DomainEvaluationError,
    ExpressionSyntaxError,
    UnsupportedExpressionError,
)
from .intervals import Interval, contains_zero, int_pow, safe_divide, scalar

ALLOWED_FUNCTIONS = ("sin", "cos", "exp", "log", "sqrt")
CONSTANTS = ("pi", "e")

_BIN_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
_UNARY_OPS = (ast.UAdd, ast.USub)


# ---------------------------------------------------------------------------
# Parsing / validation
# ---------------------------------------------------------------------------


def parse_expression(text: str, variable: str) -> ast.AST:
    """Parse ``text`` into a validated AST for the restricted language."""
    if not isinstance(text, str) or not text.strip():
        raise ExpressionSyntaxError("expression must be a non-empty string")
    if not variable.isidentifier():
        raise ExpressionSyntaxError(f"invalid variable name: {variable!r}")
    source = text.replace("^", "**")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ExpressionSyntaxError(
            f"cannot parse expression: {exc.msg} (offset {exc.offset})"
        ) from exc
    _validate_node(tree.body, variable, path="root")
    return tree.body


def _validate_node(node: ast.AST, variable: str, path: str) -> None:
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, _BIN_OPS):
            raise UnsupportedExpressionError(
                f"operator {type(node.op).__name__} is not supported at {path}"
            )
        _validate_node(node.left, variable, f"{path}/left")
        _validate_node(node.right, variable, f"{path}/right")
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, _UNARY_OPS):
            raise UnsupportedExpressionError(
                f"unary operator {type(node.op).__name__} is not supported at {path}"
            )
        _validate_node(node.operand, variable, f"{path}/operand")
        return
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_FUNCTIONS:
            name = ast.unparse(node.func) if isinstance(node.func, ast.AST) else "?"
            raise UnsupportedExpressionError(
                f"function {name!r} is not supported at {path}; "
                f"allowed: {', '.join(ALLOWED_FUNCTIONS)}"
            )
        if node.keywords or len(node.args) != 1:
            raise UnsupportedExpressionError(
                f"function {node.func.id} takes exactly one positional argument at {path}"
            )
        _validate_node(node.args[0], variable, f"{path}/arg")
        return
    if isinstance(node, ast.Name):
        if node.id == variable or node.id in CONSTANTS:
            return
        raise UnsupportedExpressionError(
            f"unknown name {node.id!r} at {path}; the only variable is {variable!r} "
            f"and constants are {', '.join(CONSTANTS)}"
        )
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise UnsupportedExpressionError(
                f"literal {node.value!r} is not a real number at {path}"
            )
        return
    raise UnsupportedExpressionError(
        f"syntax element {type(node).__name__} is not supported at {path}"
    )


def expression_to_source(node: ast.AST) -> str:
    return ast.unparse(node)


# ---------------------------------------------------------------------------
# Symbolic differentiation (AST -> AST)
# ---------------------------------------------------------------------------


def _const(value) -> ast.AST:
    return ast.Constant(value=value)


def _bin(op, left, right) -> ast.AST:
    return ast.BinOp(op=op(), left=left, right=right)


def _neg(operand) -> ast.AST:
    return ast.UnaryOp(op=ast.USub(), operand=operand)


def _call(name: str, arg: ast.AST) -> ast.AST:
    return ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=[arg], keywords=[])


def differentiate(node: ast.AST, variable: str) -> ast.AST:
    """Return the AST of d(node)/d(variable) inside the same language."""
    if isinstance(node, ast.Constant):
        return _const(0)
    if isinstance(node, ast.Name):
        return _const(1 if node.id == variable else 0)
    if isinstance(node, ast.UnaryOp):
        inner = differentiate(node.operand, variable)
        return inner if isinstance(node.op, ast.UAdd) else _neg(inner)
    if isinstance(node, ast.BinOp):
        u, v = node.left, node.right
        du, dv = differentiate(u, variable), differentiate(v, variable)
        if isinstance(node.op, ast.Add):
            return _bin(ast.Add, du, dv)
        if isinstance(node.op, ast.Sub):
            return _bin(ast.Sub, du, dv)
        if isinstance(node.op, ast.Mult):
            return _bin(ast.Add, _bin(ast.Mult, du, v), _bin(ast.Mult, u, dv))
        if isinstance(node.op, ast.Div):
            numerator = _bin(ast.Sub, _bin(ast.Mult, du, v), _bin(ast.Mult, u, dv))
            denominator = _bin(ast.Pow, v, _const(2))
            return _bin(ast.Div, numerator, denominator)
        if isinstance(node.op, ast.Pow):
            if isinstance(v, ast.Constant) and isinstance(v.value, int):
                # d/dx u**n = n * u**(n-1) * u'
                coeff = _const(v.value)
                power = _bin(ast.Pow, u, _const(v.value - 1))
                return _bin(ast.Mult, _bin(ast.Mult, coeff, power), du)
            # general case: u**v = exp(v * log u), requires u > 0 at evaluation
            return _bin(
                ast.Mult,
                _bin(ast.Pow, u, v),
                _bin(
                    ast.Add,
                    _bin(ast.Mult, dv, _call("log", u)),
                    _bin(ast.Mult, v, _bin(ast.Div, du, u)),
                ),
            )
    if isinstance(node, ast.Call):
        name = node.func.id
        u = node.args[0]
        du = differentiate(u, variable)
        if name == "sin":
            return _bin(ast.Mult, _call("cos", u), du)
        if name == "cos":
            return _neg(_bin(ast.Mult, _call("sin", u), du))
        if name == "exp":
            return _bin(ast.Mult, _call("exp", u), du)
        if name == "log":
            return _bin(ast.Div, du, u)
        if name == "sqrt":
            return _bin(ast.Div, du, _bin(ast.Mult, _const(2), _call("sqrt", u)))
    raise UnsupportedExpressionError(
        f"cannot differentiate node {type(node).__name__}"
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

PointValue = Union[mpf, Interval]


@dataclass
class _Context:
    variable: str
    mode: str  # "interval" | "point"

    def constant(self, value) -> PointValue:
        if self.mode == "interval":
            return iv.mpf(value)
        return mpf(value)


def evaluate_interval(node: ast.AST, variable: str, x: Interval) -> Interval:
    """Outward-rounded interval evaluation of ``node`` at interval ``x``."""
    return _eval(node, _Context(variable, "interval"), x, path="root")


def evaluate_point(node: ast.AST, variable: str, x) -> mpf:
    """High-precision point evaluation of ``node`` at scalar ``x``."""
    return _eval(node, _Context(variable, "point"), mpf(x), path="root")


def _eval(node: ast.AST, ctx: _Context, x: PointValue, path: str) -> PointValue:
    if isinstance(node, ast.Constant):
        return ctx.constant(node.value)
    if isinstance(node, ast.Name):
        if node.id == ctx.variable:
            return x
        if node.id == "pi":
            return ctx.constant(mp.pi)
        if node.id == "e":
            return ctx.constant(mp.e)
        raise UnsupportedExpressionError(f"unknown name {node.id!r} at {path}")
    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, ctx, x, f"{path}/operand")
        return operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.BinOp):
        left = _eval(node.left, ctx, x, f"{path}/left")
        right = _eval(node.right, ctx, x, f"{path}/right")
        return _eval_binop(node.op, left, right, ctx, path)
    if isinstance(node, ast.Call):
        arg = _eval(node.args[0], ctx, x, f"{path}/arg")
        return _eval_function(node.func.id, arg, ctx, path)
    raise UnsupportedExpressionError(f"cannot evaluate node {type(node).__name__}")


def _eval_binop(op, left, right, ctx: _Context, path: str) -> PointValue:
    if isinstance(op, ast.Add):
        return left + right
    if isinstance(op, ast.Sub):
        return left - right
    if isinstance(op, ast.Mult):
        return left * right
    if isinstance(op, ast.Div):
        return _divide(left, right, ctx, path)
    if isinstance(op, ast.Pow):
        return _power(left, right, ctx, path)
    raise UnsupportedExpressionError(f"operator {type(op).__name__} not supported")


def _divide(left, right, ctx: _Context, path: str) -> PointValue:
    if ctx.mode == "interval":
        return safe_divide(left, right, location=path)
    if right == 0:
        raise DomainEvaluationError(
            "division by zero", location=path, interval=str(right),
            reason="divisor_is_zero",
        )
    return left / right


def _power(base, exponent, ctx: _Context, path: str) -> PointValue:
    # Integer exponents are exact and defined for any base (n >= 0) or any
    # non-zero base (n < 0).
    int_exp = _as_int(exponent)
    if int_exp is not None:
        if int_exp >= 0:
            if ctx.mode == "interval":
                return int_pow(base, int_exp)
            return base ** int_exp
        # negative integer exponent -> 1 / base**(-n)
        if ctx.mode == "interval":
            if contains_zero(base):
                raise DomainEvaluationError(
                    "negative integer power of an interval containing zero",
                    location=path, reason="base_straddles_zero",
                )
            return safe_divide(iv.mpf(1), int_pow(base, -int_exp), location=path)
        if base == 0:
            raise DomainEvaluationError(
                "negative integer power of zero", location=path,
                reason="base_is_zero",
            )
        return base ** int_exp
    # General exponent: defined only for a strictly positive base.
    if ctx.mode == "interval":
        if base.a <= 0:
            raise DomainEvaluationError(
                "non-integer power requires a strictly positive base interval",
                location=path, reason="base_not_positive",
            )
        return iv.exp(exponent * iv.log(base))
    if base <= 0:
        raise DomainEvaluationError(
            "non-integer power requires a positive base",
            location=path, reason="base_not_positive",
        )
    return mp.exp(exponent * mp.log(base))


def _as_int(value) -> int | None:
    try:
        if isinstance(value, Interval):
            if value.a == value.b:
                as_scalar = scalar(value.a)
                if as_scalar == int(as_scalar):
                    return int(as_scalar)
            return None
        if value == int(value):
            return int(value)
    except (ValueError, OverflowError, TypeError):
        return None
    return None


def _eval_function(name: str, arg, ctx: _Context, path: str) -> PointValue:
    interval_mode = ctx.mode == "interval"
    try:
        if name == "sin":
            return iv.sin(arg) if interval_mode else mp.sin(arg)
        if name == "cos":
            return iv.cos(arg) if interval_mode else mp.cos(arg)
        if name == "exp":
            return iv.exp(arg) if interval_mode else mp.exp(arg)
        if name == "log":
            _require_positive(arg, "log", path)
            return iv.log(arg) if interval_mode else mp.log(arg)
        if name == "sqrt":
            _require_nonnegative(arg, "sqrt", path)
            return iv.sqrt(arg) if interval_mode else mp.sqrt(arg)
    except (ValueError, ZeroDivisionError) as exc:
        raise DomainEvaluationError(
            f"{name} evaluation failed: {exc}", location=path,
            reason="library_domain_error",
        ) from exc
    raise UnsupportedExpressionError(f"function {name!r} is not supported")


def _require_positive(arg, func: str, path: str) -> None:
    if isinstance(arg, Interval):
        if arg.a <= 0:
            raise DomainEvaluationError(
                f"{func} requires a strictly positive argument",
                location=path, interval=f"[{scalar(arg.a)}, {scalar(arg.b)}]",
                reason="argument_not_positive",
            )
    elif arg <= 0:
        raise DomainEvaluationError(
            f"{func} requires a strictly positive argument",
            location=path, interval=str(arg), reason="argument_not_positive",
        )


def _require_nonnegative(arg, func: str, path: str) -> None:
    if isinstance(arg, Interval):
        if arg.a < 0:
            raise DomainEvaluationError(
                f"{func} requires a non-negative argument",
                location=path, interval=f"[{scalar(arg.a)}, {scalar(arg.b)}]",
                reason="argument_negative",
            )
    elif arg < 0:
        raise DomainEvaluationError(
            f"{func} requires a non-negative argument",
            location=path, interval=str(arg), reason="argument_negative",
        )


def make_callable(node: ast.AST, variable: str) -> Callable[[Any], Any]:
    """High-precision mpf evaluator used only for *approximate*
    (non-certified) root refinement — never for certification."""

    def fn(x):
        return evaluate_point(node, variable, x)

    return fn
