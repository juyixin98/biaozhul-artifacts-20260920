"""Pydantic request/response schemas for the ICP service."""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, Field, field_validator


class ICPRequest(BaseModel):
    source: list[list[float]] = Field(
        ..., description="(N, 3) moving point cloud"
    )
    target: list[list[float]] = Field(
        ..., description="(M, 3) fixed point cloud"
    )
    R0: list[list[float]] | None = Field(
        None, description="(3, 3) initial rotation; identity if omitted"
    )
    t0: list[float] | None = Field(
        None, description="(3,) initial translation; zero if omitted"
    )
    max_iterations: int = Field(50, ge=1, le=1000)
    tolerance: float = Field(1e-7, gt=0.0, le=1.0)
    max_correspondence_distance: float | None = Field(None, gt=0.0)
    robust_quantile: float | None = Field(
        0.9,
        ge=0.0,
        le=1.0,
        description="fraction of closest correspondences kept; <1 enables robust trimming",
    )
    min_inliers: int = Field(3, ge=3)
    degeneracy_ratio: float = Field(1e-3, gt=0.0, lt=1.0)

    @field_validator("source", "target")
    @classmethod
    def _check_points(cls, v: list[list[float]]) -> list[list[float]]:
        if not v:
            raise ValueError("point cloud must not be empty")
        if any(len(p) != 3 for p in v):
            raise ValueError("every point must have exactly 3 coordinates")
        return v

    @field_validator("R0")
    @classmethod
    def _check_R0(cls, v: list[list[float]] | None) -> list[list[float]] | None:
        if v is None:
            return None
        if len(v) != 3 or any(len(row) != 3 for row in v):
            raise ValueError("R0 must be a 3x3 matrix")
        R = np.asarray(v, dtype=float)
        if np.max(np.abs(R.T @ R - np.eye(3))) > 1e-4:
            raise ValueError("R0 is not orthogonal")
        if abs(np.linalg.det(R) - 1.0) > 1e-2:
            raise ValueError("R0 must be a proper rotation (det = +1)")
        return v

    @field_validator("t0")
    @classmethod
    def _check_t0(cls, v: list[float] | None) -> list[float] | None:
        if v is not None and len(v) != 3:
            raise ValueError("t0 must have length 3")
        return v


class SceneRequest(BaseModel):
    n_points: int = Field(120, ge=3, le=10000)
    kind: str = Field("volume", pattern="^(volume|planar|collinear|clusters)$")
    angle_deg: float = Field(20.0, ge=0.0, le=180.0)
    translation: float = Field(0.5, ge=0.0)
    noise_std: float = Field(0.01, ge=0.0)
    overlap: float = Field(1.0, gt=0.0, le=1.0)
    n_clutter: int = Field(0, ge=0, le=10000)
    seed: int = Field(0, ge=0)


class DemoRequest(BaseModel):
    scenario: str = Field(
        "nominal",
        pattern="^(nominal|collinear|partial_overlap|bad_initial|nonconverge)$",
    )
    seed: int = Field(0, ge=0)
