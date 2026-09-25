"""服务层：统一入口 analyze / analyze_request，输入信号或请求字典，输出纯数值字典。"""

from __future__ import annotations

import numpy as np

from .detector import AnalysisConfig, detect_peaks
from .pcm import load_pcm


def analyze(
    samples,
    sample_rate: float,
    *,
    window: str = "hann",
    method: str = "auto",
    max_peaks: int = 10,
    min_relative_db: float = -45.0,
    interference_bins: int = 8,
) -> dict:
    """分析一维实信号，返回可 JSON 序列化的结果字典。"""
    x = np.asarray(samples, dtype=np.float64).ravel()
    config = AnalysisConfig(
        window=window,
        method=method,
        max_peaks=max_peaks,
        min_relative_db=min_relative_db,
        interference_bins=interference_bins,
    )
    peaks = detect_peaks(x, sample_rate, config)
    resolved = config.method if config.method != "auto" else (
        "hann-exact" if config.window == "hann" else "log-parabolic"
    )
    return {
        "sample_rate": float(sample_rate),
        "n_samples": int(x.size),
        "window": config.window,
        "method": resolved,
        "frequency_resolution_hz": float(sample_rate) / int(x.size),
        "peaks": [p.to_dict() for p in peaks],
    }


def synthesize(
    tones: list[dict],
    sample_rate: float,
    n_samples: int,
    noise_db: float | None = None,
    seed: int = 0,
) -> np.ndarray:
    """合成测试信号：若干正弦叠加，可选加性高斯白噪声。

    tones 元素: {"frequency_hz": f, "amplitude": a, "phase": p(弧度, 可选)}
    noise_db: 噪声 RMS 相对满幅 1.0 的 dB 值，例如 -60。
    """
    if n_samples < 8:
        raise ValueError(f"n_samples 至少为 8，得到 {n_samples}")
    t = np.arange(n_samples, dtype=np.float64) / float(sample_rate)
    x = np.zeros(n_samples, dtype=np.float64)
    for tone in tones:
        f = float(tone["frequency_hz"])
        a = float(tone.get("amplitude", 1.0))
        p = float(tone.get("phase", 0.0))
        x += a * np.sin(2.0 * np.pi * f * t + p)
    if noise_db is not None:
        rms = 10.0 ** (float(noise_db) / 20.0)
        rng = np.random.default_rng(seed)
        x += rms * rng.standard_normal(n_samples)
    return x


def analyze_request(request: dict) -> dict:
    """按请求字典执行分析（对应 examples/ 下的请求样例）。

    mode = "synth": 由 tones 合成信号后分析；
    mode = "pcm":   读取本地 PCM 文件后分析。
    """
    if not isinstance(request, dict):
        raise ValueError("请求必须是 JSON 对象")
    mode = request.get("mode")
    sample_rate = float(request.get("sample_rate", 0))
    if sample_rate <= 0:
        raise ValueError("请求缺少正的 sample_rate")
    analysis = request.get("analysis", {}) or {}

    if mode == "synth":
        samples = synthesize(
            tones=request.get("tones", []),
            sample_rate=sample_rate,
            n_samples=int(request.get("n_samples", 4096)),
            noise_db=request.get("noise_db"),
            seed=int(request.get("seed", 0)),
        )
    elif mode == "pcm":
        path = request.get("path")
        if not path:
            raise ValueError("pcm 模式缺少 path")
        samples = load_pcm(
            path,
            dtype=request.get("dtype", "int16"),
            max_samples=request.get("n_samples"),
            offset_samples=int(request.get("offset_samples", 0)),
        )
    else:
        raise ValueError(f"未知 mode: {mode!r}，可选 'synth' / 'pcm'")

    return analyze(samples, sample_rate, **analysis)
