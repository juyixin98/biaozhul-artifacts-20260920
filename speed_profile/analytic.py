"""解析匀加速案例：构造加速度恒为 ±a 的网格，与解析解逐项对比。

两种案例：

1. :func:`constant_accel_case` —— 静止起步后全程以恒定加速度 ``a`` 加速，
   在恰好达到 v_max 的位置截断网格。解析解：
   ``v(s)=sqrt(2 a s)``，``t(s)=sqrt(2s/a)``。
   离散算法使用与解析模型完全相同的“v² 随 s 线性”递推，因此网格节点上
   理论误差只有机器精度量级。

2. :func:`bang_coast_bang_case` —— 静止起步、+a 加速到 v_max、匀速、
   -d 减速到静止。解析地给出换相点位置与总时间，用于核对
   前/后向扫描交接和时间积分。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .topp import run


@dataclass(frozen=True)
class AnalyticComparison:
    name: str
    s: np.ndarray
    v_numeric: np.ndarray
    v_exact: np.ndarray
    t_numeric: np.ndarray
    t_exact: np.ndarray
    total_time_numeric: float
    total_time_exact: float
    max_v_abs_err: float
    max_t_abs_err: float
    total_time_abs_err: float
    total_time_rel_err: float
    a_seg: np.ndarray


def constant_accel_case(
    a: float = 2.0, v_max: float = 10.0, ds: float = 0.5
) -> AnalyticComparison:
    """全程匀加速（不触顶）：s 取到 v 恰好到达 v_max 处。"""
    s_end = v_max**2 / (2.0 * a)
    n = int(round(s_end / ds))
    s = np.linspace(0.0, s_end, n + 1)  # 保证终点恰好在网格上

    res = run(
        s=s,
        kappa=np.zeros_like(s),
        v_max=v_max,
        a_max=a,
        a_min=-a,
        a_lat_max=None,
        v_start=0.0,
        # 终点不强制静止；给定恰好满足的终点速度
        v_end=v_max,
    )
    v_exact = np.sqrt(2.0 * a * s)
    t_exact = np.sqrt(2.0 * s / a)
    return AnalyticComparison(
        name=f"静止起步恒定加速 a={a:g} m/s² 至 v={v_max:g} m/s",
        s=s,
        v_numeric=res.v,
        v_exact=v_exact,
        t_numeric=res.times,
        t_exact=t_exact,
        total_time_numeric=res.total_time,
        total_time_exact=float(t_exact[-1]),
        max_v_abs_err=float(np.max(np.abs(res.v - v_exact))),
        max_t_abs_err=float(np.max(np.abs(res.times - t_exact))),
        total_time_abs_err=float(abs(res.total_time - t_exact[-1])),
        total_time_rel_err=float(abs(res.total_time - t_exact[-1]) / t_exact[-1]),
        a_seg=res.a_seg,
    )


def bang_coast_bang_case(
    a_acc: float = 2.0,
    a_dec: float = 3.0,
    v_cruise: float = 8.0,
    ds: float = 0.2,
) -> AnalyticComparison:
    """加速—匀速—减速，首尾静止。

    解析换相点：s1 = v²/(2 a_acc)，s2 = s1 + L_cruise，s3 = s2 + v²/(2 a_dec)。
    取 L_cruise 使网格规整：直接在三个换相点之间均匀取点（端点共享）。
    解析时刻按段拼接。
    """
    s1 = v_cruise**2 / (2.0 * a_acc)
    s_dec = v_cruise**2 / (2.0 * a_dec)
    # 匀速段长度取 ds 的整数倍，保证换相点落在网格上
    n_cruise = 50
    s_cruise = n_cruise * ds
    s2 = s1 + s_cruise
    s3 = s2 + s_dec

    n1 = int(round(s1 / ds))
    n3 = int(round(s_dec / ds))
    seg1 = np.linspace(0.0, s1, n1 + 1)
    seg2 = np.linspace(s1, s2, n_cruise + 1)[1:]
    seg3 = np.linspace(s2, s3, n3 + 1)[1:]
    s = np.concatenate([seg1, seg2, seg3])

    res = run(
        s=s,
        kappa=np.zeros_like(s),
        v_max=v_cruise,
        a_max=a_acc,
        a_min=-a_dec,
        a_lat_max=None,
        v_start=0.0,
        v_end=0.0,
    )

    v_exact = np.empty_like(s)
    t_exact = np.empty_like(s)
    # 段 1：0 -> s1，匀加速
    m1 = s <= s1 + 1e-12
    v_exact[m1] = np.sqrt(2.0 * a_acc * s[m1])
    t_exact[m1] = v_exact[m1] / a_acc
    # 段 2：匀速
    t1 = v_cruise / a_acc
    m2 = (s > s1 + 1e-12) & (s <= s2 + 1e-12)
    v_exact[m2] = v_cruise
    t_exact[m2] = t1 + (s[m2] - s1) / v_cruise
    # 段 3：匀减速到 0
    t2 = t1 + s_cruise / v_cruise
    m3 = s > s2 + 1e-12
    v_exact[m3] = np.sqrt(np.maximum(2.0 * a_dec * (s3 - s[m3]), 0.0))
    t_exact[m3] = t2 + (v_cruise - v_exact[m3]) / a_dec

    total_exact = t2 + v_cruise / a_dec

    return AnalyticComparison(
        name=(
            f"加{a_acc:g}—匀速{v_cruise:g}—减{a_dec:g}，首尾静止 "
            f"(s1={s1:g}, s2={s2:g}, s3={s3:g})"
        ),
        s=s,
        v_numeric=res.v,
        v_exact=v_exact,
        t_numeric=res.times,
        t_exact=t_exact,
        total_time_numeric=res.total_time,
        total_time_exact=float(total_exact),
        max_v_abs_err=float(np.max(np.abs(res.v - v_exact))),
        max_t_abs_err=float(np.max(np.abs(res.times - t_exact))),
        total_time_abs_err=float(abs(res.total_time - total_exact)),
        total_time_rel_err=float(abs(res.total_time - total_exact) / total_exact),
        a_seg=res.a_seg,
    )
