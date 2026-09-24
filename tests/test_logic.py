"""Kleene three-valued logic truth tables."""

from app.logic import Tri as T, tri_and, tri_not, tri_or

NOT_CASES = [(T.TRUE, T.FALSE), (T.FALSE, T.TRUE), (T.UNKNOWN, T.UNKNOWN)]

AND_CASES = [
    (T.TRUE, T.TRUE, T.TRUE),
    (T.TRUE, T.FALSE, T.FALSE),
    (T.FALSE, T.TRUE, T.FALSE),
    (T.FALSE, T.FALSE, T.FALSE),
    (T.TRUE, T.UNKNOWN, T.UNKNOWN),
    (T.UNKNOWN, T.TRUE, T.UNKNOWN),
    (T.FALSE, T.UNKNOWN, T.FALSE),   # false dominates
    (T.UNKNOWN, T.FALSE, T.FALSE),
    (T.UNKNOWN, T.UNKNOWN, T.UNKNOWN),
]

OR_CASES = [
    (T.TRUE, T.TRUE, T.TRUE),
    (T.TRUE, T.FALSE, T.TRUE),
    (T.FALSE, T.TRUE, T.TRUE),
    (T.FALSE, T.FALSE, T.FALSE),
    (T.TRUE, T.UNKNOWN, T.TRUE),     # true dominates
    (T.UNKNOWN, T.TRUE, T.TRUE),
    (T.FALSE, T.UNKNOWN, T.UNKNOWN),
    (T.UNKNOWN, T.FALSE, T.UNKNOWN),
    (T.UNKNOWN, T.UNKNOWN, T.UNKNOWN),
]


def test_not_table():
    for a, expected in NOT_CASES:
        assert tri_not(a) is expected


def test_and_table():
    for a, b, expected in AND_CASES:
        assert tri_and(a, b) is expected


def test_or_table():
    for a, b, expected in OR_CASES:
        assert tri_or(a, b) is expected
