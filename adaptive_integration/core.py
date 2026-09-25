"""自适应求积驱动：Gauss-Kronrod 7-15（全局误差预算）与自适应 Simpson（递归半预算）。

约定：两个驱动都只处理 a<b 的正向区间；反向区间由 API 层翻号。

设计要点
--------
* 误差预算递归分配：
  - GK15 使用全局大顶堆（始终细分误差估计最大的子区间），总误差估计
    与总预算 abs_tol + rel_tol*|I| 比较；
  - Simpson 把局部容差随二分对半下传；多个初始面板均分全局预算。
* 深度受限：max_depth 限制单子区间连续二分次数。
* 预算受限：max_evaluations 是求值次数硬上限。
* 不静默给值：非有限节点值、深度/预算耗尽、误差不再下降均返回失败，
  失败结果里的 result 字段显式标注为不可靠近似。
"""

from __future__ import annotations

import heapq
from typing import Any, Callable, Optional

import numpy as np

from .config import IntegrationConfig
from .result import (
    IntegrationResult,
    STATUS_CONVERGED,
    STATUS_FAILED,
    ERROR_MESSAGES,
)
from .rules import gk15_rule


class NonFiniteValueError(Exception):
    """被积函数在某节点返回 inf/NaN。"""

    def __init__(self, x: float):
        self.x = x
        super().__init__(f"非有限函数值出现在 x={x:g}")


class EvaluatedFunction:
    """统计求值次数、统一检测非有限返回值的被积函数包装。"""

    def __init__(self, f: Callable[[Any], Any]):
        self.f = f
        self.count = 0

    def __call__(self, x):
        x_arr = np.asarray(x, dtype=float)
        y = np.broadcast_to(
            np.asarray(self.f(x_arr), dtype=float), x_arr.shape
        )
        self.count += int(x_arr.size)
        if not np.all(np.isfinite(y)):
            bad = x_arr[~np.isfinite(y)]
            raise NonFiniteValueError(float(bad.flat[0]))
        return np.array(y, dtype=float)


def _failed(
    method: str,
    code: str,
    detail: str,
    value: float,
    err: float,
    evals: int,
    depth: int,
    n_intervals: int,
    warnings: list[str],
    extra: Optional[dict] = None,
) -> IntegrationResult:
    diag: dict[str, Any] = {
        "best_estimate_unreliable": value,
        "current_error_estimate": err,
        "intervals_used": n_intervals,
    }
    if extra:
        diag.update(extra)
    return IntegrationResult(
        status=STATUS_FAILED,
        value=value,
        error_estimate=err,
        error_code=code,
        error_message=f"{ERROR_MESSAGES[code]}（{detail}）",
        method=method,
        evaluations=evals,
        depth_reached=depth,
        intervals=n_intervals,
        warnings=list(warnings),
        diagnostics=diag,
    )


def _initial_grid(a: float, b: float, cfg: IntegrationConfig) -> list[tuple[float, float]]:
    """由初始均匀分段数与额外分点构造升序初始子区间（要求 a<b）。"""

    edges = {a + (b - a) * k / cfg.initial_intervals
             for k in range(cfg.initial_intervals + 1)}
    if cfg.points:
        edges.update(float(p) for p in cfg.points)
    edges = sorted(edges)
    return list(zip(edges[:-1], edges[1:]))


# ===========================================================================
# Gauss-Kronrod 7-15 全局自适应
# ===========================================================================

