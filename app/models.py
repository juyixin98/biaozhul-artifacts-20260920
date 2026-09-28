"""Pydantic schemas: API request/response models and domain entities.

The same models serve as the *index/model* layer boundary between API, storage
and mining kernel: storage returns these immutable domain objects, the miner
consumes them, and the API serializes them.
"""
from __future__ import annotations

from typing import Annotated, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt

Symbol = Annotated[str, Field(min_length=1, max_length=64)]

# Strict numbers: JSON string "2" must not be silently coerced, and booleans
# must not sneak through as 0/1.
StrictNumber = Union[StrictInt, StrictFloat]


class EventIn(BaseModel):
    """Raw event as supplied by the client.

    ``position`` is never accepted from the client: it is always the event's
    ordinal in the sequence (0-based), so clients cannot fabricate gaps.
    """

    symbol: Symbol
    timestamp: Optional[float] = Field(default=None, description="Optional numeric timestamp")


class SequenceIn(BaseModel):
    sequence_id: Symbol
    # No min_length here: emptiness is a categorized spec-layer failure
    # (invalid_sequence), not a generic schema error.
    events: list[EventIn]


class CorpusCreateRequest(BaseModel):
    name: Symbol
    description: Optional[str] = Field(default=None, max_length=500)
    # Emptiness is categorized by the spec layer as ``empty_corpus``.
    sequences: list[SequenceIn]


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    position: int = Field(ge=0)
    symbol: str
    timestamp: Optional[float] = None


class Sequence(BaseModel):
    model_config = ConfigDict(frozen=True)

    sequence_id: str
    events: tuple[Event, ...]


class Corpus(BaseModel):
    model_config = ConfigDict(frozen=True)

    corpus_id: str
    name: str
    description: Optional[str]
    sequences: tuple[Sequence, ...]

    @property
    def size(self) -> int:
        return len(self.sequences)


class MineRequest(BaseModel):
    corpus_id: str
    min_support: StrictNumber = Field(description="int >=1 absolute, or float in (0,1] fraction")
    max_gap_position: Optional[StrictInt] = Field(default=None)
    max_gap_time: Optional[StrictNumber] = Field(default=None)
    max_pattern_length: Optional[StrictInt] = Field(default=None)
    include_embeddings: bool = True


class Embedding(BaseModel):
    """Concrete occurrence evidence: matched positions inside one sequence."""

    model_config = ConfigDict(frozen=True)

    sequence_id: str
    positions: tuple[int, ...]
    symbols: tuple[str, ...]
    timestamps: tuple[Optional[float], ...]
    position_gaps: tuple[int, ...]
    time_gaps: tuple[Optional[float], ...]


class SequenceSupport(BaseModel):
    model_config = ConfigDict(frozen=True)

    sequence_id: str
    embeddings: tuple[Embedding, ...]


class PatternResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    pattern: tuple[str, ...]
    length: int
    support: int
    supporting_sequence_ids: tuple[str, ...]
    occurrences: int
    evidence: tuple[SequenceSupport, ...]


class MineResponse(BaseModel):
    run_id: str
    corpus_id: str
    corpus_size: int
    constraints: dict
    duration_ms: float
    pattern_count: int
    patterns: list[PatternResult]
