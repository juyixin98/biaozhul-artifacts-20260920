"""Reference client for the signed tracking API (standard library only).

It:
1. creates a session and records the one-shot HMAC secret,
2. signs every request with HMAC-SHA256,
3. streams the two crossing objects, including a duplicated frame replay
   (the replay must return the identical response and must not create IDs),
4. prints the per-frame association report.

Run with the server up::

    uvicorn app.main:app --port 8000
    python examples/client_demo.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

BASE = os.environ.get("MOT_BASE_URL", "http://127.0.0.1:8000")


def _request(method: str, path: str, secret: str | None, payload: dict | None):
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        ts = f"{time.time():.3f}"
        body_hash = hashlib.sha256(body).hexdigest()
        msg = f"{method}\n{path}\n{ts}\n{body_hash}".encode("utf-8")
        sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
        headers.update(
            {
                "X-Session-Id": path.split("/")[2],
                "X-Timestamp": ts,
                "X-Signature": sig,
            }
        )
    req = urllib.request.Request(BASE + path, data=body if body else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def crossing_frame(f: int, dt: float = 0.1) -> dict:
    t = round(f * dt, 4)
    return {
        "frame_id": f,
        "timestamp": t,
        "detections": [
            {"x": round(8.0 * t, 4), "y": round(4.0 * t, 4), "label": "A"},
            {"x": round(20.0 - 8.0 * t, 4), "y": round(4.0 * t, 4), "label": "B"},
        ],
    }


def main() -> None:
    status, created = _request("POST", "/sessions", None, {})
    assert status == 201, created
    sid, secret = created["session_id"], created["secret"]
    print(f"session {sid} created")

    path = f"/sessions/{sid}/frames"
    last = None
    for f in range(10):
        frame = crossing_frame(f)
        status, out = _request("POST", path, secret, frame)
        assert status == 200, out
        last = out
        assoc = ", ".join(
            f"track {a['track_id']}<-det {a['detection_index']} "
            f"(dM2={a['mahalanobis_sq']:.3f}, |e|={a['euclidean']:.3f}m)"
            for a in out["associations"]
        )
        print(f"frame {f}: new={out['new_tracks']} confirmed={out['confirmed_tracks']} | {assoc}")

    # Replay the exact last frame: must be idempotent, no new IDs.
    status, replay = _request("POST", path, secret, crossing_frame(9))
    assert status == 200, replay
    assert replay["new_tracks"] == last["new_tracks"]
    assert replay["tracks"] == last["tracks"]
    print("replay frame 9: idempotent, identical track set")

    # Out-of-order frame: must be explicitly rejected with 409.
    bad = crossing_frame(3)
    status, out = _request("POST", path, secret, bad)
    assert status == 409, out
    print(f"out-of-order frame 3 rejected: {out['detail']['code']}")

    # Tampered signature: must be rejected with 403.
    ts = f"{time.time():.3f}"
    body = json.dumps(crossing_frame(10)).encode()
    req = urllib.request.Request(
        BASE + path,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Session-Id": sid,
            "X-Timestamp": ts,
            "X-Signature": "deadbeef",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
        raise SystemExit("tampered signature was accepted!")
    except urllib.error.HTTPError as exc:
        print(f"tampered signature rejected: HTTP {exc.code}")


if __name__ == "__main__":
    main()
