"""Gap-constraint checks shared by the kernel and the embedding enumerator."""

from __future__ import annotations

from app.models.domain import Event, GapConstraints, Sequence


class MiningConstraintError(ValueError):
    """Raised when constraints are inconsistent with the corpus."""


def validate_constraints(
    constraints: GapConstraints, sequences: list[Sequence]
) -> None:
    if constraints.max_pos_gap is not None and constraints.max_pos_gap < 1:
        raise MiningConstraintError("max_pos_gap must be >= 1 when set")
    if constraints.max_time_gap is not None:
        if constraints.max_time_gap < 0:
            raise MiningConstraintError("max_time_gap must be >= 0 when set")
        missing = any(
            e.timestamp is None for s in sequences for e in s.events
        )
        if missing:
            raise MiningConstraintError(
                "max_time_gap requires timestamps on every event"
            )


def gap_satisfies(
    events: tuple[Event, ...],
    prev_idx: int,
    next_idx: int,
    constraints: GapConstraints,
) -> bool:
    """Check both gap kinds between two consecutive matched positions."""
    if constraints.max_pos_gap is not None:
        if next_idx - prev_idx > constraints.max_pos_gap:
            return False
    if constraints.max_time_gap is not None:
        prev_ts = events[prev_idx].timestamp
        next_ts = events[next_idx].timestamp
        if prev_ts is None or next_ts is None:
            raise MiningConstraintError(
                "max_time_gap requires timestamps on every event"
            )
        if next_ts - prev_ts > constraints.max_time_gap:
            return False
    return True
