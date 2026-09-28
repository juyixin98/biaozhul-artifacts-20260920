"""HTTP request schemas (pydantic v2).

These models only shape the request. *Exactness* validation (rejecting binary
floats, malformed rationals, over-budget degree, etc.) lives in
:mod:`root_isolator.input.parser` so that HTTP requests and direct library use
share one validation home and one set of stable :class:`ErrorCategory` codes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class IsolationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    coefficients: list[Any] | None = Field(
        default=None,
        description=(
            "Dense coefficient list. Each coefficient is an integer or an exact "
            "decimal/fraction string (e.g. '1/10', '0.25'); binary floats are "
            "rejected. Default order is descending [a_n, ..., a_0]."
        ),
    )
    sparse: dict[str, Any] | None = Field(
        default=None,
        description="Sparse mapping of power (string key) to exact coefficient.",
    )
    order: str = Field(
        default="descending",
        description="Ordering of the dense list: 'descending' or 'ascending'.",
    )


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
