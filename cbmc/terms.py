"""The contract expression language: sorting, Z3 compilation, concrete eval.

Expressions are tagged JSON values from a closed whitelist — there is no
``eval`` and no string expression anywhere.

Integer expressions (sort ``int``)::

    {"lit": 7}
    {"var": "alice"}
    {"op": "neg", "args": [e]}
    {"op": "+", "args": [e1, e2]}          # also "-" , "*"

Boolean expressions (sort ``bool``)::

    {"bool": true}
    {"var": "locked"}
    {"op": "not", "args": [e]}
    {"op": "and", "args": [e1, e2]}        # also "or"
    {"op": "==", "args": [e1, e2]}         # int compare; also !=,<,<=,>,>=
    {"op": "ite", "args": [c, t, f]}       # branches share one sort

Math-integer arithmetic is exact (unbounded). An int expression whose leaves
include a bit-vector variable/parameter of width ``w`` is compiled to a
w-bit *unsigned* machine integer: ``+ - * neg`` wrap modulo 2**w and the
comparisons are unsigned. All bv leaves in one expression must share the same
width (mixing widths or bv with math ints is rejected at load time).
"""

from __future__ import annotations

from typing import Any

from .errors import ContractError, UnsupportedOperator

ARITH_OPS: dict[str, int] = {"+": 2, "-": 2, "*": 2, "neg": 1}
COMPARE_OPS: dict[str, int] = {
    "==": 2, "!=": 2, "<": 2, "<=": 2, ">": 2, ">=": 2,
}
BOOLEAN_OPS: dict[str, int] = {"and": 2, "or": 2, "not": 1}
ITE_OP = "ite"
INTEGER_OPS = tuple(ARITH_OPS) + tuple(COMPARE_OPS) + (ITE_OP,)
ALL_OPS = tuple(ARITH_OPS) + tuple(COMPARE_OPS) + tuple(BOOLEAN_OPS) + (ITE_OP,)


# --------------------------------------------------------------------------- #
# Well-sortedness
# --------------------------------------------------------------------------- #

def _tag(expr: Any, path: str) -> tuple[str, Any]:
    if not isinstance(expr, dict):
        raise ContractError(
            f"expression must be an object, got {type(expr).__name__}", path
        )
    if "lit" in expr:
        return "lit", expr["lit"]
    if "bool" in expr:
        return "bool", expr["bool"]
    if "var" in expr:
        return "var", expr["var"]
    if "op" in expr:
        return "op", expr["op"]
    raise ContractError(
        "expression needs exactly one of 'lit', 'bool', 'var' or 'op'", path
    )


def _check_args(expr: dict[str, Any], arity: int, path: str) -> list[Any]:
    args = expr.get("args")
    if not isinstance(args, list) or len(args) != arity:
        raise ContractError(
            f"operator {expr.get('op')!r} needs exactly {arity} arg(s)", path
        )
    return args


def _merge_int_signatures(
    sigs: list[tuple[bool, set[int]]], op: str, path: str
) -> int | None:
    """Validate integer-operand width discipline and return the bv width.

    Each signature is ``(has_math_int_var, {bv widths})``. Literal-only
    operands carry neither and are polymorphic (promoted to bv when needed).
    A math-integer *variable* can never be mixed with a bit-vector, and two
    distinct bv widths cannot be mixed.
    """
    has_math = any(m for m, _ in sigs)
    widths: set[int] = set()
    for _, ws in sigs:
        widths |= ws
    if has_math and widths:
        raise ContractError(
            f"operator {op!r}: cannot mix mathematical integers with "
            f"{next(iter(widths))}-bit machine integers",
            path,
        )
    if len(widths) > 1:
        raise ContractError(
            f"operator {op!r}: cannot mix bit-vector widths {sorted(widths)}",
            path,
        )
    return next(iter(widths), None)


