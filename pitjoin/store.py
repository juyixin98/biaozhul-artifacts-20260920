"""Feature store: ingests bitemporal feature records and indexes them.

Records are grouped by (entity_id, feature) and each group is kept as
NumPy arrays sorted by (event_ts, ingest_ts), so the join can use binary
search instead of a full scan. The index is rebuilt lazily after ingest.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .records import FeatureRecord


@dataclass
class _Group:
    """Sorted index for one (entity_id, feature) pair."""

    event_ts: np.ndarray  # int64, sorted ascending (primary key)
    ingest_ts: np.ndarray  # int64, ascending within equal event_ts
    value: np.ndarray  # float64, aligned with the two timestamp arrays


class FeatureStore:
    """In-memory bitemporal feature store.

    Ingestion is append-only: a correction is stored as a new record with
    the same (entity_id, feature, event_ts) and a larger ingest_ts, never
    as an in-place update. History is therefore fully preserved and any
    past point-in-time view remains reproducible.
    """

    def __init__(self) -> None:
        self._records: list[FeatureRecord] = []
        self._index: dict[tuple[str, str], _Group] = {}
        self._dirty = False

    def ingest(self, records: list[FeatureRecord]) -> int:
        """Append records to the store. Returns the number ingested."""
        for rec in records:
            self._records.append(rec.validate())
        if records:
            self._dirty = True
        return len(records)

    def __len__(self) -> int:
        return len(self._records)

    def features(self) -> list[str]:
        return sorted({r.feature for r in self._records})

    def entities(self) -> list[str]:
        return sorted({r.entity_id for r in self._records})

    def records(self) -> list[FeatureRecord]:
        """Return a copy of all ingested records (unsorted, ingest order)."""
        return list(self._records)

    def group(self, entity_id: str, feature: str) -> _Group | None:
        """Return the sorted index for one (entity, feature), or None."""
        self._rebuild_if_dirty()
        return self._index.get((entity_id, feature))

    def _rebuild_if_dirty(self) -> None:
        if not self._dirty:
            return
        groups: dict[tuple[str, str], list[FeatureRecord]] = {}
        for rec in self._records:
            groups.setdefault((rec.entity_id, rec.feature), []).append(rec)

        index: dict[tuple[str, str], _Group] = {}
        for key, recs in groups.items():
            event_ts = np.array([r.event_ts for r in recs], dtype=np.int64)
            ingest_ts = np.array([r.ingest_ts for r in recs], dtype=np.int64)
            value = np.array([r.value for r in recs], dtype=np.float64)
            # lexsort uses the LAST key as primary: sort by event_ts, then
            # ingest_ts within equal event_ts.
            order = np.lexsort((ingest_ts, event_ts))
            index[key] = _Group(
                event_ts=event_ts[order],
                ingest_ts=ingest_ts[order],
                value=value[order],
            )
        self._index = index
        self._dirty = False
