"""pytest 公共夹具与辅助。"""

import numpy as np
import pytest


@pytest.fixture
def rng():
    return np.random.default_rng(20260924)


def fractional_shift(x: np.ndarray, delay: float) -> np.ndarray:
    """用 FFT 频域线性相位实现非整数样本延迟（测试辅助）。

    满足时域含义 y[n] ≈ x[n - delay]（与项目符号约定一致）。
    """
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    freqs = np.fft.fftfreq(n, d=1.0)  # 单位：周/样本
    shift = np.exp(-2j * np.pi * freqs * delay)
    return np.fft.ifft(np.fft.fft(x) * shift).real
