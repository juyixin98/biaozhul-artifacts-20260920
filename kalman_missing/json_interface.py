"""JSON 字典接口：run_request(request: dict) -> dict。

请求格式
--------
{
  "model":   {"F": [[...]], "Q": [[...]], "H": [[...]], "R": [[...]]},
  "initial": {"x": [...], "P": [[...]]},
  "options": {"sym_tol": 1e-10, "psd_tol": 1e-12, "rcond": 1e-12},   // 可选
  "steps":   [{"z": [1.0, null]}, {"z": null}, ...]                  // null = 缺测
}

响应格式
--------
成功：
{
  "status": "ok",
  "estimates": [
    {"step": 1, "status": "ok", "x": [...], "P": [[...]],
     "missing": [1], "n_observed": 1, "cond_S": 12.3,
     "sym_err": 1e-17, "min_eig_P": 0.5},
    ...
  ]
}
失败（输入非法或数值失败）：
{"status": "error", "error": {"code": "...", "message": "..."}}
"""

import numpy as np

from .exceptions import KalmanError, KalmanInputError
from .filter import KalmanFilter, Tolerances
from .validation import check_num_steps


def run_request(request):
    """执行一个滤波请求，返回响应字典（所有数组转为 JSON 可序列化列表）。"""
    try:
        return _run(request)
    except KalmanError as exc:
        return {"status": "error", "error": {"code": exc.code, "message": str(exc)}}
    except (TypeError, KeyError, ValueError) as exc:
        return {
            "status": "error",
            "error": {"code": "invalid_input", "message": f"请求格式错误: {exc}"},
        }


def _run(request):
    if not isinstance(request, dict):
        raise KalmanInputError("请求必须是 JSON 对象")
    for key in ("model", "initial", "steps"):
        if key not in request:
            raise KalmanInputError(f"请求缺少必需字段 '{key}'")

    model = request["model"]
    initial = request["initial"]
    for key in ("F", "Q", "H", "R"):
        if key not in model:
            raise KalmanInputError(f"model 缺少必需字段 '{key}'")
    for key in ("x", "P"):
        if key not in initial:
            raise KalmanInputError(f"initial 缺少必需字段 '{key}'")

    opts = request.get("options") or {}
    tolerances = Tolerances(
        sym_tol=opts.get("sym_tol", 1e-10),
        psd_tol=opts.get("psd_tol", 1e-12),
        rcond=opts.get("rcond", 1e-12),
    )

    kf = KalmanFilter(
        F=model["F"], Q=model["Q"], H=model["H"], R=model["R"],
        x0=initial["x"], P0=initial["P"], tolerances=tolerances,
    )

    steps = request["steps"]
    if not isinstance(steps, list):
        raise KalmanInputError("steps 必须是数组")
    check_num_steps(len(steps))

    estimates = []
    for k, step_req in enumerate(steps, start=1):
        if step_req is None:
            z = None
        elif isinstance(step_req, dict):
            z = step_req.get("z")
        else:
            raise KalmanInputError(f"steps[{k - 1}] 必须是对象或 null")
        result = kf.step(z)
        estimates.append(
            {
                "step": result.step,
                "status": result.status,
                "x": _to_list(result.x),
                "P": _to_list(result.P),
                "missing": result.missing,
                "n_observed": result.n_observed,
                "cond_S": _finite_or_none(result.cond_S),
                "sym_err": result.sym_err,
                "min_eig_P": result.min_eig_P,
            }
        )
    return {"status": "ok", "estimates": estimates}


def _to_list(arr):
    return np.asarray(arr, dtype=float).tolist()


def _finite_or_none(v):
    return float(v) if np.isfinite(v) else None
