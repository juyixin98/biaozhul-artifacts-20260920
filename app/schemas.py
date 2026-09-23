"""Request/response schemas for the quoting API.

All token amounts are transmitted as decimal strings so no precision is
lost to JSON floats at any point.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .pool import A_MAX, A_MIN, DEFAULT_MAX_ITER, MAX_ITER_HARD, PRECISION_DECIMALS

_INT_RE = re.compile(r"^(0|[1-9][0-9]*)$")


def _uint_string(v: object, name: str) -> str:
    if not isinstance(v, str) or not _INT_RE.match(v):
        raise ValueError(f"{name} must be a non-negative integer given as a decimal string")
    return v


class QuoteRequest(BaseModel):
    reserves: list[str] = Field(..., min_length=2, max_length=2,
                                description="Pool balances in native token units, as decimal strings")
    decimals: list[int] = Field(..., min_length=2, max_length=2,
                                description="Native decimals of each token, 0..18")
    amplification: int = Field(..., ge=A_MIN, le=A_MAX,
                               description=f"Amplification coefficient A, supported range [{A_MIN}, {A_MAX}]")
    token_in: int = Field(..., ge=0, le=1, description="Index of the input token (0 or 1)")
    amount_in: str = Field(..., description="Input amount in native units of token_in, decimal string")
    max_iter: int = Field(DEFAULT_MAX_ITER, ge=1, le=MAX_ITER_HARD,
                          description="Iteration budget per solver (bounded)")

    @field_validator("reserves")
    @classmethod
    def _check_reserves(cls, v: list[str]) -> list[str]:
        return [_uint_string(item, "reserves[i]") for item in v]

    @field_validator("amount_in")
    @classmethod
    def _check_amount_in(cls, v: str) -> str:
        _uint_string(v, "amount_in")
        if int(v) <= 0:
            raise ValueError("amount_in must be strictly positive")
        return v

    @field_validator("decimals")
    @classmethod
    def _check_decimals(cls, v: list[int]) -> list[int]:
        for d in v:
            if not (0 <= d <= PRECISION_DECIMALS):
                raise ValueError(f"decimals entries must be in [0, {PRECISION_DECIMALS}]")
        return v


class SolverReport(BaseModel):
    method: str
    converged: bool
    iterations: int
    residual: str  # integer residual in invariant units, as string
    converged_via: str = "delta"  # "delta" | "cycle" (limit-cycle break, best-residual member)


class Quote(BaseModel):
    token_out: int
    amount_out: str             # native units of token_out (after explicit floor rounding)
    amount_out_normalized: str  # 18-decimal normalised units, pre-denormalisation


class QuoteResponse(BaseModel):
    status: Literal["ok", "not_converged", "solver_disagreement", "invalid_pool"]
    executable: bool            # True only when a converged, cross-checked quote exists
    reason: Optional[str] = None
    quote: Optional[Quote] = None
    d_before: Optional[str] = None
    d_after: Optional[str] = None
    invariant_residual_exact: Optional[str] = None   # exact fraction "p/q" in invariant units
    invariant_residual_float: Optional[float] = None  # NumPy longdouble diagnostic
    newton: Optional[SolverReport] = None
    bisection_reference: Optional[SolverReport] = None
    reference_amount_out_normalized: Optional[str] = None
    rounding: str = "floor toward zero (integer division); dust below 1 native unit discarded"
