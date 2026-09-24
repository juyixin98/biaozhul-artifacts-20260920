"""Camera/IMU time-alignment state machine.

The engine deliberately has no rclpy dependency: it consumes :class:`Event`s
and produces :class:`Decision`s, so the exact same algorithm runs online
(ROS node) and offline (scenario replay / tests).

Guarantees implemented here
---------------------------
* Matching uses *event time* (header stamps). For a settled camera frame the
  nearest IMU inside ``tolerance_ns`` wins; equidistant candidates tie and
  the earlier IMU stamp wins; identical IMU stamps resolve by lowest seq.
* IMU samples are either *reused* by many frames or *exclusive* (consumed on
  first match).
* Bounded caches. A frame is only settled once both stream high-water marks
  have advanced ``tolerance + out_of_order`` beyond it, so 100 ms of
  out-of-order/late data is tolerated. Expired frames are explicitly marked
  ``CAMERA_UNMATCHED``.
* A backwards time jump larger than ``reset_threshold_ns`` opens a new
  epoch. Nothing is ever matched across an epoch boundary.
* Parameter changes are staged and applied atomically between events; each
  applied set gets a version stamped on every decision settled under it.
"""

from __future__ import annotations

from typing import Any, Callable

from .config import AlignConfig, EXCLUSIVE, REUSE
from .storage import Storage
from .types import (
    CAMERA,
    IMU,
    Decision,
    Event,
    Status,
    REASON_CACHE_EVICTED,
    REASON_EPOCH_CLOSED,
    REASON_EXPIRED,
    REASON_IMU_CACHE,
    REASON_IMU_EXPIRED,
    REASON_NEAREST,
    REASON_TIE_EARLIER,
    REASON_TIE_IDENTICAL,
)

# Maximum candidates kept in the evidence payload (nearest ones first).
MAX_EVIDENCE_CANDIDATES = 5


