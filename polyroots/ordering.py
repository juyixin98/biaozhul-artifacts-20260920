"""稳定排序与显式容差的共轭配对。

排序键：(实部, 虚部, 原始下标)，对浮点噪声不做任何隐式吸附——两个根
是否“同一个实根/共轭对”完全由显式 tol 参数判定，避免静默改值。

共轭配对：要求虚部异号且 |z - conj(w)|/(1+|z|) < conjugate_tol。
用全局最优贪心（按配对距离从小到大依次锁定唯一配对），保证 1+2i、1-2i
与 1-2i+噪声 这种情形不会重复配对。纯实根（虚部数值零）不参与配对。
"""

from __future__ import annotations

import numpy as np


def stable_order(roots: np.ndarray) -> np.ndarray:
    """返回将 roots 排为规范顺序的下标数组。"""
    return np.lexsort((np.arange(roots.size), roots.imag, roots.real))


def _conj_distance(a: complex, b: complex) -> float:
    return abs(a - np.conj(b)) / (1.0 + 0.5 * (abs(a) + abs(b)))


def conjugate_pairs(
    roots: np.ndarray,
    order: np.ndarray,
    conjugate_tol: float,
    real_tol: float = 1e-12,
) -> list[int | None]:
    """按规范顺序返回每根的配对伙伴下标（规范顺序中的下标），无则 None。"""
    n = roots.size
    sorted_roots = roots[order]
    pair_of: list[int | None] = [None] * n
    candidates: list[tuple[float, int, int]] = []
    for i in range(n):
        zi = sorted_roots[i]
        if abs(zi.imag) <= real_tol * (1.0 + abs(zi)):
            continue  # 数值实根，不参与共轭配对
        for j in range(i + 1, n):
            zj = sorted_roots[j]
            # 必须虚部异号（一侧零不算）
            if zi.imag == 0.0 or zj.imag == 0.0 or (zi.imag > 0) == (zj.imag > 0):
                continue
            d = _conj_distance(zi, zj)
            if d < conjugate_tol:
                candidates.append((d, i, j))

    used: set[int] = set()
    for _, i, j in sorted(candidates, key=lambda t: (t[0], t[1], t[2])):
        if i in used or j in used:
            continue
        pair_of[i] = j
        pair_of[j] = i
        used.add(i)
        used.add(j)
    return pair_of
