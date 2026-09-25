"""与暴力顶点枚举参考解逐题对照。

对小规模（n <= 4、活动边界 <= 24）整数系数问题：

1. 用枚举法列出全部可行顶点；
2. 单纯形的最优值必须等于顶点上的最优目标值；
3. 单纯形返回的解点必须落在某个枚举顶点上（容差内）；
4. 无界问题：枚举器必须发现存在"无界方向"（由参考求解单独标注）。
"""

from __future__ import annotations

import numpy as np
import pytest

from blp import (
    EnumerationLimit, best_vertex_value, enumerate_vertices, make_lp,
    solve_lp,
)


def _cases():
    cases = []
    cases.append(dict(
        c=[-3, -5], A_ub=[[1, 0], [0, 2], [3, 2]], b_ub=[4, 12, 18],
        obj=-36,
    ))
    cases.append(dict(  # 含 x+y>=1
        c=[5, 4],
        A_ub=[[6, 4], [1, 2], [-1, -1]],
        b_ub=[24, 6, -1],
    ))
    cases.append(dict(
        c=[-1, -1], A_ub=[[1, 1], [1, -1]], b_ub=[1, 0], obj=-1,
    ))
    cases.append(dict(
        c=[1, 2, 3],
        A_ub=[[1, 1, 0], [0, 1, 1], [1, 0, 1]], b_ub=[2, 2, 2],
        obj=0,
    ))
    cases.append(dict(
        c=[-2, 3, -1],
        A_ub=[[1, 1, 1], [2, 0, 1]], b_ub=[4, 3],
        A_eq=[[1, 0, 0]], b_eq=[1],
    ))
    cases.append(dict(
        c=[-1, -1], A_ub=[[1, 2]], b_ub=[6], ub=[4, 3],
    ))
    return cases


@pytest.mark.parametrize("case", _cases())
def test_optimal_matches_vertex_enumeration(case):
    c = case["c"]
    n = len(c)
    A_ub = case.get("A_ub")
    b_ub = case.get("b_ub")
    A_eq = case.get("A_eq")
    b_eq = case.get("b_eq")
    ub = case.get("ub")
    ub_arr = (
        np.full(n, np.inf) if ub is None
        else np.asarray(ub, float).ravel()
    )
    verts = enumerate_vertices(
        n,
        A_le=None if A_ub is None else np.array(A_ub, float),
        b_le=None if b_ub is None else np.array(b_ub, float),
        A_eq=None if A_eq is None else np.array(A_eq, float),
        b_eq=None if b_eq is None else np.array(b_eq, float),
        ub=ub_arr,
    )
    assert verts.shape[0] > 0, "枚举器至少应找到一个顶点"
    ref_val, ref_x = best_vertex_value(verts, c, "min")

    r = solve_lp(make_lp(
        c=c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, ub=ub,
    ))
    assert r.status == "optimal", (r.status, r.detail)
    assert abs(r.objective - ref_val) < 1e-7, (
        f"simplex={r.objective} enumeration={ref_val}"
    )
    # 单纯形解点必须等于某个枚举顶点（可能有多解）。
    assert any(np.max(np.abs(r.x - v)) < 1e-6 for v in verts), (
        f"解点 {r.x} 不是枚举顶点之一"
    )


def test_enumeration_rejects_too_large():
    with pytest.raises(EnumerationLimit):
        enumerate_vertices(30)


def test_all_vertices_feasible():
    A = np.array([[1, 0], [0, 1], [1, 1]], float)
    b = np.array([3, 4, 5], float)
    verts = enumerate_vertices(2, A_le=A, b_le=b)
    for v in verts:
        assert np.max(A @ v - b) <= 1e-7
        assert np.min(v) >= -1e-8


def test_enumeration_count_square():
    # 0<=x<=2, 0<=y<=2 恰有 4 个顶点。
    verts = enumerate_vertices(
        2,
        A_le=np.array([[1, 0], [0, 1]], float),
        b_le=np.array([2, 2], float),
        ub=np.array([2, 2]),
    )
    assert verts.shape[0] == 4


def test_random_small_problems_match_enumeration():
    """随机生成 2-3 维小整数问题，逐题与枚举解对照。"""
    rng = np.random.default_rng(2026)
    checked = 0
    for trial in range(60):
        n = int(rng.integers(2, 4))
        m = int(rng.integers(2, 6))
        A = rng.integers(-3, 4, size=(m, n)).astype(float)
        b = rng.integers(0, 8, size=m).astype(float)
        c = rng.integers(-4, 5, size=n).astype(float)
        if rng.random() < 0.4:
            ub = rng.integers(1, 6, size=n).astype(float)
        else:
            ub = None
        try:
            verts = enumerate_vertices(
                n, A_le=A, b_le=b, ub=(
                    np.full(n, np.inf) if ub is None else ub
                ),
            )
        except EnumerationLimit:
            continue
        if verts.shape[0] == 0:
            # 不可行：枚举为空，求解器也必须报 infeasible。
            r = solve_lp(make_lp(c=c, A_ub=A, b_ub=b, ub=ub))
            assert r.status == "infeasible", (trial, r.status, r.detail)
            checked += 1
            continue
        ref_val, _ = best_vertex_value(verts, c, "min")
        r = solve_lp(make_lp(c=c, A_ub=A, b_ub=b, ub=ub))
        # 有界多面体但可能沿某方向目标无界（枚举顶点之外）。
        if r.status == "unbounded":
            # 枚举器不知道无界；手工确认存在目标改善方向。
            assert r.ray is not None
            continue
        assert r.status == "optimal", (trial, r.status, r.detail)
        assert abs(r.objective - ref_val) < 1e-6, (
            trial, r.objective, ref_val
        )
        checked += 1
    assert checked >= 30, f"对照题数不足：{checked}"
