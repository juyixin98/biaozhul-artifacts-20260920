"""Deterministic evaluation fixtures.

Each fixture is a scenario with *ground-truth object ids* used **only** for
offline scoring. The tracker under test receives positions and per-frame
detector labels; ``object_id`` never leaves this module.

Run:  python -m scripts.make_fixtures          (writes fixtures/*.json)
"""

from __future__ import annotations

import json
import os

import numpy as np

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures")

# Shared tracker tuning: position measurements are very accurate (r = 1cm^2)
# so the Mahalanobis gate can distinguish cross-pairing during crossings; the
# process noise leaves room for the straight-line motion.
TRACKER_CONFIG = {
    "q": 0.5,
    "r": 0.01**2,
    "gate_pvalue": 0.99,
    "gate_threshold": 7.0,
    "hits_to_confirm": 3,
    "max_misses": 4,
    "tentative_max_misses": 2,
    "duplicate_eps": 0.05,
    "init_vel_var": 25.0,
}


def _frame(fid, ts, dets):
    """dets: list of (object_id, x, y[, extra copies...])."""
    out = {"frame_id": fid, "timestamp": ts, "detections": []}
    for item in dets:
        oid, x, y = item[0], item[1], item[2]
        copies = item[3] if len(item) > 3 else 1
        for k in range(copies):
            jx, jy = (0.0, 0.0) if k == 0 else (0.02 * (k % 2 == 0), -0.015 * (k % 2))
            out["detections"].append(
                {
                    "x": round(x + jx, 6),
                    "y": round(y + jy, 6),
                    "label": f"f{fid}-o{oid}-c{k}",
                    "object_id": oid,
                }
            )
    return out


def crossing(n_frames: int = 25, dt: float = 0.1) -> dict:
    """Two objects move diagonally and cross near the middle.

    A: (0,0)   -> (20,10), velocity (8, 4) m/s
    B: (20,0)  -> (0,10),  velocity (-8, 4) m/s
    Closest approach ~0.8 m; with 1 cm measurement noise the wrong pairing is
    outside the Mahalanobis gate, so IDs must survive the crossing.
    """
    frames = []
    for f in range(n_frames):
        t = f * dt
        frames.append(
            _frame(
                f,
                round(t, 4),
                [("A", 8.0 * t, 4.0 * t), ("B", 20.0 - 8.0 * t, 4.0 * t)],
            )
        )
    return {
        "name": "crossing",
        "description": "Two diagonal trajectories crossing near the middle.",
        "tracker": TRACKER_CONFIG,
        "frames": frames,
    }


def occlusion(n_frames: int = 18, dt: float = 0.1) -> dict:
    """Object A disappears for 3 frames mid-sequence (short occlusion).

    A moves along x at 5 m/s; B moves along y at 3 m/s as a distractor.
    3 missed frames are below max_misses=4, so A keeps its ID.
    """
    frames = []
    for f in range(n_frames):
        t = f * dt
        dets = []
        if not (7 <= f <= 9):
            dets.append(("A", 5.0 * t, 1.0))
        dets.append(("B", 1.0, 3.0 * t))
        frames.append(_frame(f, round(t, 4), dets))
    return {
        "name": "occlusion",
        "description": "Object A occluded for 3 frames; B is a moving distractor.",
        "tracker": TRACKER_CONFIG,
        "frames": frames,
    }


def duplicates(n_frames: int = 12, dt: float = 0.1) -> dict:
    """Every detection of A is emitted twice (a few cm apart); B once.

    Exactly two tracks must exist and no ID may be created for the copies.
    """
    frames = []
    for f in range(n_frames):
        t = f * dt
        frames.append(
            _frame(
                f,
                round(t, 4),
                # 2 copies of A (within duplicate_eps=0.05... the jitter above
                # is 2cm), one of B.
                [("A", 2.0 + 4.0 * t, 2.0 + 4.0 * t, 2), ("B", 8.0, 2.0 + 3.0 * t, 1)],
            )
        )
    return {
        "name": "duplicates",
        "description": "Each frame contains a duplicated A detection (~2cm offset).",
        "tracker": TRACKER_CONFIG,
        "frames": frames,
    }


def empty_frames(n_frames: int = 16, dt: float = 0.1) -> dict:
    """Three completely empty frames, long enough to delete both tracks.

    After the gap the same two objects reappear and must get *new* ids; the
    number of ID switches while tracks are alive must remain zero.
    """
    gap = set(range(6, 9))  # 3 empty frames -> misses reach 3, below 4;
    # add a 4th to cross the deletion threshold (miss >= 4 at frame 9).
    gap.add(9)
    frames = []
    for f in range(n_frames):
        t = f * dt
        if f in gap:
            frames.append(_frame(f, round(t, 4), []))
        else:
            frames.append(
                _frame(
                    f,
                    round(t, 4),
                    [("A", 4.0 * t, 0.5), ("B", 4.0 * t, 5.0)],
                )
            )
    return {
        "name": "empty_frames",
        "description": "4 consecutive empty frames delete the tracks; objects restart later.",
        "tracker": TRACKER_CONFIG,
        "frames": frames,
    }


SCENARIOS = {
    "crossing": crossing,
    "occlusion": occlusion,
    "duplicates": duplicates,
    "empty_frames": empty_frames,
}


def main() -> None:
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    for name, fn in SCENARIOS.items():
        path = os.path.join(FIXTURE_DIR, f"{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(fn(), fh, indent=1, ensure_ascii=False)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
