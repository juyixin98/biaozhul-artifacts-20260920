"""机器人参数与校验。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RobotParams:
    """差速机器人里程计参数。

    Attributes:
        wheel_diameter_m: 轮径（米），左右轮相同。
        track_width_m: 两轮中心距（轴距，米）。
        ticks_per_rev: 每转编码器计数数（已含减速比与倍频）。
        encoder_min: 计数器下限（含）。
        encoder_max: 计数器上限（含），计数在 [min, max] 内回绕。
        max_linear_velocity_mps: 物理可达最大线速度，用于跳变诊断。
        max_angular_velocity_rps: 物理可达最大角速度，用于跳变诊断。
        max_sample_period_s: 相邻采样最大合理间隔，超过则判为丢样。
    """

    wheel_diameter_m: float
    track_width_m: float
    ticks_per_rev: int
    encoder_min: int = 0
    encoder_max: int = 65535
    max_linear_velocity_mps: float = 3.0
    max_angular_velocity_rps: float = 12.0
    max_sample_period_s: float = 0.5

    def __post_init__(self) -> None:
        if self.wheel_diameter_m <= 0:
            raise ValueError("wheel_diameter_m 必须为正")
        if self.track_width_m <= 0:
            raise ValueError("track_width_m 必须为正")
        if self.ticks_per_rev <= 0:
            raise ValueError("ticks_per_rev 必须为正")
        if self.encoder_max <= self.encoder_min:
            raise ValueError("encoder_max 必须大于 encoder_min")
        if self.max_linear_velocity_mps <= 0:
            raise ValueError("max_linear_velocity_mps 必须为正")
        if self.max_angular_velocity_rps <= 0:
            raise ValueError("max_angular_velocity_rps 必须为正")
        if self.max_sample_period_s <= 0:
            raise ValueError("max_sample_period_s 必须为正")

    @property
    def encoder_range(self) -> int:
        """计数器模值（回绕周期）。"""
        return self.encoder_max - self.encoder_min + 1

    @property
    def meters_per_tick(self) -> float:
        """单个计数对应的轮面线位移（米）。"""
        import math

        return math.pi * self.wheel_diameter_m / self.ticks_per_rev

    @classmethod
    def from_dict(cls, data: dict) -> "RobotParams":
        """从 JSON 字典构造，未知键报错以尽早暴露配置错误。"""
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"未知机器人参数键: {sorted(unknown)}")
        return cls(**data)
