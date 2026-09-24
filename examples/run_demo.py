#!/usr/bin/env python3
"""离线回放示例：运行合成场景与解析匀加速案例，打印约束核对结果。

用法：python examples/run_demo.py
不连接硬件，不产生任何图形 / 文件输出。
"""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from speed_profile import scenarios
from speed_profile.analytic import (
    bang_coast_bang_case,
    constant_accel_case,
)
from speed_profile.topp import run


def _fmt(x: float) -> str:
    if not np.isfinite(x):
        return str(x)
    return f"{x:.6g}"


def run_scenario(name: str, sc: dict) -> None:
    description = sc["description"]
    print("=" * 74)
    print(f"场景：{name} —— {description}")
    res = run(
        s=sc["s"],
        kappa=sc["kappa"],
        v_max=sc["v_max"],
        a_max=sc["a_max"],
        a_min=sc["a_min"],
        a_lat_max=sc.get("a_lat_max"),
        v_start=sc["v_start"],
        v_end=sc["v_end"],
    )
    s, v = sc["s"], res.v
    ds = np.diff(s)
    real = ds > 1e-15
    print(f"  采样点 {s.size} 个，其中零长度段 {int(np.sum(~real))} 个")
    print(f"  可行：{res.feasible}" + ("" if res.feasible else f"  原因：{res.infeasible_reasons}"))
    print(
        f"  v 范围 [{_fmt(np.min(v))}, {_fmt(np.max(v))}] m/s，"
        f"v[0]={_fmt(v[0])}，v[-1]={_fmt(v[-1])}"
    )
    print(
        "  段加速度范围："
        f"[{_fmt(np.nanmin(res.a_seg))}, {_fmt(np.nanmax(res.a_seg))}] m/s²，"
        f"限制 [{sc['a_min']:g}, {sc['a_max']:g}]"
    )
    print(f"  速度约束最大超出：{res.max_v_violation:.3e} m/s")
    print(f"  加速度约束最大超出：{res.max_a_violation:.3e} m/s²")
    print(f"  总时间（段积分累加）：{_fmt(res.total_time)} s")
    print(f"  总时间（PCHIP+quad 独立积分复核）：{_fmt(res.total_time_indep_check)} s")

    # 抽查弯道顶点附近速度
    if name == "hairpin":
        kappa = sc["kappa"]
        arc = kappa > 0.0
        v_lat = np.sqrt(sc["a_lat_max"] / np.max(kappa[arc]))
        print(
            f"  弯道限速 sqrt(a_lat/κ_max)={v_lat:.4f} m/s，"
            f"弯道内实际最大速度 {_fmt(np.max(v[arc]))} m/s"
        )


def run_zero_length_demo() -> None:
    """在直线路径中插入相邻重复弧长点，验证零长度段处理。"""
    print("=" * 74)
    print("场景：零长度段（s 含相邻重复点）+ 首尾静止")
    s = np.array([0.0, 5.0, 10.0, 10.0, 10.0, 20.0, 30.0, 30.0, 40.0])
    kappa = np.zeros_like(s)
    res = run(s=s, kappa=kappa, v_max=8.0, a_max=2.0, a_min=-2.0,
              a_lat_max=None, v_start=0.0, v_end=0.0)
    ds = np.diff(s)
    print(f"  s = {s.tolist()}")
    print(f"  各段 Δs = {ds.tolist()}")
    print(f"  零长度段数：{int(np.sum(ds == 0))}，对应 Δt = {res.dt[ds == 0].tolist()}")
    v = res.v
    dup1_equal = abs(v[2] - v[3]) < 1e-15 and abs(v[3] - v[4]) < 1e-15
    dup2_equal = abs(v[6] - v[7]) < 1e-15
    print(f"  重复节点速度是否一致：{dup1_equal and dup2_equal}")
    print(f"  可行：{res.feasible}，总时间 {_fmt(res.total_time)} s")
    print(f"  加速度约束最大超出：{res.max_a_violation:.3e} m/s²")


def run_analytic(cmp_, title: str) -> None:
    print("=" * 74)
    print(f"解析核对：{title}")
    print(f"  {cmp_.name}")
    print(f"  采样点 {cmp_.s.size} 个")
    print(f"  速度最大绝对误差：{cmp_.max_v_abs_err:.3e} m/s")
    print(f"  节点时刻最大绝对误差：{cmp_.max_t_abs_err:.3e} s")
    print(f"  总时间（数值）：{cmp_.total_time_numeric:.12f} s")
    print(f"  总时间（解析）：{cmp_.total_time_exact:.12f} s")
    print(f"  总时间绝对误差：{cmp_.total_time_abs_err:.3e} s，"
          f"相对误差：{cmp_.total_time_rel_err:.3e}")
    finite_a = cmp_.a_seg[np.isfinite(cmp_.a_seg)]
    print(f"  各段加速度取值集合：{np.unique(np.round(finite_a, 10)).tolist()}")


def main() -> int:
    for name in ("straight", "hairpin", "smooth_s_bend"):
        sc = scenarios.get_scenario(name)
        run_scenario(name, sc)

    run_zero_length_demo()

    # v 是 run_zero_length_demo 里的局部变量，这里避免静态检查工具误报
    run_analytic(constant_accel_case(), "恒定加速度（静止起步加速到 v_max）")
    run_analytic(bang_coast_bang_case(), "加速—匀速—减速（首尾静止）")
    print("=" * 74)
    print("全部离线示例运行完毕。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
