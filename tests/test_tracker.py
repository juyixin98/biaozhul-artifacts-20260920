"""Tracker 关联逻辑测试。"""

from __future__ import annotations

import pytest

from app.tracker import (
    CHI2_99_2DOF,
    StaleFrameError,
    Tracker,
    TrackerConfig,
)


def make_tracker(**kwargs) -> Tracker:
    return Tracker(TrackerConfig(**kwargs))


# ------------------------------------------------------------ 确认与删除
def test_track_confirmed_after_consecutive_hits():
    tr = make_tracker(confirm_hits=3)
    r0 = tr.step(0, 0.0, [(0.0, 0.0, "a0")])
    assert r0.births == [1]
    assert r0.tracks[0].state == "tentative"
    tr.step(1, 1.0, [(0.5, 0.0, "a1")])
    r2 = tr.step(2, 2.0, [(1.0, 0.0, "a2")])
    states = {t.track_id: t.state for t in r2.tracks}
    assert states[1] == "confirmed"
    assert r2.next_track_id == 2


def test_hit_streak_resets_on_miss():
    """连续命中被丢测打断后需要重新累计。"""
    tr = make_tracker(confirm_hits=3)
    tr.step(0, 0.0, [(0.0, 0.0, None)])
    tr.step(1, 1.0, [(0.5, 0.0, None)])
    r2 = tr.step(2, 2.0, [])  # 打断
    assert r2.tracks[0].hit_streak == 0
    assert r2.tracks[0].state == "tentative"
    tr.step(3, 3.0, [(1.5, 0.0, None)])
    tr.step(4, 4.0, [(2.0, 0.0, None)])
    r5 = tr.step(5, 5.0, [(2.5, 0.0, None)])
    assert r5.tracks[0].state == "confirmed"


def test_deleted_after_max_misses():
    tr = make_tracker(confirm_hits=3, max_misses=3)
    for k in range(3):
        tr.step(k, float(k), [(0.5 * k, 0.0, None)])
    for k in range(3, 7):
        r = tr.step(k, float(k), [])
    # 第 4 个连续空帧（misses 4 > 3）删除
    assert 1 in r.deleted
    assert all(t.state != "deleted" or t.track_id == 1 for t in r.tracks)
    alive = [t for t in r.tracks if t.state != "deleted"]
    assert alive == []


def test_short_coast_keeps_identity():
    tr = make_tracker(confirm_hits=3, max_misses=5)
    # 前 5 帧连续命中（0..4），轨迹已确认且速度已收敛
    for k in range(5):
        tr.step(k, float(k), [(0.8 * k, 0.0, None)])
    coast = tr.step(5, 5.0, [])
    assert coast.tracks[0].state == "coasting"
    # 滑行预测位置继续匀速前移
    pred_x = coast.tracks[0].position[0]
    assert pred_x > 0.8 * 4
    back = tr.step(6, 6.0, [(0.8 * 6, 0.0, None)])
    assert back.matched == [(1, 0)]
    assert back.tracks[0].track_id == 1
    assert back.tracks[0].state == "confirmed"
    assert back.births == []

# ---------------------------------------------------------------- 乱序
def test_out_of_order_frame_id_rejected():
    tr = make_tracker()
    tr.step(1, 1.0, [])
    with pytest.raises(StaleFrameError):
        tr.step(1, 2.0, [])  # 重复 frame_id（在 tracker 层直接拒绝）
    with pytest.raises(StaleFrameError):
        tr.step(0, 3.0, [])  # 回退
    # 拒绝后状态未被污染，仍可继续
    r = tr.step(2, 2.0, [])
    assert r.frame_id == 2


def test_timestamp_regression_rejected():
    tr = make_tracker()
    tr.step(0, 2.0, [])
    with pytest.raises(StaleFrameError):
        tr.step(1, 1.5, [])
    with pytest.raises(StaleFrameError):
        tr.step(2, 2.0, [])  # 同时间戳也拒绝


