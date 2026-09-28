"""Defensive safety-net tests.

These prove the system does NOT return a misleading success when an internal
invariant is violated:

* engine says "supported" but the independent enumerator finds no embedding;
* engine and enumerator disagree on the supporting-sequence set.

Both must raise ``MiningInternalError`` rather than emit a success response.
"""
from __future__ import annotations

import pytest

from app.errors import MiningInternalError
from app.miner import facade as facade_mod
from app.miner.engine import RawPattern
from app.observability import get_run_logger
from tests.conftest import build_corpus


def _run_with_raw_patterns(monkeypatch, raw_patterns):
    corpus = build_corpus({"s1": [("A", 1), ("B", 2)],
                           "s2": [("A", 1), ("B", 2)]}, corpus_id="c")

    class _FakeMiner:
        def __init__(self, *_a, **_k):
            pass

        def mine(self):
            return raw_patterns

    monkeypatch.setattr(facade_mod, "ProjectionMiner", _FakeMiner)
    from app.miner.constraints import validate_query
    from app.models import MineRequest
    constraints = validate_query(
        corpus, MineRequest(corpus_id="c", min_support=1)
    )
    logger = get_run_logger(prefix="mine", file_logging=False)
    return corpus, constraints, logger


def test_claims_support_but_no_embedding_raises_internal(monkeypatch):
    from app.miner.constraints import validate_query
    raw = [RawPattern(pattern=("Z",), supporting={"s1": frozenset({0})})]
    corpus, constraints, logger = _run_with_raw_patterns(monkeypatch, raw)
    # The engine claims symbol Z is supported, but Z occurs nowhere, so the
    # enumerator finds nothing: disagreement must surface as an internal error.
    with pytest.raises(MiningInternalError) as exc:
        facade_mod.run_mining(corpus, constraints, logger, run_id="r1")
    assert "disagreement" in str(exc.value.message)
    assert exc.value.code == "mining_internal_error"


def test_support_set_disagreement_raises_internal(monkeypatch):
    # Pattern <A,B>: engine claims support only in s1, but it is actually in
    # both s1 and s2 -> supporting-set mismatch must be raised.
    raw = [RawPattern(
        pattern=("A", "B"),
        supporting={"s1": frozenset({1})},  # deliberately omits s2
    )]
    corpus, constraints, logger = _run_with_raw_patterns(monkeypatch, raw)
    with pytest.raises(MiningInternalError) as exc:
        facade_mod.run_mining(corpus, constraints, logger, run_id="r2")
    details = exc.value.details
    assert details["enumerator_only"] == ["s2"]
    assert details["pattern"] == ["A", "B"]
