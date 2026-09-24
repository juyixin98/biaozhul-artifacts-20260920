"""零长度段（相邻重复弧长点）处理测试。"""

import numpy as np

from speed_profile.topp import run


def test_duplicate_points_mid_path():
    s = np.array([0.0, 5.0, 10.0, 10.0, 10.0, 20.0, 30.0, 30.0, 40.0])
    kappa = np.zeros_like(s)
    res = run(s=s, kappa=kappa, v_max=8.0, a_max=2.0, a_min=-2.0,
              a_lat_max=None, v_start=0.0, v_end=0.0)

    assert res.feasible, res.infeasible_reasons
    ds = np.diff(s)
    zero = ds == 0.0

    # 零长度段：时间为 0，加速度为 NaN
    assert np.all(res.dt[zero] == 0.0)
    assert np.all(np.isnan(res.a_seg[zero]))

    # 重复节点速度完全一致（同一物理位置）
    assert res.v[2] == res.v[3] == res.v[4]
    assert res.v[6] == res.v[7]

    # 与去掉重复点的路径给出相同的总时间与对应节点速度
    keep = [0, 1, 2, 5, 6, 8]
    s_u = s[keep]
    res_u = run(s=s_u, kappa=kappa[keep], v_max=8.0, a_max=2.0, a_min=-2.0,
                a_lat_max=None, v_start=0.0, v_end=0.0)
    assert np.allclose(res.v[keep], res_u.v)
    assert abs(res.total_time - res_u.total_time) < 1e-10

    # 节点时刻在重复点处不推进
    assert res.times[2] == res.times[3] == res.times[4]


def test_all_duplicate_path_stationary_is_feasible():
    """全部相邻点重合且首尾速度均为 0：静止轨迹可行，耗时 0。"""
    s = np.array([1.0, 1.0, 1.0])
    kappa = np.zeros(3)
    res = run(s=s, kappa=kappa, v_max=5.0, a_max=2.0, a_min=-2.0,
              a_lat_max=None, v_start=0.0, v_end=0.0)
    assert res.feasible
    assert res.total_time == 0.0


def test_duplicate_point_carrying_speed_constraint():
    """重复点中某个点曲率大：约束必须传播到同一位置的全部重复节点。"""
    s = np.array([0.0, 5.0, 10.0, 10.0, 15.0])
    kappa = np.array([0.0, 0.0, 0.0, 2.0, 0.0])  # 第二个重复点有曲率
    res = run(s=s, kappa=kappa, v_max=50.0, a_max=5.0, a_min=-5.0,
              a_lat_max=2.0, v_start=2.0, v_end=2.0)
    assert res.feasible, res.infeasible_reasons
    v_lat = np.sqrt(2.0 / 2.0)  # 1 m/s
    assert res.v[2] <= v_lat + 1e-12
    assert res.v[3] <= v_lat + 1e-12
    assert res.v[2] == res.v[3]
