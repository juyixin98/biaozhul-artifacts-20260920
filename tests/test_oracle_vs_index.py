"""Cross-checks: the chunked index must agree with the full-stack-scan oracle.

The oracle (kernel.oracle) is an independent single-pass implementation; the
hand-written fixture expectations are independent of both. Random corpora
are seeded and deterministic.
"""

from __future__ import annotations

import random

import pytest

from bracket_index.corpus.fixtures import CASES
from bracket_index.index.engine import CATEGORY_MATCHED, IndexEngine
from bracket_index.kernel import oracle
from bracket_index.kernel.oracle import (
    CATEGORY_TYPE_MISMATCH,
    CATEGORY_UNMATCHED_CLOSE,
    CATEGORY_UNMATCHED_OPEN,
)

ALPHABET = list("ab ,;=+") + list("()[]{}") + ['"', "'", "\\"]


def random_text(rng: random.Random, length: int) -> str:
    return "".join(rng.choice(ALPHABET) for _ in range(length))


def oracle_match_category(result: oracle.ScanResult, pos: int) -> tuple[str, int | None]:
    """What the oracle says about the bracket at `pos`."""
    if pos in result.pairs:
        return (CATEGORY_MATCHED, result.pairs[pos])
    for event in result.mismatches:
        if event.close.pos == pos:
            return (CATEGORY_TYPE_MISMATCH, event.open.pos)
    if any(t.pos == pos for t in result.unmatched_openers):
        return (CATEGORY_UNMATCHED_OPEN, None)
    if any(t.pos == pos for t in result.unmatched_closers):
        return (CATEGORY_UNMATCHED_CLOSE, None)
    raise AssertionError(f"oracle has no opinion on pos {pos}")


def assert_index_agrees_with_oracle(engine: IndexEngine, doc_id: int, text: str) -> None:
    result = oracle.scan(text)

    balance = engine.balance(doc_id)
    assert balance.balanced == result.balanced
    expected_interval = oracle.shortest_unbalanced_interval(result)
    if expected_interval is None:
        assert balance.interval is None
    else:
        assert balance.interval is not None
        assert (balance.interval.start, balance.interval.end) == (
            expected_interval.start,
            expected_interval.end,
        )
        assert balance.interval.category == expected_interval.category
    assert balance.unmatched_openers == len(result.unmatched_openers)
    assert balance.unmatched_closers == len(result.unmatched_closers)
    assert balance.mismatches == len(result.mismatches)

    for token in result.tokens:
        expected_category, expected_pos = oracle_match_category(result, token.pos)
        got = engine.match(doc_id, token.pos)
        assert got.category == expected_category, (
            f"pos {token.pos}: index={got.category} oracle={expected_category}"
        )
        assert got.match_pos == expected_pos, (
            f"pos {token.pos}: index match={got.match_pos} oracle={expected_pos}"
        )


@pytest.mark.parametrize("chunk_size", [1, 3, 8, 64])
def test_fixtures_index_vs_oracle(tmp_path, chunk_size):
    from bracket_index.index.store import Store

    store = Store(str(tmp_path / f"idx-{chunk_size}.db"))
    engine = IndexEngine(store, chunk_size=chunk_size)
    try:
        for case in CASES:
            doc_id = engine.create_document(case.text)
            assert_index_agrees_with_oracle(engine, doc_id, case.text)
    finally:
        store.close()


def test_fixtures_match_handwritten_expectations(engine):
    """Index results must equal the hand-written answers, not just the oracle."""
    for case in CASES:
        doc_id = engine.create_document(case.text)
        balance = engine.balance(doc_id)
        assert balance.category == case.category, case.name
        if case.interval is None:
            assert balance.interval is None, case.name
        else:
            assert balance.interval is not None, case.name
            assert (balance.interval.start, balance.interval.end) == (
                case.interval.start,
                case.interval.end,
            ), case.name
        for open_pos, close_pos in case.pairs.items():
            got = engine.match(doc_id, open_pos)
            assert got.category == CATEGORY_MATCHED, (case.name, open_pos)
            assert got.match_pos == close_pos, (case.name, open_pos)


def test_random_corpora_index_vs_oracle(engine):
    rng = random.Random(20260927)
    for trial in range(60):
        text = random_text(rng, rng.randrange(0, 200))
        doc_id = engine.create_document(text)
        assert_index_agrees_with_oracle(engine, doc_id, text)


def test_deeply_nested_multi_block(engine):
    rng = random.Random(7)
    blocks = []
    for _ in range(30):
        depth = rng.randrange(1, 12)
        opens = [rng.choice("([{") for _ in range(depth)]
        closes = {"(": ")", "[": "]", "{": "}"}
        blocks.append("".join(opens) + "".join(closes[c] for c in reversed(opens)))
    text = "x;".join(blocks)
    doc_id = engine.create_document(text)
    assert_index_agrees_with_oracle(engine, doc_id, text)
    assert engine.balance(doc_id).balanced
