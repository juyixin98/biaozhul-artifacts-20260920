"""合成路径场景（全部离线生成，无真实硬件、无回放文件依赖）。

每个场景返回一个与 API 请求体一致的 dict，可直接喂给
:func:`speed_profile.topp.run`。
"""

from __future__ import annotations

import numpy as np


def straight_line(
    length: float = 100.0,
    n: int = 201,
    v_max: float = 10.0,
    a_max: float = 2.0,
    a_min: float = -2.0,
    v_start: float = 0.0,
    v_end: float = 0.0,
) -> dict:
    """直线：κ 恒为 0，横向约束不生效，典型加速—匀速—减速梯形剖面。"""
    s = np.linspace(0.0, length, n)
    kappa = np.zeros_like(s)
    return dict(
        s=s,
        kappa=kappa,
        v_max=v_max,
        a_max=a_max,
        a_min=a_min,
        a_lat_max=10.0,
        v_start=v_start,
        v_end=v_end,
        description="直线（首尾静止，梯形速度剖面）",
    )


def hairpin(
    radius: float = 4.0,
    straight_len: float = 20.0,
    ds: float = 0.1,
    v_max: float = 10.0,
    a_max: float = 3.0,
    a_min: float = -4.0,
    a_lat_max: float = 4.0,
) -> dict:
    """急弯：两段直道之间夹一个半圆发卡弯（κ=1/R，必须大幅减速）。

    弧长布局：直线 [0, L] + 半圆弧 [L, L+πR] + 直线（再走 L）。
    各段长度吸附到 ds 的整数倍，段间共享端点，保证整体严格非递减。
    """
    n_straight = max(1, int(round(straight_len / ds)))
    n_arc = max(1, int(round(np.pi * radius / ds)))
    l_straight = n_straight * ds
    arc_len = n_arc * ds
    end_arc = l_straight + arc_len

    seg1 = np.linspace(0.0, l_straight, n_straight + 1)
    seg2 = np.linspace(l_straight, end_arc, n_arc + 1)[1:]
    seg3 = np.linspace(end_arc, end_arc + l_straight, n_straight + 1)[1:]
    s = np.concatenate([seg1, seg2, seg3])

    kappa = np.zeros_like(s)
    in_arc = (s >= l_straight - 0.5 * ds) & (s <= end_arc + 0.5 * ds)
    kappa[in_arc] = 1.0 / radius

    return dict(
        s=s,
        kappa=kappa,
        v_max=v_max,
        a_max=a_max,
        a_min=a_min,
        a_lat_max=a_lat_max,
        v_start=0.0,
        v_end=0.0,
        description=(
            f"急弯：半径 {radius:g} m 的半圆发卡弯，"
            f"弯道限速 sqrt(a_lat*R)={np.sqrt(a_lat_max * radius):.3f} m/s"
        ),
    )


def smooth_s_bend(
    length: float = 80.0,
    n: int = 401,
    v_max: float = 8.0,
    a_max: float = 2.0,
    a_min: float = -2.0,
    a_lat_max: float = 4.0,
) -> dict:
    """S 形缓弯：曲率光滑变号，速度上限随之平滑起伏（PCHIP 生成）。"""
    from scipy.interpolate import PchipInterpolator

    s_knots = np.array([0.0, 10.0, 25.0, 40.0, 55.0, 70.0, 80.0])
    k_knots = np.array([0.0, 0.08, 0.0, -0.10, 0.0, 0.05, 0.0])
    s = np.linspace(0.0, length, n)
    kappa = PchipInterpolator(s_knots, k_knots)(s)
    return dict(
        s=s,
        kappa=kappa,
        v_max=v_max,
        a_max=a_max,
        a_min=a_min,
        a_lat_max=a_lat_max,
        v_start=0.0,
        v_end=0.0,
        description="S 形缓弯（曲率光滑变号，PCHIP 重采样）",
    )


SCENARIOS = {
    "straight": straight_line,
    "hairpin": hairpin,
    "smooth_s_bend": smooth_s_bend,
}


def get_scenario(name: str) -> dict:
    try:
        return SCENARIOS[name]()
    except KeyError:
        raise KeyError(f"未知场景 {name!r}，可选：{sorted(SCENARIOS)}") from None
