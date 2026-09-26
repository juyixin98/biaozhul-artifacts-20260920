"""单条边上的变换时间序列：按时间排序存储，支持带覆盖检查的插值。

核心约束：查询时刻必须落在 [最早样本, 最晚样本] 区间内，否则抛出
ExtrapolationError —— 绝不把最新（或最早）样本当作区间外时刻的值。
"""

from __future__ import annotations

import bisect

import numpy as np

from . import quaternion as quat
from .exceptions import ExtrapolationError
from .transform import Transform

# 容忍浮点舍入造成的时间边界误差
_TIME_EPS = 1e-9


class TimeSeries:
    """(时间, Transform) 的升序序列。"""

    def __init__(self) -> None:
        self._times: list[float] = []
        self._transforms: list[Transform] = []

    def __len__(self) -> int:
        return len(self._times)

    @property
    def t_min(self) -> float:
        if not self._times:
            raise ExtrapolationError("时间序列为空，没有任何样本")
        return self._times[0]

    @property
    def t_max(self) -> float:
        if not self._times:
            raise ExtrapolationError("时间序列为空，没有任何样本")
        return self._times[-1]

    def insert(self, time: float, transform: Transform) -> None:
        """按时间升序插入样本；同一时刻重复插入会覆盖旧值。"""
        time = float(time)
        if not np.isfinite(time):
            raise ValueError(f"时间必须为有限数值，得到 {time}")
        idx = bisect.bisect_left(self._times, time)
        if idx < len(self._times) and abs(self._times[idx] - time) <= _TIME_EPS:
            self._times[idx] = time
            self._transforms[idx] = transform
            return
        self._times.insert(idx, time)
        self._transforms.insert(idx, transform)

    def interpolate(self, time: float) -> Transform:
        """在指定时刻插值；超出覆盖范围抛 ExtrapolationError。

        平移使用线性插值，旋转使用球面线性插值（slerp）。
        """
        time = float(time)
        if len(self._times) == 0:
            raise ExtrapolationError("时间序列为空，无法插值")
        if time < self._times[0] - _TIME_EPS:
            raise ExtrapolationError(
                f"查询时刻 {time} 早于最早样本 {self._times[0]}，禁止外推"
            )
        if time > self._times[-1] + _TIME_EPS:
            raise ExtrapolationError(
                f"查询时刻 {time} 晚于最晚样本 {self._times[-1]}，禁止外推"
            )
        idx = bisect.bisect_left(self._times, time)
        if idx < len(self._times) and abs(self._times[idx] - time) <= _TIME_EPS:
            return self._transforms[idx]
        # idx 指向第一个大于 time 的样本，time 落在 (idx-1, idx) 之间
        t0, t1 = self._times[idx - 1], self._times[idx]
        u = (time - t0) / (t1 - t0)
        a, b = self._transforms[idx - 1], self._transforms[idx]
        translation = (1.0 - u) * a.translation + u * b.translation
        rotation = quat.slerp(a.rotation, b.rotation, u)
        return Transform(translation, rotation)
