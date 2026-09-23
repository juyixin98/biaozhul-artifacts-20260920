"""Pydantic 请求/响应模型。所有整数数量以字符串传输以避免 JS/JSON 精度问题。"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from .clmm.constants import FEE_DENOMINATOR, MAX_TICK, MIN_TICK

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._-]{1,16}$")
_INT_STR_RE = re.compile(r"^[0-9]+$")


class PositionIn(BaseModel):
    lower_tick: int = Field(..., ge=MIN_TICK, le=MAX_TICK)
    upper_tick: int = Field(..., ge=MIN_TICK, le=MAX_TICK)
    liquidity: str = Field(..., description="正整数（字符串传输）")

    @field_validator("liquidity")
    @classmethod
    def _liq_positive_int(cls, v: str) -> str:
        if not _INT_STR_RE.fullmatch(v) or int(v) <= 0:
            raise ValueError("liquidity 必须为非负数字组成的正整数字符串")
        return v


class PoolCreate(BaseModel):
    pool_id: str = Field(..., examples=["demo-pool-0"])
    token0: str
    token1: str
    fee_ppm: int = Field(..., ge=0, le=FEE_DENOMINATOR - 1)
    sqrt_price_x96: str
    positions: list[PositionIn] = Field(..., min_length=1)

    @field_validator("pool_id")
    @classmethod
    def _pool_id_ok(cls, v: str) -> str:
        if not _ID_RE.fullmatch(v):
            raise ValueError("pool_id 仅允许字母数字 . _ : -，长度 1-64")
        return v

    @field_validator("token0", "token1")
    @classmethod
    def _symbol_ok(cls, v: str) -> str:
        if not _SYMBOL_RE.fullmatch(v):
            raise ValueError("代币符号仅允许字母数字 . _ -，长度 1-16")
        return v

    @field_validator("sqrt_price_x96")
    @classmethod
    def _sqrt_int_str(cls, v: str) -> str:
        if not _INT_STR_RE.fullmatch(v):
            raise ValueError("sqrt_price_x96 必须为正整数字符串")
        return v


class QuoteRequest(BaseModel):
    pool_id: str
    zero_for_one: bool
    amount_in: str = Field(..., description="输入数量，正整数字符串")
    limit_tick: int | None = Field(default=None, ge=MIN_TICK, le=MAX_TICK)

    @field_validator("amount_in")
    @classmethod
    def _amount_positive(cls, v: str) -> str:
        if not _INT_STR_RE.fullmatch(v) or int(v) <= 0:
            raise ValueError("amount_in 必须为正整数字符串")
        return v


class VerifyRequest(BaseModel):
    payload: dict
    signature: str
