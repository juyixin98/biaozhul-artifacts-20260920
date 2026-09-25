"""加窗 FFT 峰检测与亚频点插值核心流程。

流程：加窗 -> rFFT -> 幅度谱 -> 局部极大值候选 -> 主瓣抑制（避免把窗的
旁瓣当成独立峰）-> 三点插值 -> 幅度换算 -> 邻峰干扰/边界标志。

适用条件（务必阅读）：
- 每个待估峰在其主瓣宽度（Hann 约 4 个 bin）内**只有一个**信号分量；
  两个间距小于主瓣宽度的分量会合并成一个峰，此时估计有偏且无法被
  interference 标志发现（因为只检测到一个峰）。
- 估计精度受采样定理与有限窗长限制：本模块不承诺超出采样信息本身的精度。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np

from .windows import get_window
from .interpolate import (
    resolve_method,
    estimate_delta,
    hann_amplitude_correction,
    log_parabolic_peak_mag,
)

#: Hann 窗主瓣全宽（bin），小于该间距的候选峰视为同一主瓣/旁瓣
MAINLOBE_BINS = 4
#: 邻峰干扰判定距离（bin）：两个独立峰间距在此范围内时主瓣/旁瓣互相污染
DEFAULT_INTERFERENCE_BINS = 8
#: 默认相对门限：低于最强峰该 dB 数的候选被丢弃（抑制旁瓣与噪声）
DEFAULT_MIN_RELATIVE_DB = -45.0


@dataclass(frozen=True)
class AnalysisConfig:
    """分析参数。"""

    window: str = "hann"
    method: str = "auto"
    max_peaks: int = 10
    min_relative_db: float = DEFAULT_MIN_RELATIVE_DB
    suppression_bins: int = MAINLOBE_BINS
    interference_bins: int = DEFAULT_INTERFERENCE_BINS


@dataclass(frozen=True)
class PeakEstimate:
    """单个峰的估计结果。"""

    rank: int
    bin: int
    delta_bins: float
    frequency_hz: float
    amplitude: float
    interference: bool
    boundary: bool
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("extra", None)
        return d


def _amplitude_spectrum(samples: np.ndarray, window_name: str):
    """加窗 rFFT，返回 (复频谱 X, 幅度谱 mag, 窗和 sum_w)。

    mag 为未归一化的 |rFFT(x*w)|；正弦峰值幅度换算在调用处进行。
    """
    w = get_window(window_name, samples.size)
    X = np.fft.rfft(samples * w)
    return X, np.abs(X), float(np.sum(w))


def _find_candidates(mag: np.ndarray) -> list[int]:
    """局部极大值 bin 索引（含边界 bin 0 与 Nyquist）。"""
    n = mag.size
    candidates = []
    for k in range(n):
        left = mag[k - 1] if k > 0 else -np.inf
        right = mag[k + 1] if k < n - 1 else -np.inf
        if mag[k] > 0.0 and mag[k] >= left and mag[k] > right:
            candidates.append(k)
    return candidates


def _suppress_and_limit(
    candidates: list[int], mag: np.ndarray, config: AnalysisConfig
) -> list[int]:
    """按幅度降序接受候选：距已接受峰不足 suppression_bins 的丢弃，
    再按相对门限与 max_peaks 截断。返回按 bin 升序的索引。"""
    ordered = sorted(candidates, key=lambda k: mag[k], reverse=True)
    if not ordered:
        return []
    strongest = mag[ordered[0]]
    floor = strongest * 10.0 ** (config.min_relative_db / 20.0)
    accepted: list[int] = []
    for k in ordered:
        if mag[k] < floor:
            break
        if all(abs(k - j) >= config.suppression_bins for j in accepted):
            accepted.append(k)
        if len(accepted) >= config.max_peaks:
            break
    return sorted(accepted)


def _estimate_peak(
    X: np.ndarray,
    mag: np.ndarray,
    k: int,
    sum_w: float,
    method: str,
    bin_hz: float,
) -> tuple[int, float, float, float, bool]:
    """估计单个峰，返回 (bin, delta, frequency_hz, amplitude, boundary)。"""
    last = mag.size - 1
    boundary = k == 0 or k == last
    if boundary:
        delta = 0.0
        peak_mag = mag[k]
    else:
        delta = estimate_delta(method, X, mag, k)
        if method == "log-parabolic":
            peak_mag = log_parabolic_peak_mag(mag, k, delta)
        else:
            peak_mag = mag[k] * hann_amplitude_correction(delta)
    # 单边谱幅度换算：非 DC/Nyquist 乘 2
    scale = 1.0 if boundary else 2.0
    amplitude = float(scale * peak_mag / sum_w)
    frequency = float((k + delta) * bin_hz)
    return k, float(delta), frequency, amplitude, boundary


def detect_peaks(
    samples: np.ndarray, sample_rate: float, config: AnalysisConfig | None = None
) -> list[PeakEstimate]:
    """对一维实信号做峰检测与插值，返回按幅度降序的 PeakEstimate 列表。"""
    config = config or AnalysisConfig()
    x = np.asarray(samples, dtype=np.float64).ravel()
    if x.size < 8:
        raise ValueError(f"样本数过少（{x.size}），至少需要 8 点")
    if sample_rate <= 0:
        raise ValueError(f"采样率必须为正，得到 {sample_rate}")

    method = resolve_method(config.method, config.window)
    X, mag, sum_w = _amplitude_spectrum(x, config.window)
    bin_hz = sample_rate / x.size

    candidates = _find_candidates(mag)
    accepted = _suppress_and_limit(candidates, mag, config)

    peaks: list[PeakEstimate] = []
    for k in accepted:
        bk, delta, freq, amp, boundary = _estimate_peak(X, mag, k, sum_w, method, bin_hz)
        interference = any(
            j != k and abs(j - k) <= config.interference_bins for j in accepted
        )
        peaks.append(
            PeakEstimate(
                rank=0,
                bin=bk,
                delta_bins=round(delta, 6),
                frequency_hz=round(freq, 6),
                amplitude=round(amp, 9),
                interference=interference,
                boundary=boundary,
            )
        )
    peaks.sort(key=lambda p: p.amplitude, reverse=True)
    return [
        PeakEstimate(rank=i + 1, **{k: v for k, v in p.to_dict().items() if k != "rank"})
        for i, p in enumerate(peaks)
    ]
