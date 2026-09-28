"""Embedding enumeration: all positions where a pattern matches a sequence.

Backtracking over strictly increasing index tuples. Both gap kinds are
monotonic in the next index (timestamps are non-decreasing), so a violated
gap safely terminates the scan of the current branch.
"""

from __future__ import annotations

from app.mining.constraints import gap_satisfies
from app.models.domain import Event, GapConstraints


def find_embeddings(
    events: tuple[Event, ...] | list[Event],
    pattern: tuple[str, ...] | list[str],
    constraints: GapConstraints,
) -> list[tuple[int, ...]]:
    if not pattern:
        return []
    found: list[tuple[int, ...]] = []
    current: list[int] = []

    def extend(depth: int) -> None:
        if depth == len(pattern):
            found.append(tuple(current))
            return
        start = current[-1] + 1 if current else 0
        for j in range(start, len(events)):
            if current and not gap_satisfies(events, current[-1], j, constraints):
                # gaps widen monotonically with j: no later index can fix this
                break
            if events[j].symbol == pattern[depth]:
                current.append(j)
                extend(depth + 1)
                current.pop()

    extend(0)
    return found
