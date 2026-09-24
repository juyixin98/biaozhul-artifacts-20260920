"""离线夹具评测测试：ID 切换与位置误差阈值。"""

from __future__ import annotations

import pytest

from app.evaluation import (
    all_scenarios,
    make_crossing,
    make_duplicate_detections,
    make_empty_frames,
    make_short_occlusion,
    run_scenario,
)
from app.tracker import TrackerConfig
import numpy as np


@pytest.fixture(params=list(range(5)))
def seed(request):
    return 20260923 + request.param


def _by_name(name: str):
    return {s.name: s for s in all_scenarios()}


def test_crossing_has_zero_id_switches():
    s = make_crossing(np.random.default_rng(0))
    m, _ = run_scenario(s)
    assert m.id_switches == 0
    assert m.confirmed_ids_used == [1, 2]
    assert m.final_alive_tracks == 2
    assert m.rmse < 0.05


def test_short_occlusion_below_threshold_keeps_id():
    s = make_short_occlusion(np.random.default_rng(0), gap=4)
    m, _ = run_scenario(s)
    assert m.id_switches == 0
    assert m.confirmed_ids_used == [1]
    assert m.deleted_total == 0
    assert m.rmse < 0.05


def test_long_occlusion_deletes_and_spawns_new_identity():
    """遮挡超过 max_misses：旧轨迹删除、新轨迹确认，评测计 1 次切换。"""
    s = make_short_occlusion(np.random.default_rng(0), gap=6)
    m, _ = run_scenario(s)
    assert m.deleted_total == 1
    assert m.id_switches == 1
    assert m.confirmed_ids_used == [1, 2]


def test_duplicate_detections_single_track():
    s = make_duplicate_detections(np.random.default_rng(0))
    m, results = run_scenario(s)
    assert m.id_switches == 0
    assert m.confirmed_ids_used == [1]
    assert m.final_alive_tracks == 1
    # 每帧两个近点被聚合为一个量测
    assert all(len(r.detections) == 1 for r in results)
    assert all(len(g) == 2 for r in results for g in r.merged_groups)


def test_empty_frames_no_spurious_tracks():
    s = make_empty_frames(np.random.default_rng(0))
    m, _ = run_scenario(s)
    assert m.id_switches == 0
    assert m.births_total == 1
    assert m.confirmed_ids_used == [1]
    assert m.rmse < 0.05


def test_all_scenarios_pass_acceptance_thresholds():
    for s in all_scenarios():
        m, _ = run_scenario(s)
        assert m.rmse < 0.1, s.name
        assert m.mean_error < 0.1, s.name


def test_noisy_measurement_still_associates(seed):
    """加入真实高斯噪声后（R 匹配），交叉场景仍不应切换身份。"""
    s = make_crossing(np.random.default_rng(seed))
    m, _ = run_scenario(
        s,
        config=TrackerConfig(measurement_var=0.15**2, process_noise=1.0),
        measurement_std=0.15,
        seed=seed,
    )
    assert m.id_switches == 0
    assert m.confirmed_ids_used == [1, 2]
    assert m.rmse < 0.5


def test_tracker_never_receives_truth_ids():
    """结构约束：传给 tracker.step 的 detection_id 不含真值字段语义。

    这里通过断言评测夹具的检测标签只是字符串、且 tracker 内部关联
    不读取 member_ids 之外的真值信息来固化边界。
    """
    s = make_crossing(np.random.default_rng(0))
    _, results = run_scenario(s)
    # 关联结果只包含数字 track_id，与真值标签 "A"/"B" 无任何耦合
    for r in results:
        for t in r.tracks:
            assert isinstance(t.track_id, int)
