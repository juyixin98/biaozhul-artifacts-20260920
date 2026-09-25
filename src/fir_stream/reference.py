"""独立参考实现:用于验收比对。

reference_full_convolution 直接调用 np.convolve(mode="full"),
作为“整段卷积参考”。

LayeredReferenceFIR 是引擎的逐样本、逐层独立重写(不 import 引擎
内部类),与引擎共享同一套数学约定:
  - 每层是以输入历史 prime 的 FIR;
  - 切换时旧层在 fade_len 个样本内线性淡出、新层线性淡入,
    第 j 个渐变样本(0 基)上,新层增益 (j+1)/L,旧层按比例衰减,
    所有层增益之和恒为 1;
  - 空块不推进任何状态。
它故意写得直接(逐样本循环),与引擎的向量化实现互相印证。
"""

from __future__ import annotations

import numpy as np


def reference_full_convolution(x: np.ndarray, h: np.ndarray) -> np.ndarray:
    """整段卷积参考:y = x * h(全长,含尾部)。"""
    return np.convolve(
        np.asarray(x, dtype=np.float64).ravel(),
        np.asarray(h, dtype=np.float64).ravel(),
        mode="full",
    )


class _RefLayer:
    def __init__(self, coeffs, history, gain, target, remaining):
        self.h = np.asarray(coeffs, dtype=np.float64).ravel()
        m1 = self.h.size - 1
        hist = np.asarray(history, dtype=np.float64).ravel()
        if hist.size >= m1:
            self.state = hist[hist.size - m1:][::-1].copy()
        else:
            self.state = np.concatenate([hist[::-1], np.zeros(m1 - hist.size)])
        self.gain = float(gain)
        self.target = float(target)
        self.remaining = int(remaining)

    def step(self, x):
        """处理一个样本,返回该样本的滤波输出(不含增益)。"""
        m1 = self.h.size - 1
        y = self.h[0] * x + float(np.dot(self.h[1:], self.state[:m1]))
        if m1 > 0:
            self.state = np.concatenate([[x], self.state[: m1 - 1]])
        return y

    def gain_step(self):
        """推进一个样本的增益并返回推进后的增益。"""
        if self.remaining > 0:
            self.gain += (self.target - self.gain) / self.remaining
            self.remaining -= 1
        return self.gain


class LayeredReferenceFIR:
    """逐样本参考引擎(单通道或多通道,通道间完全独立)。"""

    def __init__(self, coeffs, history_capacity: int = 8192):
        self._default = np.asarray(coeffs, dtype=np.float64).ravel()
        self._capacity = int(history_capacity)
        self._channels: dict[int, dict] = {}

    def _new_channel(self):
        return {
            "layers": [_RefLayer(self._default, np.zeros(0), 1.0, 1.0, 0)],
            "history": np.zeros(0, dtype=np.float64),
        }

    def switch(self, coeffs, channels=None, fade_len: int = 0) -> None:
        h = np.asarray(coeffs, dtype=np.float64).ravel()
        targets = list(self._channels) if channels is None else list(channels)
        if channels is None:
            self._default = h
        for ch in targets:
            st = self._channels.get(ch)
            if st is None:
                continue
            if fade_len == 0:
                st["layers"] = [_RefLayer(h, st["history"], 1.0, 1.0, 0)]
            else:
                for ly in st["layers"]:
                    ly.target = 0.0
                    ly.remaining = int(fade_len)
                st["layers"].append(_RefLayer(h, st["history"], 0.0, 1.0, int(fade_len)))

    def process(self, block) -> np.ndarray:
        arr = np.asarray(block, dtype=np.float64)
        one_d = arr.ndim == 1
        arr2 = arr.reshape(-1, 1) if one_d else arr
        n, c = arr2.shape
        if n == 0:
            return arr.copy()
        out = np.zeros((n, c), dtype=np.float64)
        for ch in range(c):
            st = self._channels.get(ch)
            if st is None:
                st = self._channels[ch] = self._new_channel()
            for i in range(n):
                x = arr2[i, ch]
                acc = 0.0
                for ly in st["layers"]:
                    acc += ly.gain_step() * ly.step(x)
                out[i, ch] = acc
            st["layers"] = [
                ly for ly in st["layers"]
                if not (ly.remaining == 0 and ly.target == 0.0)
            ]
            h = np.concatenate([st["history"], arr2[:, ch]])
            if h.size > self._capacity:
                h = h[h.size - self._capacity:]
            st["history"] = h
        return out.ravel() if one_d else out

    def drain(self) -> np.ndarray:
        if not self._channels:
            return np.zeros((0, 0))
        c = max(self._channels) + 1
        tail = max(
            (ly.h.size - 1 for st in self._channels.values() for ly in st["layers"]),
            default=0,
        )
        if tail == 0:
            return np.zeros((0, c))
        return self.process(np.zeros((tail, c)))
