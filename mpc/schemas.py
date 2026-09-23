"""Pydantic request/response schemas for the MPC HTTP API."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

Vec2 = Annotated[list[float], Field(min_length=2, max_length=2)]


class SolveRequest(BaseModel):
    state: Vec2 = Field(..., description="Current [position, velocity]")
    reference: list[Vec2] = Field(
        ..., description="Reference trajectory, N+1 rows of [p_ref, v_ref]"
    )
    u_prev: float = Field(0.0, description="Last applied acceleration")

    @field_validator("reference")
    @classmethod
    def _rows(cls, v: list[list[float]]) -> list[list[float]]:
        if not v:
            raise ValueError("reference must contain N+1 rows")
        return v


class RolloutRequest(BaseModel):
    x0: Vec2 = Field(..., description="Initial [position, velocity]")
    n_steps: int = Field(40, ge=1, le=1000, description="Simulated steps")
    # Reference specification: either a fixed set-point held for the whole
    # run, or an abrupt step (before -> after at change_step).
    reference_setpoint: Vec2 | None = Field(
        None, description="Fixed [p_ref, v_ref] set-point"
    )
    reference_before: Vec2 | None = None
    reference_after: Vec2 | None = None
    change_step: int | None = Field(None, ge=0)

    disturbance_kind: Literal[
        "none", "constant", "sine", "square", "ramp", "bump"
    ] = "none"
    disturbance_amplitude: float | list[float] = 0.0
    disturbance_frequency: float = 1.0
    disturbance_phase: float = 0.0
    disturbance_step: int = 0

    # Optional controller configuration overrides.
    dt: float | None = Field(None, gt=0)
    horizon: int | None = Field(None, ge=1, le=200)
    v_max: float | None = Field(None, gt=0)
    a_max: float | None = Field(None, gt=0)
    q_pos: float | None = Field(None, ge=0)
    q_vel: float | None = Field(None, ge=0)
    q_term_pos: float | None = Field(None, ge=0)
    q_term_vel: float | None = Field(None, ge=0)
    r_delta: float | None = Field(None, ge=0)
    k_brake: float | None = Field(None, gt=0)
    osqp_time_limit: float | None = Field(None, gt=0, le=60.0)


class StepOut(BaseModel):
    k: int
    t: float
    state: list[float]
    reference: list[float]
    control: float
    status: str
    fallback: bool
    reason: str
    solve_time_s: float
    residuals: dict
    disturbance: list[float]


class RolloutResponse(BaseModel):
    steps: list[StepOut]
    final_state: list[float]
    fallback_count: int
    fallback_reasons: dict[str, int]
    max_velocity: float
    max_acceleration: float
    max_velocity_violation: float
    max_acceleration_violation: float
    config: dict
