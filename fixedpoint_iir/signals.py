"""合成信号生成与本地 PCM 数据读写。"""

from __future__ import annotations

import numpy as np

_PCM_DTYPES = {
    "s16le": np.dtype("<i2"),
    "s32le": np.dtype("<i4"),
    "u8": np.dtype("u1"),
}


def gen_impulse(n: int, amplitude: float = 1.0, index: int = 0) -> np.ndarray:
    """单位冲激信号。"""
    x = np.zeros(n)
    if 0 <= index < n:
        x[index] = amplitude
    return x


def gen_step(n: int, amplitude: float = 1.0, index: int = 0) -> np.ndarray:
    """阶跃信号。"""
    x = np.zeros(n)
    x[max(index, 0):] = amplitude
    return x


def gen_sine(n: int, freq_hz: float, sample_rate: float, amplitude: float = 1.0,
             phase: float = 0.0) -> np.ndarray:
    """正弦信号。"""
    t = np.arange(n) / sample_rate
    return amplitude * np.sin(2.0 * np.pi * freq_hz * t + phase)


def gen_multi_sine(n: int, freqs_hz: list, sample_rate: float,
                   amplitudes: list | None = None) -> np.ndarray:
    """多音叠加信号。"""
    if amplitudes is None:
        amplitudes = [1.0 / len(freqs_hz)] * len(freqs_hz)
    x = np.zeros(n)
    for f, a in zip(freqs_hz, amplitudes):
        x += gen_sine(n, f, sample_rate, a)
    return x


def gen_noise(n: int, amplitude: float = 1.0, seed: int = 0) -> np.ndarray:
    """均匀分布白噪声（确定性种子）。"""
    rng = np.random.default_rng(seed)
    return rng.uniform(-amplitude, amplitude, n)


def gen_chirp(n: int, f0_hz: float, f1_hz: float, sample_rate: float,
              amplitude: float = 1.0) -> np.ndarray:
    """线性扫频信号。"""
    t = np.arange(n) / sample_rate
    k = (f1_hz - f0_hz) / t[-1] if n > 1 else 0.0
    return amplitude * np.sin(2.0 * np.pi * (f0_hz * t + 0.5 * k * t * t))


def read_pcm(path: str, fmt: str = "s16le", normalize: bool = True) -> np.ndarray:
    """读取本地 PCM 原始数据文件。

    Args:
        path: 文件路径。
        fmt: 采样格式，支持 s16le / s32le / u8。
        normalize: True 时归一化到 [-1, 1) 浮点；False 返回整数码值。
    """
    if fmt not in _PCM_DTYPES:
        raise ValueError(f"不支持的 PCM 格式: {fmt}，可选 {sorted(_PCM_DTYPES)}")
    data = np.fromfile(path, dtype=_PCM_DTYPES[fmt])
    if not normalize:
        return data.astype(np.float64)
    if fmt == "u8":
        return (data.astype(np.float64) - 128.0) / 128.0
    info = np.iinfo(_PCM_DTYPES[fmt])
    return data.astype(np.float64) / (abs(info.min))


def write_pcm(path: str, x: np.ndarray, fmt: str = "s16le") -> None:
    """把 [-1, 1] 浮点信号写为 PCM 原始数据文件（先截断到 [-1, 1]）。"""
    if fmt not in _PCM_DTYPES:
        raise ValueError(f"不支持的 PCM 格式: {fmt}，可选 {sorted(_PCM_DTYPES)}")
    clipped = np.clip(np.asarray(x, dtype=np.float64), -1.0, 1.0)
    if fmt == "u8":
        data = np.round(clipped * 127.0 + 128.0).astype(_PCM_DTYPES[fmt])
    else:
        info = np.iinfo(_PCM_DTYPES[fmt])
        data = np.round(clipped * abs(info.min)).clip(info.min, info.max).astype(_PCM_DTYPES[fmt])
    data.tofile(path)


def build_signal(spec: dict) -> np.ndarray:
    """按请求规格构造输入信号。

    spec 示例：
        {"type": "impulse", "n": 256, "amplitude": 0.9}
        {"type": "sine", "n": 1024, "freq_hz": 440, "sample_rate": 48000}
        {"type": "noise", "n": 512, "amplitude": 0.5, "seed": 7}
        {"type": "pcm_file", "path": "in.pcm", "fmt": "s16le"}
    """
    kind = spec.get("type")
    if kind == "impulse":
        return gen_impulse(spec["n"], spec.get("amplitude", 1.0), spec.get("index", 0))
    if kind == "step":
        return gen_step(spec["n"], spec.get("amplitude", 1.0), spec.get("index", 0))
    if kind == "sine":
        return gen_sine(spec["n"], spec["freq_hz"], spec["sample_rate"],
                        spec.get("amplitude", 1.0), spec.get("phase", 0.0))
    if kind == "multi_sine":
        return gen_multi_sine(spec["n"], spec["freqs_hz"], spec["sample_rate"],
                              spec.get("amplitudes"))
    if kind == "noise":
        return gen_noise(spec["n"], spec.get("amplitude", 1.0), spec.get("seed", 0))
    if kind == "chirp":
        return gen_chirp(spec["n"], spec["f0_hz"], spec["f1_hz"],
                         spec["sample_rate"], spec.get("amplitude", 1.0))
    if kind == "pcm_file":
        return read_pcm(spec["path"], spec.get("fmt", "s16le"))
    raise ValueError(f"未知信号类型: {kind}")