# ---------------------------------------------------------- 重复检测聚合
def test_intra_frame_duplicates_merged():
    tr = make_tracker(merge_radius=0.25)
    r = tr.step(
        0,
        0.0,
        [(1.0, 1.0, "x-a"), (1.04, 0.97, "x-b"), (9.0, 9.0, "far")],
    )
    # 两个近点合并为一个检测 + 一个远点 => 2 个检测，2 条新生
    assert len(r.detections) == 2
    assert len(r.births) == 2
    assert sorted(r.merged_groups[0]) == ["x-a", "x-b"]
    merged = next(d for d in r.detections if "x-a" in d.member_ids)
    assert merged.x == pytest.approx(1.02, abs=1e-9)


def test_duplicates_do_not_spawn_extra_tracks_over_time():
    tr = make_tracker(merge_radius=0.25)
    for k in range(8):
        r = tr.step(
            k,
            float(k),
            [
                (0.7 * k, 0.0, f"A-{k}-a"),
                (0.7 * k + 0.04, -0.03, f"A-{k}-b"),
            ],
        )
    assert r.next_track_id == 2  # 只有一条轨迹
    assert sum(1 for t in r.tracks if t.state == "confirmed") == 1


# ------------------------------------------------------------------ dt
def test_variable_dt_drives_prediction():
    """不同 dt 下匀速预测应与解析位置一致（dt 真实进入 F 与 Q）。"""
    tr = make_tracker(process_noise=0.1, measurement_var=0.01)
    # 前 5 帧等间隔连续命中（frame0 出生在 x=0），速度收敛到 vx=1
    for k in range(5):
        tr.step(k, float(k), [(1.0 * k, 0.0, None)])
    vx = tr.tracks[1].x[2]
    assert vx == pytest.approx(1.0, abs=0.05)
    # 大间隔 dt=3.0：frame4 时间 4.0 -> frame5 时间 7.0
    r = tr.step(5, 7.0, [])
    assert r.dt == pytest.approx(3.0)
    # 滑行预测 = 上一帧位置 + 3 * vx
    pred = r.tracks[0].position[0]
    assert pred == pytest.approx(4.0 + 3.0 * vx, abs=0.02)


# ---------------------------------------------------------------- 门控
def test_gate_blocks_far_assignment_and_spawns():
    tr = make_tracker(confirm_hits=2, gate_threshold=CHI2_99_2DOF)
    tr.step(0, 0.0, [(0.0, 0.0, None)])
    # 50 米外的检测必在门外：应拒绝关联并新生
    r = tr.step(1, 1.0, [(50.0, 50.0, None)])
    rejected = r.rejected_by_gate
    assert len(rejected) == 1
    assert rejected[0].within_gate is False
    assert rejected[0].selected is False
    assert r.births == [2]
    # selected 的分配必须在门内
    assert all(a.selected is False for a in rejected)


def test_assignment_record_fields_present():
    tr = make_tracker()
    tr.step(0, 0.0, [(0.0, 0.0, None)])
    r = tr.step(1, 1.0, [(0.4, 0.05, None)])
    a = next(x for x in r.assignments if x.selected)
    assert a.track_id == 1
    assert a.detection_index == 0
    assert a.within_gate is True
    assert a.euclidean_distance >= 0.0
    assert a.mahalanobis_sq <= a.gate_threshold
    assert len(a.predicted_xy) == 2
    assert r.selection_basis["cost"] == "mahalanobis_sq"


def test_empty_frames_before_birth_spawn_nothing():
    tr = make_tracker()
    for k in range(4):
        r = tr.step(k, float(k), [])
    assert r.tracks == []
    assert r.next_track_id == 1
    r = tr.step(4, 4.0, [(1.0, 1.0, None)])
    assert r.births == [1]


def test_one_to_one_assignment_under_crossing():
    """两条轨迹近距离错时交叉：全局一一对应，不共享检测。"""
    tr = make_tracker(confirm_hits=2)
    for k in range(15):
        r = tr.step(
            k,
            float(k),
            [(float(k), float(k), f"A-{k}"),
             (float(8 + k), float(12 - k), f"B-{k}")],
        )
        det_used = [j for _, j in r.matched]
        assert len(det_used) == len(set(det_used))
    ids = {t.track_id for t in r.tracks}
    assert ids == {1, 2}
    assert r.deleted == []
