"""Session store: sample accumulation, late-data replay window, persistence.

Sessions are persisted as JSON files so state survives a service restart.
Late samples are accepted only if their timestamp falls inside
[latest_t - replay_window_s, +inf); older late data is rejected, never
silently merged. Estimation itself is stateless over the accumulated,
sorted sample set, so replaying accepted late samples is deterministic.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .estimator import EstimationResult, Sample, estimate_soc
from .params import Params

REASON_OK = "OK"
REASON_DUPLICATE_TIMESTAMP = "DUPLICATE_TIMESTAMP"
REASON_TOO_OLD_FOR_REPLAY = "TOO_OLD_FOR_REPLAY"
REASON_SESSION_FINALIZED = "SESSION_FINALIZED"


@dataclass
class IngestReport:
    accepted: int = 0
    rejected: int = 0
    rejections: list[dict] = field(default_factory=list)


class Session:
    def __init__(self, session_id: str, replay_window_s: float, initial_soc: Optional[float] = None):
        self.session_id = session_id
        self.replay_window_s = float(replay_window_s)
        self.initial_soc = initial_soc
        self.samples: dict[float, Sample] = {}  # keyed by timestamp
        self.finalized: bool = False
        self.result: Optional[dict] = None

    @property
    def latest_t(self) -> Optional[float]:
        return max(self.samples) if self.samples else None

    def sorted_samples(self) -> list[Sample]:
        return [self.samples[k] for k in sorted(self.samples)]

    def ingest(self, samples: list[Sample]) -> IngestReport:
        report = IngestReport()
        if self.finalized:
            report.rejected = len(samples)
            report.rejections = [
                {"t_s": s.t_s, "reason": REASON_SESSION_FINALIZED} for s in samples
            ]
            return report
        reference_latest = self.latest_t
        # The replay window only constrains data arriving AFTER the session
        # already holds samples; the first (possibly large, ordered) batch is
        # accepted in full.
        cutoff = (reference_latest - self.replay_window_s) if reference_latest is not None else None
        for s in samples:
            if s.t_s in self.samples:
                report.rejected += 1
                report.rejections.append({"t_s": s.t_s, "reason": REASON_DUPLICATE_TIMESTAMP})
                continue
            if cutoff is not None and s.t_s < cutoff:
                # Late data outside the replay window: rejected, never merged.
                report.rejected += 1
                report.rejections.append({"t_s": s.t_s, "reason": REASON_TOO_OLD_FOR_REPLAY})
                continue
            self.samples[s.t_s] = s
            report.accepted += 1
        return report

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "replay_window_s": self.replay_window_s,
            "initial_soc": self.initial_soc,
            "finalized": self.finalized,
            "samples": [
                {"t_s": s.t_s, "current_a": s.current_a, "voltage_v": s.voltage_v, "temp_c": s.temp_c}
                for s in self.sorted_samples()
            ],
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        sess = cls(d["session_id"], d["replay_window_s"], d.get("initial_soc"))
        sess.finalized = bool(d.get("finalized", False))
        sess.result = d.get("result")
        for s in d.get("samples", []):
            sess.samples[float(s["t_s"])] = Sample(
                t_s=float(s["t_s"]),
                current_a=float(s["current_a"]),
                voltage_v=float(s["voltage_v"]),
                temp_c=float(s["temp_c"]),
            )
        return sess


class SessionStore:
    """Thread-safe, file-backed session store (survives restarts)."""

    def __init__(self, params: Params, data_dir: Path):
        self.params = params
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._load_all()

    def _path(self, session_id: str) -> Path:
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
        return self.data_dir / f"{safe}.json"

    def _load_all(self) -> None:
        for p in sorted(self.data_dir.glob("*.json")):
            try:
                sess = Session.from_dict(json.loads(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue  # corrupt files are skipped, never silently trusted
            self._sessions[sess.session_id] = sess

    def _persist(self, sess: Session) -> None:
        tmp = self._path(sess.session_id).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sess.to_dict()), encoding="utf-8")
        os.replace(tmp, self._path(sess.session_id))  # atomic rename

    def create(self, replay_window_s: Optional[float] = None, initial_soc: Optional[float] = None) -> Session:
        with self._lock:
            session_id = uuid.uuid4().hex[:12]
            sess = Session(
                session_id,
                replay_window_s if replay_window_s is not None else self.params.default_replay_window_s,
                initial_soc,
            )
            self._sessions[session_id] = sess
            self._persist(sess)
            return sess

    def get(self, session_id: str) -> Optional[Session]:
        with self._lock:
            return self._sessions.get(session_id)

    def ingest(self, session_id: str, samples: list[Sample]) -> Optional[IngestReport]:
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return None
            report = sess.ingest(samples)
            self._persist(sess)
            return report

    def estimate(self, session_id: str) -> Optional[EstimationResult]:
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return None
            return estimate_soc(sess.sorted_samples(), self.params, sess.initial_soc)

    def finalize(self, session_id: str) -> Optional[EstimationResult]:
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return None
            result = estimate_soc(sess.sorted_samples(), self.params, sess.initial_soc)
            sess.finalized = True
            sess.result = result.to_dict()
            self._persist(sess)
            return result
