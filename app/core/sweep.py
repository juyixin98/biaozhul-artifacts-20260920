"""NumPy float64 *diagnostic* curve sweep.

IMPORTANT: this module is deliberately separate from the pricing path.
:mod:`app.quotes` prices every trade with 80-digit Decimal; the functions
here use plain ``numpy.float64`` vectorized arithmetic solely to sketch the
invariant curve (reserve / spot-price points) for API consumers who want a
quick picture.  Results are tagged ``float64-diagnostic`` and MUST NOT be
used as tradable quotes.
"""

from __future__ import annotations

import numpy as np


def _solve_d_float64(x: float, y: float, amp: float, max_iter: int = 200) -> np.ndarray:
    """Vectorized safeguarded Newton in float64 for f(D), x,y are arrays."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    lo = np.zeros_like(x)
    hi = np.maximum(1.0, x + y)

    def f(D):
        return D**3 / (4.0 * x * y) + (2.0 * amp - 1.0) * D - 2.0 * amp * (x + y)

    # grow hi until positive
    for _ in range(200):
        fhi = f(hi)
        growing = fhi <= 0.0
        if not np.any(growing):
            break
        hi = np.where(growing, hi * 2.0, hi)

    cur = np.clip(x + y, lo, hi)
    for _ in range(max_iter):
        fx = f(cur)
        fp = 3.0 * cur**2 / (4.0 * x * y) + (2.0 * amp - 1.0)
        nxt = cur - fx / fp
        mid = (lo + hi) / 2.0
        nxt = np.where((nxt > lo) & (nxt < hi), nxt, mid)
        fn = f(nxt)
        hi = np.where(fn > 0.0, nxt, hi)
        lo = np.where(fn <= 0.0, nxt, lo)
        cur = nxt
    return cur


def _solve_y_float64(x_new: np.ndarray, D: np.ndarray, amp: float, max_iter: int = 200) -> np.ndarray:
    """Vectorized bisection for g(y), root in (0, D)."""
    lo = np.zeros_like(x_new)
    hi = D.copy()

    def g(yy):
        return (
            8.0 * amp * x_new * yy**2
            + (8.0 * amp * x_new**2 + 4.0 * x_new * D * (1.0 - 2.0 * amp)) * yy
            - D**3
        )

    for _ in range(200):
        ghi = g(hi)
        growing = ghi < 0.0
        if not np.any(growing):
            break
        hi = np.where(growing, hi * 2.0, hi)

    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        gm = g(mid)
        hi = np.where(gm > 0.0, mid, hi)
        lo = np.where(gm <= 0.0, mid, lo)
    return (lo + hi) / 2.0


def curve_sweep(
    reserve_in: float,
    reserve_out: float,
    amp: float,
    n_points: int,
    max_fraction: float,
) -> dict:
    """Return diagnostic grid points for buys of 0..max_fraction*reserve_in."""
    fractions = np.linspace(0.0, max_fraction, n_points, dtype=np.float64)
    amounts = fractions * reserve_in

    D0 = float(_solve_d_float64(np.array([reserve_in]), np.array([reserve_out]), amp)[0])
    x_grid = reserve_in + amounts
    D_grid = np.full_like(x_grid, D0)
    y_grid = _solve_y_float64(x_grid, D_grid, amp)
    outputs = reserve_out - y_grid
    outputs = np.maximum(outputs, 0.0)

    with np.errstate(divide="ignore", invalid="ignore"):
        avg_price = np.divide(
            outputs, amounts, out=np.zeros_like(outputs), where=amounts > 0
        )

    return {
        "precision": "float64-diagnostic",
        "warning": "Approximate NumPy float64 sketch for visualization only; not a tradable quote.",
        "d_balance": D0,
        "points": [
            {
                "fraction_of_input_reserve": float(fr),
                "amount_in_normalized": float(am),
                "output_reserve_after_normalized": float(yy),
                "gross_output_normalized": float(oo),
                "average_output_per_input": float(pp),
            }
            for fr, am, yy, oo, pp in zip(
                fractions, amounts, y_grid, outputs, avg_price, strict=True
            )
        ],
    }
