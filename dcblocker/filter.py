"""一阶高通 DC 去偏置滤波器（流式、因果）。

模型（明确的一阶高通，差分方程）：

    y[n] = x[n] - x[n-1] + R * y[n-1]

其中 R = exp(-2 * pi * fc / fs)，fc 为截止频率，fs 为采样率。

该滤波器只依赖当前与历史样本（x[n-1]、y[n-1] 两个状态），
绝不使用未来样本，因此天然满足"分块不变性"。

实现说明：递推按逐样本标量顺序计算（不使用会改变浮点求和
顺序的向量化累加），因此同一条连续流无论切成多大的块依次
送入，输出逐样本严格一致（逐位相等，不是近似相等）。
"""

from __future__ import annotations

import math

import numpy as np

DEFAULT_CUTOFF_HZ = 5.0


def compute_coefficient(sample_rate: float, cutoff_hz: float) -> float:
    """由采样率与截止频率计算一阶高通系数 R = exp(-2*pi*fc/fs)。"""
    _validate_params(sample_rate, cutoff_hz)
    return math.exp(-2.0 * math.pi * cutoff_hz / sample_rate)


def _validate_params(sample_rate: float, cutoff_hz: float) -> None:
    if not math.isfinite(sample_rate) or sample_rate <= 0:
        raise ValueError(f"sample_rate 必须为正数，得到 {sample_rate!r}")
    if not math.isfinite(cutoff_hz) or cutoff_hz <= 0:
        raise ValueError(f"cutoff_hz 必须为正数，得到 {cutoff_hz!r}")
    if cutoff_hz >= sample_rate / 2:
        raise ValueError(
            f"cutoff_hz ({cutoff_hz}) 必须小于奈奎斯特频率 ({sample_rate / 2})"
        )


class DCBlocker:
    """持续流 DC 偏置估计与去除器。

    用法：
        blocker = DCBlocker(sample_rate=48000, cutoff_hz=5.0)
        y1 = blocker.process(block1)
        y2 = blocker.process(block2)   # 状态自动延续
        blocker.reset()                # 清空状态
        blocker.set_sample_rate(44100) # 重设采样率
    """

    def __init__(self, sample_rate: float, cutoff_hz: float = DEFAULT_CUTOFF_HZ):
        _validate_params(sample_rate, cutoff_hz)
        self._sample_rate = float(sample_rate)
        self._cutoff_hz = float(cutoff_hz)
        self._r = compute_coefficient(self._sample_rate, self._cutoff_hz)
        self.reset()

    # ---- 配置 ----

    @property
    def sample_rate(self) -> float:
        return self._sample_rate

    @property
    def cutoff_hz(self) -> float:
        return self._cutoff_hz

    @property
    def coefficient(self) -> float:
        """一阶高通系数 R。"""
        return self._r

    def set_sample_rate(self, sample_rate: float, reset_state: bool = False) -> None:
        """重设采样率并按新采样率重算系数 R。

        reset_state=True 时同时清空滤波器状态；默认保留状态，
        便于在不停流的情况下切换采样率。
        """
        _validate_params(sample_rate, self._cutoff_hz)
        self._sample_rate = float(sample_rate)
        self._r = compute_coefficient(self._sample_rate, self._cutoff_hz)
        if reset_state:
            self.reset()

    def set_cutoff(self, cutoff_hz: float, reset_state: bool = False) -> None:
        """重设截止频率并重算系数 R。"""
        _validate_params(self._sample_rate, cutoff_hz)
        self._cutoff_hz = float(cutoff_hz)
        self._r = compute_coefficient(self._sample_rate, self._cutoff_hz)
        if reset_state:
            self.reset()

    # ---- 状态 ----

    def reset(self) -> None:
        """清空滤波器状态（上一输入样本与上一输出样本）。"""
        self._x_prev = 0.0
        self._y_prev = 0.0

    # ---- 处理 ----

    def process(self, block) -> np.ndarray:
        """处理一个样本块，返回去偏置后的新数组（不修改入参）。

        block 可以是任意长度（含 0、1、2 的极短块）的一维类数组。
        输出与"把全部历史一次性送入"的结果逐样本严格一致（分块不变性）。
        """
        x = np.asarray(block, dtype=np.float64)
        if x.ndim != 1:
            raise ValueError(f"block 必须是一维数组，得到维度 {x.ndim}")
        if x.size == 0:
            return np.empty(0, dtype=np.float64)

        # u[n] = x[n] - x[n-1]，x[-1] 取跨块保存的状态；
        # 逐点减法无累加，结果与分块方式无关
        u = np.empty_like(x)
        u[0] = x[0] - self._x_prev
        if x.size > 1:
            np.subtract(x[1:], x[:-1], out=u[1:])

        # y[n] = u[n] + R * y[n-1]，逐样本标量递推，
        # 保证任意分块下浮点运算顺序完全一致
        y = np.empty_like(x)
        y_prev = self._y_prev
        r = self._r
        for n in range(x.size):
            y_prev = u[n] + r * y_prev
            y[n] = y_prev

        self._x_prev = float(x[-1])
        self._y_prev = float(y_prev)
        return y
