"""Stream examples/sample_frames.json to a running server, frame by frame.

    MOT_BASE_URL=http://127.0.0.1:8000 python examples/load_sample.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client_demo import _request  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> None:
    with open(os.path.join(HERE, "sample_frames.json"), encoding="utf-8") as fh:
        data = json.load(fh)

    status, created = _request("POST", "/sessions", None, {})
    assert status == 201, created
    sid, secret = created["session_id"], created["secret"]
    path = f"/sessions/{sid}/frames"
    print(f"session {sid}")

    for frame in data["frames"]:
        status, out = _request("POST", path, secret, frame)
        assert status == 200, out
        assoc = ", ".join(
            f"T{a['track_id']}<-d{a['detection_index']}"
            f"(pred=({a['prediction'][0]:.2f},{a['prediction'][1]:.2f}),"
            f" dM2={a['mahalanobis_sq']:.2f},"
            f" |e|={a['euclidean']:.3f})"
            for a in out["associations"]
        )
        print(
            f"frame {out['frame_id']:>2}: new={out['new_tracks']} "
            f"deleted={[t['track_id'] for t in out['deleted_tracks']]} "
            f"dups={len(out['duplicate_detections'])} unmatched_tracks="
            f"{[t['track_id'] for t in out['unmatched_tracks']]} | {assoc}"
        )


if __name__ == "__main__":
    main()
