"""Runtime value helpers shared by the source and IR interpreters.

Slang values: int, bool, str, None (``null``) and first-class function values.
The interpreters keep their own representation of functions, but value
formatting, truthiness and the built-in binary/unary operations live here so
the two interpreters agree on observable behavior.
"""

from __future__ import annotations

from .errors import RuntimeErr


def is_truthy(v) -> bool:
    """Slang truthiness (mirrors a typical small dynamic language)."""

    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v != 0
    if isinstance(v, str):
        return len(v) > 0
    # Functions and any other object: raise rather than silently truth-testing.
    raise RuntimeErr(f"a value of type {type_name(v)} cannot be used in a boolean context")


def type_name(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, str):
        return "str"
    # Closure types expose ``slang_type``.
    t = getattr(v, "slang_type", None)
    if t is not None:
        return t
    return type(v).__name__


def format_value(v) -> str:
    """Format a value the way the ``print`` built-in prints it."""

    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int) or isinstance(v, str):
        return str(v)
    t = getattr(v, "slang_type", None)
    if t == "closure":
        name = getattr(v, "display_name", "<lambda>") or "<lambda>"
        return f"<closure {name}>"
    if t == "builtin":
        return f"<builtin {getattr(v, 'name', '?')}>"
    return f"<{type_name(v)}>"


def _int_operands(op: str, a, b):
    if isinstance(a, bool) or isinstance(b, bool) or not isinstance(a, int) or not isinstance(b, int):
        raise RuntimeErr(
            f"operator '{op}' requires int operands, got {type_name(a)} and {type_name(b)}"
        )
    return a, b


def eval_binary(op: str, a, b):
    if op == "+":
        # String concatenation when either side is a string; int addition otherwise.
        if isinstance(a, str) or isinstance(b, str):
            if isinstance(a, bool) or isinstance(b, bool):
                raise RuntimeErr("operator '+' cannot concatenate bool with str")
            if not isinstance(a, (int, str)) or not isinstance(b, (int, str)):
                raise RuntimeErr(
                    f"operator '+' requires int or str operands, got {type_name(a)} and {type_name(b)}"
                )
            return _to_string(a) + _to_string(b)
        return _int_operands("+", a, b)[0] + _int_operands("+", a, b)[1]
    if op == "-":
        x, y = _int_operands("-", a, b)
        return x - y
    if op == "*":
        x, y = _int_operands("*", a, b)
        return x * y
    if op == "/":
        x, y = _int_operands("/", a, b)
        if y == 0:
            raise RuntimeErr("integer division by zero")
        # Truncation toward zero, like C/JS (Python's // floors).
        q = abs(x) // abs(y)
        return q if (x < 0) == (y < 0) else -q
    if op == "%":
        x, y = _int_operands("%", a, b)
        if y == 0:
            raise RuntimeErr("integer modulo by zero")
        r = abs(x) % abs(y)
        return (r if x >= 0 else -r)
    if op in ("<", "<=", ">", ">="):
        if isinstance(a, bool) or isinstance(b, bool):
            raise RuntimeErr(f"operator '{op}' cannot compare bool values")
        if isinstance(a, int) and isinstance(b, int):
            pass
        elif isinstance(a, str) and isinstance(b, str):
            pass
        else:
            raise RuntimeErr(
                f"operator '{op}' requires two int or two str operands, got {type_name(a)} and {type_name(b)}"
            )
        if op == "<":
            return a < b
        if op == "<=":
            return a <= b
        if op == ">":
            return a > b
        return a >= b
    if op == "==":
        return _equal(a, b)
    if op == "!=":
        return not _equal(a, b)
    if op == "&&":
        return a if not is_truthy(a) else b
    if op == "||":
        return a if is_truthy(a) else b
    raise RuntimeErr(f"unknown binary operator {op!r}")


def _equal(a, b) -> bool:
    # Nulls are equal; ints/bools/strings compare by value with no cross-type
    # coercion (True != 1).  Function values are reference-identical.
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, (int, str)):
        return a == b
    return a is b


def eval_unary(op: str, a):
    if op == "-":
        if isinstance(a, bool) or not isinstance(a, int):
            raise RuntimeErr(f"unary '-' requires an int, got {type_name(a)}")
        return -a
    if op == "!":
        return not is_truthy(a)
    raise RuntimeErr(f"unknown unary operator {op!r}")


def _to_string(v) -> str:
    if isinstance(v, str):
        return v
    return format_value(v)
