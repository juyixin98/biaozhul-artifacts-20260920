"""带时间戳的变换序列：关键帧、严格覆盖的插值与缺口检查。

时间语义（重要）：
- 查询时刻必须落在 [首帧, 末帧] 闭区间内，超出一律抛 TimeNotCoveredError，
  即绝不外推、绝不把最新值默认当作更早时刻的历史值；
- 时刻位于两帧之间时做插值：平移线性插值，旋转用单位四元数 slerp；
- 若两帧时间间隔超过 max_gap（如机器人数据断流），抛 TimeGapError。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import DuplicateTimestampError, InvalidKeyframeError, TimeGapError, TimeNotCoveredError
from .transform import Transform, quaternion_slerp


@dataclass(frozen=True)
class Keyframe:
    """一个时间戳上的位姿采样。"""

    timestamp: float
    transform: Transform


class TimedTransformSequence:
    """按时间升序排列的刚体变换关键帧序列（不可变内容语义）。"""

    def __init__(self, keyframes: list[Keyframe], max_gap: float | None = None):
        if not keyframes:
            raise InvalidKeyframeError("时间序列至少需要一个关键帧")
        stamps = [kf.timestamp for kf in keyframes]
        if any(not np.isfinite(t) for t in stamps):
            raise InvalidKeyframeError("时间戳必须是有限数值")
        if any(stamps[i] > stamps[i + 1] for i in range(len(stamps) - 1)):
            raise InvalidKeyframeError("关键帧必须按时间戳升序排列")
        if any(stamps[i] == stamps[i + 1] for i in range(len(stamps) - 1)):
            dup = next(stamps[i] for i in range(len(stamps) - 1) if stamps[i] == stamps[i + 1])
            raise DuplicateTimestampError(f"时间戳 {dup} 在同一序列中重复出现")
        if max_gap is not None and max_gap <= 0.0:
            raise InvalidKeyframeError("max_gap 必须为正数")
        self._keyframes = tuple(keyframes)
        self._timestamps = np.asarray(stamps, dtype=float)
        self._max_gap = None if max_gap is None else float(max_gap)

    @property
    def timestamps(self) -> np.ndarray:
        return self._timestamps

    @property
    def start_time(self) -> float:
        return float(self._timestamps[0])

    @property
    def end_time(self) -> float:
        return float(self._timestamps[-1])

    @property
    def max_gap(self) -> float | None:
        return self._max_gap

    def covers(self, timestamp: float, tol: float = 1e-12) -> bool:
        """时刻是否落在覆盖范围内（端点含容差）。"""
        return bool(self.start_time - tol <= timestamp <= self.end_time + tol)

    def lookup(self, timestamp: float, max_gap: float | None = None) -> Transform:
        """返回指定时刻的变换；越界不外推，间隔过大报缺口。

        max_gap 参数优先于构造参数；二者皆 None 时不检查缺口。
        """
        if not np.isfinite(timestamp):
            raise InvalidKeyframeError("查询时间戳必须是有限数值")
        tol = 1e-12
        if timestamp < self.start_time - tol or timestamp > self.end_time + tol:
            raise TimeNotCoveredError(
                f"查询时刻 {timestamp} 超出该边的覆盖范围 "
                f"[{self.start_time}, {self.end_time}]：不允许外推"
            )
        # 命中关键帧直接返回。
        idx = int(np.searchsorted(self._timestamps, timestamp, side="left"))
        if idx < len(self._keyframes) and np.isclose(self._timestamps[idx], timestamp, atol=tol):
            return self._keyframes[idx].transform
        # 此时一定有 idx-1 与 idx 两帧（时间在范围内且未命中）。
        kf0 = self._keyframes[idx - 1]
        kf1 = self._keyframes[idx]
        gap = kf1.timestamp - kf0.timestamp
        effective_gap = max_gap if max_gap is not None else self._max_gap
        if effective_gap is not None and gap > effective_gap + tol:
            raise TimeGapError(
                f"时刻 {timestamp} 位于关键帧 {kf0.timestamp} 与 {kf1.timestamp} 之间，"
                f"间隔 {gap:g} 超过允许的最大插值缺口 {effective_gap:g}"
            )
        alpha = (timestamp - kf0.timestamp) / gap
        return interpolate(kf0.transform, kf1.transform, alpha)


class StaticTransformProvider:
    """静态（不随时间变化）变换，需在数据中显式声明。"""

    def __init__(self, transform: Transform):
        self._transform = transform

    @property
    def transform(self) -> Transform:
        return self._transform

    def lookup(self, timestamp: float, max_gap: float | None = None) -> Transform:
        if not np.isfinite(timestamp):
            raise InvalidKeyframeError("查询时间戳必须是有限数值")
        return self._transform


def interpolate(t0: Transform, t1: Transform, alpha: float) -> Transform:
    """刚体变换插值：平移线性，旋转四元数 slerp。alpha∈[0,1]。"""
    if not 0.0 <= alpha <= 1.0:
        raise InvalidKeyframeError(f"插值系数 alpha 必须在 [0,1] 内，实际为 {alpha}")
    translation = (1.0 - alpha) * t0.translation + alpha * t1.translation
    q = quaternion_slerp(t0.to_quaternion(), t1.to_quaternion(), alpha)
    return Transform.from_quaternion(q, translation)
