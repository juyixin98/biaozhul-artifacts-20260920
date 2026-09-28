"""Concrete assertions for the half-open interval time semantics."""
from __future__ import annotations

import pytest

from app.rules.errors import ValidationFailure
from app.rules.time import Interval, overlap_point, overlaps


@pytest.mark.semantics
def test_boundary_release_is_not_an_overlap() -> None:
    first = Interval(0, 2)
    second = Interval(2, 4)
    assert not overlaps(first, second)
    assert overlap_point(first, second) is None


@pytest.mark.semantics
def test_overlapping_half_open_intervals_share_first_grid_point() -> None:
    first = Interval(1, 4)
    second = Interval(3, 6)
    assert overlaps(first, second)
    assert overlap_point(first, second) == 3


@pytest.mark.semantics
def test_zero_duration_interval_is_empty_and_conflicts_with_nothing() -> None:
    empty = Interval(2, 2)
    assert empty.is_empty
    assert empty.duration == 0
    assert list(empty.interior_points()) == []
    assert not overlaps(empty, Interval(0, 5))
    assert not overlaps(empty, Interval(2, 3))
    assert not overlaps(empty, empty)


@pytest.mark.semantics
def test_interior_points_are_start_through_end_minus_one() -> None:
    assert list(Interval(2, 5).interior_points()) == [2, 3, 4]


@pytest.mark.semantics
def test_contains_respects_half_open_end() -> None:
    interval = Interval(1, 3)
    assert not interval.contains(0)
    assert interval.contains(1)
    assert interval.contains(2)
    assert not interval.contains(3)


@pytest.mark.semantics
def test_invalid_intervals_are_rejected_without_silent_clamping() -> None:
    with pytest.raises(ValueError):
        Interval(-1, 2)
    with pytest.raises(ValueError):
        Interval(3, 2)
