"""JSON 接口编排层（纯函数，不绑定任何 Web 框架）。

请求::

    {
      "strip_width": 10,
      "rectangles": [{"width": 3, "height": 4}, ...],
      "compute_exact": false        // 可选，见下
    }

``compute_exact`` 为 ``true`` 时额外用候选位置 DFS 求真实最优高度；
仅限 n <= 8 且各整数尺寸 <= 40 的算例，否则该字段以 ``status="skipped"``
返回原因，**不影响** 主结果。

成功响应::

    {
      "status": "ok",
      "input_summary": {"strip_width": 10.0, "num_rectangles": 4},
      "lower_bounds": {"area": ..., "max_height": ..., "wide_items": ...,
                       "combined": ...},
      "heuristic": {"height": ..., "method": ...,
                    "guarantee": "heuristic (not proven optimal)",
                    "placements": [{"id": 0, "x": .., "y": ..,
                                    "width": .., "height": ..}, ...]},
      "layout_verification": {"ok": true, "height": ..., "collisions": [], ...},
      "gap": {"heuristic_minus_lower_bound": ..., "proven_optimal": false},
      "exact": {"status": "skipped", "reason": "..."}   // 仅 compute_exact=true 时
    }

失败响应::

    {"status": "error", "error": {"code": "...", "message": "..."}}

重要语义：``heuristic.height`` 是启发式上界，**从未** 被声明为全局最优；
``lower_bounds.combined`` 是可证明的有效下界。两者之间可能存在差距。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .bounds import all_lower_bounds, validate_request
from .exact import solve_exact
from .geometry import EPS
from .heuristics import best_heuristic
from .layout import verify_layout

OUT_DECIMALS = 9  # JSON 输出保留位数，避免 2.0000000000000004 类噪声


def _r(v: float) -> float:
    """输出前清洗浮点噪声并四舍五入到 :data:`OUT_DECIMALS` 位。"""
    v = float(v)
    if abs(v) < EPS:
        return 0.0
    return round(v, OUT_DECIMALS)


def error_response(code: str, message: str, http_status: int = 400) -> dict:
    return {"status": "error", "error": {"code": code, "message": message}}, http_status


def solve(req: dict) -> tuple[dict[str, Any], int]:
    """处理一个已解析为 dict 的请求，返回 ``(响应dict, 状态码)``。

    状态码：200 成功；400 输入不合法（含零尺寸/超宽/超范围/超规模）。
    本函数不抛 :class:`~packing.bounds.ValidationError`。
    """
    try:
        inst = validate_request(req)
    except Exception as e:
        # validate_request 只抛 ValidationError；防御性地兜底
        code = getattr(e, "code", "invalid_type")
        message = getattr(e, "message", str(e))
        return {"status": "error", "error": {"code": code, "message": message}}, 400

    lbs = all_lower_bounds(inst)
    heur = best_heuristic(inst)
    report = verify_layout(inst, heur.x, heur.y)

    # 自检：启发式布局必须合法；下界必须 <= 启发式高度。
    # 若内部不变量被破坏，返回 failed 状态而不是悄悄给出错误答案。
    if not report.ok:
        return {
            "status": "failed",
            "error": {
                "code": "internal_layout_invalid",
                "message": "启发式生成的布局未通过自检（出界/重叠/负坐标），不应发生",
                "detail": report.as_dict(),
            },
        }, 500
    if abs(report.height - heur.height) > EPS:
        return {
            "status": "failed",
            "error": {
                "code": "internal_height_mismatch",
                "message": "布局实际高度与启发式报告高度不一致，不应发生",
            },
        }, 500
    if lbs["combined"] > heur.height + EPS:
        return {
            "status": "failed",
            "error": {
                "code": "internal_bound_violation",
                "message": "有效下界超过启发式高度（界或布局其一有误），不应发生",
            },
        }, 500

    placements = [
        {
            "id": i,
            "x": _r(heur.x[i]),
            "y": _r(heur.y[i]),
            "width": _r(inst.widths[i]),
            "height": _r(inst.heights[i]),
        }
        for i in range(inst.n)
    ]

    resp: dict[str, Any] = {
        "status": "ok",
        "input_summary": {
            "strip_width": _r(inst.strip_width),
            "num_rectangles": inst.n,
        },
        "lower_bounds": {k: _r(v) for k, v in lbs.items()},
        "heuristic": {
            "height": _r(heur.height),
            "method": heur.method,
            "guarantee": heur.guarantee,
            "placements": placements,
        },
        "layout_verification": report.as_dict() | {"height": _r(report.height)},
        "gap": {
            "heuristic_minus_lower_bound": _r(heur.height - lbs["combined"]),
            "note": "gap=0 仅说明启发式高度达到有效下界、可间接证明该算例最优；"
                    "gap>0 时真实最优可能介于下界与启发式高度之间，启发式不保证最优。",
            "proven_optimal": bool(abs(heur.height - lbs["combined"]) <= EPS),
        },
    }

    if bool(req.get("compute_exact", False)) if isinstance(req, dict) else False:
        ex = solve_exact(inst)
        if ex.status == "optimal":
            assert ex.x is not None and ex.y is not None
            ex_report = verify_layout(inst, ex.x, ex.y)
            resp["exact"] = {
                "status": "optimal",
                "height": _r(ex.height),
                "placements": [
                    {"id": i, "x": _r(ex.x[i]), "y": _r(ex.y[i])}
                    for i in range(inst.n)
                ],
                "layout_verification": ex_report.as_dict()
                | {"height": _r(ex_report.height)},
                "note": "该高度由子集和坐标 + 位掩码 DFS 穷举证明为真实最优（仅限小整数算例）。",
            }
            # 记录启发式相对真实最优的差距（诊断用，不改变主结果语义）
            resp["gap"]["heuristic_minus_exact_optimum"] = _r(heur.height - ex.height)
        else:
            resp["exact"] = {"status": ex.status, "reason": ex.reason}

    return resp, 200
