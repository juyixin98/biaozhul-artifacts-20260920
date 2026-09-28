"""Corpus specification validation: concrete failure categories."""
from __future__ import annotations

from dataclasses import replace

import pytest

from app.corpus.spec import normalize_corpus
from app.errors import (
    EmptyCorpusError,
    InvalidEventError,
    InvalidSequenceError,
)
from app.models import CorpusCreateRequest, EventIn, SequenceIn
from tests.conftest import make_request


def _normalize(sequences, corpus_id="c1"):
    return normalize_corpus(corpus_id, make_request(sequences))


def test_positions_are_assigned_by_ordinal_not_supplied():
    corpus = _normalize({"s1": [("A", None), ("B", None)]})
    positions = [e.position for e in corpus.sequences[0].events]
    assert positions == [0, 1]


def test_empty_corpus_is_rejected_with_category():
    request = CorpusCreateRequest(name="empty", sequences=[])
    with pytest.raises(EmptyCorpusError) as exc:
        normalize_corpus("c1", request)
    assert exc.value.code == "empty_corpus"


def test_empty_sequence_is_rejected():
    request = CorpusCreateRequest(
        name="x",
        sequences=[SequenceIn(sequence_id="s1", events=[])],
    )
    with pytest.raises(InvalidSequenceError):
        normalize_corpus("c1", request)


def test_blank_symbol_is_rejected():
    with pytest.raises(InvalidEventError) as exc:
        _normalize({"s1": [("  ", None)]})
    assert exc.value.code == "invalid_event"


def test_symbol_with_whitespace_is_rejected():
    with pytest.raises(InvalidEventError):
        _normalize({"s1": [("A B", None)]})


def test_duplicate_sequence_ids_are_rejected():
    # Build via models directly: dict literals cannot carry duplicate keys.
    request = CorpusCreateRequest(
        name="x",
        sequences=[
            SequenceIn(sequence_id="dup", events=[EventIn(symbol="A")]),
            SequenceIn(sequence_id="dup", events=[EventIn(symbol="B")]),
        ],
    )
    with pytest.raises(InvalidSequenceError) as exc:
        normalize_corpus("c1", request)
    assert exc.value.details["sequence_id"] == "dup"


def test_partial_timestamps_rejected():
    with pytest.raises(InvalidSequenceError) as exc:
        _normalize({"s1": [("A", 1.0), ("B", None)]})
    assert "timestamp" in str(exc.value.message).lower()


def test_decreasing_timestamps_rejected():
    with pytest.raises(InvalidSequenceError) as exc:
        _normalize({"s1": [("A", 5.0), ("B", 4.0)]})
    assert "non-decreasing" in str(exc.value.message)


def test_equal_timestamps_are_normalized_fine():
    corpus = _normalize({"s1": [("A", 5.0), ("B", 5.0)]})
    ts = [e.timestamp for e in corpus.sequences[0].events]
    assert ts == [5.0, 5.0]


def test_non_finite_timestamp_rejected():
    with pytest.raises(InvalidEventError):
        _normalize({"s1": [("A", float("inf"))]})


def test_symbol_over_64_chars_rejected_at_model_layer():
    # The length cap is a field constraint on Symbol; model construction for a
    # 65-char symbol therefore fails (surfaced as invalid_request_body).
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        EventIn(symbol="x" * 65)
    # Boundary: exactly 64 is accepted.
    assert EventIn(symbol="x" * 64).symbol == "x" * 64


def test_sequence_over_event_limit_rejected(monkeypatch):
    from app.corpus import spec as spec_mod
    tiny = replace(spec_mod.settings, max_sequence_events=1)
    monkeypatch.setattr(spec_mod, "settings", tiny)
    with pytest.raises(InvalidSequenceError):
        _normalize({"s1": [("A", None), ("B", None)]})


def test_corpus_over_sequence_limit_rejected(monkeypatch):
    from app.corpus import spec as spec_mod
    tiny = replace(spec_mod.settings, max_corpus_sequences=1)
    monkeypatch.setattr(spec_mod, "settings", tiny)
    with pytest.raises(InvalidSequenceError):
        _normalize({"s1": [("A", None)], "s2": [("B", None)]})
