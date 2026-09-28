"""独立参考答案 —— 全部由成熟库产生, 不经被测内核。

- :func:`scipy_reference_eigh`: SciPy 封装的 LAPACK (``dsyevr`` 系)
  双精度参考, 用于常规对照。
- :func:`mpmath_reference_eigh`: mpmath 任意精度对称特征分解,
  用于近退化 / 尺度悬殊等双精度难以判读的场景, 给出独立高精度真值。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.linalg

from mpmath import mp


@dataclass(frozen=True)
class ReferenceEigh:
    eigenvalues: np.ndarray   # float64 (mpmath 结果按其精度四舍五入下来)
    eigenvectors: np.ndarray
    source: str
    precision_digits: int


def scipy_reference_eigh(matrix: np.ndarray) -> ReferenceEigh:
    """LAPACK (SciPy ``eigh``) 参考分解。"""
    eigvals, eigvecs = scipy.linalg.eigh(matrix)
    return ReferenceEigh(
        eigenvalues=np.asarray(eigvals, dtype=np.float64),
        eigenvectors=np.asarray(eigvecs, dtype=np.float64),
        source="scipy.linalg.eigh(LAPACK dsyevd)",
        precision_digits=15,
    )


def mpmath_reference_eigh(
    matrix: np.ndarray, precision_digits: int = 80
) -> ReferenceEigh:
    """mpmath 任意精度对称特征分解 (独立算法路径)。

    mpmath 使用与本服务 Householder/QL 完全不同的实现, 因此其输出可作为
    高精度独立真值; 返回 float64 数组仅为便于与被测结果做数值比较。
    """
    n = matrix.shape[0]
    with mp.workdps(precision_digits):
        mp_matrix = mp.matrix(matrix.tolist())
        raw_vals, raw_vecs = mp.eigsy(mp_matrix)
        eigvals = np.array(
            [float(mp.nstr(raw_vals[i, 0], 25)) for i in range(n)],
            dtype=np.float64,
        )
        eigvecs = np.array(
            [[float(mp.nstr(raw_vecs[i, j], 25)) for j in range(n)]
             for i in range(n)],
            dtype=np.float64,
        )
    # eigsy 通常已升序, 保险起见重排
    order = np.argsort(eigvals)
    return ReferenceEigh(
        eigenvalues=eigvals[order],
        eigenvectors=eigvecs[:, order],
        source=f"mpmath.mp.eigsy({precision_digits} digits)",
        precision_digits=precision_digits,
    )
