"""流式稳健统计量：固定窗口中位数与 MAD（仅使用过去样本）。"""

from __future__ import annotations

import numpy as np

# 标准正态分布下 MAD ≈ 0.6745 * sigma，故 sigma ≈ MAD / 0.6745。
MAD_TO_STD = 1.0 / 0.6744897501960817


class StreamingRobustStats:
    """容量为 ``window_size`` 的环形缓冲区，保存最近的过去观测值。

    因果性保证
    ----------
    - :meth:`add` 只在对当前样本做出判决之后由调用方执行；
    - 统计量在任意时刻只反映已经 :meth:`add` 进来的历史样本，
      当前样本与未来样本均不在其中；
    - NaN/inf 等缺样由调用方决定策略，本类本身拒绝插入非有限值。
    """

    __slots__ = ("_buf", "_capacity", "_pos", "_size")

    def __init__(self, window_size: int):
        if not isinstance(window_size, (int, np.integer)) or window_size <= 0:
            raise ValueError(f"window_size 必须为正整数，实际为 {window_size!r}")
        self._capacity = int(window_size)
        self._buf = np.empty(self._capacity, dtype=np.float64)
        self._pos = 0  # 下一个写入位置
        self._size = 0  # 已存样本数（达到容量后保持不变）

    @property
    def size(self) -> int:
        return self._size

    @property
    def capacity(self) -> int:
        return self._capacity

    def add(self, value: float) -> None:
        """插入一个**已经判决过**的有限历史样本，超出容量则淘汰最旧样本。"""
        v = float(value)
        if not np.isfinite(v):
            raise ValueError("统计窗口只接受有限值；缺样（NaN/inf）应由检测器策略处理")
        self._buf[self._pos] = v
        self._pos = (self._pos + 1) % self._capacity
        self._size = min(self._size + 1, self._capacity)

    def values(self) -> np.ndarray:
        """按时间从旧到新返回窗口内容的副本。"""
        if self._size < self._capacity:
            return self._buf[: self._size].copy()
        # 环形回绕：_pos 指向最旧样本。
        return np.concatenate((self._buf[self._pos :], self._buf[: self._pos]))

    def median(self) -> float:
        """窗口中位数；窗口为空时返回 NaN。"""
        if self._size == 0:
            return float("nan")
        return float(np.median(self.values()))

    def mad(self) -> float:
        """中位数绝对偏差 MAD = median(|x - median(x)|)；窗口为空时返回 NaN。"""
        if self._size == 0:
            return float("nan")
        vals = self.values()
        return float(np.median(np.abs(vals - np.median(vals))))

    def scale(self, min_scale: float = 0.0) -> float:
        """稳健尺度估计 MAD/0.6745，并施加绝对下限 ``min_scale``。

        窗口为空时返回 NaN。``min_scale`` 用于常量信号（MAD=0）时避免
        阈值塌缩为 0，属于显式配置的绝对量纲下限（见 README）。
        """
        if self._size == 0:
            return float("nan")
        if min_scale < 0:
            raise ValueError(f"min_scale 不能为负，实际为 {min_scale}")
        return max(self.mad() * MAD_TO_STD, float(min_scale))
