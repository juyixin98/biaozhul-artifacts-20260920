"""精确打分: 稀疏点积、余弦与全扫描 TopK(正确性基准)。

零向量语义
----------
余弦相似度要求两向量范数均非零。本项目把"任一侧为零向量"的情况
统一定义为 ``0.0``(不返回 NaN, 也不人为抬成 1), 因此零向量文档
参与检索时名次稳定地落在所有正相关文档之后。

TopK 稳定性
-----------
按 ``(分数降序, 文档 ID 升序)`` 排序:
- 分数严格相等(并列)时, 文档 ID 更小者优先, 结果与数据插入顺序无关;
- 始终恰好返回 ``min(k, 命中文档数)`` 条, 边界上的并列也按 ID 决断,
  不做"同分全部带出"的不确定性扩展。
"""

from __future__ import annotations

import numpy as np

from .vector import SparseVector, l2_norm

__all__ = ["sparse_dot", "cosine", "brute_force_topk"]


def sparse_dot(a: SparseVector, b: SparseVector) -> float:
    """两个已合并稀疏向量的点积(双指针归并, 支持负权重)。"""
    ai = a.indices
    bi = b.indices
    if ai.shape[0] == 0 or bi.shape[0] == 0:
        return 0.0
    # 遍历较短的链, 在较长链上二分查找
    if ai.shape[0] > bi.shape[0]:
        a, b = b, a
        ai, bi = a.indices, b.indices
    total = 0.0
    pos = np.searchsorted(bi, ai)
    valid = pos < bi.shape[0]
    if np.any(valid):
        hit_pos = pos[valid]
        match = bi[hit_pos] == ai[valid]
        if np.any(match):
            av = a.values[valid][match]
            bv = b.values[hit_pos[match]]
            # 维度升序累加, 与倒排索引路径保持完全一致的舍入顺序
            total = float(np.dot(av.astype(np.float64), bv.astype(np.float64)))
    return total


def cosine(a: SparseVector, b: SparseVector) -> float:
    """余弦相似度; 任一向量为零向量时定义为 0.0。"""
    na = l2_norm(a.values)
    nb = l2_norm(b.values)
    if na == 0.0 or nb == 0.0:
        return 0.0
    return sparse_dot(a, b) / (na * nb)


def brute_force_topk(
    query: SparseVector,
    docs: list[SparseVector],
    k: int,
) -> list[tuple[int, float]]:
    """全扫描基准: 逐文档计算余弦, 返回 ``[(doc_id, score), ...]``。

    文档 ID 即其在 ``docs`` 中的下标。分数为 float64,
    排序键为 ``(-score, doc_id)``, 保证并列稳定。
    """
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise ValueError("k 必须是正整数")
    n = len(docs)
    scores = np.zeros(n, dtype=np.float64)
    qnorm = l2_norm(query.values)
    for doc_id, doc in enumerate(docs):
        if doc.dim != query.dim:
            raise ValueError(
                f"文档 {doc_id} 的维度 {doc.dim} 与查询维度 {query.dim} 不一致"
            )
        if qnorm == 0.0:
            scores[doc_id] = 0.0
            continue
        dnorm = l2_norm(doc.values)
        if dnorm == 0.0:
            scores[doc_id] = 0.0
        else:
            scores[doc_id] = sparse_dot(query, doc) / (qnorm * dnorm)

    order = np.lexsort((np.arange(n), -scores))
    top = order[: min(k, n)]
    return [(int(i), float(scores[i])) for i in top]
