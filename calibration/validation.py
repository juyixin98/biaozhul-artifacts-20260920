"""输入校验：在系统边界把非法数据挡掉，并给出明确错误信息。"""

from __future__ import annotations

import numpy as np

from .errors import CalibrationError


def _to_finite_array(raw, name: str) -> np.ndarray:
    """把入参转为一维 float64 数组，拒绝 None、非数值、NaN/Inf。"""
    try:
        arr = np.asarray(raw, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise CalibrationError(
            "INVALID_INPUT", f"{name} 必须是数值数组: {exc}"
        ) from exc
    if arr.ndim != 1:
        raise CalibrationError(
            "INVALID_INPUT", f"{name} 必须是一维数组，实际维度为 {arr.ndim}"
        )
    if arr.size == 0:
        raise CalibrationError("EMPTY_INPUT", f"{name} 不能为空")
    if not np.all(np.isfinite(arr)):
        raise CalibrationError(
            "INVALID_INPUT", f"{name} 中存在 NaN 或 Inf 等非有限值"
        )
    return arr


def validate_inputs(y_true, proba, sample_weight=None) -> tuple:
    """校验并归一化标签、概率与样本权重。

    Returns
    -------
    (y, p, w)
        三者均为长度相同的一维 float64 数组；``y`` 取值 0/1，
        ``p`` 取值于 ``[0, 1]``，``w`` 为正数。
    """
    y = _to_finite_array(y_true, "y_true")
    p = _to_finite_array(proba, "proba")

    if y.shape != p.shape:
        raise CalibrationError(
            "LENGTH_MISMATCH",
            f"y_true 与 proba 长度必须一致: {y.shape[0]} != {p.shape[0]}",
        )

    # 标签：只接受二分类 0/1
    if not np.all((y == 0) | (y == 1)):
        raise CalibrationError(
            "INVALID_LABEL", "y_true 只能包含二分类标签 0 和 1"
        )

    # 概率：闭区间 [0, 1] 校验
    out_of_range = np.where((p < 0.0) | (p > 1.0))[0]
    if out_of_range.size > 0:
        idx = int(out_of_range[0])
        raise CalibrationError(
            "INVALID_PROBABILITY",
            f"proba 必须落在 [0, 1]，第 {idx} 个样本的概率为 {float(p[idx])}",
        )

    if sample_weight is None:
        w = np.ones_like(p)
    else:
        w = _to_finite_array(sample_weight, "sample_weight")
        if w.shape != p.shape:
            raise CalibrationError(
                "LENGTH_MISMATCH",
                f"sample_weight 与 proba 长度必须一致: "
                f"{w.shape[0]} != {p.shape[0]}",
            )
        if np.any(w < 0):
            raise CalibrationError(
                "INVALID_WEIGHT", "sample_weight 不能包含负数"
            )
        if not np.any(w > 0):
            raise CalibrationError(
                "INVALID_WEIGHT", "sample_weight 至少要有一个正权重"
            )

    return y, p, w


def validate_n_bins(n_bins) -> int:
    """校验 ECE 分箱数量。"""
    if isinstance(n_bins, bool) or not isinstance(n_bins, int):
        raise CalibrationError(
            "INVALID_N_BINS", f"n_bins 必须是正整数，收到 {n_bins!r}"
        )
    if n_bins <= 0:
        raise CalibrationError(
            "INVALID_N_BINS", f"n_bins 必须是正整数，收到 {n_bins}"
        )
    return n_bins


def validate_epsilon(epsilon) -> float:
    """校验概率端点裁剪幅度。"""
    eps = float(epsilon)
    if not np.isfinite(eps) or not (0.0 <= eps < 0.5):
        raise CalibrationError(
            "INVALID_EPSILON",
            f"epsilon 必须落在 [0, 0.5)，收到 {epsilon!r}",
        )
    return eps
