"""本地合成测试夹具。

所有矩阵都是确定性的 (固定种子) 本地合成数据, 不依赖任何外部业务数据。

关键: 多数夹具通过显式正交混合 ``Q diag(spectrum) Q^T`` 构造,
**特征值真值就是构造参数本身**, 与被测内核无关; 成熟库参考
(SciPy / mpmath) 见 :mod:`eigenservice.reference`。
"""

from __future__ import annotations

import numpy as np


def random_orthogonal(n: int, seed: int) -> np.ndarray:
    """用种子化 QR 分解生成确定性正交阵。"""
    rng = np.random.default_rng(seed)
    q, r = np.linalg.qr(rng.standard_normal((n, n)))
    # 消除 QR 符号自由度, 保证确定性
    q *= np.sign(np.diag(r))
    return q


def with_spectrum(eigenvalues: list[float], seed: int) -> np.ndarray:
    """由显式谱和确定性正交混合构造对称矩阵 ``Q diag(w) Q^T``。"""
    w = np.asarray(eigenvalues, dtype=np.float64)
    q = random_orthogonal(len(w), seed)
    matrix = (q * w) @ q.T
    return (matrix + matrix.T) * 0.5


def diagonal_matrix() -> np.ndarray:
    return np.diag([-3.0, -1.0, 0.5, 2.0, 7.0])


def repeated_spectrum_matrix(seed: int = 101) -> np.ndarray:
    # 谱: 三重 2.0, 两重 -1.5
    return with_spectrum([2.0, 2.0, 2.0, -1.5, -1.5], seed)


def near_degenerate_matrix(seed: int = 202) -> np.ndarray:
    # 三个间隙 ~1e-10 的近退化特征值, 与其余谱隔开
    return with_spectrum([-5.0, 1.0, 1.0 + 1e-10, 1.0 + 2e-10, 4.0], seed)


def scale_disparity_matrix(seed: int = 303) -> np.ndarray:
    # 跨 12 个数量级的谱
    return with_spectrum([1e6, 1.0, 1e-6], seed)


def random_symmetric(n: int, seed: int = 404) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((n, n))
    return x + x.T


def mildly_asymmetric_matrix(rel_level: float, seed: int = 505) -> np.ndarray:
    """已知相对不对称水平的矩阵 (用于容差分类)。"""
    base = random_symmetric(4, seed)
    perturb = np.zeros_like(base)
    perturb[0, 1] = 1.0
    perturb[1, 0] = -1.0  # 纯反对称, ||perturb||_F = sqrt(2)
    return base + perturb * (rel_level * np.linalg.norm(base, "fro") / np.sqrt(2.0))
