"""合成轨迹与传感器数据生成。

全部数据为人工合成，不连接任何硬件：

* :func:`generate_trajectory` 生成一条二维真值轨迹
  （匀速直线运动叠加正弦横向运动）。
* :func:`make_sensor_events` 让一类“传感器”按自己的频率对轨迹采样，
  可配置：时钟恒定偏移、时间戳噪声、传输延迟（导致乱序到达）、
  随机丢包、中断间隙（产生过期未匹配）、同一时间戳的重复消息。
* :func:`calibration_pairs` 生成一小段物理上同时刻的校准点对，
  供时钟偏移估计使用。

生成结果均携带真值时间，便于把在线算法与离线定义逐一对照。
"""

from dataclasses import dataclass
from typing import Any

import numpy as np

from .models import Message


@dataclass(frozen=True)
class Trajectory:
    times: np.ndarray  # (N,) 真值时间
    positions: np.ndarray  # (N, 2) 二维位置

    def position_at(self, t: float) -> np.ndarray:
        """对轨迹做线性插值，返回 t 时刻的二维位置。"""
        x = np.interp(t, self.times, self.positions[:, 0])
        y = np.interp(t, self.times, self.positions[:, 1])
        return np.array([x, y], dtype=float)


@dataclass(frozen=True)
class SensorEvent:
    """一次合成的传感器消息及其生成真值。"""

    message: Message
    true_time: float  # 物理事件的真实时间
    arrival_time: float  # 到达匹配器的（接收）时间，决定推送顺序

    @property
    def raw_timestamp(self) -> float:
        return self.message.timestamp


def generate_trajectory(
    duration: float = 20.0,
    dt: float = 0.005,
    speed: float = 1.0,
    lateral_amp: float = 0.5,
    lateral_freq: float = 0.2,
) -> Trajectory:
    """生成二维真值轨迹：x 匀速推进，y 正弦摆动。"""
    if duration <= 0 or dt <= 0:
        raise ValueError("duration 与 dt 必须为正")
    times = np.arange(0.0, duration, dt)
    x = speed * times
    y = lateral_amp * np.sin(2.0 * np.pi * lateral_freq * times)
    return Trajectory(times=times, positions=np.column_stack([x, y]))


def _in_gap(t: float, gap_intervals: tuple[tuple[float, float], ...]) -> bool:
    return any(lo <= t < hi for lo, hi in gap_intervals)


def make_sensor_events(
    stream: str,
    trajectory: Trajectory,
    rate_hz: float,
    *,
    clock_offset: float = 0.0,
    timestamp_jitter: float = 0.0,
    delay_mean: float = 0.02,
    delay_jitter: float = 0.0,
    drop_prob: float = 0.0,
    gap_intervals: tuple[tuple[float, float], ...] = (),
    duplicate_spec: tuple[int, ...] = (),
    seed: int = 0,
) -> list[SensorEvent]:
    """生成一类传感器的全部事件（已按事件时间排序）。

    Args:
        stream: "a" / "b"，用于生成消息 id 前缀。
        rate_hz: 采样频率（Hz）。
        clock_offset: 该钟相对真值钟的恒定偏移：原始时间戳 = 真值 + 偏移
            + 高斯噪声。A 流通常为 0，B 流设置非零值模拟时钟偏移。
        timestamp_jitter: 时间戳高斯噪声标准差（秒）。
        delay_mean / delay_jitter: 传输延迟均值与高斯抖动标准差；
            ``到达时间 = 真值时间 + 延迟 + 抖动``，抖动足够大时相邻消息
            会乱序到达。
        drop_prob: 每条消息的独立丢弃概率（模拟随机丢包）。
        gap_intervals: 中断的真值时间区间，区间内不产生任何消息。
        duplicate_spec: 需要产生“同一时间戳重复消息”的采样序号，
            例如 (3, 10) 表示第 3、10 个采样各多发一条同时间戳消息，
            重复消息拥有不同 id。
        seed: 随机种子。
    """
    if stream not in ("a", "b"):
        raise ValueError("stream 必须为 'a' 或 'b'")
    if rate_hz <= 0:
        raise ValueError("rate_hz 必须为正")
    if not 0.0 <= drop_prob < 1.0:
        raise ValueError("drop_prob 必须位于 [0, 1)")

    rng = np.random.default_rng(seed)
    period = 1.0 / rate_hz
    duration = float(trajectory.times[-1])
    n_samples = int(np.floor(duration / period)) + 1
    dup_indices = set(duplicate_spec)

    events: list[SensorEvent] = []
    seq = 0
    for sample_idx in range(n_samples):
        t_true = sample_idx * period
        if t_true > duration:
            break
        if _in_gap(t_true, gap_intervals) or rng.random() < drop_prob:
            continue

        raw_ts = t_true + clock_offset + (
            rng.normal(0.0, timestamp_jitter) if timestamp_jitter > 0 else 0.0
        )
        arrival = t_true + delay_mean + (
            rng.normal(0.0, delay_jitter) if delay_jitter > 0 else 0.0
        )
        pos = trajectory.position_at(t_true)
        msg = Message(
            id=f"{stream}{seq:04d}",
            timestamp=float(raw_ts),
            payload={
                "seq": seq,
                "position": [float(pos[0]), float(pos[1])],
                "true_time": float(t_true),
            },
        )
        events.append(SensorEvent(msg, float(t_true), float(arrival)))

        # 同一时间戳的重复消息：时间戳、到达时间完全相同，仅 id 不同
        if sample_idx in dup_indices:
            dup = Message(
                id=f"{stream}{seq:04d}-d1",
                timestamp=msg.timestamp,
                payload=dict(msg.payload) if isinstance(msg.payload, dict) else msg.payload,
            )
            events.append(SensorEvent(dup, float(t_true), float(arrival)))

        seq += 1

    return events


