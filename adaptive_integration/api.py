"""JSON 请求/响应接口。

请求（单个 JSON 对象，UTF-8）：

```json
{
  "expression": "exp(-x*x)",
  "a": -1.0,
  "b": 1.0,
  "eps_abs": 1e-10,
  "eps_rel": 1e-10,
  "method": "simpson",
  "max_depth": 20,
  "max_evals": 100000
}
```

仅 ``expression``、``a``、``b`` 必填，其余有默认值；出现未知字段直接拒绝。

成功响应::

    {"status": "converged", "value": 1.4936482656, "error_estimate": ..., ...}

失败响应（不收敛 / 奇点 / 请求非法，HTTP 无关；由 ``status`` 与 ``error_code`` 区分）::

    {"status": "failed"|"invalid_request", "value": null, "error_code": "...", ...}
"""

from __future__ import annotations

import math
from typing import Any

from .core import (DEFAULT_MAX_DEPTH, DEFAULT_MAX_EVALS, LIMIT_MAX_DEPTH,
                   LIMIT_MAX_EVALS, MAX_ABS_TOLERANCE, MAX_INTERVAL_SPAN,
                   MAX_REL_TOLERANCE, METHODS, integrate)
from .parser import ParseError, ParsedFunction

_REQUIRED = ("expression", "a", "b")
_KNOWN_FIELDS = frozenset({
    "expression", "a", "b", "eps_abs", "eps_rel",
    "method", "max_depth", "max_evals",
})


def _is_real_number(v: Any) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(float(v)))


def _invalid(message: str, code: str = "INVALID_REQUEST", details=None) -> dict:
    resp = {"status": "invalid_request", "converged": False, "value": None,
            "error_estimate": None, "error_code": code, "message": message}
    if details is not None:
        resp["details"] = details
    return resp


def handle_request(request: Any) -> dict:
    """处理一个已解析的 JSON 对象，返回可 ``json.dumps`` 的响应字典。"""
    if not isinstance(request, dict):
        return _invalid("请求体必须是 JSON 对象。")

    unknown = sorted(set(request) - _KNOWN_FIELDS)
    if unknown:
        return _invalid(f"存在未知字段: {unknown}；允许的字段为 "
                        f"{sorted(_KNOWN_FIELDS)}。",
                        details={"unknown_fields": unknown})

    missing = [k for k in _REQUIRED if k not in request]
    if missing:
        return _invalid(f"缺少必填字段: {missing}。",
                        details={"missing_fields": missing})

    expression = request["expression"]
    if not isinstance(expression, str) or not expression.strip():
        return _invalid("expression 必须是非空字符串。")
    a, b = request["a"], request["b"]
    if not _is_real_number(a):
        return _invalid("a 必须是有限数值（不允许 NaN/Infinity/字符串/布尔值）。")
    if not _is_real_number(b):
        return _invalid("b 必须是有限数值（不允许 NaN/Infinity/字符串/布尔值）。")
    a, b = float(a), float(b)
    if abs(b - a) > MAX_INTERVAL_SPAN:
        return _invalid(f"积分区间跨度不得超过 {MAX_INTERVAL_SPAN:g}。")

    eps_abs = request.get("eps_abs", 1.0e-8)
    eps_rel = request.get("eps_rel", 1.0e-8)
    if not _is_real_number(eps_abs) or not 0.0 <= float(eps_abs) <= MAX_ABS_TOLERANCE:
        return _invalid(f"eps_abs 必须是 [0, {MAX_ABS_TOLERANCE:g}] 内的有限数。")
    if not _is_real_number(eps_rel) or not 0.0 <= float(eps_rel) <= MAX_REL_TOLERANCE:
        return _invalid(f"eps_rel 必须是 [0, {MAX_REL_TOLERANCE:g}] 内的有限数。")
    if float(eps_abs) + float(eps_rel) <= 0.0:
        return _invalid("eps_abs 与 eps_rel 之和必须为正（不允许两者同时为 0）。")

    method = request.get("method", "simpson")
    if method not in METHODS:
        return _invalid(f"method 必须是 {list(METHODS)} 之一。",
                        details={"allowed": list(METHODS)})

    max_depth = request.get("max_depth", DEFAULT_MAX_DEPTH)
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) \
            or not 1 <= max_depth <= LIMIT_MAX_DEPTH:
        return _invalid(f"max_depth 必须是 [1, {LIMIT_MAX_DEPTH}] 内的整数。")
    max_evals = request.get("max_evals", DEFAULT_MAX_EVALS)
    if not isinstance(max_evals, int) or isinstance(max_evals, bool) \
            or not 1 <= max_evals <= LIMIT_MAX_EVALS:
        return _invalid(f"max_evals 必须是 [1, {LIMIT_MAX_EVALS}] 内的整数。")

    try:
        f = ParsedFunction(expression)
    except ParseError as exc:
        return _invalid(f"被积表达式解析失败: {exc}", code="PARSE_ERROR")

    result = integrate(
        f, a, b, eps_abs=float(eps_abs), eps_rel=float(eps_rel),
        method=method, max_depth=max_depth, max_evals=max_evals)

    response: dict[str, Any] = {
        "converged": result.converged,
        "status": "converged" if result.converged else "failed",
        "value": result.value,
        "error_estimate": result.error_estimate,
        "error_code": result.error_code,
        "message": result.message,
        "n_evals": result.n_evals,
        "n_intervals": result.n_intervals,
        "max_depth_reached": result.max_depth,
        "method": result.method,
        "details": result.details,
        "request_echo": {
            "expression": expression, "a": a, "b": b,
            "eps_abs": float(eps_abs), "eps_rel": float(eps_rel),
            "method": method, "max_depth": max_depth, "max_evals": max_evals,
        },
    }
    return response
