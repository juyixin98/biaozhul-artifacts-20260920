"""Time-ordered fusion engine with checkpointed late-message replay.

Messages carry their own *measurement time* and are fused strictly in
measurement-time order, never in arrival order.  A message arriving late
(within :data:`LATE_WINDOW_S` of the high-water mark) is inserted into the
timeline and the filter is replayed from the nearest cached checkpoint; older
messages are rejected without touching the state.

Every accepted measurement produces a checkpoint snapshot and a step record
(state + innovation diagnostics).  Checkpoints older than the replay window
are pruned, but a single *baseline* checkpoint at the oldest surviving point
of the timeline is retained, so the cache stays bounded while replay remains
possible for every accepted message.

A SHA-256 hash chain over the accepted steps gives tamper evidence for the
replay results; each checkpoint also stores the chain hash at its position.
"""

from __future__ import annotations

import hashlib
import math
from bisect import bisect_right
from dataclasses import dataclass, field

import numpy as np

from .ekf import (
    OBS_DIM,
    STATE_DIM,
    EKF,
    enforce_psd,
)

#: messages older than (high-water mark - this) are refused
LATE_WINDOW_S = 2.0

#: chi-squared, 2 dof, p = 0.99 -> NIS gate threshold
DEFAULT_GATE = 9.210340371976184

#: keep checkpoints this much beyond the late window before pruning
PRUNE_SLACK_S = 0.25

#: deterministic encoding of a float for the hash chain
def _f(x: float) -> str:
    return repr(float(x))


def canonical_step(step: dict) -> bytes:
    """Canonical byte encoding of one accepted step for the hash chain."""
    parts = [
        step["message_id"],
        _f(step["t"]),
        step["kind"],
        _f(step["dt"]),
        "x:" + ",".join(_f(v) for v in step["x"]),
        "xp:" + ",".join(_f(v) for v in step["x_pred"]),
        "P:" + ",".join(_f(v) for v in np.asarray(step["P"], dtype=np.float64).ravel()),
        "Pp:" + ",".join(_f(v) for v in np.asarray(step["p_pred"], dtype=np.float64).ravel()),
        "nu:" + ",".join(_f(v) for v in step["innovation"]),
        "S:" + ",".join(_f(v) for v in np.asarray(step["S"], dtype=np.float64).ravel()),
        _f(step["nis"]),
        "K:" + ",".join(_f(v) for v in np.asarray(step["K"], dtype=np.float64).ravel()),
    ]
    return "|".join(parts).encode("utf-8")


GENESIS_HASH = hashlib.sha256(b"ekf-fusion-genesis").hexdigest()


@dataclass
class Record:
    """One ingested measurement in timeline order."""

    mid: str
    t: float
    kind: str
    z: np.ndarray
    r: np.ndarray
    seq: int
    accepted: bool = False


@dataclass
class Checkpoint:
    """Filter snapshot at an accepted record (or the retained baseline)."""

    x: np.ndarray
    p: np.ndarray
    t: float
    initialized: bool
    chain_hash: str


