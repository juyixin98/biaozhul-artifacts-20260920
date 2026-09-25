"""合成测试信号生成器（离线服务输入）。"""

from __future__ import annotations

import numpy as np


def impulse(n: int, index: int | None = None, amplitude: float = 1.0) -> np.ndarray:
    """单位脉冲，默认位于序列中点。"""
    if n < 1:
        raise ValueError("n must be >= 1")
    if index is None:
        index = n // 2
    if not 0 <= index < n:
        raise ValueError("index out of range")
    x = np.zeros(n, dtype=np.float64)
    x[index] = amplitude
    return x


def sine(
    freq: float,
    fs: float,
    duration: float,
    amplitude: float = 1.0,
    phase: float = 0.0,
) -> np.ndarray:
    """正弦信号。freq 单位 Hz，duration 单位秒。"""
    n = int(round(fs * duration))
    t = np.arange(n, dtype=np.float64) / fs
    return amplitude * np.sin(2.0 * np.pi * freq * t + phase)


def multitone(
    freqs: list[float],
    fs: float,
    duration: float,
    amplitude: float = 1.0,
) -> np.ndarray:
    """多音叠加，各分量等幅，总峰值按分量数归一。"""
    if not freqs:
        raise ValueError("freqs must be non-empty")
    n = int(round(fs * duration))
    t = np.arange(n, dtype=np.float64) / fs
    x = np.zeros(n, dtype=np.float64)
    for f in freqs:
        x += np.sin(2.0 * np.pi * f * t)
    return amplitude / len(freqs) * x


def noise(n: int, seed: int = 0, amplitude: float = 1.0) -> np.ndarray:
    """确定性白噪声（固定种子，便于复现）。"""
    rng = np.random.default_rng(seed)
    return amplitude * rng.standard_normal(n)


def make_signal(spec: dict) -> tuple[np.ndarray, float]:
    """按请求字典生成信号。返回 (x, fs)。

    spec 示例::
        {"type": "sine", "freq": 1000, "fs": 48000, "duration": 0.1}
        {"type": "impulse", "n": 512, "fs": 48000}
        {"type": "noise", "n": 4096, "seed": 1, "fs": 48000}
        {"type": "multitone", "freqs": [1000, 3000], "fs": 48000, "duration": 0.1}
    """
    kind = spec.get("type")
    fs = float(spec.get("fs", 48000.0))
    amp = float(spec.get("amplitude", 1.0))
    if kind == "sine":
        return sine(spec["freq"], fs, spec.get("duration", 1.0), amp), fs
    if kind == "impulse":
        return impulse(int(spec.get("n", 512)), spec.get("index"), amp), fs
    if kind == "multitone":
        return multitone(list(spec["freqs"]), fs, spec.get("duration", 1.0), amp), fs
    if kind == "noise":
        return noise(int(spec.get("n", 4096)), int(spec.get("seed", 0)), amp), fs
    raise ValueError(f"unknown signal type: {kind!r}")