def calibration_pairs(
    trajectory: Trajectory,
    n_points: int = 21,
    *,
    clock_offset_b: float = 0.0,
    timestamp_jitter: float = 0.001,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """生成 n_points 对同时刻校准读数（A 钟读数, B 钟读数）。

    两流在这些真值时刻“恰好同时”各采一帧，时间戳噪声相互独立，
    B 钟带恒定偏移。返回的两个数组可直接喂给
    :func:`sensor_matcher.clock.estimate_offset`。
    """
    if n_points <= 0:
        raise ValueError("n_points 必须为正")
    rng = np.random.default_rng(seed)
    duration = float(trajectory.times[-1])
    truth = np.linspace(0.0, duration, n_points)
    a = truth + rng.normal(0.0, timestamp_jitter, n_points)
    b = truth + clock_offset_b + rng.normal(0.0, timestamp_jitter, n_points)
    return a, b


def arrival_order(
    events_a: list[SensorEvent],
    events_b: list[SensorEvent],
) -> list[tuple[str, SensorEvent]]:
    """把两流事件按到达时间合并为确定性的推送顺序。

    到达时间相同则先 A 后 B，保证可复现。
    """
    merged: list[tuple[float, str, SensorEvent]] = []
    for ev in events_a:
        merged.append((ev.arrival_time, "a", ev))
    for ev in events_b:
        merged.append((ev.arrival_time, "b", ev))
    merged.sort(key=lambda x: (x[0], 0 if x[1] == "a" else 1, x[2].message.id))
    return [(stream, ev) for _t, stream, ev in merged]


def required_out_of_orderness(
    events_a: list[SensorEvent],
    events_b: list[SensorEvent],
    offset_a: float = 0.0,
    offset_b: float = 0.0,
) -> float:
    """根据实际到达顺序计算匹配器所需的乱序有界量 δ。

    定义为：按到达顺序依次到达时，``max(0, 此前观测最大时间戳 - 当前时间戳)``
    的全局最大值，时间戳取**时钟校正后**的统一时基（跨流比较才有意义）。
    取该值时水位线假设恰好成立，在线结果应与离线一致。
    """
    delta = 0.0
    max_seen = float("-inf")
    for stream, ev in arrival_order(events_a, events_b):
        t = ev.message.timestamp + (offset_b if stream == "b" else offset_a)
        if max_seen != float("-inf"):
            delta = max(delta, max_seen - t)
        max_seen = max(max_seen, t)
    # 留一个极小的安全余量吸收浮点比较误差
    return delta + 1e-12


def events_to_messages(
    events: list[SensorEvent],
    clock_offset_correction: float = 0.0,
) -> list[Message]:
    """施加时钟校正，把事件转为统一时基上的消息（id 保持不变）。"""
    from .clock import corrected_time

    return [
        Message(
            id=ev.message.id,
            timestamp=corrected_time(ev.message.timestamp, clock_offset_correction),
            payload=ev.message.payload,
        )
        for ev in events
    ]
