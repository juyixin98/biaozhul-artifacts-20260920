"""Tied longest candidates: all returned, stable deterministic order."""

from app.corpus import Document
from app.index import build_index, mine_longest

DOCS = [
    Document("one", b"aaXYZbbQQQcc"),
    Document("two", b"0QQQ11XYZ22"),
]


def test_equal_length_candidates_all_returned_in_byte_order():
    index = build_index(DOCS)
    length, candidates = mine_longest(index, min_docs=2)
    assert length == 3
    # Both "QQQ" and "XYZ" are longest; order must be lexicographic by bytes.
    assert candidates == [b"QQQ", b"XYZ"]
    assert candidates == sorted(candidates)


def test_tie_order_is_deterministic_across_rebuilds():
    first = mine_longest(build_index(DOCS), min_docs=2)
    second = mine_longest(build_index(list(reversed(DOCS))), min_docs=2)
    # Document order must not influence the candidate ordering.
    assert first == second


def test_tie_order_stable_across_repeated_queries():
    index = build_index(DOCS)
    runs = [mine_longest(index, min_docs=2) for _ in range(5)]
    assert all(run == runs[0] for run in runs)
