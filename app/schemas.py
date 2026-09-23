"""Pydantic request/response schemas for the HTTP API.

Large integers (token amounts, liquidity, sqrt prices) are serialized as
JSON *strings* so JavaScript-based clients do not lose precision; both
string and integer forms are accepted on input.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .config import FEE_DENOMINATOR, FEE_NUMERATOR, TICK_MAX, TICK_MIN


class BigInt:
    """Annotation helper: accept int or numeric string, emit string."""


def _coerce_bigint(v: object, field_name: str) -> str:
    if isinstance(v, bool):
        raise ValueError(f"{field_name} must be an integer or numeric string")
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            raise ValueError(f"{field_name} is empty")
        try:
            return str(int(s, 10))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a base-10 integer string") from exc
    raise ValueError(f"{field_name} must be an integer or numeric string")


class PositionIn(BaseModel):
    lower_tick: int = Field(..., ge=TICK_MIN, le=TICK_MAX)
    upper_tick: int = Field(..., ge=TICK_MIN, le=TICK_MAX)
    liquidity: int = Field(..., ge=0)


class CreatePool(BaseModel):
    pool_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    token0: str = Field(..., min_length=1, max_length=32)
    token1: str = Field(..., min_length=1, max_length=32)
    current_tick: int = Field(..., ge=TICK_MIN, le=TICK_MAX)
    fee_numerator: int = Field(default=FEE_NUMERATOR, gt=0)
    fee_denominator: int = Field(default=FEE_DENOMINATOR, gt=1)
    positions: list[PositionIn] = Field(default_factory=list)

    @field_validator("positions")
    @classmethod
    def _validate_positions(cls, v: list[PositionIn]) -> list[PositionIn]:
        for p in v:
            if p.lower_tick >= p.upper_tick:
                raise ValueError("each position requires lower_tick < upper_tick")
        return v


class QuoteRequest(BaseModel):
    zero_for_one: bool
    amount_in: int | str = Field(
        ..., description="gross input amount in smallest unit (int or numeric string)"
    )

    @field_validator("amount_in")
    @classmethod
    def _amount(cls, v: object) -> str:
        s = _coerce_bigint(v, "amount_in")
        if int(s) <= 0:
            raise ValueError("amount_in must be positive")
        return s
