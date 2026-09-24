"""Request/response schemas. Units are explicit in field names and docs."""
from __future__ import annotations

import math
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SampleIn(BaseModel):
    """One telemetry sample.

    * ``t_s``: UTC epoch seconds (float)
    * ``current_a``: amperes; positive = discharge, negative = charge
    * ``voltage_v``: volts
    * ``temp_c``: degrees Celsius
    """

    model_config = ConfigDict(extra="forbid")

    t_s: float = Field(ge=0.0, description="UTC epoch seconds")
    current_a: float = Field(ge=-1000.0, le=1000.0, description="A, + = discharge, - = charge")
    voltage_v: float = Field(ge=0.0, le=10.0, description="V")
    temp_c: float = Field(ge=-60.0, le=120.0, description="deg C")

    @field_validator("t_s", "current_a", "voltage_v", "temp_c")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("value must be finite (NaN/Inf rejected)")
        return v


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    initial_soc: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    initial_sigma: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    samples: list[SampleIn] = Field(min_length=1, max_length=200_000)


class EstimateRequest(BaseModel):
    """Stateless one-shot offline estimation."""

    model_config = ConfigDict(extra="forbid")

    initial_soc: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    initial_sigma: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    samples: list[SampleIn] = Field(min_length=1, max_length=200_000)
