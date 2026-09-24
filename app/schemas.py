"""HTTP 协议的 Pydantic 模型。

测量消息（单条）：
{
  "id": "odo-0001",              # 全局唯一，去重依据
  "type": "odometry" | "gnss",
  "time": 100.25,                # 测量时间（秒，任意单调时间基准）
  "measurement": [vx, vy]        # odometry
  "measurement": [px, py]        # gnss
  "R": [[r00,r01],[r10,r11]],    # 测量协方差（必须对称、半正定）
  "seq": 12,                     # 可选，同时间消息的确定性次序
  "gate_nis": 9.21               # 可选，覆盖默认门限
}
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

MeasType = Literal["odometry", "gnss"]


class Measurement(BaseModel):
    id: str = Field(..., min_length=1, description="全局唯一测量 ID，用于去重")
    type: MeasType
    time: float = Field(..., description="测量时间（秒）")
    measurement: list[float] = Field(..., min_length=2, max_length=2)
    R: list[list[float]] = Field(..., description="2x2 测量噪声协方差")
    seq: int = Field(0, description="同时间消息的确定性次序（小者先处理）")
    gate_nis: float | None = Field(None, gt=0.0)

    @field_validator("measurement")
    @classmethod
    def _finite_measurement(cls, v: list[float]) -> list[float]:
        if any(x != x or x in (float("inf"), float("-inf")) for x in v):
            raise ValueError("measurement must be finite")
        return v

    @field_validator("R")
    @classmethod
    def _shape_R(cls, v: list[list[float]]) -> list[list[float]]:
        if len(v) != 2 or any(len(row) != 2 for row in v):
            raise ValueError("R must be 2x2")
        for row in v:
            for x in row:
                if x != x or x in (float("inf"), float("-inf")):
                    raise ValueError("R must be finite")
        return v

    @field_validator("time")
    @classmethod
    def _finite_time(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("time must be finite")
        return v


class MeasurementBatch(BaseModel):
    measurements: list[Measurement] = Field(..., min_length=1)


class Health(BaseModel):
    status: str
    initialized: bool
    buffered: int
    checkpoints: int
    latest_time: float | None


class RejectionEvidence(BaseModel):
    id: str | None
    time: float | None
    type: str | None
    reason: str
    detail: dict[str, Any]
