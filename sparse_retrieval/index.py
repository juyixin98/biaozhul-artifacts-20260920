"""Inverted index for exact sparse-vector cosine retrieval.

The index maps each dimension to a posting list of ``(doc_id, weight)``.
A query touches only the posting lists of its own dimensions, so the only
documents scored are *candidates* — documents sharing at least one dimension
with the query. This is the (exactness-preserving) pruning the index
performs; every non-candidate has a dot product of exactly ``0.0`` and
therefore a cosine score of exactly ``0.0``, which is filled in afterwards
so TopK results are identical to a full scan.

TopK ordering is stable: results sort by ``(-score, doc_id)``, so tied
scores are always broken by ascending document id.

Accumulation order contract: dot products are accumulated iterating query
dimensions in ascending order, per document. ``brute_force.brute_force_query``
uses the same order, so index and full-scan scores are bitwise identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from sparse_retrieval.vector import SparseVector


@dataclass(frozen=True)
class QueryResult:
    """One TopK hit plus query-level statistics."""

    results: Tuple[Tuple[int, float], ...]  # ((doc_id, score), ...) sorted
    candidates_examined: int  # docs sharing >= 1 dimension with the query
    num_docs: int  # total docs in the index at query time


def _validate_doc_id(doc_id: object) -> int:
    if isinstance(doc_id, bool) or not isinstance(doc_id, int):
        raise ValueError(f"doc_id must be an int, got {doc_id!r}")
    if doc_id < 0:
        raise ValueError(f"doc_id must be >= 0, got {doc_id}")
    return doc_id


def _topk(scores: Dict[int, float], k: int) -> Tuple[Tuple[int, float], ...]:
    """Stable TopK: descending score, ties broken by ascending doc_id."""
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(ordered[:k])


class InvertedIndex:
    """Exact cosine TopK over sparse vectors via an inverted index."""

    def __init__(self) -> None:
        # dim -> list of (doc_id, weight), kept sorted by doc_id.
        self._postings: Dict[int, List[Tuple[int, float]]] = {}
        self._norms: Dict[int, float] = {}
        self._docs: Dict[int, SparseVector] = {}

    def __len__(self) -> int:
        return len(self._docs)

    @property
    def num_dimensions(self) -> int:
        return len(self._postings)

    def __contains__(self, doc_id: int) -> bool:
        return doc_id in self._docs

    def get(self, doc_id: int) -> SparseVector:
        return self._docs[doc_id]

    def add(self, doc_id: int, vector: SparseVector) -> None:
        """Insert or replace a document. Zero vectors are allowed: they are
        stored (norm 0.0, cosine 0.0 with everything) but post to no list."""
        doc_id = _validate_doc_id(doc_id)
        if not isinstance(vector, SparseVector):
            raise ValueError(f"vector must be a SparseVector, got {vector!r}")
        if doc_id in self._docs:
            self.remove(doc_id)
        for dim, weight in vector:
            self._postings.setdefault(dim, []).append((doc_id, weight))
        # Posting lists are appended in arbitrary doc order; re-sort the
        # lists this document touched. Query results do not depend on
        # posting order (accumulation is keyed by doc_id), but deterministic
        # storage keeps the structure inspectable.
        for dim, _ in vector:
            self._postings[dim].sort(key=lambda pw: pw[0])
        self._docs[doc_id] = vector
        self._norms[doc_id] = vector.norm

    def remove(self, doc_id: int) -> bool:
        """Remove a document. Returns True if it existed."""
        doc_id = _validate_doc_id(doc_id)
        vector = self._docs.pop(doc_id, None)
        if vector is None:
            return False
        self._norms.pop(doc_id, None)
        for dim, _ in vector:
            plist = self._postings[dim]
            plist[:] = [(d, w) for d, w in plist if d != doc_id]
            if not plist:
                del self._postings[dim]
        return True

    def query(self, vector: SparseVector, k: int) -> QueryResult:
        """Exact cosine TopK. ``k`` is clamped to the number of documents."""
        if not isinstance(vector, SparseVector):
            raise ValueError(f"vector must be a SparseVector, got {vector!r}")
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise ValueError(f"k must be a positive int, got {k!r}")

        # Accumulate dot products over candidates only. Iterating query
        # dimensions in ascending order matches brute_force_query exactly.
        dots: Dict[int, float] = {}
        for dim, q_weight in vector:
            plist = self._postings.get(dim)
            if plist is None:
                continue
            for doc_id, d_weight in plist:
                dots[doc_id] = dots.get(doc_id, 0.0) + q_weight * d_weight
        candidates_examined = len(dots)

        q_norm = vector.norm
        scores: Dict[int, float] = {}
        for doc_id, dot in dots.items():
            denom = q_norm * self._norms[doc_id]
            scores[doc_id] = dot / denom if denom > 0.0 else 0.0
        # Non-candidates have dot == 0.0 exactly, hence cosine 0.0. Fill
        # them in so TopK (including k > #candidates) equals a full scan.
        for doc_id in self._docs:
            if doc_id not in scores:
                scores[doc_id] = 0.0

        k = min(k, len(self._docs))
        return QueryResult(
            results=_topk(scores, k),
            candidates_examined=candidates_examined,
            num_docs=len(self._docs),
        )
