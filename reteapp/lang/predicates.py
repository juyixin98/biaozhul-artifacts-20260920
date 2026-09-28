"""Predicate evaluation shared by the compiler plans and the Rete network.

Kept free of any network state so it can be unit-tested in isolation and
reused by the brute-force reference matcher (which must NOT import the core).
"""

from __future__ import annotations

from typing import Any

from .model import Operator


class PredicateError(TypeError):
    """Raised when operands cannot be compared (never silently treated as false)."""


def _comparable(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return True
    return type(left) is type(right)


def evaluate(op: Operator, left: Any, right: Any) -> bool:
    """Evaluate one constraint. Ordering operators require comparable types."""

    if op is Operator.EQ:
        return left == right
    if op is Operator.NEQ:
        return left != right
    if op is Operator.IN:
        return left in right
    if op is Operator.NOT_IN:
        return left not in right
    if not _comparable(left, right):
        raise PredicateError(
            f"cannot apply {op.value!r} between {type(left).__name__} and {type(right).__name__}"
        )
    if op is Operator.LT:
        return left < right
    if op is Operator.LTE:
        return left <= right
    if op is Operator.GT:
        return left > right
    if op is Operator.GTE:
        return left >= right
    raise PredicateError(f"unsupported operator {op!r}")
