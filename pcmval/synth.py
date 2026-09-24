"""合成测试信号(浮点振幅,域 [-1.0, 1.0))。"""

from __future__ import annotations

import numpy as np

_FULL_SCALE_NEG = -1.0
_FULL_SCALE_POS_16 = 32767 / 32768
_FULL_SCALE_POS_24 = 8388607 / 8388608


def sine(freq: float, sample_rate: int, duration: float, amplitude: float = 0.5) -> np.ndarray:
    """正弦波。"""
    n = int(round(sample_rate * duration))
    t = np.arange(n, dtype=np.float64) / sample_rate
    return amplitude * np.sin(2.0 * np.pi * freq * t)


def silence(sample_rate: int, duration: float) -> np.ndarray:
    """数字静音(全零)。"""
    return np.zeros(int(round(sample_rate * duration)), dtype=np.float64)


def full_scale(bits: int, n: int = 8) -> np.ndarray:
    """满幅极值序列 [-1, +max, -1, +max, ...],用于量化边界验证。"""
    pos = _FULL_SCALE_POS_16 if bits == 16 else _FULL_SCALE_POS_24
    return np.where(np.arange(n) % 2 == 0, _FULL_SCALE_NEG, pos)


def ramp(bits: int) -> np.ndarray:
    """覆盖全部关键量化点的斜坡:整数域 [-2**(b-1), 2**(b-1)-1] 均匀取 64 点。"""
    half = 1 << (bits - 1)
    ints = np.linspace(-half, half - 1, 64).round().astype(np.int64)
    return ints.astype(np.float64) / half


def make(kind: str, sample_rate: int, duration: float, bits: int, amplitude: float = 0.5) -> np.ndarray:
    """按名称生成信号:sine | silence | fullscale | ramp。"""
    if kind == "sine":
        return sine(440.0, sample_rate, duration, amplitude)
    if kind == "silence":
        return silence(sample_rate, duration)
    if kind == "fullscale":
        return full_scale(bits, n=max(8, int(round(sample_rate * duration))))
    if kind == "ramp":
        return ramp(bits)
    raise ValueError(f"unknown signal kind {kind!r}; expected sine|silence|fullscale|ramp")
