"""测试共用工具。"""

from __future__ import annotations

import numpy as np

from sparse_retrieval.vector import SparseVector


def random_sparse(
    rng: np.random.Generator,
    dim: int,
    nnz: int,
    *,
    neg_prob: float = 0.3,
) -> SparseVector:
    """生成随机稀疏向量(已合并)。"""
    idx = rng.choice(dim, size=min(nnz, dim), replace=False)
    val = rng.normal(0.0, 1.0, size=idx.size)
    val *= np.where(rng.random(idx.size) < neg_prob, -1.0, 1.0)
    return SparseVector.create(idx.tolist(), val.tolist(), dim)
