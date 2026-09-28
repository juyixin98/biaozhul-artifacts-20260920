"""Domain model for the mining kernel.

Semantics:
- A Sequence is an ordered list of Events. Timestamps, when present, are
  non-decreasing within a sequence (enforced by corpus validation).
- An embedding of a pattern in a sequence is a strictly increasing tuple of
  event positions whose symbols match the pattern and whose consecutive
  gaps satisfy the GapConstraints.
- Support of a pattern = number of DISTINCT sequences with >= 1 embedding.
  Multiple embeddings inside one sequence never raise the count.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Event:
    symbol: str
    timestamp: float | None = None


@dataclass(frozen=True)
class Sequence:
    sequence_id: str
    events: tuple[Event, ...]


@dataclass(frozen=True)
class GapConstraints:
    """Position gap and time gap are declared independently.

    max_pos_gap: max allowed index difference between two consecutive matched
        events (j - i <= max_pos_gap). None = unbounded.
    max_time_gap: max allowed timestamp difference between two consecutive
        matched events. None = unbounded. Requires timestamped events.
    """

    max_pos_gap: int | None = None
    max_time_gap: float | None = None


@dataclass(frozen=True)
class EmbeddingEvidence:
    sequence_id: str
    positions: tuple[int, ...]


@dataclass
class PatternResult:
    pattern: tuple[str, ...]
    support: int
    embeddings: list[EmbeddingEvidence] = field(default_factory=list)
    evidence_complete: bool = True
