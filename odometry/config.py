"""机器人参数与诊断阈值的配置定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class RobotParams:
    """差速机器人几何与编码器参数。

    Attributes:
        wheel_diameter_left: 左轮直径 (m)。
        wheel_diameter_right: 右轮直径 (m)。
        track_width: 两轮中心间距（轴距）(m)。
        ticks_per_revolution: 每轮每转编码器计数（已含减速比与倍频）。
        encoder_modulus: 编码器计数器模值（如 16 位计数器为 65536）。
            为 None 时认为计数器不回绕，不做回绕修正。
    """

    wheel_diameter_left: float
    wheel_diameter_right: float
    track_width: float
    ticks_per_revolution: float
    encoder_modulus: Optional[int] = None

    def __post_init__(self) -> None:
        if self.wheel_diameter_left <= 0 or self.wheel_diameter_right <= 0:
            raise ValueError("轮径必须为正数")
        if self.track_width <= 0:
            raise ValueError("轴距必须为正数")
        if self.ticks_per_revolution <= 0:
            raise ValueError("每转计数必须为正数")
        if self.encoder_modulus is not None and self.encoder_modulus < 2:
            raise ValueError("计数器模值必须 >= 2")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RobotParams":
        try:
            return cls(
                wheel_diameter_left=float(data["wheel_diameter_left"]),
                wheel_diameter_right=float(data["wheel_diameter_right"]),
                track_width=float(data["track_width"]),
                ticks_per_revolution=float(data["ticks_per_revolution"]),
                encoder_modulus=(
                    None
                    if data.get("encoder_modulus") is None
                    else int(data["encoder_modulus"])
                ),
            )
        except KeyError as exc:
            raise ValueError(f"robot 配置缺少字段: {exc}") from exc


@dataclass(frozen=True)
class DiagnosticThresholds:
    """异常跳变诊断阈值。

    Attributes:
        max_linear_velocity: 单步线速度上限 (m/s)，超过则标记 velocity_spike。
        max_angular_velocity: 单步角速度上限 (rad/s)，超过则标记 angular_spike。
        max_dt: 相邻采样时间间隔上限 (s)，超过则标记 time_gap（疑似丢样）。
    """

    max_linear_velocity: float = 5.0
    max_angular_velocity: float = 20.0
    max_dt: float = 0.5

    def __post_init__(self) -> None:
        if self.max_linear_velocity <= 0:
            raise ValueError("max_linear_velocity 必须为正数")
        if self.max_angular_velocity <= 0:
            raise ValueError("max_angular_velocity 必须为正数")
        if self.max_dt <= 0:
            raise ValueError("max_dt 必须为正数")

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "DiagnosticThresholds":
        if data is None:
            return cls()
        known = {"max_linear_velocity", "max_angular_velocity", "max_dt"}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"diagnostics 配置含未知字段: {sorted(unknown)}")
        return cls(**{k: float(v) for k, v in data.items()})
