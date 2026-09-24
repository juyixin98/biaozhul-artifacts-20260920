"""请求/响应 Pydantic 模型（HTTP 协议）。"""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, Field, field_validator, model_validator

from .core.ik import IKStatus


class OrientationInput(BaseModel):
    """目标姿态：3x3 旋转矩阵或四元数 [x, y, z, w]，二选一。"""

    rotation_matrix: list[list[float]] | None = Field(
        default=None, description="3x3 旋转矩阵（基坐标下）"
    )
    quaternion: list[float] | None = Field(
        default=None, description="四元数 [x, y, z, w]，无需预归一化"
    )

    @model_validator(mode="after")
    def _validate(self) -> "OrientationInput":
        if (self.rotation_matrix is None) == (self.quaternion is None):
            raise ValueError("orientation 必须且只能提供 rotation_matrix 或 quaternion 之一")
        if self.rotation_matrix is not None:
            R = np.asarray(self.rotation_matrix, dtype=float)
            if R.shape != (3, 3):
                raise ValueError("rotation_matrix 必须是 3x3")
            err = float(np.linalg.norm(R.T @ R - np.eye(3)))
            if err > 1e-4:
                raise ValueError(f"rotation_matrix 非正交（R^T R - I 范数 {err:.2e}）")
            if abs(float(np.linalg.det(R)) - 1.0) > 1e-4:
                raise ValueError("rotation_matrix 行列式必须为 +1")
        else:
            q = np.asarray(self.quaternion, dtype=float)
            if q.shape != (4,):
                raise ValueError("quaternion 必须是长度 4 的 [x,y,z,w]")
            if float(np.linalg.norm(q)) < 1e-12:
                raise ValueError("quaternion 不能为零向量")
        return self

    def to_matrix(self) -> np.ndarray:
        if self.rotation_matrix is not None:
            return np.asarray(self.rotation_matrix, dtype=float)
        q = np.asarray(self.quaternion, dtype=float)
        x, y, z, w = q / np.linalg.norm(q)
        R = np.array(
            [
                [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
            ],
            dtype=float,
        )
        return R


class IKRequest(BaseModel):
    position: list[float] = Field(..., min_length=3, max_length=3, description="目标位置 [x,y,z] (m)")
    orientation: OrientationInput
    current_joints: list[float] | None = Field(
        default=None,
        min_length=6,
        max_length=6,
        description="当前关节角 (rad)，用于多解择优；可不提供（默认零位）",
    )
    extra_seeds: list[list[float]] | None = Field(
        default=None, description="额外初值（每组 6 个关节角，rad）"
    )

    @field_validator("extra_seeds")
    @classmethod
    def _check_seeds(cls, v):
        if v is not None:
            for s in v:
                if len(s) != 6:
                    raise ValueError("每个 extra_seed 必须是 6 个关节角")
        return v

    @field_validator("current_joints")
    @classmethod
    def _finite(cls, v):
        if v is not None and not all(isinstance(x, (int, float)) and np.isfinite(x) for x in v):
            raise ValueError("current_joints 必须为有限实数")
        return v


class CandidateOut(BaseModel):
    joints: list[float]
    joints_wrapped: list[float]
    position_error: float
    orientation_error: float
    joint_distance_to_current: float
    raw_distance_to_current: float
    seed_index: int


class AttemptOut(BaseModel):
    seed_index: int
    enforce_limits: bool
    converged: bool
    iterations: int
    position_error: float
    orientation_error: float
    sigma_min: float
    saturated_axes: list[int]
    terminated_reason: str


class IKResponse(BaseModel):
    status: IKStatus
    success: bool
    message: str
    joints: list[float] | None = None
    joints_wrapped: list[float] | None = None
    position_error: float | None = None
    orientation_error: float | None = None
    candidates: list[CandidateOut] = []
    diagnostics: dict = {}
