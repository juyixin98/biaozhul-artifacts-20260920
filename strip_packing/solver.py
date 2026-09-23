"""求解编排：校验 → 下界 → 启发式（上界）→ 可选精确求解。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .geometry import Tolerance, verify_layout
from .validation import validate_instance
from .lower_bounds import lower_bounds
from .heuristics import run_heuristics
from .exact import exact_packing

EXACT_AUTO_N = 6
EXACT_NODE_LIMIT = 2_000_000
EXACT_TIME_LIMIT = 10.0


@dataclass
class PackingSolution:
    """求解结果（与 JSON 响应的字段一一对应）。"""

    status: str
    strip_width: float
    num_rectangles: int
    lower_bound: float
    lower_bounds_detail: dict
    heuristic_height: float
    heuristic_name: str
    placements: list
    gap: float
    gap_ratio: Optional[float]
    exact: Optional[dict]
    verification: dict


def solve_packing(payload: dict) -> PackingSolution:
    """执行一次完整求解。

    payload 结构见 :mod:`strip_packing.api` 与 ``examples/``。
    ``exact`` 字段：``"auto"``（n<=8 时求精确解，默认）、``True``
    （强制，超限仍拒绝执行并返回 limit_reached 之前的常规结果）、
    ``False``（仅启发式）。
    """
    if not isinstance(payload, dict):
        raise _type_error(payload)

    if "strip_width" not in payload:
        from .validation import PackingError
        raise PackingError("INVALID_JSON", "缺少顶层字段 strip_width")
    if "rectangles" not in payload:
        from .validation import PackingError
        raise PackingError("INVALID_JSON", "缺少顶层字段 rectangles")

    tol_cfg = payload.get("tolerance", {})
    if not isinstance(tol_cfg, dict):
        from .validation import PackingError
        raise PackingError("INVALID_TOLERANCE", "tolerance 必须为对象")
    atol = tol_cfg.get("atol", 1e-9)
    rtol = tol_cfg.get("rtol", 1e-9)

    W, rects = validate_instance(payload["strip_width"],
                                 payload["rectangles"], atol, rtol)
    n = len(rects)
    tol = Tolerance(float(atol), float(rtol))

    # 1) 严格下界
    lbs = lower_bounds(rects, W)
    lb = lbs["lower_bound"]

    # 2) 启发式（上界），所有产出均经过碰撞/边界复核
    cand = run_heuristics(rects, W, tol)
    if n == 0:
        best = {"name": "empty", "height": 0.0, "placements": []}
    else:
        best = cand[0]

    # 3) 可选精确求解
    exact_mode = payload.get("exact", "auto")
    run_exact = exact_mode is True or (
        exact_mode == "auto" and n <= EXACT_AUTO_N)
    exact_result = None
    status = "ok"
    if run_exact and n > 0:
        exact_result = exact_packing(
            rects, W, best["height"], global_lb=lb,
            node_limit=EXACT_NODE_LIMIT, time_limit=EXACT_TIME_LIMIT)
        if exact_result["status"] == "optimal" \
                and exact_result["placements"] is not None:
            rep = verify_layout(rects, exact_result["placements"], W, tol)
            if not rep["valid"]:
                from .validation import PackingError
                raise PackingError(
                    "LAYOUT_VERIFICATION_FAILED",
                    "精确解布局复核失败: " + "; ".join(rep["violations"][:5]))
            exact_result["verified_height"] = rep["height"]
            best = {"name": "EXACT",
                    "height": rep["height"],
                    "placements": exact_result["placements"]}
        elif exact_result["status"] == "optimal":
            # 分支定界证明了最优高度值，但没有单独重建布局：
            # proved_by 为下界夹逼或"值等于启发式上界"。两种情形下
            # 当前 best（已复核的启发式布局）都达到该最优高度。
            proved_h = exact_result.get("proved_height", best["height"])
            exact_result["proved_height"] = proved_h
        elif exact_result["status"] == "limit_reached":
            status = "ok_exact_limit_reached"

    upper = best["height"]
    gap = max(0.0, upper - lb)
    gap_ratio = (gap / upper) if upper > 0.0 else (0.0 if lb == 0.0 else None)

    verification = verify_layout(rects, best["placements"], W, tol)

    placements_out = [
        {"id": p.id, "x": p.x, "y": p.y, "width": p.w, "height": p.h}
        for p in best["placements"]
    ]
    exact_out = None
    if exact_result is not None:
        exact_out = {
            "requested": True,
            "status": exact_result["status"],
            "optimal": exact_result["optimal"],
            "nodes_explored": exact_result["nodes"],
            "elapsed_sec": round(exact_result["elapsed_sec"], 6),
            "node_limit": exact_result["node_limit"],
            "time_limit_sec": exact_result["time_limit"],
        }
    elif exact_mode is False or exact_mode == "auto":
        exact_out = {"requested": exact_mode is True,
                     "status": "not_run", "optimal": False}
    else:
        exact_out = {"requested": True, "status": "not_run",
                     "optimal": False,
                     "reason": f"n>{EXACT_AUTO_N} 且未强制"}

    return PackingSolution(
        status=status,
        strip_width=W,
        num_rectangles=n,
        lower_bound=lb,
        lower_bounds_detail=lbs,
        heuristic_height=upper,
        heuristic_name=best["name"],
        placements=placements_out,
        gap=gap,
        gap_ratio=gap_ratio,
        exact=exact_out,
        verification={"valid": verification["valid"],
                      "violations": verification["violations"]},
    )


def _type_error(payload):
    from .validation import PackingError
    return PackingError("INVALID_JSON",
                        f"请求体必须为 JSON 对象，收到 {type(payload).__name__}")
