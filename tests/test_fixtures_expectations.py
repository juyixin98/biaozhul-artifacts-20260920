"""Concrete, hand-computed expectations for the synthetic fixtures.

Each assertion states an exact result and the comment records how it was
derived.  These answers are written independently of the mining code (the
derivations use only the documented embedding semantics).  The deeper
engine-vs-oracle cross-check lives in ``test_oracle_equivalence.py``; this
module pins the fixtures' documented contract so a future change cannot
silently alter intended behavior.
"""
from __future__ import annotations

from app.corpus.fixtures import all_fixture_requests
from app.corpus.spec import normalize_corpus
from app.miner.constraints import validate_query
from app.miner.facade import run_mining
from app.models import MineRequest
from app.observability import get_run_logger


def _mine(request, *, min_support, max_gap_position=None, max_gap_time=None):
    corpus = normalize_corpus("fixture-under-test", request)
    q = MineRequest(
        corpus_id=corpus.corpus_id,
        min_support=min_support,
        max_gap_position=max_gap_position,
        max_gap_time=max_gap_time,
    )
    constraints = validate_query(corpus, q)
    logger = get_run_logger(file_logging=False)
    response = run_mining(corpus, constraints, logger,
                          run_id=logger.run_id)
    logger.close()
    return {pr.pattern: pr for pr in response.patterns}


def test_fixture_catalogue_is_complete():
    assert set(all_fixture_requests()) == {
        "repeated_symbols", "time_ties", "multiple_embeddings",
        "gap_boundaries", "pruning_trap",
    }


def test_repeated_symbols_expectations():
    patterns = _mine(all_fixture_requests()["repeated_symbols"], min_support=2)
    # Singles: A and B occur in all three; C only in s2,s3.
    assert patterns[("A",)].support == 3
    assert patterns[("B",)].support == 3
    assert patterns[("C",)].support == 2
    # <A,B>: s1 (A before B) and s2 only. s3 is B then A.
    assert patterns[("A", "B")].supporting_sequence_ids == ("s1", "s2")
    # In s1 the five ordered (A-before-B) pairs: A positions 0,1,3 x B 2,4.
    s1 = next(ss for ss in patterns[("A", "B")].evidence if ss.sequence_id == "s1")
    assert {e.positions for e in s1.embeddings} == {
        (0, 2), (0, 4), (1, 2), (1, 4), (3, 4),
    }
    # <A,C>: s2 (0,2) and s3 (1,2); s1 has no C.
    assert set(patterns[("A", "C")].supporting_sequence_ids) == {"s2", "s3"}
    # No length-3 pattern reaches support 2 in this fixture.
    assert all(len(p) <= 2 for p in patterns)


def test_time_ties_zero_gap_expectations():
    patterns = _mine(all_fixture_requests()["time_ties"],
                     min_support=1, max_gap_time=0)
    # Derivation: gap-0 requires strictly increasing positions but equal times.
    assert set(patterns[("A", "B")].supporting_sequence_ids) == {"s1", "s2"}
    assert set(patterns[("B", "C")].supporting_sequence_ids) == {"s2"}
    assert set(patterns[("A", "C")].supporting_sequence_ids) == {"s2"}
    assert set(patterns[("A", "B", "C")].supporting_sequence_ids) == {"s2"}
    s2 = next(ss for ss in patterns[("A", "B", "C")].evidence
              if ss.sequence_id == "s2")
    assert s2.embeddings[0].positions == (0, 1, 2)
    assert s2.embeddings[0].time_gaps == (0.0, 0.0)


def test_multiple_embeddings_expectations():
    patterns = _mine(all_fixture_requests()["multiple_embeddings"], min_support=2)
    # <A,B,C>: s1 has four embeddings; s3 has three; s2 has no C.
    abc = patterns[("A", "B", "C")]
    assert set(abc.supporting_sequence_ids) == {"s1", "s3"}
    s1 = next(ss for ss in abc.evidence if ss.sequence_id == "s1")
    s3 = next(ss for ss in abc.evidence if ss.sequence_id == "s3")
    assert {e.positions for e in s1.embeddings} == {
        (0, 1, 3), (0, 1, 5), (0, 4, 5), (2, 4, 5),
    }
    assert {e.positions for e in s3.embeddings} == {
        (0, 1, 4), (0, 3, 4), (2, 3, 4),
    }
    # <A,B,A,B,C> threads both As and both Bs in s1/s3 (support 2).
    long_pat = ("A", "B", "A", "B", "C")
    assert set(patterns[long_pat].supporting_sequence_ids) == {"s1", "s3"}
    long_s1 = next(ss for ss in patterns[long_pat].evidence
                   if ss.sequence_id == "s1")
    assert [e.positions for e in long_s1.embeddings] == [(0, 1, 2, 4, 5)]


def test_gap_boundaries_expectations():
    request = all_fixture_requests()["gap_boundaries"]
    # Position gap exactly 2 admits s1 (distance 2) but not s2 (distance 3).
    at2 = _mine(request, min_support=1, max_gap_position=2)
    assert set(at2[("A", "B")].supporting_sequence_ids) == {"s1", "s3", "s4"}
    # Time gap exactly 5 admits s3 (gap 5) but not s4 (gap 6).
    at5 = _mine(request, min_support=1, max_gap_time=5)
    assert set(at5[("A", "B")].supporting_sequence_ids) == {"s1", "s2", "s3"}
    # Both limits simultaneously: only s3 satisfies pos<=1 and time<=5.
    both = _mine(request, min_support=1,
                 max_gap_position=1, max_gap_time=5)
    assert set(both[("A", "B")].supporting_sequence_ids) == {"s3"}


def test_pruning_trap_expectations():
    request = all_fixture_requests()["pruning_trap"]
    tight = _mine(request, min_support=2, max_gap_position=1)
    ab = tight[("A", "B")]
    # The later A (pos 2) is the only legal projector under gap 1.
    trap = next(ss for ss in ab.evidence if ss.sequence_id == "trap")
    assert [e.positions for e in trap.embeddings] == [(2, 3)]
    assert set(ab.supporting_sequence_ids) == {"ok", "trap"}

    unbounded = _mine(request, min_support=2)
    trap = next(ss for ss in unbounded[("A", "B")].evidence
                if ss.sequence_id == "trap")
    assert {e.positions for e in trap.embeddings} == {(0, 3), (2, 3)}
