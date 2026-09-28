"""Equivalence of the projection engine against the independent oracle.

The expected answers here come from :mod:`tests.oracle`, a separate
brute-force implementation that imports nothing from ``app.miner``.  Two kinds
of checks:

1. hand-built scenarios with asserted exact pattern/support/embedding sets;
2. seeded randomized fuzzing: many small corpora x constraint combinations are
   generated deterministically and the two implementations must agree on the
   frequent pattern set, the supporting-sequence set and EVERY embedding.
"""
from __future__ import annotations

import random

from tests.conftest import run_kernel
from tests.oracle import brute_mine


def app_result_as_oracle_dict(response):
    """Serialize the app response into the SAME shape as the oracle output."""
    out = {}
    for pr in response.patterns:
        hits = {}
        for ss in pr.evidence:
            hits[ss.sequence_id] = sorted(e.positions for e in ss.embeddings)
        out[pr.pattern] = hits
    return out


def sequences_from_corpus(corpus):
    return {
        seq.sequence_id: [(e.symbol, e.timestamp) for e in seq.events]
        for seq in corpus.sequences
    }


def assert_engine_matches_oracle(sequences, *, min_support, max_gap_position=None,
                                 max_gap_time=None, max_pattern_length=64):
    response = run_kernel(
        sequences,
        min_support=min_support,
        max_gap_position=max_gap_position,
        max_gap_time=max_gap_time,
        max_pattern_length=max_pattern_length,
    )
    engine = app_result_as_oracle_dict(response)
    oracle = {
        pattern: {sid: sorted(embs) for sid, embs in hits.items()}
        for pattern, hits in brute_mine(
            sequences,
            min_support=min_support,
            max_gap_position=max_gap_position,
            max_gap_time=max_gap_time,
            max_pattern_length=max_pattern_length,
        ).items()
    }

    assert set(engine) == set(oracle), (
        f"pattern-set mismatch\n engine only: {set(engine) - set(oracle)}\n"
        f" oracle only: {set(oracle) - set(engine)}"
    )
    for pattern in oracle:
        assert set(engine[pattern]) == set(oracle[pattern]), (
            f"support-set mismatch for {pattern}: "
            f"{set(engine[pattern]) ^ set(oracle[pattern])}"
        )
        for sid in oracle[pattern]:
            assert engine[pattern][sid] == oracle[pattern][sid], (
                f"embedding mismatch for {pattern} in {sid}: "
                f"engine={engine[pattern][sid]} oracle={oracle[pattern][sid]}"
            )


# ----------------------------------------------------------- hand-built cases

def test_oracle_repeated_symbols_fixture():
    sequences = {
        "s1": [("A", 1), ("A", 2), ("B", 3), ("A", 4), ("B", 5)],
        "s2": [("A", 1), ("B", 2), ("C", 3)],
        "s3": [("B", 1), ("A", 2), ("C", 3)],
    }
    assert_engine_matches_oracle(sequences, min_support=2)


def test_oracle_time_ties_fixture():
    sequences = {
        "s1": [("A", 10), ("B", 10), ("C", 20)],
        "s2": [("A", 10), ("B", 10), ("C", 10)],
        "s3": [("A", 10), ("B", 11), ("C", 20)],
    }
    for kwargs in (
        {"min_support": 2},
        {"min_support": 1, "max_gap_time": 0},
        {"min_support": 1, "max_gap_time": 10},
    ):
        assert_engine_matches_oracle(sequences, **kwargs)


def test_oracle_pruning_trap_with_gap():
    sequences = {
        "trap": [("A", 1), ("C", 2), ("A", 3), ("B", 4)],
        "ok": [("A", 1), ("A", 2), ("B", 3)],
    }
    assert_engine_matches_oracle(sequences, min_support=2, max_gap_position=1)
    assert_engine_matches_oracle(sequences, min_support=1, max_gap_position=2)
    assert_engine_matches_oracle(sequences, min_support=1)


def test_oracle_gap_boundaries_fixture():
    sequences = {
        "s1": [("A", 0), ("x", 1), ("B", 2)],
        "s2": [("A", 0), ("x", 1), ("y", 2), ("B", 3)],
        "s3": [("A", 0.0), ("B", 5.0)],
        "s4": [("A", 0.0), ("B", 6.0)],
    }
    for gp in (None, 1, 2, 3):
        for gt in (None, 0, 4, 5, 6):
            assert_engine_matches_oracle(
                sequences, min_support=1, max_gap_position=gp, max_gap_time=gt
            )


# ------------------------------------------------------------- seeded fuzzing

ALPHABET = ["A", "B", "C"]  # small alphabet -> repeats & multiple embeddings


def _generate_case(rng):
    n_sequences = rng.randint(1, 5)
    sequences = {}
    for i in range(n_sequences):
        length = rng.randint(1, 7)
        events = []
        ts = 0.0
        for _ in range(length):
            events.append((rng.choice(ALPHABET), ts))
            # Non-decreasing, with explicit ties to exercise time-gap 0.
            ts += rng.choice([0.0, 0.0, 1.0, 2.0, 5.0])
        sequences[f"s{i}"] = events
    return sequences


def test_seeded_fuzz_matches_oracle_across_constraint_grid():
    rng = random.Random(20260927)  # deterministic: reproducible run identity
    case_count = 60
    for case_idx in range(case_count):
        sequences = _generate_case(rng)
        min_support = rng.choice([1, 1, 2, 3])
        max_gap_position = rng.choice([None, None, 1, 2, 3])
        max_gap_time = rng.choice([None, None, 0, 1, 3, 6])
        max_pattern_length = rng.choice([64, 64, 2, 3])
        # The corpus is small, so an absolute support larger than it simply
        # yields no patterns; cap it to keep the comparison interesting.
        min_support = min(min_support, len(sequences))
        assert_engine_matches_oracle(
            sequences,
            min_support=min_support,
            max_gap_position=max_gap_position,
            max_gap_time=max_gap_time,
            max_pattern_length=max_pattern_length,
        )