class PairingEngine:
    def __init__(self, storage: Storage, config: AlignConfig,
                 now_ns: Callable[[], int] | None = None,
                 initial_recv_ns: int = 0):
        self.storage = storage
        self.config = config
        self._now_ns = now_ns
        self._staged: AlignConfig | None = None

        self.epoch_id = 0
        self._cameras: list[Event] = []   # kept sorted by (stamp, seq)
        self._imus: list[Event] = []      # kept sorted by (stamp, seq)
        self._camera_hwm: int | None = None
        self._imu_hwm: int | None = None
        self._uid = 0
        self._last_seq = {"camera": -1, "imu": -1}

        self.storage.record_config(
            config.version, initial_recv_ns, config.to_params(),
            note="initial config at engine start")
        self.storage.open_epoch(self.epoch_id, initial_recv_ns, reason="init")

    # ------------------------------------------------------------ helpers
    def _clock(self) -> int:
        return int(self._now_ns()) if self._now_ns else 0

    @staticmethod
    def _insort(events: list[Event], ev: Event) -> None:
        key = (ev.stamp_ns, ev.seq)
        lo, hi = 0, len(events)
        while lo < hi:
            mid = (lo + hi) // 2
            if (events[mid].stamp_ns, events[mid].seq) < key:
                lo = mid + 1
            else:
                hi = mid
        events.insert(lo, ev)

    def _settle_margin(self) -> int:
        """Stamp-space safety margin: tolerance + disorder window."""
        return self.config.tolerance_ns + self.config.out_of_order_ns

    def snapshot(self) -> dict[str, Any]:
        """Debug/observability view of the bounded caches."""
        return {
            "epoch_id": self.epoch_id,
            "config_version": self.config.version,
            "staged_config_version": (None if self._staged is None
                                      else self._staged.version),
            "camera_cache": len(self._cameras),
            "imu_cache": len(self._imus),
            "camera_cache_max": self.config.camera_cache_max,
            "imu_cache_max": self.config.imu_cache_max,
            "camera_hwm_ns": self._camera_hwm,
            "imu_hwm_ns": self._imu_hwm,
        }

    # ----------------------------------------------------- configuration
    def stage_config(self, recv_ns: int | None = None, note: str = "dynamic update",
                     **changes: Any) -> int:
        """Validate and stage new parameters.

        The change does NOT affect any decision until
        :meth:`apply_staged_config` runs at an event boundary. Returns the
        future version number.
        """
        candidate = self.config.validated_replace(version=self.config.version + 1, **changes)
        self._staged = (candidate, recv_ns if recv_ns is not None else self._clock(), note)
        return candidate.version

    def apply_staged_config(self) -> AlignConfig | None:
        """Atomically swap in the staged config at the processing boundary."""
        if self._staged is None:
            return None
        candidate, recv_ns, note = self._staged
        self.config = candidate
        self._staged = None
        self.storage.record_config(candidate.version, recv_ns,
                                   candidate.to_params(), note=note)
        self._enforce_cache_bounds()
        return candidate

    # ------------------------------------------------------------- events
    def add_event(
        self,
        kind: str,
        stamp_ns: int,
        recv_ns: int,
        seq: int | None = None,
        payload_hash: str = "",
        frame_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> Event:
        """Ingest one event. Parameter updates apply first (boundary), the
        event is classified by epoch, then everything that can settle does.
        """
        if kind not in (CAMERA, IMU):
            raise ValueError(f"unknown event kind {kind!r}")
        self.apply_staged_config()

        if seq is None:
            self._last_seq[kind] += 1
            seq = self._last_seq[kind]
        else:
            self._last_seq[kind] = max(self._last_seq[kind], seq)

        self._uid += 1
        ev = Event(
            kind=kind, stamp_ns=int(stamp_ns), recv_ns=int(recv_ns), seq=int(seq),
            payload_hash=payload_hash, uid=self._uid, frame_id=frame_id,
            payload=payload or {},
        )

        hwm = self._camera_hwm if kind == CAMERA else self._imu_hwm
        if hwm is not None and ev.stamp_ns < hwm - self.config.reset_threshold_ns:
            # Backwards jump beyond the reset threshold: a real clock reset,
            # not tolerable out-of-order data (a late packet lags the hwm by
            # at most out_of_order_ns; the reset threshold is configured
            # independently and should be >= that window). Seal the old
            # epoch; this event starts a fresh one.
            self._close_epoch(recv_ns, reason="clock_reset", open_new=True)

        if kind == CAMERA:
            self._insort(self._cameras, ev)
            if self._camera_hwm is None or ev.stamp_ns > self._camera_hwm:
                self._camera_hwm = ev.stamp_ns
        else:
            self._insort(self._imus, ev)
            if self._imu_hwm is None or ev.stamp_ns > self._imu_hwm:
                self._imu_hwm = ev.stamp_ns

        self._settle()
        self._enforce_cache_bounds()
        return ev

    # ----------------------------------------------------------- epochs
    def _close_epoch(self, recv_ns: int, reason: str, open_new: bool) -> None:
        """Flush everything still pending in the current epoch. Cameras/IMUs
        from the old epoch can never meet newer data. When ``open_new`` is
        false (shutdown drain) no empty epoch is created."""
        self._settle(force_all=True)
        if self.config.imu_policy == EXCLUSIVE:
            for imu in self._imus:
                self._emit(Decision(
                    epoch_id=self.epoch_id, status=Status.IMU_UNMATCHED,
                    config_version=self.config.version,
                    imu_seq=imu.seq, imu_stamp_ns=imu.stamp_ns,
                    reason=REASON_EPOCH_CLOSED, imu_payload_hash=imu.payload_hash,
                    event_recv_ns=imu.recv_ns, settled_recv_ns=recv_ns,
                ))
        self.storage.close_epoch(self.epoch_id, recv_ns)
        old_epoch = self.epoch_id
        self._cameras.clear()
        self._imus.clear()
        self._camera_hwm = None
        self._imu_hwm = None
        if open_new:
            self.epoch_id = old_epoch + 1
            self.storage.open_epoch(self.epoch_id, recv_ns, reason=reason)
    # ---------------------------------------------------------- matching
    def _candidate_evidence(self, cam: Event) -> list[dict[str, Any]]:
        cands = [
            {
                "imu_seq": imu.seq,
                "imu_stamp_ns": imu.stamp_ns,
                "dt_ns": cam.stamp_ns - imu.stamp_ns,
                "abs_dt_ns": abs(imu.stamp_ns - cam.stamp_ns),
                "payload_hash": imu.payload_hash,
            }
            for imu in self._imus
        ]
        cands.sort(key=lambda c: (c["abs_dt_ns"], c["imu_stamp_ns"], c["imu_seq"]))
        return cands[:MAX_EVIDENCE_CANDIDATES]

    def _decide_camera(self, cam: Event, settled_recv_ns: int,
                       force: bool, force_reason: str = REASON_CACHE_EVICTED) -> Decision:
        tol = self.config.tolerance_ns
        within = [imu for imu in self._imus if abs(imu.stamp_ns - cam.stamp_ns) <= tol]
        within.sort(key=lambda imu: (abs(imu.stamp_ns - cam.stamp_ns),
                                     imu.stamp_ns, imu.seq))
        candidates = self._candidate_evidence(cam)

        if within:
            best_abs = abs(within[0].stamp_ns - cam.stamp_ns)
            winners = [imu for imu in within
                       if abs(imu.stamp_ns - cam.stamp_ns) == best_abs]
            # Sorting by (distance, stamp, seq) already applies the rule:
            # nearest, ties -> earlier stamp, identical stamps -> lower seq.
            winner = winners[0] if len(winners) == 1 else within[0]
            tie = len(winners) > 1
            if tie:
                stamps = {imu.stamp_ns for imu in winners}
                reason = (REASON_TIE_IDENTICAL if len(stamps) == 1
                          else REASON_TIE_EARLIER)
            else:
                reason = REASON_NEAREST
            dt = cam.stamp_ns - winner.stamp_ns
            if self.config.imu_policy == EXCLUSIVE:
                self._imus.remove(winner)
            return Decision(
                epoch_id=self.epoch_id, status=Status.MATCHED,
                config_version=self.config.version,
                camera_seq=cam.seq, camera_stamp_ns=cam.stamp_ns,
                imu_seq=winner.seq, imu_stamp_ns=winner.stamp_ns,
                dt_ns=dt, abs_dt_ns=abs(dt), tie=tie, within_tolerance=True,
                forced_eviction=force and force_reason == REASON_CACHE_EVICTED,
                reason=reason, candidates=candidates,
                camera_payload_hash=cam.payload_hash,
                imu_payload_hash=winner.payload_hash,
                event_recv_ns=cam.recv_ns, settled_recv_ns=settled_recv_ns,
            )

        return Decision(
            epoch_id=self.epoch_id, status=Status.CAMERA_UNMATCHED,
            config_version=self.config.version,
            camera_seq=cam.seq, camera_stamp_ns=cam.stamp_ns,
            within_tolerance=False,
            forced_eviction=force and force_reason == REASON_CACHE_EVICTED,
            reason=(REASON_EPOCH_CLOSED if force and force_reason == REASON_EPOCH_CLOSED
                    else force_reason if force else REASON_EXPIRED),
            candidates=candidates, camera_payload_hash=cam.payload_hash,
            event_recv_ns=cam.recv_ns, settled_recv_ns=settled_recv_ns,
        )

    def _emit(self, d: Decision) -> int:
        return self.storage.record_decision(d)

    # ---------------------------------------------------------- settling
    def _settle(self, force_all: bool = False) -> None:
        """Settle every camera whose match can no longer change."""
        if not self._cameras:
            return
        margin = self._settle_margin()
        now_recv = max((c.recv_ns for c in self._cameras), default=0)
        if self._imus:
            now_recv = max(now_recv, max(i.recv_ns for i in self._imus))

        ready: list[Event] = []
        keep: list[Event] = []
        for cam in self._cameras:
            if force_all:
                ready.append(cam)
            elif (self._camera_hwm is not None and self._imu_hwm is not None
                  and self._camera_hwm - cam.stamp_ns >= margin
                  and self._imu_hwm - cam.stamp_ns >= margin):
                ready.append(cam)
            else:
                keep.append(cam)

        # Earliest event time first: under the exclusive policy the earliest
        # frame gets first claim on a shared IMU.
        ready.sort(key=lambda c: (c.stamp_ns, c.seq))
        self._cameras = keep
        for cam in ready:
            self._emit(self._decide_camera(
                cam, now_recv, force=force_all, force_reason=REASON_EPOCH_CLOSED))

        if not force_all:
            self._expire_imus(now_recv, margin)

    def _expire_imus(self, now_recv: int, margin: int) -> None:
        if self.config.imu_policy != EXCLUSIVE or not self._imus:
            return
        # An IMU may only be dropped once no camera that could still use it
        # can possibly arrive:
        #   * any pending camera must be farther than tolerance from it;
        #   * the earliest future (late) camera, whose stamp is
        #     camera_hwm - W, must also be farther than tolerance.
        reach = self._camera_reach()
        if reach is None:
            return
        cutoff = reach - self.config.tolerance_ns
        remaining: list[Event] = []
        for imu in self._imus:
            if imu.stamp_ns <= cutoff:
                self._emit(Decision(
                    epoch_id=self.epoch_id, status=Status.IMU_UNMATCHED,
                    config_version=self.config.version,
                    imu_seq=imu.seq, imu_stamp_ns=imu.stamp_ns,
                    reason=REASON_IMU_EXPIRED, imu_payload_hash=imu.payload_hash,
                    event_recv_ns=imu.recv_ns, settled_recv_ns=now_recv,
                ))
            else:
                remaining.append(imu)
        self._imus = remaining

    def _camera_reach(self) -> int | None:
        """Oldest camera stamp that can plausibly still arrive/settle."""
        if self._camera_hwm is None:
            return None
        reach = self._camera_hwm - self.config.out_of_order_ns
        if self._cameras and self._cameras[0].stamp_ns < reach:
            reach = self._cameras[0].stamp_ns
        return reach

    def _enforce_cache_bounds(self) -> None:
        """Keep both caches bounded; evicted events are settled explicitly."""
        while len(self._cameras) > self.config.camera_cache_max:
            victim = self._cameras.pop(0)  # oldest by (stamp, seq)
            self._emit(self._decide_camera(victim, victim.recv_ns, force=True))

        while len(self._imus) > self.config.imu_cache_max:
            victim = self._imus[0]
            reach = self._camera_reach()
            # Safe to drop only when the IMU cannot decide any pending or
            # foreseeable late camera.
            safe = reach is None or victim.stamp_ns < reach - self.config.tolerance_ns
            if not safe:
                # Boundedness still wins: force-settle the oldest camera so
                # an IMU is never dropped while it could decide a frame.
                forced_cam = self._cameras.pop(0)
                self._emit(self._decide_camera(forced_cam, forced_cam.recv_ns, force=True))
                continue
            self._imus.pop(0)
            if self.config.imu_policy == EXCLUSIVE:
                self._emit(Decision(
                    epoch_id=self.epoch_id, status=Status.IMU_UNMATCHED,
                    config_version=self.config.version,
                    imu_seq=victim.seq, imu_stamp_ns=victim.stamp_ns,
                    reason=REASON_IMU_CACHE, forced_eviction=True,
                    imu_payload_hash=victim.payload_hash,
                    event_recv_ns=victim.recv_ns, settled_recv_ns=victim.recv_ns,
                ))
            # Under reuse an IMU is a reference sample: a bounded drop is not
            # an "unmatched" event, so it leaves the cache without a record.

    # -------------------------------------------------------------- end
    def finalize(self, recv_ns: int | None = None) -> None:
        """Drain at shutdown: settle remaining cameras, close the epoch.
        No new empty epoch is opened."""
        recv = recv_ns if recv_ns is not None else self._clock()
        self._close_epoch(recv, reason="finalize", open_new=False)
