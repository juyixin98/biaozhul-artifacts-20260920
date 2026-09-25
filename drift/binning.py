"""固定宽度分箱：显式处理缺值桶与两侧溢出桶。

桶布局（共 ``n_bins + 3`` 个）::

    [underflow] [bin_0] [bin_1] ... [bin_{n-1}] [overflow] [missing]

- 内部桶宽度相等，边界来自基线数据（等宽网格，由 min/max 决定）；
- ``-inf`` 归入下溢出桶，``+inf`` 归入上溢出桶（网格右端点之上全部溢出）；
- ``NaN`` 与 ``None`` 归入缺值桶；
- 基线与当前窗口使用同一套边界，保证桶可逐桶对比。
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

import numpy as np

MISSING_LABEL = "missing"
UNDERFLOW_LABEL = "underflow"
OVERFLOW_LABEL = "overflow"


@dataclass(frozen=True)
class FixedBins:
    """等宽分箱定义。

    属性：
        n_bins: 内部桶数量（不含两个溢出桶和缺值桶）。
        edges: 长度 ``n_bins + 1`` 的单调递增边界；内部桶 i 为
            ``[edges[i], edges[i+1])``，最后一个内部桶右端点闭。
        min_value/max_value: 建箱所用的基线最小/最大值。
    """

    n_bins: int
    edges: np.ndarray
    min_value: float
    max_value: float

    @property
    def total_bins(self) -> int:
        """含溢出桶与缺值桶的总桶数。"""
        return self.n_bins + 3

    @property
    def width(self) -> float:
        return float(self.edges[1] - self.edges[0])

    def to_dict(self) -> dict:
        return {
            "n_bins": self.n_bins,
            "edges": [float(x) for x in self.edges],
            "min_value": float(self.min_value),
            "max_value": float(self.max_value),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FixedBins":
        edges = np.asarray(data["edges"], dtype=float)
        return cls(
            n_bins=int(data["n_bins"]),
            edges=edges,
            min_value=float(data["min_value"]),
            max_value=float(data["max_value"]),
        )


@dataclass(frozen=True)
class BinCounts:
    """一次窗口赋值后的逐桶计数。"""

    bins: FixedBins
    counts: np.ndarray  # 长度 total_bins 的整数计数

    @property
    def n_observed(self) -> int:
        """非缺失观测数（含溢出桶）。"""
        return int(self.counts[:-1].sum())

    @property
    def n_missing(self) -> int:
        return int(self.counts[-1])

    @property
    def n_total(self) -> int:
        return int(self.counts.sum())

    def to_dict(self) -> dict:
        return {
            "bins": self.bins.to_dict(),
            "counts": [int(x) for x in self.counts],
        }


def _to_float_array(values: Iterable[float]) -> np.ndarray:
    """把输入转为浮点 ndarray；None / NaN 统一记为 NaN。

    非数值、布尔、无穷字符串等非法输入直接抛错（在系统边界快速失败）。
    """
    arr = np.asarray(list(values), dtype=object)
    if arr.ndim != 1:
        raise ValueError(f"期望一维数值序列，得到 ndim={arr.ndim}")
    out = np.empty(arr.shape[0], dtype=float)
    for i, v in enumerate(arr):
        if v is None:
            out[i] = np.nan
            continue
        if isinstance(v, bool):
            # bool 是 int 子类，但把 True/False 当特征值通常是上游错误
            raise ValueError(f"位置 {i} 出现布尔值，请传入数值或 None")
        if isinstance(v, (int, float, np.integer, np.floating)):
            out[i] = float(v)
        else:
            raise ValueError(
                f"位置 {i} 出现非数值类型 {type(v).__name__}: {v!r}"
            )
        if math.isnan(out[i]) or math.isinf(out[i]):
            continue  # NaN/+-inf 是合法的缺值/溢出语义，在赋值阶段处理
    return out


def fit_fixed_bins(
    baseline: Iterable[float],
    n_bins: int = 10,
) -> FixedBins:
    """用基线数据拟合等宽分箱。

    边界只由非缺失、有限的基线值决定。

    - ``n_bins`` 必须 >= 1；
    - 基线有效值少于 2 个、或全部相等时退化为单点网格，
      调用方应改用更小的桶数或先核查特征；
    - 基线中的 inf 不参与定界（否则边界变成 inf），但会计入溢出桶；
    - 基线中的 NaN/None 只进入缺值桶。
    """
    if not isinstance(n_bins, (int, np.integer)) or isinstance(n_bins, bool):
        raise ValueError("n_bins 必须是正整数")
    if n_bins < 1:
        raise ValueError(f"n_bins 必须 >= 1，得到 {n_bins}")

    arr = _to_float_array(baseline)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        raise ValueError("基线没有任何有限非缺失值，无法确定分箱边界")

    lo = float(finite.min())
    hi = float(finite.max())
    if lo == hi:
        # 单点退化网格：以该点为唯一中心式边界 [x, x]，
        # 任何不等于该点的值都落入溢出桶；调用方会在标签中看到这一点。
        edges = np.array([lo, lo], dtype=float)
        return FixedBins(
            n_bins=1, edges=edges, min_value=lo, max_value=hi
        )

    edges = np.linspace(lo, hi, n_bins + 1)
    # 消除浮点误差可能导致的非单调
    edges = np.maximum.accumulate(edges)
    return FixedBins(
        n_bins=n_bins,
        edges=edges,
        min_value=lo,
        max_value=hi,
    )


def assign_counts(bins: FixedBins, values: Iterable[float]) -> BinCounts:
    """按既定桶统计单个窗口的计数（基线与当前窗口共用此函数）。"""
    arr = _to_float_array(values)

    total = bins.total_bins
    counts = np.zeros(total, dtype=np.int64)

    nan_mask = np.isnan(arr)
    counts[total - 1] = int(nan_mask.sum())

    finite_or_inf = arr[~nan_mask]
    if finite_or_inf.size:
        # np.digitize(right=False) 返回 i 表示 edges[i-1] <= x < edges[i]：
        #   0 -> x < edges[0]（下溢）；len(edges) -> x >= edges[-1]；
        # digitize 的返回下标与计数布局恰好对齐：count[0] 下溢、
        # count[1..n_bins] 内部桶、count[n_bins+1] 上溢。
        # +/-inf 天然落在 0 / len(edges)，无需特判。
        idx = np.digitize(finite_or_inf, bins.edges, right=False)
        # 最后一个内部桶右端点闭合：仅“恰好等于 edges[-1]”留在桶内，
        # 严格大于基线最大值的有限值仍进上溢出桶。
        at_right_edge = (idx == len(bins.edges)) & (
            finite_or_inf == bins.edges[-1]
        )
        idx = np.where(at_right_edge, bins.n_bins, idx)
        counts += np.bincount(idx, minlength=total).astype(np.int64)

    return BinCounts(bins=bins, counts=counts)


def bin_labels(bins: FixedBins) -> list[str]:
    """生成与计数数组对齐的人类可读桶标签。"""
    labels: list[str] = [UNDERFLOW_LABEL]
    edges = bins.edges
    for i in range(bins.n_bins):
        lo, hi = float(edges[i]), float(edges[i + 1])
        if i == bins.n_bins - 1:
            labels.append(f"[{lo:.6g},{hi:.6g}]")
        else:
            labels.append(f"[{lo:.6g},{hi:.6g})")
    labels.append(OVERFLOW_LABEL)
    labels.append(MISSING_LABEL)
    return labels


def empirical_frequencies(counts: BinCounts) -> np.ndarray:
    """非缺失桶上的经验频率（溢出桶参与分布；缺值桶单独报告，不进 PSI）。

    全部缺失（或空窗口）时返回全 0，并由指标层决定如何处理。
    """
    observed = counts.n_observed
    if observed == 0:
        return np.zeros(counts.counts[:-1].shape[0], dtype=float)
    return counts.counts[:-1].astype(float) / observed


def missing_rate(counts: BinCounts) -> float:
    """缺值率；空窗口约定为 1.0（没有任何有效观测）。"""
    if counts.n_total == 0:
        return 1.0
    return counts.n_missing / counts.n_total
