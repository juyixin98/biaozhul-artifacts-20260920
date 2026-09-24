"""解析匀加速案例误差核对。

离散模型使用与解析案例相同的 v²-随-s-线性递推，网格节点上的误差应
只有机器精度量级；段时间解析公式同理。
"""

import numpy as np

from speed_profile.analytic import (
    bang_coast_bang_case,
    constant_accel_case,
)
from speed_profile.topp import run


def test_constant_acceleration_matches_analytic():
    cmp_ = constant_accel_case(a=2.0, v_max=10.0, ds=0.5)
    # 节点速度 / 时刻：机器精度量级
    assert cmp_.max_v_abs_err < 1e-12
    assert cmp_.max_t_abs_err < 1e-11
    assert cmp_.total_time_rel_err < 1e-12

    # 解析总时间 t = v/a = 5 s
    assert abs(cmp_.total_time_exact - 5.0) < 1e-12

    # 每个真实段的加速度恒为 a（除网格端点浮点误差外严格成立）
    a_seg = cmp_.a_seg[np.isfinite(cmp_.a_seg)]
    assert np.allclose(a_seg, 2.0, atol=1e-10, rtol=1e-12)


def test_constant_acceleration_fine_grid():
    for ds in (1.0, 0.25, 0.01):
        cmp_ = constant_accel_case(a=3.0, v_max=12.0, ds=ds)
        assert cmp_.max_v_abs_err < 1e-11
        assert cmp_.total_time_rel_err < 1e-12


def test_bang_coast_bang_matches_analytic():
    cmp_ = bang_coast_bang_case(a_acc=2.0, a_dec=3.0, v_cruise=8.0, ds=0.2)
    # 换相点严格在网格上，离散递推与解析解一致
    assert cmp_.max_v_abs_err < 1e-11
    assert cmp_.max_t_abs_err < 1e-10
    assert cmp_.total_time_rel_err < 1e-11

    a_seg = cmp_.a_seg[np.isfinite(cmp_.a_seg)]
    uniq = np.unique(np.round(a_seg, 8))
    assert set(uniq.tolist()) <= {2.0, 0.0, -3.0}

    # 解析总时间：4 + 10/8 + 8/3 = 7.9166... s（匀速段 10 m）
    t_expect = 8.0 / 2.0 + 10.0 / 8.0 + 8.0 / 3.0
    assert abs(cmp_.total_time_exact - t_expect) < 1e-12


def test_trapezoid_independent_cross_check_on_analytic():
    """在 v>0 的内部区间，梯形积分 1/v 与解析时间的偏差应随网格收敛。

    对 sqrt 型速度曲线，梯形积分在网格足够细时误差很小（这里只要求
    一个保守上界，验证“独立复核”确实在工作）。
    """
    a, v_max, ds = 2.0, 10.0, 0.02
    s_end = v_max**2 / (2.0 * a)
    n = int(round(s_end / ds))
    s = np.linspace(0.0, s_end, n + 1)
    res = run(s=s, kappa=np.zeros_like(s), v_max=v_max, a_max=a, a_min=-a,
              a_lat_max=None, v_start=0.0, v_end=v_max)
    # 首段（静止起步）用精确段时间补齐；内部区间独立梯形积分
    interior_t = np.trapezoid(1.0 / res.v[1:], s[1:])
    exact_interior = np.sqrt(2.0 * s[1:] / a)[-1] - np.sqrt(2.0 * s[1] / a)
    assert abs(interior_t - exact_interior) / exact_interior < 1e-3
    assert np.isfinite(res.total_time_indep_check)
