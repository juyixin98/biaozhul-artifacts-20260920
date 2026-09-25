"""编程接口：把表达式/数值参数编译并派发到对应驱动。

这一层负责：
* 参数校验（IntegrationConfig.validate_request）；
* 表达式安全编译（parser，绝不走 eval）；
* 反向区间（a>b）归一化后翻号；
* 退化区间（a==b）直接返回 0；
* 把解析错误翻译为结构化失败结果。
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from .config import IntegrationConfig, IntegratorError
from .core import gk15_adaptive, simpson_adaptive
from .parser import compile_expression, ParseError
from .result import (
    IntegrationResult,
    STATUS_CONVERGED,
    STATUS_FAILED,
    ERROR_MESSAGES,
)


def _failure(code: str, method: str = "", detail: str = "") -> IntegrationResult:
    message = ERROR_MESSAGES[code]
    if detail:
        message = f"{message}（{detail}）"
    return IntegrationResult(
        status=STATUS_FAILED,
        error_code=code,
        error_message=message,
        method=method,
    )


def integrate(
    expression: str,
    a: float,
    b: float,
    *,
    method: str = "gk15",
    abs_tol: float = 1e-10,
    rel_tol: float = 1e-8,
    max_depth: int = 60,
    max_evaluations: int = 100_000,
    initial_intervals: int = 1,
    points: Optional[Sequence[float]] = None,
) -> IntegrationResult:
    """对表达式 ``expression`` 从 ``a`` 到 ``b`` 做自适应积分。

    任何错误都返回 ``converged=False`` 的结构化结果（不抛出），
    唯一例外是编程调用时传入了错误类型的参数会触发 IntegratorError
    的子类 TypeError 路径——这里统一捕获后同样转为失败结果。
    """

    try:
        cfg = IntegrationConfig.validate_request(
            expression,
            a,
            b,
            method=method,
            abs_tol=abs_tol,
            rel_tol=rel_tol,
            max_depth=max_depth,
            max_evaluations=max_evaluations,
            initial_intervals=initial_intervals,
            points=points,
        )
    except IntegratorError as ex:
        return _failure("INVALID_REQUEST", method, str(ex))

    try:
        f = compile_expression(expression)
    except ParseError as ex:
        return _failure("PARSE_ERROR", method, str(ex))

    # 浮点下 a==b 直接为 0（含 [a, a+tiny] 这种退化情形）
    if a == b or abs(float(b) - float(a)) == 0.0:
        return IntegrationResult(
            status=STATUS_CONVERGED,
            value=0.0,
            error_estimate=0.0,
            method=cfg.method,
            evaluations=0,
            depth_reached=0,
            intervals=0,
        )

    sign = 1.0
    lo, hi = a, b
    if lo > hi:
        lo, hi = b, a
        sign = -1.0

    if cfg.method == "gk15":
        result = gk15_adaptive(f, lo, hi, cfg)
    else:
        result = simpson_adaptive(f, lo, hi, cfg)

    if sign < 0 and result.value is not None and result.value == result.value:
        result.value = -result.value
    return result


def integrate_request(request: dict[str, Any]) -> IntegrationResult:
    """接受已解析的 JSON dict（字段名见 README），返回结果。"""

    if not isinstance(request, dict):
        return _failure("INVALID_REQUEST", detail="请求体必须是 JSON 对象")

    expression = request.get("expression")
    a = request.get("a")
    b = request.get("b")
    if expression is None or a is None or b is None:
        return _failure(
            "INVALID_REQUEST",
            request.get("method", ""),
            "缺少必填字段：expression、a、b",
        )

    kwargs: dict[str, Any] = {}
    for key in (
        "method",
        "abs_tol",
        "rel_tol",
        "max_depth",
        "max_evaluations",
        "initial_intervals",
        "points",
    ):
        if key in request:
            kwargs[key] = request[key]

    return integrate(expression, a, b, **kwargs)
