"""Core mathematics for the two-asset stable pool.

Invariant (defined by this project; it is a StableSwap-*style* construction
and does NOT claim to replicate any deployed protocol byte-for-byte):

    Ann * (x + y) + D  ==  Ann * D + D**3 / (4 * x * y)

where
  * x, y   : pool balances normalised to 18-decimal fixed point (integers)
  * A      : amplification coefficient, integer, A_MIN <= A <= A_MAX
  * Ann    : A * n**n = 4 * A        (n = 2 assets)
  * D      : invariant constant ("total value" in normalised units)

Rearranged, D is the unique positive root of

    f(D) = D**3 / (4xy) + (Ann - 1) * D - Ann * (x + y) = 0

which is strictly increasing in D for A >= 1, x, y > 0, so the root is
unique and both Newton iteration and bisection are well posed.

All solving is done in exact Python integer arithmetic (arbitrary
precision). No floats appear anywhere in the solving path. Every division
is floor division; that is the only rounding and it is explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

N_COINS = 2
PRECISION_DECIMALS = 18
PRECISION = 10**PRECISION_DECIMALS  # normalisation target: 18 decimals
A_MIN = 1                     # amplification bounds (project-defined range)
A_MAX = 5000
DEFAULT_MAX_ITER = 255        # default iteration budget per solver
MAX_ITER_HARD = 1000          # absolute cap accepted from clients
CONVERGENCE_TOL = 1           # |next - prev| <= 1 (normalised integer unit)


class PoolStateError(Exception):
    """Raised when the pool state cannot support a quote at all."""


@dataclass(frozen=True)
class SolveResult:
    value: int                # solved value (D or y), meaningful only if converged
    iterations: int           # iterations actually consumed
    residual: int             # |F(value)| in integer invariant units
    converged: bool
    # "delta": |next - prev| <= CONVERGENCE_TOL.
    # "cycle": floor division produced a limit cycle near the root; the
    #          returned value is the cycle member with the smallest
    #          invariant residual. Both members bracket the true root.
    converged_via: str = "delta"


def _d_residual(d: int, s: int, ann: int, x: int, y: int) -> int:
    d_p = (d * d) // (x * N_COINS)
    d_p = (d_p * d) // (y * N_COINS)
    return abs(ann * s + d - ann * d - d_p)


def _check_amplification(amplification: int) -> None:
    if not (A_MIN <= amplification <= A_MAX):
        raise PoolStateError(
            f"amplification {amplification} outside supported range "
            f"[{A_MIN}, {A_MAX}]"
        )


def compute_d(x: int, y: int, amplification: int, max_iter: int = DEFAULT_MAX_ITER) -> SolveResult:
    """Solve the invariant constant D for balances (x, y) by bounded Newton
    iteration in exact integer arithmetic.

    Update rule (n = 2, Ann = 4A):

        D_P      = D**3 / (4 * x * y)          (floored, staged divisions)
        D_{k+1}  = (Ann*S + 2*D_P) * D_k / ((Ann - 1)*D_k + 3*D_P)

    Converged when |D_{k+1} - D_k| <= CONVERGENCE_TOL.
    """
    _check_amplification(amplification)
    if x < 0 or y < 0:
        raise PoolStateError("negative reserves are not allowed")
    if x == 0 and y == 0:
        raise PoolStateError("pool is empty: both reserves are zero")
    if x == 0 or y == 0:
        raise PoolStateError(
            "invariant has no finite solution when exactly one reserve is zero"
        )

    ann = amplification * N_COINS**N_COINS  # 4A
    s = x + y
    d = s  # initial guess
    d_p = 0
    iterations = 0
    converged = False
    converged_via = "delta"
    recent: list[int] = []  # recent iterates, for limit-cycle detection
    for k in range(1, max_iter + 1):
        iterations = k
        # D_P = D**3 // (4 * x * y), staged to keep the floor semantics exact
        d_p = (d * d) // (x * N_COINS)
        d_p = (d_p * d) // (y * N_COINS)
        numerator = (ann * s + N_COINS * d_p) * d
        denominator = (ann - 1) * d + (N_COINS + 1) * d_p
        d_next = numerator // denominator
        if abs(d_next - d) <= CONVERGENCE_TOL:
            d = d_next
            converged = True
            break
        if d_next in recent:
            # Floor division trapped the iteration in a limit cycle around
            # the root. Take the cycle member with the smallest residual.
            candidates = recent[recent.index(d_next):] + [d, d_next]
            d = min(candidates, key=lambda v: _d_residual(v, s, ann, x, y))
            converged = True
            converged_via = "cycle"
            break
        recent.append(d)
        if len(recent) > 8:
            recent.pop(0)
        d = d_next

    residual = _d_residual(d, s, ann, x, y)
    return SolveResult(value=d, iterations=iterations, residual=residual,
                       converged=converged, converged_via=converged_via)


def get_y(x: int, d: int, amplification: int, max_iter: int = DEFAULT_MAX_ITER) -> SolveResult:
    """Given the *other* balance x and invariant D, solve for y by bounded
    Newton iteration in exact integer arithmetic.

    The invariant as an equation in y reduces to

        h(y) = y**2 + (b - D) * y - c = 0
        b    = x + D // Ann
        c    = D**3 / (4 * Ann * x)    (floored, staged divisions)

    Newton update:

        y_{k+1} = (y_k**2 + c) / (2*y_k + b - D)

    Converged when |y_{k+1} - y_k| <= CONVERGENCE_TOL.
    """
    _check_amplification(amplification)
    if x <= 0:
        raise PoolStateError("get_y requires a strictly positive balance for x")
    if d <= 0:
        raise PoolStateError("get_y requires a strictly positive invariant D")

    ann = amplification * N_COINS**N_COINS
    # c = D**3 // (4 * Ann * x), staged floor divisions
    c = (d * d) // (x * N_COINS)
    c = (c * d) // (ann * N_COINS)
    b = x + d // ann

    y = d  # initial guess
    iterations = 0
    converged = False
    converged_via = "delta"
    recent: list[int] = []

    def h(v: int) -> int:
        return v * v + (b - d) * v - c

    for k in range(1, max_iter + 1):
        iterations = k
        denominator = 2 * y + b - d
        if denominator <= 0:
            # Iteration left the contraction region; report non-convergence
            # rather than dividing by a non-positive number.
            break
        y_next = (y * y + c) // denominator
        if abs(y_next - y) <= CONVERGENCE_TOL:
            y = y_next
            converged = True
            break
        if y_next in recent:
            # Limit cycle from floor division; take the best cycle member.
            candidates = recent[recent.index(y_next):] + [y, y_next]
            y = min(candidates, key=lambda v: abs(h(v)))
            converged = True
            converged_via = "cycle"
            break
        recent.append(y)
        if len(recent) > 8:
            recent.pop(0)
        y = y_next

    residual = abs(h(y))
    return SolveResult(value=y, iterations=iterations, residual=residual,
                       converged=converged, converged_via=converged_via)


def get_y_bisection(x: int, d: int, amplification: int, max_iter: int = DEFAULT_MAX_ITER) -> SolveResult:
    """Independent reference root-finder for the same equation as `get_y`,
    using plain bisection instead of Newton. Used only to cross-check the
    Newton result; it shares no iteration state with it.

    Solves h(y) = y**2 + (b - D)*y - c = 0 on a bracket that is expanded
    until it straddles the root, then halved until its width <= 1.
    """
    _check_amplification(amplification)
    if x <= 0:
        raise PoolStateError("get_y_bisection requires a strictly positive x")
    if d <= 0:
        raise PoolStateError("get_y_bisection requires a strictly positive D")

    ann = amplification * N_COINS**N_COINS
    c = (d * d) // (x * N_COINS)
    c = (c * d) // (ann * N_COINS)
    b = x + d // ann

    def h(v: int) -> int:
        return v * v + (b - d) * v - c

    if c == 0:
        # Degenerate: y = 0 is the non-negative root.
        return SolveResult(value=0, iterations=0, residual=abs(h(0)), converged=True)

    lo = 0                      # h(0) = -c < 0
    hi = d                      # starting upper guess, expanded below
    iterations = 0
    budget = max_iter
    while h(hi) <= 0:
        hi *= 2
        iterations += 1
        budget -= 1
        if budget <= 0:
            return SolveResult(value=hi, iterations=iterations,
                               residual=abs(h(hi)), converged=False)

    converged = False
    while budget > 0:
        iterations += 1
        budget -= 1
        if hi - lo <= 1:
            converged = True
            break
        mid = (lo + hi) // 2
        if h(mid) <= 0:
            lo = mid
        else:
            hi = mid
    else:
        converged = hi - lo <= 1

    # Root sits in (lo, hi]; take hi (first value with h > 0).
    return SolveResult(value=hi, iterations=iterations,
                       residual=abs(h(hi)), converged=converged)


def invariant_residual_exact(x: int, y: int, d: int, amplification: int) -> Fraction:
    """Exact residual of the invariant at (x, y, D):

        R = Ann*(x + y) + D - Ann*D - D**3 / (4xy)

    Returned as a Fraction so nothing is lost to rounding.
    """
    ann = amplification * N_COINS**N_COINS
    lhs = ann * (x + y) + d
    rhs = ann * d + Fraction(d**3, 4 * x * y)
    return abs(Fraction(lhs) - rhs)


def normalize(amount: int, decimals: int) -> int:
    """Scale a token amount from its native decimals to 18-decimal
    normalised fixed point. Exact (pure multiplication)."""
    return amount * 10 ** (PRECISION_DECIMALS - decimals)


def denormalize(amount_norm: int, decimals: int) -> int:
    """Scale a normalised amount back to native token decimals.

    Explicit final rounding: floor toward zero (integer division). Dust
    below one native unit is discarded and never silently credited.
    """
    return amount_norm // 10 ** (PRECISION_DECIMALS - decimals)
