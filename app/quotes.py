"""Quote engine: validation, fee, swap math, guards and response assembly.

Trade rule
----------
Given pre-trade normalized reserves ``(x, y)`` and input ``a``:

1. solve ``D`` from ``(x, y)`` and hold it invariant
2. full input enters the curve: solve ``y'`` for ``x' = x + a`` at fixed D
3. gross output               ``gross = y - y'``
4. fee (output side)          ``fee = gross * fee_bps / 10_000``
5. net output                 ``net = gross - fee``
6. integer payout             ``floor(net * 10**decimals_out)``

The quote is marked ``tradable`` only if every guard passes:
``D`` and ``y'`` Newton solves converged, the Newton roots agree with the
independent bisection roots (relative AND absolute), ``y'`` leaves positive
output, the floored net payout is nonzero, and the post-trade D recomputed
from the actual integer post-trade reserves does not decrease beyond dust.
"""

from __future__ import annotations

import decimal
import hashlib
import json

from . import schemas
from .core import config, invariant
from .core.precision import d, high_precision, relative_error, to_normalized, to_units_floor

INVARIANT_EQUATION = "D^3/(4*x*y) + 2*A*D = 2*A*(x+y) + D  (n=2, normalized units)"
AMP_RANGE = (
    f"A integer in [{config.AMP_MIN}, {config.AMP_MAX}] (stable-coin regime; "
    "constant-product is the A->0 limit; larger A -> flatter near balance)"
)


