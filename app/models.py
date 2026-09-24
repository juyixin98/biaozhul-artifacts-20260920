"""请求/响应数据模型（Pydantic v2）。"""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, Field, field_validator


class SmoothRequest(BaseModel):
    points: list[list[float]] = Field(
        ..., description="折线路径 [[x,y], ...]，长度 2..100；首尾为固定端点"
    )
    obstacles: list[list[float]] = Field(
        default_factory=list,
        description="矩形障碍 [[xmin,ymin,xmax,ymax], ...]（轴对齐）",
    )
    deviation_bound: float = Field(
        ..., gt=0, description="每个控制点相对原路径的最大允许偏离（世界单位，硬约束）"
    )
    safety_margin: float = Field(
        0.0, ge=0, description="轨迹到障碍的最小安全间隙（世界单位）"
    )
    max_curvature: float = Field(
        ..., gt=0, description="曲率上界 κ_max（1/世界单位，Menger 曲率）"
    )
    max_iter: int = Field(150, ge=1, le=500, description="SLSQP 主迭代预算（硬上限 500）")
    save_run: bool = Field(True, description="是否将目标函数与约束残差落盘到 runs/")

    @field_validator("points")
    @classmethod
    def _check_points(cls, v):
        if not (2 <= len(v) <= 100):
            raise ValueError("points 长度必须在 2..100 之间")
        for i, p in enumerate(v):
            if len(p) != 2:
                raise ValueError(f"points[{i}] 必须是 [x, y]")
            for x in p:
                if not np.isfinite(x):
                    raise ValueError(f"points[{i}] 含非有限数值")
                if abs(x) > 1.0e9:
                    raise ValueError(f"points[{i}] 坐标绝对值过大（>1e9）")
        return v

    @field_validator("obstacles")
    @classmethod
    def _check_obstacles(cls, v):
        for j, o in enumerate(v):
            if len(o) != 4:
                raise ValueError(f"obstacles[{j}] 必须是 [xmin,ymin,xmax,ymax]")
            if not all(np.isfinite(x) for x in o):
                raise ValueError(f"obstacles[{j}] 含非有限数值")
            xmin, ymin, xmax, ymax = o
            if not (xmin < xmax and ymin < ymax):
                raise ValueError(f"obstacles[{j}] 要求 xmin<xmax 且 ymin<ymax")
        return v
