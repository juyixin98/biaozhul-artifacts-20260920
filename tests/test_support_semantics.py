"""Support is counted by *sequence identity*, never by occurrence count.

These tests assert concrete numbers on hand-built inputs, including the
headline case: one symbol repeated many times in one sequence contributes
exactly one to support while still producing multiple embeddings.
"""
from __future__ import annotations

from tests.conftest import index_by_pattern, run_kernel


def test_single_symbol_repeated_four_times_has_support_one():
    # A appears 4 times in one sequence: support must be 1, occurrences 4.
    response = run_kernel({"s1": [("A", 1), ("A", 2), ("A", 3), ("A", 4)]},
                          min_support=1)
    patterns = index_by_pattern(response)
    only = patterns[("A",)]
    assert only.support == 1
    assert only.occurrences == 4
    assert only.supporting_sequence_ids == ("s1",)
    assert len(only.evidence[0].embeddings) == 4


def test_repeated_symbols_do_not_inflate_multi_sequence_support():
    sequences = {
        "s1": [("A", 1), ("A", 2), ("B", 3), ("A", 4), ("B", 5)],
        "s2": [("A", 1), ("B", 2), ("C", 3)],
        "s3": [("B", 1), ("A", 2), ("C", 3)],  # B before A: no <A,B>
    }
    response = run_kernel(sequences, min_support=2)
    patterns = index_by_pattern(response)

    a = patterns[("A",)]
    assert a.support == 3
    # s1 alone has three As, yet the per-sequence support stays one identity.
    assert a.occurrences == 5  # 3 in s1 + 1 in s2 + 1 in s3
    assert a.supporting_sequence_ids == ("s1", "s2", "s3")

    ab = patterns[("A", "B")]
    assert ab.support == 2
    assert ab.supporting_sequence_ids == ("s1", "s2")
    # Five ordered A-before-B pairs in s1, one in s2; still support 2.
    assert ab.occurrences == 6
    s1_embeddings = ab.evidence[0].embeddings
    assert {e.positions for e in s1_embeddings} == {
        (0, 2), (0, 4), (1, 2), (1, 4), (3, 4),
    }

    # s3 has B before A only, so <A,B> must carry no evidence for s3.
    assert all(ss.sequence_id != "s3" for ss in ab.evidence)


def test_pattern_below_support_threshold_is_absent_not_reported_as_success():
    sequences = {
        "s1": [("A", 1), ("B", 2)],
        "s2": [("A", 1)],
        "s3": [("A", 1)],
    }
    response = run_kernel(sequences, min_support=3)
    patterns = index_by_pattern(response)
    assert ("A",) in patterns          # A in all three
    assert ("A", "B") not in patterns  # only s1 -> below threshold, absent


def test_support_is_a_set_not_summed_over_embeddings():
    # <A,B> embeds three times per sequence: (0,1),(0,3),(2,3). Support is 2.
    sequences = {
        "s1": [("A", 1), ("B", 2), ("A", 3), ("B", 4)],
        "s2": [("A", 1), ("B", 2), ("A", 3), ("B", 4)],
    }
    response = run_kernel(sequences, min_support=2)
    patterns = index_by_pattern(response)
    ab = patterns[("A", "B")]
    assert ab.support == 2
    assert ab.occurrences == 6
    assert [ss.sequence_id for ss in ab.evidence] == ["s1", "s2"]
    for ss in ab.evidence:
        assert {e.positions for e in ss.embeddings} == {(0, 1), (0, 3), (2, 3)}


def test_fractional_min_support_rounds_up_to_sequence_count():
    sequences = {
        "s1": [("A", 1)],
        "s2": [("A", 1)],
        "s3": [("B", 1)],
    }
    # ceil(0.5 * 3) = 2
    response = run_kernel(sequences, min_support=0.5)
    patterns = index_by_pattern(response)
    assert patterns[("A",)].support == 2
    assert ("B",) not in patterns
