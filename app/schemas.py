"""Pydantic request/response models for the smoothing API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .smoother import MAX_POINTS_HARD_LIMIT

Point = tuple[float, float]

STATUS_VALUES = (
    "optimal",
    "infeasible",
    "iteration_limit",
    "timeout",
    "solver_error",
    "input_collision",
    "invalid_input",
    "error",
)
Status = Literal[
    "optimal", "infeasible", "iteration_limit", "timeout",
    "solver_error", "input_collision", "invalid_input", "error",
]


class Rectangle(BaseModel):
    """Axis-aligned rectangle given by center + full width/height."""

    model_config = ConfigDict(extra="forbid")

    cx: float = Field(..., description="center x")
    cy: float = Field(..., description="center y")
    width: float = Field(..., gt=0, description="full width (> 0)")
    height: float = Field(..., gt=0, description="full height (> 0)")


class SmoothingParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_iterations: int = Field(200, ge=1, le=1000)
    timeout_seconds: float = Field(30.0, gt=0, le=300)
    curvature_cap: float | None = Field(None, gt=0)
    corridor: float | None = Field(None, gt=0)
    clearance: float = Field(0.0, ge=0)
    min_edge_length: float | None = Field(None, gt=0)
    deviation_weight: float = Field(1.0, ge=0)
    bend_weight: float = Field(0.0, ge=0)
    jerk_weight: float = Field(0.05, ge=0)
    optimize_midpoint_clearance: bool = True

    @model_validator(mode="after")
    def _at_least_one_weight(self):
        if self.deviation_weight + self.bend_weight + self.jerk_weight <= 0:
            raise ValueError(
                "at least one of deviation_weight/bend_weight/jerk_weight must be positive"
            )
        return self


class SmoothRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: list[Point] = Field(
        ...,
        description="polyline vertices, 2..%d points; endpoints are fixed" % MAX_POINTS_HARD_LIMIT,
    )
    obstacles: list[Rectangle] = Field(default_factory=list)
    params: SmoothingParameters = Field(default_factory=SmoothingParameters)
    request_id: str | None = Field(None, max_length=128)

    @field_validator("path")
    @classmethod
    def _check_path(cls, v):
        if len(v) < 2:
            raise ValueError("path must contain at least 2 points")
        if len(v) > MAX_POINTS_HARD_LIMIT:
            raise ValueError(f"path exceeds {MAX_POINTS_HARD_LIMIT}-point limit ({len(v)} given)")
        for p in v:
            if not (p[0] == p[0] and p[1] == p[1]):  # NaN check
                raise ValueError("path coordinates must be finite")
        return v


class VerificationReport(BaseModel):
    ok: bool
    min_clearance: float | None
    min_sample_sdf: float | None
    min_segment_distance: float | None
    samples_checked: int
    control_segments_checked: int
    sample_spacing: float
    worst_sample: list[float] | None
    worst_segment_index: int | None
    required_clearance: float


class Residuals(BaseModel):
    max_violation: float
    max_violation_normalized: float
    residuals_physical: dict[str, float]
    residuals_normalized: dict[str, float]
    observed_physical: dict[str, float | None]
    observed_normalized: dict[str, float]
    satisfied: bool


class SmoothResponse(BaseModel):
    status: Status
    success: bool
    message: str
    points: list[Point]
    point_count: int
    fixed_start: Point
    fixed_goal: Point
    iterations: int
    max_iterations: int
    objective: float
    objective_components: dict[str, float]
    residuals: Residuals
    verification: VerificationReport | None
    elapsed_seconds: float
    n_variables: int
    n_control_points: int
    collapsed_duplicates: int
    parameters: dict
    request_id: str | None
    request_sha256: str
    response_sha256: str
