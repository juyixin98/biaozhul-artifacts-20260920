"""离线路径时间参数化（速度约束路径参数化）。

离散网格上以“段内匀加速”为运动模型：

    v_{i+1}^2 - v_i^2 = 2 a_seg_i * Δs_i,   a_seg_i ∈ [a_min, a_max]

速度上限同时包含给定的 v_max 与横向加速度限制
``v <= sqrt(a_lat_max / |κ|)``。

先做前向扫描（受 a_max 限制，由起点向终点传播可达速度），再做后向扫描
（受 -a_min 的减速能力限制，由终点向起点传播），取两者与速度上限的最小值
作为最终速度剖面，然后积分时间并逐项复核约束。
"""

from __future__ import annotations

import warnings

import numpy as np
from scipy.integrate import quad
from scipy.interpolate import PchipInterpolator

from .model import ParameterizationInput, ParameterizationResult, validate_input

# 复核约束时允许的相对 / 绝对容差（离散模型本身精确，余量只给浮点误差）
CHECK_RTOL = 1e-9
CHECK_ATOL = 1e-9
# 判定“正长度段上速度为 0”等不可行情况的阈值
ZERO_SPEED_TOL = 1e-12
MIN_DS = 1e-15


def velocity_cap(inp: ParameterizationInput) -> np.ndarray:
    """各节点的速度上限：v_max、横向加速度限制以及首尾速度的交集。"""
    cap = inp.v_max.copy()
    if inp.a_lat_max is not None:
        kappa_abs = np.abs(inp.kappa)
        nonzero = kappa_abs > 0.0
        lat_limit = np.full_like(cap, np.inf)
        lat_limit[nonzero] = np.sqrt(inp.a_lat_max / kappa_abs[nonzero])
        cap = np.minimum(cap, lat_limit)
    cap[0] = min(cap[0], inp.v_start)
    cap[-1] = min(cap[-1], inp.v_end)
    return cap


def _forward_sweep(cap: np.ndarray, ds: np.ndarray, a_max: float) -> np.ndarray:
    """前向扫描：v[i] 受上一段全力加速能力限制。"""
    n = cap.size
    v = np.empty(n)
    v[0] = cap[0]
    two_a_ds = 2.0 * a_max * ds  # ds 为 0 时该项为 0，速度原样传播
    for i in range(1, n):
        reachable_sq = v[i - 1] * v[i - 1] + two_a_ds[i - 1]
        v[i] = min(cap[i], np.sqrt(max(reachable_sq, 0.0)))
    return v


def _backward_sweep(cap: np.ndarray, ds: np.ndarray, a_min: float) -> np.ndarray:
    """后向扫描：从终点回看，v[i] 受下一段全力减速能力限制。"""
    n = cap.size
    v = np.empty(n)
    v[-1] = cap[-1]
    two_d_dec_ds = -2.0 * a_min * ds  # 减速度大小 -a_min
    for i in range(n - 2, -1, -1):
        reachable_sq = v[i + 1] * v[i + 1] + two_d_dec_ds[i]
        v[i] = min(cap[i], np.sqrt(max(reachable_sq, 0.0)))
    return v


def _segment_times(s: np.ndarray, v: np.ndarray) -> np.ndarray:
    """按段内匀加速解析积分各段时间。

    恒定加速度下 v(s) 线性于 s 的平方关系，可直接得到
    ``Δt = 2Δs / (v_i + v_{i+1})``；零长度段 Δt=0；
    理论上不可行（正长度但两端速度都为 0）的段记 inf。
    """
    ds = np.diff(s)
    vsum = v[:-1] + v[1:]
    dt = np.zeros_like(ds)
    positive_ds = ds > MIN_DS
    moving = positive_ds & (vsum > 0.0)
    dt[moving] = 2.0 * ds[moving] / vsum[moving]
    stalled = positive_ds & (vsum <= 0.0)
    dt[stalled] = np.inf
    return dt


