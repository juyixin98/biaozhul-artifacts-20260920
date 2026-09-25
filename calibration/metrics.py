"""二分类概率校准指标的 NumPy 实现。

三个指标的数学定义（设样本数为 n，标签 ``y_i ∈ {0,1}``，
正类预测概率 ``p_i``，样本权重 ``w_i``，总权重
``W = Σ_i w_i``）：

Brier 分数（均方概率误差，越小越好）::

    BS = (1/W) * Σ_i w_i * (p_i - y_i)²

对数损失（二分类交叉熵，越小越好）::

    LL = -(1/W) * Σ_i w_i * [ y_i*ln(p_i) + (1-y_i)*ln(1-p_i) ]

概率端点 0/1 会令对数无定义，支持三种策略（``endpoint_strategy``）：

- ``"clip"``（默认）：把每个对数项的概率参数裁剪到不小于 ``epsilon``
  （``y=1`` 项裁剪 ``p``，``y=0`` 项裁剪 ``1-p``，``epsilon`` 默认 1e-15）。
  因此端点上"预测正确"（``y=1,p=1`` 或 ``y=0,p=0``）的损失恰为 0，
  "预测错误"（``y=1,p=0`` 或 ``y=0,p=1``）的损失恰为 ``-ln(epsilon)``；
- ``"error"``：出现 0/1 端点且对应损失发散时直接报错；
- ``"ignore"``：跳过损失发散的样本，仅对其余样本按权重归一化
  （``0*ln(0)`` 按约定视为 0）。

等宽分箱 ECE（Expected Calibration Error，越小越好）::

    ECE = Σ_{b: W_b>0} (W_b / W_nonempty) * | acc_b - conf_b |

其中 ``W_b`` 为第 b 个箱子的样本权重和，``acc_b`` 为箱内加权正类比例，
``conf_b`` 为箱内加权平均概率。空箱（``W_b == 0``）不参与求和，
归一化分母 ``W_nonempty`` 只计非空箱的权重。箱边界等宽：
``[0, 1/n_bins), [1/n_bins, 2/n_bins), …, [(n_bins-1)/n_bins, 1]``，
概率 1.0 归入最后一个箱子。
"""

from __future__ import annotations

import numpy as np

from .errors import CalibrationError
from .validation import validate_epsilon, validate_inputs, validate_n_bins

CLIP_EPSILON = 1e-15
ENDPOINT_STRATEGIES = ("clip", "error", "ignore")


def brier_score(y_true, proba, sample_weight=None) -> float:
    """加权 Brier 分数。"""
    y, p, w = validate_inputs(y_true, proba, sample_weight)
    total_weight = w.sum()
    return float(np.sum(w * (p - y) ** 2) / total_weight)


def log_loss(
    y_true,
    proba,
    sample_weight=None,
    endpoint_strategy: str = "clip",
    epsilon: float = CLIP_EPSILON,
) -> float:
    """加权二分类对数损失，端点处理见模块文档。"""
    y, p, w = validate_inputs(y_true, proba, sample_weight)

    if endpoint_strategy not in ENDPOINT_STRATEGIES:
        raise CalibrationError(
            "INVALID_STRATEGY",
            f"endpoint_strategy 必须是 {ENDPOINT_STRATEGIES} 之一，"
            f"收到 {endpoint_strategy!r}",
        )
    eps = validate_epsilon(epsilon)

    # 每个样本的损失是否发散：正标签遇 p=0，或负标签遇 p=1。
    divergent = ((y == 1) & (p == 0.0)) | ((y == 0) & (p == 1.0))

    if endpoint_strategy == "error" and np.any(divergent & (w > 0)):
        idx = int(np.where(divergent & (w > 0))[0][0])
        raise CalibrationError(
            "ENDPOINT_LOSS",
            f"第 {idx} 个样本的对数损失发散：标签 {int(y[idx])}，"
            f"概率 {p[idx]:.1f}（endpoint_strategy='error'）",
        )

    if endpoint_strategy == "clip":
        # 逐项裁剪：正类项保证 p >= eps，负类项保证 1-p >= eps。
        # 这样端点上"预测正确"的损失恰为 0（不会因对称裁剪 1-eps 而被
        # 误伤），"预测错误"的损失恰为 -ln(eps)。
        # np.where 会同时求值两个分支，故显式忽略 log(0) 告警；
        # eps=0 时发散项按 NumPy 语义得到 inf。
        with np.errstate(divide="ignore"):
            per_sample = np.where(
                y > 0,
                -np.log(np.maximum(p, eps)),
                -np.log(np.maximum(1.0 - p, eps)),
            )
        denom = w.sum()
    else:  # ignore：约定 0*ln(0) = 0，跳过发散样本
        finite_mask = ~divergent
        if not np.any(finite_mask & (w > 0)):
            raise CalibrationError(
                "ENDPOINT_LOSS",
                "endpoint_strategy='ignore' 时所有正权重样本的损失都发散，"
                "无法计算对数损失",
            )
        p_f, y_f, w_f = p[finite_mask], y[finite_mask], w[finite_mask]
        # 非发散保证：y=1 时 p>0、y=0 时 p<1，故以下两个对数都有限。
        per_sample = np.empty_like(p_f)
        pos = y_f > 0
        per_sample[pos] = -np.log(p_f[pos])
        per_sample[~pos] = -np.log1p(-p_f[~pos])
        w, denom = w_f, w_f.sum()

    return float(np.sum(w * per_sample) / denom)


