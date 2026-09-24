"""Core record types for the point-in-time join service.

Timestamps are integer epoch seconds (any monotone integer unit works, as
long as event_ts and ingest_ts share the same clock/unit).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeatureRecord:
    """One version of one feature value for one entity.

    Attributes:
        entity_id: Entity the feature describes (e.g. user id).
        feature: Feature name (e.g. "click_rate_7d").
        value: Numeric feature value.
        event_ts: When the fact became true in the world (event time).
        ingest_ts: When this version landed in the feature store
            (ingestion / system time). A correction to the same fact is a
            new record with the same event_ts and a larger ingest_ts.
    """

    entity_id: str
    feature: str
    value: float
    event_ts: int
    ingest_ts: int

    def validate(self) -> "FeatureRecord":
        if not self.entity_id:
            raise ValueError("entity_id must be non-empty")
        if not self.feature:
            raise ValueError("feature must be non-empty")
        if self.event_ts < 0 or self.ingest_ts < 0:
            raise ValueError("timestamps must be non-negative")
        return self


@dataclass(frozen=True)
class SpineRow:
    """One row of the join spine: an entity observed at an event time."""

    entity_id: str
    event_ts: int

    def validate(self) -> "SpineRow":
        if not self.entity_id:
            raise ValueError("entity_id must be non-empty")
        if self.event_ts < 0:
            raise ValueError("event_ts must be non-negative")
        return self
