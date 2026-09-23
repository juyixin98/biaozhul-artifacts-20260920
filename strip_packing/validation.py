"""输入校验：数值容差、失败状态与输入范围。

输入范围（硬限制）
------------------
============================  ==========================================
字段                          限制
============================  ==========================================
``strip_width``               (0, MAX_DIM] 内的有限正数
单个矩形宽/高                 (0, MAX_DIM] 内的有限正数
矩形数量                      0..MAX_RECTS（0 为合法空实例）
单个矩形宽                    必须 <= strip_width（否则无法放置，拒绝）
容差 atol / rtol              [0, MAX_TOL] 且有限
============================  ==========================================

**零尺寸拒绝**：宽或高为 0（或负、NaN、无穷）的矩形一律拒绝；
条带宽度为 0 或非有限同样拒绝。所有拒绝抛出 :class:`PackingError`，
其 ``code`` 字段为稳定的机器可读错误码（见 JSON 接口）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MAX_RECTS = 200
MAX_DIM = 1.0e6
MAX_TOL = 1.0e-3


@dataclass(frozen=True)
class Limits:
    """输入范围常量，供外部（测试/文档）引用。"""

    max_rects: int = MAX_RECTS
    max_dim: float = MAX_DIM
    max_tol: float = MAX_TOL


class PackingError(ValueError):
    """输入实例不合法时抛出。

    属性
    ----
    code:
        稳定错误码字符串：

        - ``INVALID_JSON``        顶层结构/字段类型错误；
        - ``INVALID_WIDTH``       条带宽度非有限正数或超过 MAX_DIM；
        - ``INVALID_RECTANGLE``   某个矩形结构或尺寸非法（含零尺寸）；
        - ``RECTANGLE_TOO_WIDE``  矩形宽度超过条带宽度（超宽拒绝）；
        - ``TOO_MANY_RECTANGLES`` 矩形数量超过 MAX_RECTS；
        - ``INVALID_TOLERANCE``   atol/rtol 越界。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _finite_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(float(v))


def validate_instance(strip_width, rects, atol=1e-9, rtol=1e-9):
    """校验装箱实例，返回规整后的 ``(W, [(id, w, h), ...])``。

    id 缺省时使用矩形在列表中的 0 基序号。校验失败抛
    :class:`PackingError`。
    """
    if not _finite_number(strip_width) or float(strip_width) <= 0:
        raise PackingError(
            "INVALID_WIDTH",
            f"strip_width 必须为有限正数，收到 {strip_width!r}")
    W = float(strip_width)
    if W > MAX_DIM:
        raise PackingError(
            "INVALID_WIDTH",
            f"strip_width={W:g} 超过上限 {MAX_DIM:g}")

    if not _finite_number(atol) or not _finite_number(rtol):
        raise PackingError(
            "INVALID_TOLERANCE", f"容差必须为有限数: atol={atol!r}, rtol={rtol!r}")
    if atol < 0 or rtol < 0 or atol > MAX_TOL or rtol > MAX_TOL:
        raise PackingError(
            "INVALID_TOLERANCE",
            f"容差越界: atol={atol:g}, rtol={rtol:g}，允许范围 [0, {MAX_TOL:g}]")

    if not isinstance(rects, (list, tuple)):
        raise PackingError(
            "INVALID_RECTANGLE", f"rectangles 必须为数组，收到 {type(rects).__name__}")
    if len(rects) > MAX_RECTS:
        raise PackingError(
            "TOO_MANY_RECTANGLES",
            f"矩形数量 {len(rects)} 超过上限 {MAX_RECTS}（本项目仅支持小中规模）")

    norm = []
    for idx, r in enumerate(rects):
        if not isinstance(r, dict):
            raise PackingError(
                "INVALID_RECTANGLE", f"rectangles[{idx}] 必须为对象")
        rid = r.get("id", idx)
        if "width" not in r or "height" not in r:
            raise PackingError(
                "INVALID_RECTANGLE",
                f"rectangles[{idx}] (id={rid!r}) 缺少 width/height 字段")
        w, h = r["width"], r["height"]
        if not _finite_number(w) or not _finite_number(h):
            raise PackingError(
                "INVALID_RECTANGLE",
                f"rectangles[{idx}] (id={rid!r}) 的宽高必须为有限数，"
                f"收到 width={w!r}, height={h!r}")
        w, h = float(w), float(h)
        if w <= 0.0 or h <= 0.0:
            raise PackingError(
                "INVALID_RECTANGLE",
                f"rectangles[{idx}] (id={rid!r}) 零尺寸或负尺寸被拒绝: "
                f"width={w:g}, height={h:g}（要求 width>0 且 height>0）")
        if w > MAX_DIM or h > MAX_DIM:
            raise PackingError(
                "INVALID_RECTANGLE",
                f"rectangles[{idx}] (id={rid!r}) 尺寸超过上限 {MAX_DIM:g}")
        if w > W + (atol + rtol * max(1.0, W)):
            raise PackingError(
                "RECTANGLE_TOO_WIDE",
                f"rectangles[{idx}] (id={rid!r}) 宽度 {w:g} 超过条带宽度 "
                f"{W:g}，且矩形不允许旋转，无法放置")
        norm.append((rid, w, h))

    return W, norm
