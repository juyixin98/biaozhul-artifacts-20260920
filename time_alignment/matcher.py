"""Event-time alignment engine (storage-agnostic).

Algorithm
---------
Messages are processed strictly in *receive* order.  Within an epoch the
largest event time seen is tracked.  A camera frame becomes final once the
watermark

    W = latest_seen_event_time - reorder_tolerance

reaches its timestamp: by then any message later than the frame by less than
``reorder_tolerance`` would already have arrived, so a still-missing IMU is a
real gap (packet loss) rather than reordering.

* A frame is paired with the IMU sample minimising ``|t_cam - t_imu|`` subject
  to ``<= tolerance_ns``.  On an equal-distance tie the **earlier** IMU event
  time wins (deterministic ``(abs_dt, t_imu, recv_seq)`` ordering).
* ``imu_exclusive=True``  -> an IMU sample is consumed by at most one frame.
* ``imu_exclusive=False`` -> an IMU sample may be reused, up to
  ``imu_max_uses`` times (``-1`` = unlimited).
* Pending buffers are bounded: overflow evicts the *oldest* buffered event,
  which is still emitted as an explicit overflow outcome.
* A time back-jump (``t + reorder_tolerance < max_seen``) or an explicit reset
  closes the current epoch; pairing never crosses epoch boundaries.
* Parameter requests are swapped in atomically at receive boundaries; every
  emitted record is stamped with the parameter version in force.
"""
from __future__ import annotations

import bisect
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .model import (
    AlignParams,
    EpochReason,
    ImuStatus,
    PairStatus,
    RecordKind,
    StreamEvent,
    UNLIMITED_USES,
)


@dataclass(frozen=True)
class Outcome:
    """One finalised piece of evidence produced by the matcher."""

    kind: RecordKind
    epoch: int
    params_version: int
    payload: dict


@dataclass
class _PendingImu:
    ev: StreamEvent
    key: tuple[int, int]
    use_count: int = 0
    removed: bool = False  # exclusive samples stay indexed but unavailable

    def available(self, p: AlignParams) -> bool:
        if self.removed:
            return False
        if not p.imu_exclusive and p.imu_max_uses == UNLIMITED_USES:
            return True
        return self.use_count < p.imu_max_uses


@dataclass
class _EpochState:
    epoch: int
    hint: Optional[str]
    max_seen: Optional[int] = None
    clock_generation: int = 0
    cam_keys: list[tuple[int, int]] = field(default_factory=list)
    imu_keys: list[tuple[int, int]] = field(default_factory=list)
    cams: dict[tuple[int, int], StreamEvent] = field(default_factory=dict)
    imus: dict[tuple[int, int], _PendingImu] = field(default_factory=dict)


@dataclass
class MatchResult:
    """Convenience view over the outcomes of one register/finalize call."""

    outcomes: list[Outcome]

    @property
    def pairs(self) -> list[dict]:
        return [o.payload for o in self.outcomes if o.kind is RecordKind.PAIR]

    @property
    def imus(self) -> list[dict]:
        return [o.payload for o in self.outcomes if o.kind is RecordKind.IMU]

    @property
    def epochs(self) -> list[dict]:
        return [o.payload for o in self.outcomes if o.kind is RecordKind.EPOCH]

    @property
    def params_changes(self) -> list[dict]:
        return [o.payload for o in self.outcomes if o.kind is RecordKind.PARAMS]


