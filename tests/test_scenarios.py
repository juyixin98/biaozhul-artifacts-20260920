"""Fixture-based acceptance tests: crossing, short occlusion, duplicated
detections and empty frames. Ground truth is scored offline; the tracker
never sees ``object_id``."""

import json
import os

import pytest

from scripts.evaluate import MATCH_GATE, evaluate
from scripts.make_fixtures import SCENARIOS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "fixtures")

EXPECT = {
    "crossing": {"id_switches": 0, "track_count": 2, "max_err": 0.05},
    "occlusion": {"id_switches": 0, "track_count": 2, "max_err": 0.2},
    "duplicates": {"id_switches": 0, "track_count": 2, "max_err": 0.05},
    "empty_frames": {"id_switches": 0, "track_count": 4, "max_err": 0.05},
}


@pytest.fixture(scope="module", autouse=True)
def ensure_fixtures():
    if not os.path.exists(os.path.join(FIX, "crossing.json")):
        from scripts.make_fixtures import main as make

        make()


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario_metrics(name):
    with open(os.path.join(FIX, f"{name}.json"), encoding="utf-8") as fh:
        fixture = json.load(fh)
    metrics = evaluate(fixture)
    exp = EXPECT[name]
    assert metrics["id_switches"] == exp["id_switches"], metrics
    assert metrics["track_count"] == exp["track_count"], metrics
    assert metrics["max_euclidean"] <= exp["max_err"], metrics
    assert metrics["matched_points"] > 0


def test_empty_frames_delete_then_rebirth():
    with open(os.path.join(FIX, "empty_frames.json"), encoding="utf-8") as fh:
        fixture = json.load(fh)
    metrics = evaluate(fixture)
    assert metrics["deleted_count"] == 2


def test_duplicates_are_fused_every_frame():
    from app.mot.tracker import Detection, MultiTargetTracker, TrackerConfig

    with open(os.path.join(FIX, "duplicates.json"), encoding="utf-8") as fh:
        fixture = json.load(fh)
    tracker = MultiTargetTracker(TrackerConfig(**fixture["tracker"]))
    raw_counts, fused_counts = [], []
    for fr in fixture["frames"]:
        feed = [Detection(d["x"], d["y"], d.get("label")) for d in fr["detections"]]
        dets, dropped = tracker._fuse_duplicates(feed)
        raw_counts.append(len(feed))
        fused_counts.append(len(dets))
        res = tracker.step(fr["frame_id"], fr["timestamp"], feed)
        assert res.duplicate_detections  # one dropped copy reported each frame
    assert set(raw_counts) == {3}  # 2 copies of A + 1 of B
    assert set(fused_counts) == {2}  # fused to exactly two observations


def test_tracker_never_receives_object_id():
    """Static guarantee via the feed boundary: the Detection objects handed
    to the tracker cannot carry object_id, and the fixture field is read only
    inside the evaluator."""

    import inspect

    from app.mot import tracker as tracker_mod

    src = inspect.getsource(tracker_mod)
    assert "object_id" not in src
    with open(os.path.join(FIX, "crossing.json"), encoding="utf-8") as fh:
        fixture = json.load(fh)
    for fr in fixture["frames"]:
        for d in fr["detections"]:
            assert "object_id" in d  # truth exists in the fixture file ...
    # ... but is stripped at the evaluator boundary.
    fr0 = fixture["frames"][0]
    feed = [{k: v for k, v in d.items() if k != "object_id"} for d in fr0["detections"]]
    assert all("object_id" not in d for d in feed)
