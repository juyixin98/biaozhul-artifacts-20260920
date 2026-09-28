"""Position gap and time gap are declared independently.

Boundary convention (explicitly asserted here):

* position gap = index distance, adjacent events have gap 1; a pair at exactly
  ``max_gap_position`` is ACCEPTED (<=), one beyond is rejected;
* time gap = timestamp difference; equal timestamps give gap 0 and are accepted
  by ``max_gap_time = 0``; exact equality is again ACCEPTED (<=);
* when BOTH are declared, a pair must satisfy BOTH (intersection).
"""
from __future__ import annotations

import pytest

from app.errors import InvalidConstraintError
from tests.conftest import index_by_pattern, run_kernel

# s1: A . B      position gap 2, time gap 2
# s2: A . . B    position gap 3, time gap 3
# s3: A@0 B@5    position gap 1, time gap 5
# s4: A@0 B@6    position gap 1, time gap 6
GAP_CORPUS = {
    "s1": [("A", 0), ("x", 1), ("B", 2)],
    "s2": [("A", 0), ("x", 1), ("y", 2), ("B", 3)],
    "s3": [("A", 0.0), ("B", 5.0)],
    "s4": [("A", 0.0), ("B", 6.0)],
}


def ab_support(**kwargs):
    response = run_kernel(GAP_CORPUS, min_support=1, **kwargs)
    patterns = index_by_pattern(response)
    ab = patterns[("A", "B")]
    return set(ab.supporting_sequence_ids)


# ------------------------------------------------------- position gap boundary

def test_position_gap_one_keeps_only_adjacent():
    assert ab_support(max_gap_position=1) == {"s3", "s4"}


def test_position_gap_exact_boundary_is_inclusive():
    # s1 has gap exactly 2: included at limit 2, excluded at limit 1.
    assert ab_support(max_gap_position=2) == {"s1", "s3", "s4"}
    assert "s1" not in ab_support(max_gap_position=1)


def test_position_gap_three_includes_everything():
    assert ab_support(max_gap_position=3) == {"s1", "s2", "s3", "s4"}


# ----------------------------------------------------------- time gap boundary

def test_time_gap_exact_boundary_is_inclusive():
    # s3 has time gap exactly 5: included at 5, excluded at 4.
    assert ab_support(max_gap_time=5) == {"s1", "s2", "s3"}
    assert ab_support(max_gap_time=4) == {"s1", "s2"}
    assert "s3" in ab_support(max_gap_time=5)
    assert "s3" not in ab_support(max_gap_time=4)


def test_time_gap_six_includes_everything():
    assert ab_support(max_gap_time=6) == {"s1", "s2", "s3", "s4"}


# --------------------------------------------- both constraints intersect (AND)

def test_both_gaps_must_hold_simultaneously():
    # limit pos=1 AND time=5: only s3 passes BOTH (s4 fails time, s1/s2 pos).
    assert ab_support(max_gap_position=1, max_gap_time=5) == {"s3"}


def test_position_and_time_gaps_are_not_merged():
    # s1/s2 pass the time limit but fail the position limit -> still excluded.
    result = ab_support(max_gap_position=1, max_gap_time=100)
    assert result == {"s3", "s4"}
    result = ab_support(max_gap_position=100, max_gap_time=5)
    assert result == {"s1", "s2", "s3"}


# --------------------------------------------------------------- time ties (0)

TIME_TIE_CORPUS = {
    "s1": [("A", 10), ("B", 10), ("C", 20)],   # A=B tie, then gap 10
    "s2": [("A", 10), ("B", 10), ("C", 10)],   # triple tie
    "s3": [("A", 10), ("B", 11), ("C", 20)],   # no tie on A->B
}


def test_equal_timestamps_are_legal_ordered_pairs():
    # Without any time limit a tie is simply an ordered pair and <A,B> matches.
    response = run_kernel(TIME_TIE_CORPUS, min_support=1)
    patterns = index_by_pattern(response)
    assert set(patterns[("A", "B")].supporting_sequence_ids) == {"s1", "s2", "s3"}
    s1 = next(ss for ss in patterns[("A", "B")].evidence if ss.sequence_id == "s1")
    tied = s1.embeddings[0]
    assert tied.positions == (0, 1)
    assert tied.time_gaps == (0.0,)


def test_time_gap_zero_accepts_ties_and_rejects_positive_gaps():
    response = run_kernel(TIME_TIE_CORPUS, min_support=1, max_gap_time=0)
    patterns = index_by_pattern(response)
    # <A,B>: s1 tie (0), s2 tie (0) -> yes; s3 gap 1 -> no.
    assert set(patterns[("A", "B")].supporting_sequence_ids) == {"s1", "s2"}
    # <B,C>: only s2 has the tie.
    assert set(patterns[("B", "C")].supporting_sequence_ids) == {"s2"}
    # <A,B,C>: only the fully-tied s2 can thread two zero-gap pairs.
    abc = patterns[("A", "B", "C")]
    assert set(abc.supporting_sequence_ids) == {"s2"}
    s2 = next(ss for ss in abc.evidence if ss.sequence_id == "s2")
    assert s2.embeddings[0].positions == (0, 1, 2)
    assert s2.embeddings[0].time_gaps == (0.0, 0.0)


def test_ties_without_time_constraint_all_match():
    response = run_kernel(TIME_TIE_CORPUS, min_support=2)
    patterns = index_by_pattern(response)
    for pat in (("A", "B"), ("B", "C"), ("A", "B", "C")):
        assert set(patterns[pat].supporting_sequence_ids) == {"s1", "s2", "s3"}


# ------------------------------------------------------- constraint validation

def test_time_gap_requires_timestamps_failure_category():
    no_ts = {
        "s1": [("A", None), ("B", None)],
        "s2": [("A", None), ("B", None)],
    }
    with pytest.raises(InvalidConstraintError) as exc:
        run_kernel(no_ts, min_support=1, max_gap_time=5)
    assert exc.value.code == "invalid_constraint"
    assert "timestamp" in str(exc.value.message)


@pytest.mark.parametrize("bad_gap", [0, -1])
def test_position_gap_below_one_is_rejected(bad_gap):
    with pytest.raises(InvalidConstraintError):
        run_kernel(GAP_CORPUS, min_support=1, max_gap_position=bad_gap)


@pytest.mark.parametrize("bad_support", [0, -2, 1.5, 0.0])
def test_bad_min_support_is_rejected_with_category(bad_support):
    with pytest.raises(InvalidConstraintError) as exc:
        run_kernel(GAP_CORPUS, min_support=bad_support)
    assert exc.value.code == "invalid_constraint"
