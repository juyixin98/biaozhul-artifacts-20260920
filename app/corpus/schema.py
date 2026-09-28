"""Corpus specification: input validation at the system boundary.

Rules enforced here:
- sequence ids are unique within a corpus
- every sequence has at least one event; symbols are non-empty
- timestamps are non-decreasing within a sequence
- timestamp presence is consistent across the whole corpus (all events
  carry one, or none do) so that time-gap semantics stay unambiguous
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.domain import Event, Sequence


class EventSpec(BaseModel):
    symbol: str = Field(min_length=1, max_length=64)
    timestamp: float | None = None


class SequenceSpec(BaseModel):
    sequence_id: str = Field(min_length=1, max_length=128)
    events: list[EventSpec] = Field(min_length=1)

    @field_validator("events")
    @classmethod
    def timestamps_non_decreasing(cls, events: list[EventSpec]) -> list[EventSpec]:
        stamps = [e.timestamp for e in events if e.timestamp is not None]
        if any(b < a for a, b in zip(stamps, stamps[1:])):
            raise ValueError("timestamps must be non-decreasing within a sequence")
        return events


class CorpusSpec(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    sequences: list[SequenceSpec] = Field(min_length=1)

    @field_validator("sequences")
    @classmethod
    def unique_sequence_ids(cls, seqs: list[SequenceSpec]) -> list[SequenceSpec]:
        ids = [s.sequence_id for s in seqs]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate sequence_id in corpus")
        return seqs

    @model_validator(mode="after")
    def consistent_timestamps(self) -> "CorpusSpec":
        presence = {
            e.timestamp is not None
            for s in self.sequences
            for e in s.events
        }
        if len(presence) > 1:
            raise ValueError(
                "timestamps must be present on all events or on none"
            )
        return self

    @property
    def has_timestamps(self) -> bool:
        return self.sequences[0].events[0].timestamp is not None

    def to_domain(self) -> list[Sequence]:
        return [
            Sequence(
                sequence_id=s.sequence_id,
                events=tuple(Event(symbol=e.symbol, timestamp=e.timestamp) for e in s.events),
            )
            for s in self.sequences
        ]
