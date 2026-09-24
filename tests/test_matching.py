"""Tests for timestamp association and trajectory parsing."""

from __future__ import annotations

import numpy as np
import pytest

from app.errors import EvaluationError
from app.matching import associate_tracks, build_track
from tests._helpers import pose, q_identity


def _track(times):
    return build_track([pose(t, [t, 0.0, 0.0], q_identity()) for t in times], "test")


def test_duplicate_timestamp_rejected():
    with pytest.raises(EvaluationError) as exc:
        _track([0.0, 0.1, 0.1, 0.2])
    assert exc.value.code == "DUPLICATE_TIMESTAMP"


def test_nearest_match_within_window():
    est = _track([0.0, 0.1, 0.2, 0.3])
    gt = _track([0.005, 0.105, 0.305])
    assoc = associate_tracks(est, gt, max_time_diff=0.02)
    assert list(assoc.gt_idx) == [0, 1, 2]
    assert list(assoc.est_idx) == [0, 1, 3]
    assert np.allclose(assoc.time_diffs, [0.005, 0.005, 0.005])


def test_tie_break_chooses_earlier_ground_truth():
    # est 0.1 is exactly between gt 0.0 and gt 0.2 -> earlier GT wins.
    est = _track([0.1])
    gt = _track([0.0, 0.2])
    assoc = associate_tracks(est, gt, max_time_diff=0.2)
    assert list(assoc.gt_idx) == [0]


def test_no_matches_raises():
    est = _track([10.0, 11.0])
    gt = _track([0.0, 1.0])
    with pytest.raises(EvaluationError) as exc:
        associate_tracks(est, gt, max_time_diff=0.02)
    assert exc.value.code == "NO_MATCHES"


def test_ground_truth_never_paired_twice():
    # Two estimates equally close to one GT pose; only one pair survives.
    est = _track([0.0, 0.01])
    gt = _track([0.005])
    assoc = associate_tracks(est, gt, max_time_diff=0.1)
    assert assoc.gt_idx.shape[0] == 1
