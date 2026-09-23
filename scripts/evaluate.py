"""Offline evaluation of the tracker against fixture ground truth.

The tracker is fed **only** positions/labels (object_id is stripped before the
call).  Per frame, surviving confirmed tracks are matched to ground-truth
detections by a greedy nearest-neighbour assignment inside a gate, and the
per-track mapping is compared frame to frame:

* ``id_switches``  - frames where a track's mapped object_id changes
* ``mean_euclidean`` / ``max_euclidean`` - track-vs-truth position error
* ``track_count``  - distinct confirmed track ids created
* ``deleted_count``- tracks deleted by the miss threshold
* duplicate detections per frame must be fused (asserted in the caller tests)

Usage::

    python -m scripts.evaluate --fixture fixtures/crossing.json
    python -m scripts.evaluate --all
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.mot.tracker import (  # noqa: E402
    Detection,
    MultiTargetTracker,
    TrackerConfig,
)

MATCH_GATE = 1.0  # metres; post-update confirmed track to GT position


def evaluate(fixture: dict, verbose: bool = False) -> dict:
    cfg = TrackerConfig(**fixture["tracker"])
    tracker = MultiTargetTracker(cfg)

    id_switches = 0
    errors: list[float] = []
    mapping: dict[int, str] = {}
    confirmed_seen: set[int] = set()
    per_frame_rows = []

    for fr in fixture["frames"]:
        # Ground truth stays here; only x/y/label crosses the boundary.
        gt = [{"id": d["object_id"], "x": d["x"], "y": d["y"]} for d in fr["detections"]]
        feed = [Detection(d["x"], d["y"], d.get("label")) for d in fr["detections"]]
        res = tracker.step(fr["frame_id"], fr["timestamp"], feed)
        confirmed_seen.update(t.track_id for t in res.tracks if t.status == "confirmed")

        # Greedy gated matching between post-update confirmed tracks and GT.
        tracks = [t for t in res.tracks if t.status == "confirmed"]
        pairs = []
        for t in tracks:
            for g in gt:
                dist = float(np.hypot(t.position[0] - g["x"], t.position[1] - g["y"]))
                if dist <= MATCH_GATE:
                    pairs.append((dist, t.track_id, g["id"]))
        pairs.sort()
        used_t, used_g = set(), set()
        frame_map: dict[int, str] = {}
        for dist, tid, gid in pairs:
            if tid in used_t or gid in used_g:
                continue
            used_t.add(tid)
            used_g.add(gid)
            frame_map[tid] = gid
            errors.append(dist)

        for tid, gid in frame_map.items():
            prev = mapping.get(tid)
            if prev is not None and prev != gid:
                id_switches += 1
                if verbose:
                    per_frame_rows.append(
                        f"  frame {fr['frame_id']}: track {tid} switched {prev} -> {gid}"
                    )
            mapping[tid] = gid

    deleted = _count_deleted(fixture, cfg)

    return {
        "fixture": fixture["name"],
        "frames": len(fixture["frames"]),
        "id_switches": id_switches,
        "track_count": len(confirmed_seen),
        "mean_euclidean": float(np.mean(errors)) if errors else 0.0,
        "max_euclidean": float(np.max(errors)) if errors else 0.0,
        "matched_points": len(errors),
        "deleted_count": deleted,
        "notes": "\n".join(per_frame_rows),
    }


def _count_deleted(fixture: dict, cfg: TrackerConfig) -> int:
    tracker = MultiTargetTracker(cfg)
    deleted = 0
    for fr in fixture["frames"]:
        feed = [Detection(d["x"], d["y"], d.get("label")) for d in fr["detections"]]
        res = tracker.step(fr["frame_id"], fr["timestamp"], feed)
        deleted += len(res.deleted_tracks)
    return deleted


def run_file(path: str, verbose: bool = False) -> dict:
    with open(path, encoding="utf-8") as fh:
        fixture = json.load(fh)
    return evaluate(fixture, verbose=verbose)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(__file__))
    if args.all:
        paths = sorted(glob.glob(os.path.join(root, "fixtures", "*.json")))
    elif args.fixture:
        paths = [args.fixture]
    else:
        ap.error("give --fixture PATH or --all")
    rows = [run_file(p, args.verbose) for p in paths]
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
