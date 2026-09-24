"""Unit tests for the pairing engine semantics."""

import pytest

from time_alignment.config import AlignConfig, EXCLUSIVE, REUSE
from time_alignment.engine import PairingEngine
from time_alignment.storage import Storage
from time_alignment.types import (
    CAMERA,
    IMU,
    Status,
    REASON_NEAREST,
    REASON_TIE_EARLIER,
    REASON_TIE_IDENTICAL,
    REASON_EXPIRED,
    REASON_EPOCH_CLOSED,
    REASON_CACHE_EVICTED,
)

NS = 1_000_000  # 1 ms


def make_engine(policy=EXCLUSIVE, tol_ns=5 * NS, ooo_ns=100 * NS,
                reset_ns=100 * NS, camera_cache=2048, imu_cache=8192):
    storage = Storage(":memory:")
    cfg = AlignConfig(
        version=1, tolerance_ns=tol_ns, imu_policy=policy,
        out_of_order_ns=ooo_ns, reset_threshold_ns=reset_ns,
        camera_cache_max=camera_cache, imu_cache_max=imu_cache)
    return PairingEngine(storage, cfg, initial_recv_ns=0), storage


def decisions(storage, status=None):
    rows = storage.all_decisions()
    if status is not None:
        rows = [r for r in rows if r["status"] == status]
    return rows


def matched_pairs(storage):
    return [(r["camera_seq"], r["imu_seq"], r["dt_ns"])
            for r in decisions(storage, Status.MATCHED.value)]


class TestNearestAndTies:
    def test_nearest_within_tolerance(self):
        eng, st = make_engine()
        eng.add_event(IMU, 100 * NS, 100 * NS, seq=1)
        eng.add_event(CAMERA, 102 * NS, 102 * NS, seq=0)
        eng.finalize(recv_ns=300 * NS)
        # dt = camera - imu: +2ms
        assert matched_pairs(st) == [(0, 1, 2 * NS)]
        row = decisions(st, Status.MATCHED.value)[0]
        assert row["reason"] == REASON_NEAREST
        assert row["tie"] is False
        assert row["within_tolerance"] is True

    def test_outside_tolerance_expires_explicitly(self):
        eng, st = make_engine(tol_ns=5 * NS)
        eng.add_event(IMU, 100 * NS, 100 * NS, seq=0)
        eng.add_event(CAMERA, 106 * NS, 106 * NS, seq=0)
        eng.finalize(recv_ns=400 * NS)
        un = decisions(st, Status.CAMERA_UNMATCHED.value)
        assert len(un) == 1
        assert un[0]["camera_seq"] == 0
        assert un[0]["reason"] in (REASON_EXPIRED, REASON_EPOCH_CLOSED)

    def test_equidistant_tie_chooses_earlier_imu(self):
        eng, st = make_engine()
        eng.add_event(IMU, 100 * NS, 100 * NS, seq=10)
        eng.add_event(IMU, 104 * NS, 104 * NS, seq=11)
        eng.add_event(CAMERA, 102 * NS, 102 * NS, seq=0)
        eng.finalize(recv_ns=400 * NS)
        pairs = matched_pairs(st)
        assert pairs == [(0, 10, 2 * NS)]  # earlier IMU (100ms) wins
        row = decisions(st, Status.MATCHED.value)[0]
        assert row["tie"] is True
        assert row["reason"] == REASON_TIE_EARLIER

    def test_identical_imu_timestamps_lowest_seq_wins(self):
        eng, st = make_engine()
        eng.add_event(IMU, 100 * NS, 100 * NS, seq=10)
        eng.add_event(IMU, 100 * NS, 101 * NS, seq=11)
        eng.add_event(CAMERA, 100 * NS, 102 * NS, seq=0)
        eng.finalize(recv_ns=400 * NS)
        assert matched_pairs(st) == [(0, 10, 0)]
        row = decisions(st, Status.MATCHED.value)[0]
        assert row["tie"] is True
        assert row["reason"] == REASON_TIE_IDENTICAL


