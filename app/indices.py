"""Indices over the corpus and over the frequent-itemset table.

``TransactionIndex`` stores each singleton item as a bitset of transaction
ids; itemset support counts are intersections, matching set-based support
library semantics (each transaction contributes at most once per itemset).

``FrequentItemsetTable`` is the support lookup the rule generator reads from.
The project premise is rule generation *from already-mined frequent
itemsets*; :mod:`app.apriori` is one concrete producer for that table.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Sequence

from .corpus import CorpusSpec
from .models import FrequentItemset, Itemset, Transaction


class TransactionIndex:
    def __init__(self, transactions: Sequence[Transaction], n_transactions: int) -> None:
        self._n = n_transactions
        self._singletons: Dict[str, int] = {}
        self._transaction_sets: Dict[int, frozenset[str]] = {}
        for t in transactions:
            self._transaction_sets[t.tid] = frozenset(t.items)
            for item in t.items:
                self._singletons[item] = self._singletons.get(item, 0) | (1 << t.tid)

    @classmethod
    def from_corpus(cls, corpus: CorpusSpec) -> "TransactionIndex":
        # TIDs in CorpusSpec are original row ids; remap compactly so bit
        # positions are dense while preserving a mapping back if needed.
        return cls(corpus.transactions, corpus.n_transactions)

    @property
    def n_transactions(self) -> int:
        return self._n

    def singleton_items(self) -> List[str]:
        return sorted(self._singletons)

    def itemset_count(self, items: Iterable[str]) -> int:
        """Count transactions containing *every* item (set containment)."""
        mask = None
        for item in items:
            item_mask = self._singletons.get(item)
            if item_mask is None:
                return 0
            mask = item_mask if mask is None else (mask & item_mask)
            if mask == 0:
                return 0
        if mask is None:
            raise ValueError("cannot count an empty itemset")
        return mask.bit_count()

    def contains_all(self, tid: int, items: Iterable[str]) -> bool:
        tset = self._transaction_sets[tid]
        return all(item in tset for item in items)


class FrequentItemsetTable:
    """Read model: all frequent itemsets with their counts and supports."""

    def __init__(self, itemsets: Mapping[Itemset, FrequentItemset], n_transactions: int) -> None:
        self._by_items: Dict[Itemset, FrequentItemset] = dict(itemsets)
        self._n = n_transactions

    @property
    def n_transactions(self) -> int:
        return self._n

    def __len__(self) -> int:
        return len(self._by_items)

    def __contains__(self, items: Itemset) -> bool:
        return tuple(sorted(items)) in self._by_items

    def get(self, items: Iterable[str]) -> FrequentItemset | None:
        return self._by_items.get(tuple(sorted(items)))

    def require(self, items: Iterable[str]) -> FrequentItemset:
        key = tuple(sorted(items))
        if key not in self._by_items:
            raise KeyError(f"itemset {key!r} is not present in the frequent-itemset table")
        return self._by_items[key]

    def all(self) -> List[FrequentItemset]:
        return [self._by_items[k] for k in sorted(self._by_items)]

    def itemsets(self) -> List[Itemset]:
        return [fi.items for fi in self.all()]

    @classmethod
    def from_counts(
        cls, counts: Mapping[Itemset, int], n_transactions: int
    ) -> "FrequentItemsetTable":
        if n_transactions <= 0:
            raise ValueError("n_transactions must be positive")
        built: Dict[Itemset, FrequentItemset] = {}
        for raw_items, count in counts.items():
            items = tuple(sorted(raw_items))
            if not items:
                raise ValueError("empty itemsets are not supported")
            if not 0 <= count <= n_transactions:
                raise ValueError(
                    f"count {count} for {items!r} out of range [0, {n_transactions}]"
                )
            built[items] = FrequentItemset(
                items=items,
                support_count=count,
                support=count / n_transactions,
            )
        return cls(built, n_transactions)
