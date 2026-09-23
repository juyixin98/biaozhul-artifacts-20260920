"""Numerical configuration: Decimal precision, rounding rules, parameter bounds.

All invariant arithmetic is performed with :class:`decimal.Decimal`.
Asset amounts are first normalized from integer token units to human-scaled
floats-of-decimals (``amount / 10**decimals``); the *numerical answer* is
computed there, then converted back to integer units with an explicit
rounding rule.  See ``precision.py`` for the conversion helpers.

Design choices (deliberately explicit so the behavior is auditable):

* Working precision: :data:`DECIMAL_PREC` digits for all intermediate math.
* Final integer rounding: output amounts are rounded toward zero
  (:data:`OUTPUT_ROUNDING`, i.e. floor for positive values).  Rounding the
  user's payout down can never enlarge the payout, so it can never weaken
  the pool beyond the real (real-valued) root.
* Charged fees round the input down is avoided; fees are taken in normalized
  space and the resulting payout is floored once, at the end.
"""

from __future__ import annotations

import decimal

# ---------------------------------------------------------------------------
# Precision / rounding
# ---------------------------------------------------------------------------

#: Digits of Decimal precision used for *all* invariant math.
DECIMAL_PREC: int = 80

#: Rounding for intermediate Decimal algebra.
INTERNAL_ROUNDING = decimal.ROUND_HALF_EVEN

#: Rounding applied once, when converting the normalized output back to an
#: integer token amount.  Toward-zero (= floor for positive values) ensures
#: the quoted payout never exceeds the exact real-valued payout.
OUTPUT_ROUNDING = decimal.ROUND_DOWN

# ---------------------------------------------------------------------------
# Invariant parameter bounds (validated on every request)
# ---------------------------------------------------------------------------

#: Number of assets in the pool.  The whole module is specialized to n == 2.
N_ASSETS: int = 2

#: Allowed amplification parameter A.  A = 1 collapses to the constant-product
#: curve; large A flattens the curve toward constant-sum near balance.
AMP_MIN: int = 1
AMP_MAX: int = 100_000

#: Fee in basis points, charged on the *input* (deducted before the swap).
FEE_BPS_MIN: int = 0
FEE_BPS_MAX: int = 10_000  # 100 % would be a nonsense quote but is bounded
FEE_BPS_DEFAULT: int = 4  # 0.04 %

#: Token decimals must be non-negative and cannot exceed this, so that
#: normalized values cannot be made arbitrarily tiny in a request.
DECIMALS_MIN: int = 0
DECIMALS_MAX: int = 36

# ---------------------------------------------------------------------------
# Solver settings
# ---------------------------------------------------------------------------

#: Default / bounded Newton iteration cap.  Requests may lower it (to probe
#: the iteration-limit behavior) but never raise it beyond the hard cap.
NEWTON_MAX_ITER_DEFAULT: int = 64
NEWTON_MAX_ITER_HARD_CAP: int = 512

#: Bisection reference solver iteration cap (fixed; not caller controlled).
BISECT_MAX_ITER: int = 300

#: Newton stops when the relative update |step| / |value| AND the residual
#: (on an absolute, normalized-unit scale) both drop below their thresholds.
#: At 80 working digits, ~50 decimal places leaves a wide safety margin.
NEWTON_REL_TOL: decimal.Decimal = decimal.Decimal("1E-50")

#: Absolute accuracy required of a root in normalized units.  Guarantees
#: tiny trades (micro-outputs) are resolved instead of being absorbed into a
#: large pre-trade reserve.  1e-60 of one normalized token is economically
#: zero; outputs below this are rejected as ZERO_PAYOUT downstream.
ROOT_ABS_TOL: decimal.Decimal = decimal.Decimal("1E-60")

#: Bisection stops when the bracket is narrower than this absolute width.
BISECT_ABS_TOL: decimal.Decimal = decimal.Decimal("1E-65")

#: Post-trade invariant guard: a quote is rejected unless the *new* pool
#: D (computed independently from post-trade reserves) is at least
#: ``D_before * (1 - D_DRIFT_TOL)``.  A healthy swap only grows D; the
#: tolerance absorbs integer-floor dust.
D_DRIFT_TOL: decimal.Decimal = decimal.Decimal("1E-12")

#: Two roots (Newton vs. independent bisection) must agree both relatively
#: and in absolute normalized units for the quote to be declared consistent.
#: The absolute term catches a microscopic output that two large-scale
#: relative errors could hide.
ROOT_AGREE_TOL: decimal.Decimal = decimal.Decimal("1E-40")
ROOT_AGREE_ABS: decimal.Decimal = decimal.Decimal("1E-50")

#: Integer payout below/at this is treated as "economically zero" and the
#: quote is rejected rather than returning a non-tradable zero payout.
MIN_PAYOUT_UNITS: int = 0