class TestImuPolicy:
    def test_exclusive_imu_consumed_once(self):
        eng, st = make_engine(policy=EXCLUSIVE, tol_ns=2 * NS)
        eng.add_event(IMU, 100 * NS, 100 * NS, seq=0)
        eng.add_event(CAMERA, 100 * NS, 100 * NS, seq=0)
        eng.add_event(CAMERA, 101 * NS, 101 * NS, seq=1)
        eng.finalize(recv_ns=400 * NS)
        pairs = matched_pairs(st)
        imu_seqs = [p[1] for p in pairs]
        assert len(imu_seqs) == len(set(imu_seqs))
        assert pairs[0][0] == 0
        assert {r["camera_seq"] for r in
                decisions(st, Status.CAMERA_UNMATCHED.value)} == {1}

    def test_reuse_imu_can_match_many(self):
        eng, st = make_engine(policy=REUSE, tol_ns=2 * NS)
        eng.add_event(IMU, 100 * NS, 100 * NS, seq=0)
        eng.add_event(CAMERA, 100 * NS, 100 * NS, seq=0)
        eng.add_event(CAMERA, 101 * NS, 101 * NS, seq=1)
        eng.add_event(CAMERA, 102 * NS, 102 * NS, seq=2)
        eng.finalize(recv_ns=400 * NS)
        assert matched_pairs(st) == [(0, 0, 0), (1, 0, 1 * NS),
                                     (2, 0, 2 * NS)]
        assert decisions(st, Status.CAMERA_UNMATCHED.value) == []


class TestOutOfOrder:
    def test_late_imu_within_window_is_used(self):
        eng, st = make_engine(tol_ns=5 * NS, ooo_ns=100 * NS)
        eng.add_event(IMU, 0, 0, seq=0)
        eng.add_event(CAMERA, 0, 0, seq=0)
        eng.add_event(IMU, 200 * NS, 200 * NS, seq=2)
        eng.add_event(CAMERA, 200 * NS, 200 * NS, seq=2)
        # late IMU for the 100ms frame, arriving 90ms late (< 100ms window)
        eng.add_event(IMU, 100 * NS, 190 * NS, seq=1)
        eng.add_event(CAMERA, 100 * NS, 100 * NS, seq=1)
        eng.add_event(IMU, 310 * NS, 310 * NS, seq=3)
        eng.add_event(CAMERA, 310 * NS, 310 * NS, seq=3)
        eng.finalize(recv_ns=500 * NS)
        pairs = {c: i for c, i, _ in matched_pairs(st)}
        assert pairs[1] == 1  # late arrival honored


