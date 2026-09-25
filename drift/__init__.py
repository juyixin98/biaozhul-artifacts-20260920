"""特征统计漂移监测（数值特征）。

只依赖 NumPy，提供：

- :func:`fit_fixed_bins` / :func:`assign_counts`：固定分箱、缺值桶、溢出桶；
- :func:`compute_drift`：PSI 等分布差异指标与平滑规则；
- :mod:`drift.synthetic`：可复现合成数据；
- :mod:`drift.service`：基于标准库 http.server 的本地 HTTP 服务。
"""
from .binning import (
    FixedBins,
    BinCounts,
    fit_fixed_bins,
    assign_counts,
    bin_labels,
)
from .metrics import (
    DriftResult,
    compute_drift,
    smooth_distribution,
    psi,
)

__all__ = [
    "FixedBins",
    "BinCounts",
    "fit_fixed_bins",
    "assign_counts",
    "bin_labels",
    "DriftResult",
    "compute_drift",
    "smooth_distribution",
    "psi",
]
