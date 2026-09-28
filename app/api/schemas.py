"""Pydantic request schemas.

Responses are returned as plain JSON-safe dicts (NaN/Inf rendered as the
strings ``"NaN"``/``"Infinity"``); request bodies are validated here.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..services import synthetic
from ..services.comparison import ORDERINGS

_ALLOWED_ORDERINGS = frozenset(ORDERINGS)
_SPECIAL_TOKENS = frozenset({"nan", "infinity", "+infinity", "-infinity"})


class CompareRequest(BaseModel):
    """Body of ``POST /api/v1/compare``."""

    values: list[Any] = Field(
        ..., min_length=1, description="JSON numbers and/or the tokens NaN, Infinity, -Infinity"
    )
    block_size: int | None = Field(default=None, ge=1)
    orderings: list[str] | None = None
    shuffle_seed: int = 0

    @field_validator("values")
    @classmethod
    def _validate_values(cls, v: list[Any]) -> list[Any]:
        for i, item in enumerate(v):
            if isinstance(item, bool):
                raise ValueError(f"values[{i}]: boolean is not a valid summand")
            if isinstance(item, str):
                if item.strip().lower() not in _SPECIAL_TOKENS:
                    raise ValueError(
                        f"values[{i}]: string {item!r} is not NaN/Infinity/-Infinity"
                    )
            elif not isinstance(item, (int, float)):
                raise ValueError(f"values[{i}]: value of type {type(item).__name__} is not numeric")
        return v

    @field_validator("orderings")
    @classmethod
    def _validate_orderings(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if not v:
            raise ValueError("orderings must be a non-empty list when provided")
        bad = [name for name in v if name not in _ALLOWED_ORDERINGS]
        if bad:
            raise ValueError(
                f"unknown ordering(s) {bad}; allowed: {sorted(_ALLOWED_ORDERINGS)}"
            )
        return v


class ScenarioRequest(BaseModel):
    """Body of ``POST /api/v1/scenario``."""

    scenario: str = Field(..., description=f"one of {sorted(synthetic.SCENARIOS)}")
    n: int = Field(..., ge=1)
    block_size: int | None = Field(default=None, ge=1)
    orderings: list[str] | None = None
    shuffle_seed: int = 0

    @field_validator("scenario")
    @classmethod
    def _validate_scenario(cls, v: str) -> str:
        if v not in synthetic.SCENARIOS:
            raise ValueError(
                f"unknown scenario {v!r}; allowed: {sorted(synthetic.SCENARIOS)}"
            )
        return v

    @field_validator("orderings")
    @classmethod
    def _validate_orderings(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if not v:
            raise ValueError("orderings must be a non-empty list when provided")
        bad = [name for name in v if name not in _ALLOWED_ORDERINGS]
        if bad:
            raise ValueError(f"unknown ordering(s) {bad}; allowed: {sorted(_ALLOWED_ORDERINGS)}")
        return v
