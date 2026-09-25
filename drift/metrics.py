"""分布漂移指标与平滑规则。

核心指标（在非缺失桶上计算，溢出桶参与分布，缺值桶单独报告）：

- **PSI**（Population Stability Index）：
  ``PSI = sum_b (q_b - p_b) * ln(q_b / p_b)``，p 为基线频率、q 为当前频率；
- **JS 散度**（Jensen–Shannon divergence，以 2 为底时取值 [0, 1]）；
- **TVD**（总变差距离，取值 [0, 1]）；
- **缺值率差** ``miss_q - miss_p``；
- 逐桶 PSI 贡献，便于定位漂移来源。

平滑规则（处理空桶，见 :func:`smooth_distribution`）：

- ``none``：不平滑。任一侧空桶时 PSI 相应项为 ``inf``（如实暴露空桶）；
- ``laplace``：计数加 ``alpha`` 后归一化（对称、可解释为伪计数）；
- ``floor``：频率设下限 ``epsilon`` 后联合重新归一化（PSI 工程常用做法）。

注意：PSI/JS 等都是描述性分布差异量，**任何阈值切分都只是工程经验规则，
不是“分布相同/不同”的统计证明**。小样本下指标噪声很大，应结合样本量判断。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from .binning import (
    BinCounts,
    FixedBins,
    bin_labels,
    missing_rate,
)

SmoothingMethod = Literal["none", "laplace", "floor"]

# 经验阈值（仅用于打标提示，不是统计检验结论）
PSI_LITTLE_DRIFT = 0.1
PSI_SOME_DRIFT = 0.25
DEFAULT_ALPHA = 0.5  # 杰弗里斯式伪计数；经典拉普拉斯取 1
DEFAULT_EPSILON = 1e-4
DEFAULT_MIN_SAMPLE = 30  # 低于该观测数标记 small_sample（启发式）


@dataclass(frozen=True)
class DriftResult:
    """单次漂移计算的完整结果。"""

    feature: str
    bins: FixedBins
    labels: list[str]
    baseline_counts: np.ndarray
    current_counts: np.ndarray
    baseline_freq: np.ndarray  # 平滑后的非缺失桶频率
    current_freq: np.ndarray
    psi_per_bin: np.ndarray
    psi: float
    js_divergence: float
    tvd: float
    missing_rate_baseline: float
    missing_rate_current: float
    missing_rate_delta: float
    n_baseline: int
    n_current: int
    n_baseline_observed: int
    n_current_observed: int
    smoothing: SmoothingMethod
    alpha: float | None
    epsilon: float | None
    small_sample: bool
    current_all_missing: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "feature": self.feature,
            "n_baseline": self.n_baseline,
            "n_current": self.n_current,
            "n_baseline_observed": self.n_baseline_observed,
            "n_current_observed": self.n_current_observed,
            "smoothing": self.smoothing,
            "alpha": self.alpha,
            "epsilon": self.epsilon,
            "psi": _json_float(self.psi),
            "js_divergence": _json_float(self.js_divergence),
            "tvd": _json_float(self.tvd),
            "missing_rate_baseline": self.missing_rate_baseline,
            "missing_rate_current": self.missing_rate_current,
            "missing_rate_delta": self.missing_rate_current
            - self.missing_rate_baseline,
            "small_sample": self.small_sample,
            "current_all_missing": self.current_all_missing,
            "psi_band": interpret_psi(self.psi),
            "notes": list(self.notes),
            "bins": {
                "labels": list(self.labels),
                "baseline_counts": [int(x) for x in self.baseline_counts],
                "current_counts": [int(x) for x in self.current_counts],
                "baseline_freq": [_json_float(x) for x in self.baseline_freq],
                "current_freq": [_json_float(x) for x in self.current_freq],
                "psi_per_bin": [_json_float(x) for x in self.psi_per_bin],
            },
        }


def _json_float(x: float) -> float | str | None:
    """JSON 不支持 NaN/Inf，转为字符串标记，保证输出严格合法 JSON。"""
    if x is None:
        return None
    xf = float(x)
    if np.isnan(xf):
        return "nan"
    if np.isposinf(xf):
        return "inf"
    if np.isneginf(xf):
        return "-inf"
    return xf


def smooth_distribution(
    baseline_counts: np.ndarray,
    current_counts: np.ndarray,
    method: SmoothingMethod = "laplace",
    alpha: float = DEFAULT_ALPHA,
    epsilon: float = DEFAULT_EPSILON,
) -> tuple[np.ndarray, np.ndarray]:
    """把非缺失桶计数转为（可能平滑后的）频率，两侧频率各自求和为 1。

    - ``none``：直接归一化，保留 0；总观测为 0 时返回全 0；
    - ``laplace``：``(c + alpha) / (N + alpha * B)``，双侧同 alpha；
    - ``floor``：先归一化，再把低于 ``epsilon`` 的频率抬到 epsilon，
      双侧联合除以各自之和重新归一化。

    双侧使用同一种平滑，避免不对称伪计数制造虚假漂移。
    """
    p_counts = np.asarray(baseline_counts, dtype=float)
    q_counts = np.asarray(current_counts, dtype=float)
    if p_counts.shape != q_counts.shape:
        raise ValueError("基线与当前桶计数长度不一致")
    b = p_counts.shape[0]

    if method == "none":
        p = p_counts / p_counts.sum() if p_counts.sum() > 0 else p_counts.copy()
        q = q_counts / q_counts.sum() if q_counts.sum() > 0 else q_counts.copy()
        return p, q

    if method == "laplace":
        if alpha <= 0:
            raise ValueError("laplace 平滑的 alpha 必须 > 0")
        p = (p_counts + alpha) / (p_counts.sum() + alpha * b)
        q = (q_counts + alpha) / (q_counts.sum() + alpha * b)
        return p, q

    if method == "floor":
        if not 0 < epsilon < 1:
            raise ValueError("floor 平滑的 epsilon 必须在 (0, 1) 内")
        p = p_counts / p_counts.sum() if p_counts.sum() > 0 else p_counts.copy()
        q = q_counts / q_counts.sum() if q_counts.sum() > 0 else q_counts.copy()
        p = np.maximum(p, epsilon)
        q = np.maximum(q, epsilon)
        return p / p.sum(), q / q.sum()

    raise ValueError(f"未知平滑方法: {method!r}，可选 none/laplace/floor")


def psi(
    baseline_freq: np.ndarray,
    current_freq: np.ndarray,
) -> tuple[float, np.ndarray]:
    """计算 PSI 与逐桶贡献。

    约定（与常见 PSI 实现一致）：
    任一侧频率为 0 而另一侧非 0 时，对应桶贡献为 ``+inf``；
    两侧均为 0 时贡献为 0。返回的总 PSI 是各桶贡献之和。
    """
    p = np.asarray(baseline_freq, dtype=float)
    q = np.asarray(current_freq, dtype=float)
    if p.shape != q.shape:
        raise ValueError("基线与当前频率长度不一致")

    out = np.zeros(p.shape[0], dtype=float)
    both_pos = (p > 0) & (q > 0)
    out[both_pos] = (q[both_pos] - p[both_pos]) * np.log(
        q[both_pos] / p[both_pos]
    )
    one_zero = ((p == 0) ^ (q == 0)) & ((p + q) > 0)
    out[one_zero] = np.inf
    # 理论上 PSI 各项非负；浮点误差可能产生极小负值，截断到 0
    out = np.where((out == 0) | np.isinf(out), out, np.maximum(out, 0.0))
    return float(out.sum()), out


def js_divergence(
    baseline_freq: np.ndarray,
    current_freq: np.ndarray,
    base: float = 2.0,
) -> float:
    """Jensen–Shannon 散度；base=2 时上界为 1。

    频率必须各自求和为 1（请使用 :func:`smooth_distribution` 的输出）。
    若任一侧退化为全 0（不平滑且无观测），返回 NaN——无法定义。
    """
    p = np.asarray(baseline_freq, dtype=float)
    q = np.asarray(current_freq, dtype=float)
    if p.sum() == 0 or q.sum() == 0:
        return float("nan")
    m = 0.5 * (p + q)

    def _kl(a: np.ndarray, b: np.ndarray) -> float:
        mask = a > 0
        return float(np.sum(a[mask] * np.log(a[mask] / b[mask])))

    raw = 0.5 * _kl(p, m) + 0.5 * _kl(q, m)
    if base == np.e:
        return max(raw, 0.0)
    return max(raw / np.log(base), 0.0)


def total_variation(
    baseline_freq: np.ndarray,
    current_freq: np.ndarray,
) -> float:
    """总变差距离 0.5 * sum|p - q|；全 0 一侧时返回 NaN。"""
    p = np.asarray(baseline_freq, dtype=float)
    q = np.asarray(current_freq, dtype=float)
    if p.sum() == 0 or q.sum() == 0:
        return float("nan")
    return float(0.5 * np.abs(p - q).sum())


def interpret_psi(value: float) -> str:
    """按业界经验规则给 PSI 打标。

    明确声明：0.1 / 0.25 只是常用的工程启发式切分，
    **不是统计显著性或“无漂移”的证明**，小样本下尤不可靠。
    """
    if value is None or np.isnan(value):
        return "undefined"
    if np.isinf(value):
        return "severe_drift_rule_of_thumb"
    if value < PSI_LITTLE_DRIFT:
        return "little_drift_rule_of_thumb"
    if value < PSI_SOME_DRIFT:
        return "some_drift_rule_of_thumb"
    return "severe_drift_rule_of_thumb"


def _same_bins(a: FixedBins, b: FixedBins) -> bool:
    """FixedBins 含 ndarray，不能直接用 == 比较。"""
    return (
        a.n_bins == b.n_bins
        and a.min_value == b.min_value
        and a.max_value == b.max_value
        and np.array_equal(a.edges, b.edges)
    )


def compute_drift(
    baseline: BinCounts,
    current: BinCounts,
    feature: str = "feature",
    smoothing: SmoothingMethod = "laplace",
    alpha: float = DEFAULT_ALPHA,
    epsilon: float = DEFAULT_EPSILON,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> DriftResult:
    """基于同一边界的两次桶计数计算漂移指标。"""
    if not _same_bins(baseline.bins, current.bins):
        raise ValueError("基线与当前窗口必须使用同一套 FixedBins 边界")

    labels_full = bin_labels(baseline.bins)
    labels = labels_full[:-1]  # 缺值桶不进 PSI

    p_counts = baseline.counts[:-1]
    q_counts = current.counts[:-1]
    p_freq, q_freq = smooth_distribution(
        p_counts,
        q_counts,
        method=smoothing,
        alpha=alpha,
        epsilon=epsilon,
    )
    psi_value, psi_per_bin = psi(p_freq, q_freq)
    js_value = js_divergence(p_freq, q_freq)
    tvd_value = total_variation(p_freq, q_freq)

    miss_p = missing_rate(baseline)
    miss_q = missing_rate(current)

    notes: list[str] = []
    all_missing = current.n_observed == 0
    if all_missing:
        notes.append(
            "当前窗口没有任何非缺失观测；频率由平滑规则构造，"
            "PSI 仅反映“信息缺失”的极端假设，不能解读为真实分布漂移"
        )
    if baseline.n_observed == 0:
        notes.append("基线没有任何非缺失观测，指标不可定义")
    small = (
        baseline.n_observed < min_sample
        or current.n_observed < min_sample
    )
    if small:
        notes.append(
            f"观测数低于启发式下限 {min_sample}，指标方差大，"
            "阈值打标不构成统计结论"
        )
    if smoothing == "none" and np.isinf(psi_value):
        notes.append(
            "未做平滑且存在一侧空桶，PSI=inf；"
            "可改用 laplace/floor 平滑获得有限值（平滑本身是工程约定）"
        )

    return DriftResult(
        feature=feature,
        bins=baseline.bins,
        labels=labels,
        baseline_counts=p_counts,
        current_counts=q_counts,
        baseline_freq=p_freq,
        current_freq=q_freq,
        psi_per_bin=psi_per_bin,
        psi=psi_value,
        js_divergence=js_value,
        tvd=tvd_value,
        missing_rate_baseline=miss_p,
        missing_rate_current=miss_q,
        missing_rate_delta=miss_q - miss_p,
        n_baseline=baseline.n_total,
        n_current=current.n_total,
        n_baseline_observed=baseline.n_observed,
        n_current_observed=current.n_observed,
        smoothing=smoothing,
        alpha=alpha if smoothing == "laplace" else None,
        epsilon=epsilon if smoothing == "floor" else None,
        small_sample=small,
        current_all_missing=all_missing,
        notes=notes,
    )
