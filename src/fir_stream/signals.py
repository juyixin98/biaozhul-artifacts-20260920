"""合成测试信号生成(多通道,确定性,可复现)。"""

from __future__ import annotations

import numpy as np


def generate_signal(kind: str, n: int, channels: int = 1, sample_rate: int = 48000,
                    seed: int = 0, **kwargs) -> np.ndarray:
    """生成 (n, channels) 的 float64 信号;channels==1 时返回 (n,)。"""
    n = int(n)
    channels = int(channels)
    t = np.arange(n, dtype=np.float64) / float(sample_rate)
    rng = np.random.default_rng(seed)

    if kind == "silence":
        x = np.zeros((n, channels))
    elif kind == "impulse":
        x = np.zeros((n, channels))
        at = int(kwargs.get("at", 0))
        if 0 <= at < n:
            x[at, :] = 1.0
    elif kind == "step":
        x = np.zeros((n, channels))
        at = int(kwargs.get("at", n // 2))
        amp = float(kwargs.get("amplitude", 1.0))
        if at < n:
            x[max(at, 0):, :] = amp
    elif kind == "sine":
        freqs = kwargs.get("freqs", [440.0 + 110.0 * ch for ch in range(channels)])
        amp = float(kwargs.get("amplitude", 0.8))
        f = np.asarray(freqs, dtype=np.float64).ravel()
        if f.size != channels:
            raise ValueError("freqs 数量必须等于通道数")
        x = amp * np.sin(2.0 * np.pi * t[:, None] * f[None, :])
    elif kind == "noise":
        amp = float(kwargs.get("amplitude", 0.5))
        x = amp * rng.standard_normal((n, channels))
    elif kind == "mixed":
        # 每通道不同频率的正弦叠加 + 少量噪声,适合突跳检测
        amp = float(kwargs.get("amplitude", 0.6))
        noise_amp = float(kwargs.get("noise_amplitude", 0.02))
        x = np.zeros((n, channels))
        for ch in range(channels):
            f1 = 200.0 + 170.0 * ch
            f2 = 1500.0 + 430.0 * ch
            x[:, ch] = (0.7 * np.sin(2 * np.pi * f1 * t)
                        + 0.3 * np.sin(2 * np.pi * f2 * t))
        x = amp * x + noise_amp * rng.standard_normal((n, channels))
    else:
        raise ValueError(f"未知信号类型: {kind!r}")

    x = np.asarray(x, dtype=np.float64)
    return x.ravel() if channels == 1 else x