class QuoteRequestError(Exception):
    """Malformed/domain-invalid request -> HTTP 422."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _solver_report(r: invariant.RootResult) -> schemas.SolverReport:
    return schemas.SolverReport(**r.as_report())


def _spot_price_out_per_in(
    x: decimal.Decimal, y: decimal.Decimal, D: decimal.Decimal, A: int
) -> decimal.Decimal:
    """Marginal price -dy/dx from the invariant, output units per input unit.

    From F = D^3/(4xy) + 2AD - 2A(x+y) - D = 0:
        dy/dx = -[D^3/(4 x^2 y) + 2A] / [D^3/(4 x y^2) + 2A]
    """
    a = d(A)
    num = D ** 3 / (d(4) * x ** 2 * y) + d(2) * a
    den = D ** 3 / (d(4) * x * y ** 2) + d(2) * a
    return num / den


def _quote_id(req: schemas.QuoteRequest) -> str:
    # Real cryptographic operation: SHA-256 over the canonical request.
    # It identifies/deduplicates identical quote requests; it is not a
    # signature and no secret is involved.
    body = json.dumps(
        req.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _nontradable(
    req: schemas.QuoteRequest,
    code: str,
    message: str,
    *,
    d_newton: invariant.RootResult,
    d_bisect: invariant.RootResult | None,
    y_newton: invariant.RootResult | None = None,
    y_bisect: invariant.RootResult | None = None,
    roots_agree: bool = False,
    root_agreement_rel: decimal.Decimal | None = None,
    post_trade: schemas.PostTradeReport | None = None,
) -> schemas.QuoteResponse:
    return schemas.QuoteResponse(
        quote_id=_quote_id(req),
        tradable=False,
        error=schemas.ErrorBlock(code=code, message=message),
        invariant=INVARIANT_EQUATION,
        amp_range=AMP_RANGE,
        precision_digits=config.DECIMAL_PREC,
        output_rounding=str(config.OUTPUT_ROUNDING),
        pool={
            "reserve_in_units": str(req.reserve_in_units),
            "reserve_out_units": str(req.reserve_out_units),
            "decimals_in": req.decimals_in,
            "decimals_out": req.decimals_out,
            "amp": req.amp,
            "fee_bps": req.fee_bps,
        },
        quote=None,
        solver=schemas.SolverSection(
            d_newton=_solver_report(d_newton),
            d_bisection=_solver_report(d_bisect) if d_bisect is not None else None,
            y_newton=_solver_report(y_newton) if y_newton is not None else None,
            y_bisection=_solver_report(y_bisect) if y_bisect is not None else None,
            roots_agree=roots_agree,
            root_agreement_rel=(
                format(root_agreement_rel, "E") if root_agreement_rel is not None else "N/A"
            ),
            post_trade=post_trade,
        ),
    )


def compute_quote(req: schemas.QuoteRequest) -> schemas.QuoteResponse:
    """Run the full quote pipeline inside one high-precision Decimal context."""
    with high_precision():
        return _compute(req)


def _compute(req: schemas.QuoteRequest) -> schemas.QuoteResponse:
    x = to_normalized(req.reserve_in_units, req.decimals_in)
    y = to_normalized(req.reserve_out_units, req.decimals_out)
    a_in = to_normalized(req.amount_in_units, req.decimals_in)

    # Domain checks (request-level, surfaced as 422).
    if x <= 0 or y <= 0:
        raise QuoteRequestError(
            "ZERO_RESERVE",
            "both pool reserves must be strictly positive; a stable-swap quote "
            "is undefined on an empty or one-sided pool",
        )
    if a_in <= 0:
        raise QuoteRequestError(
            "ZERO_INPUT", "amount_in_units must be strictly positive to obtain a quote"
        )

    max_iter = min(req.max_iterations, config.NEWTON_MAX_ITER_HARD_CAP)

    # --- fee convention: charged on the OUTPUT -----------------------------
    # The entire input enters pool reserves (x grows by a_in); the invariant
    # is solved for the gross output, then the fee is deducted from the gross
    # output and the *net* output is floored to integer units.  Charging on
    # the output makes the realized integer reserves identical in shape to
    # the curve state, so the post-trade D guard is a clean "D must not
    # decrease" check (fee + floor dust can only increase it).
    fee_bps = d(req.fee_bps)
    x_new = x + a_in

    # --- D solve (production: safeguarded Newton) --------------------------
    d_n = invariant.solve_d(x, y, req.amp, max_iter=max_iter)

    # Independent reference (always computed when requested; it is also the
    # cross-check the API promises even on the happy path).
    d_b = invariant.solve_d_bisect(x, y, req.amp) if req.include_reference else None

    if not d_n.converged:
        return _nontradable(
            req,
            "D_NOT_CONVERGED",
            f"D solve did not converge within {max_iter} iterations; no tradable quote",
            d_newton=d_n,
            d_bisect=d_b,
        )
    if d_b is not None and not d_b.converged:  # pragma: no cover - bisection cap is huge
        return _nontradable(
            req,
            "REFERENCE_FAILED",
            "independent D bisection reference failed; refusing quote on disagreement",
            d_newton=d_n,
            d_bisect=d_b,
        )

    D = d_n.value

    # --- y' solve at fixed D ----------------------------------------------
    y_n = invariant.solve_y(x_new, D, req.amp, y_prev=y, max_iter=max_iter)

    if not y_n.converged:
        return _nontradable(
            req,
            "Y_NOT_CONVERGED",
            f"y' solve did not converge within {max_iter} iterations; no tradable quote",
            d_newton=d_n,
            d_bisect=d_b,
            y_newton=y_n,
        )

    y_b = (
        invariant.solve_y_bisect(x_new, D, req.amp)
        if req.include_reference
        else None
    )
    if y_b is not None and not y_b.converged:  # pragma: no cover
        return _nontradable(
            req,
            "REFERENCE_FAILED",
            "independent y' bisection reference failed; refusing quote",
            d_newton=d_n,
            d_bisect=d_b,
            y_newton=y_n,
            y_bisect=y_b,
        )

    y_agree_rel = relative_error(y_n.value, y_b.value) if y_b is not None else d(0)
    y_agree_abs = abs(y_n.value - y_b.value) if y_b is not None else d(0)
    # Also cross-check D roots (relative).
    d_agree_rel = relative_error(d_n.value, d_b.value) if d_b is not None else d(0)
    roots_agree = (
        y_b is None
        or (
            y_agree_rel < config.ROOT_AGREE_TOL
            and y_agree_abs < config.ROOT_AGREE_ABS
            and (d_b is None or d_agree_rel < config.ROOT_AGREE_TOL)
        )
    )
    worst_agree = max(y_agree_rel, d_agree_rel)

    if not roots_agree:
        return _nontradable(
            req,
            "ROOT_MISMATCH",
            "Newton root disagrees with the independent bisection reference beyond tolerance",
            d_newton=d_n,
            d_bisect=d_b,
            y_newton=y_n,
            y_bisect=y_b,
            roots_agree=False,
            root_agreement_rel=worst_agree,
        )

    y_new = y_n.value

    # --- economic validity -------------------------------------------------
    if y_new >= y:
        return _nontradable(
            req,
            "INSUFFICIENT_LIQUIDITY",
            "requested input would not drain any output reserve at fixed D; "
            "trade size is infeasible for this pool",
            d_newton=d_n,
            d_bisect=d_b,
            y_newton=y_n,
            y_bisect=y_b,
            roots_agree=True,
            root_agreement_rel=worst_agree,
        )

    gross_out_norm = y - y_new
    fee_out_norm = gross_out_norm * fee_bps / d(10_000)
    net_out_norm = gross_out_norm - fee_out_norm
    amount_out_units = to_units_floor(net_out_norm, req.decimals_out)
    if amount_out_units <= config.MIN_PAYOUT_UNITS:
        return _nontradable(
            req,
            "ZERO_PAYOUT",
            "output rounds to zero integer token units; trade too small to quote",
            d_newton=d_n,
            d_bisect=d_b,
            y_newton=y_n,
            y_bisect=y_b,
            roots_agree=True,
            root_agreement_rel=worst_agree,
        )

    # --- post-trade invariant guard using INTEGER post reserves ------------
    # Full input in; floored NET output out.  Fee stays in the pool, so this
    # settlement is strictly favorable to the pool and its D must not be
    # below the pre-trade D (minus a dust allowance for Decimal/root noise).
    x_after = x + a_in  # == to_normalized(reserve_in + amount_in, decimals_in)
    y_after = to_normalized(
        req.reserve_out_units - amount_out_units, req.decimals_out
    )
    if y_after < 0:  # pragma: no cover - guarded by y_new >= y above
        return _nontradable(
            req,
            "INSUFFICIENT_LIQUIDITY",
            "payout exceeds output reserve",
            d_newton=d_n,
            d_bisect=d_b,
            y_newton=y_n,
            y_bisect=y_b,
            roots_agree=True,
            root_agreement_rel=worst_agree,
        )

    # Post-trade D is a verification value, not a price; use the independent
    # bisection solver so the check cannot fail merely because safeguarded
    # Newton walks slowly in the flat, high-A region.
    d_after_res = invariant.solve_d_bisect(x_after, y_after, req.amp)
    # Reference state: the exact fee-inclusive, pre-floor post-trade state
    # ``(x + a_in, y - net_out)``.  Integer settlement floors the payout, so
    # the realized output reserve is within one output unit (dust) ABOVE the
    # exact state (we never pay out more); the input reserve is exact.
    # Hence the integer post-trade D must be within dust of the reference D,
    # and never materially below it.  The reference D legitimately exceeds the
    # pre-trade D by the retained fee surplus; that surplus is reported
    # separately in ``fee_surplus_rel`` and is NOT treated as a violation.
    y_exact = y - net_out_norm
    # The fee-inclusive reference state is NOT on the constant-D curve, and a
    # Newton walk from x+a_in can converge slowly there.  Because this value
    # is a *check* rather than a price, we compute it with the independent
    # bisection solver (a separate, guaranteed-bracketing path).
    d_ref_res = invariant.solve_d_bisect(x + a_in, y_exact, req.amp)
    # Integer settlement floors the payout, so y_after differs from y_exact
    # by less than one output unit; in flat (high-A) regions D is highly
    # sensitive to y, so the corresponding D deviation is amplified.  We size
    # the band to a few output units relative to the output reserve, which
    # simultaneously covers the floor dust and the reference root noise.
    # It remains tiny relative to any economically meaningful move.
    few_units = d(10) / (d(10) ** req.decimals_out)
    # y > 0 is guaranteed (zero reserves are rejected up front); do NOT floor
    # the denominator at 1, or sub-unit reserves get a spuriously tight band.
    floor_dust_rel = few_units / y
    tol = max(config.D_DRIFT_TOL, floor_dust_rel)
    if d_ref_res.converged:
        # integer-execution rounding deviation vs exact fee-inclusive state
        drift = (d_after_res.value - d_ref_res.value) / d_ref_res.value
        invariant_ok = d_after_res.converged and -tol <= drift <= tol
        fee_surplus_rel = (d_ref_res.value - D) / D
    else:
        invariant_ok = False
        drift = (d_after_res.value - D) / D
        fee_surplus_rel = drift

    post_trade = schemas.PostTradeReport(
        d_before_normalized=str(D),
        d_reference_normalized=str(d_ref_res.value if d_ref_res.converged else D),
        d_after_normalized=str(d_after_res.value),
        d_drift_rel=format(drift, "E"),
        fee_surplus_rel=format(fee_surplus_rel, "E"),
        invariant_ok=invariant_ok,
    )

    if not invariant_ok:
        return _nontradable(
            req,
            "INVARIANT_VIOLATION",
            "post-trade D drifts downward beyond tolerance; refusing to quote",
            d_newton=d_n,
            d_bisect=d_b,
            y_newton=y_n,
            y_bisect=y_b,
            roots_agree=True,
            root_agreement_rel=worst_agree,
            post_trade=post_trade,
        )

    # --- pricing metadata --------------------------------------------------
    spot_before = _spot_price_out_per_in(x, y, D, req.amp)
    # Effective realized price = NET output / input.
    effective_price = net_out_norm / a_in
    price_impact_bps = (spot_before - effective_price) / spot_before * d(10_000)

    quote = schemas.QuotePayload(
        gross_out_normalized=str(gross_out_norm),
        amount_out_units=str(amount_out_units),
        amount_out_normalized=str(net_out_norm),
        fee_out_units=str(to_units_floor(fee_out_norm, req.decimals_out)),
        fee_out_normalized=str(fee_out_norm),
        spot_price_before=str(spot_before),
        effective_price=str(effective_price),
        price_impact_bps=str(price_impact_bps),
    )

    return schemas.QuoteResponse(
        quote_id=_quote_id(req),
        tradable=True,
        error=None,
        invariant=INVARIANT_EQUATION,
        amp_range=AMP_RANGE,
        precision_digits=config.DECIMAL_PREC,
        output_rounding=str(config.OUTPUT_ROUNDING),
        pool={
            "reserve_in_units": str(req.reserve_in_units),
            "reserve_out_units": str(req.reserve_out_units),
            "decimals_in": req.decimals_in,
            "decimals_out": req.decimals_out,
            "amp": req.amp,
            "fee_bps": req.fee_bps,
        },
        quote=quote,
        solver=schemas.SolverSection(
            d_newton=_solver_report(d_n),
            d_bisection=_solver_report(d_b),
            y_newton=_solver_report(y_n),
            y_bisection=_solver_report(y_b),
            roots_agree=True,
            root_agreement_rel=format(worst_agree, "E"),
            post_trade=post_trade,
        ),
    )
