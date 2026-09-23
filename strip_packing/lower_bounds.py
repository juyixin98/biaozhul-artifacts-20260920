"""条带装箱高度的严格下界。

设最优装箱高度为 H*（矩形不旋转、固定条带宽 W）。本模块给出三个
**可证明不超过 H*** 的下界，取最大值返回。下界永远不会高估最优值；
启发式高度（上界）与下界之间的差距是真实差距，不归因于最优解。

1. 面积下界  ``L_area = Σ w_i h_i / W``
   矩形总面积不超过条带被占用部分的面积 W·H*。
2. 最大高度  ``L_max = max_i h_i``
   每个矩形必须完整放入，高度方向至少容纳最高矩形。
3. 两两下界  ``L_pair = max{ h_i + h_j | i<j, w_i + w_j > W }``
   若两个矩形宽度之和 > W，它们在水平方向不可能完全并排：
   不存在一条水平线同时穿过二者而不超宽（若存在公共水平线，
   该线上两矩形占用总宽 > W，矛盾）。因此它们的垂直投影区间
   不相交，一上一下，总高度至少 h_i + h_j。

说明：界 3 的宽度判据使用数学上的严格大于 ``w_i + w_j > W``，
不施加任何容差"放松"（放松只会让下界有被高估的风险）。
"""

from __future__ import annotations

import numpy as np


def lower_bounds(rects, strip_width: float) -> dict:
    """计算三个下界及其最大值。

    参数
    ----
    rects:
        已校验的矩形列表 ``[(id, w, h), ...]``。
    strip_width:
        条带宽度 W。

    返回
    ----
    dict
        ``{"area", "max_height", "pairwise", "lower_bound"}``，
        空实例的所有下界均为 0。
    """
    W = float(strip_width)
    n = len(rects)
    if n == 0:
        return {"area": 0.0, "max_height": 0.0,
                "pairwise": 0.0, "lower_bound": 0.0}

    ids = [r[0] for r in rects]
    w = np.array([r[1] for r in rects], dtype=float)
    h = np.array([r[2] for r in rects], dtype=float)

    # 1. 面积下界
    lb_area = float(np.dot(w, h) / W)

    # 2. 最大高度下界
    lb_max = float(np.max(h))

    # 3. 两两下界：宽度之和严格大于 W 的矩形对必须上下叠放
    lb_pair = 0.0
    if n >= 2:
        ws = w[:, None] + w[None, :]
        hs = h[:, None] + h[None, :]
        iu, ju = np.triu_indices(n, k=1)
        mask = ws[iu, ju] > W
        if np.any(mask):
            lb_pair = float(np.max(hs[iu[mask], ju[mask]]))

    lb = max(lb_area, lb_max, lb_pair)
    return {"area": lb_area, "max_height": lb_max,
            "pairwise": lb_pair, "lower_bound": lb}
