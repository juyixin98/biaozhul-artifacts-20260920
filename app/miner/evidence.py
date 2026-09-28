"""Embedding evidence enumerator.

Given a pattern and a sequence this enumerates **all** strictly-increasing
position tuples that match the pattern under the constraints.  It is a
deliberately independent implementation from the projection engine:

* the engine tracks sets of suffix starts for *support* (sequence identities);
* this module walks the full choice tree for *evidence* (occurrences).

They share only the constraint predicate (:meth:`ValidatedConstraints.pair_ok`),
which is the single declared definition of a legal pair.  Cross-checking the
two implementations in tests is what makes the evidence trustworthy.
"""
from __future__ import annotations

from app.miner.constraints import ValidatedConstraints
from app.models import Embedding
from app.miner.engine import SeqData


def enumerate_embeddings(
    data: SeqData, pattern: tuple[str, ...], c: ValidatedConstraints
) -> list[tuple[int, ...]]:
    """All matching position tuples, in lexicographic order."""
    results: list[tuple[int, ...]] = []
    path: list[int] = []

    def extend(symbol_index: int, search_from: int) -> None:
        if symbol_index == len(pattern):
            results.append(tuple(path))
            return
        symbol = pattern[symbol_index]
        for p in range(search_from, len(data.symbols)):
            if data.symbols[p] != symbol:
                continue
            if path:
                q = path[-1]
                # Position gap exceeded: later positions are only farther.
                if c.max_gap_position is not None and (p - q) > c.max_gap_position:
                    break
                if c.max_gap_time is not None:
                    tq, tp = data.timestamps[q], data.timestamps[p]
                    # Timestamps are non-decreasing: a later position can only
                    # widen the time gap, so this scan can stop as well.
                    if tp is None or tq is None or (tp - tq) > c.max_gap_time:
                        break
            path.append(p)
            extend(symbol_index + 1, p + 1)
            path.pop()

    extend(0, 0)
    return results


def build_embedding(data: SeqData, positions: tuple[int, ...]) -> Embedding:
    position_gaps = tuple(
        positions[i] - positions[i - 1] for i in range(1, len(positions))
    )
    time_gaps: list[float | None] = []
    for i in range(1, len(positions)):
        tq, tp = data.timestamps[positions[i - 1]], data.timestamps[positions[i]]
        time_gaps.append(None if tq is None or tp is None else tp - tq)
    return Embedding(
        sequence_id=data.sequence_id,
        positions=tuple(positions),
        symbols=tuple(data.symbols[p] for p in positions),
        timestamps=tuple(data.timestamps[p] for p in positions),
        position_gaps=position_gaps,
        time_gaps=tuple(time_gaps),
    )
