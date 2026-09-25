"""单通道流式 FIR 滤波器(带延迟线状态)。

约定:
- 所有计算使用 float64。
- 初始状态为零(等价于输入前补零)。
- process() 支持任意块长,包括 0(空块):空块返回形状 (0,) 的数组,
  不读取也不修改任何内部状态。
- 连续多块处理的结果与一次性整段处理完全一致(状态在块间精确保持)。
"""

from __future__ import annotations

import numpy as np


class StreamingFIR:
    """单通道 FIR 滤波器,内部维护长度 M-1 的延迟线。"""

    def __init__(self, coeffs, state: np.ndarray | None = None):
        h = np.asarray(coeffs, dtype=np.float64).ravel()
        if h.size == 0:
            raise ValueError("FIR 系数不能为空")
        self._h = h
        m1 = h.size - 1
        if state is None:
            self._state = np.zeros(m1, dtype=np.float64)
        else:
            s = np.asarray(state, dtype=np.float64).ravel()
            if s.size != m1:
                raise ValueError(
                    f"初始状态长度必须为 M-1={m1},实际为 {s.size}"
                )
            self._state = s.copy()

    @property
    def coeffs(self) -> np.ndarray:
        return self._h

    @property
    def order(self) -> int:
        """滤波器阶数 M-1。"""
        return self._h.size - 1

    @property
    def state(self) -> np.ndarray:
        """延迟线副本:最近 order 个输入样本,按时间顺序(最旧在前)。"""
        return self._state.copy()

    def prime(self, history: np.ndarray) -> None:
        """用输入历史填充延迟线(用于无瞬态滤波器切换)。

        history 为“当前样本之前的输入”,按时间顺序排列;
        若长度超过 order,取末尾 order 个;不足则前面补零。
        """
        hist = np.asarray(history, dtype=np.float64).ravel()
        m1 = self.order
        if hist.size >= m1:
            self._state = hist[hist.size - m1:].copy()
        else:
            self._state = np.concatenate([np.zeros(m1 - hist.size), hist])

    def process(self, x: np.ndarray) -> np.ndarray:
        """处理一块输入,返回等长输出;空块不改变状态。"""
        x = np.asarray(x, dtype=np.float64).ravel()
        n = x.size
        if n == 0:
            return np.empty(0, dtype=np.float64)
        ext = np.concatenate([self._state, x])
        y = np.convolve(ext, self._h, mode="valid")
        # ext 的最后 order 个样本即新的延迟线(时间顺序)
        self._state = ext[n:]
        return y
