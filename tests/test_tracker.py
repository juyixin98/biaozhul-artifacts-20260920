"""Tracker behaviour: confirmation/deletion, out-of-order rejection, duplicate
fusion, dt-dependent prediction and the truth-id firewall."""

import pytest

from app.mot.tracker import (
    Detection,
    FrameOrderError,
    MultiTargetTracker,
    TrackStatus,
    TrackerConfig,
)


def make_tracker(**over):
    cfg = TrackerConfig(**over)
    return MultiTargetTracker(cfg)


def test_new_track_requires_consecutive_hits_to_confirm():
    t = make_tracker(hits_to_confirm=3)
    r1 = t.step(0, 0.0, [Detection(0, 0)])
    assert r1.new_tracks == [1]
    assert t._tracks[1].status is TrackStatus.TENTATIVE
    r2 = t.step(1, 0.1, [Detection(1, 0)])
    r3 = t.step(2, 0.2, [Detection(2, 0)])
    assert r3.confirmed_tracks == [1]
    # A miss resets the consecutive-hit streak for a tentative track.
    r4 = t.step(3, 0.3, [])
    assert t._tracks[1].hits == 0
    r5 = t.step(4, 0.4, [Detection(4, 0)])
    assert r5.confirmed_tracks == []


def test_consecutive_misses_delete_confirmed_track():
    t = make_tracker(hits_to_confirm=2, max_misses=3)
    for f in range(3):
        t.step(f, 0.1 * f, [Detection(f, 0)])
    assert t._tracks[1].status is TrackStatus.CONFIRMED
    t.step(3, 0.3, [])
    t.step(4, 0.4, [])
    assert 1 in t._tracks
    r = t.step(5, 0.5, [])
    assert [s.track_id for s in r.deleted_tracks] == [1]
    assert 1 not in t._tracks


def test_ids_monotonic_and_not_recycled():
    t = make_tracker(hits_to_confirm=1, tentative_max_misses=1)
    t.step(0, 0.0, [Detection(0, 0)])
    t.step(1, 0.1, [])  # miss -> tentative deleted
    r = t.step(2, 0.2, [Detection(2, 0)])
    assert r.new_tracks == [2]


def test_duplicate_detections_do_not_inflate_ids():
    t = make_tracker(hits_to_confirm=2, duplicate_eps=0.1)
    r0 = t.step(0, 0.0, [Detection(1.0, 1.0, "a"), Detection(1.02, 0.99, "a-copy")])
    assert len(r0.new_tracks) == 1
    assert len(r0.duplicate_detections) == 1
    r1 = t.step(1, 0.1, [Detection(2.0, 1.0), Detection(2.01, 1.01)])
    assert r1.new_tracks == []
    assert len(t._tracks) == 1


def test_out_of_order_frame_id_rejected():
    t = make_tracker()
    t.step(1, 0.1, [])
    with pytest.raises(FrameOrderError):
        t.step(1, 0.2, [])  # equal frame id
    with pytest.raises(FrameOrderError):
        t.step(0, 0.3, [])  # earlier frame id


def test_non_increasing_timestamp_rejected():
    t = make_tracker()
    t.step(0, 1.0, [])
    with pytest.raises(FrameOrderError):
        t.step(1, 1.0, [])
    with pytest.raises(FrameOrderError):
        t.step(2, 0.5, [])


def test_dt_participates_in_transition():
    # Same motion, but frames spaced 1 s apart: prediction must move with dt.
    t = make_tracker(hits_to_confirm=2)
    t.step(0, 0.0, [Detection(0.0, 0.0)])
    r = t.step(1, 1.0, [Detection(10.0, 0.0)])
    as0 = r.associations[0]
    assert r.dt == pytest.approx(1.0)
    # prediction at birth position (velocity unlearned); euclidean reported
    assert as0.euclidean == pytest.approx(10.0, abs=1e-6)

    t2 = make_tracker(hits_to_confirm=4)
    for f in range(3):
        t2.step(f, f * 1.0, [Detection(10.0 * f, 0.0)])
    r2 = t2.step(3, 3.0, [Detection(30.0, 0.0)])
    # learned velocity ~10 m/s, prediction after dt=1 should be near 30
    assert r2.associations[0].euclidean < 2.0


def test_empty_frame_predicts_all_tracks_as_unmatched():
    t = make_tracker(hits_to_confirm=2, max_misses=5)
    for f in range(2):
        t.step(f, 0.1 * f, [Detection(f, 0), Detection(f, 5)])
    r = t.step(2, 0.2, [])
    assert r.associations == []
    assert {s.track_id for s in r.unmatched_tracks} == {1, 2}


def test_association_reports_prediction_distance_and_basis():
    t = make_tracker(hits_to_confirm=3)
    t.step(0, 0.0, [Detection(0.0, 0.0)])
    t.step(1, 0.1, [Detection(1.0, 0.0)])
    r = t.step(2, 0.2, [Detection(2.0, 0.0)])
    a = r.associations[0]
    assert a.selection_basis.startswith("hungarian-global-minimum")
    assert a.mahalanobis_sq <= a.gate_threshold
    assert a.euclidean >= 0.0
    assert len(a.prediction) == 2


def test_detection_dict_never_carries_truth_id_into_state():
    # The tracker API surface accepts dicts with x/y only; extra truth
    # semantics have no slot on the Detection dataclass.
    t = make_tracker(hits_to_confirm=2)
    t.step(0, 0.0, [{"x": 0.0, "y": 0.0}])
    r = t.step(1, 0.1, [{"x": 1.0, "y": 0.0, "label": "detector-row-7"}])
    det = r.unmatched_detections
    assert all("object_id" not in d for d in det)
    assert t._tracks[1].kf.x.shape == (4,)


def test_two_objects_keep_identity_through_near_crossing():
    # Crossing paths with 0.8 m closest approach and 1 cm noise: no switch.
    t = make_tracker(
        q=0.5,
        r=0.01**2,
        gate_threshold=7.0,
        hits_to_confirm=2,
        max_misses=5,
        duplicate_eps=0.05,
        init_vel_var=25.0,
    )
    n = 25
    mapping = {}
    switches = 0
    for f in range(n):
        ts = f * 0.1
        r = t.step(
            f,
            ts,
            [
                Detection(8.0 * ts, 4.0 * ts, "A"),
                Detection(20.0 - 8.0 * ts, 4.0 * ts, "B"),
            ],
        )
        for a in r.associations:
            x, y = a.measurement
            truth = "A" if abs(x - 8.0 * ts) < abs(x - (20.0 - 8.0 * ts)) else "B"
            if a.track_id in mapping and mapping[a.track_id] != truth:
                switches += 1
            mapping[a.track_id] = truth
    assert switches == 0
    assert len(mapping) == 2
