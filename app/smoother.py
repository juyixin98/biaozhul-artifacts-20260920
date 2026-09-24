"""平滑服务管线：校验、归一化、SLSQP 求解、整段精确碰撞验证、失败回退。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np

from .geometry import validate_trajectory
from .optimizer import solve_smoothing
from .preprocess import (
    characteristic_length,
    expand_with_map,
    merge_consecutive_duplicates,
)

MAX_POINTS = 100
MAX_ITER_BUDGET = 500  # 明确的迭代预算硬上限

RUNS_DIR = Path(
    os.environ.get("TRAJECTORY_RUNS_DIR", str(Path(__file__).resolve().parent.parent / "runs"))
)


class ValidationError(ValueError):
    """请求输入非法（由 API 层映射为 422）。"""


def _check_request(req: dict) -> tuple[np.ndarray, list[tuple[float, float, float, float]]]:
    pts = req.get("points")
    if not isinstance(pts, list) or not (2 <= len(pts) <= MAX_POINTS):
        raise ValidationError(f"points 必须是长度 2..{MAX_POINTS} 的列表")
    arr = np.empty((len(pts), 2), dtype=float)
    for i, p in enumerate(pts):
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ValidationError(f"points[{i}] 必须是 [x, y]")
        for v in p:
            if not isinstance(v, (int, float)) or not np.isfinite(v):
                raise ValidationError(f"points[{i}] 含非有限数值")
        arr[i] = (float(p[0]), float(p[1]))

    obstacles = []
    for j, o in enumerate(req.get("obstacles", [])):
        if not isinstance(o, (list, tuple)) or len(o) != 4:
            raise ValidationError(f"obstacles[{j}] 必须是 [xmin,ymin,xmax,ymax]")
        if not all(isinstance(v, (int, float)) and np.isfinite(v) for v in o):
            raise ValidationError(f"obstacles[{j}] 含非有限数值")
        xmin, ymin, xmax, ymax = (float(v) for v in o)
        if not (xmin < xmax and ymin < ymax):
            raise ValidationError(f"obstacles[{j}] 要求 xmin<xmax 且 ymin<ymax")
        obstacles.append((xmin, ymin, xmax, ymax))

    for name in ("deviation_bound", "safety_margin", "max_curvature", "max_iter"):
        v = req.get(name)
        if v is not None and not (isinstance(v, (int, float)) and np.isfinite(v)):
            raise ValidationError(f"{name} 必须是有限数")

    if req.get("deviation_bound", 0) < 0:
        raise ValidationError("deviation_bound 不能为负")
    if req.get("safety_margin", 0) < 0:
        raise ValidationError("safety_margin 不能为负")
    if req.get("max_curvature", 1) <= 0:
        raise ValidationError("max_curvature 必须为正")
    if not (1 <= int(req.get("max_iter", 150)) <= MAX_ITER_BUDGET):
        raise ValidationError(f"max_iter 必须在 1..{MAX_ITER_BUDGET} 之间")
    return arr, obstacles


def _points_json(pts: np.ndarray) -> list[list[float]]:
    return [[float(x), float(y)] for x, y in np.asarray(pts, dtype=float)]


def _gate_failures(residuals_norm: dict, scale: float, deviation_bound: float,
                   max_curvature: float) -> list[str]:
    """独立残差门禁：不采信优化器自报的 success，任何一项不过都视为失败。"""
    tol = scale * 1.0e-6
    failures = []
    if residuals_norm["deviation_max_norm"] * scale > deviation_bound + tol:
        failures.append("deviation_bound_violated")
    if residuals_norm["curvature_max_norm"] / scale > max_curvature + 1.0e-7 / scale:
        failures.append("curvature_limit_violated")
    return failures


def smooth_trajectory(req: dict) -> dict:
    raw_points, obstacles_world = _check_request(req)
    start_wall = time.perf_counter()

    deviation_bound = float(req.get("deviation_bound", 0.0))
    safety_margin = float(req.get("safety_margin", 0.0))
    max_curvature = float(req.get("max_curvature", 1.0))
    max_iter = int(req.get("max_iter", 150))
    save_artifact = bool(req.get("save_run", True))

    original_payload = _points_json(raw_points)

    # 1) 合并连续重复点（尺度相关容差）。
    scale = characteristic_length(raw_points)
    dedup = merge_consecutive_duplicates(raw_points, tol=1.0e-9 * scale)
    pts = dedup.points

    # 2) 先校验原始路径；固定端点落入障碍/安全距离时无法平滑修复，直接失败。
    pre_clearance, pre_events = validate_trajectory(pts, obstacles_world, safety_margin)
    if pre_events:
        return _failure(
            original_payload,
            reason="original_path_in_collision",
            detail=f"{len(pre_events)} 个线段侵入障碍或不满足安全余量",
            min_clearance=pre_clearance,
            scale=scale,
            elapsed=time.perf_counter() - start_wall,
            req=req,
            save_artifact=save_artifact,
        )

    # 3) 归一化到特征尺度（除以包围盒对角线），改善跨尺度数值条件。
    p_norm = pts / scale
    o_norm = [tuple(v / scale for v in o) for o in obstacles_world]
    bound_n = deviation_bound / scale
    margin_n = safety_margin / scale
    curv_n = max_curvature * scale

    def respond(points_world, success, reason, solve_runs, gate_failures=None,
                min_clearance=None, detail=None):
        elapsed = time.perf_counter() - start_wall
        out_pts = expand_with_map(points_world, dedup.index_map)
        clearance_field = (
            float(min_clearance)
            if min_clearance is not None and not np.isinf(min_clearance)
            else None
        )
        resp = {
            "success": success,
            "result": "smoothed" if success else "original",
            "reason": reason,
            "points": _points_json(out_pts if success else raw_points),
            "n_points": len(raw_points),
            "n_unique_consecutive": dedup.unique_count,
            "endpoints_fixed": True,
            "limits": {
                "max_points": MAX_POINTS,
                "max_iter": max_iter,
                "max_iter_hard_limit": MAX_ITER_BUDGET,
            },
            "solve_runs": solve_runs,
            "gate_failures": gate_failures or [],
            "min_clearance_world": clearance_field,
            "elapsed_seconds": float(elapsed),
            "detail": detail,
        }
        if success and solve_runs:
            last = solve_runs[-1]
            resp["metrics"] = {
                "objective_initial": last["objective_history"][0],
                "objective_final": last["objective_history"][-1],
                "objective_history": last["objective_history"],
                "residuals_norm": last["residuals_norm"],
                "residuals_world": last["residuals_world"],
                "iterations_total": sum(r["iterations"] for r in solve_runs),
            }
        resp["integrity_sha256"] = _integrity(resp)
        if save_artifact:
            _save_run(req, resp, success, reason)
        return resp

    # 点太少（去重后 <=2）：没有可移动节点，原样返回（已通过碰撞预检）。
    if len(p_norm) <= 2:
        return respond(
            pts, success=True, reason="trivial_path_no_degrees_of_freedom",
            solve_runs=[], gate_failures=[], min_clearance=pre_clearance,
        )

    # 4) 第一次求解（迭代预算 = max_iter）。
    res1, samples1 = solve_smoothing(
        p_norm, o_norm, bound_n, margin_n, curv_n, max_iter=max_iter
    )
    runs_meta = [_run_meta(res1, scale, attempt=1)]

    def finalize(res):
        y = res.points * scale
        gate = _gate_failures(res.residuals, scale, deviation_bound, max_curvature)
        min_clr, events = validate_trajectory(y, obstacles_world, safety_margin)
        if events:
            gate.append("trajectory_collision")
        if gate:
            return None, gate, min_clr, events
        return y, gate, min_clr, events

    if res1.converged:
        y_world, gate, min_clr, events = finalize(res1)
        if y_world is not None:
            return respond(y_world, True, "converged", runs_meta, gate, min_clr)
    else:
        gate = ["solver_not_converged"]
        events = []
        min_clr = None

    # 5) 未收敛不重试；碰撞/门禁失败且仍有迭代预算时，在最深侵入点补采样后重试一次。
    if res1.converged and "trajectory_collision" in gate and res1.iterations < max_iter:
        extra = []
        for ev in events:
            if ev.t >= 0.0:
                extra.append((ev.segment_index, ev.t, ev.obstacle_index))
        # 两轮主迭代合计严格不超过用户预算。
        retry_budget = max_iter - res1.iterations
        res2, _ = solve_smoothing(
            p_norm, o_norm, bound_n, margin_n, curv_n,
            max_iter=retry_budget, extra_samples=extra,
        )
        runs_meta.append(_run_meta(res2, scale, attempt=2, extra_samples=extra))
        if res2.converged:
            y_world, gate2, min_clr2, events2 = finalize(res2)
            if y_world is not None:
                return respond(y_world, True, "converged_after_resample", runs_meta,
                               gate2, min_clr2)
            gate, min_clr = gate2, min_clr2
        else:
            gate = ["solver_not_converged_after_resample"]
            min_clr = None

    reason_map = {
        "trajectory_collision": "collision_after_solve",
        "deviation_bound_violated": "deviation_constraint_unsatisfied",
        "curvature_limit_violated": "curvature_constraint_unsatisfied",
        "solver_not_converged": "solver_not_converged",
        "solver_not_converged_after_resample": "solver_not_converged",
    }
    reason = reason_map.get(gate[0], "infeasible")
    return respond(
        pts, False, reason, runs_meta, gate, min_clr,
        detail="；".join(sorted(set(gate))) or None,
    )


def _run_meta(res, scale, attempt, extra_samples=None) -> dict:
    r = res.residuals
    clr_n = r["obstacle_sample_min_clearance_norm"]
    clr_n = None if np.isinf(clr_n) else float(clr_n)
    clr_w = None if clr_n is None else float(clr_n * scale)
    residuals_world = {
        "deviation_max": r["deviation_max_norm"] * scale,
        "deviation_violation": r["deviation_violation_norm"] * scale,
        "curvature_max": r["curvature_max_norm"] / scale,
        "curvature_violation": r["curvature_violation_norm"] / scale,
        "obstacle_sample_min_clearance": clr_w,
    }
    residuals_norm = dict(r)
    residuals_norm["obstacle_sample_min_clearance_norm"] = clr_n
    return {
        "attempt": attempt,
        "converged": res.converged,
        "reason": res.reason,
        "iterations": res.iterations,
        "objective_history": res.objective_history,
        "residuals_norm": residuals_norm,
        "residuals_world": residuals_world,
        "warning_messages": res.warning_messages,
        "extra_samples": extra_samples or [],
    }


def _integrity(resp: dict) -> str:
    """对不含摘要字段的规范 JSON 负载计算 SHA-256（真实密码学哈希）。"""
    payload = {k: v for k, v in resp.items() if k != "integrity_sha256"}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _save_run(req: dict, resp: dict, success: bool, reason: str) -> str | None:
    """保存目标函数历史与约束残差到 runs/（失败可复现）。"""
    try:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        run_id = f"{int(time.time() * 1000):d}_{os.getpid()}_{reason}"
        path = RUNS_DIR / f"{run_id}.json"
        record = {"request": req, "success": success, "reason": reason,
                  "response": resp}
        path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        return str(path)
    except (OSError, ValueError) as exc:  # 落盘失败不得影响响应
        import sys
        print(f"[smoother] 保存运行记录失败: {exc!r}", file=sys.stderr)
        return None


def _failure(original_payload, *, reason, detail, min_clearance, scale, elapsed,
             req, save_artifact) -> dict:
    resp = {
        "success": False,
        "result": "original",
        "reason": reason,
        "points": original_payload,
        "n_points": len(original_payload),
        "endpoints_fixed": True,
        "limits": {
            "max_points": MAX_POINTS,
            "max_iter": int(req.get("max_iter", 150)),
            "max_iter_hard_limit": MAX_ITER_BUDGET,
        },
        "solve_runs": [],
        "gate_failures": [reason],
        "min_clearance_world": (
            None if min_clearance is None or np.isinf(min_clearance)
            else float(min_clearance)
        ),
        "elapsed_seconds": float(elapsed),
        "detail": detail,
    }
    resp["integrity_sha256"] = _integrity(resp)
    if save_artifact:
        _save_run(req, resp, False, reason)
    return resp
