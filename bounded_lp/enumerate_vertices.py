"""顶点枚举参考实现（独立于单纯形，仅用于测试交叉核验）。

对小规模问题，枚举非负约束外的所有“活跃约束”组合，选 n 个线性无关约束
联立求解顶点，再检查可行性。这是教科书式的暴力方法，与单纯形完全独立，
因此两者结果一致可以互相印证。

仅适用于 n <= 15 左右的小问题。
"""

from __future__ import annotations

from itertools import combinations

import numpy as np

from bounded_lp.problem import LPProblem
from bounded_lp.simplex import INFEASIBLE, OPTIMAL, UNBOUNDED

RANK_TOL = 1e-9
FEAS_TOL = 1e-7


def _all_rows(problem: LPProblem):
    """组装不等式系统 G z <= h（z = x - lb, z >= 0 由 -I 行表示）。

    等式 a^T x = b 拆成 a^T z <= b-a^T lb 与 -a^T z <= -(b-a^T lb) 两行；
    有限上界补 z_j <= ub-lb 行。返回平移坐标下的 (G, h)。
    """
    rows = []
    rhs = []
    if problem.A_ub is not None and problem.A_ub.shape[0]:
        rows.extend(problem.A_ub.tolist())
        rhs.extend((problem.b_ub - problem.A_ub @ problem.lb).tolist())
    if problem.A_eq is not None and problem.A_eq.shape[0]:
        shift = problem.A_eq @ problem.lb
        for a, b, s in zip(problem.A_eq, problem.b_eq, shift):
            rows.append(a.tolist())
            rhs.append(float(b - s))
            rows.append((-a).tolist())
            rhs.append(-float(b - s))
    for j in range(problem.n):
        if np.isfinite(problem.ub[j]):
            row = [0.0] * problem.n
            row[j] = 1.0
            rows.append(row)
            rhs.append(float(problem.ub[j] - problem.lb[j]))
    G = np.array(rows, dtype=float)
    h = np.array(rhs, dtype=float)
    return G, h


def enumerate_vertices(problem: LPProblem, *, feas_tol: float = FEAS_TOL):
    """枚举全部可行顶点（原始坐标），形状 (k, n)；k=0 表示空可行域。"""
    n = problem.n
    G, h = _all_rows(problem)

    # z >= 0 写成 -z <= 0
    M = np.vstack([G, -np.eye(n)]) if G.shape[0] else -np.eye(n)
    kk = np.concatenate([h, np.zeros(n)]) if G.shape[0] else np.zeros(n)

    verts: list[np.ndarray] = []
    seen: set[tuple[float, ...]] = set()

    for combo in combinations(range(M.shape[0]), n):
        B = M[list(combo)]
        if np.linalg.matrix_rank(B, RANK_TOL) < n:
            continue
        try:
            z = np.linalg.solve(B, kk[list(combo)])
        except np.linalg.LinAlgError:
            continue
        if np.max(M @ z - kk) > feas_tol:
            continue
        z = np.where(np.abs(z) < feas_tol, 0.0, z)
        key = tuple(np.round(z, 7))
        if key not in seen:
            seen.add(key)
            verts.append(problem.lb + z)

    return np.array(verts) if verts else np.zeros((0, n))


def find_recession_ray(problem: LPProblem, f: np.ndarray,
                       *, feas_tol: float = FEAS_TOL):
    """在给定顶点（内部从 0 起）的衰退锥中找改善方向 d：G d<=0, d>=0, f^T d<0。

    衰退锥的极射线由“n-1 个线性无关的活跃面法向”确定。枚举 G∪(-I) 中
    取 n-1 行联立 A d=0 的一维零空间，核验 ±方向。若存在改善衰退射线，
    必存在一条极射线改善（有限个极射线的非负组合若严格改善，至少一条
    极射线严格改善）。
    """
    n = problem.n
    G, _ = _all_rows(problem)
    M = np.vstack([G, -np.eye(n)]) if G.shape[0] else -np.eye(n)
    n_rows = M.shape[0]

    if n_rows < n - 1:
        combos = [()]
    else:
        combos = combinations(range(n_rows), n - 1)

    for combo in combos:
        A = M[list(combo)] if combo else np.zeros((0, n))
        if np.linalg.matrix_rank(A, RANK_TOL) < n - 1:
            continue
        # 零空间：SVD 最后一列
        _, _, vh = np.linalg.svd(A, full_matrices=True)
        d = vh[-1]
        for cand in (d, -d):
            cand = np.where(np.abs(cand) < feas_tol, 0.0, cand)
            if np.min(cand) < -10 * feas_tol:
                continue
            if G.shape[0] and np.max(G @ cand) > 10 * feas_tol:
                continue
            if f @ cand >= -10 * feas_tol:
                continue
            norm = np.max(np.abs(cand))
            return cand / norm
    return None


def reference_solve(problem: LPProblem, *, feas_tol: float = FEAS_TOL):
    """用顶点枚举 + 衰退锥极射线枚举求参考结论。

    返回 ``(status, info)``：
      - ("optimal", {"x", "objective"})
      - ("infeasible", {})
      - ("unbounded", {"x", "ray"})
    """
    sign = 1.0 if problem.sense == "min" else -1.0
    f = sign * problem.c

    verts = enumerate_vertices(problem, feas_tol=feas_tol)
    if verts.shape[0] == 0:
        return INFEASIBLE, {}

    vals = verts @ f
    k = int(np.argmin(vals))
    x, v = verts[k], float(vals[k])

    d = find_recession_ray(problem, f, feas_tol=feas_tol)
    if d is not None:
        return UNBOUNDED, {"x": x, "ray": d}
    return OPTIMAL, {"x": x, "objective": float(problem.c @ x)}
