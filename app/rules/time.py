"""Integer time grid and half-open interval arithmetic.

Every resource occupation in the system is a uniformly **half-open**
integer interval ``[start, end)``:

* the resource is held at grid points ``start, start+1, ..., end-1``;
* it is released at ``end``, so an interval starting exactly at ``end``
  does **not** conflict (boundary release);
* a zero-duration interval ``[t, t)`` is empty and never overlaps
  anything, including another empty interval.

All temporal reasoning code (replay engine, solver, and the test-side
oracle) is expected to use these same definitions.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Interval:
    """A half-open ``[start, end)`` interval on the integer time grid."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError(f"interval start must be >= 0, got {self.start}")
        if self.end < self.start:
            raise ValueError(f"interval end {self.end} before start {self.start}")

    @property
    def duration(self) -> int:
        return self.end - self.start

    @property
    def is_empty(self) -> bool:
        """A zero-duration interval occupies no grid points."""
        return self.end == self.start

    def interior_points(self) -> range:
        """Grid points at which a running action is active.

        For ``[s, e)`` these are ``s .. e-1``. A zero-duration interval
        yields an empty range (its duration invariant is vacuous).
        """
        return range(self.start, self.end)

    def contains(self, t: int) -> bool:
        return self.start <= t < self.end


def overlaps(a: Interval, b: Interval) -> bool:
    """True iff two half-open intervals share at least one grid point.

    ``[0, 2)`` vs ``[2, 4)`` do not overlap (boundary release);
    ``[t, t)`` overlaps nothing.
    """
    if a.is_empty or b.is_empty:
        return False
    return max(a.start, b.start) < min(a.end, b.end)


def overlap_point(a: Interval, b: Interval) -> int | None:
    """First shared grid point, or ``None`` if the intervals are disjoint."""
    if not overlaps(a, b):
        return None
    return max(a.start, b.start)
