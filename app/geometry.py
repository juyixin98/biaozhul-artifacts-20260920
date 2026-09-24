"""二维几何原语：矩形有符号距离、线段-矩形精确校验、Menger 曲率。

约定：矩形 obstacle = (xmin, ymin, xmax, ymax)，且 xmin < xmax、ymin < ymax。
有符号距离约定：点在矩形外（或边界上）为非负，矩形内部为负。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

EPS_NUM = 1.0e-12


# ---------------------------------------------------------------------------
# 矩形有符号距离
# ---------------------------------------------------------------------------

def rect_sdf(points: np.ndarray, rect: tuple[float, float, float, float]) -> np.ndarray:
    """返回点集到轴对齐矩形的有符号距离（外正内负）。

    对内部点：sdf = -min(qx, qy)，其中 qx/qy 为点到最近竖直/水平边的距离。
    对外部点：sdf = ||max(q, 0)||，q 为点到矩形中心盒投影的分量。
    """
    pts = np.asarray(points, dtype=float).reshape(-1, 2)
    xmin, ymin, xmax, ymax = rect
    x = pts[:, 0]
    y = pts[:, 1]
    qx = np.maximum(xmin - x, x - xmax)
    qy = np.maximum(ymin - y, y - ymax)
    outside = (qx > 0.0) | (qy > 0.0)
    sdf = np.empty(len(pts), dtype=float)
    sdf[outside] = np.hypot(
        np.maximum(qx[outside], 0.0), np.maximum(qy[outside], 0.0)
    )
    sdf[~outside] = -np.minimum(np.abs(qx[~outside]), np.abs(qy[~outside]))
    return sdf


def rect_sdf_grad(point: np.ndarray, rect: tuple[float, float, float, float]) -> np.ndarray:
    """单点 SDF 的解析梯度（2,）；位于非光滑面（延长线/对角）上时返回次梯度。"""
    x, y = float(point[0]), float(point[1])
    xmin, ymin, xmax, ymax = rect
    qx = max(xmin - x, x - xmax)
    qy = max(ymin - y, y - ymax)

    if qx <= 0.0 and qy <= 0.0:
        # 点在矩形内部：sdf = -min(-qx, -qy)，即取最近边距离的负值。
        dx_in = min(x - xmin, xmax - x)
        dy_in = min(y - ymin, ymax - y)
        if dx_in < dy_in:
            return np.array([-1.0 if (x - xmin) < (xmax - x) else 1.0, 0.0])
        if dy_in < dx_in:
            return np.array([0.0, -1.0 if (y - ymin) < (ymax - y) else 1.0])
        return np.array([0.0, 0.0])  # 内部等距线：非光滑点，取 0 次梯度

    if qx > 0.0 and qy > 0.0:
        # 对角区域：朝最近角点方向。
        denom = np.hypot(qx, qy)
        gx = -1.0 if (xmin - x) >= (x - xmax) else 1.0
        gy = -1.0 if (ymin - y) >= (y - ymax) else 1.0
        return np.array([gx * qx / denom, gy * qy / denom])

    if qx > 0.0:
        # 左/右竖直带内：最近点在竖直边上。
        return np.array([-1.0 if (xmin - x) >= (x - xmax) else 1.0, 0.0])

    # 下/上水平带内：最近点在水平边上。
    return np.array([0.0, -1.0 if (ymin - y) >= (y - ymax) else 1.0])


# ---------------------------------------------------------------------------
# 线段-矩形精确（无采样）校验
# ---------------------------------------------------------------------------

def _clip_t(p, q, t0, t1):
    """Liang-Barsky 一维裁剪辅助。返回 (可见, t0, t1)。"""
    if abs(p) < EPS_NUM:
        if q < 0.0:
            return False, t0, t1
        return True, t0, t1
    r = q / p
    if p < 0.0:
        if r > t1:
            return False, t0, t1
        if r > t0:
            t0 = r
    else:
        if r < t0:
            return False, t0, t1
        if r < t1:
            t1 = r
    return True, t0, t1


def _lb_interval(a: np.ndarray, b: np.ndarray, rect):
    """线段 a->b 与矩形（含边界）相交的参数区间 [t0, t1] ⊂ [0,1]；不相交返回 None。"""
    xmin, ymin, xmax, ymax = rect
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    t0, t1 = 0.0, 1.0
    ok, t0, t1 = _clip_t(-dx, a[0] - xmin, t0, t1)
    if not ok:
        return None
    ok, t0, t1 = _clip_t(dx, xmax - a[0], t0, t1)
    if not ok:
        return None
    ok, t0, t1 = _clip_t(-dy, a[1] - ymin, t0, t1)
    if not ok:
        return None
    ok, t0, t1 = _clip_t(dy, ymax - a[1], t0, t1)
    if not ok:
        return None
    if t0 > t1 + EPS_NUM:
        return None
    return t0, t1


def _point_segment_distance(px, py, ax, ay, bx, by) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    len2 = vx * vx + vy * vy
    if len2 <= EPS_NUM:
        return float(np.hypot(px - ax, py - ay))
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / len2))
    return float(np.hypot(px - (ax + t * vx), py - (ay + t * vy)))


def _segment_rect_clearance_exterior(a, b, rect) -> float:
    """线段与矩形不相交时的精确最小距离：端点到四条边的点-段距离最小值。"""
    xmin, ymin, xmax, ymax = rect
    corners = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]
    best = float("inf")
    for k in range(4):
        ex0, ey0 = corners[k]
        ex1, ey1 = corners[(k + 1) % 4]
        for px, py in (a, b):
            best = min(best, _point_segment_distance(px, py, ex0, ey0, ex1, ey1))
    return best


def segment_rect_penetration(a, b, rect):
    """精确计算线段 a->b 对矩形的最大侵入深度及位置。

    返回 (penetration, t)：
      penetration < 0 表示线段进入矩形内部（碰撞），t 为 SDF 最小处的参数；
      penetration = 0 表示仅相切/接触边界（允许）；
      penetration > 0 表示与矩形严格分离，值为最小间隙。
    不做采样：相交区间内 SDF 是分段凸二次函数，最小值只可能出现在
    端点、矩形中线 x=xmid、y=ymid 以及两侧等距线 qx(t)=qy(t) 的根上。
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if np.hypot(b[0] - a[0], b[1] - a[1]) <= EPS_NUM:
        return -float(rect_sdf(a.reshape(1, 2), rect)[0]), 0.0

    interval = _lb_interval(a, b, rect)
    if interval is None:
        return _segment_rect_clearance_exterior(a, b, rect), -1.0

    t0, t1 = interval
    xmin, ymin, xmax, ymax = rect
    xmid = 0.5 * (xmin + xmax)
    ymid = 0.5 * (ymin + ymax)
    dx = b[0] - a[0]
    dy = b[1] - a[1]

    candidates = [t0, t1]
    # 矩形竖直/水平中线（分段切换面）。
    if abs(dx) > EPS_NUM:
        t = (xmid - a[0]) / dx
        if t0 < t < t1:
            candidates.append(t)
    if abs(dy) > EPS_NUM:
        t = (ymid - a[1]) / dy
        if t0 < t < t1:
            candidates.append(t)
    # 等距线 |x(t)-xmid| - hx/2 = |y(t)-ymid| - hy/2 的 4 种符号组合。
    hx = xmax - xmin
    hy = ymax - ymin
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            denom = sx * dx - sy * dy
            if abs(denom) <= EPS_NUM:
                continue
            t = (sy * (a[1] - ymid) - sx * (a[0] - xmid) + 0.5 * (hx - hy)) / denom
            if t0 < t < t1:
                candidates.append(t)

    cand = np.unique(np.array(candidates, dtype=float))
    pts = a[None, :] + cand[:, None] * (b - a)[None, :]
    sd = rect_sdf(pts, rect)
    i = int(np.argmin(sd))
    min_sdf = float(sd[i])
    if min_sdf < -EPS_NUM:
        return -min_sdf, float(cand[i])
    # 严格位于内部的点应给出负 sdf；若全部 >= 0 则只是边界接触/外部。
    if min_sdf >= -EPS_NUM:
        # 可能只擦到边界：间隙为 0；外部情况由 LB 不相交分支处理。
        return 0.0, -1.0
    return -min_sdf, float(cand[i])


