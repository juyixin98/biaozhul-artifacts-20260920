"""Integer-search primitives, independent of any distribution.

The integer search is deliberately separated from the non-central distribution
machinery so each can be validated on its own:

* :func:`continuous_threshold` inverts a strictly increasing scalar function
  via bisection (used for continuous sample-size roots).
* :func:`smallest_integer_above` walks integers upward and *witnesses* the
  boundary: the returned ``n`` satisfies ``value(n) >= target`` while
  ``value(n - 1) < target`` is recorded as evidence of minimality.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional

ScalarFn = Callable[[float], float]
IntValueFn = Callable[[int], float]


@dataclass(frozen=True)
class IntegerBoundary:
    """Witness of the minimal-integer condition."""

    n: int
    target: float
    value_at_n: float
    value_at_n_minus_one: Optional[float]

    @property
    def n_passes(self) -> bool:
        return self.value_at_n >= self.target

    @property
    def n_minus_one_fails(self) -> bool:
        return self.value_at_n_minus_one is None or self.value_at_n_minus_one < self.target


def continuous_threshold(
    fn: ScalarFn,
    target: float,
    lower: float,
    upper: float,
    *,
    xtol: float = 1e-10,
    max_iter: int = 200,
) -> float:
    """First ``x`` in ``[lower, upper]`` with ``fn(x) >= target`` (continuous).

    ``fn`` must be non-decreasing. Raises ``ValueError`` if the target is not
    bracketed, and ``RuntimeError`` on non-convergence.
    """
    lo, hi = float(lower), float(upper)
    f_lo, f_hi = fn(lo), fn(hi)
    if f_lo >= target:
        return lo
    if f_hi < target:
        raise ValueError(f"target {target} not bracketed: f({hi})={f_hi:.6g}")
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        if fn(mid) >= target:
            hi = mid
        else:
            lo = mid
        if hi - lo <= xtol:
            return hi
    raise RuntimeError("continuous_threshold did not converge")


def smallest_integer_above(
    value_fn: IntValueFn,
    target: float,
    start: int,
    *,
    cap: int,
    floor: int = 1,
) -> IntegerBoundary:
    """Smallest ``n >= max(start, floor)`` with ``value_fn(n) >= target``.

    Evaluates each integer in turn and returns the *first* crossing, so the
    boundary witness proves both clauses of the acceptance condition:

    * ``value_at_n >= target``           — the returned size is adequate;
    * ``value_at_n_minus_one < target``  — one fewer subject is not.

    The walk-up (rather than relying on a monotone closed form) is essential
    for exact binomial power, whose acceptance region changes discretely and
    can exhibit small saw-tooth jumps.
    """
    n = max(int(start), int(floor))
    if n > cap:
        raise ValueError(f"start {n} exceeds cap {cap}")
    while n <= cap:
        v = value_fn(n)
        if not math.isfinite(v):
            raise ArithmeticError(f"non-finite power at n={n}: {v}")
        if v >= target:
            v_prev: Optional[float] = None
            if n - 1 >= floor:
                v_prev = value_fn(n - 1)
            return IntegerBoundary(n=n, target=target, value_at_n=v, value_at_n_minus_one=v_prev)
        n += 1
    raise OverflowError(f"no integer <= {cap} reached target {target}")


def ceil_disjoint(x: float) -> int:
    """Ceiling robust to floating representation; always >= 1."""
    return max(int(math.ceil(x - 1e-9)), 1)


def smallest_integer_with_backtrack(
    value_fn: IntValueFn,
    target: float,
    start: int,
    *,
    cap: int,
    floor: int = 1,
    backtrack_budget: int = 4096,
) -> IntegerBoundary:
    """Minimal integer with ``value_fn(n) >= target``, allowing a warm start
    above the answer.

    The exact binomial operating characteristic is a step function with
    plateaus, and the normal-approximation warm start can overshoot. Merely
    walking *up* from the warm start would then return a non-minimal n whose
    n-1 also passes. Procedure:

    1. ascend from ``start`` to a feasible point if needed;
    2. descend one integer at a time while the neighbour still passes
       (bounded by ``backtrack_budget`` evaluations);
    3. if the budget is exhausted, halve into a bracket below the plateau and
       walk up inside it;
    4. return the first crossing, witnessing value(n) and value(n-1).
    """
    n = max(int(start), floor)
    if n > cap:
        raise ValueError(f"start {n} exceeds cap {cap}")

    # 1) ascend to feasibility
    v = value_fn(n)
    while v < target and n < cap:
        n += 1
        v = value_fn(n)
    if v < target:
        raise OverflowError(f"no integer <= {cap} reached target {target}")

    # 2) stepwise descent across plateaus / warm-start overshoot
    steps = 0
    while n > floor and steps < backtrack_budget:
        v_prev = value_fn(n - 1)
        if v_prev < target:
            return IntegerBoundary(n=n, target=target, value_at_n=v,
                                   value_at_n_minus_one=v_prev)
        n -= 1
        v = v_prev
        steps += 1
    if n == floor:
        v_prev = value_fn(n - 1) if n - 1 >= floor else None
        return IntegerBoundary(n=n, target=target, value_at_n=v,
                               value_at_n_minus_one=v_prev)

    # 3) halve to bracket a failing point below n, then walk up
    hi = n
    lo = max(floor, hi // 2)
    while value_fn(lo) >= target and lo > floor:
        hi = lo
        lo = max(floor, lo // 2)
    n = lo
    v = value_fn(n)
    while v < target and n < cap:
        n += 1
        v = value_fn(n)
    if v < target:
        raise OverflowError(f"no integer <= {cap} reached target {target}")
    v_prev = value_fn(n - 1) if n - 1 >= floor else None
    return IntegerBoundary(n=n, target=target, value_at_n=v,
                           value_at_n_minus_one=v_prev)