def _independent_total_time(s: np.ndarray, v: np.ndarray, dt: np.ndarray) -> float:
    """独立复核总时间（与主递推完全不同的一条计算路径）。

    主算法用闭式段时间 ``2Δs/(v_i+v_{i+1})``（段内 v² 随 s 线性）。
    这里改为：对离散速度做 **PCHIP 光滑插值** 得到连续 v̂(s)，再用
    ``scipy.integrate.quad`` 自适应求积计算 ``∫ 1/v̂ ds``。光滑插值
    不是逐段线性函数，因此不会与闭式段时间在代数上恒等——它度量的
    是离散剖面相对连续解的逼近误差，网格越细两者越接近。

    首尾静止时 1/v 在端点呈 s^{-1/2} 奇异，quad 不直接处理；首尾各
    两个段采用闭式段时间（恒加速模型下精确），只对内部速度严格为正
    的区间做 PCHIP + quad。存在无法通行的正长度段时返回 inf。
    """
    ds = np.diff(s)
    real = ds > MIN_DS
    stalled = real & (v[:-1] <= ZERO_SPEED_TOL) & (v[1:] <= ZERO_SPEED_TOL)
    if np.any(stalled):
        return float("inf")

    lo, hi = 0, s.size - 1
    edge_time = 0.0
    if v[0] <= ZERO_SPEED_TOL:
        edge_time += float(dt[0] + dt[1])
        lo = 2
    if v[-1] <= ZERO_SPEED_TOL:
        edge_time += float(dt[-1] + dt[-2])
        hi = s.size - 3
    if hi - lo < 2:
        if not np.all(np.isfinite(dt)):
            return float("inf")
        return float(np.sum(dt))
    if np.any(v[lo:hi + 1] <= 0.0):
        return float("inf")

    s_in = s[lo:hi + 1]
    v_slice = v[lo:hi + 1]
    # PCHIP 要求 x 严格递增：合并零长度段对应的重复位置。
    # 同一物理位置上的重复节点速度在主算法中已被统一（取 min），
    # 这里按组取最小速度作为该位置的代表值。
    s_grp, group = np.unique(s_in, return_inverse=True)
    v_grp = np.full(s_grp.size, np.inf)
    np.minimum.at(v_grp, group, v_slice)
    s_in, v_in = s_grp, v_grp
    if s_in.size < 2:
        if not np.all(np.isfinite(dt)):
            return float("inf")
        return float(np.sum(dt))

    v_smooth = PchipInterpolator(s_in, v_in)

    def integrand(x: float) -> float:
        val = float(v_smooth(x))
        return 1.0 / val if val > 0.0 else 0.0

    # 速度包络在约束交接处有折点（v² 随 s 分段线性），1/v 相应分段光滑。
    # 节点较多时 quad 的断点数量受 limit 限制，因此按节点块手工分段
    # 自适应积分（每块足够光滑），再求和。
    block = 100
    t_interior = 0.0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=UserWarning)
        for start in range(0, s_in.size - 1, block):
            seg_b = s_in[start:min(start + block + 1, s_in.size)]
            val, _ = quad(
                integrand,
                float(seg_b[0]),
                float(seg_b[-1]),
                limit=200,
                epsabs=1e-11,
                epsrel=1e-11,
                points=list(seg_b[1:-1]),
            )
            t_interior += val
    return edge_time + float(t_interior)


def _check_constraints(
    inp: ParameterizationInput,
    v: np.ndarray,
    cap: np.ndarray,
    a_seg: np.ndarray,
) -> tuple[float, float]:
    """逐段复核速度与纵向加速度约束，返回各自的最大超出量。"""
    # 速度上限：用 envelope 本身构造时 v <= cap 恒成立，这里仍独立核对一遍
    v_excess = np.maximum(v - cap - CHECK_ATOL - CHECK_RTOL * np.maximum(cap, 0.0), 0.0)
    max_v_violation = float(np.max(v_excess)) if v_excess.size else 0.0

    # 段加速度（零长度段为 NaN，不参与复核）
    over = np.maximum(
        a_seg - inp.a_max - CHECK_ATOL - CHECK_RTOL * abs(inp.a_max), 0.0
    )
    under = np.maximum(
        inp.a_min - a_seg - CHECK_ATOL - CHECK_RTOL * abs(inp.a_min), 0.0
    )
    over = np.where(np.isnan(over), 0.0, over)
    under = np.where(np.isnan(under), 0.0, under)
    return max_v_violation, float(max(np.max(over), np.max(under)))


