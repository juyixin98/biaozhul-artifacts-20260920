"""请求/响应 Pydantic 模型与运行时配置。"""

from __future__ import annotations

import dataclasses
from typing import Literal, Optional

import numpy as np
from pydantic import BaseModel, Field, field_validator, model_validator

from .constants import (
    ACCEL_UNIT_TO_MS2,
    CANONICAL_AXES,
    GYRO_UNIT_TO_RADS,
    TIME_UNIT_TO_S,
)

AccelUnit = Literal["ms2", "g"]
GyroUnit = Literal["rads", "degs", "rad_h", "deg_h", "rpm"]
TimeUnit = Literal["s", "ms", "us", "ns"]


class UnitsModel(BaseModel):
    """输入数据单位（内部统一换算为 SI）。"""

    accel: AccelUnit = "ms2"
    gyro: GyroUnit = "rads"
    time: TimeUnit = "s"


class AxesModel(BaseModel):
    """坐标轴重映射：内部 X/Y/Z 分别取自输入的哪一列、取什么符号。

    例如 ``{"x": "+y", "y": "-x", "z": "+z"}`` 表示输入为 NED/载体系旋转后的排列。
    三个内部轴必须分别引用不同的输入轴，否则拒绝请求。
    """

    x: Literal["+x", "-x", "+y", "-y", "+z", "-z"] = "+x"
    y: Literal["+x", "-x", "+y", "-y", "+z", "-z"] = "+y"
    z: Literal["+x", "-x", "+y", "-y", "+z", "-z"] = "+z"

    @model_validator(mode="after")
    def _check_distinct(self) -> "AxesModel":
        refs = [self.x[1], self.y[1], self.z[1]]
        if sorted(refs) != ["x", "y", "z"]:
            raise ValueError(
                "axes 必须对输入 x/y/z 三个轴各引用一次（允许变号与重排）"
            )
        return self


class DetectionModel(BaseModel):
    """静止检测与漂移模型参数。所有阈值均使用 SI 单位。"""

    window_seconds: float = Field(0.5, gt=0.0, le=60.0)
    window_overlap: float = Field(0.5, ge=0.0, lt=1.0)
    min_static_seconds: float = Field(1.0, gt=0.0, le=3600.0)
    # 窗内陀螺逐轴标准差上限（rad/s）；默认 0.02 rad/s ≈ 1.15 °/s
    gyro_std_thresh: float = Field(0.02, gt=0.0)
    # 窗内加速度逐轴标准差上限（m/s^2）
    accel_std_thresh: float = Field(0.05, gt=0.0)
    # 重力幅值容差（m/s^2）：|norm(a) - g| <= tol
    gravity_mag_tol: float = Field(0.25, gt=0.0)
    # 相邻静止窗之间允许跨越的最长非静止窗数（形态学闭运算）
    bridge_max_gap_windows: int = Field(2, ge=0)
    # 时间缺口：gap > factor * 中位采样间隔 即分段（factor 很大时等价于不分段）
    time_gap_factor: float = Field(5.0, gt=1.0)
    # 异常峰值：逐样本稳健 z 分数（中位数 + k*MAD）阈值
    spike_z: float = Field(8.0, gt=0.0)
    # 温漂：纳入温度模型所需的最小静止窗数与温度跨度
    temp_min_windows: int = Field(8, ge=3)
    temp_min_span: float = Field(5.0, gt=0.0)
    # 时漂：纳入时间模型所需的最小静止窗数与时间跨度（秒）
    drift_min_windows: int = Field(8, ge=3)
    drift_min_span: float = Field(30.0, gt=0.0)


class EstimateRequest(BaseModel):
    """/estimate 请求体。"""

    timestamps: list[float] = Field(..., min_length=1)
    accel: list[list[float]] = Field(..., min_length=1)
    gyro: list[list[float]] = Field(..., min_length=1)
    temperature: Optional[list[float]] = None
    units: UnitsModel = Field(default_factory=UnitsModel)
    axes: AxesModel = Field(default_factory=AxesModel)
    detection: DetectionModel = Field(default_factory=DetectionModel)

    @field_validator("accel", "gyro")
    @classmethod
    def _check_triplets(cls, v: list[list[float]]) -> list[list[float]]:
        for i, row in enumerate(v):
            if len(row) != 3:
                raise ValueError(f"第 {i} 行不是 3 分量向量")
            if not all(np.isfinite(row)):
                raise ValueError(f"第 {i} 行包含 NaN/Inf")
        return v

    @field_validator("timestamps")
    @classmethod
    def _check_ts_finite(cls, v: list[float]) -> list[float]:
        if not all(np.isfinite(v)):
            raise ValueError("timestamps 包含 NaN/Inf")
        return v

    @field_validator("temperature")
    @classmethod
    def _check_temp_finite(cls, v: Optional[list[float]]) -> Optional[list[float]]:
        if v is not None and not all(np.isfinite(v)):
            raise ValueError("temperature 包含 NaN/Inf")
        return v

    @model_validator(mode="after")
    def _check_lengths(self) -> "EstimateRequest":
        n = len(self.timestamps)
        if len(self.accel) != n or len(self.gyro) != n:
            raise ValueError("timestamps / accel / gyro 长度必须一致")
        if self.temperature is not None and len(self.temperature) != n:
            raise ValueError("temperature 长度必须与 timestamps 一致")
        return self


@dataclasses.dataclass(frozen=True)
class RuntimeConfig:
    """经过单位换算/轴映射后的运行时配置（供算法层使用）。"""

    accel_scale: float
    gyro_scale: float
    time_scale: float
    axis_perm: tuple[int, int, int]  # 内部轴 <- 输入列索引
    axis_sign: tuple[float, float, float]
    window_seconds: float
    window_overlap: float
    min_static_seconds: float
    gyro_std_thresh: float
    accel_std_thresh: float
    gravity_mag_tol: float
    bridge_max_gap_windows: int
    time_gap_factor: float
    spike_z: float
    temp_min_windows: int
    temp_min_span: float
    drift_min_windows: int
    drift_min_span: float

    @classmethod
    def from_request(cls, req: EstimateRequest) -> "RuntimeConfig":
        u, a, d = req.units, req.axes, req.detection
        mapping = {"x": 0, "y": 1, "z": 2}
        perm = (mapping[a.x[1]], mapping[a.y[1]], mapping[a.z[1]])
        sign = tuple(1.0 if s == "+" else -1.0 for s in (a.x[0], a.y[0], a.z[0]))
        return cls(
            accel_scale=ACCEL_UNIT_TO_MS2[u.accel],
            gyro_scale=GYRO_UNIT_TO_RADS[u.gyro],
            time_scale=TIME_UNIT_TO_S[u.time],
            axis_perm=perm,
            axis_sign=sign,  # type: ignore[arg-type]
            window_seconds=d.window_seconds,
            window_overlap=d.window_overlap,
            min_static_seconds=d.min_static_seconds,
            gyro_std_thresh=d.gyro_std_thresh,
            accel_std_thresh=d.accel_std_thresh,
            gravity_mag_tol=d.gravity_mag_tol,
            bridge_max_gap_windows=d.bridge_max_gap_windows,
            time_gap_factor=d.time_gap_factor,
            spike_z=d.spike_z,
            temp_min_windows=d.temp_min_windows,
            temp_min_span=d.temp_min_span,
            drift_min_windows=d.drift_min_windows,
            drift_min_span=d.drift_min_span,
        )