# Internal analysis returns (sort, has_math_int_var, bv_widths).
def _analyze(
    expr: dict[str, Any], scope: dict[str, Any], path: str,
    expected: str | None = None,
) -> tuple[str, bool, set[int]]:
    kind, value = _tag(expr, path)

    if kind == "lit":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ContractError("'lit' must be an integer", path)
        result = ("int", False, set())
    elif kind == "bool":
        if not isinstance(value, bool):
            raise ContractError("'bool' must be true or false", path)
        result = ("bool", False, set())
    elif kind == "var":
        if not isinstance(value, str) or value not in scope:
            raise ContractError(f"unknown variable {value!r}", path)
        v = scope[value]
        if v.sort == "int":
            result = ("int", v.width is None,
                      set() if v.width is None else {v.width})
        else:
            result = ("bool", False, set())
    else:
        op = value
        if op not in ALL_OPS:
            raise UnsupportedOperator(op, path, ALL_OPS)
        args = _check_args(expr, _arity(op), path)

        if op in ARITH_OPS:
            sigs = [_analyze(a, scope, f"{path}.args[{i}]", "int")
                    for i, a in enumerate(args)]
            width = _merge_int_signatures(
                [(m, ws) for _, m, ws in sigs], op, path)
            result = ("int", any(m for _, m, _ in sigs),
                      {width} if width is not None else set())
        elif op in COMPARE_OPS:
            sigs = [_analyze(a, scope, f"{path}.args[{i}]", "int")
                    for i, a in enumerate(args)]
            _merge_int_signatures([(m, ws) for _, m, ws in sigs], op, path)
            result = ("bool", False, set())
        elif op in BOOLEAN_OPS:
            for i, a in enumerate(args):
                _analyze(a, scope, f"{path}.args[{i}]", "bool")
            result = ("bool", False, set())
        else:  # ite
            _analyze(args[0], scope, f"{path}.args[0]", "bool")
            t = _analyze(args[1], scope, f"{path}.args[1]")
            f = _analyze(args[2], scope, f"{path}.args[2]")
            if t[0] != f[0]:
                raise ContractError(
                    "'ite' branches must have the same sort", path)
            if t[0] == "int":
                width = _merge_int_signatures(
                    [(t[1], t[2]), (f[1], f[2])], ITE_OP, path)
                result = ("int", t[1] or f[1],
                          {width} if width is not None else set())
            else:
                result = ("bool", False, set())

    if expected is not None and result[0] != expected:
        raise ContractError(
            f"expected {expected} expression but found {result[0]} expression",
            path,
        )
    return result


def sort_expr(
    expr: dict[str, Any],
    scope: dict[str, Any],
    path: str,
    expected: str | None = None,
) -> tuple[str, int | None]:
    """Validate ``expr``; return its sort (``"int" | "bool"``) and bv width.

    ``scope`` maps visible names to objects exposing ``.sort``/``.width``
    (i.e. :class:`~cbmc.model.Var` or :class:`~cbmc.model.Param`).
    Integer *literals* are polymorphic and get promoted to bit-vector width
    when their surrounding expression is a machine-integer expression.
    """
    sort, _has_math, widths = _analyze(expr, scope, path, expected)
    width = next(iter(widths), None) if sort == "int" else None
    return sort, width


def int_signature(
    expr: dict[str, Any], scope: dict[str, Any], path: str = "<int>"
) -> tuple[bool, set[int]]:
    """Return ``(has_math_int_var, bv_widths)`` for a well-sorted int expr."""
    sort, has_math, widths = _analyze(expr, scope, path, "int")
    return has_math, widths


def _arity(op: str) -> int:
    for table in (ARITH_OPS, COMPARE_OPS, BOOLEAN_OPS):
        if op in table:
            return table[op]
    return 3  # ite


def node_width(expr: dict[str, Any], widths: dict[str, int | None]) -> int | None:
    """Infer the bv width of an int expression from its variable leaves."""
    kind, value = _tag(expr, "<width>")
    if kind == "var":
        return widths.get(value)
    if kind in ("lit", "bool"):
        return None
    op = value
    if op in ARITH_OPS or op in COMPARE_OPS or op == ITE_OP:
        found = [w for a in expr["args"]
                 if (w := node_width(a, widths)) is not None]
        return found[0] if found else None
    return None


