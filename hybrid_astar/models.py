"""Pydantic request/response models for the planning API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Pose(BaseModel):
    x: float
    y: float
    theta: float = 0.0  # heading in radians


class VehicleParams(BaseModel):
    length: float = 2.0
    width: float = 1.0
    min_turning_radius: float = 2.0
    n_circles: int = 3
    margin: float = 0.1


class PlannerParams(BaseModel):
    heading_bins: int = 36
    primitive_length: float = 1.0
    sample_step: float = 0.25
    reverse_penalty: float = 2.0
    gear_switch_penalty: float = 2.0
    curvature_penalty: float = 0.1
    use_heuristic: bool = True
    heuristic_weight: float = 1.0
    allow_reverse: bool = True
    max_expansions: int = 200_000
    goal_tol_xy: float = 0.75
    goal_tol_theta_deg: float = 20.0


class PlanRequest(BaseModel):
    """One offline planning request (synthetic map only)."""

    grid: list[list[int]] = Field(..., description="occupancy grid, 1=obstacle")
    resolution: float = 0.5
    origin: tuple[float, float] = (0.0, 0.0)
    start: Pose
    goal: Pose
    vehicle: VehicleParams = VehicleParams()
    planner: PlannerParams = PlannerParams()


class PathPoint(BaseModel):
    x: float
    y: float
    theta: float
    gear: int


class PlanResponse(BaseModel):
    success: bool
    message: str
    cost: float | None = None
    expansions: int = 0
    elapsed_ms: float = 0.0
    path: list[PathPoint] = []
