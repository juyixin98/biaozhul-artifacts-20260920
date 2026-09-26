"""合成编码器数据生成器（仅用于测试与示例，不接触真实硬件）。

按分段匀速运动指令 (v, omega, duration) 仿真理想编码器输出：
左右轮线速度 v_l = v - omega*L/2, v_r = v + omega*L/2，
累积为计数读数（整数化并按模值回绕）。
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from .config import RobotParams

# 运动段: (线速度 m/s, 角速度 rad/s, 持续时间 s)
Segment = Tuple[float, float, float]


def simulate_samples(
    params: RobotParams,
    segments: Sequence[Segment],
    dt: float,
    t0: float = 0.0,
    initial_left: int = 0,
    initial_right: int = 0,
) -> List[Dict[str, float]]:
    """生成合成采样序列。

    Args:
        params: 机器人参数（模值非 None 时计数按模回绕）。
        segments: 运动段列表，逐段首尾相接。
        dt: 采样周期 (s)。
        t0: 起始时间 (s)。
        initial_left / initial_right: 起始计数（用于构造回绕场景）。

    Returns:
        [{"t": ..., "left": ..., "right": ...}, ...]，含起始样本。
    """
    if dt <= 0:
        raise ValueError("dt 必须为正数")

    meters_per_tick_left = 3.141592653589793 * params.wheel_diameter_left / (
        params.ticks_per_revolution
    )
    meters_per_tick_right = 3.141592653589793 * params.wheel_diameter_right / (
        params.ticks_per_revolution
    )

    # 以浮点累积真实位置对应的计数，取整后作为读数，模拟量化
    exact_left = float(initial_left)
    exact_right = float(initial_right)
    samples: List[Dict[str, float]] = [
        {
            "t": t0,
            "left": _to_reading(exact_left, params.encoder_modulus),
            "right": _to_reading(exact_right, params.encoder_modulus),
        }
    ]

    t = t0
    half_track = params.track_width / 2.0
    for linear_v, angular_v, duration in segments:
        steps = round(duration / dt)
        if steps < 1:
            raise ValueError(f"运动段时长 {duration} 不足一个采样周期 {dt}")
        v_left = linear_v - angular_v * half_track
        v_right = linear_v + angular_v * half_track
        for _ in range(steps):
            exact_left += v_left * dt / meters_per_tick_left
            exact_right += v_right * dt / meters_per_tick_right
            t += dt
            samples.append(
                {
                    "t": t,
                    "left": _to_reading(exact_left, params.encoder_modulus),
                    "right": _to_reading(exact_right, params.encoder_modulus),
                }
            )
    return samples


def _to_reading(exact_count: float, modulus) -> int:
    reading = int(round(exact_count))
    if modulus is not None:
        reading %= modulus
    return reading
