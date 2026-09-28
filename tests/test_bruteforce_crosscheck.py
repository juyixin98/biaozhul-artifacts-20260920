"""Cross-check the suffix-array core against exhaustive enumeration."""

import random

import pytest

from app.corpus import Document
from app.index import build_index, locate, mine_longest
from tests.brute_force import brute_coverage, brute_longest, brute_occurrences

ALPHABET = b"abcd"


def _random_docs(rng: random.Random, count: int) -> list[bytes]:
    return [
        bytes(rng.choice(ALPHABET) for _ in range(rng.randint(1, 12)))
        for _ in range(count)
    ]


@pytest.mark.parametrize("seed", range(40))
def test_kernel_matches_brute_force(seed):
    rng = random.Random(seed)
    contents = _random_docs(rng, rng.randint(1, 4))
    docs = [Document(f"doc{i}", content) for i, content in enumerate(contents)]
    index = build_index(docs)
    for min_docs in range(1, len(docs) + 1):
        expected_length, expected_candidates = brute_longest(contents, min_docs)
        length, candidates = mine_longest(index, min_docs)
        assert length == expected_length, f"seed={seed} min_docs={min_docs}"
        assert candidates == expected_candidates, f"seed={seed} min_docs={min_docs}"
        for candidate in candidates:
            located, truncated = locate(index, candidate, occurrence_cap=10_000)
            assert not truncated
            # Coverage set: distinct documents, exactly as brute force sees it.
            assert list(located.doc_coverage) == [
                f"doc{i}" for i in brute_coverage(contents, candidate)
            ]
            # Occurrences: raw offsets, overlaps included, same multiset.
            assert [(o.doc_id, o.offset) for o in located.occurrences] == [
                (f"doc{i}", off) for i, off in brute_occurrences(contents, candidate)
            ]


@pytest.mark.parametrize("seed", range(100, 120))
def test_kernel_matches_brute_force_binary_alphabet(seed):
    rng = random.Random(seed)
    contents = [
        bytes(rng.randrange(4) for _ in range(rng.randint(1, 10)))
        for _ in range(rng.randint(2, 3))
    ]
    docs = [Document(f"b{i}", content) for i, content in enumerate(contents)]
    index = build_index(docs)
    for min_docs in (1, 2):
        expected = brute_longest(contents, min_docs)
        assert mine_longest(index, min_docs) == expected
