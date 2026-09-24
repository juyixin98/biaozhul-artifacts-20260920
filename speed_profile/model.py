"""输入输出数据模型与参数校验。

只依赖 numpy / pydantic；pydantic 仅在 API 边界（api.py）使用，
核心算法只消费 :class:`ParameterizationInput` 中的 numpy 数组。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

DEFAULT_A_MIN = -10.0
DEFAULT_A_MAX = 10.0
DEFAULT_A_LAT_MAX = 10.0
DEFAULT_V_MAX = 10.0
DEFAULT_V_START = 0.0
DEFAULT_V_END = 0.0


@dataclass(frozen=True)
class ParameterizationInput:
    """一次离线参数化的全部输入。

    Attributes
    ----------
    s:
        弧长采样点（米），必须非递减，允许相邻相等（零长度段）。
    kappa:
        各采样点曲率（1/米），与 s 等长。
    v_max:
        各点速度上限（米/秒）；标量或与 s 等长的数组。
    a_min, a_max:
        纵向（沿路径切向）加速度区间（米/秒^2），a_min < 0 < a_max。
    a_lat_max:
        横向（向心）加速度上限（米/秒^2）。``None`` 表示只使用 v_max。
    v_start, v_end:
        首、尾节点速度（通常为 0，表示静止起步 / 静止结束）。
    """

    s: np.ndarray
    kappa: np.ndarray
    v_max: np.ndarray
    a_min: float = DEFAULT_A_MIN
    a_max: float = DEFAULT_A_MAX
    a_lat_max: float | None = DEFAULT_A_LAT_MAX
    v_start: float = DEFAULT_V_START
    v_end: float = DEFAULT_V_END


@dataclass(frozen=True)
class ParameterizationResult:
    """参数化结果。所有逐点数组长度为 N，逐段数组长度为 N-1。"""

    v: np.ndarray          # 各节点速度 m/s
    v_cap: np.ndarray      # 各点速度上限（含横向加速度与首尾约束）m/s
    v_forward: np.ndarray  # 前向扫描得到的速度包络
    v_backward: np.ndarray # 后向扫描得到的速度包络
    times: np.ndarray      # 各节点相对时刻 t[0]=0
    dt: np.ndarray         # 各段时间 Δt_i（N-1）
    a_seg: np.ndarray      # 各段纵向加速度；零长度段为 NaN
    total_time: float      # 末节点时刻
    total_time_indep_check: float  # PCHIP+quad 独立积分复核的总时间
    feasible: bool
    infeasible_reasons: list[str]
    max_v_violation: float
    max_a_violation: float


def _as_array(name: str, value, n: int) -> np.ndarray:
    arr = np.asarray(value, dtype=float).reshape(-1)
    if arr.shape[0] == 1:
        arr = np.full(n, float(arr[0]))
    if arr.shape != (n,):
        raise ValueError(f"{name} 长度应为 {n}（或标量），实际为 {arr.shape[0]}")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} 含有 NaN 或 Inf")
    return arr


def validate_input(
    s,
    kappa,
    v_max=DEFAULT_V_MAX,
    a_min: float = DEFAULT_A_MIN,
    a_max: float = DEFAULT_A_MAX,
    a_lat_max: float | None = DEFAULT_A_LAT_MAX,
    v_start: float = DEFAULT_V_START,
    v_end: float = DEFAULT_V_END,
) -> ParameterizationInput:
    """构造并校验输入，非法输入抛出 ``ValueError``。"""
    s_arr = np.asarray(s, dtype=float).reshape(-1)
    kappa_arr = np.asarray(kappa, dtype=float).reshape(-1)

    if s_arr.size < 2:
        raise ValueError("至少需要 2 个采样点")
    if s_arr.shape != kappa_arr.shape:
        raise ValueError(
            f"s 与 kappa 长度必须相同：{s_arr.shape[0]} != {kappa_arr.shape[0]}"
        )
    if not np.all(np.isfinite(s_arr)):
        raise ValueError("s 含有 NaN 或 Inf")
    if not np.all(np.isfinite(kappa_arr)):
        raise ValueError("kappa 含有 NaN 或 Inf")
    if np.any(np.diff(s_arr) < 0):
        raise ValueError("s 必须非递减（允许相邻相等，即零长度段），但不能倒退")

    n = s_arr.size
    v_max_arr = _as_array("v_max", v_max, n)
    if np.any(v_max_arr < 0):
        raise ValueError("v_max 必须非负")

    if not np.isfinite(a_min) or not np.isfinite(a_max):
        raise ValueError("a_min / a_max 必须为有限值")
    if a_min >= a_max:
        raise ValueError(f"需要 a_min < a_max，实际 a_min={a_min}, a_max={a_max}")

    if a_lat_max is not None:
        if not np.isfinite(a_lat_max) or a_lat_max <= 0:
            raise ValueError("a_lat_max 必须为正有限值，或为 None")

    for name, val in (("v_start", v_start), ("v_end", v_end)):
        if not np.isfinite(val) or val < 0:
            raise ValueError(f"{name} 必须为非负有限值")

    return ParameterizationInput(
        s=s_arr,
        kappa=kappa_arr,
        v_max=v_max_arr,
        a_min=float(a_min),
        a_max=float(a_max),
        a_lat_max=(None if a_lat_max is None else float(a_lat_max)),
        v_start=float(v_start),
        v_end=float(v_end),
    )
