"""Corpus specification: validation and normalization of raw input.

A corpus is normalized deterministically:

* event ``position`` is always the event ordinal inside its sequence (0-based),
  never client-supplied;
* timestamps are optional, but within one sequence they are all-or-nothing and,
  when present, must be finite and non-decreasing (equal timestamps encode
  time ties, which are legal);
* sequence ids must be unique and non-empty.

Failures raise categorized domain errors (see :mod:`app.errors`) instead of
being silently coerced.
"""
from __future__ import annotations

import math
from typing import Iterable

from app.config import settings
from app.errors import (
    EmptyCorpusError,
    InvalidEventError,
    InvalidSequenceError,
)
from app.models import Corpus, CorpusCreateRequest, Event, Sequence

_ALLOWED_SYMBOL_PUNCT = ("_", "-", ".", ":")


def _check_symbol(symbol: str, *, kind: str, seq_id: str | None = None) -> None:
    if not isinstance(symbol, str) or not symbol.strip():
        raise InvalidEventError(
            f"{kind} must be a non-empty string",
            details={"sequence_id": seq_id, "value": symbol},
        )
    # The 64-char upper bound is enforced by the Symbol field constraint on
    # the request models (field constraints are preferred to hand checks).
    if any(ch.isspace() for ch in symbol):
        raise InvalidEventError(
            f"{kind} must not contain whitespace",
            details={"sequence_id": seq_id, "value": symbol},
        )


def _normalize_events(raw_events: Iterable, seq_id: str) -> tuple[Event, ...]:
    events: list[Event] = []
    present_flags: list[bool] = []
    for position, raw in enumerate(raw_events):
        symbol = getattr(raw, "symbol", None)
        _check_symbol(symbol, kind="event symbol", seq_id=seq_id)
        ts = getattr(raw, "timestamp", None)
        if ts is not None:
            if not math.isfinite(ts):
                raise InvalidEventError(
                    "timestamp must be finite",
                    details={"sequence_id": seq_id, "position": position},
                )
        present_flags.append(ts is not None)
        events.append(Event(position=position, symbol=symbol, timestamp=ts))

    if not events:
        raise InvalidSequenceError(
            "sequence must contain at least one event",
            details={"sequence_id": seq_id},
        )
    if len(events) > settings.max_sequence_events:
        raise InvalidSequenceError(
            f"sequence exceeds {settings.max_sequence_events} events",
            details={"sequence_id": seq_id, "events": len(events)},
        )
    if any(present_flags) and not all(present_flags):
        raise InvalidSequenceError(
            "timestamps must be present on every event or on none (no partial timestamps)",
            details={"sequence_id": seq_id},
        )
    for prev, cur in zip(events, events[1:]):
        if prev.timestamp is not None and cur.timestamp < prev.timestamp:
            raise InvalidSequenceError(
                "timestamps must be non-decreasing (equal values encode time ties)",
                details={
                    "sequence_id": seq_id,
                    "positions": [prev.position, cur.position],
                    "timestamps": [prev.timestamp, cur.timestamp],
                },
            )
    return tuple(events)


def normalize_corpus(corpus_id: str, request: CorpusCreateRequest) -> Corpus:
    """Validate a create request and produce a normalized frozen corpus."""
    _check_symbol(corpus_id, kind="corpus id")
    _check_symbol(request.name, kind="corpus name")

    if not request.sequences:
        raise EmptyCorpusError("corpus must contain at least one sequence")
    if len(request.sequences) > settings.max_corpus_sequences:
        raise InvalidSequenceError(
            f"corpus exceeds {settings.max_corpus_sequences} sequences",
            details={"sequences": len(request.sequences)},
        )

    seen: set[str] = set()
    sequences: list[Sequence] = []
    for raw_seq in request.sequences:
        seq_id = raw_seq.sequence_id
        _check_symbol(seq_id, kind="sequence id")
        if seq_id in seen:
            raise InvalidSequenceError(
                "duplicate sequence id",
                details={"sequence_id": seq_id},
            )
        seen.add(seq_id)
        events = _normalize_events(raw_seq.events, seq_id)
        sequences.append(Sequence(sequence_id=seq_id, events=events))

    return Corpus(
        corpus_id=corpus_id,
        name=request.name,
        description=request.description,
        sequences=tuple(sequences),
    )


def corpus_has_timestamps(corpus: Corpus) -> bool:
    """Whether every sequence in the corpus carries timestamps."""
    return all(
        seq.events[0].timestamp is not None for seq in corpus.sequences if seq.events
    )
