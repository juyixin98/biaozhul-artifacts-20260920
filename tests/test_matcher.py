"""Unit tests for the alignment engine itself."""
import pytest

from time_alignment.matcher import AlignmentMatcher
from time_alignment.model import (
    AlignParams, StreamEvent, PairStatus, ImuStatus, EpochReason, UNLIMITED_USES,
    RecordKind,
)

TOL = 5_000_000
RT = 100_000_000


def cam(cid, t, recv=None):
    return StreamEvent("camera", t, cid, recv)


def imu(iid, t, recv=None):
    return StreamEvent("imu", t, iid, recv)


def tick(m, t=1_000_000_000):
    """Advance the watermark so all buffered events before t finalise."""
    return m.register(imu("__tick__", t))


def recent_pairs(m):
    m.finalize()
    return {o.payload["camera"]["id"]: o.payload
            for o in m.recent if o.kind is RecordKind.PAIR}


def recent_imus(m):
    m.finalize()
    return {o.payload["imu"]["id"]: o.payload
            for o in m.recent if o.kind is RecordKind.IMU}


def test_nearest_within_tolerance_and_exclusive_consume():
    m = AlignmentMatcher(AlignParams(tolerance_ns=TOL, reorder_tolerance_ns=RT))
    m.register(imu("i0", 10_000_000))
    m.register(imu("i1", 12_000_000))
    m.register(cam("c0", 11_000_000))
    m.register(cam("c1", 12_000_000))
    m.register(imu("tick", 130_000_000))  # finalises c0->i0, c1->i1
    # a late duplicate near 10ms inside the window: i0 already consumed
    m.register(cam("c2", 11_000_000, 225_000_000))
    pm = recent_pairs(m)
    assert pm["c0"]["imu"]["id"] == "i0"
    assert pm["c1"]["imu"]["id"] == "i1"
    assert pm["c2"]["status"] == PairStatus.EPOCH_CLOSED_UNPAIRED.value \
        or pm["c2"]["imu"] is None


def test_tie_earlier_timestamp_wins():
    m = AlignmentMatcher(AlignParams(tolerance_ns=TOL))
    m.register(imu("i_early", 9_000_000))
    m.register(imu("i_late", 11_000_000))
    m.register(cam("c0", 10_000_000))
    pm = recent_pairs(m)
    assert pm["c0"]["imu"]["id"] == "i_early"
    assert pm["c0"]["tie_break"] == "earlier"


def test_equal_timestamp_tie_broken_by_receive_seq():
    m = AlignmentMatcher(AlignParams(tolerance_ns=TOL))
    m.register(imu("i0", 10_000_000))
    m.register(imu("i1", 10_000_000))
    m.register(cam("c0", 10_000_000))
    m.register(cam("c1", 10_000_000))
    pm = recent_pairs(m)
    assert pm["c0"]["imu"]["id"] == "i0"
    assert pm["c0"]["dt_ns"] == 0
    assert pm["c1"]["imu"]["id"] == "i1"


def test_burst_ordering_with_buffered_frames():
    m = AlignmentMatcher(AlignParams(tolerance_ns=TOL))
    for e in [cam("c0", 10_000_000), cam("c1", 10_001_000),
              imu("i0", 9_999_000), imu("i1", 10_001_000)]:
        m.register(e)
    pm = recent_pairs(m)
    assert pm["c0"]["imu"]["id"] == "i0"
    assert pm["c1"]["imu"]["id"] == "i1"
    assert pm["c0"]["dt_ns"] == 1_000


def test_late_within_reorder_window_is_accepted():
    m = AlignmentMatcher(AlignParams(tolerance_ns=TOL,
                                     reorder_tolerance_ns=RT))
    m.register(imu("i1", 205_000_000))
    m.register(imu("i2", 240_000_000))
    # frame at 201ms received at 230ms: within 100ms of latest seen (240ms)
    r = m.register(cam("c_late", 201_000_000, 230_000_000))
    assert r.pairs == []  # still protected by the 100ms window
    tick(m, 350_000_000)
    pm = recent_pairs(m)
    assert pm["c_late"]["imu"]["id"] == "i1"
    assert pm["c_late"]["status"] == "matched"
    assert pm["c_late"]["dt_ns"] == -4_000_000


def test_backjump_opens_new_epoch_no_cross_epoch_pairing():
    m = AlignmentMatcher(AlignParams(tolerance_ns=TOL,
                                     reorder_tolerance_ns=RT))
    m.register(imu("i_old", 10_000_000))
    m.register(cam("c_old", 11_000_000))
    tick(m, 200_000_000)  # epoch 1 pair finalises
    # back-jump beyond the 100ms reorder window
    r = m.register(cam("c_bj", 50_000_000, 201_000_000))
    opens = [e for e in r.epochs if e.get("event") == "open"]
    assert opens[-1]["reason"] == EpochReason.CLOCK_BACKJUMP.value
    # epoch 1 data must not pair across the boundary
    pm = recent_pairs(m)
    assert pm["c_old"]["imu"]["id"] == "i_old" and pm["c_old"]["epoch"] == 1
    assert pm["c_bj"]["epoch"] == 2 and pm["c_bj"]["imu"] is None
    assert pm["c_bj"]["status"] == PairStatus.STREAM_END_UNPAIRED.value


