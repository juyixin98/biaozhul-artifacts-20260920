"""FIR 系数工具:常用滤波器设计(窗函数法)与内置测试滤波器。"""

from __future__ import annotations

import numpy as np


def lowpass(cutoff: float, num_taps: int, sample_rate: int = 48000) -> np.ndarray:
    """窗函数法低通 FIR(Hamming 窗),cutoff 单位 Hz。"""
    return _windowed_sinc(cutoff, num_taps, sample_rate, pass_zero=True)


def highpass(cutoff: float, num_taps: int, sample_rate: int = 48000) -> np.ndarray:
    """窗函数法高通 FIR(谱反转),num_taps 必须为奇数。"""
    if num_taps % 2 == 0:
        raise ValueError("高通 FIR 的 num_taps 必须为奇数")
    lp = _windowed_sinc(cutoff, num_taps, sample_rate, pass_zero=True)
    h = -lp
    h[num_taps // 2] += 1.0
    return h


def identity() -> np.ndarray:
    """直通滤波器 y[n] = x[n]。"""
    return np.array([1.0])


def delay(k: int) -> np.ndarray:
    """纯延迟滤波器 y[n] = x[n-k]。"""
    h = np.zeros(int(k) + 1)
    h[int(k)] = 1.0
    return h


def _windowed_sinc(cutoff, num_taps, sample_rate, pass_zero=True):
    num_taps = int(num_taps)
    if num_taps < 1:
        raise ValueError("num_taps 必须 >= 1")
    fc = float(cutoff) / float(sample_rate)
    if not 0.0 < fc < 0.5:
        raise ValueError("cutoff 必须满足 0 < cutoff < sample_rate/2")
    n = np.arange(num_taps, dtype=np.float64)
    mid = (num_taps - 1) / 2.0
    h = np.sinc(2.0 * fc * (n - mid)) * 2.0 * fc
    h *= np.hamming(num_taps)
    h /= h.sum()  # 直流增益归一化为 1
    return h


def design(spec: dict, sample_rate: int = 48000) -> np.ndarray:
    """按规格字典设计滤波器,供请求 JSON 使用。

    支持:
      {"kind": "lowpass",  "cutoff": Hz, "num_taps": N}
      {"kind": "highpass", "cutoff": Hz, "num_taps": N(奇数)}
      {"kind": "identity"}
      {"kind": "delay",    "k": K}
      {"kind": "coeffs",   "coeffs": [...]}
    """
    kind = spec.get("kind")
    if kind == "lowpass":
        return lowpass(spec["cutoff"], spec["num_taps"], sample_rate)
    if kind == "highpass":
        return highpass(spec["cutoff"], spec["num_taps"], sample_rate)
    if kind == "identity":
        return identity()
    if kind == "delay":
        return delay(spec["k"])
    if kind == "coeffs":
        return np.asarray(spec["coeffs"], dtype=np.float64)
    raise ValueError(f"未知滤波器规格: {kind!r}")
