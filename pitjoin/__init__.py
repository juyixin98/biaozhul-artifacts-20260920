"""pitjoin: point-in-time correct offline feature join (bitemporal).

Selects, for each (entity, event time) spine row, the feature version that
was actually available at that moment: event_ts <= spine ts AND
ingest_ts <= spine ts. This excludes late-arriving data and post-hoc
revisions, preventing training/serving skew and label leakage.
"""

from .records import FeatureRecord, SpineRow
from .store import FeatureStore
from .join import JoinedFeature, join_as_matrix, point_in_time_join

__all__ = [
    "FeatureRecord",
    "SpineRow",
    "FeatureStore",
    "JoinedFeature",
    "point_in_time_join",
    "join_as_matrix",
]

__version__ = "0.1.0"
