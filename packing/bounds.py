"""输入校验、范围限制与装箱下界。

本模块只计算 *可证明有效（valid）* 的下界：任何合法布局高度 H 都必须 >= LB。
启发式高度在 :mod:`packing.heuristics` 中，二者严格区分，绝不在输出中把
启发式结果称作最优。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geometry import EPS, is_positive_size

# ---- 公开的输入范围限制（小/中规模纯后端，拒绝超规模以防资源耗尽）----
MAX_STRIP_WIDTH = 1.0e6
MAX_DIMENSION = 1.0e6          # 单个矩形宽/高上限
MAX_RECTANGLES = 300           # 矩形数量硬上限
MAX_AREA = 1.0e12              # 单个矩形面积上限（乘积溢出防护）
EXACT_MAX_RECTS = 8            # 精确/穷举允许的最大矩形数
EXACT_MAX_COORD = 40           # 精确/穷举坐标整数上限（候选位置组合爆炸防护）


class ValidationError(ValueError):
    """请求不合法（对应 JSON 响应 status=error）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Instance:
    """校验通过后的装箱实例。矩形不允许旋转，宽高顺序固定。"""

    strip_width: float
    widths: np.ndarray   # shape (n,) float64
    heights: np.ndarray

    @property
    def n(self) -> int:
        return int(self.widths.shape[0])


def validate_request(req: dict) -> Instance:
    """校验并归一化一个 JSON 请求体。

    接受格式::

        {"strip_width": number,
         "rectangles": [{"width": w, "height": h}, ...]}

    错误一律抛 :class:`ValidationError`，带稳定的机器可读 ``code``：

    * ``invalid_type``        顶层或字段类型错
    * ``missing_field``       缺 strip_width / rectangles
    * ``bad_strip_width``     条带宽 <= 0、NaN/Inf 或超范围
    * ``empty_rectangles``    矩形列表为空
    * ``too_many_rectangles`` 超过 :data:`MAX_RECTANGLES`
    * ``bad_rectangle_size``  某矩形宽/高 <= EPS、NaN/Inf 或超范围（含零尺寸）
    * ``rectangle_too_wide``  某矩形宽度 > 条带宽（无论怎么放都放不下）
    * ``area_too_large``      单个矩形面积超过 :data:`MAX_AREA`
    """
    if not isinstance(req, dict):
        raise ValidationError("invalid_type", "请求体必须是 JSON 对象")

    if "strip_width" not in req:
        raise ValidationError("missing_field", "缺少字段 strip_width")
    if "rectangles" not in req:
        raise ValidationError("missing_field", "缺少字段 rectangles")

    sw = req["strip_width"]
    if isinstance(sw, bool) or not isinstance(sw, (int, float)):
        raise ValidationError("bad_strip_width", "strip_width 必须是数字")
    sw = float(sw)
    if not np.isfinite(sw):
        raise ValidationError("bad_strip_width", "strip_width 必须是有限数")
    if not is_positive_size(sw):
        raise ValidationError("bad_strip_width", "strip_width 必须为正数（容差 %.0e）" % EPS)
    if sw > MAX_STRIP_WIDTH:
        raise ValidationError("bad_strip_width", "strip_width 超过上限 %g" % MAX_STRIP_WIDTH)

    rects = req["rectangles"]
    if not isinstance(rects, list):
        raise ValidationError("invalid_type", "rectangles 必须是数组")
    if len(rects) == 0:
        raise ValidationError("empty_rectangles", "rectangles 不能为空（至少一个矩形）")
    if len(rects) > MAX_RECTANGLES:
        raise ValidationError(
            "too_many_rectangles",
            "矩形数量 %d 超过上限 %d（本服务限定小/中规模）" % (len(rects), MAX_RECTANGLES),
        )

    widths = np.empty(len(rects), dtype=np.float64)
    heights = np.empty(len(rects), dtype=np.float64)
    for i, r in enumerate(rects):
        if not isinstance(r, dict) or "width" not in r or "height" not in r:
            raise ValidationError(
                "invalid_type",
                "rectangles[%d] 必须是含 width/height 的对象" % i,
            )
        w, h = r["width"], r["height"]
        if isinstance(w, bool) or isinstance(h, bool) or not isinstance(w, (int, float)) \
                or not isinstance(h, (int, float)):
            raise ValidationError("bad_rectangle_size",
                                  "rectangles[%d] 的 width/height 必须是数字" % i)
        w, h = float(w), float(h)
        if not (np.isfinite(w) and np.isfinite(h)):
            raise ValidationError("bad_rectangle_size",
                                  "rectangles[%d] 的 width/height 必须有限" % i)
        # 零尺寸（或 <= EPS 的噪声）与负尺寸在此一并拒绝
        if not (is_positive_size(w) and is_positive_size(h)):
            raise ValidationError(
                "bad_rectangle_size",
                "rectangles[%d] 含零或负尺寸：width=%g height=%g（容差 %.0e）"
                % (i, w, h, EPS),
            )
        if w > MAX_DIMENSION or h > MAX_DIMENSION:
            raise ValidationError("bad_rectangle_size",
                                  "rectangles[%d] 宽/高超过上限 %g" % (i, MAX_DIMENSION))
        # 超宽拒绝：不旋转时宽度超过条带宽度的矩形无法放入
        if w > sw + EPS:
            raise ValidationError(
                "rectangle_too_wide",
                "rectangles[%d] 宽度 %g 超过条带宽度 %g（矩形不允许旋转）" % (i, w, sw),
            )
        if w * h > MAX_AREA:
            raise ValidationError("area_too_large",
                                  "rectangles[%d] 面积超过上限 %g" % (i, MAX_AREA))
        widths[i] = w
        heights[i] = h

    return Instance(strip_width=sw, widths=widths, heights=heights)