def _segment_accelerations(s: np.ndarray, v: np.ndarray) -> np.ndarray:
    """各段纵向加速度 ``(v_{i+1}²-v_i²)/(2Δs)``；零长度段为 NaN。"""
    ds = np.diff(s)
    a_seg = np.full(ds.size, np.nan)
    real_seg = ds > MIN_DS
    a_seg[real_seg] = (v[1:][real_seg] ** 2 - v[:-1][real_seg] ** 2) / (
        2.0 * ds[real_seg]
    )
    return a_seg


def parameterize(inp: ParameterizationInput) -> ParameterizationResult:
    """执行完整的前后向扫描参数化。"""
    cap = velocity_cap(inp)
    ds = np.diff(inp.s)

    vf = _forward_sweep(cap, ds, inp.a_max)
    vb = _backward_sweep(cap, ds, inp.a_min)
    v = np.minimum(vf, vb)

    dt = _segment_times(inp.s, v)
    times = np.concatenate(([0.0], np.cumsum(dt)))
    total_time = float(times[-1])

    # 段加速度（零长度段为 NaN）
    a_seg = _segment_accelerations(inp.s, v)
    real_seg = ds > MIN_DS

    max_v_violation, max_a_violation = _check_constraints(inp, v, cap, a_seg)

    # ---- 不可行性诊断 -------------------------------------------------
    reasons: list[str] = []
    if abs(v[0] - inp.v_start) > CHECK_ATOL + CHECK_RTOL * max(inp.v_start, 1.0):
        reasons.append(
            f"起点速度不可达：要求 v_start={inp.v_start:g}，"
            f"但起点速度上限为 {cap[0]:g}"
        )
    if abs(v[-1] - inp.v_end) > CHECK_ATOL + CHECK_RTOL * max(inp.v_end, 1.0):
        reasons.append(
            f"终点速度不可达：要求 v_end={inp.v_end:g}，"
            f"但终点速度上限为 {cap[-1]:g}"
        )
    stalled = real_seg & ((v[:-1] <= ZERO_SPEED_TOL) & (v[1:] <= ZERO_SPEED_TOL))
    if np.any(stalled):
        idx = int(np.argmax(stalled))
        reasons.append(
            f"第 {idx} 段（s={inp.s[idx]:g}~{inp.s[idx + 1]:g}）为正长度段，"
            "但两端可行速度均为 0，轨迹在该处无法通行（加速度 / 速度上限矛盾）"
        )
    if max_v_violation > 0.0 or max_a_violation > 0.0:
        reasons.append(
            f"数值约束超出：速度 {max_v_violation:.3e}，加速度 {max_a_violation:.3e}"
        )

    indep_time = _independent_total_time(inp.s, v, dt)

    return ParameterizationResult(
        v=v,
        v_cap=cap,
        v_forward=vf,
        v_backward=vb,
        times=times,
        dt=dt,
        a_seg=a_seg,
        total_time=total_time,
        total_time_indep_check=indep_time,
        feasible=not reasons,
        infeasible_reasons=reasons,
        max_v_violation=max_v_violation,
        max_a_violation=max_a_violation,
    )


def run(
    s,
    kappa,
    v_max=10.0,
    a_min: float = -10.0,
    a_max: float = 10.0,
    a_lat_max: float | None = 10.0,
    v_start: float = 0.0,
    v_end: float = 0.0,
) -> ParameterizationResult:
    """便捷入口：校验输入并执行参数化。"""
    return parameterize(
        validate_input(
            s=s,
            kappa=kappa,
            v_max=v_max,
            a_min=a_min,
            a_max=a_max,
            a_lat_max=a_lat_max,
            v_start=v_start,
            v_end=v_end,
        )
    )
