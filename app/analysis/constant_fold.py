"""Constant-condition evaluation for branch elimination.

Only *obvious* boolean constants are folded, and the decision is used solely
to drop an unreachable edge of if/while nodes.  Nothing else about runtime
values is tracked, so this never introduces unsoundness: folding the wrong
way is impossible for the patterns recognized here (literals and boolean
operators over literals / validator calls).

Returns:
    True / False  -- the condition is a compile-time constant boolean
    None          -- unknown; both branches remain possible
"""

from __future__ import annotations

from ..language import BinaryOp, BoolLit, Call, Expr, UnaryOp
from .policy import classify


def const_truth(expr: Expr, policy: dict) -> bool | None:
    if isinstance(expr, BoolLit):
        return expr.value
    if isinstance(expr, UnaryOp) and expr.op == "not":
        inner = const_truth(expr.operand, policy)
        return None if inner is None else not inner
    if isinstance(expr, BinaryOp):
        if expr.op == "and":
            l, r = const_truth(expr.left, policy), const_truth(expr.right, policy)
            if l is False or r is False:
                return False
            if l is True and r is True:
                return True
            return None
        if expr.op == "or":
            l, r = const_truth(expr.left, policy), const_truth(expr.right, policy)
            if l is True or r is True:
                return True
            if l is False and r is False:
                return False
            return None
    if isinstance(expr, Call) and classify(policy, expr.callee) == "validator":
        # validator results are runtime-dependent
        return None
    return None
