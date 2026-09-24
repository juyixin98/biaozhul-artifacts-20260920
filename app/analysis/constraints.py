"""Condition-based sanitization ("guard refinement").

On a branch governed by a *validator* call, the validator's root arguments
are treated as clean on the TRUE branch (and tainted-check-rejected pattern
on the FALSE branch of a negated validator).

Only validator calls are recognized.  Equality with a literal is deliberately
NOT a sanitizer: taint is a provenance property and an attacker-controlled
string can equal the literal at runtime.  Unrecognized guards simply yield no
refinement, which can only cause false positives -- never false negatives.
"""

from __future__ import annotations

from ..language import BinaryOp, Call, Expr, UnaryOp, Variable
from .policy import classify


def sanitized_by_condition(
    cond: Expr,
    polarity: bool,
    policy: dict,
) -> set[str]:
    """Variable names that are provably clean on the given branch."""
    return _solve(cond, polarity, policy)


def _solve(expr: Expr, polarity: bool, policy: dict) -> set[str]:
    # Unwrap logical negation by flipping polarity.
    if isinstance(expr, UnaryOp) and expr.op == "not":
        return _solve(expr.operand, not polarity, policy)
    if isinstance(expr, BinaryOp):
        if expr.op == "and":
            lp, rp = _solve(expr.left, True, policy), _solve(expr.right, True, policy)
            ln, rn = _solve(expr.left, False, policy), _solve(expr.right, False, policy)
            return (lp & rp) if polarity else (ln | rn)
        if expr.op == "or":
            lp, rp = _solve(expr.left, True, policy), _solve(expr.right, True, policy)
            ln, rn = _solve(expr.left, False, policy), _solve(expr.right, False, policy)
            return (lp | rp) if polarity else (ln & rn)
        return set()
    if isinstance(expr, Call) and classify(policy, expr.callee) == "validator":
        if polarity:
            names: set[str] = set()
            for arg in expr.args:
                names |= _root_names(arg)
            return names
    return set()


def _root_names(expr: Expr) -> set[str]:
    """Reduce a validator argument to the variable(s) it directly governs."""
    if isinstance(expr, Variable):
        return {expr.name}
    if isinstance(expr, BinaryOp):
        return _root_names(expr.left) | _root_names(expr.right)
    if isinstance(expr, UnaryOp):
        return _root_names(expr.operand)
    return set()
