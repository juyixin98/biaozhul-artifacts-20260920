"""Full-scan (brute force) cosine retrieval — the exactness reference.

Computes cosine similarity between the query and *every* document, with no
pruning. Dot products are accumulated iterating query dimensions in
ascending order per document — the same order as ``InvertedIndex.query`` —
so scores are bitwise identical to the index path, not merely close.
"""

from __future__ import annotations

from typing import Dict, Mapping, Tuple

from sparse_retrieval.index import QueryResult, _topk
from sparse_retrieval.vector import SparseVector


def brute_force_query(
    docs: Mapping[int, SparseVector], vector: SparseVector, k: int
) -> QueryResult:
    """Exact cosine TopK by scanning every document."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError(f"k must be a positive int, got {k!r}")

    q_norm = vector.norm
    scores: Dict[int, float] = {}
    for doc_id, doc_vec in docs.items():
        doc_entries = doc_vec.as_dict()
        dot = 0.0
        for dim, q_weight in vector:
            d_weight = doc_entries.get(dim)
            if d_weight is not None:
                dot += q_weight * d_weight
        denom = q_norm * doc_vec.norm
        scores[doc_id] = dot / denom if denom > 0.0 else 0.0

    k = min(k, len(docs))
    return QueryResult(
        results=_topk(scores, k),
        candidates_examined=len(docs),  # a full scan examines everything
        num_docs=len(docs),
    )
