"""归一化互相关与延迟（峰）估计核心。

NCC 采用 Pearson 形式（每个 lag 独立做零均值、单位能量归一化），因此对两通道间的
直流偏置与固定幅度增益都不敏感。相关约定为：在 lag ``d`` 处比较 ``ref[k - d]`` 与
``chan[k]``，于是

    chan[k] = ref[k - D]  =>  NCC 在 d = D 处取峰，

正的 D 即 chan 相对 ref 的右移（滞后）采样数。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import EstimatorConfig


@dataclass(frozen=True)
class LagEstimate:
    """单个窗口的延迟估计结果。

    ``confident`` 为 True 时 ``delay_samples`` 才可信；否则它仍是最大 NCC 峰的位置，
    但调用方应按 ``reasons`` 中的原因视为不确定结果。
    """

    delay_samples: float
    peak: float
    second_peak: float
    peak_ratio: float
    rms: float
    rms_db: float
    confident: bool
    reasons: tuple[str, ...]
    edge_hit: bool


def normalized_cross_correlation(
    ref: np.ndarray,
    chan: np.ndarray,
    max_lag: int,
    subtract_mean: bool = True,
) -> np.ndarray:
    """计算 lag 区间 ``[-max_lag, max_lag]`` 上的归一化互相关。

    参数
    ----
    ref, chan:
        两个等长一维实信号（长度 N）。
    max_lag:
        最大搜索延迟（采样点），要求 ``2*max_lag < N``。
    subtract_mean:
        True（默认）时为 Pearson 相关（每个 lag 对参与内积的两个切片分别去均值）；
        False 时为纯能量归一化相关（假设输入本身已去均值）。

    返回
    ----
    np.ndarray
        长度 ``2*max_lag+1``，索引 ``i`` 对应 ``lag = i - max_lag``。

    实现为直接切片内积，长度 L 的窗口复杂度约 O(L * max_lag)。对本服务面向的
    离线中小窗口（数千采样点）足够快，且语义直白、便于审计；长信号可通过分块
    （:mod:`delay_correlator.windows`）控制单窗规模。
    """
    ref = np.asarray(ref, dtype=np.float64)
    chan = np.asarray(chan, dtype=np.float64)
    if ref.ndim != 1 or chan.ndim != 1:
        raise ValueError("ref 与 chan 必须是一维数组")
    if ref.shape != chan.shape:
        raise ValueError(f"ref 与 chan 必须等长，收到 {ref.shape} 与 {chan.shape}")
    n = ref.size
    if max_lag <= 0:
        raise ValueError("max_lag 必须为正整数")
    if n <= 2 * max_lag:
        raise ValueError(f"信号长度 ({n}) 必须大于 2*max_lag ({2 * max_lag})")

    lags = np.arange(-max_lag, max_lag + 1, dtype=np.int64)
    corr = np.zeros(lags.size, dtype=np.float64)

    for i, d in enumerate(lags):
        if d >= 0:
            # 比较 ref[k - d] 与 chan[k]：取 ref[0 : n-d] 与 chan[d : n]
            a = ref[: n - d]
            b = chan[d:]
        else:
            # d < 0：取 ref[-d : n] 与 chan[0 : n+d]
            a = ref[-d:]
            b = chan[: n + d]

        if subtract_mean:
            a = a - a.mean()
            b = b - b.mean()

        denom = float(np.sqrt(np.dot(a, a) * np.dot(b, b)))
        # 静音/常数切片能量为 0：相关无定义，保持 0.0（随后由能量门限判不确定）。
        corr[i] = float(np.dot(a, b) / denom) if denom > 0.0 else 0.0

    return corr


def _parabolic_refine(corr: np.ndarray, idx: int) -> float:
    """以 idx 为中心做三点抛物线插值，返回亚采样修正后的位置（索引坐标）。

    对 NCC 峰附近的连续近似：修正量 = 0.5*(y_- - y_+) / (y_- - 2 y_0 + y_+)。
    峰在边界或曲率非负（平顶）时不修正，直接返回整数位置。
    """
    if idx <= 0 or idx >= corr.size - 1:
        return float(idx)
    y_m, y_0, y_p = float(corr[idx - 1]), float(corr[idx]), float(corr[idx + 1])
    denom = y_m - 2.0 * y_0 + y_p
    if denom >= 0.0:
        return float(idx)
    return idx + 0.5 * (y_m - y_p) / denom


def _find_second_peak(
    corr: np.ndarray, peak_idx: int, min_distance: int
) -> float:
    """次强候选峰高度（按 NCC 绝对值）。

    先贪心取全局 |NCC| 最大点；次峰在与主峰索引距离 >= ``min_distance`` 的位置中
    取最大 |NCC|。按绝对值比较可以识别出“强反相峰与正峰并列”这类二义性。
    """
    if min_distance <= peak_idx:
        # 距离 >= min_distance 的左侧候选：索引 0 .. peak_idx-min_distance
        left = np.abs(corr[: peak_idx - min_distance + 1])
    else:
        left = np.empty(0)
    if peak_idx + min_distance <= corr.size - 1:
        right = np.abs(corr[peak_idx + min_distance :])
    else:
        right = np.empty(0)
    candidates = np.concatenate((left, right))
    if candidates.size == 0:
        return 0.0
    return float(candidates.max())


def estimate_lag(
    ref: np.ndarray,
    chan: np.ndarray,
    config: EstimatorConfig,
) -> LagEstimate:
    """在单个窗口上估计延迟并给出置信度。

    不确定状态（``confident=False``）的原因码：

    ``low_energy``
        任一通道 RMS 低于 ``min_rms_db``（静音/近静音窗口）。
    ``low_peak``
        NCC 最大绝对峰低于 ``min_peak``（两通道不相关，常见于纯噪声/错配）。
    ``multiple_peaks``
        次峰与主峰之比超过 ``max_secondary_peak_ratio``（周期信号天然多峰）。
    ``edge_hit``
        峰贴在 ``|lag| == max_lag`` 边界，真实延迟可能超出搜索范围。
    """
    corr = normalized_cross_correlation(
        ref, chan, config.max_lag, config.subtract_mean
    )

    abs_corr = np.abs(corr)
    peak_idx = int(np.argmax(abs_corr))
    peak_value = float(corr[peak_idx])  # 保留符号：反相峰应为负值
    peak_abs = abs(peak_value)

    if config.interpolate:
        refined_idx = _parabolic_refine(abs_corr, peak_idx)
    else:
        refined_idx = float(peak_idx)
    delay_samples = refined_idx - config.max_lag

    second_peak = _find_second_peak(abs_corr, peak_idx, config.min_peak_distance)
    peak_ratio = float(second_peak / peak_abs) if peak_abs > 0.0 else float("inf")

    # 能量按两通道中较弱者判定：一路静音即无法可靠做相关。
    rms_ref = float(np.sqrt(np.mean(ref.astype(np.float64) ** 2)))
    rms_chan = float(np.sqrt(np.mean(chan.astype(np.float64) ** 2)))
    rms = min(rms_ref, rms_chan)
    rms_db = 20.0 * np.log10(rms) if rms > 0.0 else float("-inf")

    edge_hit = bool(peak_idx == 0 or peak_idx == corr.size - 1)

    reasons: list[str] = []
    if rms < config.rms_floor:
        reasons.append("low_energy")
    if peak_abs < config.min_peak:
        reasons.append("low_peak")
    if peak_ratio > config.max_secondary_peak_ratio:
        reasons.append("multiple_peaks")
    if edge_hit:
        reasons.append("edge_hit")

    return LagEstimate(
        delay_samples=float(delay_samples),
        peak=peak_value,
        second_peak=float(second_peak),
        peak_ratio=peak_ratio,
        rms=rms,
        rms_db=float(rms_db),
        confident=not reasons,
        reasons=tuple(reasons),
        edge_hit=edge_hit,
    )
