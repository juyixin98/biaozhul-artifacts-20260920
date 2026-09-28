"""Unit tests for embedding enumeration (boundary conditions of both gaps)."""

import pytest

from app.mining.constraints import MiningConstraintError
from app.mining.embedding import find_embeddings
from app.models.domain import Event, GapConstraints

NO_GAP = GapConstraints()


def ev(symbol, ts=None):
    return Event(symbol=symbol, timestamp=ts)


def test_repeated_symbols_each_position_matches():
    events = (ev("A"), ev("A"), ev("B"))
    assert find_embeddings(events, ("A",), NO_GAP) == [(0,), (1,)]
    assert find_embeddings(events, ("A", "A"), NO_GAP) == [(0, 1)]
    assert find_embeddings(events, ("A", "B"), NO_GAP) == [(0, 2), (1, 2)]


def test_empty_pattern_has_no_embedding():
    assert find_embeddings((ev("A"),), (), NO_GAP) == []


def test_position_gap_boundary_inclusive():
    events = (ev("A"), ev("X"), ev("B"))
    assert find_embeddings(events, ("A", "B"), GapConstraints(max_pos_gap=2)) == [(0, 2)]
    assert find_embeddings(events, ("A", "B"), GapConstraints(max_pos_gap=1)) == []


def test_time_gap_boundary_inclusive():
    events = (ev("A", 0.0), ev("B", 5.0))
    assert find_embeddings(events, ("A", "B"), GapConstraints(max_time_gap=5.0)) == [(0, 1)]
    assert find_embeddings(events, ("A", "B"), GapConstraints(max_time_gap=4.0)) == []


def test_simultaneous_events_match_zero_time_gap():
    events = (ev("A", 1.0), ev("B", 1.0))
    assert find_embeddings(events, ("A", "B"), GapConstraints(max_time_gap=0.0)) == [(0, 1)]


def test_time_gap_without_timestamps_raises():
    with pytest.raises(MiningConstraintError):
        find_embeddings((ev("A"), ev("B")), ("A", "B"), GapConstraints(max_time_gap=1.0))


def test_both_gaps_apply_together():
    events = (ev("A", 0.0), ev("X", 0.3), ev("B", 1.0))
    constraints = GapConstraints(max_pos_gap=2, max_time_gap=0.4)
    # position gap 2 is fine, but time gap 1.0 > 0.4 rejects A->B
    assert find_embeddings(events, ("A", "B"), constraints) == []
    # A->X: position gap 1 and time gap 0.3 both pass
    assert find_embeddings(events, ("A", "X"), constraints) == [(0, 1)]
