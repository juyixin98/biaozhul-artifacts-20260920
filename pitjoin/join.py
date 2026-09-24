"""Point-in-time join: pick the feature version visible at each spine time.

Selection rule for spine row (entity, T) and feature F, over all stored
versions of (entity, F):

1. Keep versions with event_ts <= T      (the fact already existed at T)
2. Keep versions with ingest_ts <= T     (the fact was IN THE STORE at T —
   excludes late arrivals and post-hoc revisions, i.e. leakage)
3. Among survivors pick the largest event_ts (freshest fact);
   break ties by the largest ingest_ts (latest available correction).

If nothing survives, the feature is missing for that spine row and the
reason is reported explicitly instead of silently leaking a future value.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from .records import SpineRow
from .store import FeatureStore

REASON_NO_RECORDS = "MISSING: no records exist for (entity, feature)"
REASON_ALL_FUTURE = "MISSING: every version has event_ts > spine ts (fact did not exist yet)"
REASON_ALL_LATE = "MISSING: versions exist with event_ts <= spine ts but all were ingested after spine ts (late data)"


@dataclass(frozen=True)
class JoinedFeature:
    """Result of joining one feature onto one spine row, with evidence."""

    entity_id: str
    feature: str
    spine_ts: int
    value: float | None
    selected_event_ts: int | None
    selected_ingest_ts: int | None
    reason: str
    versions_total: int
    rejected_future_event: int  # event_ts > spine_ts
    rejected_late_ingest: int  # event_ts <= spine_ts but ingest_ts > spine_ts
    candidates: int  # passed both time filters

    def to_dict(self) -> dict:
        return asdict(self)


def _select_one(store: FeatureStore, entity_id: str, feature: str, spine_ts: int) -> JoinedFeature:
    group = store.group(entity_id, feature)
    if group is None or group.event_ts.size == 0:
        return JoinedFeature(
            entity_id, feature, spine_ts, None, None, None,
            REASON_NO_RECORDS, 0, 0, 0, 0,
        )

    total = int(group.event_ts.size)
    # Prefix of versions whose event time already existed at spine_ts.
    prefix_end = int(np.searchsorted(group.event_ts, spine_ts, side="right"))
    future_event = total - prefix_end

    # Within the prefix, scan newest-first (sorted by event_ts, then
    # ingest_ts) for the first version that had been ingested by spine_ts.
    chosen = -1
    late_ingest = 0
    for i in range(prefix_end - 1, -1, -1):
        if group.ingest_ts[i] <= spine_ts:
            chosen = i
            break
        late_ingest += 1
    # late_ingest above only counts versions newer than the chosen one;
    # recount exactly for the report.
    if prefix_end > 0:
        late_ingest = int(np.count_nonzero(group.ingest_ts[:prefix_end] > spine_ts))
    candidates = prefix_end - late_ingest

    if chosen < 0:
        reason = REASON_ALL_LATE if prefix_end > 0 else REASON_ALL_FUTURE
        return JoinedFeature(
            entity_id, feature, spine_ts, None, None, None,
            reason, total, future_event, late_ingest, candidates,
        )

    reason = (
        f"SELECTED: newest version with event_ts<={spine_ts} and "
        f"ingest_ts<={spine_ts} (event_ts={int(group.event_ts[chosen])}, "
        f"ingest_ts={int(group.ingest_ts[chosen])}); "
        f"excluded {future_event} future-event and {late_ingest} late-ingest version(s)"
    )
    return JoinedFeature(
        entity_id, feature, spine_ts,
        float(group.value[chosen]),
        int(group.event_ts[chosen]),
        int(group.ingest_ts[chosen]),
        reason, total, future_event, late_ingest, candidates,
    )


def point_in_time_join(
    store: FeatureStore,
    spine: list[SpineRow],
    features: list[str],
) -> list[dict]:
    """Join `features` onto each spine row at its own event time.

    Returns one dict per (spine row, feature) with the selected value and
    the full selection rationale (which version, why, what was excluded).
    """
    if not features:
        raise ValueError("features must be non-empty")
    results: list[dict] = []
    for row in spine:
        row.validate()
        for feature in features:
            results.append(_select_one(store, row.entity_id, feature, row.event_ts).to_dict())
    return results


def join_as_matrix(
    store: FeatureStore,
    spine: list[SpineRow],
    features: list[str],
) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """Vectorized view: (n_spine, n_features) value matrix + missing mask.

    Missing features are NaN in the matrix and True in the mask. The
    per-cell rationale list is returned alongside for auditability.
    """
    details = point_in_time_join(store, spine, features)
    n_rows, n_feats = len(spine), len(features)
    matrix = np.full((n_rows, n_feats), np.nan, dtype=np.float64)
    missing = np.ones((n_rows, n_feats), dtype=bool)
    for i, d in enumerate(details):
        r, c = divmod(i, n_feats)
        if d["value"] is not None:
            matrix[r, c] = d["value"]
            missing[r, c] = False
    return matrix, missing, details
