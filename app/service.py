"""In-memory session store: owns tracker instances, replay protection and
serialization of tracker results onto the wire protocol.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, is_dataclass
from typing import Any

from . import security
from .mot.tracker import (
    Detection,
    FrameOrderError,
    FrameResult,
    MultiTargetTracker,
    TrackerConfig,
)
from .schemas import FrameIn, TrackerParams


class ReplayError(Exception):
    """A frame_id that was already processed is submitted again."""


class BodyMismatchError(Exception):
    """A repeated frame_id is submitted with a different body."""


def _to_plain(obj: Any) -> Any:
    if is_dataclass(obj):
        return {k: _to_plain(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    return obj


class Session:
    def __init__(self, params: TrackerParams | None) -> None:
        p = params or TrackerParams()
        self.params = p
        self.tracker = MultiTargetTracker(
            TrackerConfig(
                q=p.q,
                r=p.r,
                gate_pvalue=p.gate_pvalue,
                gate_threshold=p.gate_threshold,
                hits_to_confirm=p.hits_to_confirm,
                max_misses=p.max_misses,
                tentative_max_misses=p.tentative_max_misses,
                duplicate_eps=p.duplicate_eps,
                init_vel_var=p.init_vel_var,
            )
        )
        self.created_at = time.time()
        self.lock = threading.Lock()
        # frame_id -> (body sha256, serialized response)
        self.processed: dict[int, tuple[str, dict]] = {}

    @staticmethod
    def _serialize(res: FrameResult) -> dict:
        out = _to_plain(res)
        for a in out["associations"]:
            a["in_gate"] = a["mahalanobis_sq"] <= a["gate_threshold"]
        return out

    def handle_frame(self, frame: FrameIn, body_sha: str) -> dict:
        """Process one frame.

        * The tracker rejects non-increasing frame ids / timestamps itself.
        * A frame_id already seen in this session is an explicit replay:
          identical body => stored response is returned (idempotent, no new
          IDs); different body => :class:`BodyMismatchError`.
        """
        with self.lock:
            prev = self.tracker.last_frame_id
            if prev is not None and frame.frame_id == prev:
                stored_sha, stored = self.processed[frame.frame_id]
                if stored_sha != body_sha:
                    raise BodyMismatchError(
                        f"frame_id {frame.frame_id} was already processed with a "
                        "different body"
                    )
                return stored
            if prev is not None and frame.frame_id < prev:
                # Surface the tracker's own ordered-message contract.
                raise FrameOrderError(
                    f"frame_id {frame.frame_id} < last processed {prev}; "
                    "out-of-order frames are rejected"
                )

            dets = [Detection(d.x, d.y, d.label) for d in frame.detections]
            result = self.tracker.step(frame.frame_id, frame.timestamp, dets)
            payload = self._serialize(result)
            self.processed[frame.frame_id] = (body_sha, payload)
            return payload

    def state(self) -> dict:
        with self.lock:
            tr = self.tracker
            return {
                "created_at": self.created_at,
                "tracker": self.params.model_dump(),
                "last_frame_id": tr.last_frame_id,
                "last_timestamp": tr.last_timestamp,
                "track_count": len(tr._tracks),
                "confirmed_track_ids": [
                    tid
                    for tid, t in sorted(tr._tracks.items())
                    if t.status.value == "confirmed"
                ],
            }


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._secrets: dict[str, str] = {}
        self._lock = threading.Lock()

    def create(self, params: TrackerParams | None) -> tuple[str, str]:
        sid = security.new_session_id()
        secret = security.new_secret()
        with self._lock:
            self._sessions[sid] = Session(params)
            self._secrets[sid] = secret
        return sid, secret

    def get(self, sid: str) -> Session | None:
        return self._sessions.get(sid)

    def secret_of(self, sid: str) -> str | None:
        return self._secrets.get(sid)

    def reset(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._secrets.clear()


store = SessionStore()
