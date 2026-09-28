"""Differential test: projection-growth kernel vs independent naive miner
on seeded randomized corpora, including repeated events, simultaneous
timestamps, and both gap kinds."""

import random

import pytest

from app.mining.kernel import PrefixGrowthMiner
from app.models.domain import Event, GapConstraints, Sequence
from tests.naive_miner import naive_mine

ALPHABET = ("A", "B", "C")


def random_corpus(rng: random.Random, with_timestamps: bool) -> list[Sequence]:
    sequences = []
    for i in range(4):
        length = rng.randint(1, 6)
        events = []
        ts = 0
        for _ in range(length):
            if with_timestamps:
                ts += rng.randint(0, 2)  # non-decreasing, allows simultaneity
            events.append(
                Event(
                    symbol=rng.choice(ALPHABET),
                    timestamp=float(ts) if with_timestamps else None,
                )
            )
        sequences.append(Sequence(sequence_id=f"R{i}", events=tuple(events)))
    return sequences


CONFIGS = [
    pytest.param({"with_timestamps": False, "max_pos_gap": None, "max_time_gap": None}, id="no_gaps"),
    pytest.param({"with_timestamps": False, "max_pos_gap": 2, "max_time_gap": None}, id="pos_gap_2"),
    pytest.param({"with_timestamps": True, "max_pos_gap": None, "max_time_gap": 2.0}, id="time_gap_2"),
    pytest.param({"with_timestamps": True, "max_pos_gap": 2, "max_time_gap": 3.0}, id="both_gaps"),
]


@pytest.mark.parametrize("config", CONFIGS)
@pytest.mark.parametrize("seed", [11, 23, 37])
def test_kernel_agrees_with_naive_miner(config, seed):
    rng = random.Random(seed)
    sequences = random_corpus(rng, config["with_timestamps"])
    constraints = GapConstraints(
        max_pos_gap=config["max_pos_gap"], max_time_gap=config["max_time_gap"]
    )
    miner = PrefixGrowthMiner(sequences, constraints, min_support=2, max_pattern_len=3)
    kernel_result = {r.pattern: r.support for r in miner.mine()}
    naive_result = naive_mine(
        sequences,
        min_support=2,
        max_pos_gap=config["max_pos_gap"],
        max_time_gap=config["max_time_gap"],
        max_len=3,
    )
    assert kernel_result == naive_result
