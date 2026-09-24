"""急弯（发卡弯）场景测试：入弯减速、弯中限速、出弯加速，逐段核对约束。"""

import numpy as np

from speed_profile import scenarios
from speed_profile.topp import run

RTOL = 1e-9
ATOL = 1e-9


def _check_segments(s, v, a_min, a_max, v_max, kappa, a_lat_max):
    ds = np.diff(s)
    real = ds > 1e-15

    # 纵向加速度
    a_seg = np.full(ds.size, np.nan)
    a_seg[real] = (v[1:][real] ** 2 - v[:-1][real] ** 2) / (2.0 * ds[real])
    assert np.nanmax(a_seg) <= a_max + ATOL + RTOL * abs(a_max)
    assert np.nanmin(a_seg) >= a_min - ATOL - RTOL * abs(a_min)

    # 速度上限（含横向）
    cap = np.full(s.size, np.inf)
    curved = np.abs(kappa) > 0
    cap[curved] = np.sqrt(a_lat_max / np.abs(kappa[curved]))
    cap = np.minimum(cap, v_max)
    assert np.all(v <= cap + 1e-9)


def test_hairpin_slowdown_and_constraints():
    sc = scenarios.hairpin()
    res = run(**{k: v for k, v in sc.items() if k != "description"})

    assert res.feasible, res.infeasible_reasons
    s, kappa = sc["s"], sc["kappa"]
    assert res.v[0] == 0.0 and res.v[-1] == 0.0

    v_lat = np.sqrt(sc["a_lat_max"] / (1.0 / 4.0))  # R=4, a_lat=4 -> 4 m/s

    # 弯内速度不得超过横向限速，且至少有一个弯内点贴着该上限（最优性抽查）
    arc = kappa > 0
    assert np.all(res.v[arc] <= v_lat + 1e-9)
    assert np.max(res.v[arc]) >= v_lat - 1e-6

    # 入弯前存在明显减速：弯前直道峰值显著高于弯内峰值
    before_arc = s < 20.0 - 0.2
    assert np.max(res.v[before_arc]) > np.max(res.v[arc]) + 1.0

    # 出弯后重新加速
    after_arc = s > 20.0 + np.pi * 4.0 + 0.2
    assert np.max(res.v[after_arc]) > np.max(res.v[arc]) + 1.0

    _check_segments(s, res.v, sc["a_min"], sc["a_max"],
                    sc["v_max"], kappa, sc["a_lat_max"])

    # 时间合理：正长度段 dt > 0 且有限；总时间有限
    ds = np.diff(s)
    assert np.all(res.dt[ds > 1e-15] > 0.0)
    assert np.all(np.isfinite(res.dt))
    assert np.isfinite(res.total_time)


def test_tighter_lateral_limit_gives_lower_speed_and_more_time():
    """横向加速度上限收紧时，弯内速度必须更低，总时间必须更长。"""
    sc_loose = scenarios.hairpin(a_lat_max=9.0)
    sc_tight = scenarios.hairpin(a_lat_max=1.0)
    r_loose = run(**{k: v for k, v in sc_loose.items() if k != "description"})
    r_tight = run(**{k: v for k, v in sc_tight.items() if k != "description"})
    assert r_loose.feasible and r_tight.feasible
    arc = sc_loose["kappa"] > 0
    assert np.max(r_tight.v[arc]) < np.max(r_loose.v[arc])
    assert r_tight.total_time > r_loose.total_time


def test_sharp_bend_near_stop_rest_to_rest():
    """极急弯（横向限速接近 0）在首尾静止下仍可行：近乎停车地通过。"""
    sc = scenarios.hairpin(radius=0.25, a_lat_max=1.0, ds=0.05,
                           a_max=3.0, a_min=-4.0)
    res = run(**{k: v for k, v in sc.items() if k != "description"})
    assert res.feasible, res.infeasible_reasons
    assert np.max(res.v[sc["kappa"] > 0]) <= np.sqrt(1.0 / 0.25) + 1e-9
