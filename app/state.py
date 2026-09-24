"""Session store: sample history, replay-window enforcement, deterministic
recompute, persistence (survives restarts), and evidence logging.

Late data policy: a sample whose timestamp is older than
``t_max_stored - replay_horizon_s`` is rejected as stale (reported, never
silently dropped). Late samples inside the window are merged and the whole
history is deterministically recomputed from the session anchor (initial
SOC/sigma), so replay never mutates state incrementally.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from . import evidence
from .engine import Sample, estimate
from .params import Params

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SessionError(RuntimeError):
    code = "SESSION_ERROR"


class SessionNotFound(SessionError):
    code = "SESSION_NOT_FOUND"


class SessionExists(SessionError):
    code = "SESSION_EXISTS"


class StaleData(SessionError):
    code = "STALE_DATA"

    def __init__(self, stale_ts: list[float], horizon_s: float):
        super().__init__(
            f"{len(stale_ts)} sample(s) older than the replay window "
            f"({horizon_s:.0f}s) were rejected"
        )
        self.stale_ts = stale_ts
        self.horizon_s = horizon_s


def _atomic_write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


class SessionStore:
    def __init__(self, root: str | Path, params: Params, param_sha256: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.params = params
        self.param_sha256 = param_sha256

    # --- paths ---------------------------------------------------------
    def _dir(self, session_id: str) -> Path:
        if not SESSION_ID_RE.match(session_id):
            raise SessionNotFound(f"invalid or unknown session id: {session_id!r}")
        return self.root / session_id

    def _state_path(self, session_id: str) -> Path:
        return self._dir(session_id) / "state.json"

    def _evidence_path(self, session_id: str) -> Path:
        return self._dir(session_id) / "evidence.jsonl"

    # --- lifecycle -----------------------------------------------------
    def create_session(
        self,
        session_id: Optional[str] = None,
        initial_soc: Optional[float] = None,
        initial_sigma: Optional[float] = None,
    ) -> dict[str, Any]:
        session_id = session_id or uuid.uuid4().hex[:16]
        if not SESSION_ID_RE.match(session_id):
            raise SessionError(f"invalid session id: {session_id!r}")
        d = self._dir(session_id)
        if d.exists():
            raise SessionExists(f"session already exists: {session_id}")
        d.mkdir(parents=True)
        state = {
            "session_id": session_id,
            "param_version": self.params.version,
            "param_sha256": self.param_sha256,
            "initial_soc": initial_soc,
            "initial_sigma": initial_sigma,
            "samples": [],
            "result": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        _atomic_write_json(self._state_path(session_id), state)
        evidence.append_record(self._evidence_path(session_id), {
            "type": "session_created",
            "ts": time.time(),
            "session_id": session_id,
            "initial_soc": initial_soc,
            "initial_sigma": initial_sigma,
            "param_version": self.params.version,
            "param_sha256": self.param_sha256,
        })
        return {"session_id": session_id, "param_version": self.params.version}

    def exists(self, session_id: str) -> bool:
        try:
            return self._state_path(session_id).exists()
        except SessionNotFound:
            return False

    def list_sessions(self) -> list[str]:
        return sorted(
            p.name for p in self.root.iterdir()
            if p.is_dir() and SESSION_ID_RE.match(p.name) and (p / "state.json").exists()
        )

    def delete_session(self, session_id: str) -> None:
        d = self._dir(session_id)
        if not d.exists():
            raise SessionNotFound(f"unknown session: {session_id}")
        shutil.rmtree(d)

    # --- state ---------------------------------------------------------
    def load_state(self, session_id: str) -> dict[str, Any]:
        path = self._state_path(session_id)
        if not path.exists():
            raise SessionNotFound(f"unknown session: {session_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _recompute(self, state: dict[str, Any]) -> dict[str, Any]:
        samples = [Sample(*row) for row in state["samples"]]
        return estimate(samples, self.params, state["initial_soc"], state["initial_sigma"])

    def verify_anchor(self, state: dict[str, Any]) -> bool:
        """Recompute from the stored anchor (initial SOC + full sample
        history) and check it reproduces the stored result exactly."""
        if state["result"] is None:
            return True
        return self._recompute(state) == state["result"]

    def ingest(self, session_id: str, samples: list[Sample]) -> dict[str, Any]:
        state = self.load_state(session_id)
        existing: list[list[float]] = state["samples"]
        existing_ts = {row[0] for row in existing}
        t_max = max(existing_ts) if existing_ts else None
        horizon_floor = (t_max - self.params.replay_horizon_s) if t_max is not None else None

        accepted: list[list[float]] = []
        stale: list[float] = []
        duplicates = 0
        for s in samples:
            if s.t_s in existing_ts:
                duplicates += 1
                continue
            if horizon_floor is not None and s.t_s < horizon_floor:
                stale.append(s.t_s)
                continue
            accepted.append([s.t_s, s.current_a, s.voltage_v, s.temp_c])

        if not accepted and stale:
            evidence.append_record(self._evidence_path(session_id), {
                "type": "ingest_rejected",
                "ts": time.time(),
                "session_id": session_id,
                "stale_rejected": stale,
                "replay_horizon_s": self.params.replay_horizon_s,
            })
            raise StaleData(stale, self.params.replay_horizon_s)

        recomputed = False
        anchor_verified = True
        if accepted:
            merged = sorted(existing + accepted, key=lambda r: r[0])
            state["samples"] = merged
            state["result"] = self._recompute(state)
            state["updated_at"] = time.time()
            _atomic_write_json(self._state_path(session_id), state)
            # Anchor check: reload from disk and prove the persisted history
            # reproduces the persisted result bit-for-bit.
            reloaded = self.load_state(session_id)
            anchor_verified = self.verify_anchor(reloaded)
            recomputed = True
            evidence.append_record(self._evidence_path(session_id), {
                "type": "ingest",
                "ts": time.time(),
                "session_id": session_id,
                "accepted": len(accepted),
                "duplicates": duplicates,
                "stale_rejected": stale,
                "n_samples": len(merged),
                "anchor_verified": anchor_verified,
                "final_soc": state["result"]["summary"]["soc"],
                "final_sigma": state["result"]["summary"]["sigma"],
            })

        return {
            "session_id": session_id,
            "accepted": len(accepted),
            "duplicates": duplicates,
            "stale_rejected": stale,
            "recomputed": recomputed,
            "anchor_verified": anchor_verified,
            "summary": state["result"]["summary"] if state["result"] else None,
        }

    def recompute(self, session_id: str) -> dict[str, Any]:
        state = self.load_state(session_id)
        state["result"] = self._recompute(state)
        state["updated_at"] = time.time()
        _atomic_write_json(self._state_path(session_id), state)
        anchor_verified = self.verify_anchor(self.load_state(session_id))
        evidence.append_record(self._evidence_path(session_id), {
            "type": "recompute",
            "ts": time.time(),
            "session_id": session_id,
            "n_samples": len(state["samples"]),
            "anchor_verified": anchor_verified,
            "final_soc": state["result"]["summary"]["soc"],
            "final_sigma": state["result"]["summary"]["sigma"],
        })
        return {
            "session_id": session_id,
            "anchor_verified": anchor_verified,
            "summary": state["result"]["summary"],
        }

    def get_evidence(self, session_id: str) -> dict[str, Any]:
        path = self._evidence_path(session_id)
        if not self._state_path(session_id).exists():
            raise SessionNotFound(f"unknown session: {session_id}")
        return {
            "session_id": session_id,
            "records": evidence.read_records(path),
            "chain_valid": evidence.verify_chain(path),
        }