class AlignmentMatcher:
    def __init__(self, params: Optional[AlignParams] = None) -> None:
        self.params: AlignParams = params or AlignParams()
        self.params.validate()
        self._pending_params: deque[AlignParams] = deque()
        self._ep: Optional[_EpochState] = None
        self.epoch = 0
        self._recv_seq = 0
        self.recent: deque[Outcome] = deque(maxlen=self.params.max_outcomes)
        self.total_pairs = 0
        self.total_unpaired = 0
        self.total_imu_finalised = 0
        self.total_epochs = 0

    # ---------------------------------------------------------------- public

    def request_params(self, params: AlignParams) -> None:
        """Queue a parameter change; applied atomically at the next boundary.

        Safe to call from a ROS parameter callback at any instant.  Values are
        validated now (bad configs rejected immediately); the swap itself is
        deferred so no record is emitted under a half-applied configuration.
        """
        params.validate()
        if params.version <= self.params.version:
            raise ValueError(
                f"params version must increase: got {params.version}, "
                f"current {self.params.version}")
        self._pending_params.append(params)

    def register(self, ev: StreamEvent) -> MatchResult:
        """Process one received message in receive order; emit final records."""
        out: list[Outcome] = []
        # Boundary: swap queued parameter versions atomically.
        self._drain_params(out)

        if ev.kind == "reset":
            self._close_epoch(out, EpochReason.RESET_EVENT,
                              detail=ev.payload or {"source_id": ev.source_id})
            self._open_epoch(EpochReason.RESET_EVENT, ev, out)
            return self._finish(out)

        assert ev.t_ns is not None
        if ev.seq is None:
            object.__setattr__(ev, "seq", self._next_seq())
        else:
            self._recv_seq = max(self._recv_seq, ev.seq)
        if ev.recv_ns is None:
            object.__setattr__(ev, "recv_ns", ev.t_ns)

        if self._ep is None:
            self._open_epoch(EpochReason.FIRST_EVENT, ev, out)

        # External /clock epoch hint change (ROS time restart) resets epoch.
        if ev.epoch_hint is not None and ev.epoch_hint != self._ep.hint:
            self._close_epoch(out, EpochReason.RESET_EVENT,
                              detail={"clock_hint": ev.epoch_hint})
            self._open_epoch(EpochReason.RESET_EVENT, ev, out)

        # A new clock generation (/clock restarted, even inside the 100ms
        # back-jump window) always begins a fresh epoch.
        if ev.clock_generation != self._ep.clock_generation:
            self._close_epoch(out, EpochReason.RESET_EVENT,
                              detail={"clock_generation": ev.clock_generation,
                                      "prev_clock_generation":
                                      self._ep.clock_generation})
            self._open_epoch(EpochReason.RESET_EVENT, ev, out)

        rt = self.params.reorder_tolerance_ns
        if (self._ep.max_seen is not None
                and ev.t_ns + rt < self._ep.max_seen):
            # Back-jump beyond the reorder window: old time cannot be matched
            # against data already retired, so start a fresh epoch.
            self._close_epoch(
                out, EpochReason.CLOCK_BACKJUMP,
                detail={"t_ns": ev.t_ns,
                        "prev_max_seen_ns": self._ep.max_seen,
                        "gap_ns": self._ep.max_seen - ev.t_ns})
            self._open_epoch(EpochReason.CLOCK_BACKJUMP, ev, out)

        self._ingest(ev, out)

        # Watermark derives from the maximum event time *ever seen in this
        # epoch*, not this message's stamp: a late out-of-order message must
        # never pull the watermark backwards and re-open settled times.
        wm = self._ep.max_seen - rt
        self._expire(wm, out)
        return self._finish(out)

    def finalize(self) -> MatchResult:
        """Flush everything still buffered at clean stream end."""
        out: list[Outcome] = []
        self._drain_params(out)
        if self._ep is not None:
            self._flush_ep(out, terminal=True)
            self._ep = None
        return self._finish(out)

    # ------------------------------------------------------------- params

    def _drain_params(self, out: list[Outcome]) -> None:
        while self._pending_params:
            p = self._pending_params.popleft()
            self.params = p
            self.recent = deque(self.recent, maxlen=p.max_outcomes)
            out.append(Outcome(RecordKind.PARAMS, self.epoch, p.version,
                               {"version": p.version, "params": p.to_dict()}))

    # ------------------------------------------------------------- epochs

    def _next_seq(self) -> int:
        self._recv_seq += 1
        return self._recv_seq

    def _open_epoch(self, reason: EpochReason, ev: Optional[StreamEvent],
                    out: list[Outcome]) -> None:
        self.epoch += 1
        self.total_epochs += 1
        hint = ev.epoch_hint if ev is not None else None
        self._ep = _EpochState(epoch=self.epoch, hint=hint,
                               clock_generation=(ev.clock_generation
                                                 if ev is not None else 0))
        out.append(Outcome(RecordKind.EPOCH, self.epoch,
                           self.params.version, {
                               "epoch": self.epoch,
                               "prev_epoch": self.epoch - 1,
                               "event": "open",
                               "reason": reason.value,
                               "start_t_ns": ev.t_ns if ev is not None else None,
                               "recv_ns": (ev.recv_ns if ev is not None
                                           and ev.recv_ns is not None else None),
                               "source_id": ev.source_id if ev is not None else "",
                           }))

    def _close_epoch(self, out: list[Outcome], reason: EpochReason,
                     detail: Optional[dict] = None) -> None:
        if self._ep is None:
            return
        payload: dict = {"epoch": self.epoch, "event": "close",
                         "closed_reason": reason.value}
        if detail:
            payload["reset"] = detail
        if self._ep.max_seen is not None:
            payload["max_seen_ns"] = self._ep.max_seen
        out.append(Outcome(RecordKind.EPOCH, self.epoch,
                           self.params.version, payload))
        self._flush_ep(out, terminal=False)
        self._ep = None

    # ------------------------------------------------------------- ingest

    def _ingest(self, ev: StreamEvent, out: list[Outcome]) -> None:
        ep = self._ep
        assert ep is not None and ev.seq is not None
        key = (ev.t_ns, ev.seq)
        if ev.kind == "camera":
            bisect.insort(ep.cam_keys, key)
            ep.cams[key] = ev
            if len(ep.cams) > self.params.max_camera_pending:
                old_key = ep.cam_keys.pop(0)
                old = ep.cams.pop(old_key)
                self._overflow_camera(old, out)
        else:
            pi = _PendingImu(ev=ev, key=key)
            bisect.insort(ep.imu_keys, key)
            ep.imus[key] = pi
            if len(ep.imus) > self.params.max_imu_pending:
                old_key = ep.imu_keys.pop(0)
                old = ep.imus.pop(old_key)
                self._overflow_imu(old, out)
        ep.max_seen = (ev.t_ns if ep.max_seen is None
                       else max(ep.max_seen, ev.t_ns))

    # ------------------------------------------------------------- expiry

    def _expire(self, wm: int, out: list[Outcome]) -> None:
        ep = self._ep
        assert ep is not None
        while ep.cam_keys and ep.cam_keys[0][0] <= wm:
            key = ep.cam_keys.pop(0)
            cam = ep.cams.pop(key)
            self._resolve_camera(cam, wm, out,
                                 PairStatus.EXPIRED_UNPAIRED)
        # Pending cameras all have t > wm.  An IMU with t < wm - tol is
        # necessarily farther than tol from every pending camera, so it can
        # never be matched again: retire it (with an explicit status).
        limit = wm - self.params.tolerance_ns
        while ep.imu_keys and ep.imu_keys[0][0] < limit:
            key = ep.imu_keys.pop(0)
            pi = ep.imus.pop(key)
            self._retire_imu(pi, wm, out,
                             ImuStatus.UNUSED_EXPIRED, ImuStatus.USED_EXPIRED,
                             "watermark_expired")

    def _flush_ep(self, out: list[Outcome], terminal: bool) -> None:
        ep = self._ep
        assert ep is not None
        wm = ep.max_seen if ep.max_seen is not None else 0
        cam_unpaired = (PairStatus.STREAM_END_UNPAIRED if terminal
                        else PairStatus.EPOCH_CLOSED_UNPAIRED)
        while ep.cam_keys:
            key = ep.cam_keys.pop(0)
            cam = ep.cams.pop(key)
            self._resolve_camera(cam, wm, out, cam_unpaired)
        imu_status = ImuStatus.STREAM_END if terminal else ImuStatus.EPOCH_CLOSED
        while ep.imu_keys:
            key = ep.imu_keys.pop(0)
            pi = ep.imus.pop(key)
            # Epoch closure / stream end is the explicit terminal cause for
            # every survivor, whether or not the sample had served a frame.
            self._retire_imu(pi, wm, out, imu_status, imu_status,
                             "stream_end" if terminal else "epoch_closed")

    # ------------------------------------------------------------- resolve

    def _best_imu(self, cam: StreamEvent
                  ) -> tuple[Optional[_PendingImu], int, bool]:
        """Nearest *available* IMU within tolerance; earlier time wins ties.

        Returns (candidate, abs_dt, tie_flag) where tie_flag means another
        equidistant but later IMU existed and was deliberately not chosen.
        """
        ep = self._ep
        assert ep is not None and cam.t_ns is not None
        tol = self.params.tolerance_ns
        best: Optional[_PendingImu] = None
        best_score: Optional[tuple[int, int, int]] = None
        for key in ep.imu_keys:
            pi = ep.imus[key]
            if not pi.available(self.params):
                continue
            adt = abs(key[0] - cam.t_ns)
            if adt > tol:
                continue
            score = (adt, key[0], key[1])
            if best_score is None or score < best_score:
                best, best_score = pi, score
        tie = False
        if best is not None:
            bt = best.ev.t_ns
            for key in ep.imu_keys:
                pi = ep.imus[key]
                if pi is best or not pi.available(self.params):
                    continue
                if abs(key[0] - cam.t_ns) == abs(bt - cam.t_ns) and key[0] > bt:
                    tie = True
                    break
        return best, (best_score[0] if best_score else -1), tie

    def _resolve_camera(self, cam: StreamEvent, wm: int,
                        out: list[Outcome], unpaired: PairStatus) -> None:
        pi, _adt, tie = self._best_imu(cam)
        if pi is None:
            reason = {"stream_end_unpaired": "stream_end",
                      "epoch_closed_unpaired": "epoch_closed",
                      "expired_unpaired": "no_candidate_within_tolerance"}[
                          unpaired.value]
            out.append(self._pair_record(cam, None, None, None,
                                         unpaired.value, reason, 0, wm))
            self.total_unpaired += 1
            return
        pi.use_count += 1
        if self.params.imu_exclusive:
            pi.removed = True  # remains indexed until its own retirement
        # Signed dt: positive means the IMU sample is earlier than the frame.
        out.append(self._pair_record(
            cam, pi.ev, cam.t_ns - pi.ev.t_ns,
            "earlier" if tie else None,
            PairStatus.MATCHED.value, "nearest_within_tolerance",
            pi.use_count, wm))
        self.total_pairs += 1

    def _pair_record(self, cam: StreamEvent, imu: Optional[StreamEvent],
                     dt_ns: Optional[int], tie_break: Optional[str],
                     status: str, reason: str, use_count: int,
                     wm: Optional[int]) -> Outcome:
        return Outcome(RecordKind.PAIR, self.epoch, self.params.version, {
            "camera": self._src(cam),
            "imu": self._src(imu) if imu is not None else None,
            "dt_ns": dt_ns,
            "tie_break": tie_break,
            "status": status,
            "reason": reason,
            "watermark_ns": wm,
            "imu_use_count": use_count,
            "epoch": self.epoch,
            "params_version": self.params.version,
        })

    def _retire_imu(self, pi: _PendingImu, wm: int, out: list[Outcome],
                    unused_status: ImuStatus, used_status: ImuStatus,
                    reason: str) -> None:
        status = used_status if pi.use_count > 0 else unused_status
        out.append(Outcome(RecordKind.IMU, self.epoch,
                           self.params.version, {
                               "imu": self._src(pi.ev),
                               "status": status.value,
                               "reason": reason,
                               "watermark_ns": wm,
                               "use_count": pi.use_count,
                               "epoch": self.epoch,
                               "params_version": self.params.version,
                           }))
        self.total_imu_finalised += 1

    def _overflow_camera(self, cam: StreamEvent, out: list[Outcome]) -> None:
        out.append(self._pair_record(
            cam, None, None, None,
            PairStatus.CAMERA_BUFFER_OVERFLOW.value,
            "max_camera_pending_exceeded", 0, None))
        self.total_unpaired += 1

    def _overflow_imu(self, pi: _PendingImu, out: list[Outcome]) -> None:
        out.append(Outcome(RecordKind.IMU, self.epoch,
                           self.params.version, {
                               "imu": self._src(pi.ev),
                               "status": ImuStatus.IMU_BUFFER_OVERFLOW.value,
                               "reason": "max_imu_pending_exceeded",
                               "watermark_ns": None,
                               "use_count": pi.use_count,
                               "epoch": self.epoch,
                               "params_version": self.params.version}))
        self.total_imu_finalised += 1

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _src(ev: StreamEvent) -> dict:
        return {"id": ev.source_id, "t_ns": ev.t_ns,
                "recv_ns": ev.recv_ns, "seq": ev.seq}

    def _finish(self, out: list[Outcome]) -> MatchResult:
        self.recent.extend(out)
        return MatchResult(out)
