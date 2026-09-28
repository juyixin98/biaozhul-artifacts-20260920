"""数值输入校验。

- 解析嵌套序列为 float64 二维方阵
- 拒绝 NaN / Inf
- 规模受配置约束
- 对称性按 **相对容差** 验证: ``||A - A^T||_F <= tol * ||A||_F``
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from .config import EigenConfig
from .errors import AsymmetryError, InvalidMatrixError


def parse_matrix(payload: Any, config: EigenConfig) -> np.ndarray:
    """把外部输入解析为 ``float64`` 方阵并完成全部输入校验。

    Args:
        payload: 嵌套序列 (行优先), 例如 ``[[1.0, 2.0], [2.0, 1.0]]``。
        config: 规模与容差配置。

    Returns:
        规整后的方阵 (主动做一次对称化拷贝, 但仅在容差通过之后)。

    Raises:
        InvalidMatrixError: 形状 / 类型 / 有限性 / 规模不合法。
        AsymmetryError: 相对容差下不对称。
    """
    matrix = _to_ndarray(payload)
    _check_shape_and_size(matrix, config)

    if not np.all(np.isfinite(matrix)):
        raise InvalidMatrixError(
            "矩阵包含 NaN 或 Inf, 无法进行特征分解。",
            {"non_finite_count": int(np.size(matrix) - np.sum(np.isfinite(matrix)))},
        )

    asymmetry = _relative_asymmetry(matrix)
    if asymmetry["relative_asymmetry"] > config.sym_tol:
        raise AsymmetryError(
            "输入矩阵在相对容差下不满足对称性。",
            {
                "relative_asymmetry": asymmetry["relative_asymmetry"],
                "absolute_asymmetry": asymmetry["absolute_asymmetry"],
                "sym_tol": config.sym_tol,
                "max_offdiagonal_mismatch": asymmetry["max_mismatch"],
                "hint": "检查 A[i][j] 与 A[j][i], 或在配置中放宽 sym_tol。",
            },
        )

    # 容差通过后, 消去上下三角的微小差异, 保证后续算法看到精确对称输入。
    sym = (matrix + matrix.T) * 0.5
    return np.ascontiguousarray(sym, dtype=np.float64)


def _to_ndarray(payload: Any) -> np.ndarray:
    if not isinstance(payload, np.ndarray) and (
        not isinstance(payload, Sequence) or isinstance(payload, (str, bytes))
    ):
        raise InvalidMatrixError(
            "输入必须是行优先的二维数组 (JSON 嵌套数组)。",
            {"received_type": type(payload).__name__},
        )
    try:
        matrix = np.asarray(payload, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise InvalidMatrixError(f"矩阵元素无法解析为实数: {exc}") from exc
    if matrix.ndim != 2:
        raise InvalidMatrixError(
            "矩阵必须是二维的。",
            {"received_ndim": int(matrix.ndim), "shape": list(matrix.shape)},
        )
    return matrix


def _check_shape_and_size(matrix: np.ndarray, config: EigenConfig) -> None:
    rows, cols = matrix.shape
    if rows != cols:
        raise InvalidMatrixError(
            "矩阵必须为方阵。", {"rows": int(rows), "cols": int(cols)}
        )
    if rows < config.min_size or rows > config.max_size:
        raise InvalidMatrixError(
            "矩阵规模超出允许范围。",
            {"size": int(rows), "min_size": config.min_size,
             "max_size": config.max_size},
        )


def _relative_asymmetry(matrix: np.ndarray) -> dict[str, float]:
    diff = matrix - matrix.T
    abs_asym = float(np.linalg.norm(diff, ord="fro"))
    norm = float(np.linalg.norm(matrix, ord="fro"))
    scale = norm if norm > 0.0 else 1.0
    mismatch = float(np.max(np.abs(diff))) if diff.size else 0.0
    return {
        "absolute_asymmetry": abs_asym,
        "relative_asymmetry": abs_asym / scale,
        "max_mismatch": mismatch,
    }
