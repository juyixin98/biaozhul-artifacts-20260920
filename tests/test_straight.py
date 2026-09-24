"""直线场景测试：梯形速度剖面、首尾静止、每段约束核对。"""

import numpy as np

from speed_profile import scenarios
from speed_profile.topp import run

RTOL = 1e-9
ATOL = 1e-9


def test_straight_line_rest_to_rest():
    sc = scenarios.straight_line(length=100.0, n=201, v_max=10.0,
                                 a_max=2.0, a_min=-2.0)
    res = run(**{k: v for k, v in sc.items() if k != "description"})

    assert res.feasible, res.infeasible_reasons
    s = sc["s"]
    n = s.size

    # 首尾静止
    assert res.v[0] == 0.0
    assert res.v[-1] == 0.0
    assert np.all(res.v >= 0.0)

    # 不超过速度上限
    assert np.all(res.v <= sc["v_max"] + ATOL)
    assert res.max_v_violation <= ATOL

    # 段时间非负，节点时刻单调递增
    assert np.all(res.dt >= 0.0)
    assert np.all(np.diff(res.times) >= -ATOL)

    # 逐段核对加速度约束（独立重算，不依赖库内部诊断）
    ds = np.diff(s)
    a_check = (res.v[1:] ** 2 - res.v[:-1] ** 2) / (2.0 * ds)
    assert np.all(a_check <= sc["a_max"] + ATOL + RTOL * abs(sc["a_max"]))
    assert np.all(a_check >= sc["a_min"] - ATOL - RTOL * abs(sc["a_min"]))

    # 用 (v_{i+1}-v_i)/Δt 再核一遍加速度，与 v²/2s 形式一致
    a_time = (res.v[1:] - res.v[:-1]) / res.dt
    assert np.allclose(a_time, a_check, atol=1e-8, rtol=1e-8)

    # 存在一段真正达到巡航速度的平台
    assert np.isclose(np.max(res.v), sc["v_max"], atol=1e-8)
    cruise = np.isclose(res.v, sc["v_max"], atol=1e-7)
    assert cruise.sum() > 5

    # 独立复核（PCHIP 光滑插值 + quad 自适应积分），与闭式段时间不应差太多
    assert np.isfinite(res.total_time_indep_check)
    assert abs(res.total_time_indep_check - res.total_time) < 0.05

    # 解析梯形剖面总时间：两段加减速各 v/a=5 s、距离各 25 m，巡航 50 m / 10 = 5 s
    assert abs(res.total_time - 15.0) < 1e-7


def test_independent_cross_check_converges_with_grid():
    """网格加密时，PCHIP+quad 独立积分与闭式段时间的差应明显下降。"""
    diffs = []
    for n in (51, 201, 801):
        sc = scenarios.straight_line(length=100.0, n=n, v_max=10.0,
                                     a_max=2.0, a_min=-2.0)
        r = run(**{k: v for k, v in sc.items() if k != "description"})
        diffs.append(abs(r.total_time_indep_check - r.total_time))
    assert diffs[-1] < diffs[0] / 3
    assert diffs[-1] < 5e-3


def test_straight_line_symmetric_profile():
    """短到无法触顶的直线：全程加速-减速，峰值由距离决定。"""
    L = 20.0
    a = 4.0
    s = np.linspace(0.0, L, 401)
    res = run(s=s, kappa=np.zeros_like(s), v_max=50.0,
              a_max=a, a_min=-a, a_lat_max=None, v_start=0.0, v_end=0.0)
    assert res.feasible
    # v_peak^2 = a L（加速段与减速段各占一半距离）
    v_peak_expected = np.sqrt(a * L)
    assert abs(np.max(res.v) - v_peak_expected) < 1e-8
    # 总时间：两个三角形，t = 2 v_peak / a
    assert abs(res.total_time - 2.0 * v_peak_expected / a) < 1e-8


def test_straight_line_moving_endpoints():
    """首尾不必静止：给定可行的非零端点速度。"""
    s = np.linspace(0.0, 50.0, 51)
    res = run(s=s, kappa=np.zeros_like(s), v_max=12.0,
              a_max=2.0, a_min=-2.0, a_lat_max=None,
              v_start=3.0, v_end=4.0)
    assert res.feasible
    assert np.isclose(res.v[0], 3.0)
    assert np.isclose(res.v[-1], 4.0)
    ds = np.diff(s)
    a_check = (res.v[1:] ** 2 - res.v[:-1] ** 2) / (2.0 * ds)
    assert np.all(a_check <= 2.0 + 1e-9)
    assert np.all(a_check >= -2.0 - 1e-9)
