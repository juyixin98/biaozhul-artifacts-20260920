"""Pydantic request/response schemas with strict numeric validation."""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .mpc import MpcConfig

_DEFAULT = MpcConfig()


class ConfigOverride(BaseModel):
    """All fields optional; omitted fields take the deterministic defaults."""

    horizon: int | None = Field(default=None, ge=1, le=500)
    dt: float | None = Field(default=None, gt=0, le=10.0)
    v_max: float | None = Field(default=None, gt=0)
    u_max: float | None = Field(default=None, gt=0)
    q_position: float | None = Field(default=None, ge=0)
    q_velocity: float | None = Field(default=None, ge=0)
    r_input: float | None = Field(default=None, ge=0)
    r_rate: float | None = Field(default=None, ge=0)
    time_limit: float | None = Field(default=None, gt=0, le=60.0)
    max_iter: int | None = Field(default=None, ge=1, le=10_000_000)
    eps_abs: float | None = Field(default=None, gt=0)
    eps_rel: float | None = Field(default=None, gt=0)
    polish: bool | None = None
    verify_tol: float | None = Field(default=None, gt=0)

    def build(self) -> MpcConfig:
        overrides = {
            k: v for k, v in self.model_dump().items() if v is not None
        }
        return dataclasses_replace(overrides)


def dataclasses_replace(overrides: dict[str, Any]) -> MpcConfig:
    return MpcConfig(**overrides)


def _finite_list(name: str, values: list[float]) -> list[float]:
    if not isinstance(values, list) or len(values) == 0:
            raise ValueError(f"{name} must be a non-empty list")
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{name} entries must be numbers")
        if not math.isfinite(v):
            raise ValueError(f"{name} entries must be finite")
    return values


class SolveRequest(BaseModel):
    state: list[float] = Field(description="[position, velocity]")
    reference: list[float] = Field(description="position targets, len 1..horizon")
    u_prev: float = 0.0
    config: ConfigOverride | None = None

    @field_validator("state")
    @classmethod
    def _state(cls, v: list[float]) -> list[float]:
        if len(v) != 2:
            raise ValueError("state must have exactly 2 entries [p, v]")
        return _finite_list("state", v)

    @field_validator("reference")
    @classmethod
    def _ref(cls, v: list[float]) -> list[float]:
        return _finite_list("reference", v)

    @field_validator("u_prev")
    @classmethod
    def _uprev(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("u_prev must be finite")
        return v


class SimulateRequest(BaseModel):
    initial_state: list[float] = Field(description="[position, velocity]")
    reference: list[float] = Field(description="target trajectory, length >= 1")
    steps: int = Field(default=50, ge=1, le=2000)
    disturbance_amplitude: float = Field(default=0.0, ge=0)
    disturbance_seed: int = Field(default=0, ge=0)
    config: ConfigOverride | None = None

    @field_validator("initial_state")
    @classmethod
    def _state(cls, v: list[float]) -> list[float]:
        if len(v) != 2:
            raise ValueError("initial_state must have exactly 2 entries [p, v]")
        return _finite_list("initial_state", v)

    @field_validator("reference")
    @classmethod
    def _ref(cls, v: list[float]) -> list[float]:
        return _finite_list("reference", v)
