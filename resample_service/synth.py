"""合成测试信号生成。"""

from __future__ import annotations

import numpy as np


def sine(fs: float, freq: float, duration: float, amplitude: float = 1.0,
         phase: float = 0.0) -> np.ndarray:
    n = int(round(fs * duration))
    t = np.arange(n) / fs
    return amplitude * np.sin(2.0 * np.pi * freq * t + phase)


def impulse(n: int, index: int | None = None, amplitude: float = 1.0) -> np.ndarray:
    x = np.zeros(int(n))
    if index is None:
        index = n // 2
    x[int(index)] = amplitude
    return x


def multitone(fs: float, components, duration: float) -> np.ndarray:
    """components: [(freq, amplitude), ...]"""
    n = int(round(fs * duration))
    t = np.arange(n) / fs
    x = np.zeros(n)
    for freq, amp in components:
        x += amp * np.sin(2.0 * np.pi * freq * t)
    return x


def chirp(fs: float, f0: float, f1: float, duration: float,
          amplitude: float = 1.0) -> np.ndarray:
    """线性扫频。"""
    n = int(round(fs * duration))
    t = np.arange(n) / fs
    k = (f1 - f0) / duration
    return amplitude * np.sin(2.0 * np.pi * (f0 * t + 0.5 * k * t * t))


def noise(n: int, seed: int = 0, amplitude: float = 1.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return amplitude * rng.standard_normal(int(n))
