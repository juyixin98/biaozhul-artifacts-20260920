"""Independent brute-force reference implementation.

Deliberately written WITHOUT importing anything from app.kernel / app.index
so cross-check tests compare two genuinely independent computations:
exhaustive substring enumeration per document versus the suffix-array core.
Only usable for short texts (O(total * len^2) substring enumeration).
"""

from __future__ import annotations


def _substrings_of_length(doc: bytes, length: int) -> set[bytes]:
    return {doc[i : i + length] for i in range(len(doc) - length + 1)}


def brute_longest(docs: list[bytes], min_docs: int) -> tuple[int, list[bytes]]:
    """(length, sorted distinct substrings) covering >= min_docs documents."""
    max_len = max((len(d) for d in docs), default=0)
    for length in range(max_len, 0, -1):
        coverage: dict[bytes, int] = {}
        for doc in docs:
            for sub in _substrings_of_length(doc, length):
                coverage[sub] = coverage.get(sub, 0) + 1
        winners = sorted(sub for sub, count in coverage.items() if count >= min_docs)
        if winners:
            return length, winners
    return 0, []


def brute_occurrences(docs: list[bytes], pattern: bytes) -> list[tuple[int, int]]:
    """All (doc index, offset) occurrences, overlaps included, sorted."""
    hits: list[tuple[int, int]] = []
    for doc_index, doc in enumerate(docs):
        start = 0
        while True:
            pos = doc.find(pattern, start)
            if pos < 0:
                break
            hits.append((doc_index, pos))
            start = pos + 1  # +1 keeps overlapping occurrences
    return sorted(hits)


def brute_coverage(docs: list[bytes], pattern: bytes) -> list[int]:
    """Sorted distinct document indices containing the pattern."""
    return sorted({doc_index for doc_index, _ in brute_occurrences(docs, pattern)})
