"""HTTP API 的 Pydantic 协议模型与入参校验。"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field, field_validator


class PointCloudRequest(BaseModel):
    points: list[list[float]] = Field(
        ..., description="(N,3) 点云，全局坐标 [[x,y,z], ...]"
    )
    point_ids: list[str] | None = Field(
        default=None, description="可选，调用方提供的点 ID，长度必须等于 N 且唯一"
    )
    seed_indices: list[int] | None = Field(
        default=None, description="可选先验地面种子点的下标（0 基）"
    )
    params: "ParamsModel | None" = Field(default=None, description="分割参数覆盖")

    @field_validator("points")
    @classmethod
    def _check_shape(cls, v):
        if not isinstance(v, list):
            raise ValueError("points 必须是数组")
        for row in v:
            if not isinstance(row, list) or len(row) != 3:
                raise ValueError("points 中每个点必须是长度为 3 的数组 [x,y,z]")
            for x in row:
                if isinstance(x, bool) or not isinstance(x, (int, float)):
                    raise ValueError("点坐标必须是数值")
                if not math.isfinite(x):
                    raise ValueError("点坐标必须是有限数值")
        return v

    @field_validator("point_ids")
    @classmethod
    def _check_ids(cls, v):
        if v is not None:
            if any(not isinstance(i, str) for i in v):
                raise ValueError("point_ids 必须全部是字符串")
            if len(v) != len(set(v)):
                raise ValueError("point_ids 必须唯一")
        return v

    @field_validator("seed_indices")
    @classmethod
    def _check_seeds(cls, v):
        if v is not None:
            if any(not isinstance(i, int) or isinstance(i, bool) for i in v):
                raise ValueError("seed_indices 必须是整数下标")
            if any(i < 0 for i in v):
                raise ValueError("seed_indices 下标必须 >= 0")
        return v


class ParamsModel(BaseModel):
    ransac_iterations: int | None = Field(default=None, ge=1)
    distance_threshold: float | None = Field(default=None, gt=0)
    seed_rng: int | None = None
    max_tilt_deg: float | None = Field(default=None, gt=0, lt=90)
    min_inlier_ratio: float | None = Field(default=None, gt=0, le=1)
    min_inlier_count: int | None = Field(default=None, ge=3)
    min_unique_points: int | None = Field(default=None, ge=3)
    point_margin: float | None = Field(default=None, ge=1)
    tile_size: float | None = None
    tile_overlap: float | None = Field(default=None, ge=0, lt=1)
    conflict_margin: float | None = Field(default=None, gt=1)

    @field_validator("tile_size")
    @classmethod
    def _check_tile_size(cls, v):
        if v is not None and not isinstance(v, (int, float)):
            raise ValueError("tile_size 必须是数值")
        if v is not None and (isinstance(v, bool) or not math.isfinite(v)):
            raise ValueError("tile_size 必须是有限数值")
        return v


class EvaluateRequest(BaseModel):
    predicted: list[str] = Field(..., description="预测标签 ground/non_ground/undecidable")
    truth: list[int] = Field(..., description="人工真值：1=地面，0=非地面")

    @field_validator("predicted")
    @classmethod
    def _check_labels(cls, v):
        allowed = {"ground", "non_ground", "undecidable"}
        bad = set(v) - allowed
        if bad:
            raise ValueError(f"非法标签: {sorted(bad)}")
        return v

    @field_validator("truth")
    @classmethod
    def _check_truth(cls, v):
        bad = set(v) - {0, 1}
        if bad:
            raise ValueError("truth 只能包含 0 或 1")
        return v


PointCloudRequest.model_rebuild()
