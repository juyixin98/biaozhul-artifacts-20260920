"""Multi-target tracker: constant-velocity Kalman prediction, Mahalanobis
gating and one-to-one Hungarian assignment.

Design properties required by the task:

* The elapsed time between frames participates in the state transition.
* Frames must arrive strictly in order: a repeated ``frame_id`` is rejected at
  the session layer (see ``app.service``); a non-increasing ``frame_id`` or a
  timestamp not strictly greater than the previous one is rejected here with
  :class:`FrameOrderError`.
* New tracks start tentative and are confirmed only after ``hits_to_confirm``
  *consecutive* hits.
* Confirmed tracks are deleted after ``max_misses`` consecutive missed
  updates; tentative tracks are deleted after ``tentative_max_misses``.
* Duplicate detections inside one frame are fused before association, so the
  same detection delivered twice can never inflate the ID count.
* Truth IDs are never accepted by this module: detections only carry
  ``x, y`` (and an optional opaque client-side detection label used solely for
  duplicate diagnostics in the response).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.stats import chi2

from .kalman import KalmanBox2D


class TrackStatus(str, Enum):
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"


class FrameOrderError(ValueError):
    """Raised when frames arrive out of order or time goes backwards."""


@dataclass(frozen=True)
class Detection:
    """One 2-D detection. ``label`` is an opaque client tag, never an ID for
    association purposes; it is echoed back for duplicate diagnostics only."""

    x: float
    y: float
    label: str | None = None


@dataclass
class TrackerConfig:
    q: float = 1.0
    """Kalman process-noise intensity."""
    r: float = 0.05**2
    """Per-axis measurement-noise variance."""
    gate_pvalue: float = 0.99
    """Chi-square acceptance probability for the Mahalanobis gate."""
    gate_threshold: float | None = None
    """Explicit squared-Mahalanobis gate; overrides ``gate_pvalue`` if set."""
    hits_to_confirm: int = 3
    max_misses: int = 4
    tentative_max_misses: int = 2
    duplicate_eps: float = 0.1
    """Detections closer than this (Euclidean) within a frame are duplicates."""
    init_vel_var: float = 100.0


@dataclass
class Track:
    track_id: int
    kf: KalmanBox2D
    status: TrackStatus = TrackStatus.TENTATIVE
    hits: int = 1
    """Consecutive hits (reset to 0 on a miss)."""
    misses: int = 0
    """Consecutive misses."""
    age: int = 1
    """Frames since birth, including the birth frame."""
    total_hits: int = 1
    last_update_frame: int | None = None


def chi2_gate(pvalue: float) -> float:
    """Squared-Mahalanobis threshold for a 2-D chi-square gate."""
    return float(chi2.ppf(pvalue, df=2))


@dataclass
class Association:
    track_id: int
    detection_index: int
    prediction: tuple[float, float]
    measurement: tuple[float, float]
    mahalanobis_sq: float
    euclidean: float
    gate_threshold: float
    runner_up: dict | None = None
    selection_basis: str = ""


@dataclass
class TrackSnapshot:
    track_id: int
    status: str
    position: tuple[float, float]
    velocity: tuple[float, float]
    hits: int
    misses: int
    age: int
    total_hits: int
    predicted_position: tuple[float, float]
    mahalanobis_sq: float | None
    gate_threshold: float
    in_gate: bool | None


@dataclass
class FrameResult:
    frame_id: int
    timestamp: float
    dt: float
    gate_threshold: float
    associations: list[Association] = field(default_factory=list)
    unmatched_tracks: list[TrackSnapshot] = field(default_factory=list)
    unmatched_detections: list[dict] = field(default_factory=list)
    new_tracks: list[int] = field(default_factory=list)
    confirmed_tracks: list[int] = field(default_factory=list)
    deleted_tracks: list[TrackSnapshot] = field(default_factory=list)
    tracks: list[TrackSnapshot] = field(default_factory=list)
    duplicate_detections: list[dict] = field(default_factory=list)
    cost_matrix: list[list[float | None]] = field(default_factory=list)
    rows_track_ids: list[int] = field(default_factory=list)


def _euclidean(a: tuple[float, float], b: tuple[float, float]) -> float:
    return float(np.hypot(a[0] - b[0], a[1] - b[1]))


class MultiTargetTracker:
    """Stateful frame-by-frame multi-object tracker."""

    def __init__(self, config: TrackerConfig | None = None) -> None:
        self.config = config or TrackerConfig()
        self._tracks: dict[int, Track] = {}
        self._next_id = 1
        self.last_frame_id: int | None = None
        self.last_timestamp: float | None = None
        self.gate_threshold = (
            float(self.config.gate_threshold)
            if self.config.gate_threshold is not None
            else chi2_gate(self.config.gate_pvalue)
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _snapshot(self, tr: Track, predicted: np.ndarray | None = None) -> TrackSnapshot:
        # ``position``/``velocity`` are the current filter state (post-update
        # when the track was hit this frame, pure prediction when missed);
        # ``predicted_position`` is the pre-update prediction for this frame.
        pred = tr.kf.position if predicted is None else np.asarray(predicted)
        return TrackSnapshot(
            track_id=tr.track_id,
            status=tr.status.value,
            position=(float(tr.kf.x[0]), float(tr.kf.x[1])),
            velocity=(float(tr.kf.x[2]), float(tr.kf.x[3])),
            hits=tr.hits,
            misses=tr.misses,
            age=tr.age,
            total_hits=tr.total_hits,
            predicted_position=(float(pred[0]), float(pred[1])),
            mahalanobis_sq=None,
            gate_threshold=self.gate_threshold,
            in_gate=None,
        )

    def _fuse_duplicates(self, dets: list[Detection]) -> tuple[list[Detection], list[dict]]:
        """Greedy single-linkage clustering of detections within ``duplicate_eps``.

        Each cluster is merged into its centroid; the dropped members are
        reported as duplicates.  This guarantees that the same physical
        observation delivered twice in one frame cannot create two tracks.
        """
        kept: list[Detection] = []
        dropped: list[dict] = []
        used = [False] * len(dets)
        eps = self.config.duplicate_eps
        for i, d in enumerate(dets):
            if used[i]:
                continue
            members = [i]
            used[i] = True
            for j in range(i + 1, len(dets)):
                if used[j]:
                    continue
                dj = dets[j]
                if any(
                    np.hypot(dets[m].x - dj.x, dets[m].y - dj.y) <= eps for m in members
                ):
                    members.append(j)
                    used[j] = True
            if len(members) == 1:
                kept.append(d)
                continue
            cx = float(np.mean([dets[m].x for m in members]))
            cy = float(np.mean([dets[m].y for m in members]))
            labels = [dets[m].label for m in members if dets[m].label is not None]
            kept.append(Detection(cx, cy, labels[0] if labels else None))
            for m in members[1:]:
                dropped.append(
                    {
                        "x": dets[m].x,
                        "y": dets[m].y,
                        "label": dets[m].label,
                        "merged_with_index": len(kept) - 1,
                        "centroid": [cx, cy],
                    }
                )
        return kept, dropped

    # ------------------------------------------------------------------ #
    # Main step
    # ------------------------------------------------------------------ #
    def step(
        self,
        frame_id: int,
        timestamp: float,
        detections: list[Detection | tuple[float, float] | dict],
    ) -> FrameResult:
        # ---- Order enforcement -----------------------------------------
        if self.last_frame_id is not None and frame_id <= self.last_frame_id:
            raise FrameOrderError(
                f"frame_id {frame_id} is not greater than previous "
                f"{self.last_frame_id}; re-submitting an already processed "
                "frame is rejected (use the stored response of the original)"
            )
        if self.last_timestamp is not None and not timestamp > self.last_timestamp:
            raise FrameOrderError(
                f"timestamp {timestamp} is not strictly greater than previous "
                f"{self.last_timestamp}"
            )
        dt = 0.0 if self.last_timestamp is None else float(timestamp - self.last_timestamp)

        norm_dets: list[Detection] = []
        for d in detections:
            if isinstance(d, Detection):
                norm_dets.append(d)
            elif isinstance(d, dict):
                norm_dets.append(
                    Detection(float(d["x"]), float(d["y"]), d.get("label"))
                )
            else:
                norm_dets.append(Detection(float(d[0]), float(d[1])))
        dets, dup_report = self._fuse_duplicates(norm_dets)

        result = FrameResult(
            frame_id=frame_id,
            timestamp=float(timestamp),
            dt=dt,
            gate_threshold=self.gate_threshold,
            duplicate_detections=dup_report,
        )

        # ---- Predict every live track with the real elapsed time --------
        predictions: dict[int, np.ndarray] = {}
        for tr in self._tracks.values():
            predictions[tr.track_id] = tr.kf.predict(dt if dt > 0 else 0.0)

        # ---- Gated cost matrix -----------------------------------------
        track_ids = sorted(self._tracks)
        gated_cost = np.full((len(track_ids), len(dets)), np.inf, dtype=float)
        raw_cost: list[list[float | None]] = [
            [None] * len(dets) for _ in track_ids
        ]
        for i, tid in enumerate(track_ids):
            tr = self._tracks[tid]
            for j, det in enumerate(dets):
                d2 = tr.kf.mahalanobis((det.x, det.y))
                raw_cost[i][j] = float(d2)
                if d2 <= self.gate_threshold:
                    gated_cost[i, j] = d2
        result.cost_matrix = raw_cost
        result.rows_track_ids = list(track_ids)

        # ---- Hungarian assignment over feasible pairs ------------------
        feasible_rows = [
            i for i in range(len(track_ids)) if np.any(np.isfinite(gated_cost[i]))
        ]
        feasible_cols = [
            j for j in range(len(dets)) if np.any(np.isfinite(gated_cost[:, j]))
        ]
        matches: list[tuple[int, int]] = []
        if feasible_rows and feasible_cols:
            sub = gated_cost[np.ix_(feasible_rows, feasible_cols)]
            row_ind, col_ind = linear_sum_assignment(sub)
            for r, c in zip(row_ind, col_ind):
                if np.isfinite(sub[r, c]):
                    matches.append((feasible_rows[r], feasible_cols[c]))

        matched_rows = {m[0] for m in matches}
        matched_cols = {m[1] for m in matches}

        # ---- Update matched tracks --------------------------------------
        for i, j in matches:
            tr = self._tracks[track_ids[i]]
            det = dets[j]
            pred = predictions[tr.track_id]
            tr.kf.update((det.x, det.y))
            tr.hits += 1
            tr.total_hits += 1
            tr.misses = 0
            tr.last_update_frame = frame_id
            if tr.status is TrackStatus.TENTATIVE and tr.hits >= self.config.hits_to_confirm:
                tr.status = TrackStatus.CONFIRMED
                result.confirmed_tracks.append(tr.track_id)

            # Runner-up: smallest gated alternative assignment for this track.
            runner_up = None
            alt = [
                (raw_cost[i][k], k)
                for k in range(len(dets))
                if k != j and raw_cost[i][k] is not None and raw_cost[i][k] <= self.gate_threshold
            ]
            if alt:
                alt.sort(key=lambda t: t[0])
                d2_alt, k = alt[0]
                runner_up = {
                    "detection_index": k,
                    "mahalanobis_sq": d2_alt,
                    "euclidean": _euclidean(
                        (float(pred[0]), float(pred[1])), (dets[k].x, dets[k].y)
                    ),
                }
            result.associations.append(
                Association(
                    track_id=tr.track_id,
                    detection_index=j,
                    prediction=(float(pred[0]), float(pred[1])),
                    measurement=(det.x, det.y),
                    mahalanobis_sq=float(raw_cost[i][j]),
                    euclidean=_euclidean((float(pred[0]), float(pred[1])), (det.x, det.y)),
                    gate_threshold=self.gate_threshold,
                    runner_up=runner_up,
                    selection_basis=(
                        "hungarian-global-minimum: one-to-one assignment minimizing "
                        "the sum of squared Mahalanobis distances over all pairs "
                        "inside the chi-square gate"
                    ),
                )
            )

        # ---- Unmatched tracks: miss bookkeeping -------------------------
        deleted: list[int] = []
        for i, tid in enumerate(track_ids):
            tr = self._tracks[tid]
            if i in matched_rows:
                continue
            tr.misses += 1
            tr.hits = 0
            limit = (
                self.config.tentative_max_misses
                if tr.status is TrackStatus.TENTATIVE
                else self.config.max_misses
            )
            snap = self._snapshot(tr, predictions[tid])
            if tr.misses >= limit:
                deleted.append(tid)
                result.deleted_tracks.append(snap)
            else:
                result.unmatched_tracks.append(snap)

        # ---- Unmatched detections: tentative tracks ---------------------
        for j, det in enumerate(dets):
            if j in matched_cols:
                continue
            tid = self._next_id
            self._next_id += 1
            kf = KalmanBox2D(
                (det.x, det.y),
                q=self.config.q,
                r=self.config.r,
                init_vel_var=self.config.init_vel_var,
            )
            tr = Track(track_id=tid, kf=kf, last_update_frame=frame_id)
            self._tracks[tid] = tr
            result.new_tracks.append(tid)
            result.unmatched_detections.append(
                {
                    "detection_index": j,
                    "x": det.x,
                    "y": det.y,
                    "label": det.label,
                    "new_track_id": tid,
                    "reason": "no gated track candidate; tentative track created",
                }
            )

        for tid in deleted:
            del self._tracks[tid]

        # ---- Age + final snapshots --------------------------------------
        # Age the tracks that existed at the start of this frame; tracks born
        # this frame keep age == 1.
        for tid in track_ids:
            tr = self._tracks.get(tid)
            if tr is not None:
                tr.age += 1
        for tr in self._tracks.values():
            result.tracks.append(self._snapshot(tr))
        result.tracks.sort(key=lambda s: s.track_id)

        self.last_frame_id = frame_id
        self.last_timestamp = float(timestamp)
        return result
