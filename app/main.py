"""FastAPI application: stable-pool offline quote service (backend only)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import schemas
from .core import config
from .core.sweep import curve_sweep
from .quotes import INVARIANT_EQUATION, AMP_RANGE, QuoteRequestError, compute_quote

app = FastAPI(
    title="Two-Asset Stable-Pool Offline Quote API",
    version="1.0.0",
    description=(
        "Offline quotes for a two-asset stable-swap-style AMM. All pricing uses "
        f"{config.DECIMAL_PREC}-digit Decimal arithmetic on precision-normalized "
        "amounts; outputs are floored to integer token units. A bounded "
        "safeguarded-Newton solver is cross-checked by an independent bisection "
        "solver. Non-convergent or guard-failing quotes are never tradable.\n\n"
        f"Invariant: `{INVARIANT_EQUATION}`. {AMP_RANGE}."
    ),
)


@app.exception_handler(QuoteRequestError)
async def quote_request_error_handler(_: Request, exc: QuoteRequestError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "tradable": False,
            "error": {"code": exc.code, "message": exc.message},
        },
    )


@app.exception_handler(ValidationError)
async def pydantic_error_handler(_: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/invariant")
async def invariant_spec() -> dict:
    return {
        "n_assets": config.N_ASSETS,
        "equation_normalized": INVARIANT_EQUATION,
        "amp_min": config.AMP_MIN,
        "amp_max": config.AMP_MAX,
        "amp_note": "A=1 reproduces constant product; larger A flattens toward constant-sum",
        "fee_bps_min": config.FEE_BPS_MIN,
        "fee_bps_max": config.FEE_BPS_MAX,
        "decimal_precision_digits": config.DECIMAL_PREC,
        "output_rounding": str(config.OUTPUT_ROUNDING),
        "newton_rel_tol": str(config.NEWTON_REL_TOL),
        "root_abs_tol": str(config.ROOT_ABS_TOL),
        "bisection_abs_tol": str(config.BISECT_ABS_TOL),
        "newton_max_iter_default": config.NEWTON_MAX_ITER_DEFAULT,
        "newton_max_iter_hard_cap": config.NEWTON_MAX_ITER_HARD_CAP,
        "bisection_max_iter": config.BISECT_MAX_ITER,
        "root_agreement_rel_tol": str(config.ROOT_AGREE_TOL),
        "root_agreement_abs_tol": str(config.ROOT_AGREE_ABS),
        "post_trade_d_drift_tol": str(config.D_DRIFT_TOL),
        "fee_convention": "charged on the gross output before floor rounding",
        "note": (
            "Self-defined stable-swap-family curve for demonstration/research. "
            "Constant product is the A->0 limit of this parameterization; the "
            "accepted integer A in [1,10^5] is the stable-coin regime. "
            "Not a byte-for-byte replica of any deployed protocol."
        ),
    }


@app.post("/quote", response_model=schemas.QuoteResponse)
async def quote(req: schemas.QuoteRequest) -> schemas.QuoteResponse:
    return compute_quote(req)


@app.post("/diagnostic/sweep")
async def diagnostic_sweep(
    reserve_in: float,
    reserve_out: float,
    amp: float = 20.0,
    n_points: int = 32,
    max_fraction: float = 0.5,
) -> dict:
    """NumPy float64 curve sketch. Never a tradable quote."""
    if reserve_in <= 0 or reserve_out <= 0:
        raise QuoteRequestError("ZERO_RESERVE", "reserves must be strictly positive")
    if not (config.AMP_MIN <= amp <= config.AMP_MAX):
        raise QuoteRequestError("AMP_OUT_OF_RANGE", f"amp must be within [{config.AMP_MIN},{config.AMP_MAX}]")
    if not (2 <= n_points <= 500) or not (0.0 < max_fraction <= 1_000.0):
        raise QuoteRequestError("BAD_SWEEP_PARAMS", "n_points in [2,500], max_fraction in (0,1000]")
    return curve_sweep(reserve_in, reserve_out, amp, n_points, max_fraction)
