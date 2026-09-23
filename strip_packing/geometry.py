"""几何原语：容差、碰撞检测与布局校验。

坐标系统
--------
条带左下角为原点 ``(0, 0)``，x 轴向右（宽度方向，固定宽度 W），
y 轴向上（高度方向，待最小化）。矩形 r 的放置为 ``(x, y, w, h)``，
占据闭区域 ``[x, x+w] × [y, y+h]``（共享边界不算重叠）。

数值容差
--------
装箱实例的输入为有限非负实数。浮点运算下，两个本应恰好接触的矩形
可能出现微小的负间隙（例如 ``-1e-15``）。本模块用 **绝对+相对**
容差判定重叠：

    eps = atol + rtol * max(1.0, 参与比较的特征尺度)

- 水平方向特征尺度取 ``max(w1, w2, strip_width)``；
- 垂直方向特征尺度取 ``max(h1, h2, 参考高度)``。

仅当 x、y 两个方向的区间重叠量都严格大于 eps 时才报告碰撞。
因此恰有正间隙 / 共享边界的合法布局不会被误判；真正的重叠
（量级远大于 eps）一定会被检出。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Tolerance:
    """重叠判定容差。

    属性
    ----
    atol:
        绝对容差，默认 1e-9。
    rtol:
        相对容差，默认 1e-9。有效容差随参与比较的特征尺度放大，
        以容纳大坐标下的浮点舍入。
    """

    atol: float = 1e-9
    rtol: float = 1e-9

    def eps_for(self, scale: float) -> float:
        """给定特征尺度下的有效容差。"""
        return self.atol + self.rtol * max(1.0, float(scale))


@dataclass(frozen=True)
class Placement:
    """单个矩形的放置结果。"""

    id: object
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def top(self) -> float:
        return self.y + self.h


def rects_overlap(p1: Placement, p2: Placement,
                  tol: Tolerance = Tolerance()) -> bool:
    """两个放置是否在面积意义上重叠（共享边界不算重叠）。

    重叠量 ``ov = min(右端点) - max(左端点)``；仅当两个方向的
    重叠量都严格大于对应有效容差时返回 ``True``。
    """
    x_scale = max(p1.w, p2.w, p1.x, p2.x,
                  p1.right, p2.right)
    y_scale = max(p1.h, p2.h, p1.y, p2.y,
                  p1.top, p2.top)
    ox = min(p1.right, p2.right) - max(p1.x, p2.x)
    oy = min(p1.top, p2.top) - max(p1.y, p2.y)
    return (ox > tol.eps_for(x_scale)
            and oy > tol.eps_for(y_scale))


def verify_layout(rects, placements, strip_width: float,
                  tol: Tolerance = Tolerance()):
    """校验一份布局是否合法。

    检查项：
      1. 每个矩形在条带内（x >= 0、右端点 <= W、y >= 0）；
      2. 任意两个矩形面积不重叠；
      3. 放置数量与矩形数量一致，顺序一一对应。

    参数
    ----
    rects:
        输入矩形列表，每项为 ``(id, w, h)``。
    placements:
        :class:`Placement` 列表。
    strip_width:
        条带固定宽度 W。

    返回
    ----
    dict
        ``{"valid": bool, "height": 布局最高上沿, "violations": [...]}``，
        violations 为人类可读的违例描述（碰撞条目形如
        ``"collision: id_i 与 id_j"``）。
    """
    violations = []
    height = 0.0

    if len(placements) != len(rects):
        violations.append(
            f"placement count {len(placements)} != rectangle count {len(rects)}")
        return {"valid": False, "height": height, "violations": violations}

    x_scale_w = max([strip_width] + [p.w for p in placements] +
                    [p.right for p in placements])
    y_scale_h = max([1.0] + [p.h for p in placements] +
                    [p.top for p in placements])
    eps_x = tol.eps_for(x_scale_w)
    eps_y = tol.eps_for(y_scale_h)

    for (rid, rw, rh), p in zip(rects, placements):
        if p.x < -eps_x:
            violations.append(f"{p.id!r}: x={p.x:g} 超出条带左边界")
        if p.y < -eps_y:
            violations.append(f"{p.id!r}: y={p.y:g} 超出条带底边")
        if p.right > strip_width + eps_x:
            violations.append(
                f"{p.id!r}: 右端点 x+w={p.right:g} 超出条带宽度 {strip_width:g}")
        # 尺寸必须与输入一致（不旋转：宽高不交换）
        if abs(p.w - rw) > eps_x or abs(p.h - rh) > eps_y:
            violations.append(
                f"{p.id!r}: 放置尺寸 {p.w:g}x{p.h:g} 与输入 {rw:g}x{rh:g} 不一致")
        height = max(height, p.top)

    n = len(placements)
    for i in range(n):
        pi = placements[i]
        for j in range(i + 1, n):
            pj = placements[j]
            ox = min(pi.right, pj.right) - max(pi.x, pj.x)
            oy = min(pi.top, pj.top) - max(pi.y, pj.y)
            if ox > eps_x and oy > eps_y:
                violations.append(f"collision: {pi.id!r} 与 {pj.id!r}")

    return {"valid": not violations, "height": height,
            "violations": violations}
