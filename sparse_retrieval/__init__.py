"""稀疏向量相似检索后端服务。

仅依赖 NumPy(可选, 用于合成数据生成)与 Python 标准库,
不下载任何外部模型或数据集。
"""

from .vector import (
    SparseVector,
    SparseVectorError,
    l2_norm,
    merge_entries,
    normalize_entries,
    parse_entries,
)
from .scoring import brute_force_topk, cosine, sparse_dot
from .index import InvertedIndex, IndexError, SearchResult, ScoredDoc

__all__ = [
    "SparseVector",
    "SparseVectorError",
    "l2_norm",
    "merge_entries",
    "normalize_entries",
    "parse_entries",
    "brute_force_topk",
    "cosine",
    "sparse_dot",
    "InvertedIndex",
    "IndexError",
    "SearchResult",
    "ScoredDoc",
]