class TestEpochs:
    def test_backwards_jump_opens_new_epoch_no_cross_matching(self):
        eng, st = make_engine(reset_ns=100 * NS, ooo_ns=100 * NS)
        eng.add_event(IMU, 100 * NS, 100 * NS, seq=0)
        eng.add_event(CAMERA, 100 * NS, 100 * NS, seq=0)
        eng.add_event(IMU, 200 * NS, 200 * NS, seq=1)
        eng.add_event(CAMERA, 200 * NS, 200 * NS, seq=1)
        # backwards 150ms jump on camera -> epoch boundary; this frame is the
        # first event of the new epoch and cannot see the old epoch's IMU.
        eng.add_event(CAMERA, 50 * NS, 250 * NS, seq=50)
        assert eng.epoch_id == 1
        # IMU from the NEW epoch
        eng.add_event(IMU, 60 * NS, 251 * NS, seq=50)
        eng.add_event(CAMERA, 60 * NS, 252 * NS, seq=51)
        eng.add_event(IMU, 300 * NS, 300 * NS, seq=60)
        eng.add_event(CAMERA, 300 * NS, 301 * NS, seq=60)
        eng.finalize(recv_ns=500 * NS)
        rows = decisions(st, Status.MATCHED.value)
        epochs = {(r["camera_seq"], r["epoch_id"]) for r in rows}
        assert (0, 0) in epochs and (1, 0) in epochs
        assert (51, 1) in epochs and (60, 1) in epochs
        # frame 50 in epoch 1 must NOT match the old epoch's imu_seq 0 (which
        # is at exactly the same stamp): no cross-epoch pairing.
        r50 = [r for r in decisions(st, Status.CAMERA_UNMATCHED.value)
               if r["camera_seq"] == 50 and r["epoch_id"] == 1]
        assert r50

    def test_small_backward_step_within_window_is_not_reset(self):
        eng, st = make_engine(reset_ns=100 * NS, ooo_ns=100 * NS)
        for t in (0, 100, 200, 300):
            eng.add_event(IMU, t * NS, t * NS, seq=t // 100)
            eng.add_event(CAMERA, t * NS, t * NS, seq=t // 100)
        # 60ms late packet: no new epoch
        eng.add_event(IMU, 240 * NS, 360 * NS, seq=99)
        assert eng.epoch_id == 0
        eng.finalize(recv_ns=600 * NS)


class TestCacheBounds:
    def test_camera_cache_bound_forces_eviction_decision(self):
        eng, st = make_engine(camera_cache=2, tol_ns=5 * NS, ooo_ns=100 * NS)
        # Two cameras arrive with no IMU progress -> bounded cache forces
        # explicit eviction decisions.
        eng.add_event(CAMERA, 10 * NS, 10 * NS, seq=0)
        eng.add_event(CAMERA, 11 * NS, 11 * NS, seq=1)
        eng.add_event(CAMERA, 12 * NS, 12 * NS, seq=2)
        rows = decisions(st, Status.CAMERA_UNMATCHED.value)
        assert rows and rows[0]["forced_eviction"] is True
        assert rows[0]["reason"] == REASON_CACHE_EVICTED
        eng.finalize(recv_ns=400 * NS)

    def test_imu_cache_bound_does_not_lose_decidable_match(self):
        # tiny IMU cache + a pending camera: engine must force-settle the
        # camera rather than dropping the IMU that could decide it.
        eng, st = make_engine(policy=EXCLUSIVE, imu_cache=1, camera_cache=4,
                              tol_ns=5 * NS, ooo_ns=10 * NS)
        eng.add_event(CAMERA, 0, 0, seq=0)
        eng.add_event(IMU, 0, 1 * NS, seq=0)   # exact match for cam0
        eng.add_event(IMU, 200 * NS, 200 * NS, seq=2)  # overflows IMU cache
        pairs = matched_pairs(st)
        assert (0, 0, 0) in pairs
        eng.finalize(recv_ns=600 * NS)


class TestAtomicConfig:
    def test_stage_only_applies_at_boundary_and_versions_decisions(self):
        eng, st = make_engine(tol_ns=1 * NS)
        # A long pre-stream settles completely under v1 (hwms sit at 1000ms
        # before any change is staged).
        for t in (0, 110, 220, 330, 440, 550, 660, 770, 880, 1000):
            eng.add_event(IMU, t * NS, t * NS, seq=t // 110)
            eng.add_event(CAMERA, t * NS, t * NS, seq=t // 110)
        pre = {r["camera_seq"]: r["config_version"]
               for r in decisions(st, Status.MATCHED.value)}
        assert pre and all(v == 1 for v in pre.values())
        # Boundary: widen tolerance 1ms -> 20ms before the new pair arrives.
        eng.stage_config(tolerance_ns=20 * NS)
        # 10ms offset: impossible under v1, a valid nearest match under v2.
        eng.add_event(IMU, 1010 * NS, 1010 * NS, seq=100)
        eng.add_event(CAMERA, 1020 * NS, 1020 * NS, seq=100)
        eng.add_event(IMU, 1130 * NS, 1130 * NS, seq=101)
        eng.add_event(CAMERA, 1130 * NS, 1130 * NS, seq=101)
        eng.finalize(recv_ns=1400 * NS)
        v2_matches = [(r["camera_seq"], r["imu_seq"], r["dt_ns"])
                      for r in decisions(st, Status.MATCHED.value)
                      if r["config_version"] == 2]
        # The 10ms pair exists as a match ONLY because v2 widened tolerance.
        assert (100, 100, 10 * NS) in v2_matches
        assert (101, 101, 0) in v2_matches
        # All pre-boundary cameras (seq < 100) matched exactly under v1 math.
        assert all(abs(r["dt_ns"]) == 0 for r in decisions(st, Status.MATCHED.value)
                   if r["camera_seq"] is not None and r["camera_seq"] < 100)
        versions = [c["version"] for c in st.list_configs()]
        assert versions == [1, 2]

    def test_invalid_param_rejected(self):
        eng, st = make_engine()
        with pytest.raises(ValueError):
            eng.stage_config(tolerance_ns=-5)
        with pytest.raises(ValueError):
            eng.stage_config(imu_policy="banana")
        with pytest.raises(ValueError):
            eng.stage_config(camera_cache_max=0)
        assert eng.config.version == 1
