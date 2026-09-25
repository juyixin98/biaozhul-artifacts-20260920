"""输入校验:所有指标计算前的统一边界检查。

非法输入一律抛出 ValueError,绝不静默通过。
"""

from __future__ import annotations

import numpy as np


def _as_1d_float(name: str, values: object) -> np.ndarray:
    """把输入转成一维 float64 数组,失败时抛出带字段名的 ValueError。"""
    try:
        arr = np.asarray(values, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是可转换为浮点数的序列: {exc}") from exc
    if arr.ndim != 1:
        raise ValueError(f"{name} 必须是一维序列, 实际维度为 {arr.ndim}")
    if arr.size == 0:
        raise ValueError(f"{name} 不能为空")
    return arr


def validate_inputs(
    y_true: object,
    y_prob: object,
    sample_weight: object | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """校验标签、预测概率与可选样本权重,返回 (y, p, w) 三个数组。

    规则:
    - y_true 必须只含 0/1;
    - y_prob 必须有限且落在闭区间 [0, 1](端点 0/1 合法,由端点策略处理);
    - sample_weight 缺省时为全 1;必须有限且非负,总权重必须大于 0。
    """
    y = _as_1d_float("y_true", y_true)
    p = _as_1d_float("y_prob", y_prob)

    if y.shape[0] != p.shape[0]:
        raise ValueError(
            f"y_true 与 y_prob 长度不一致: {y.shape[0]} != {p.shape[0]}"
        )

    if not np.all(np.isfinite(y)):
        raise ValueError("y_true 含有 NaN 或无穷值")
    if not np.all((y == 0.0) | (y == 1.0)):
        raise ValueError("y_true 只能取 0 或 1(二分类)")

    if not np.all(np.isfinite(p)):
        raise ValueError("y_prob 含有 NaN 或无穷值")
    if np.any(p < 0.0) or np.any(p > 1.0):
        bad = p[(p < 0.0) | (p > 1.0)]
        raise ValueError(
            f"y_prob 必须落在 [0, 1] 区间内, 发现非法值: {bad[:5].tolist()}"
        )

    if sample_weight is None:
        w = np.ones_like(y)
    else:
        w = _as_1d_float("sample_weight", sample_weight)
        if w.shape[0] != y.shape[0]:
            raise ValueError(
                f"sample_weight 与 y_true 长度不一致: {w.shape[0]} != {y.shape[0]}"
            )
        if not np.all(np.isfinite(w)):
            raise ValueError("sample_weight 含有 NaN 或无穷值")
        if np.any(w < 0.0):
            raise ValueError("sample_weight 不能包含负权重")

    total_weight = float(np.sum(w))
    if total_weight <= 0.0:
        raise ValueError("样本权重总和必须大于 0")

    return y, p, w
