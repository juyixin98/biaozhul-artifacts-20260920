"""Integer semantics shared by the concrete interpreters and SCCP's
abstract evaluator.

Rules:
* arbitrary precision integers (Python ints);
* ``/`` and ``%`` follow truncation-toward-zero semantics (C/JS style):
      q = trunc(a / b);  a = q * b + (a % b)
* division/modulo by zero raises :class:`ZeroDivisionTrapped`;
* comparisons yield 0 / 1; ``&&`` / ``||`` are non-short-circuiting.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import Span


class ZeroDivisionTrapped(Exception):
    """Raised when ``/`` or ``%`` is executed with a zero divisor."""

    def __init__(self, span: Span | None = None):
        super().__init__("division or modulo by zero")
        self.span = span


@dataclass(frozen=True)
class Trap:
    """Sentinel: this operand combination definitely traps.

    Used only inside the abstract evaluator; never stored in a lattice cell.
    """


TRAP = Trap()


def trunc_div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionTrapped()
    # Python // floors; emulate truncation toward zero.
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def trunc_mod(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionTrapped()
    return a - trunc_div(a, b) * b


def apply_unary(op: str, x: int) -> int:
    if op == "neg":
        return -x
    if op == "not":
        return int(x == 0)
    raise ValueError(f"unknown unary op {op}")  # pragma: no cover


def apply_binary(op: str, a: int, b: int) -> int:
    if op == "add":
        return a + b
    if op == "sub":
        return a - b
    if op == "mul":
        return a * b
    if op == "div":
        return trunc_div(a, b)
    if op == "mod":
        return trunc_mod(a, b)
    if op == "eq":
        return int(a == b)
    if op == "ne":
        return int(a != b)
    if op == "lt":
        return int(a < b)
    if op == "le":
        return int(a <= b)
    if op == "gt":
        return int(a > b)
    if op == "ge":
        return int(a >= b)
    if op == "and":
        return int(a != 0 and b != 0)
    if op == "or":
        return int(a != 0 or b != 0)
    raise ValueError(f"unknown binary op {op}")  # pragma: no cover