@dataclass
class CollisionEvent:
    segment_index: int
    obstacle_index: int
    penetration: float  # 侵入深度（>0 表示进入内部）
    t: float            # 线段参数，-1 表示仅接触/外部


def validate_trajectory(points, obstacles, safety_margin: float = 0.0):
    """对整条折线（不只是控制点）逐线段做精确碰撞校验。

    返回 (min_clearance, events)：
      min_clearance：所有「线段 × 障碍」对的最小间隙（侵入时为负）。
      events：所有 penetration >= safety_margin 违例的列表。
    """
    pts = np.asarray(points, dtype=float)
    min_clearance = float("inf")
    events: list[CollisionEvent] = []
    for k in range(len(pts) - 1):
        a, b = pts[k], pts[k + 1]
        for j, rect in enumerate(obstacles):
            pen, t = segment_rect_penetration(a, b, rect)
            clearance = -pen if t >= 0.0 else pen
            min_clearance = min(min_clearance, clearance)
            if clearance < safety_margin - 1.0e-9:
                events.append(
                    CollisionEvent(
                        segment_index=k,
                        obstacle_index=j,
                        penetration=max(0.0, -clearance),
                        t=t,
                    )
                )
    if min_clearance == float("inf"):
        min_clearance = float("inf") if not obstacles else min_clearance
    return min_clearance, events


# ---------------------------------------------------------------------------
# 曲率（Menger）与有限差 Jacobian
# ---------------------------------------------------------------------------

def menger_curvature_sq(p_prev, p, p_next) -> float:
    """三个连续点的 Menger 曲率平方 κ² = 4·|叉积|²/(a²b²c²)。"""
    p_prev = np.asarray(p_prev, dtype=float)
    p = np.asarray(p, dtype=float)
    p_next = np.asarray(p_next, dtype=float)
    a2 = float(np.sum((p - p_prev) ** 2))
    b2 = float(np.sum((p_next - p) ** 2))
    c2 = float(np.sum((p_next - p_prev) ** 2))
    cross2 = (
        float((p[0] - p_prev[0]) * (p_next[1] - p_prev[1]))
        - float((p[1] - p_prev[1]) * (p_next[0] - p_prev[0]))
    ) ** 2
    denom = a2 * b2 * c2
    if denom <= 1.0e-30:
        return 0.0  # 重复相邻点：退化为 0（重复点在预处理阶段已合并）。
    return 4.0 * cross2 / denom


def numerical_jacobian(fun, x, eps: float):
    """前向差分数值雅可比：fun(x) -> (m,)，x -> (n,)。返回 (m, n)。"""
    x = np.asarray(x, dtype=float)
    f0 = np.asarray(fun(x), dtype=float)
    m = f0.shape[0]
    jac = np.empty((m, x.shape[0]), dtype=float)
    for i in range(x.shape[0]):
        xp = x.copy()
        step = eps * max(1.0, abs(xp[i]))
        xp[i] += step
        jac[:, i] = (np.asarray(fun(xp), dtype=float) - f0) / step
    return jac