class FusionEngine:
    def __init__(self, q: float = 1.0, gate: float = DEFAULT_GATE,
                 late_window_s: float = LATE_WINDOW_S):
        self.q = float(q)
        self.gate = float(gate)
        self.late_window_s = float(late_window_s)

        # merged timeline of ingested records, sorted by (t, seq)
        self.records: list[Record] = []
        # message id -> timeline record (duplicate detection)
        self._by_id: dict[str, Record] = {}
        # accepted message id -> step diagnostics (also the steps trace)
        self.steps: dict[str, dict] = {}
        # accepted message id -> checkpoint
        self.checkpoints: dict[str, Checkpoint] = {}
        # outlier rejections: message id -> evidence record
        self.rejections: dict[str, dict] = {}
        # id of the retained baseline checkpoint, or None
        self.baseline_id: str | None = None

        self.ekf = EKF(q=self.q)
        self.hwm_time: float | None = None
        self.latest_time: float | None = None
        self.chain_hash: str = GENESIS_HASH
        self._seq = 0

    # ------------------------------------------------------------------ keys

    def _key(self, i: int) -> tuple[float, int]:
        r = self.records[i]
        return (r.t, r.seq)

    def _sort_keys(self) -> list[tuple[float, int]]:
        return [(r.t, r.seq) for r in self.records]

    # ------------------------------------------------------------- ingestion

    def submit(
        self,
        mid: str,
        t: float,
        kind: str,
        z: np.ndarray,
        r: np.ndarray,
    ) -> dict:
        """Ingest one structurally valid measurement.

        Returns a result dict::

            {"status": "accepted"|"rejected",
             "reason": None|"...", "step": <step dict or None>,
             "insert_index": int, "replayed": bool}
        """
        if mid in self._by_id:
            return {"status": "rejected", "reason": "duplicate_message_id",
                    "step": None, "insert_index": -1, "replayed": False}

        if not math.isfinite(t):
            return {"status": "rejected", "reason": "time_non_finite",
                    "step": None, "insert_index": -1, "replayed": False}

        # Out-of-window late message: refuse before any state change.
        if self.hwm_time is not None and t < self.hwm_time - self.late_window_s - 1e-9:
            return {
                "status": "rejected",
                "reason": "late_too_old",
                "step": None,
                "insert_index": -1,
                "replayed": False,
                "high_water_time": self.hwm_time,
                "late_window_s": self.late_window_s,
            }

        self._seq += 1
        rec = Record(mid=mid, t=float(t), kind=kind,
                     z=np.asarray(z, dtype=np.float64).copy(),
                     r=np.asarray(r, dtype=np.float64).copy(),
                     seq=self._seq)

        # An arrival is "late" when its measurement time is behind an
        # already-seen high-water mark; that path restores a checkpoint.
        was_late = self.hwm_time is not None and rec.t < self.hwm_time

        keys = self._sort_keys()
        pos = bisect_right(keys, (rec.t, rec.seq))
        self.records.insert(pos, rec)
        self._by_id[mid] = rec
        if self.hwm_time is None or rec.t > self.hwm_time:
            self.hwm_time = rec.t

        used_checkpoint = self._replay_from(pos)
        self._prune_checkpoints()

        if rec.accepted:
            return {"status": "accepted", "reason": None,
                    "step": self.steps[mid], "insert_index": pos,
                    "replayed": was_late and used_checkpoint}
        evidence = self.rejections.get(mid)
        reason = evidence["reason"] if evidence else "rejected"
        return {"status": "rejected", "reason": reason, "step": None,
                "insert_index": pos,
                "replayed": was_late and used_checkpoint,
                "evidence": evidence}

    # ---------------------------------------------------------------- replay

    def _find_anchor(self, pos: int) -> tuple[int | None, Checkpoint | None]:
        """Most recent accepted record strictly before ``pos`` with a
        checkpoint, plus its timeline index.  Returns (None, None) when no
        usable checkpoint exists (cold-start full replay)."""
        for i in range(pos - 1, -1, -1):
            r = self.records[i]
            if r.accepted and r.mid in self.checkpoints:
                return i, self.checkpoints[r.mid]
        return None, None

    def _replay_from(self, pos: int) -> bool:
        """Re-run the filter from a checkpoint over records [anchor+1 .. end].

        Returns ``True`` when an existing checkpoint was used (a real replay),
        ``False`` for a cold start or an append with an empty suffix.
        """
        anchor_i, ckpt = self._find_anchor(pos)

        # Discard derived state for the suffix; it is rebuilt deterministically.
        suffix_start = (anchor_i + 1) if anchor_i is not None else 0
        for r in self.records[suffix_start:]:
            r.accepted = False
            self.steps.pop(r.mid, None)
            self.rejections.pop(r.mid, None)
            self.checkpoints.pop(r.mid, None)

        if anchor_i is not None:
            # restore the snapshot captured just after the anchor record
            self.ekf.x = ckpt.x.copy()
            self.ekf.p = ckpt.p.copy()
            self.ekf.initialized = ckpt.initialized
            self.chain_hash = ckpt.chain_hash
            cur_t = ckpt.t
            used_checkpoint = True
        else:
            self.ekf = EKF(q=self.q)
            self.chain_hash = GENESIS_HASH
            cur_t = None
            used_checkpoint = False

        latest_t = self.records[anchor_i].t if anchor_i is not None else None
        for rec in self.records[suffix_start:]:
            self._process_record(rec, cur_t)
            cur_t = rec.t
            latest_t = rec.t

        self.latest_time = latest_t
        return used_checkpoint and len(self.records) - suffix_start >= 1

    def _process_record(self, rec: Record, prev_t: float | None) -> None:
        """Predict to ``rec.t`` then initialize, gate, or update."""
        if not self.ekf.initialized:
            # Bootstrap directly from the first measurement.  No prediction
            # exists yet; the record's own state is the initial state.
            self.ekf.initialize(rec.kind, rec.z, rec.r)
            self._commit_accepted(rec, dt=0.0, x_pred=self.ekf.x.copy(),
                                  p_pred=self.ekf.p.copy(), innovation=np.zeros(OBS_DIM),
                                  s=rec.r.copy(), nis=0.0,
                                  k=np.zeros((STATE_DIM, OBS_DIM)), bootstrapped=True)
            self.latest_time = rec.t
            return

        dt = rec.t - prev_t
        if dt < 0.0:  # defensive: timeline order guarantees dt >= 0
            dt = 0.0
        x_pred, p_pred, clips_p, min_eig_p = self.ekf.predict(dt)

        res = self.ekf.update(rec.kind, rec.z, rec.r, self.gate, x_pred, p_pred)
        if res.accepted:
            rec.accepted = True
            self._commit_accepted(
                rec, dt=dt, x_pred=x_pred, p_pred=p_pred,
                innovation=res.innovation, s=res.s, nis=res.nis,
                k=res.kalman_gain, psd_clips_pred=clips_p,
                psd_clips_upd=res.psd_clips_upd, min_eig_pred=min_eig_p,
                min_eig_upd=res.min_eig_upd)
        else:
            rec.accepted = False
            self.rejections[rec.mid] = {
                "message_id": rec.mid,
                "t": rec.t,
                "kind": rec.kind,
                "z": rec.z.tolist(),
                "innovation": res.innovation.tolist(),
                "nis": res.nis,
                "gate": self.gate,
                "reason": "outlier_gate",
                "predicted_position": x_pred[0:2].tolist(),
                "predicted_velocity": x_pred[2:4].tolist(),
            }

    def _commit_accepted(self, rec: Record, *, dt, x_pred, p_pred,
                         innovation, s, nis, k, bootstrapped: bool = False,
                         psd_clips_pred: int = 0, psd_clips_upd: int = 0,
                         min_eig_pred: float = 0.0, min_eig_upd: float = 0.0) -> None:
        rec.accepted = True
        step = {
            "message_id": rec.mid,
            "t": rec.t,
            "kind": rec.kind,
            "seq": rec.seq,
            "dt": float(dt),
            "x": self.ekf.x.tolist(),
            "P": self.ekf.p.tolist(),
            "x_pred": x_pred.tolist(),
            "p_pred": p_pred.tolist(),
            "innovation": innovation.tolist(),
            "S": s.tolist(),
            "nis": float(nis),
            "K": k.tolist(),
            "bootstrapped": bootstrapped,
            "psd_clips_pred": int(psd_clips_pred),
            "psd_clips_upd": int(psd_clips_upd),
            "min_eig_pred": float(min_eig_pred),
            "min_eig_upd": float(min_eig_upd),
        }
        self.chain_hash = hashlib.sha256(
            self.chain_hash.encode("ascii") + b"||" + canonical_step(step)
        ).hexdigest()
        step["chain_hash"] = self.chain_hash
        self.steps[rec.mid] = step
        self.checkpoints[rec.mid] = Checkpoint(
            x=self.ekf.x.copy(), p=self.ekf.p.copy(), t=rec.t,
            initialized=self.ekf.initialized, chain_hash=self.chain_hash)
        self.latest_time = rec.t

    # ------------------------------------------------------------ maintenance

    def _prune_checkpoints(self) -> None:
        """Drop checkpoints outside the replay window, keeping one baseline.

        The baseline is the checkpoint at the oldest surviving accepted
        record; it lets a replay cover a late insertion whose only usable
        anchor is old.  Steps and records themselves are retained (they are
        the output trace); only the snapshot cache is bounded here.
        """
        if self.hwm_time is None:
            return
        cutoff = self.hwm_time - self.late_window_s - PRUNE_SLACK_S
        keep_ids: set[str] = set()
        oldest_accepted: str | None = None
        for r in self.records:
            if r.accepted:
                oldest_accepted = r.mid
                break
        for r in self.records:
            if r.accepted and r.t >= cutoff:
                keep_ids.add(r.mid)
        if oldest_accepted is not None:
            keep_ids.add(oldest_accepted)
            self.baseline_id = oldest_accepted
        for mid in list(self.checkpoints):
            if mid not in keep_ids:
                del self.checkpoints[mid]

    # ---------------------------------------------------------------- output

    def state(self) -> dict:
        if not self.ekf.initialized:
            return {"initialized": False, "t": None,
                    "x": None, "P": None, "n_steps": 0,
                    "n_ingested": len(self.records),
                    "n_rejected_outliers": len(self.rejections),
                    "n_checkpoints": len(self.checkpoints),
                    "chain_hash": self.chain_hash,
                    "high_water_time": self.hwm_time}
        psd_ok, _, min_eig = enforce_psd(self.ekf.p)
        symmetric = bool(np.allclose(psd_ok, psd_ok.T, atol=1e-12))
        return {
            "initialized": True,
            "t": self.latest_time,
            "x": self.ekf.x.tolist(),
            "P": self.ekf.p.tolist(),
            "position": self.ekf.x[0:2].tolist(),
            "velocity": self.ekf.x[2:4].tolist(),
            "n_steps": len(self.steps),
            "n_ingested": len(self.records),
            "n_rejected_outliers": len(self.rejections),
            "n_checkpoints": len(self.checkpoints),
            "psd": symmetric and bool(np.allclose(psd_ok, self.ekf.p)),
            "min_eigenvalue": min_eig,
            "total_psd_clips": self.ekf.total_psd_clips,
            "chain_hash": self.chain_hash,
            "high_water_time": self.hwm_time,
            "gate": self.gate,
        }

    def steps_in_order(self) -> list[dict]:
        return [self.steps[r.mid] for r in self.records if r.accepted]

    def verify_chain(self) -> dict:
        """Recompute the hash chain over the ordered steps."""
        h = GENESIS_HASH
        mismatches: list[str] = []
        for step in self.steps_in_order():
            h = hashlib.sha256(
                h.encode("ascii") + b"||" + canonical_step(step)
            ).hexdigest()
            if not h == step["chain_hash"]:
                mismatches.append(step["message_id"])
                h = step["chain_hash"]
        return {
            "ok": not mismatches and h == self.chain_hash,
            "recomputed_head": h,
            "stored_head": self.chain_hash,
            "mismatched_steps": mismatches,
            "n_steps": len(self.steps),
        }