# ---- 有效下界 ----------------------------------------------------------------
#
# 下面三个界均为 *有效下界*（valid lower bounds），证明随各函数 docstring 给出。
# 对任意合法（不旋转、不重叠、不出条带）布局高度 H，都有 H >= LB。

def area_lower_bound(inst: Instance) -> float:
    """面积下界 ``LB_area = ceil-tol(总矩形面积 / 条带宽度)``。

    有效性：所有矩形都位于宽 W、高 H 的条带内且互不重叠（共边不算），
    故总面积 sum(w_i h_i) <= W·H，即 H >= sum(area)/W。

    注：不做整数上取整——问题是连续的，连续量本身就是有效界。
    输出时仅在 1e-9 容差内清理浮点噪声。
    """
    total_area = float(np.sum(inst.widths * inst.heights))
    lb = total_area / inst.strip_width
    if abs(lb) < EPS:
        lb = 0.0
    return lb


def max_height_lower_bound(inst: Instance) -> float:
    """最大件高下界：``LB_h = max_i h_i``。

    有效性：每个矩形都必须完整放进高 H 的条带，故 H >= 任一 h_i。
    """
    return float(np.max(inst.heights))


def wide_items_lower_bound(inst: Instance) -> float:
    """宽件下界（Martello 类 L2 的退化/经典形式）。

    取所有宽度严格大于条带宽度一半（``w_i > W/2``，容差 EPS）的矩形。
    任意两个这样的矩形在水平投影上必然正长度重叠（因为 w_i + w_j > W），
    所以它们在任何布局中都不能处于同一水平带（y 区间不能重叠），
    其高度必须在竖直方向上累加：

        H >= sum_{i: w_i > W/2} h_i

    宽度恰为 W/2 的两个矩形可以并排贴合（w_i + w_j == W，无正长度交集），
    故用严格 ``>``（配合 EPS）。

    这是一个独立于面积界与最大件高界的有效下界。
    """
    w = inst.widths
    h = inst.heights
    half = inst.strip_width / 2.0
    mask = w > half + EPS
    return float(np.sum(h[mask]))


def all_lower_bounds(inst: Instance) -> dict[str, float]:
    """计算全部已实现的有效下界，并给出取最大值的组合界。

    返回 ``{"area": .., "max_height": .., "wide_items": .., "combined": ..}``。
    ``combined`` 是三者最大值，因此仍是有效下界，且通常最紧。
    """
    lbs = {
        "area": area_lower_bound(inst),
        "max_height": max_height_lower_bound(inst),
        "wide_items": wide_items_lower_bound(inst),
    }
    lbs["combined"] = max(lbs.values())
    return lbs
