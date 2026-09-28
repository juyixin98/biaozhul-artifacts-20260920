"""Pydantic request/response contracts for the HTTP service.

The wire format deliberately accepts only JSON integers and *strings*
(``"3"``, ``"3/2"``, ``"0.1"``, ``"1.5e3"``).  JSON fractional numbers become
Python ``float`` and are rejected by the exact parser, so a client can never
trigger silent float conversion through the API.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Reasonable hard bounds so a malformed giant request fails fast as INPUT_ERROR.
MAX_DIM = 1024
MAX_DIGIT_BUDGET = 1_048_576


class SolveRequest(BaseModel):
    A: list[list[Any]] = Field(
        ..., description="m x n coefficient matrix; ints or exact strings"
    )
    b: list[Any] = Field(..., description="right-hand side, length m")
    digit_budget: int = Field(
        default=4096,
        ge=1,
        le=MAX_DIGIT_BUDGET,
        description="max decimal digits any intermediate integer may occupy",
    )
    decimal_dps: int = Field(
        default=40, ge=1, le=1000, description="display-only decimal precision"
    )
    include_float_diagnosis: bool = Field(
        default=False,
        description="also run the clearly-labelled lossy NumPy/SciPy comparison",
    )
    run_id: str | None = Field(
        default=None, description="optional client-supplied run id"
    )


class RankRequest(BaseModel):
    A: list[list[Any]]
    digit_budget: int = Field(default=4096, ge=1, le=MAX_DIGIT_BUDGET)
    decimal_dps: int = Field(default=40, ge=1, le=1000)
    include_float_diagnosis: bool = False
    run_id: str | None = None
