"""顶点枚举参考实现（暴力法，仅供小规模对照测试）。

对多面体 ``{y >= 0 : A_le y <= b_le, A_eq y = b_eq, y <= ub}``，
每个顶点都由 n 个线性独立的"有效边界"确定：等式约束恒有效，
其余边界来自 ``A_le y = b_le``、``y_j = 0``、``y_j = ub_j``。

枚举所有 n 选 m 的活动集合，解线性方程，可行性过滤、去重。
组合数为指数级，因此硬性限制活动边界数量（默认 24），只用于
测试中的小整数问题，与单纯形结果逐一对照。
"""

from __future__ import annotations

from itertools import combinations

import numpy as np

from .model import TOL

MAX_ACTIVE = 24


class EnumerationLimit(Exception):
    """候选活动边界过多，暴力枚举不适用。"""


def enumerate_vertices(
    n: int,
    A_le=None, b_le=None,
    A_eq=None, b_eq=None,
    ub=None,
    max_active: int = MAX_ACTIVE,
) -> np.ndarray:
    """返回去重后的可行顶点数组，形状 ``(V, n)``。"""
    if A_le is not None and A_le.size == 0:
        A_le, b_le = None, None
    if A_eq is not None and A_eq.size == 0:
        A_eq, b_eq = None, None
    ub = np.full(n, np.inf) if ub is None else np.asarray(ub, float).ravel()

    # 活动边界候选：(法向量, 右端)。
    normals = []
    rhs = []
    me = A_eq.shape[0] if A_eq is not None else 0
    if A_eq is not None:
        for i in range(me):
            normals.append(A_eq[i])
            rhs.append(b_eq[i])
    if A_le is not None:
        for i in range(A_le.shape[0]):
            normals.append(A_le[i])
            rhs.append(b_le[i])
    for j in range(n):
        v = np.zeros(n)
        v[j] = 1.0
        normals.append(v)
        rhs.append(0.0)
        if np.isfinite(ub[j]):
            normals.append(v)
            rhs.append(ub[j])

    M = np.array(normals)
    h = np.array(rhs)
    optional = list(range(me, M.shape[0]))
    if len(optional) > max_active:
        raise EnumerationLimit(
            f"活动边界候选 {len(optional)} > {max_active}，暴力枚举不适用"
        )

    vertices: list[np.ndarray] = []
    need = n - me
    if need < 0:
        return np.zeros((0, n))
    for extra in combinations(optional, need):
        rows = list(range(me)) + list(extra)
        B = M[rows]
        # 条件数粗筛奇异组合。
        if np.linalg.matrix_rank(B, tol=TOL.pivot) < n:
            continue
        try:
            y = np.linalg.solve(B, h[rows])
        except np.linalg.LinAlgError:
            continue
        if _is_feasible(y, A_le, b_le, A_eq, b_eq, ub):
            if not any(_same_vertex(y, v) for v in vertices):
                vertices.append(y)
    return np.array(vertices) if vertices else np.zeros((0, n))


def _is_feasible(y, A_le, b_le, A_eq, b_eq, ub) -> bool:
    scale = lambda v: max(1.0, np.max(np.abs(v))) if v.size else 1.0
    if A_le is not None:
        v = A_le @ y - b_le
        if np.max(v) > TOL.feas * scale(b_le):
            return False
    if A_eq is not None:
        v = np.abs(A_eq @ y - b_eq)
        if np.max(v) > TOL.feas * scale(b_eq):
            return False
    if np.min(y) < -TOL.feas:
        return False
    finite = np.isfinite(ub)
    if np.any(finite) and np.max(y[finite] - ub[finite]) > TOL.feas:
        return False
    return True


def _same_vertex(a: np.ndarray, b: np.ndarray) -> bool:
    return bool(np.max(np.abs(a - b)) <= 1e2 * TOL.feas)


def best_vertex_value(vertices: np.ndarray, c, sense: str = "min"):
    """从枚举顶点中取最优目标值（有界时与单纯形最优值对照）。"""
    if vertices.shape[0] == 0:
        return None, None
    vals = vertices @ np.asarray(c, float).ravel()
    if sense == "max":
        i = int(np.argmax(vals))
    else:
        i = int(np.argmin(vals))
    return float(vals[i]), vertices[i]
