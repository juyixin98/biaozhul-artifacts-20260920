"""合成轨迹与传感器数据生成(仅用于离线测试与演示, 不连接硬件).

场景: 机器人沿平面轨迹运动, 两个传感器(如轮式里程计与 IMU 积分)
以不同频率、带噪声地对同一轨迹采样, B 时钟可带固定偏移,
到达顺序可局部打乱, 可随机丢帧.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from sensor_pairing.models import SensorMessage


def ground_truth_position(t: np.ndarray) -> np.ndarray:
    """真值轨迹: 缓转弯道的平面位置, 返回形状 (N, 2)."""
    t = np.asarray(t, dtype=np.float64)
    x = t
    y = np.sin(0.5 * t)
    return np.column_stack([x, y])


@dataclass
class SyntheticStreams:
    """一对合成传感器流及其生成参数."""

    stream_a: list[SensorMessage]  # 按到达顺序
    stream_b: list[SensorMessage]  # 按到达顺序
    clock_offset_b: float
    seed: int


def _sample_stream(
    rng: np.random.Generator,
    prefix: str,
    rate_hz: float,
    duration: float,
    noise_std: float,
    time_jitter_std: float,
    clock_offset: float,
    drop_prob: float,
) -> list[SensorMessage]:
    n = int(rate_hz * duration)
    t = np.arange(n, dtype=np.float64) / rate_hz
    t = t + rng.normal(0.0, time_jitter_std, size=n) + clock_offset
    pos = ground_truth_position(t - clock_offset)
    pos = pos + rng.normal(0.0, noise_std, size=pos.shape)
    keep = rng.random(n) >= drop_prob
    return [
        SensorMessage(
            msg_id=f"{prefix}{i}",
            timestamp=float(t[i]),
            data={"x": float(pos[i, 0]), "y": float(pos[i, 1])},
        )
        for i in range(n)
        if keep[i]
    ]


def _shuffle_within_window(
    rng: np.random.Generator,
    messages: list[SensorMessage],
    window: int,
) -> list[SensorMessage]:
    """在长度为 window 的滑动块内打乱顺序, 模拟网络乱序到达."""
    if window <= 1:
        return list(messages)
    out: list[SensorMessage] = []
    for start in range(0, len(messages), window):
        block = list(messages[start : start + window])
        perm = rng.permutation(len(block))
        out.extend(block[int(j)] for j in perm)
    return out


def generate_synthetic_streams(
    duration: float = 10.0,
    rate_a_hz: float = 50.0,
    rate_b_hz: float = 30.0,
    clock_offset_b: float = 0.0,
    noise_std: float = 0.01,
    time_jitter_std: float = 0.002,
    drop_prob: float = 0.0,
    disorder_window: int = 1,
    seed: int = 42,
) -> SyntheticStreams:
    """生成两路合成传感器流(到达顺序).

    Args:
        duration: 轨迹时长(秒).
        rate_a_hz / rate_b_hz: 两路采样频率.
        clock_offset_b: B 时钟相对 A 的领先量(秒).
        noise_std: 位置观测噪声标准差(米).
        time_jitter_std: 时间戳抖动标准差(秒).
        drop_prob: 每条消息被丢弃的概率.
        disorder_window: 乱序窗口大小(1 表示严格有序).
        seed: 随机种子(可复现).
    """
    rng = np.random.default_rng(seed)
    stream_a = _sample_stream(
        rng, "a", rate_a_hz, duration, noise_std, time_jitter_std, 0.0, drop_prob
    )
    stream_b = _sample_stream(
        rng,
        "b",
        rate_b_hz,
        duration,
        noise_std,
        time_jitter_std,
        clock_offset_b,
        drop_prob,
    )
    stream_a = _shuffle_within_window(rng, stream_a, disorder_window)
    stream_b = _shuffle_within_window(rng, stream_b, disorder_window)
    return SyntheticStreams(
        stream_a=stream_a,
        stream_b=stream_b,
        clock_offset_b=clock_offset_b,
        seed=seed,
    )


def interleave_by_arrival(
    stream_a: list[SensorMessage],
    stream_b: list[SensorMessage],
    seed: int = 0,
) -> list[tuple[str, SensorMessage]]:
    """把两路到达序列合并成单一到达序列(随机交织, 保持各路内部顺序)."""
    rng = np.random.default_rng(seed)
    merged: list[tuple[str, SensorMessage]] = [("a", m) for m in stream_a]
    merged += [("b", m) for m in stream_b]
    perm = rng.permutation(len(merged))
    shuffled = [merged[int(i)] for i in perm]
    # 保持每路内部到达顺序的稳定重排: 按原路内序号归并.
    out: list[tuple[str, SensorMessage]] = []
    ia = ib = 0
    for stream, _ in shuffled:
        if stream == "a":
            out.append(("a", stream_a[ia]))
            ia += 1
        else:
            out.append(("b", stream_b[ib]))
            ib += 1
    return out
