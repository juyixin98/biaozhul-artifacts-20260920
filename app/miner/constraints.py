"""Query constraint validation and the shared, separately-declared gap rules.

Position gap and time gap are *independent* constraints:

* position gap between two matched events = index distance ``p - q``
  (adjacent events have gap 1);
* time gap = ``timestamp(p) - timestamp(q)`` (equal timestamps give gap 0,
  which a limit of 0 accepts);

a pair must satisfy **both** declared limits; an undeclared limit is
unbounded.  These rules live in one place and are reused by the projection
engine, the evidence enumerator and (indirectly) the independent test oracle,
so constraint semantics cannot drift between modules.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from app.config import settings
from app.corpus.spec import corpus_has_timestamps
from app.errors import InvalidConstraintError
from app.models import Corpus, MineRequest


@dataclass(frozen=True)
class ValidatedConstraints:
    """Resolved, immutable constraints used by the mining engine.

    ``min_support`` here is always an absolute sequence count.
    """

    min_support: int
    max_gap_position: Optional[int]
    max_gap_time: Optional[float]
    max_pattern_length: int

    def pair_ok(self, q: int, p: int,
                tq: Optional[float], tp: Optional[float]) -> bool:
        """Whether two matched positions form a legal consecutive pair."""
        if self.max_gap_position is not None and (p - q) > self.max_gap_position:
            return False
        if self.max_gap_time is not None:
            if tp is None or tq is None:
                return False
            if (tp - tq) > self.max_gap_time:
                return False
        return True


def validate_query(corpus: Corpus, request: MineRequest) -> ValidatedConstraints:
    """Validate a mining request against a concrete corpus.

    Raises a categorized :class:`InvalidConstraintError` on any semantic
    problem.  Booleans are rejected as ``min_support`` (``bool`` is a subclass
    of ``int`` in Python and must not sneak through).
    """
    raw = request.min_support
    # NOTE: booleans and non-numeric types are already rejected at the model
    # layer by StrictNumber, so here raw is a genuine int or float.
    if isinstance(raw, int):
        if raw < 1:
            raise InvalidConstraintError(
                "absolute min_support must be >= 1", details={"value": raw}
            )
        absolute = raw
    else:
        # raw is a genuine float here (StrictNumber already rejected other
        # types and booleans at the request-model layer).
        if not math.isfinite(raw) or not (0.0 < raw <= 1.0):
            raise InvalidConstraintError(
                "fractional min_support must be within (0, 1]",
                details={"value": raw},
            )
        absolute = max(1, math.ceil(raw * corpus.size))

    max_gap_position = request.max_gap_position
    if max_gap_position is not None and max_gap_position < 1:
        raise InvalidConstraintError(
            "max_gap_position must be >= 1 (adjacent events have gap 1)",
            details={"value": max_gap_position},
        )

    max_gap_time = request.max_gap_time
    if max_gap_time is not None:
        if not math.isfinite(max_gap_time) or max_gap_time < 0:
            raise InvalidConstraintError(
                "max_gap_time must be finite and >= 0",
                details={"value": max_gap_time},
            )
        if not corpus_has_timestamps(corpus):
            raise InvalidConstraintError(
                "max_gap_time requires timestamps on every event of every sequence",
                details={"constraint": "max_gap_time"},
            )

    requested_length = request.max_pattern_length
    if requested_length is not None and requested_length < 1:
        raise InvalidConstraintError(
            "max_pattern_length must be >= 1",
            details={"value": requested_length},
        )
    max_pattern_length = min(
        requested_length or settings.max_pattern_length,
        settings.max_pattern_length,
    )
    return ValidatedConstraints(
        min_support=absolute,
        max_gap_position=max_gap_position,
        max_gap_time=max_gap_time,
        max_pattern_length=max_pattern_length,
    )
