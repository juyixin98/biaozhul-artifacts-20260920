"""数值容差与双目标支配关系。

两目标（时间 t、费用 c）均假定 **非负**。由于权重按 JSON 数字读入为 IEEE-754
float64，累计和可能产生舍入误差，因此支配判定不使用严格的 ``<=``，而采用
“容差带”判定。

容差尺度定义为::

    s = max(1.0, |t1|, |t2|, |c1|, |c2|)

容差为 ``eps * s``（默认 eps=1e-9）。即权重在 1 附近时容差约 1e-9，权重在
1e6 量级时容差约 1e-3，属于相对+绝对混合容差。

记 (t1,c1) 为候选（新标签），(t2,c2) 为已有标签：

* ``weak_le``  : 两维都“小于等于（容差内）”
* ``strict_lt``: 至少一维严格更小（差值超过容差带）

于是：

* weak_le(a,b) and strict_lt(a,b)  ⇒ a 支配 b
* weak_le 两个方向都成立且两个方向都不 strict ⇒ 两标签在容差内 **相等**（重复）
"""

from __future__ import annotations

import numpy as np

DEFAULT_EPS = 1e-9
MIN_EPS = 0.0
MAX_EPS = 1e-2


def tolerance_scale(t1: float, c1: float, t2: float, c2: float) -> float:
    """容差尺度 s（见模块说明）。"""
    return float(max(1.0, abs(t1), abs(t2), abs(c1), abs(c2)))


def _tol(eps: float, scale: float) -> float:
    return eps * scale


def weak_le(t1: float, c1: float, t2: float, c2: float, eps: float) -> bool:
    """候选 (t1,c1) 是否在容差带内逐维 ≤ (t2,c2)。"""
    tol = _tol(eps, tolerance_scale(t1, c1, t2, c2))
    return (t1 <= t2 + tol) and (c1 <= c2 + tol)


def strict_lt(t1: float, c1: float, t2: float, c2: float, eps: float) -> bool:
    """候选是否至少有一维严格小于 (t2,c2)（超出容差带）。"""
    tol = _tol(eps, tolerance_scale(t1, c1, t2, c2))
    return (t1 < t2 - tol) or (c1 < c2 - tol)


def dominates(t1: float, c1: float, t2: float, c2: float, eps: float) -> bool:
    """(t1,c1) 是否支配 (t2,c2)。"""
    return weak_le(t1, c1, t2, c2, eps) and strict_lt(
        t1, c1, t2, c2, eps
    )


def weights_equal(t1: float, c1: float, t2: float, c2: float, eps: float) -> bool:
    """两对权重是否在容差内相等（两维都落在容差带内）。"""
    tol = _tol(eps, tolerance_scale(t1, c1, t2, c2))
    return abs(t1 - t2) <= tol and abs(c1 - c2) <= tol


def pareto_front(weight_pairs, eps: float = DEFAULT_EPS):
    """对一组 (time, cost) 权重做 Pareto 过滤（参考实现/测试用）。

    :returns: 保留下来的下标列表（按 time 升序）。
    """
    pts = sorted(enumerate(weight_pairs), key=lambda kv: (kv[1][0], kv[1][1]))
    kept = []
    for orig_idx, (t, c) in pts:
        # 被任一已有保留点支配则丢弃；权重相等的点都保留（与求解器“相等标签共存”
        # 语义一致），调用方需要去重时可自行按 weights_equal 合并。
        dominated = any(
            weak_le(tk, ck, t, c, eps) and strict_lt(tk, ck, t, c, eps)
            for _, (tk, ck) in kept
        )
        if not dominated:
            kept.append((orig_idx, (t, c)))
    return [orig_idx for orig_idx, _ in kept]


def finite_nonneg(x: float) -> bool:
    """权重合法性：有限且非负。"""
    v = float(x)
    return bool(np.isfinite(v)) and v >= 0.0
