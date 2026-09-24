"""约束轨迹平滑优化器（归一化坐标系内求解）。

决策变量：折线内部控制点 y_1..y_{M-2}（首尾端点固定），扁平为长度 2(M-2)。
目标：加速度（二阶差分）+ 加加速度（三阶差分，曲率变化）+ 对原路径的偏离跟踪。
约束：
  1) 每个内部节点偏离原路径 <= deviation_bound（硬约束）；
  2) Menger 曲率上界 max_curvature（限制曲率）；
  3) 每个采样点对每个邻近矩形障碍 sdf(p) >= safety_margin。
障碍约束的 Jacobian 用解析梯度；曲率约束用前向差分 Jacobian。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import warnings

import numpy as np
from scipy.optimize import minimize

from .geometry import menger_curvature_sq, numerical_jacobian, rect_sdf, rect_sdf_grad


# ---------------------------------------------------------------------------
# 差分算子
# ---------------------------------------------------------------------------

def difference_matrices(m: int):
    """返回 D1 (m-1 × m)、D2 (m-2 × m)、D3 (m-3 × m)。"""
    d1 = np.zeros((max(0, m - 1), m))
    for i in range(m - 1):
        d1[i, i] = -1.0
        d1[i, i + 1] = 1.0

    d2 = np.zeros((max(0, m - 2), m))
    for i in range(m - 2):
        d2[i, i] = 1.0
        d2[i, i + 1] = -2.0
        d2[i, i + 2] = 1.0

    d3 = np.zeros((max(0, m - 3), m))
    for i in range(m - 3):
        d3[i, i] = -1.0
        d3[i, i + 1] = 3.0
        d3[i, i + 2] = -3.0
        d3[i, i + 3] = 1.0
    return d1, d2, d3


# ---------------------------------------------------------------------------
# 障碍采样
# ---------------------------------------------------------------------------

@dataclass
class Sample:
    seg: int        # 线段索引（点 k -> k+1）
    u: float        # 线段参数 [0, 1]
    obs: int        # 障碍索引


def build_obstacle_samples(points, obstacles, margin, bound, min_dim, extra=None):
    """生成「线段 × 参数 × 邻近障碍」的碰撞采样表。

    邻近过滤（保守，不漏）：障碍矩形按 (bound + margin + 0.5*min_dim) 膨胀后，
    与线段包围盒相交才纳入——这是线段可能触及膨胀矩形的必要条件。
    采样密度随线段长度/障碍最小尺寸自适应，每段 3..24 个样本（含两端）。
    extra: 精确校验失败后追加的 (seg, u, obs) 深侵入样本。
    """
    n = len(points)
    rad = bound + margin + 0.5 * min_dim
    samples: list[Sample] = []
    for k in range(n - 1):
        a, b = points[k], points[k + 1]
        seg_len = float(np.hypot(b[0] - a[0], b[1] - a[1]))
        # 采样间距约为障碍最小尺寸的一半（3..24 个/段，含两端）；
        # 任何漏检由 solve 后的整段精确校验 + 深点补采样兜底。
        n_samp = int(np.clip(np.ceil(2.0 * seg_len / max(min_dim, 1.0e-9)) + 1, 3, 24))
        us = np.linspace(0.0, 1.0, n_samp)
        sx0, sx1 = sorted((a[0], b[0]))
        sy0, sy1 = sorted((a[1], b[1]))
        for j, (xmin, ymin, xmax, ymax) in enumerate(obstacles):
            if sx1 < xmin - rad or sx0 > xmax + rad:
                continue
            if sy1 < ymin - rad or sy0 > ymax + rad:
                continue
            for u in us:
                samples.append(Sample(seg=k, u=float(u), obs=j))
    if extra:
        existing = {(s.seg, round(s.u, 12), s.obs) for s in samples}
        for seg, u, obs in extra:
            key = (seg, round(float(u), 12), obs)
            if key not in existing:
                samples.append(Sample(seg=seg, u=float(u), obs=obs))
                existing.add(key)
    return samples


# ---------------------------------------------------------------------------
# 求解结果
# ---------------------------------------------------------------------------

@dataclass
class SolveResult:
    converged: bool
    reason: str
    points: np.ndarray | None = None          # 归一化坐标下的整条折线
    iterations: int = 0
    objective_history: list[float] = field(default_factory=list)
    residuals: dict = field(default_factory=dict)
    warning_messages: list[str] = field(default_factory=list)


def solve_smoothing(
    points_norm,
    obstacles_norm,
    deviation_bound: float,
    safety_margin: float,
    max_curvature: float,
    max_iter: int,
    extra_samples=None,
    weights=(1.0, 1.0, 0.5),
):
    """在归一化坐标系执行一次 SLSQP 求解。

    weights = (w_accel 二阶差分, w_jerk 三阶差分/曲率变化, w_track 偏离原路径)。
    """
    p0 = np.asarray(points_norm, dtype=float)
    m = len(p0)
    obstacles = [tuple(o) for o in obstacles_norm]
    w_acc, w_jerk, w_track = weights

    _, d2, d3 = difference_matrices(m)
    q_mat = w_acc * (d2.T @ d2) + w_jerk * (d3.T @ d3) + w_track * np.eye(m)

    n_int = m - 2
    z0 = p0[1:-1].reshape(-1).copy()

    def reconstruct(z):
        y = p0.copy()
        y[1:-1] = z.reshape(n_int, 2)
        return y

    def objective(z):
        y = reconstruct(z)
        # J = 0.5 y^T Q y - w_track·y^T p0（省略与 y 无关的常数项），
        # 与 objective_grad 严格一致（SLSQP 要求两者匹配）。
        val = 0.5 * float(np.einsum("ij,jk,ik->", q_mat, y, y))
        val -= w_track * float(np.sum(y * p0))
        return val

    def objective_grad(z):
        y = reconstruct(z)
        gy = q_mat @ y - w_track * p0
        return gy[1:-1].reshape(-1)

    constraints = []

    # --- 偏离约束（内部节点）：bound² - ||y_i - p0_i||² >= 0 ---
    def dev_fun(z):
        yi = z.reshape(n_int, 2)
        d2v = np.sum((yi - p0[1:-1]) ** 2, axis=1)
        return deviation_bound**2 - d2v

    def dev_jac(z):
        yi = z.reshape(n_int, 2)
        g = np.zeros((n_int, 2 * n_int))
        for i in range(n_int):
            g[i, 2 * i : 2 * i + 2] = 2.0 * (p0[i + 1] - yi[i])
        return g

    if deviation_bound > 0.0:
        constraints.append({"type": "ineq", "fun": dev_fun, "jac": dev_jac})
    else:
        # bound == 0：内部节点不可移动，用等式约束固定为初始折线。
        constraints.append(
            {
                "type": "eq",
                "fun": lambda z: z - z0,
                "jac": lambda z: np.eye(2 * n_int),
            }
        )

    # --- 曲率约束（Menger）：κmax² - κ²(三元组) >= 0，数值 Jacobian ---
    def curv_fun_z(z):
        y = reconstruct(z)
        vals = np.empty(m - 2)
        for i in range(1, m - 1):
            vals[i - 1] = max_curvature**2 - menger_curvature_sq(y[i - 1], y[i], y[i + 1])
        return vals

    def curv_jac(z):
        return numerical_jacobian(curv_fun_z, z, eps=1.0e-8)

    constraints.append({"type": "ineq", "fun": curv_fun_z, "jac": curv_jac})

    # --- 障碍采样约束：sdf(p(u)) - margin >= 0，解析 Jacobian ---
    if obstacles:
        dims = [min(o[2] - o[0], o[3] - o[1]) for o in obstacles]
        min_dim = max(min(dims), 1.0e-6)
    else:
        min_dim = 1.0
    samples = build_obstacle_samples(
        p0, obstacles, safety_margin, deviation_bound, min_dim, extra_samples
    )

    if samples:
        obs_groups: dict[int, list[int]] = {}
        for r, s in enumerate(samples):
            obs_groups.setdefault(s.obs, []).append(r)

        def sample_points(z):
            y = reconstruct(z)
            pts = np.empty((len(samples), 2))
            for r, s in enumerate(samples):
                pts[r] = (1.0 - s.u) * y[s.seg] + s.u * y[s.seg + 1]
            return pts

        def obs_fun(z):
            pts = sample_points(z)
            vals = np.empty(len(samples))
            for j, rows in obs_groups.items():
                idx = np.asarray(rows, dtype=int)
                vals[idx] = rect_sdf(pts[idx], obstacles[j]) - safety_margin
            return vals

        def obs_jac(z):
            y = reconstruct(z)
            jac = np.zeros((len(samples), 2 * n_int))
            for r, s in enumerate(samples):
                p = (1.0 - s.u) * y[s.seg] + s.u * y[s.seg + 1]
                g = rect_sdf_grad(p, obstacles[s.obs])
                for node, wgt in ((s.seg, 1.0 - s.u), (s.seg + 1, s.u)):
                    if 1 <= node <= m - 2 and wgt != 0.0:
                        col = 2 * (node - 1)
                        jac[r, col] = wgt * g[0]
                        jac[r, col + 1] = wgt * g[1]
            return jac

        constraints.append({"type": "ineq", "fun": obs_fun, "jac": obs_jac})

    # --- 求解 ---
    history = [objective(z0)]

    def callback(xk):
        history.append(objective(xk))

    caught_warnings: list[str] = []
    with warnings.catch_warnings(record=True) as wlist:
        warnings.simplefilter("always")
        result = minimize(
            objective,
            z0,
            jac=objective_grad,
            method="SLSQP",
            constraints=constraints,
            callback=callback,
            options={"maxiter": max_iter, "ftol": 1.0e-10, "disp": False},
        )
        for w in wlist:
            caught_warnings.append(f"{w.category.__name__}: {w.message}")

    y_star = reconstruct(result.x)

    # --- 残差（归一化坐标）---
    dev = np.sqrt(np.sum((y_star - p0) ** 2, axis=1))
    curv_sq = np.array(
        [
            menger_curvature_sq(y_star[i - 1], y_star[i], y_star[i + 1])
            for i in range(1, m - 1)
        ]
    ) if m >= 3 else np.array([0.0])
    obstacle_rows = obs_fun(result.x) if samples else np.array([np.inf])

    residuals = {
        "deviation_max_norm": float(np.max(dev)),
        "deviation_violation_norm": float(max(0.0, np.max(dev) - deviation_bound)),
        "curvature_max_norm": float(np.sqrt(np.max(curv_sq))),
        "curvature_violation_norm": float(
            max(0.0, np.sqrt(np.max(curv_sq)) - max_curvature)
        ),
        "obstacle_sample_min_clearance_norm": float(np.min(obstacle_rows)),
        "constraint_incompatible_warning": any(
            "Inequality constraints incompatible" in msg for msg in caught_warnings
        ),
    }

    incompatible = residuals["constraint_incompatible_warning"]
    converged = bool(result.success) and result.status == 0 and not incompatible

    return SolveResult(
        converged=converged,
        reason="converged" if converged else str(result.message),
        points=y_star,
        iterations=int(getattr(result, "nit", len(history) - 1)),
        objective_history=[float(v) for v in history],
        residuals=residuals,
        warning_messages=caught_warnings,
    ), samples