def gk15_adaptive(
    func: Callable[[Any], Any],
    a: float,
    b: float,
    cfg: IntegrationConfig,
) -> IntegrationResult:
    method = "gk15"
    f = EvaluatedFunction(func)
    warnings: list[str] = []

    intervals0 = _initial_grid(a, b, cfg)

    # 预算连初始网格都无法覆盖
    if 15 * len(intervals0) > cfg.max_evaluations:
        return _failed(
            method,
            "EVALUATION_BUDGET_EXHAUSTED",
            f"初始网格需要 {15 * len(intervals0)} 次求值，"
            f"超过上限 {cfg.max_evaluations}",
            0.0,
            float("nan"),
            0,
            0,
            0,
            warnings,
        )

    heap: list = []
    total_val = 0.0
    total_err = 0.0
    max_depth = 0
    floor_intervals = 0
    endpoint_deep_splits = 0

    try:
        for ia, ib in intervals0:
            r, e, _rabs, _rasc, floor_hit = gk15_rule(f, ia, ib)
            total_val += r
            total_err += e
            floor_intervals += int(floor_hit)
            # 堆项：(-误差, 序号, a, b, depth, 积分值)
            heapq.heappush(heap, (-e, len(heap), ia, ib, 0, r))
    except NonFiniteValueError as ex:
        return _failed(
            method, "INVALID_VALUE_AT_POINT", f"x={ex.x:g}",
            total_val, total_err, f.count, max_depth,
            len(heap), warnings,
            {"location": ex.x},
        )

    n_intervals = len(heap)

    def _target() -> float:
        return cfg.abs_tol + cfg.rel_tol * abs(total_val)

    while total_err > _target():
        neg_err, seq, ia, ib, depth, old_r = heapq.heappop(heap)
        old_err = -neg_err
        mid = 0.5 * (ia + ib)

        if depth >= cfg.max_depth:
            return _failed(
                method,
                "DEPTH_LIMIT_REACHED",
                f"最差子区间 [{ia:.12g}, {ib:.12g}]（深度 {depth}）"
                f"误差 {old_err:.3e}，总误差 {total_err:.3e}，"
                f"目标 {_target():.3e}",
                total_val,
                total_err,
                f.count,
                max_depth,
                n_intervals,
                warnings,
                {"worst_interval": [ia, ib],
                 "worst_interval_error": old_err},
            )

        # 每次细分两个子区间，各 15 个新节点
        if f.count + 30 > cfg.max_evaluations:
            heapq.heappush(heap, (-old_err, seq, ia, ib, depth, old_r))
            return _failed(
                method,
                "EVALUATION_BUDGET_EXHAUSTED",
                f"细分 [{ia:.12g}, {ib:.12g}] 前已用 {f.count} 次求值，"
                f"上限 {cfg.max_evaluations}；总误差 {total_err:.3e}，"
                f"目标 {_target():.3e}",
                total_val,
                total_err,
                f.count,
                max_depth,
                n_intervals,
                warnings,
                {"worst_interval": [ia, ib]},
            )

        try:
            rl, el, _, _, fl = gk15_rule(f, ia, mid)
            rr, er, _, _, fr = gk15_rule(f, mid, ib)
        except NonFiniteValueError as ex:
            return _failed(
                method, "INVALID_VALUE_AT_POINT", f"x={ex.x:g}",
                total_val, total_err, f.count, max(depth + 1, max_depth),
                n_intervals, warnings,
                {"location": ex.x, "parent_interval": [ia, ib]},
            )

        child_err = el + er
        # 舍入平台检测：足够深时最差区间误差完全不下降 -> 奇点/舍入失败
        if depth >= 15 and child_err >= old_err:
            return _failed(
                method,
                "ROUND_OFF_NO_PROGRESS",
                f"子区间 [{ia:.12g}, {ib:.12g}]（深度 {depth}）"
                f"细分后误差 {child_err:.3e} 未小于细分前 {old_err:.3e}",
                total_val - old_r + rl + rr,
                total_err - old_err + child_err,
                f.count,
                max(depth + 1, max_depth),
                n_intervals + 1,
                warnings,
                {"worst_interval": [ia, ib]},
            )

        total_val += rl + rr - old_r
        total_err += child_err - old_err
        floor_intervals += int(fl) + int(fr)
        new_depth = depth + 1
        max_depth = max(max_depth, new_depth)
        if new_depth >= 20 and (ia == a or ib == b):
            endpoint_deep_splits += 1

        heapq.heappush(heap, (-el, seq * 2, ia, mid, new_depth, rl))
        heapq.heappush(heap, (-er, seq * 2 + 1, mid, ib, new_depth, rr))
        n_intervals += 1

    if floor_intervals > 0:
        warnings.append(
            f"{floor_intervals} 个子区间的误差估计触及浮点舍入底噪"
            "（50*eps*积分|f|），绝对误差下界可能偏乐观"
        )
    if endpoint_deep_splits > 0:
        warnings.append(
            "端点附近子区间被细分到深度 20 以上才达标：被积函数可能"
            "含端点弱奇异性，结果在请求容差内但可靠性降低"
        )

    return IntegrationResult(
        status=STATUS_CONVERGED,
        value=total_val,
        error_estimate=total_err,
        method=method,
        evaluations=f.count,
        depth_reached=max_depth,
        intervals=n_intervals,
        warnings=warnings,
    )


# ===========================================================================
# 自适应 Simpson（局部误差预算递归对半）
# ===========================================================================

