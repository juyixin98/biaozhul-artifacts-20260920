"""Pydantic request/response schemas for the IK service."""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, Field, field_validator


class IKRequest(BaseModel):
    """Target end-effector pose.

    position: TCP target in the robot base frame, metres [x, y, z].
    rotation: 3x3 rotation matrix R_{0,6} (orthogonal, det = +1).
    current_joints: optional present joint configuration in radians; the
        service selects the verified solution nearest to it using periodic
        angular distance.
    weights: optional 6 positive per-joint weights for that distance.
    """

    position: list[float] = Field(..., min_length=3, max_length=3)
    rotation: list[list[float]] = Field(..., min_length=3, max_length=3)
    current_joints: list[float] | None = Field(None, min_length=6, max_length=6)
    weights: list[float] | None = Field(None, min_length=6, max_length=6)
    seed: int = Field(20260923, ge=0, le=2**63 - 1)

    @field_validator("rotation")
    @classmethod
    def _check_rotation_rows(cls, v: list[list[float]]) -> list[list[float]]:
        if any(len(row) != 3 for row in v):
            raise ValueError("rotation must be a 3x3 matrix")
        if not all(isinstance(x, (int, float)) for row in v for x in row):
            raise ValueError("rotation entries must be numbers")
        R = np.asarray(v, dtype=float)
        if not np.all(np.isfinite(R)):
            raise ValueError("rotation entries must be finite")
        if not np.allclose(R @ R.T, np.eye(3), atol=1e-4):
            raise ValueError("rotation is not orthogonal")
        if abs(float(np.linalg.det(R)) - 1.0) > 1e-4:
            raise ValueError("rotation determinant must be +1")
        return v

    @field_validator("position", "current_joints")
    @classmethod
    def _check_finite(cls, v):
        if v is not None and not all(isinstance(x, (int, float)) and
                                     np.isfinite(x) for x in v):
            raise ValueError("values must be finite numbers")
        return v

    @field_validator("weights")
    @classmethod
    def _check_weights(cls, v):
        if v is not None and any(x <= 0 for x in v):
            raise ValueError("weights must be 6 positive numbers")
        return v

    def rotation_array(self) -> np.ndarray:
        return np.asarray(self.rotation, dtype=float)

    def position_array(self) -> np.ndarray:
        return np.asarray(self.position, dtype=float)


class HealthResponse(BaseModel):
    status: str
    model: str
    reach_max_m: float


class ErrorResponse(BaseModel):
    detail: str
