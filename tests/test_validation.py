"""输入校验与不可行情形诊断测试。"""

import numpy as np
import pytest

from speed_profile.topp import run


def test_s_must_be_nondecreasing():
    s = np.array([0.0, 1.0, 0.5, 2.0])
    with pytest.raises(ValueError, match="非递减"):
        run(s=s, kappa=np.zeros(4))


def test_length_mismatch():
    with pytest.raises(ValueError, match="长度必须相同"):
        run(s=np.array([0.0, 1.0, 2.0]), kappa=np.zeros(2))


def test_too_few_points():
    with pytest.raises(ValueError, match="至少需要 2 个"):
        run(s=np.array([0.0]), kappa=np.zeros(1))


def test_nan_rejected():
    with pytest.raises(ValueError, match="NaN"):
        run(s=np.array([0.0, 1.0, np.nan]), kappa=np.zeros(3))


def test_bounds_order():
    with pytest.raises(ValueError, match="a_min < a_max"):
        run(s=np.array([0.0, 1.0]), kappa=np.zeros(2), a_min=2.0, a_max=1.0)


def test_negative_vmax_rejected():
    with pytest.raises(ValueError, match="v_max"):
        run(s=np.array([0.0, 1.0]), kappa=np.zeros(2), v_max=-1.0)


def test_start_speed_above_cap_infeasible():
    """起点速度超过该处上限：标记不可行而不是静默裁剪。"""
    s = np.linspace(0, 10, 11)
    res = run(s=s, kappa=np.zeros_like(s), v_max=5.0,
              a_max=2.0, a_min=-2.0, a_lat_max=None,
              v_start=8.0, v_end=0.0)
    assert not res.feasible
    assert any("起点速度不可达" in r for r in res.infeasible_reasons)


def test_stalled_segment_infeasible():
    """正长度段两端速度上限均为 0（v_max=0 通道）：无法通行。"""
    s = np.array([0.0, 1.0, 2.0, 3.0])
    v_max = np.array([1.0, 0.0, 1.0, 1.0])
    res = run(s=s, kappa=np.zeros(4), v_max=v_max,
              a_max=2.0, a_min=-2.0, a_lat_max=None,
              v_start=0.0, v_end=0.0)
    assert not res.feasible
    assert any("无法通行" in r for r in res.infeasible_reasons)


def test_endpoint_too_far_to_reach():
    """距离太短却要求高终点速度：终点速度不可达。"""
    s = np.linspace(0, 0.1, 6)
    res = run(s=s, kappa=np.zeros_like(s), v_max=50.0,
              a_max=1.0, a_min=-1.0, a_lat_max=None,
              v_start=0.0, v_end=5.0)
    assert not res.feasible
    assert any("终点速度不可达" in r for r in res.infeasible_reasons)


def test_per_point_vmax_array():
    s = np.linspace(0, 10, 11)
    v_max = np.full(11, 5.0)
    v_max[5] = 2.0
    res = run(s=s, kappa=np.zeros(11), v_max=v_max,
              a_max=5.0, a_min=-5.0, a_lat_max=None,
              v_start=0.0, v_end=0.0)
    assert res.feasible
    assert res.v[5] <= 2.0 + 1e-12