def test_explicit_reset_closes_and_reopens():
    m = AlignmentMatcher(AlignParams(tolerance_ns=TOL))
    m.register(imu("i0", 10_000_000))
    r = m.register(StreamEvent("reset", None, "r1"))
    reasons = [e.get("closed_reason") for e in r.epochs
               if e.get("event") == "close"]
    assert reasons == [EpochReason.RESET_EVENT.value]
    opens = [e["epoch"] for e in r.epochs if e.get("event") == "open"]
    assert opens == [2]
    m.register(imu("i_new", 2_000_000))
    m.register(cam("c0", 1_000_000))
    pm = recent_pairs(m)
    assert pm["c0"]["imu"]["id"] == "i_new"
    assert pm["c0"]["epoch"] == 2


def test_reuse_config_unlimited_and_capped():
    p = AlignParams(imu_exclusive=False, imu_max_uses=UNLIMITED_USES,
                    tolerance_ns=TOL)
    m = AlignmentMatcher(p)
    m.register(imu("i0", 10_000_000))
    for k in range(3):
        m.register(cam(f"c{k}", 10_000_000 + k * 1_000))
    pm = recent_pairs(m)
    for k in range(3):
        assert pm[f"c{k}"]["imu"]["id"] == "i0"
        assert pm[f"c{k}"]["imu_use_count"] == k + 1
    assert recent_imus(m)["i0"]["use_count"] == 3

    m2 = AlignmentMatcher(AlignParams(imu_exclusive=False, imu_max_uses=2,
                                      tolerance_ns=TOL))
    m2.register(imu("j0", 10_000_000))
    m2.register(cam("a0", 10_000_000))
    m2.register(cam("a1", 10_001_000))
    m2.register(cam("a2", 10_002_000))
    pm = recent_pairs(m2)
    assert pm["a0"]["status"] == "matched"
    assert pm["a1"]["status"] == "matched"
    assert pm["a2"]["status"] != "matched"  # cap reached, no other IMU


def test_unused_imu_explicitly_marked():
    m = AlignmentMatcher(AlignParams(tolerance_ns=TOL))
    m.register(imu("i_lonely", 10_000_000))
    tick(m, 200_000_000)
    im = recent_imus(m)
    assert im["i_lonely"]["status"] == ImuStatus.UNUSED_EXPIRED.value
    assert im["i_lonely"]["use_count"] == 0


def test_bounded_camera_buffer_emits_overflow():
    m = AlignmentMatcher(AlignParams(max_camera_pending=2,
                                     tolerance_ns=TOL,
                                     reorder_tolerance_ns=RT))
    m.register(cam("old0", 0))
    m.register(cam("old1", 1_000))
    m.register(cam("old2", 2_000))  # evicts old0
    statuses = [o.payload["status"] for o in m.recent
                if o.kind is RecordKind.PAIR]
    assert PairStatus.CAMERA_BUFFER_OVERFLOW.value in statuses


def test_bounded_imu_buffer_emits_overflow():
    m = AlignmentMatcher(AlignParams(max_imu_pending=2, tolerance_ns=TOL))
    m.register(imu("a", 0))
    m.register(imu("b", 1_000))
    m.register(imu("c", 2_000))  # evicts a
    statuses = [o.payload["status"] for o in m.recent
                if o.kind is RecordKind.IMU]
    assert ImuStatus.IMU_BUFFER_OVERFLOW.value in statuses


def test_params_take_effect_at_boundary_and_versioned():
    m = AlignmentMatcher(AlignParams(version=1, imu_exclusive=True))
    m.register(imu("i0", 10_000_000))
    m.register(cam("c0", 10_000_000))
    tick(m, 200_000_000)
    m.request_params(AlignParams(
        version=2, imu_exclusive=False, imu_max_uses=UNLIMITED_USES))
    m.register(imu("i1", 300_000_000))
    m.register(cam("c1", 300_000_000))
    m.register(cam("c2", 300_001_000))
    tick(m, 500_000_000)
    pm = recent_pairs(m)
    assert pm["c0"]["params_version"] == 1
    assert pm["c1"]["params_version"] == pm["c2"]["params_version"] == 2
    assert pm["c1"]["imu"]["id"] == pm["c2"]["imu"]["id"] == "i1"
    assert any(o.kind is RecordKind.PARAMS and o.payload["version"] == 2
               for o in m.recent)


def test_clock_generation_change_resets_even_within_window():
    # A /clock restart that moves time back less than 100ms would evade the
    # back-jump rule; the explicit clock generation must still reset epoch.
    m = AlignmentMatcher(AlignParams(tolerance_ns=TOL,
                                     reorder_tolerance_ns=RT))
    m.register(imu("i0", 100_000_000))
    m.register(cam("c0", 101_000_000))
    r = m.register(StreamEvent("camera", 95_000_000, "c1",
                               clock_generation=1))
    opens = [e for e in r.epochs if e.get("event") == "open"]
    assert opens[-1]["reason"] == EpochReason.RESET_EVENT.value
    pm = recent_pairs(m)
    assert pm["c0"]["epoch"] == 1 and pm["c1"]["epoch"] == 2
    assert pm["c1"]["imu"] is None


def test_params_rejects_non_monotonic_and_invalid():
    m = AlignmentMatcher(AlignParams(version=5))
    with pytest.raises(ValueError):
        m.request_params(AlignParams(version=5))
    with pytest.raises(ValueError):
        m.request_params(AlignParams(version=6, tolerance_ns=-1))


def test_atomicity_half_applied_config_never_observed():
    m = AlignmentMatcher(AlignParams(version=1))
    m.request_params(AlignParams(version=2, tolerance_ns=6_000_000))
    m.request_params(AlignParams(version=3, tolerance_ns=7_000_000))
    r = m.register(imu("tick", 1_000_000_000))
    versions = [o.payload["version"] for o in r.outcomes
                if o.kind is RecordKind.PARAMS]
    assert versions == [2, 3]
    assert m.params.version == 3 and m.params.tolerance_ns == 7_000_000
