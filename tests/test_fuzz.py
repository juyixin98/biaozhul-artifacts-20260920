"""随机化测试：在随机网格 / 随机约束下逐段独立核对全部约束。"""

import numpy as np
import pytest

from speed_profile.topp import run


@pytest.mark.parametrize("seed", range(20))
def test_random_grids_respect_all_constraints(seed):
    rng = np.random.default_rng(seed)

    # 随机非递减弧长：随机正步长，一部分置 0（零长度段）
    n = int(rng.integers(20, 80))
    steps = rng.uniform(0.05, 2.0, size=n - 1)
    steps[rng.random(n - 1) < 0.12] = 0.0
    s = np.concatenate([[0.0], np.cumsum(steps)])
    # 再整体插入若干与前一节点重合的点
    for _ in range(3):
        i = int(rng.integers(1, s.size))
        s = np.insert(s, i, s[i - 1])

    kappa = rng.uniform(-0.5, 0.5, size=s.size)
    kappa[rng.random(s.size) < 0.3] = 0.0  # 部分点为直道
    v_max = float(rng.uniform(3.0, 15.0))
    a_max = float(rng.uniform(1.0, 6.0))
    a_min = float(-rng.uniform(1.0, 6.0))
    a_lat = float(rng.uniform(2.0, 12.0))

    res = run(s=s, kappa=kappa, v_max=v_max, a_max=a_max, a_min=a_min,
              a_lat_max=a_lat, v_start=0.0, v_end=0.0)
    if not res.feasible:
        # 随机配置可能不可行（例如首个弯道限速点太靠近起点）；
        # 只要给出了诊断原因即视为自洽。
        assert res.infeasible_reasons
        return

    v = res.v
    seg_ds = np.diff(s)
    real = seg_ds > 1e-15

    # 纵向加速度逐段核对
    a_seg = np.full(seg_ds.size, np.nan)
    a_seg[real] = (v[1:][real] ** 2 - v[:-1][real] ** 2) / (2.0 * seg_ds[real])
    assert np.nanmax(a_seg) <= a_max + 1e-8
    assert np.nanmin(a_seg) >= a_min - 1e-8

    # 速度上限（v_max 与横向限制）
    cap = np.full(s.size, v_max, dtype=float)
    curved = kappa != 0.0
    cap[curved] = np.minimum(cap[curved], np.sqrt(a_lat / np.abs(kappa[curved])))
    assert np.all(v <= cap + 1e-8)
    assert v[0] == 0.0 and v[-1] == 0.0

    # 时间：正长度段严格正，零长度段为 0；零长度段加速度为 NaN
    assert np.all(res.dt[real] > 0.0)
    assert np.all(res.dt[~real] == 0.0)
    assert np.all(np.isnan(res.a_seg[~real]))

    # 时间最优性：每个内部点的 v 必须贴着
    # {速度上限、前向包络、后向包络} 三者之一，否则扫描取 min 有 bug。
    vf, vb = res.v_forward, res.v_backward
    binding = (
        np.isclose(v, np.minimum.reduce([cap, vf, vb]), rtol=1e-9, atol=1e-10)
    )
    assert np.all(binding)
