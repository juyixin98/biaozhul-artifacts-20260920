"""Projection database correctness: alternative embeddings must survive.

The projection for a prefix retains EVERY suffix start reachable from some
embedding.  These tests pin down cases where a greedy first-match or a
single-suffix projection would prune a valid alternative embedding and report
a frequent pattern as absent.
"""
from __future__ import annotations

from tests.conftest import index_by_pattern, run_kernel


def test_pruning_trap_tight_position_gap_uses_later_embedding():
    # trap: A C A B ; only the LATER A (pos 2) reaches B (pos 3) with gap 1.
    sequences = {
        "trap": [("A", 1), ("C", 2), ("A", 3), ("B", 4)],
        "ok":   [("A", 1), ("A", 2), ("B", 3)],
    }
    response = run_kernel(sequences, min_support=2, max_gap_position=1)
    patterns = index_by_pattern(response)
    ab = patterns[("A", "B")]
    assert ab is not None, "greedy first-match would wrongly drop <A,B>"
    assert ab.support == 2
    assert set(ab.supporting_sequence_ids) == {"ok", "trap"}

    trap = next(ss for ss in ab.evidence if ss.sequence_id == "trap")
    # Only embedding is (2,3); the early A at 0 cannot reach B within gap 1.
    assert [e.positions for e in trap.embeddings] == [(2, 3)]


def test_pruning_trap_without_gap_lists_both_embeddings():
    sequences = {
        "trap": [("A", 1), ("C", 2), ("A", 3), ("B", 4)],
        "ok":   [("A", 1), ("A", 2), ("B", 3)],
    }
    response = run_kernel(sequences, min_support=2)
    patterns = index_by_pattern(response)
    ab = patterns[("A", "B")]
    trap = next(ss for ss in ab.evidence if ss.sequence_id == "trap")
    # Unbounded gap: both (0,3) and (2,3) are legal and must be reported.
    assert {e.positions for e in trap.embeddings} == {(0, 3), (2, 3)}


def test_two_adjacent_as_both_retained_as_projectors():
    # s2-style sequence alone: A A B B.  Both As must project onto both Bs.
    sequences = {
        "x": [("A", 1), ("A", 2), ("B", 3), ("B", 4)],
        "y": [("A", 1), ("B", 2)],
    }
    response = run_kernel(sequences, min_support=2)
    patterns = index_by_pattern(response)
    ab = patterns[("A", "B")]
    x = next(ss for ss in ab.evidence if ss.sequence_id == "x")
    assert {e.positions for e in x.embeddings} == {
        (0, 2), (0, 3), (1, 2), (1, 3),
    }


def test_alternative_embedding_needed_for_longer_pattern():
    # A C A B with tight gap=1: in s1 the two As (positions 0 and 2) are
    # distance 2 apart, so <A,A,B> fails there; s2 = A A B embeds it via
    # (0,1,2). Support is therefore 1 and the pattern is pruned, while <A,B>
    # remains frequent via s1's later A.  Proves the two As are distinct
    # projectors rather than collapsed onto the first.
    sequences = {
        "s1": [("A", 1), ("C", 2), ("A", 3), ("B", 4)],
        "s2": [("A", 1), ("A", 2), ("B", 3)],
    }
    response = run_kernel(sequences, min_support=2, max_gap_position=1)
    patterns = index_by_pattern(response)
    assert ("A", "A", "B") not in patterns
    assert ("A", "B") in patterns


def test_multiple_embeddings_pattern_aba_and_abc():
    # s1: A B A C B C -> <A,B,C>: A@0 reaches B@1->{C@3,C@5} and B@4->C@5,
    #     plus A@2->B@4->C@5: four embeddings.
    # s3: A B A B C   -> <A,B,C> embeddings (0,1,4),(0,3,4),(2,3,4)
    sequences = {
        "s1": [("A", 1), ("B", 2), ("A", 3), ("C", 4), ("B", 5), ("C", 6)],
        "s2": [("A", 1), ("A", 2), ("B", 3), ("B", 4)],
        "s3": [("A", 1), ("B", 2), ("A", 3), ("B", 4), ("C", 5)],
    }
    response = run_kernel(sequences, min_support=2)
    patterns = index_by_pattern(response)
    abc = patterns[("A", "B", "C")]
    assert abc.support == 2  # s1 and s3 only
    assert set(abc.supporting_sequence_ids) == {"s1", "s3"}
    s1 = next(ss for ss in abc.evidence if ss.sequence_id == "s1")
    s3 = next(ss for ss in abc.evidence if ss.sequence_id == "s3")
    assert {e.positions for e in s1.embeddings} == {
        (0, 1, 3), (0, 1, 5), (0, 4, 5), (2, 4, 5),
    }
    assert {e.positions for e in s3.embeddings} == {
        (0, 1, 4), (0, 3, 4), (2, 3, 4),
    }


def test_evidence_gaps_are_recorded_per_embedding():
    sequences = {
        "s1": [("A", 1), ("x", 2), ("B", 3), ("y", 4), ("C", 5)],
        "s2": [("A", 1), ("B", 2), ("C", 3)],
    }
    response = run_kernel(sequences, min_support=2)
    patterns = index_by_pattern(response)
    abc = patterns[("A", "B", "C")]
    s1 = next(ss for ss in abc.evidence if ss.sequence_id == "s1")
    only = s1.embeddings[0]
    assert only.positions == (0, 2, 4)
    assert only.position_gaps == (2, 2)
    assert only.time_gaps == (2.0, 2.0)
    s2 = next(ss for ss in abc.evidence if ss.sequence_id == "s2")
    assert s2.embeddings[0].position_gaps == (1, 1)
    assert s2.embeddings[0].time_gaps == (1.0, 1.0)
