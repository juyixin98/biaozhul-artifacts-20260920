"""分块窗口：把长信号切成窗口并逐窗估计延迟。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import EstimatorConfig
from .correlator import LagEstimate, estimate_lag


@dataclass(frozen=True)
class WindowEstimate:
    """一个窗口的估计值连同其在原信号中的位置。"""

    index: int
    start: int
    end: int
    estimate: LagEstimate

    def to_dict(self, sample_rate: float | None) -> dict:
        e = self.estimate
        out: dict = {
            "window": self.index,
            "start_sample": self.start,
            "end_sample": self.end,
            "delay_samples": _finite(e.delay_samples),
            "delay_seconds": _finite(e.delay_samples / sample_rate)
            if sample_rate
            else None,
            "peak": _finite(e.peak),
            "second_peak": _finite(e.second_peak),
            "peak_ratio": _finite(e.peak_ratio),
            "rms": _finite(e.rms),
            "rms_db": _finite(e.rms_db),
            "edge_hit": e.edge_hit,
            "status": "confident" if e.confident else "uncertain",
        }
        if not e.confident:
            out["reasons"] = list(e.reasons)
        return out


def iter_windows(
    n: int, window_size: int, hop_size: int
) -> list[tuple[int, int]]:
    """返回 ``[start, end)`` 窗口列表。

    非重叠（``hop_size == window_size``）时末尾不足一窗的信号被丢弃；
    hop 更小时，只要还能切出长度为 ``window_size`` 的完整窗口就继续，
    同样不补零、不留半截窗（半截窗会让 NCC 边缘归一化失真）。
    """
    if window_size <= 0 or hop_size <= 0:
        raise ValueError("window_size 与 hop_size 必须为正整数")
    windows: list[tuple[int, int]] = []
    start = 0
    while start + window_size <= n:
        windows.append((start, start + window_size))
        start += hop_size
    return windows


def estimate_windows(
    ref: np.ndarray,
    chan: np.ndarray,
    config: EstimatorConfig,
) -> list[WindowEstimate]:
    """逐窗口估计延迟。

    ``window_size`` 为 None 时整段信号作为单个窗口；否则按窗口切分。
    两通道等长（由上游 :func:`delay_correlator.pipeline.run_analysis` 保证）。
    """
    n = ref.size
    if config.window_size is None:
        windows = [(0, n)] if n > 2 * config.max_lag else []
        hop = None
    else:
        w = int(config.window_size)
        hop = int(config.hop_size) if config.hop_size is not None else w
        windows = iter_windows(n, w, hop)

    results: list[WindowEstimate] = []
    for i, (start, end) in enumerate(windows):
        est = estimate_lag(ref[start:end], chan[start:end], config)
        results.append(WindowEstimate(index=i, start=start, end=end, estimate=est))
    return results


def _finite(x: float) -> float | None:
    """JSON 不支持 ±inf/NaN：低能量窗口的 -inf dB 等输出为 null。"""
    return float(x) if np.isfinite(x) else None
