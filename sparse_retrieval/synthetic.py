"""Reproducible synthetic sparse data, generated with NumPy only.

No external models or datasets are downloaded; everything derives from a
seeded ``numpy.random.Generator`` so runs are bit-for-bit reproducible.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np

from sparse_retrieval.vector import SparseVector


def random_sparse_pairs(
    rng: np.random.Generator,
    dim: int,
    nnz: int,
    negative_ratio: float = 0.3,
) -> List[Tuple[int, float]]:
    """``nnz`` distinct dimensions in ``[0, dim)`` with weights in (-1, 1).

    Roughly ``negative_ratio`` of the weights are negative, so corpora
    exercise negative scores by construction.
    """
    nnz = min(nnz, dim)
    dims = rng.choice(dim, size=nnz, replace=False)
    weights = rng.uniform(0.05, 1.0, size=nnz)
    sign_flip = rng.uniform(0.0, 1.0, size=nnz) < negative_ratio
    weights[sign_flip] *= -1.0
    return [(int(d), float(w)) for d, w in zip(dims, weights)]


def random_corpus(
    seed: int,
    num_docs: int,
    dim: int,
    min_nnz: int = 5,
    max_nnz: int = 50,
    negative_ratio: float = 0.3,
) -> Dict[int, SparseVector]:
    """A reproducible corpus: doc ids ``0 .. num_docs-1``."""
    rng = np.random.default_rng(seed)
    docs: Dict[int, SparseVector] = {}
    for doc_id in range(num_docs):
        nnz = int(rng.integers(min_nnz, max_nnz + 1))
        docs[doc_id] = SparseVector(
            random_sparse_pairs(rng, dim, nnz, negative_ratio)
        )
    return docs


def random_queries(
    seed: int,
    num_queries: int,
    dim: int,
    min_nnz: int = 3,
    max_nnz: int = 20,
    negative_ratio: float = 0.3,
) -> List[SparseVector]:
    rng = np.random.default_rng(seed)
    queries: List[SparseVector] = []
    for _ in range(num_queries):
        nnz = int(rng.integers(min_nnz, max_nnz + 1))
        queries.append(
            SparseVector(random_sparse_pairs(rng, dim, nnz, negative_ratio))
        )
    return queries


def to_dense(vector: SparseVector, dim: int) -> np.ndarray:
    """Dense ``float64`` row for NumPy-side verification."""
    out = np.zeros(dim, dtype=np.float64)
    for d, w in vector:
        out[d] = w
    return out
