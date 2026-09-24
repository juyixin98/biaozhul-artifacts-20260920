"""Request/response schemas for the HTTP API."""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, Field, field_validator


class EdgeIn(BaseModel):
    """One relative-pose constraint."""

    i: int = Field(ge=0, description="index of the 'from' node")
    j: int = Field(ge=0, description="index of the 'to' node")
    measurement: list[float] = Field(
        min_length=3, max_length=3, description="measured relative pose [x, y, theta]"
    )
    information: list[list[float]] = Field(
        description="3x3 information matrix (symmetric positive definite)"
    )

    @field_validator("information")
    @classmethod
    def _check_information(cls, v: list[list[float]]) -> list[list[float]]:
        m = np.asarray(v, dtype=float)
        if m.shape != (3, 3):
            raise ValueError("information must be a 3x3 matrix")
        if not np.allclose(m, m.T, atol=1e-9):
            raise ValueError("information matrix must be symmetric")
        try:
            np.linalg.cholesky(m)
        except np.linalg.LinAlgError:
            raise ValueError("information matrix must be positive definite")
        return v


class OptionsIn(BaseModel):
    max_iterations: int = Field(default=50, ge=1, le=1000)
    robust_kernel: str = Field(default="huber", pattern="^(none|huber)$")
    huber_delta: float = Field(default=1.0, gt=0)
    gradient_tolerance: float = Field(default=1e-6, gt=0)
    cost_tolerance: float = Field(default=1e-9, ge=0)
    fix_node: int = Field(default=0, ge=0, description="node held fixed (gauge)")
    degeneracy_threshold: float = Field(default=1e-9, gt=0)


class OptimizeRequest(BaseModel):
    initial_poses: list[list[float]] = Field(
        min_length=1, description="initial pose guesses [[x, y, theta], ...]"
    )
    edges: list[EdgeIn] = Field(min_length=1)
    options: OptionsIn = OptionsIn()

    @field_validator("initial_poses")
    @classmethod
    def _check_poses(cls, v: list[list[float]]) -> list[list[float]]:
        for k, p in enumerate(v):
            if len(p) != 3:
                raise ValueError(f"pose {k} must have exactly 3 components")
        return v


class DegeneracyReport(BaseModel):
    is_degenerate: bool
    min_eigenvalue: float | None = None
    max_eigenvalue: float | None = None
    condition_estimate: float | None = None
    note: str = ""


class OptimizeResponse(BaseModel):
    success: bool
    message: str
    optimized_poses: list[list[float]]
    iterations: int
    converged: bool
    cost_history: list[float]
    gradient_norm_history: list[float]
    final_cost: float
    final_gradient_norm: float
    degeneracy: DegeneracyReport
