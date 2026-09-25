"""JSON 接口层：请求/响应均为可 json.dumps 的纯字典。

请求格式::

    {
      "method": "huber" | "ols",          // 可选，默认 "huber"
      "X": [[..], ..],                    // 必填，n×p
      "y": [..],                          // 必填，长度 n
      "delta": "auto" 或正数,             // 可选，仅 huber，默认 "auto"
      "reg_lambda": 0.0,                  // 可选，默认 0.0
      "tol": 1e-7,                        // 可选，默认 1e-7
      "max_iter": 100,                    // 可选，默认 100
      "fit_intercept": true               // 可选，默认 true
    }

成功响应::

    {"ok": true, "method": ..., "result": { ... }}

失败响应（HTTP/CLI 无关，用 ok 与 error 字段表达）::

    {"ok": false, "method": ..., "error": {"type": "input_error"|"numerical_error",
                                           "message": "..."}}
"""

from __future__ import annotations

from typing import Any

from .huber import fit_huber, fit_ols
from .validation import InputError, NumericalError

_DEFAULT_DELTA = "auto"
_DEFAULT_REG_LAMBDA = 0.0
_DEFAULT_TOL = 1e-7
_DEFAULT_MAX_ITER = 100
_DEFAULT_FIT_INTERCEPT = True

_ALLOWED_METHODS = ("huber", "ols")


def _require_dict(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise InputError("请求体必须是 JSON 对象")
    return payload


def fit_from_json(payload: Any) -> dict:
    """解析 JSON 请求字典，返回 JSON 可序列化响应字典（不抛异常）。"""
    try:
        req = _require_dict(payload)
        method = req.get("method", "huber")
        if method not in _ALLOWED_METHODS:
            raise InputError(
                f"未知 method={method!r}，支持：{', '.join(_ALLOWED_METHODS)}"
            )
        if "X" not in req or "y" not in req:
            raise InputError("缺少必填字段 X 和/或 y")

        fit_intercept = req.get("fit_intercept", _DEFAULT_FIT_INTERCEPT)
        if not isinstance(fit_intercept, bool):
            raise InputError("fit_intercept 必须是布尔值")

        if method == "ols":
            result = fit_ols(req["X"], req["y"], fit_intercept=fit_intercept)
            return {"ok": True, "method": "ols", "result": result}

        delta = req.get("delta", _DEFAULT_DELTA)
        if isinstance(delta, bool) or not isinstance(delta, (str, int, float)):
            raise InputError("delta 必须是 \"auto\" 或正数")
        if isinstance(delta, str) and delta != "auto":
            raise InputError("delta 为字符串时只允许 \"auto\"")

        result = fit_huber(
            req["X"],
            req["y"],
            delta=delta,
            reg_lambda=req.get("reg_lambda", _DEFAULT_REG_LAMBDA),
            tol=req.get("tol", _DEFAULT_TOL),
            max_iter=req.get("max_iter", _DEFAULT_MAX_ITER),
            fit_intercept=fit_intercept,
        )
        return {"ok": True, "method": "huber", "result": result}
    except InputError as exc:
        return {
            "ok": False,
            "method": (
                payload.get("method", "huber")
                if isinstance(payload, dict)
                else "huber"
            ),
            "error": {"type": "input_error", "message": str(exc)},
        }
    except NumericalError as exc:
        return {
            "ok": False,
            "method": req.get("method", "huber"),
            "error": {"type": "numerical_error", "message": str(exc)},
        }
