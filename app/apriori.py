"""Level-wise Apriori producer for the frequent-itemset table.

This module materializes frequent itemsets from a :class:`TransactionIndex`.
It is a *producer* for :class:`~app.indices.FrequentItemsetTable`; the rule
generator itself only depends on the table, so externally supplied frequent
itemsets are accepted identically.
"""
from __future__ import annotations

from itertools import combinations
from typing import Dict, List

from .indices import FrequentItemsetTable, TransactionIndex
from .models import FrequentItemset, Itemset


def _generate_candidates(prev_frequent_set: set[Itemset], length: int) -> List[Itemset]:
    """Self-join + prune candidate generation (F_{k-1} x F_{k-1})."""
    prev_frequent = sorted(prev_frequent_set)
    candidates: set[Itemset] = set()
    for i, first in enumerate(prev_frequent):
        for second in prev_frequent[i + 1 :]:
            # Join when the first k-2 items agree.
            if first[: length - 2] == second[: length - 2]:
                candidate = tuple(sorted(set(first) | set(second)))
                if len(candidate) != length:
                    continue
                # Prune: every (k-1)-subset must be frequent.
                if all(
                    tuple(sorted(subset)) in prev_frequent_set
                    for subset in combinations(candidate, length - 1)
                ):
                    candidates.add(candidate)
    return sorted(candidates)


def mine_frequent_itemsets(
    index: TransactionIndex,
    min_support: float,
    *,
    max_length: int | None = None,
) -> FrequentItemsetTable:
    """Return all itemsset with relative support >= ``min_support``.

    ``min_support`` is in (0, 1]; zero is rejected here because support zero
    itemsets are, by definition, not frequent and would dominate enumeration
    with meaningless zero-count rules.
    """
    if not 0.0 < min_support <= 1.0:
        raise ValueError("min_support must be in (0, 1]")

    n = index.n_transactions
    min_count = min_support * n
    # Standard ceiling semantics: support >= min_support  <=>  count >= ceil.
    import math

    threshold = max(1, math.ceil(min_count - 1e-12))

    table: Dict[Itemset, FrequentItemset] = {}

    # k = 1
    current: List[Itemset] = []
    for item in index.singleton_items():
        singleton = (item,)
        count = index.itemset_count(singleton)
        if count >= threshold:
            current.append(singleton)
            table[singleton] = FrequentItemset(singleton, count, count / n)

    length = 2
    prev_frequent = current
    while prev_frequent and (max_length is None or length <= max_length):
        prev_set = set(prev_frequent)
        candidates = _generate_candidates(prev_set, length)
        next_frequent: List[Itemset] = []
        for candidate in candidates:
            count = index.itemset_count(candidate)
            if count >= threshold:
                next_frequent.append(candidate)
                table[candidate] = FrequentItemset(candidate, count, count / n)
        prev_frequent = next_frequent
        length += 1

    return FrequentItemsetTable(table, n)