def expected_calibration_error(
    y_true,
    proba,
    sample_weight=None,
    n_bins: int = 10,
    return_bins: bool = False,
):
    """等宽分箱的加权 ECE。

    Parameters
    ----------
    return_bins:
        为 True 时额外返回每个箱子的统计明细（含空箱标记）。
    """
    y, p, w = validate_inputs(y_true, proba, sample_weight)
    n_bins = validate_n_bins(n_bins)

    # 概率 1.0 单独并入最后一箱，其余按 floor(p * n_bins) 落箱。
    bin_ids = np.minimum((p * n_bins).astype(np.int64), n_bins - 1)

    total_weight = w.sum()
    ece = 0.0
    bins = []
    for b in range(n_bins):
        mask = bin_ids == b
        w_b = w[mask].sum()
        lo, hi = b / n_bins, (b + 1) / n_bins
        if w_b <= 0.0:
            bins.append(
                {
                    "bin": b,
                    "range": [lo, hi],
                    "count": 0,
                    "weight": 0.0,
                    "mean_proba": None,
                    "positive_rate": None,
                    "gap": None,
                }
            )
            continue
        conf_b = float(np.sum(w[mask] * p[mask]) / w_b)
        acc_b = float(np.sum(w[mask] * y[mask]) / w_b)
        gap = abs(acc_b - conf_b)
        # 非空箱权重 / 全部非空箱权重（等宽箱在样本全覆盖时即 w_b/total_weight）
        ece += (w_b / total_weight) * gap
        bins.append(
            {
                "bin": b,
                "range": [lo, hi],
                "count": int(mask.sum()),
                "weight": float(w_b),
                "mean_proba": conf_b,
                "positive_rate": acc_b,
                "gap": gap,
            }
        )

    ece = float(ece)
    if return_bins:
        return ece, bins
    return ece


def evaluate_calibration(
    y_true,
    proba,
    sample_weight=None,
    n_bins: int = 10,
    endpoint_strategy: str = "clip",
    epsilon: float = CLIP_EPSILON,
) -> dict:
    """一次性计算全部校准指标，返回可 JSON 序列化的结果字典。"""
    n_bins = validate_n_bins(n_bins)
    bs = brier_score(y_true, proba, sample_weight)
    ll = log_loss(y_true, proba, sample_weight, endpoint_strategy, epsilon)
    ece, bins = expected_calibration_error(
        y_true, proba, sample_weight, n_bins=n_bins, return_bins=True
    )
    _, _, w = validate_inputs(y_true, proba, sample_weight)
    return {
        "n_samples": int(len(np.asarray(y_true))),
        "total_weight": float(w.sum()),
        "n_bins": n_bins,
        "endpoint_strategy": endpoint_strategy,
        "brier_score": bs,
        "log_loss": ll,
        "ece": ece,
        "bins": bins,
    }