def simpson_adaptive(
    func: Callable[[Any], Any],
    a: float,
    b: float,
    cfg: IntegrationConfig,
) -> IntegrationResult:
    method = "simpson"
    f = EvaluatedFunction(func)
    warnings: list[str] = []
    total_intervals = 0
    max_depth = 0
    leaf_err_sum = 0.0  # 各接受叶节点实现误差估计 |S2-S1|/15 之和

    class _GiveUp(Exception):
        def __init__(self, code, detail, extra=None):
            self.code = code
            self.detail = detail
            self.extra = extra or {}

    def _recurse(sa, sb, fa, fc, fb, s_whole, eps1, depth):
        nonlocal total_intervals, max_depth, leaf_err_sum
        max_depth = max(max_depth, depth)
        m_l = sa + 0.25 * (sb - sa)
        m_r = sb - 0.25 * (sb - sa)

        if f.count + 2 > cfg.max_evaluations:
            raise _GiveUp(
                "EVALUATION_BUDGET_EXHAUSTED",
                f"已用 {f.count} 次求值，上限 {cfg.max_evaluations}",
            )
        try:
            f_l = f(m_l)
            f_r = f(m_r)
        except NonFiniteValueError as ex:
            raise _GiveUp(
                "INVALID_VALUE_AT_POINT",
                f"x={ex.x:g}",
                {"location": ex.x, "parent_interval": [sa, sb]},
            )

        h = sb - sa
        # 两个半步 Simpson 面板：半宽 h/2，系数 (h/2)/6 = h/12
        s_left = (h / 12.0) * (fa + 4.0 * f_l + fc)
        s_right = (h / 12.0) * (fc + 4.0 * f_r + fb)
        s2 = s_left + s_right
        diff = s2 - s_whole
        err12 = abs(diff) / 15.0

        if err12 <= eps1:
            total_intervals += 2
            leaf_err_sum += err12
            return s2 + diff / 15.0  # Richardson 外推

        if depth >= cfg.max_depth:
            raise _GiveUp(
                "DEPTH_LIMIT_REACHED",
                f"子区间 [{sa:.12g}, {sb:.12g}]（深度 {depth}）"
                f"误差 {err12:.3e} > 局部容差 {eps1:.3e}",
                {"worst_interval": [sa, sb],
                 "worst_interval_error": err12},
            )

        mid = 0.5 * (sa + sb)
        half_eps = eps1 / 2.0  # 局部误差预算对半下传
        return (
            _recurse(sa, mid, fa, f_l, fc, s_left, half_eps, depth + 1)
            + _recurse(mid, sb, fc, f_r, fb, s_right, half_eps, depth + 1)
        )

    panels = _initial_grid(a, b, cfg)
    edges = [p[0] for p in panels] + [panels[-1][1]]
    # 初始需要：所有边点（相邻面板复用）+ 每个面板中点
    if f.count + len(edges) + len(panels) > cfg.max_evaluations:
        return _failed(
            method, "EVALUATION_BUDGET_EXHAUSTED",
            f"初始 Simpson 网格需要 {len(edges) + len(panels)} 次求值，"
            f"超过上限 {cfg.max_evaluations}",
            0.0, float("nan"), 0, 0, 0, warnings,
        )

    try:
        edge_vals = []
        for e in edges:
            try:
                edge_vals.append(f(e))
            except NonFiniteValueError as ex:
                raise _GiveUp(
                    "INVALID_VALUE_AT_POINT", f"x={ex.x:g}",
                    {"location": ex.x},
                )
        panel_data = []  # (pa, pb, fa, fc, fb, s1)
        for k, (pa, pb) in enumerate(panels):
            try:
                fc = f(0.5 * (pa + pb))
            except NonFiniteValueError as ex:
                raise _GiveUp(
                    "INVALID_VALUE_AT_POINT", f"x={ex.x:g}",
                    {"location": ex.x},
                )
            fa, fb = edge_vals[k], edge_vals[k + 1]
            h = pb - pa
            s1 = (h / 6.0) * (fa + 4.0 * fc + fb)
            panel_data.append((pa, pb, fa, fc, fb, s1))
    except _GiveUp as give:
        return _failed(
            method, give.code, give.detail,
            float("nan"), float("nan"), f.count, max_depth,
            total_intervals, warnings, give.extra,
        )

    # 用粗近似估计 |I| 确定全局目标，再均分到各初始面板，面板内递归对半
    coarse = float(sum(p[5] for p in panel_data))
    global_target = cfg.abs_tol + cfg.rel_tol * abs(coarse)
    eps_panel = global_target / len(panel_data)

    total_value = 0.0
    try:
        for pa, pb, fa, fc, fb, s1 in panel_data:
            total_value += _recurse(
                pa, pb, fa, fc, fb, s1, eps_panel, 1
            )
    except _GiveUp as give:
        return _failed(
            method, give.code, give.detail,
            float("nan"), float("nan"), f.count, max_depth,
            total_intervals, warnings, give.extra,
        )

    if leaf_err_sum == 0.0:
        warnings.append(
            "部分叶节点 Simpson 两级估计差值恰为 0：对高振荡/窄峰被积函数"
            "可能发生等距节点混叠，此时误差估计失效，结果可能不可靠；"
            "可改用 gk15、增大 initial_intervals 或收紧容差"
        )

    return IntegrationResult(
        status=STATUS_CONVERGED,
        value=total_value,
        error_estimate=leaf_err_sum,
        method=method,
        evaluations=f.count,
        depth_reached=max_depth,
        intervals=total_intervals,
        warnings=warnings,
        diagnostics={
            "requested_tolerance": global_target,
            "note": "误差估计为所有接受叶节点 |S2-S1|/15 之和；"
                    "局部容差随二分对半分配。估计为 0 时警惕等距节点混叠"
        },
    )
