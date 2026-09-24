"""Pydantic API schemas. Extra fields are forbidden so client typos fail."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator

SAFETY_BANNER = (
    "EXPERIMENTAL SYNTHETIC-MODEL OUTPUT - NOT FOR REAL CHARGE CONTROL, "
    "NOT SAFETY-RELATED, OFFLINE ESTIMATION ONLY"
)


class SampleIn(BaseModel):
    model_config = {"extra": "forbid"}

    t_s: float = Field(..., description="timestamp in seconds (epoch or relative), monotonic non-decreasing within a batch")
    current_a: float = Field(..., description="current; >0 discharge, <0 charge")
    voltage_v: float = Field(..., gt=0, description="terminal pack voltage, volts")
    temp_c: float = Field(..., ge=-100.0, le=200.0, description="pack/cell temperature, degrees C")

    @field_validator("t_s")
    @classmethod
    def t_finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("t_s must be finite")
        return v

    @field_validator("current_a")
    @classmethod
    def i_finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("current_a must be finite")
        return v


class EstimateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    initial_soc: Optional[float] = Field(None, ge=0.0, le=1.0)
    samples: list[SampleIn] = Field(..., min_length=1)


class IngestRequest(BaseModel):
    model_config = {"extra": "forbid"}

    samples: list[SampleIn] = Field(..., min_length=1)
    replay_window_s: Optional[float] = Field(None, gt=0.0)
    initial_soc: Optional[float] = Field(None, ge=0.0, le=1.0)


class IngestResponse(BaseModel):
    session_id: str
    accepted: int
    rejected: int
    rejections: list[dict]
    n_stored: int
    params_version: str
    params_digest: str
    not_for_control: str = SAFETY_BANNER


class StatusResponse(BaseModel):
    session_id: str
    finalized: bool
    n_samples: int
    replay_window_s: float
    result: dict
    params_version: str
    params_digest: str
    not_for_control: str = SAFETY_BANNER


class ParamsInfo(BaseModel):
    version: str
    digest: str
    sign_convention: dict
    verified: bool
    not_for_control: str = SAFETY_BANNER


class VerifyResponse(BaseModel):
    ok: bool
    records: int
    last_hash: str
