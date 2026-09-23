"""FastAPI application: two-asset stable-pool offline quoting."""

from __future__ import annotations

from fastapi import FastAPI

from .pool import (
    A_MAX,
    A_MIN,
    CONVERGENCE_TOL,
    DEFAULT_MAX_ITER,
    MAX_ITER_HARD,
    PRECISION_DECIMALS,
)
from .schemas import QuoteRequest, QuoteResponse
from .service import REFERENCE_TOLERANCE, execute_quote

app = FastAPI(
    title="Stable Pool Quoter",
    version="1.0.0",
    description=(
        "Offline two-asset stable-pool quoting. Bounded-iteration Newton solver "
        "in exact integer arithmetic, cross-checked by an independent bisection "
        "root-finder. Non-converged results never produce an executable quote."
    ),
)


@app.get("/v1/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/v1/metadata")
def metadata() -> dict:
    return {
        "invariant": "Ann*(x + y) + D == Ann*D + D**3/(4*x*y), with Ann = 4*A",
        "invariant_note": (
            "StableSwap-style construction defined by this project; "
            "not a byte-for-byte replica of any deployed protocol."
        ),
        "n_coins": 2,
        "amplification_range": [A_MIN, A_MAX],
        "normalization_decimals": PRECISION_DECIMALS,
        "arithmetic": "exact integer (arbitrary precision) in the solving path; "
                      "NumPy longdouble used for diagnostics only",
        "solvers": {
            "primary": "newton (bounded iteration)",
            "reference": "bisection (independent root-finder)",
            "convergence_tolerance_units": CONVERGENCE_TOL,
            "reference_agreement_tolerance_units": REFERENCE_TOLERANCE,
            "default_max_iter": DEFAULT_MAX_ITER,
            "max_iter_hard_cap": MAX_ITER_HARD,
        },
        "rounding": "floor toward zero on all divisions and on final denormalisation",
        "quote_policy": "executable only when every solver converges and the "
                        "bisection reference agrees with Newton",
    }


@app.post("/v1/quote", response_model=QuoteResponse)
def quote(req: QuoteRequest) -> QuoteResponse:
    return execute_quote(req)
