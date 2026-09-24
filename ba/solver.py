"""Levenberg-Marquardt 束调整求解器。

支持两种正规方程求解方式：
- "schur"：先消元三维点（Schur 补），解相机约化方程后回代求点；
- "full" ：直接组装并求解完整正规方程（用于对照验证）。

规范（gauge）处理：
- 固定首相机（去除 6 自由度刚体规范）；
- 单目尺度歧义通过对相机 1 平移模长施加强先验（scale anchor）消除。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import linalg as sla

from .lie import exp_so3
from .problem import BAProblem
from .projection import project, projection_jacobians


@dataclass
class SolverOptions:
    max_iterations: int = 50
    solver: str = "schur"  # "schur" | "full"
    lambda_init: float = 1e-3
    lambda_up: float = 10.0
    lambda_down: float = 0.1
    fix_first_camera: bool = True
    scale_anchor: bool = True
    scale_weight: float = 1e4  # 尺度先验权重（强约束近似硬锚定）
    scale_target: float | None = None  # None 时取初始 ||t_1||
    depth_epsilon: float = 1e-6  # z <= 该值视为负深度/无效观测
    cost_tol: float = 1e-12  # 相对代价下降小于该值则收敛


@dataclass
class SolveResult:
    problem: BAProblem  # 优化后的问题（含优化后的位姿与点）
    converged: bool
    iterations: int
    cost_history: list[float] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)


class _LinearSystem:
    """一次 LM 迭代中线性化得到的正规方程块。"""

    def __init__(self, n_cam_vars: int, n_points: int):
        self.B = np.zeros((n_cam_vars, n_cam_vars))  # 相机-相机块
        self.C = np.zeros((n_points, 3, 3))  # 点块（块对角）
        self.E = np.zeros((n_cam_vars, 3 * n_points))  # 相机-点交叉块
        self.b_cam = np.zeros(n_cam_vars)
        self.b_pt = np.zeros(3 * n_points)


class BASolver:
    def __init__(self, options: SolverOptions | None = None):
        self.options = options or SolverOptions()

    # ------------------------------------------------------------------ #
    # 残差与代价
    # ------------------------------------------------------------------ #
    def _valid_residuals(self, problem: BAProblem):
        """返回 (有效观测残差列表, 负深度观测数)。

        每个元素: (obs_index, residual(2,), J_cam(2,6) 或 None, J_point(2,3))
        负深度（z <= eps）观测被剔除并计数。
        """
        entries = []
        n_negative_depth = 0
        intr = problem.intrinsics
        for k, obs in enumerate(problem.observations):
            pose = problem.cameras[obs.camera]
            point = problem.points[obs.point]
            pc = pose.transform(point)
            if pc[2] <= self.options.depth_epsilon:
                n_negative_depth += 1
                continue
            uv, _ = project(intr, pose, point)
            J_cam, J_pt = projection_jacobians(intr, pose, point)
            entries.append((k, uv - obs.uv, J_cam, J_pt))
        return entries, n_negative_depth

    def _scale_prior(self, problem: BAProblem):
        """尺度锚定残差：sqrt(w) * (||t_1|| - s0) 及其对 dt 的雅可比。"""
        if not self.options.scale_anchor or len(problem.cameras) < 2:
            return None
        t1 = problem.cameras[1].t
        n = float(np.linalg.norm(t1))
        if n < 1e-12:
            return None
        target = self.options.scale_target
        if target is None:
            target = self._scale_target0  # 求解开始时记录
        w = self.options.scale_weight
        r = np.sqrt(w) * (n - target)
        J = np.sqrt(w) * (t1 / n)  # (3,) 对 dt 的导数
        return r, J

    def _total_cost(self, problem: BAProblem) -> tuple[float, int]:
        entries, n_neg = self._valid_residuals(problem)
        cost = 0.5 * sum(float(r @ r) for _, r, _, _ in entries)
        prior = self._scale_prior(problem)
        if prior is not None:
            cost += 0.5 * float(prior[0] ** 2)
        return cost, n_neg

    # ------------------------------------------------------------------ #
    # 正规方程组装
    # ------------------------------------------------------------------ #
    def _build_normal_equations(self, problem: BAProblem, free_cams: list[int]):
        n_points = len(problem.points)
        cam_slot = {c: i for i, c in enumerate(free_cams)}
        n_cam_vars = 6 * len(free_cams)
        ls = _LinearSystem(n_cam_vars, n_points)

        entries, n_neg = self._valid_residuals(problem)
        cost = 0.0
        used_point_obs = np.zeros(n_points, dtype=int)

        for _, r, J_cam, J_pt in entries:
            obs = problem.observations[_]
            cost += 0.5 * float(r @ r)
            used_point_obs[obs.point] += 1
            ls.C[obs.point] += J_pt.T @ J_pt
            ls.b_pt[3 * obs.point : 3 * obs.point + 3] -= J_pt.T @ r
            if obs.camera in cam_slot:
                s = 6 * cam_slot[obs.camera]
                ls.B[s : s + 6, s : s + 6] += J_cam.T @ J_cam
                ls.E[s : s + 6, 3 * obs.point : 3 * obs.point + 3] += J_cam.T @ J_pt
                ls.b_cam[s : s + 6] -= J_cam.T @ r

        # 尺度锚定先验（作用于相机 1 的平移分量）
        prior = self._scale_prior(problem)
        if prior is not None and 1 in cam_slot:
            r_s, J_s = prior
            cost += 0.5 * float(r_s**2)
            s = 6 * cam_slot[1] + 3  # 平移分量的偏移
            ls.B[s : s + 3, s : s + 3] += np.outer(J_s, J_s)
            ls.b_cam[s : s + 3] -= J_s * r_s

        return ls, cost, n_neg, used_point_obs

    # ------------------------------------------------------------------ #
    # 两种求解方式
    # ------------------------------------------------------------------ #
    @staticmethod
    def _spd_solve(A: np.ndarray, b: np.ndarray) -> np.ndarray:
        """正定系统求解：优先 Cholesky（SciPy），非正定则退回普通 LU。"""
        try:
            return sla.cho_solve(sla.cho_factor(A), b)
        except np.linalg.LinAlgError:
            return np.linalg.solve(A, b)

    @staticmethod
    def _spd_invert(A: np.ndarray) -> np.ndarray:
        """正定矩阵求逆：优先 Cholesky，失败退回一般逆。"""
        try:
            return sla.cho_solve(sla.cho_factor(A), np.eye(A.shape[0]))
        except np.linalg.LinAlgError:
            return np.linalg.inv(A)

    @staticmethod
    def _solve_schur(ls: _LinearSystem, damping: float) -> np.ndarray:
        """Schur 补消元：先消去点变量，解相机约化方程，再回代。"""
        n_cam = ls.B.shape[0]
        n_pts = ls.C.shape[0]
        # 阻尼（Marquardt）：H += lambda * diag(H)，零对角兜底
        B = ls.B + damping * (np.diag(np.diag(ls.B)) + 1e-9 * np.eye(n_cam))
        C_inv = np.zeros_like(ls.C)
        for j in range(n_pts):
            Cj = ls.C[j] + damping * (np.diag(np.diag(ls.C[j])) + 1e-9 * np.eye(3))
            C_inv[j] = BASolver._spd_invert(Cj)
        # S = B - E C^-1 E^T ; rhs = b_cam - E C^-1 b_pt
        ECinv = np.zeros_like(ls.E)
        for j in range(n_pts):
            ECinv[:, 3 * j : 3 * j + 3] = ls.E[:, 3 * j : 3 * j + 3] @ C_inv[j]
        S = B - ECinv @ ls.E.T
        rhs = ls.b_cam - ECinv @ ls.b_pt
        d_cam = BASolver._spd_solve(S, rhs)
        d_pt = np.zeros(3 * n_pts)
        for j in range(n_pts):
            d_pt[3 * j : 3 * j + 3] = C_inv[j] @ (
                ls.b_pt[3 * j : 3 * j + 3] - ls.E[:, 3 * j : 3 * j + 3].T @ d_cam
            )
        return np.concatenate([d_cam, d_pt])

    @staticmethod
    def _solve_full(ls: _LinearSystem, damping: float) -> np.ndarray:
        """直接组装完整正规方程并求解（对照用）。"""
        n_cam = ls.B.shape[0]
        n_pts = ls.C.shape[0]
        n = n_cam + 3 * n_pts
        H = np.zeros((n, n))
        H[:n_cam, :n_cam] = ls.B
        for j in range(n_pts):
            H[n_cam + 3 * j : n_cam + 3 * j + 3, n_cam + 3 * j : n_cam + 3 * j + 3] = ls.C[j]
        H[:n_cam, n_cam:] = ls.E
        H[n_cam:, :n_cam] = ls.E.T
        b = np.concatenate([ls.b_cam, ls.b_pt])
        H += damping * (np.diag(np.diag(H)) + 1e-9 * np.eye(n))
        return np.linalg.solve(H, b)

    # ------------------------------------------------------------------ #
    # 增量应用
    # ------------------------------------------------------------------ #
    @staticmethod
    def _apply_step(problem: BAProblem, free_cams: list[int], delta: np.ndarray) -> BAProblem:
        out = problem.copy()
        n_cam_vars = 6 * len(free_cams)
        for i, c in enumerate(free_cams):
            dr = delta[6 * i : 6 * i + 3]
            dt = delta[6 * i + 3 : 6 * i + 6]
            out.cameras[c].R = exp_so3(dr) @ out.cameras[c].R
            out.cameras[c].t = out.cameras[c].t + dt
        out.points = out.points + delta[n_cam_vars:].reshape(-1, 3)
        return out

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def solve(self, problem: BAProblem) -> SolveResult:
        opt = self.options
        if opt.solver not in ("schur", "full"):
            raise ValueError(f"未知求解器: {opt.solver}")

        problem = problem.copy()
        n_cams = len(problem.cameras)
        free_cams = [i for i in range(n_cams) if not (opt.fix_first_camera and i == 0)]

        # 记录尺度锚定目标（初始 ||t_1||）
        self._scale_target0 = (
            opt.scale_target
            if opt.scale_target is not None
            else (float(np.linalg.norm(problem.cameras[1].t)) if n_cams > 1 else 0.0)
        )

        lam = opt.lambda_init
        cost, n_neg = self._total_cost(problem)
        cost_history = [cost]
        converged = False
        iterations = 0

        for it in range(opt.max_iterations):
            iterations = it + 1
            ls, cost, n_neg, used_point_obs = self._build_normal_equations(problem, free_cams)
            if opt.solver == "schur":
                delta = self._solve_schur(ls, lam)
            else:
                delta = self._solve_full(ls, lam)

            trial = self._apply_step(problem, free_cams, delta)
            new_cost, _ = self._total_cost(trial)

            if new_cost < cost:
                rel = (cost - new_cost) / max(cost, 1e-30)
                problem = trial
                # 尺度硬锚定：把 ||t_1|| 投影回目标值（消除尺度规范自由度）
                if opt.scale_anchor and n_cams > 1 and 1 in free_cams:
                    n1 = float(np.linalg.norm(problem.cameras[1].t))
                    if n1 > 1e-12:
                        problem.cameras[1].t *= self._scale_target0 / n1
                    cost, _ = self._total_cost(problem)
                else:
                    cost = new_cost
                cost_history.append(cost)
                lam = max(lam * opt.lambda_down, 1e-12)
                if rel < opt.cost_tol or cost < 1e-30:
                    converged = True
                    break
            else:
                lam *= opt.lambda_up
                if lam > 1e12:
                    break

        # 诊断信息
        _, n_neg = self._total_cost(problem)
        _, _, _, used_point_obs = self._build_normal_equations(problem, free_cams)
        under_observed = [int(j) for j in range(len(problem.points)) if used_point_obs[j] < 2]
        diagnostics = {
            "solver": opt.solver,
            "num_cameras": n_cams,
            "num_points": len(problem.points),
            "num_observations": len(problem.observations),
            "num_negative_depth_excluded": int(n_neg),
            "under_observed_points": under_observed,
            "fixed_cameras": [0] if opt.fix_first_camera else [],
            "scale_anchor": {
                "enabled": bool(opt.scale_anchor and n_cams > 1),
                "method": "soft_prior + hard_projection",
                "weight": opt.scale_weight,
                "target": self._scale_target0,
                "final_t1_norm": float(np.linalg.norm(problem.cameras[1].t)) if n_cams > 1 else None,
            },
            "final_lambda": lam,
        }
        return SolveResult(
            problem=problem,
            converged=converged,
            iterations=iterations,
            cost_history=cost_history,
            diagnostics=diagnostics,
        )