def _mask(value: int, width: int) -> int:
    return value % (1 << width)


def evaluate(expr: dict[str, Any], env: dict[str, Any],
             widths: dict[str, int | None],
             force_width: int | None = None) -> Any:
    """Concretely evaluate ``expr`` against python values in ``env``.

    Bit-vector arithmetic wraps with two's-complement unsigned semantics,
    matching the Z3 compilation in :class:`SymbolicBuilder`. ``force_width``
    promotes bare integer literals to a machine width (used for bv targets).
    """
    kind, value = _tag(expr, "<eval>")
    if kind == "lit":
        width = force_width
        return _mask(int(value), width) if width is not None else int(value)
    if kind == "bool":
        return bool(value)
    if kind == "var":
        if value not in env:
            raise ContractError(f"unbound variable {value!r} during eval", "<eval>")
        v = env[value]
        width = widths.get(value)
        return _mask(v, width) if width is not None else v

    op = value
    width = node_width(expr, widths) or force_width
    args = [evaluate(a, env, widths, width) for a in expr["args"]]

    if op in ("+", "-", "*"):
        x, y = args
        out = {"+": x + y, "-": x - y, "*": x * y}[op]
        return _mask(out, width) if width is not None else out
    if op == "neg":
        out = -args[0]
        return _mask(out, width) if width is not None else out
    if op in COMPARE_OPS:
        x, y = args
        return {
            "==": x == y, "!=": x != y, "<": x < y, "<=": x <= y,
            ">": x > y, ">=": x >= y,
        }[op]
    if op == "and":
        return bool(args[0]) and bool(args[1])
    if op == "or":
        return bool(args[0]) or bool(args[1])
    if op == "not":
        return not bool(args[0])
    if op == ITE_OP:
        return args[1] if args[0] else args[2]
    raise UnsupportedOperator(op, "<eval>", ALL_OPS)


class SymbolicBuilder:
    """Compiles JSON expressions into Z3 terms given a symbol table.

    ``force_width`` promotes integer literals to a bit-vector width when the
    surrounding target expression is a machine integer (literals have no
    intrinsic width). Variable widths come from ``widths``.
    """

    def __init__(self, widths: dict[str, int | None],
                 force_width: int | None = None):
        self.widths = widths
        self.force_width = force_width

    def _scoped(self, width: int | None) -> "SymbolicBuilder":
        return SymbolicBuilder(self.widths, width)

    def build(self, expr: dict[str, Any], sym: dict[str, Any]) -> Any:
        import z3

        kind, value = _tag(expr, "<z3>")
        if kind == "lit":
            width = node_width(expr, self.widths) or self.force_width
            if width is not None:
                return z3.BitVecVal(value % (1 << width), width)
            return z3.IntVal(value)
        if kind == "bool":
            return z3.BoolVal(value)
        if kind == "var":
            return sym[value]

        op = value
        width = node_width(expr, self.widths) or self.force_width
        args = [self._scoped(width).build(a, sym) for a in expr["args"]]

        if op in ("+", "-", "*"):
            x, y = args
            return {"+": x + y, "-": x - y, "*": x * y}[op]
        if op == "neg":
            return -args[0]
        if op in COMPARE_OPS:
            x, y = args
            if width is not None:  # unsigned machine-integer comparison
                cmp = {
                    "==": lambda a, b: a == b,
                    "!=": lambda a, b: a != b,
                    "<": z3.ULT, "<=": z3.ULE, ">": z3.UGT, ">=": z3.UGE,
                }[op]
                return cmp(x, y)
            return {
                "==": x == y, "!=": x != y, "<": x < y, "<=": x <= y,
                ">": x > y, ">=": x >= y,
            }[op]
        if op == "and":
            return z3.And(args[0], args[1])
        if op == "or":
            return z3.Or(args[0], args[1])
        if op == "not":
            return z3.Not(args[0])
        if op == ITE_OP:
            return z3.If(args[0], args[1], args[2])
        raise UnsupportedOperator(op, "<z3>", ALL_OPS)
