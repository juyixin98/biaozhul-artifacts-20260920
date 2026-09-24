"""请求/响应模型 (Pydantic v2)。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

FaultKind = Literal[
    "lose_artifact",   # 模拟产物在发布前丢失
    "corrupt_result",  # 模拟计算结果被运行期篡改, 校验不通过
    "crash",           # 模拟运行中进程崩溃 (os._exit)
]


class CalibrationIn(BaseModel):
    """二维坐标标定: x' = r00*x + r01*y + tx 等。"""

    matrix: list[list[float]] = Field(description="2x2 旋转/缩放矩阵")
    translation: list[float] = Field(description="二维平移 [tx, ty]")
    note: str = ""

    @field_validator("matrix")
    @classmethod
    def _check_matrix(cls, v: list[list[float]]) -> list[list[float]]:
        if len(v) != 2 or any(len(row) != 2 for row in v):
            raise ValueError("matrix 必须是 2x2")
        return v

    @field_validator("translation")
    @classmethod
    def _check_translation(cls, v: list[float]) -> list[float]:
        if len(v) != 2:
            raise ValueError("translation 必须有 2 个分量")
        return v


class ParamsIn(BaseModel):
    sample_rate: float = Field(gt=0.0, le=1.0)
    grid_size: float = Field(gt=0.0)
    min_cluster_size: int = Field(ge=1)
    bounds: list[float] = Field(description="标定时域范围 [xmin,ymin,xmax,ymax]")

    @field_validator("bounds")
    @classmethod
    def _check_bounds(cls, v: list[float]) -> list[float]:
        if len(v) != 4 or v[0] >= v[2] or v[1] >= v[3]:
            raise ValueError("bounds 必须是 [xmin,ymin,xmax,ymax] 且 min<max")
        return v


class AlgorithmIn(BaseModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = ""


class SnapshotIn(BaseModel):
    bag_id: str
    params_id: str
    calibration_id: str
    algorithm_id: str


class AttemptIn(BaseModel):
    delay_ms: int = Field(default=0, ge=0, le=10_000)
    fault: FaultKind | None = None
