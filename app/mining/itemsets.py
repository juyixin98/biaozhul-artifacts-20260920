"""Frequent itemset mining (Apriori).

Works on iterables of frozensets, so the dedup-per-transaction contract is
enforced by the corpus layer and this kernel never sees duplicates.
Support is a *ratio* in (0, 1]; counts are kept alongside for warnings.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class FrequentItemset:
    items: frozenset[str]
    support_count: int
    support: float  # support_count / n_transactions


def _apriori_gen(prev: list[frozenset[str]], size: int) -> Iterator[frozenset[str]]:
    """Generate candidate itemsets of ``size`` from frequent size-1-smaller sets.

    Different pairs can share the same union, so candidates are
    deduplicated before being yielded.
    """
    seen: set[frozenset[str]] = set()
    prev_sorted = sorted(prev, key=lambda s: sorted(s))
    for i in range(len(prev_sorted)):
        for j in range(i + 1, len(prev_sorted)):
            union = prev_sorted[i] | prev_sorted[j]
            if len(union) == size and union not in seen:
                seen.add(union)
                yield union


def mine_frequent_itemsets(
    transactions: Iterable[frozenset[str]],
    min_support: float,
) -> tuple[list[FrequentItemset], int]:
    """Mine all itemsets with support >= ``min_support``.

    Returns the itemsets (sorted by size then items) and the transaction
    count N, which callers need for metric computation.
    """
    if not 0.0 < min_support <= 1.0:
        raise ValueError(f"min_support must be in (0, 1], got {min_support}")

    txs = list(transactions)
    n = len(txs)
    if n == 0:
        return [], 0
    # Exact ceiling: smallest integer c with c / n >= min_support
    # (avoids float drift from ceil(min_support * n)).
    min_count = 0
    while min_count < n and min_count / n < min_support:
        min_count += 1
    if min_count == 0:
        min_count = 1

    def count_support(candidate: frozenset[str]) -> int:
        return sum(1 for tx in txs if candidate <= tx)

    singletons = sorted({item for tx in txs for item in tx})
    current = [frozenset([i]) for i in singletons]
    results: list[FrequentItemset] = []
    size = 1
    while current:
        frequent = []
        for cand in current:
            c = count_support(cand)
            if c >= min_count:
                frequent.append(cand)
                results.append(FrequentItemset(cand, c, c / n))
        size += 1
        current = list(_apriori_gen(frequent, size)) if len(frequent) > 1 else []

    results.sort(key=lambda fi: (len(fi.items), sorted(fi.items)))
    return results, n
