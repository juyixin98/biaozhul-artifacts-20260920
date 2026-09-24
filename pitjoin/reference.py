"""Naive pure-Python reference join, used to cross-validate the NumPy path.

`ignore_ingest_ts=True` reproduces the classic leakage bug: it only filters
on event time and takes the latest ingested version, so late arrivals and
post-hoc revisions leak into training data. The demo script uses this to
quantify the accuracy inflation caused by leakage.
"""

from __future__ import annotations

from .records import FeatureRecord, SpineRow


def naive_join_one(
    records: list[FeatureRecord],
    row: SpineRow,
    feature: str,
    ignore_ingest_ts: bool = False,
) -> FeatureRecord | None:
    best: FeatureRecord | None = None
    for rec in records:
        if rec.entity_id != row.entity_id or rec.feature != feature:
            continue
        if rec.event_ts > row.event_ts:
            continue
        if not ignore_ingest_ts and rec.ingest_ts > row.event_ts:
            continue
        if best is None or (rec.event_ts, rec.ingest_ts) > (best.event_ts, best.ingest_ts):
            best = rec
    return best


def naive_join(
    records: list[FeatureRecord],
    spine: list[SpineRow],
    features: list[str],
    ignore_ingest_ts: bool = False,
) -> list[list[FeatureRecord | None]]:
    """Returns per-spine-row lists of selected records (None = missing)."""
    return [
        [naive_join_one(records, row, f, ignore_ingest_ts) for f in features]
        for row in spine
    ]
