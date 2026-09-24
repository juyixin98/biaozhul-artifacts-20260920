"""单窗口延迟估计：峰检测、置信度与不确定状态判定。

不确定状态（``status == "uncertain"``）由以下任一原因触发，原因列表在
``uncertainty_reasons`` 中给出：

- ``low_energy``     ：两通道整体能量低于 ``energy_db``（默认 -60 dBFS 去均值能量）；
- ``low_coherence``  ：最强峰相关系数低于 ``min_coherence``（默认 0.5）；
- ``ambiguous_peaks``：存在两个以上相距超过 ``ambiguity_guard`` 样本、
                       强度接近（比值/差距超阈）的峰——典型周期信号症状；
- ``boundary_peak``  ：最强峰位于搜索范围边界，真实峰可能在范围之外。

全部原因都不触发时 ``status == "ok"``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .correlator import CorrelationProfile, normalized_xcorr

__all__ = ["EstimateResult", "estimate_window", "local_maxima"]

# 峰高相等的容差：FFT/浮点噪声约 1e-12 以下视为同高。
_PEAK_EPS = 1e-9


@dataclass
class EstimateResult:
    """单窗口估计结果。"""

    window_index: int
    sample_start: int
    sample_end: int
    lag_samples: int
    """最优整数延迟（样本），B 相对 A：正=B 晚。"""
    lag_seconds: float | None
    """延迟秒数（需给定采样率）。"""
    peak_coherence: float
    """最强峰的归一化相关系数，主置信度指标。"""
    second_coherence: float | None
    """次强候选峰相关系数（经非极大抑制后），无次峰为 None。"""
    peak_ratio: float | None
    """次峰/主峰强度比（越接近 1 越不可信），无次峰为 None。"""
    prominence: float | None
    """主峰与次峰的相关系数差距，无次峰为 None。"""
    fractional_lag: float | None
    """抛物线插值的亚样本延迟（仅峰在搜索范围内部时给出）。"""
    at_boundary: bool
    status: str
    """``"ok"`` 或 ``"uncertain"``。"""
    uncertainty_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_index": self.window_index,
            "sample_start": int(self.sample_start),
            "sample_end": int(self.sample_end),
            "lag_samples": int(self.lag_samples),
            "lag_seconds": self.lag_seconds,
            "fractional_lag": self.fractional_lag,
            "peak_coherence": float(self.peak_coherence),
            "second_coherence": (
                None if self.second_coherence is None else float(self.second_coherence)
            ),
            "peak_ratio": None if self.peak_ratio is None else float(self.peak_ratio),
            "prominence": None if self.prominence is None else float(self.prominence),
            "at_boundary": bool(self.at_boundary),
            "status": self.status,
            "uncertainty_reasons": list(self.uncertainty_reasons),
        }


def local_maxima(corr: np.ndarray, eps: float = _PEAK_EPS) -> np.ndarray:
    """返回严格局部极大的峰索引；平顶取平台中点。

    判定：峰点不小于左右邻居，且其所在平台严格高于平台两侧紧邻值。
    全平（例如常数信号）返回空数组。
    """
    corr = np.asarray(corr, dtype=np.float64)
    n = corr.size
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    peaks: list[int] = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(corr[j + 1] - corr[i]) <= eps:
            j += 1
        # [i, j] 为等值平台；覆盖全数组的平台（如常数信号）没有可比较的
        # 邻居，不算峰。
        if i == 0 and j == n - 1:
            break
        left_ok = i == 0 or corr[i] > corr[i - 1] + eps
        right_ok = j == n - 1 or corr[j] > corr[j + 1] + eps
        if left_ok and right_ok:
            peaks.append((i + j) // 2)
        i = j + 1
    return np.asarray(peaks, dtype=np.int64)


def _parabolic_peak(corr: np.ndarray, idx: int) -> float | None:
    """三点抛物线拟合亚样本峰位置（相对整数索引的偏移折入返回值）。

    返回连续滞后轴上的峰位置（索引单位）；峰在边界（无两侧点）时返回 None。
    """
    if idx <= 0 or idx >= corr.size - 1:
        return None
    y0, y1, y2 = corr[idx - 1], corr[idx], corr[idx + 1]
    denom = y0 - 2.0 * y1 + y2
    if denom >= 0.0:
        # 非负曲率说明数值上不是干净的尖峰，不做插值。
        return None
    delta = 0.5 * (y0 - y2) / denom
    if not np.isfinite(delta) or abs(delta) >= 1.0:
        return None
    return float(idx + delta)


def estimate_window(
    a: np.ndarray,
    b: np.ndarray,
    max_lag: int,
    *,
    window_index: int = 0,
    sample_start: int = 0,
    sample_rate: float | None = None,
    method: str = "zncc",
    min_coherence: float = 0.5,
    ambiguity_guard: int = 3,
    ambiguity_ratio: float = 0.9,
    ambiguity_gap: float = 0.1,
    energy_db: float = -60.0,
    profile: CorrelationProfile | None = None,
) -> EstimateResult:
    """对一对等长信号段做延迟估计。

    参数
    ----
    a, b:
        双通道信号段（等长）。
    max_lag:
        搜索的最大绝对延迟（样本）。
    sample_rate:
        给定后同时输出 ``lag_seconds``。
    method:
        ``"zncc"`` 或 ``"ncc"``，见 :mod:`delay_correlator.correlator`。
    min_coherence:
        峰相关系数低于此值判 ``low_coherence``。
    ambiguity_guard:
        非极大抑制半径（样本）；候选峰相距超过它才计为独立次峰。
    ambiguity_ratio, ambiguity_gap:
        次峰/主峰比值大于 ``ambiguity_ratio`` **或** 峰间差距小于
        ``ambiguity_gap`` 时判 ``ambiguous_peaks``。
    energy_db:
        去均值能量阈值（dBFS，以满幅平方为参考）。两通道合并 RMS 低于它判
        ``low_energy``（静音）。

    返回
    ----
    EstimateResult
    """
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    if profile is None:
        profile = normalized_xcorr(a, b, max_lag=max_lag, method=method)
    corr = profile.corr
    lags = profile.lags

    reasons: list[str] = []

    # ---- 能量检查（静音 / 低能量）--------------------------------
    n = a.size
    mean_a, mean_b = a.mean(), b.mean()
    var_energy = float(np.sum((a - mean_a) ** 2) + np.sum((b - mean_b) ** 2))
    pooled_rms = np.sqrt(var_energy / max(2 * n, 1))
    energy_threshold = 10.0 ** (energy_db / 20.0)
    if pooled_rms < energy_threshold:
        reasons.append("low_energy")

    # ---- 峰检测 + 非极大抑制 --------------------------------------
    peaks = local_maxima(corr)
    # 边界点没有“两侧邻居”，若其值不低于内侧点也纳入候选。
    if corr.size >= 2 and corr[0] >= corr[1] - _PEAK_EPS and not (peaks == 0).any():
        peaks = np.append(peaks, 0)
    if corr.size >= 2 and corr[-1] >= corr[-2] - _PEAK_EPS and not (
        peaks == corr.size - 1
    ).any():
        peaks = np.append(peaks, corr.size - 1)
    peaks = peaks[np.argsort(corr[peaks])[::-1]] if peaks.size else peaks

    guard = max(0, int(ambiguity_guard))
    kept: list[int] = []
    for p in peaks:
        if all(abs(int(p) - int(q)) > guard for q in kept):
            kept.append(int(p))
    best = int(np.argmax(corr))  # 全局最优，即使落在平台也按索引给出
    best_idx = int(best)
    peak_val = float(corr[best_idx])

    second_idx: int | None = kept[1] if len(kept) > 1 else None
    second_val = float(corr[second_idx]) if second_idx is not None else None

    ratio = None
    prominence = None
    if second_val is not None and peak_val != 0.0:
        ratio = second_val / peak_val if peak_val > 0 else None
        prominence = peak_val - second_val
        ambiguous = False
        if peak_val > 0 and ratio is not None and ratio >= ambiguity_ratio:
            ambiguous = True
        if prominence <= ambiguity_gap:
            ambiguous = True
        if ambiguous:
            reasons.append("ambiguous_peaks")

    if peak_val < min_coherence:
        reasons.append("low_coherence")

    at_boundary = best_idx == 0 or best_idx == corr.size - 1
    if at_boundary:
        reasons.append("boundary_peak")

    lag = int(lags[best_idx])
    frac_idx = _parabolic_peak(corr, best_idx)
    fractional_lag = None
    if frac_idx is not None:
        # lags 轴为等距整数轴，偏移可线性映射。
        fractional_lag = float(lags[0]) + frac_idx

    return EstimateResult(
        window_index=int(window_index),
        sample_start=int(sample_start),
        sample_end=int(sample_start + n),
        lag_samples=lag,
        lag_seconds=(lag / sample_rate if sample_rate else None),
        peak_coherence=peak_val,
        second_coherence=second_val,
        peak_ratio=ratio,
        prominence=prominence,
        fractional_lag=fractional_lag,
        at_boundary=at_boundary,
        status="uncertain" if reasons else "ok",
        uncertainty_reasons=reasons,
    )
