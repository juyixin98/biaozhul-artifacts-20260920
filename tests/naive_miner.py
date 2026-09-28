"""Independent naive miner used ONLY by tests for differential checking.

Deliberately implemented differently from the production kernel: brute-force
enumeration of all index combinations via itertools, no projection, no
prefix growth. Agreement between the two on randomized corpora is evidence
that neither implementation's structure biases the result.
"""

from __future__ import annotations

import itertools

from app.models.domain import Sequence


def naive_embeddings(events, pattern, max_pos_gap, max_time_gap):
    found = []
    for idxs in itertools.combinations(range(len(events)), len(pattern)):
        if not all(events[i].symbol == s for i, s in zip(idxs, pattern)):
            continue
        ok = True
        for a, b in zip(idxs, idxs[1:]):
            if max_pos_gap is not None and b - a > max_pos_gap:
                ok = False
                break
            if max_time_gap is not None:
                if events[b].timestamp - events[a].timestamp > max_time_gap:
                    ok = False
                    break
        if ok:
            found.append(idxs)
    return found


def naive_mine(
    sequences: list[Sequence],
    min_support: int,
    max_pos_gap=None,
    max_time_gap=None,
    max_len: int = 3,
) -> dict[tuple[str, ...], int]:
    alphabet = sorted({e.symbol for s in sequences for e in s.events})
    results: dict[tuple[str, ...], int] = {}
    for length in range(1, max_len + 1):
        for pattern in itertools.product(alphabet, repeat=length):
            support = sum(
                1
                for s in sequences
                if naive_embeddings(s.events, pattern, max_pos_gap, max_time_gap)
            )
            if support >= min_support:
                results[pattern] = support
    return results
