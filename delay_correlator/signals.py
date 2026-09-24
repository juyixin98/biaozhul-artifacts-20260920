"""合成测试信号生成：噪声、正弦、扫频，以及带已知延迟的双通道对。

``make_delayed_pair`` 生成的信号满足 B[n] = A[n - delay]（零填充移位），
即本项目统一符号约定：delay > 0 表示 B 晚于 A。真值延迟随结果一并返回，
供自动化测试与人工核对。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = [
    "white_noise",
    "sine",
    "chirp",
    "shift_signal",
    "add_awgn",
    "make_delayed_pair",
    "DelayedPair",
]

rng = np.random.default_rng


def white_noise(
    n: int, *, amplitude: float = 1.0, seed: int | None = None
) -> np.ndarray:
    """限带白噪声（均匀分布，幅度约在 ``[-amplitude, amplitude]``）。"""
    return amplitude * rng(seed).uniform(-1.0, 1.0, size=int(n))


def sine(
    n: int,
    freq: float,
    sample_rate: float,
    *,
    amplitude: float = 1.0,
    phase: float = 0.0,
) -> np.ndarray:
    """正弦波 ``amplitude * sin(2π f t + phase)``。"""
    t = np.arange(int(n)) / float(sample_rate)
    return amplitude * np.sin(2.0 * np.pi * float(freq) * t + float(phase))


def chirp(
    n: int,
    f0: float,
    f1: float,
    sample_rate: float,
    *,
    amplitude: float = 1.0,
) -> np.ndarray:
    """线性扫频（瞬时频率从 ``f0`` 线性变到 ``f1``）。"""
    t = np.arange(int(n)) / float(sample_rate)
    duration = n / float(sample_rate)
    phase = 2.0 * np.pi * (f0 * t + 0.5 * (f1 - f0) / duration * t * t)
    return amplitude * np.sin(phase)


def shift_signal(x: np.ndarray, delay: int) -> np.ndarray:
    """零填充整数移位：``y[n] = x[n - delay]``。

    delay > 0 右移（y 更晚），delay < 0 左移（y 更早）。
    """
    x = np.asarray(x, dtype=np.float64)
    d = int(delay)
    y = np.zeros_like(x)
    if d >= 0:
        if d < x.size:
            y[d:] = x[: x.size - d]
    else:
        k = -d
        if k < x.size:
            y[: x.size - k] = x[k:]
    return y


def add_awgn(
    x: np.ndarray, snr_db: float, *, seed: int | None = None
) -> np.ndarray:
    """按指定信噪比（dB，功率比）叠加高斯白噪声。"""
    x = np.asarray(x, dtype=np.float64)
    sig_power = float(np.mean(x * x))
    if sig_power <= 0.0:
        raise ValueError("信号能量为 0，无法按 SNR 加噪")
    noise_power = sig_power / (10.0 ** (snr_db / 10.0))
    noise = rng(seed).normal(0.0, np.sqrt(noise_power), size=x.shape)
    return x + noise


@dataclass(frozen=True)
class DelayedPair:
    """合成双通道对及其真值。"""

    a: np.ndarray
    b: np.ndarray
    true_delay: int
    signal_type: str
    sample_rate: float


def make_delayed_pair(
    *,
    n: int = 8000,
    delay: int = 0,
    signal_type: str = "noise",
    sample_rate: float = 8000.0,
    freq: float = 440.0,
    freq_end: float | None = None,
    amplitude: float = 0.8,
    snr_db: float | None = None,
    seed: int | None = 0,
    seed_b: int | None = None,
) -> DelayedPair:
    """生成带已知整数延迟的双通道信号。

    参数
    ----
    n:
        每通道样本数。
    delay:
        真值延迟（样本），B 相对 A，可正可负。
    signal_type:
        ``"noise"``（白噪声）、``"sine"``（正弦）、``"chirp"``（线性扫频）、
        ``"uncorrelated"``（两路独立噪声，无真实延迟，用于低相关测试）、
        ``"silence"``（全零，用于静音测试）。
    snr_db:
        B 通道在移位后叠加的噪声信噪比；None 表示不加噪。
    seed:
        随机种子。
    seed_b:
        ``uncorrelated`` 时 B 通道使用的独立种子（默认 seed + 777）。
    """
    n = int(n)
    st = signal_type.lower()
    if st == "noise":
        a = white_noise(n, amplitude=amplitude, seed=seed)
    elif st == "sine":
        a = sine(n, freq=freq, sample_rate=sample_rate, amplitude=amplitude)
    elif st == "chirp":
        a = chirp(
            n,
            f0=freq,
            f1=freq_end if freq_end is not None else freq * 4.0,
            sample_rate=sample_rate,
            amplitude=amplitude,
        )
    elif st == "uncorrelated":
        a = white_noise(n, amplitude=amplitude, seed=seed)
        b = white_noise(n, amplitude=amplitude, seed=(seed_b if seed_b is not None
                                                      else (seed or 0) + 777))
        return DelayedPair(a=a, b=b, true_delay=0, signal_type=st,
                           sample_rate=float(sample_rate))
    elif st == "silence":
        a = np.zeros(n, dtype=np.float64)
        return DelayedPair(a=a, b=np.zeros_like(a), true_delay=int(delay),
                           signal_type=st, sample_rate=float(sample_rate))
    else:
        raise ValueError(f"未知 signal_type: {signal_type!r}")

    b = shift_signal(a, delay)
    if snr_db is not None:
        b = add_awgn(b, snr_db=snr_db, seed=(None if seed is None else seed + 1))
    return DelayedPair(
        a=a,
        b=b,
        true_delay=int(delay),
        signal_type=st,
        sample_rate=float(sample_rate),
    )
